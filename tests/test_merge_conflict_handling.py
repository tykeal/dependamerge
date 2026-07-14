# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for merge-conflict recovery in ``AsyncMergeManager``.

When a PR is ``dirty`` (a real merge conflict), it has no merge path
of its own.  ``_handle_merge_conflict`` recovers dependabot PRs by
posting ``@dependabot rebase`` (which regenerates lockfiles and
re-signs the commit), then waits \u2014 bounded by ``merge_timeout`` \u2014 for
the rebase and required checks to land, approving the rebased commit
and enabling auto-merge.  Any other author is reported and failed fast
(no wait), since there is no automated way to clear a content conflict.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from dependamerge.merge_manager import MergeResult, MergeStatus
from dependamerge.models import PullRequestInfo
from tests.conftest import make_merge_manager


def _make_pr(
    *,
    author: str = "dependabot[bot]",
    mergeable_state: str | None = "dirty",
    number: int = 425,
    repo: str = "lfreleng-actions/lftools-uv",
) -> PullRequestInfo:
    """Build a conflicted ``PullRequestInfo`` for conflict tests."""
    return PullRequestInfo(
        number=number,
        node_id="PR_kwDOTestNode",
        title="Chore: Bump safety from 3.7.0 to 3.8.1",
        body="bump",
        author=author,
        head_sha="cafe" * 10,
        base_branch="main",
        head_branch="dependabot/uv/safety-3.8.1",
        state="open",
        mergeable=False,
        mergeable_state=mergeable_state,
        behind_by=None,
        files_changed=[],
        repository_full_name=repo,
        html_url=f"https://github.com/{repo}/pull/{number}",
        reviews=[],
        review_comments=[],
    )


def _result(pr: PullRequestInfo) -> MergeResult:
    return MergeResult(pr_info=pr, status=MergeStatus.PENDING)


class TestRequestDependabotRebase:
    """Unit tests for ``_request_dependabot_rebase``."""

    @pytest.mark.asyncio
    async def test_posts_when_no_existing_comment(self) -> None:
        mgr, client = make_merge_manager()
        client.get = AsyncMock(return_value=[])
        client.post_issue_comment = AsyncMock()
        pr = _make_pr()

        ok = await mgr._request_dependabot_rebase(pr, "owner", "repo")

        assert ok is True
        client.post_issue_comment.assert_awaited_once_with(
            "owner", "repo", 425, "@dependabot rebase"
        )

    @pytest.mark.asyncio
    async def test_skips_when_rebase_comment_exists(self) -> None:
        """An existing rebase request is treated as in-flight (no dup)."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(return_value=[{"body": "@dependabot rebase"}])
        client.post_issue_comment = AsyncMock()
        pr = _make_pr()

        ok = await mgr._request_dependabot_rebase(pr, "owner", "repo")

        assert ok is True
        client.post_issue_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_when_comment_listing_fails(self) -> None:
        """If we cannot list comments, post anyway (dup is harmless)."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        client.post_issue_comment = AsyncMock()
        pr = _make_pr()

        ok = await mgr._request_dependabot_rebase(pr, "owner", "repo")

        assert ok is True
        client.post_issue_comment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_post_fails(self) -> None:
        mgr, client = make_merge_manager()
        client.get = AsyncMock(return_value=[])
        client.post_issue_comment = AsyncMock(side_effect=RuntimeError("nope"))
        pr = _make_pr()

        ok = await mgr._request_dependabot_rebase(pr, "owner", "repo")

        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_false_without_client(self) -> None:
        mgr, _client = make_merge_manager()
        mgr._github_client = None

        ok = await mgr._request_dependabot_rebase(_make_pr(), "o", "r")

        assert ok is False


class TestHandleMergeConflict:
    """Outcome tests for ``_handle_merge_conflict``."""

    @pytest.mark.asyncio
    async def test_non_dependabot_fails_fast(self) -> None:
        """A non-dependabot conflict is reported and failed without a wait."""
        mgr, _client = make_merge_manager()
        pr = _make_pr(author="someuser")
        with (
            patch.object(
                mgr, "_request_dependabot_rebase", new_callable=AsyncMock
            ) as mock_rebase,
            patch.object(
                mgr, "_wait_for_auto_merge", new_callable=AsyncMock
            ) as mock_wait,
            patch.object(mgr, "log") as mock_log,
            patch.object(mgr, "_console") as mock_console,
        ):
            result = await mgr._handle_merge_conflict(
                pr, "lfreleng-actions", "lftools-uv", _result(pr)
            )

        assert result.status == MergeStatus.FAILED
        assert result.error == "merge conflicts"
        mock_rebase.assert_not_called()
        mock_wait.assert_not_called()
        # The specific 🔀 cause line is logged (not printed — real
        # merges keep the console clean; the reason reaches the user
        # via ``result.error`` in the end-of-run summary).
        assert any(
            "🔀 Merge conflict" in str(call.args[0])
            for call in mock_log.info.call_args_list
            if call.args
        )
        # ...and nothing is printed to the console for this PR.
        printed = " ".join(
            str(c.args[0]) for c in mock_console.print.call_args_list if c.args
        )
        assert "🔀 Merge conflict" not in printed
        assert "❌ Failed" not in printed

    @pytest.mark.asyncio
    async def test_rebase_request_failure_fails(self) -> None:
        mgr, _client = make_merge_manager()
        pr = _make_pr()
        with (
            patch.object(
                mgr,
                "_request_dependabot_rebase",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr, "_wait_for_auto_merge", new_callable=AsyncMock
            ) as mock_wait,
        ):
            result = await mgr._handle_merge_conflict(
                pr, "lfreleng-actions", "lftools-uv", _result(pr)
            )

        assert result.status == MergeStatus.FAILED
        assert result.error == "merge conflicts"
        mock_wait.assert_not_called()

    @pytest.mark.asyncio
    async def test_rebase_clears_and_merges(self) -> None:
        """Rebase lands (phase 1) then auto-merge closes the PR (phase 2)."""
        mgr, _client = make_merge_manager()
        pr = _make_pr()
        calls = {"n": 0}

        async def fake_wait(pr_info, owner, repo, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # Dependabot rebased: conflict cleared, now waiting checks.
                pr_info.mergeable_state = "blocked"
                return (False, False)
            # Phase 2: auto-merge fires and the PR closes merged.
            pr_info.state = "closed"
            return (True, True)

        with (
            patch.object(
                mgr,
                "_request_dependabot_rebase",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(mgr, "_wait_for_auto_merge", new=fake_wait),
            patch.object(
                mgr, "_approve_pr", new_callable=AsyncMock, return_value=True
            ) as mock_approve,
            patch.object(
                mgr,
                "_enable_auto_merge_for_pr",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_enable,
        ):
            result = await mgr._handle_merge_conflict(
                pr, "lfreleng-actions", "lftools-uv", _result(pr)
            )

        assert result.status == MergeStatus.MERGED
        # Approval happens only after the rebase cleared the conflict.
        mock_approve.assert_awaited_once()
        mock_enable.assert_awaited_once()
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_still_dirty_at_timeout_fails(self) -> None:
        """If the conflict never clears, fail with the conflict cause."""
        mgr, _client = make_merge_manager()
        pr = _make_pr()

        async def fake_wait(pr_info, owner, repo, **kwargs):
            # Phase 1 times out still dirty.
            return (False, False)

        with (
            patch.object(
                mgr,
                "_request_dependabot_rebase",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(mgr, "_wait_for_auto_merge", new=fake_wait),
            patch.object(mgr, "_approve_pr", new_callable=AsyncMock) as mock_approve,
        ):
            result = await mgr._handle_merge_conflict(
                pr, "lfreleng-actions", "lftools-uv", _result(pr)
            )

        assert result.status == MergeStatus.FAILED
        assert result.error == "merge conflicts"
        # Never approved a PR that stayed conflicted.
        mock_approve.assert_not_called()

    @pytest.mark.asyncio
    async def test_rebase_clears_but_checks_pending(self) -> None:
        """Rebase lands but checks outlast the window -> auto-merge pending."""
        mgr, _client = make_merge_manager()
        pr = _make_pr()
        calls = {"n": 0}

        async def fake_wait(pr_info, owner, repo, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                pr_info.mergeable_state = "behind"
                return (False, False)
            # Phase 2 times out without closing (checks still running).
            return (False, False)

        with (
            patch.object(
                mgr,
                "_request_dependabot_rebase",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(mgr, "_wait_for_auto_merge", new=fake_wait),
            patch.object(mgr, "_approve_pr", new_callable=AsyncMock, return_value=True),
            patch.object(
                mgr,
                "_enable_auto_merge_for_pr",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await mgr._handle_merge_conflict(
                pr, "lfreleng-actions", "lftools-uv", _result(pr)
            )

        assert result.status == MergeStatus.AUTO_MERGE_PENDING

    @pytest.mark.asyncio
    async def test_closed_without_merge_reports_closed(self) -> None:
        """A PR closed without merging during the wait reports CLOSED."""
        mgr, _client = make_merge_manager()
        pr = _make_pr()

        async def fake_wait(pr_info, owner, repo, **kwargs):
            pr_info.state = "closed"
            return (True, False)

        with (
            patch.object(
                mgr,
                "_request_dependabot_rebase",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(mgr, "_wait_for_auto_merge", new=fake_wait),
        ):
            result = await mgr._handle_merge_conflict(
                pr, "lfreleng-actions", "lftools-uv", _result(pr)
            )

        assert result.status == MergeStatus.CLOSED
        assert "closed without merging" in (result.error or "")

    @pytest.mark.asyncio
    async def test_rebase_clears_but_auto_merge_unavailable_merges_directly(
        self,
    ) -> None:
        """Rebase clears to clean but auto-merge fails -> merge directly.

        Regression for the Copilot finding: when
        ``_enable_auto_merge_for_pr`` returns False we must not return
        a misleading AUTO_MERGE_PENDING; if the PR is mergeable we
        merge it ourselves.
        """
        mgr, _client = make_merge_manager()
        pr = _make_pr()
        calls = {"n": 0}

        async def fake_wait(pr_info, owner, repo, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                pr_info.mergeable_state = "clean"
                return (False, False)
            return (False, False)

        with (
            patch.object(
                mgr,
                "_request_dependabot_rebase",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(mgr, "_wait_for_auto_merge", new=fake_wait),
            patch.object(mgr, "_approve_pr", new_callable=AsyncMock, return_value=True),
            patch.object(
                mgr,
                "_enable_auto_merge_for_pr",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge,
        ):
            result = await mgr._handle_merge_conflict(
                pr, "lfreleng-actions", "lftools-uv", _result(pr)
            )

        assert result.status == MergeStatus.MERGED
        mock_merge.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rebase_clears_but_auto_merge_unavailable_and_not_clean(
        self,
    ) -> None:
        """Auto-merge unavailable and PR not mergeable -> FAILED (not pending)."""
        mgr, _client = make_merge_manager()
        pr = _make_pr()
        calls = {"n": 0}

        async def fake_wait(pr_info, owner, repo, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                pr_info.mergeable_state = "blocked"
                return (False, False)
            return (False, False)

        with (
            patch.object(
                mgr,
                "_request_dependabot_rebase",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(mgr, "_wait_for_auto_merge", new=fake_wait),
            patch.object(mgr, "_approve_pr", new_callable=AsyncMock, return_value=True),
            patch.object(
                mgr,
                "_enable_auto_merge_for_pr",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr, "_merge_pr_with_retry", new_callable=AsyncMock
            ) as mock_merge,
        ):
            result = await mgr._handle_merge_conflict(
                pr, "lfreleng-actions", "lftools-uv", _result(pr)
            )

        # Must NOT be AUTO_MERGE_PENDING (auto-merge was never armed).
        assert result.status == MergeStatus.FAILED
        assert "auto-merge unavailable" in (result.error or "")
        mock_merge.assert_not_called()

    @pytest.mark.asyncio
    async def test_approval_failure_after_rebase_is_reported(self) -> None:
        """An approval error after the rebase is reported locally, not bubbled."""
        mgr, _client = make_merge_manager()
        pr = _make_pr()

        async def fake_wait(pr_info, owner, repo, **kwargs):
            # Rebase cleared the conflict (now blocked, awaiting review).
            pr_info.mergeable_state = "blocked"
            return (False, False)

        with (
            patch.object(
                mgr,
                "_request_dependabot_rebase",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(mgr, "_wait_for_auto_merge", new=fake_wait),
            patch.object(
                mgr,
                "_approve_pr",
                new_callable=AsyncMock,
                side_effect=RuntimeError("403 Forbidden"),
            ),
            patch.object(
                mgr, "_enable_auto_merge_for_pr", new_callable=AsyncMock
            ) as mock_enable,
        ):
            result = await mgr._handle_merge_conflict(
                pr, "lfreleng-actions", "lftools-uv", _result(pr)
            )

        assert result.status == MergeStatus.FAILED
        assert "approval failed" in (result.error or "")
        # The failure is handled before auto-merge is even attempted.
        mock_enable.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_wait_auto_merge_armed_reports_pending(self) -> None:
        """Fire-and-forget with auto-merge available -> AUTO_MERGE_PENDING."""
        mgr, _client = make_merge_manager(max_wait=0)
        assert mgr._no_wait is True
        pr = _make_pr()

        with (
            patch.object(
                mgr,
                "_request_dependabot_rebase",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(mgr, "_approve_pr", new_callable=AsyncMock, return_value=True),
            patch.object(
                mgr,
                "_enable_auto_merge_for_pr",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                mgr, "_wait_for_auto_merge", new_callable=AsyncMock
            ) as mock_wait,
        ):
            result = await mgr._handle_merge_conflict(
                pr, "lfreleng-actions", "lftools-uv", _result(pr)
            )

        assert result.status == MergeStatus.AUTO_MERGE_PENDING
        # Fire-and-forget never blocks on the wait loop.
        mock_wait.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_wait_auto_merge_unavailable_reports_blocked(self) -> None:
        """Fire-and-forget with auto-merge unavailable -> BLOCKED.

        Regression for the Copilot finding: in the ``max_wait == 0``
        path the return of ``_enable_auto_merge_for_pr`` was ignored, so
        a PR that could not arm auto-merge was still reported as
        AUTO_MERGE_PENDING even though GitHub would never merge it.
        """
        mgr, _client = make_merge_manager(max_wait=0)
        assert mgr._no_wait is True
        pr = _make_pr()

        with (
            patch.object(
                mgr,
                "_request_dependabot_rebase",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(mgr, "_approve_pr", new_callable=AsyncMock, return_value=True),
            patch.object(
                mgr,
                "_enable_auto_merge_for_pr",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr, "_wait_for_auto_merge", new_callable=AsyncMock
            ) as mock_wait,
        ):
            result = await mgr._handle_merge_conflict(
                pr, "lfreleng-actions", "lftools-uv", _result(pr)
            )

        # Must NOT be AUTO_MERGE_PENDING (auto-merge was never armed).
        assert result.status == MergeStatus.BLOCKED
        assert "auto-merge unavailable" in (result.error or "")
        mock_wait.assert_not_called()


class TestConflictRoutingFromMergeSinglePr:
    """``_merge_single_pr`` routes ``dirty`` PRs to the conflict handler."""

    @staticmethod
    def _common_patches(mgr):
        from dependamerge.github2gerrit_detector import (
            GitHub2GerritDetectionResult,
        )

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
        )

    @pytest.mark.asyncio
    async def test_snapshot_dirty_routes_before_approval(self) -> None:
        """A PR dirty at fetch goes to the handler without being approved."""
        mgr, client = make_merge_manager()
        pr = _make_pr(mergeable_state="dirty")
        client.get = AsyncMock(return_value={"state": "open"})

        p1, p2 = self._common_patches(mgr)
        with (
            p1,
            p2,
            patch.object(
                mgr,
                "_handle_merge_conflict",
                new_callable=AsyncMock,
            ) as mock_handler,
            patch.object(mgr, "_approve_pr", new_callable=AsyncMock) as mock_approve,
        ):
            mock_handler.return_value = MergeResult(
                pr_info=pr, status=MergeStatus.AUTO_MERGE_PENDING
            )
            await mgr._merge_single_pr(pr)

        mock_handler.assert_awaited_once()
        # The PR must not be approved before the rebase is requested.
        mock_approve.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_dirty_routes_to_handler(self) -> None:
        """A PR that becomes dirty mid-merge is caught after a failed merge.

        The pre-dispatch peek still sees a clean PR (so the merge is
        attempted); the merge then fails and the off-lock post-failure
        refresh reveals the conflict and routes it to recovery.
        """
        mgr, client = make_merge_manager(repo_scoped=True)
        mgr._post_approval_delay = 0.0
        # Snapshot clean -> passes early checks; the merge fails and the
        # post-failure refresh reveals the conflict.
        client.get = AsyncMock(
            return_value={
                "state": "open",
                "mergeable": False,
                "mergeable_state": "dirty",
            }
        )
        pr = _make_pr(mergeable_state="clean")
        pr.mergeable = True

        p1, p2 = self._common_patches(mgr)
        with (
            p1,
            p2,
            patch.object(
                mgr,
                "_check_merge_requirements",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
            patch.object(
                mgr, "_approve_pr", new_callable=AsyncMock, return_value=False
            ),
            # The pre-dispatch peek sees it still clean; the conflict
            # only surfaces on the post-failure refresh.
            patch.object(
                mgr, "_is_pr_dirty_now", new_callable=AsyncMock, return_value=False
            ),
            patch.object(
                mgr, "_handle_merge_conflict", new_callable=AsyncMock
            ) as mock_handler,
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_merge,
        ):
            mock_handler.return_value = MergeResult(
                pr_info=pr, status=MergeStatus.AUTO_MERGE_PENDING
            )
            await mgr._merge_single_pr(pr)

        # The merge is attempted; the failure + refresh route to recovery.
        mock_merge.assert_awaited_once()
        mock_handler.assert_awaited_once()
