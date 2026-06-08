# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for repo-scoped mergeability conflict detection in ``AsyncMergeManager``.

In a repo-scoped batch the PR list is fetched once up front, so a
worker can act on a stale snapshot: merging one PR may make a sibling
PR ``dirty`` (a ``uv.lock`` / workflow-pin conflict) before its own
merge is dispatched.  Two complementary checks catch this:

* ``_is_pr_dirty_now`` is the **pre-dispatch** check — a single GET
  run *inside* the per-repo dispatch lock (the only point ordered
  after any sibling merge), immediately before the merge dispatch, so
  a freshly-conflicted PR skips the doomed merge entirely.
* ``_refresh_pr_mergeability`` is the **post-failure** check — it
  polls GitHub's recompute window and therefore runs only *after* a
  failed merge attempt and *off* the dispatch lock, catching a PR that
  turned ``dirty`` during our own merge window instead of producing a
  misleading "Failed to merge after all retry attempts".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from dependamerge.github2gerrit_detector import GitHub2GerritDetectionResult
from dependamerge.merge_manager import MergeStatus
from dependamerge.models import PullRequestInfo
from tests.conftest import make_merge_manager


def _make_pr(
    *,
    state: str = "open",
    mergeable: bool | None = True,
    mergeable_state: str | None = "clean",
    author: str = "renovate[bot]",
    number: int = 425,
    repo: str = "lfreleng-actions/lftools-uv",
) -> PullRequestInfo:
    """Build a minimal ``PullRequestInfo`` for refresh tests."""
    return PullRequestInfo(
        number=number,
        node_id="PR_kwDOTestNode",
        title="Chore: Bump safety from 3.7.0 to 3.8.1",
        body="bump",
        author=author,
        head_sha="cafe" * 10,
        base_branch="main",
        head_branch="dependabot/uv/safety-3.8.1",
        state=state,
        mergeable=mergeable,
        mergeable_state=mergeable_state,
        behind_by=None,
        files_changed=[],
        repository_full_name=repo,
        html_url=f"https://github.com/{repo}/pull/{number}",
        reviews=[],
        review_comments=[],
    )


class TestRefreshPrMergeability:
    """Direct unit tests for ``_refresh_pr_mergeability``."""

    @pytest.mark.asyncio
    async def test_concrete_dirty_updates_in_one_get(self) -> None:
        """A concrete ``dirty`` state is recorded with a single GET."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            return_value={
                "state": "open",
                "mergeable": False,
                "mergeable_state": "dirty",
                "head": {"sha": "newsha123"},
            }
        )
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        await mgr._refresh_pr_mergeability(pr, "owner", "repo")

        assert pr.mergeable_state == "dirty"
        assert pr.mergeable is False
        assert pr.head_sha == "newsha123"
        client.get.assert_awaited_once_with("/repos/owner/repo/pulls/425")

    @pytest.mark.asyncio
    async def test_concrete_clean_updates_in_one_get(self) -> None:
        """A concrete ``clean`` state is recorded with a single GET."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            return_value={
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
            }
        )
        pr = _make_pr(mergeable=None, mergeable_state="unknown")

        await mgr._refresh_pr_mergeability(pr, "owner", "repo")

        assert pr.mergeable_state == "clean"
        assert pr.mergeable is True
        client.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_polls_while_computing_then_settles(self) -> None:
        """While GitHub recomputes (``mergeable=null``) we poll until concrete."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            side_effect=[
                # GitHub is still recomputing after the base moved.
                {"state": "open", "mergeable": None, "mergeable_state": "unknown"},
                # Settled: the sibling merge created a conflict.
                {"state": "open", "mergeable": False, "mergeable_state": "dirty"},
            ]
        )
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        with patch(
            "dependamerge.merge_manager.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            await mgr._refresh_pr_mergeability(pr, "owner", "repo")

        assert pr.mergeable_state == "dirty"
        assert client.get.await_count == 2
        mock_sleep.assert_awaited()

    @pytest.mark.asyncio
    async def test_timeout_records_latest_known_state(self, monkeypatch) -> None:
        """When GitHub never settles, record its latest answer and return."""
        # Zero timeout: the deadline passes on the first poll iteration.
        monkeypatch.setattr(
            "dependamerge.merge_manager.MERGEABILITY_REFRESH_TIMEOUT_SECONDS",
            0.0,
        )
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            return_value={
                "state": "open",
                "mergeable": None,
                "mergeable_state": "unknown",
            }
        )
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        await mgr._refresh_pr_mergeability(pr, "owner", "repo")

        # The latest concrete-ish value GitHub gave us is recorded so
        # downstream logic does not act on the older snapshot.
        assert pr.mergeable_state == "unknown"
        client.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closed_state_short_circuits(self) -> None:
        """A closed PR is recorded and returns without polling."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            return_value={
                "state": "closed",
                "mergeable": None,
                "mergeable_state": "unknown",
            }
        )
        pr = _make_pr(state="open")

        await mgr._refresh_pr_mergeability(pr, "owner", "repo")

        assert pr.state == "closed"
        client.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_client_is_noop(self) -> None:
        """With no GitHub client the snapshot is left untouched."""
        mgr, _client = make_merge_manager()
        mgr._github_client = None
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        await mgr._refresh_pr_mergeability(pr, "owner", "repo")

        assert pr.mergeable_state == "clean"

    @pytest.mark.asyncio
    async def test_api_error_leaves_snapshot_untouched(self) -> None:
        """A failed refresh must not blank out the existing snapshot."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        await mgr._refresh_pr_mergeability(pr, "owner", "repo")

        assert pr.mergeable_state == "clean"
        assert pr.mergeable is True

    @pytest.mark.asyncio
    async def test_non_dict_payload_is_ignored(self) -> None:
        """A non-dict payload degrades to a no-op rather than crashing."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(return_value=["unexpected"])
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        await mgr._refresh_pr_mergeability(pr, "owner", "repo")

        assert pr.mergeable_state == "clean"


class TestDispatchRefreshSkipsConflict:
    """``_merge_single_pr`` dispatch-time refresh + dirty-skip behaviour."""

    @staticmethod
    def _patches(mgr):
        """Patch the manager methods that run before the dispatch lock."""
        return (
            patch.object(
                mgr,
                "_detect_github2gerrit",
                new_callable=AsyncMock,
                return_value=GitHub2GerritDetectionResult(),
            ),
            patch.object(
                mgr,
                "_get_merge_method_for_repo",
                new_callable=AsyncMock,
                return_value="merge",
            ),
            patch.object(
                mgr,
                "_check_merge_requirements",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
            patch.object(
                mgr, "_approve_pr", new_callable=AsyncMock, return_value=False
            ),
        )

    @pytest.mark.asyncio
    async def test_fresh_conflict_skips_merge_and_reports_conflict(self) -> None:
        """A conflict revealed at dispatch skips the doomed merge call."""
        mgr, client = make_merge_manager(repo_scoped=True)
        mgr._post_approval_delay = 0.0
        # Snapshot says clean (passes early eligibility); the live
        # refresh at dispatch reveals the sibling-merge conflict.
        client.get = AsyncMock(
            return_value={
                "state": "open",
                "mergeable": False,
                "mergeable_state": "dirty",
                "head": {"sha": "conflictsha"},
            }
        )
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        p1, p2, p3, p4 = self._patches(mgr)
        with (
            p1,
            p2,
            p3,
            p4,
            patch.object(
                mgr, "_merge_pr_with_retry", new_callable=AsyncMock
            ) as mock_merge,
        ):
            result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.FAILED
        # The doomed merge API call must be skipped entirely.
        mock_merge.assert_not_called()

    @pytest.mark.asyncio
    async def test_clean_after_refresh_proceeds_to_merge(self) -> None:
        """When the refresh confirms ``clean`` the merge proceeds normally."""
        mgr, client = make_merge_manager(repo_scoped=True)
        mgr._post_approval_delay = 0.0
        client.get = AsyncMock(
            return_value={
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
            }
        )
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        p1, p2, p3, p4 = self._patches(mgr)
        with (
            p1,
            p2,
            p3,
            p4,
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge,
        ):
            result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.MERGED
        mock_merge.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_org_scoped_run_does_not_refresh(self) -> None:
        """Outside repo-scoped mode the dispatch-time refresh is skipped."""
        mgr, client = make_merge_manager(repo_scoped=False)
        mgr._post_approval_delay = 0.0
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        p1, p2, p3, p4 = self._patches(mgr)
        with (
            p1,
            p2,
            p3,
            p4,
            patch.object(
                mgr, "_refresh_pr_mergeability", new_callable=AsyncMock
            ) as mock_refresh,
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.MERGED
        mock_refresh.assert_not_called()
