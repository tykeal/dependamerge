# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for reactive behind-PR recovery after a rejected merge.

Step 5 only rebases proactively when branch protection demands
up-to-date heads.  When that probe misses (or the base moved after
it ran) and GitHub rejects the merge with the PR ``behind``, the
recovery is reactive:

- ``_handle_merge_failure``: dependabot PRs get the ``@dependabot
  rebase`` macro (signed rebase, no immediate retry) with auto-merge
  armed; other authors keep the REST ``update-branch`` + retry path.
- ``_merge_single_pr``'s not-merged classification: a behind PR left
  with auto-merge armed reports ``AUTO_MERGE_PENDING`` instead of a
  spurious failure.
"""

from unittest.mock import AsyncMock, patch

import pytest

from dependamerge.github2gerrit_detector import GitHub2GerritDetectionResult
from dependamerge.merge_manager import MergeStatus
from dependamerge.models import PullRequestInfo
from tests.conftest import make_merge_manager

_BEHIND_PR = PullRequestInfo(
    number=88,
    node_id="PR_kwDOTestNode88",
    title="Chore: Bump zizmor from 1.4.0 to 1.5.0",
    body="Dependabot PR",
    author="dependabot[bot]",
    head_sha="abc123",
    base_branch="main",
    head_branch="dependabot/pip/zizmor-1.5.0",
    state="open",
    mergeable=True,
    mergeable_state="behind",
    behind_by=1,
    files_changed=[],
    repository_full_name="owner/repo",
    html_url="https://github.com/owner/repo/pull/88",
    reviews=[],
    review_comments=[],
)


class TestHandleMergeFailureBehind:
    """Reactive branch-refresh strategy after a rejected merge."""

    @pytest.mark.asyncio
    async def test_dependabot_behind_uses_macro_not_update_branch(self) -> None:
        """Dependabot PR → macro + auto-merge armed, no REST update."""
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.get = AsyncMock(return_value=[])  # no existing comments
        client.post_issue_comment = AsyncMock()
        client.update_branch = AsyncMock()
        arm = AsyncMock(return_value=True)

        pr = _BEHIND_PR.model_copy()
        with patch.object(mgr, "_enable_auto_merge_with_approval", arm):
            should_retry = await mgr._handle_merge_failure(pr, "owner", "repo")

        # No immediate retry: dependabot rebases asynchronously.
        assert should_retry is False
        client.post_issue_comment.assert_awaited_once_with(
            "owner", "repo", 88, "@dependabot rebase"
        )
        client.update_branch.assert_not_awaited()
        arm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dependabot_self_rebase_not_duplicated(self) -> None:
        """A dependabot self-rebase in progress → no duplicate macro."""
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.get = AsyncMock(return_value=[])
        client.post_issue_comment = AsyncMock()
        client.update_branch = AsyncMock()
        arm = AsyncMock(return_value=True)

        pr = _BEHIND_PR.model_copy(update={"body": "Dependabot is rebasing this PR"})
        with patch.object(mgr, "_enable_auto_merge_with_approval", arm):
            should_retry = await mgr._handle_merge_failure(pr, "owner", "repo")

        assert should_retry is False
        client.post_issue_comment.assert_not_awaited()
        client.update_branch.assert_not_awaited()
        arm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_dependabot_behind_uses_update_branch(self) -> None:
        """Non-dependabot bots keep the REST update-branch + retry path."""
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.update_branch = AsyncMock()

        pr = _BEHIND_PR.model_copy(update={"author": "pre-commit-ci[bot]"})
        should_retry = await mgr._handle_merge_failure(pr, "owner", "repo")

        assert should_retry is True
        client.update_branch.assert_awaited_once_with("owner", "repo", 88)

    @pytest.mark.asyncio
    async def test_no_fix_disables_recovery(self) -> None:
        """--no-fix → no macro, no update-branch, no retry."""
        mgr, client = make_merge_manager(fix_out_of_date=False)
        client.post_issue_comment = AsyncMock()
        client.update_branch = AsyncMock()

        should_retry = await mgr._handle_merge_failure(
            _BEHIND_PR.model_copy(), "owner", "repo"
        )

        assert should_retry is False
        client.post_issue_comment.assert_not_awaited()
        client.update_branch.assert_not_awaited()


class TestBehindAutoMergePendingClassification:
    """Behind + auto-merge armed after a failed merge → AUTO_MERGE_PENDING."""

    @pytest.mark.asyncio
    async def test_failed_merge_with_armed_auto_merge_reports_pending(
        self,
    ) -> None:
        mgr, client = make_merge_manager(
            preview_mode=False, fix_out_of_date=True, merge_timeout=0.1
        )
        pr = _BEHIND_PR.model_copy()

        # Behind + non-strict → Step 5 skips the rebase and Step 5.5
        # skips the wait; the direct merge attempt fails, and the
        # reactive recovery (exercised separately above) armed
        # auto-merge for this PR.
        client.requires_strict_status_checks = AsyncMock(return_value=False)
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "behind",
                "state": "open",
                "merged": False,
            }
        )
        client.get_required_status_checks = AsyncMock(return_value=[])

        # The reactive recovery arms auto-merge *during* the failed
        # merge attempt (inside ``_merge_pr_with_retry`` →
        # ``_handle_merge_failure``), so mimic that here: the key is
        # NOT armed when the Step 6 skip gate runs, only afterwards.
        async def _fail_and_arm(
            pr_info: PullRequestInfo, owner: str, repo: str
        ) -> bool:
            mgr._auto_merge_enabled.add("owner/repo#88")
            return False

        no_g2g = GitHub2GerritDetectionResult()
        with (
            patch.object(
                mgr,
                "_detect_github2gerrit",
                new_callable=AsyncMock,
                return_value=no_g2g,
            ),
            patch.object(
                mgr,
                "_get_merge_method_for_repo",
                new_callable=AsyncMock,
                return_value="merge",
            ),
            patch.object(
                mgr,
                "_trigger_stale_precommit_ci",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr,
                "_check_merge_requirements",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
            patch.object(
                mgr,
                "_approve_pr",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                mgr,
                "_approve_and_retry_if_review_required",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                side_effect=_fail_and_arm,
            ),
        ):
            result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.AUTO_MERGE_PENDING
        assert result.error is not None
        assert "behind" in result.error
