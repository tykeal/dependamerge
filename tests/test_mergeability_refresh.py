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
from dependamerge.merge_manager import MergeResult, MergeStatus
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
    async def test_timeout_normalizes_empty_state_to_unknown(self, monkeypatch) -> None:
        """An empty ``mergeable_state`` at timeout is recorded as ``unknown``.

        GitHub signals "still computing" as either ``"unknown"`` or an
        empty string; both must overwrite a stale concrete snapshot so
        downstream reporting never shows a misleading ``clean`` once the
        recompute window times out.
        """
        monkeypatch.setattr(
            "dependamerge.merge_manager.MERGEABILITY_REFRESH_TIMEOUT_SECONDS",
            0.0,
        )
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            return_value={
                "state": "open",
                "mergeable": None,
                "mergeable_state": "",
            }
        )
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        await mgr._refresh_pr_mergeability(pr, "owner", "repo")

        # The empty string is normalised so the stale ``clean`` snapshot
        # is not left in place.
        assert pr.mergeable_state == "unknown"
        client.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timeout_normalizes_null_state_to_unknown(self, monkeypatch) -> None:
        """A null ``mergeable_state`` at timeout is recorded as ``unknown``.

        ``mergeable_state is None`` is a still-computing signal too; if
        GitHub returns it for the whole refresh window the stale
        concrete snapshot must still be overwritten with an honest
        ``unknown`` rather than left as a misleading ``clean``.
        """
        monkeypatch.setattr(
            "dependamerge.merge_manager.MERGEABILITY_REFRESH_TIMEOUT_SECONDS",
            0.0,
        )
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            return_value={
                "state": "open",
                "mergeable": None,
                "mergeable_state": None,
            }
        )
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        await mgr._refresh_pr_mergeability(pr, "owner", "repo")

        # A null state at timeout is normalised, not dropped.
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


class TestIsPrDirtyNow:
    """Direct unit tests for the pre-dispatch ``_is_pr_dirty_now`` peek."""

    @pytest.mark.asyncio
    async def test_concrete_dirty_returns_true_and_records_state(self) -> None:
        """A concrete ``dirty`` returns True and records current state."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            return_value={
                "state": "open",
                "mergeable": False,
                "mergeable_state": "dirty",
                "head": {"sha": "conflictsha"},
            }
        )
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        assert await mgr._is_pr_dirty_now(pr, "owner", "repo") is True
        assert pr.mergeable_state == "dirty"
        assert pr.mergeable is False
        assert pr.head_sha == "conflictsha"
        client.get.assert_awaited_once_with("/repos/owner/repo/pulls/425")

    @pytest.mark.asyncio
    async def test_concrete_clean_returns_false_without_mutating(self) -> None:
        """A concrete ``clean`` peek leaves the snapshot untouched."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            return_value={
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
            }
        )
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        assert await mgr._is_pr_dirty_now(pr, "owner", "repo") is False
        assert pr.mergeable_state == "clean"

    @pytest.mark.asyncio
    async def test_still_computing_returns_false_without_mutating(self) -> None:
        """A transient ``unknown`` must not overwrite a concrete ``clean``.

        The peek is a single GET with no recompute poll; when GitHub is
        still computing it returns False and preserves the prior
        snapshot so the transient-405-on-``clean`` retry path is kept.
        """
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            return_value={
                "state": "open",
                "mergeable": None,
                "mergeable_state": "unknown",
            }
        )
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        assert await mgr._is_pr_dirty_now(pr, "owner", "repo") is False
        assert pr.mergeable_state == "clean"
        assert pr.mergeable is True
        client.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closed_pr_returns_false(self) -> None:
        """A closed PR is not a conflict; defer to the closed-PR path."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            return_value={
                "state": "closed",
                "mergeable": None,
                "mergeable_state": "dirty",
            }
        )
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        assert await mgr._is_pr_dirty_now(pr, "owner", "repo") is False
        assert pr.mergeable_state == "clean"

    @pytest.mark.asyncio
    async def test_api_error_returns_false_without_mutating(self) -> None:
        """An errored peek degrades to False and preserves the snapshot."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        assert await mgr._is_pr_dirty_now(pr, "owner", "repo") is False
        assert pr.mergeable_state == "clean"

    @pytest.mark.asyncio
    async def test_non_dict_payload_returns_false(self) -> None:
        """A non-dict payload degrades to False rather than crashing."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(return_value=["unexpected"])
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        assert await mgr._is_pr_dirty_now(pr, "owner", "repo") is False
        assert pr.mergeable_state == "clean"

    @pytest.mark.asyncio
    async def test_no_client_returns_false(self) -> None:
        """With no GitHub client the peek is a no-op returning False."""
        mgr, _client = make_merge_manager()
        mgr._github_client = None
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        assert await mgr._is_pr_dirty_now(pr, "owner", "repo") is False
        assert pr.mergeable_state == "clean"


class TestDispatchMergeabilityRouting:
    """``_merge_single_pr`` pre-dispatch peek + post-failure refresh.

    A repo-scoped run peeks live merge state inside the dispatch lock
    (a single GET) so a sibling-conflicted PR is routed to conflict
    recovery *without* a doomed merge attempt; a PR that turns dirty
    during the merge window is still caught by the off-lock
    post-failure refresh.
    """

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
    async def test_predispatch_dirty_routes_without_merge_attempt(self) -> None:
        """A sibling conflict is caught pre-dispatch, skipping the merge.

        The pre-dispatch peek reveals the ``dirty`` state, so we route
        straight to conflict recovery without ever calling
        ``_merge_pr_with_retry`` (which would 405 and churn its retry
        loop against the stale ``clean`` snapshot).
        """
        mgr, client = make_merge_manager(repo_scoped=True)
        mgr._post_approval_delay = 0.0
        # The pre-dispatch peek reveals the sibling-merge conflict.
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
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_merge,
        ):
            result = await mgr._merge_single_pr(pr)

        # The merge is never attempted; the conflict is handled directly
        # (renovate is non-dependabot, so it fails as a plain conflict).
        mock_merge.assert_not_awaited()
        assert result.status == MergeStatus.FAILED
        assert result.error == "merge conflicts"

    @pytest.mark.asyncio
    async def test_during_window_dirty_routes_via_post_failure_refresh(self) -> None:
        """A PR that turns dirty mid-merge is caught after the failure.

        The pre-dispatch peek sees a still-clean PR (returns False), the
        merge is attempted and fails, and only then does the off-lock
        post-failure refresh reveal the fresh conflict and route it.
        """
        mgr, _client = make_merge_manager(repo_scoped=True)
        mgr._post_approval_delay = 0.0
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        async def _reveal_dirty(pr_info, _owner, _repo):
            pr_info.mergeable_state = "dirty"

        p1, p2, p3, p4 = self._patches(mgr)
        with (
            p1,
            p2,
            p3,
            p4,
            patch.object(
                mgr, "_is_pr_dirty_now", new_callable=AsyncMock, return_value=False
            ) as mock_peek,
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_merge,
            patch.object(
                mgr,
                "_refresh_pr_mergeability",
                new_callable=AsyncMock,
                side_effect=_reveal_dirty,
            ) as mock_refresh,
        ):
            result = await mgr._merge_single_pr(pr)

        mock_peek.assert_awaited_once()
        mock_merge.assert_awaited_once()
        mock_refresh.assert_awaited_once()
        assert result.status == MergeStatus.FAILED
        assert result.error == "merge conflicts"

    @pytest.mark.asyncio
    async def test_successful_merge_peeks_but_skips_poll_refresh(self) -> None:
        """A clean PR pays only the cheap peek, never the poll refresh."""
        mgr, _client = make_merge_manager(repo_scoped=True)
        mgr._post_approval_delay = 0.0
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        p1, p2, p3, p4 = self._patches(mgr)
        with (
            p1,
            p2,
            p3,
            p4,
            patch.object(
                mgr, "_is_pr_dirty_now", new_callable=AsyncMock, return_value=False
            ) as mock_peek,
            patch.object(
                mgr, "_refresh_pr_mergeability", new_callable=AsyncMock
            ) as mock_refresh,
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
        # The bounded single-GET peek runs; the heavier poll refresh
        # (reserved for the post-failure path) does not.
        mock_peek.assert_awaited_once()
        mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_org_scope_skips_peek_and_refresh(self) -> None:
        """Outside repo scope neither the peek nor the refresh runs."""
        mgr, _client = make_merge_manager(repo_scoped=False)
        mgr._post_approval_delay = 0.0
        pr = _make_pr(mergeable=True, mergeable_state="clean")

        p1, p2, p3, p4 = self._patches(mgr)
        with (
            p1,
            p2,
            p3,
            p4,
            patch.object(
                mgr, "_is_pr_dirty_now", new_callable=AsyncMock, return_value=False
            ) as mock_peek,
            patch.object(
                mgr, "_refresh_pr_mergeability", new_callable=AsyncMock
            ) as mock_refresh,
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr, "_is_pr_already_merged", new_callable=AsyncMock, return_value=False
            ),
            patch.object(
                mgr, "_report_merge_failure", new_callable=AsyncMock
            ) as mock_report,
        ):
            mock_report.return_value = MergeResult(
                pr_info=pr, status=MergeStatus.FAILED
            )
            await mgr._merge_single_pr(pr)

        # The repo_scoped gate means org runs never peek or refresh.
        mock_peek.assert_not_called()
        mock_refresh.assert_not_called()
