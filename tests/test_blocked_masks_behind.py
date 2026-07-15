# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the blocked-masks-behind handling in Step 5.

GitHub's ``mergeable_state`` is a single value, so ``blocked`` (a
failing required check) masks ``behind`` (a stale head).  When a
required check failed against a head that predates fixes on the base
branch, only a rebase re-runs the check against current base.  These
tests cover:

- ``_block_reason_indicates_check_blockage`` classification
- ``_blocked_pr_needs_rebase`` staleness probe (reason gate, compare
  gate, error handling, probe ordering)
- Step 5 routing: a blocked-but-stale PR takes the rebase path in
  ``_merge_single_pr``
- ``GitHubAsync.get_behind_by`` compare-API parsing
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from dependamerge.github2gerrit_detector import GitHub2GerritDetectionResult
from dependamerge.merge_manager import AsyncMergeManager, MergeStatus
from dependamerge.models import PullRequestInfo
from dependamerge.rebase import Step5Outcome
from tests.conftest import make_merge_manager

_BLOCKED_PR = PullRequestInfo(
    number=92,
    node_id="PR_kwDOTestNode92",
    title="Chore: Bump release-drafter from 7.4.0 to 7.5.1",
    body="Dependabot PR",
    author="dependabot[bot]",
    head_sha="feedfacecafe",
    base_branch="main",
    head_branch="dependabot/github_actions/release-drafter-7.5.1",
    state="open",
    mergeable=True,
    mergeable_state="blocked",
    behind_by=None,
    files_changed=[],
    repository_full_name="owner/repo",
    html_url="https://github.com/owner/repo/pull/92",
    reviews=[],
    review_comments=[],
)


# ---------------------------------------------------------------------------
# _block_reason_indicates_check_blockage
# ---------------------------------------------------------------------------


class TestBlockReasonIndicatesCheckBlockage:
    """Classification of check-related vs unrelated block reasons."""

    @pytest.mark.parametrize(
        "reason",
        [
            "Blocked by failing check: Zizmor Scan 🌈",
            "Blocked by 3 failing checks",
            "Blocked by missing required status: pre-commit.ci - pr",
            "Blocked by 2 missing required statuses: a, b",
            "Blocked by pending required check: DCO",
            "Blocked by 2 pending required checks: a, b",
        ],
    )
    def test_check_related_reasons_match(self, reason: str) -> None:
        assert AsyncMergeManager._block_reason_indicates_check_blockage(reason)

    @pytest.mark.parametrize(
        "reason",
        [
            None,
            "Blocked by branch protection (requires approval)",
            "Human reviewer requested changes",
            "Blocked by 2 unresolved Copilot reviews",
            "Blocked by 4 unresolved Copilot comments",
            "Blocked by repository ruleset (no specific failing condition detected)",
        ],
    )
    def test_unrelated_reasons_do_not_match(self, reason: str | None) -> None:
        assert not AsyncMergeManager._block_reason_indicates_check_blockage(reason)


# ---------------------------------------------------------------------------
# _blocked_pr_needs_rebase
# ---------------------------------------------------------------------------


class TestBlockedPrNeedsRebase:
    """Staleness probe for blocked PRs.

    The block reason itself is analysed once in ``_merge_single_pr``
    and passed in, so the probe receives it as an argument and only
    spends API budget on the compare call.
    """

    @pytest.mark.asyncio
    async def test_failing_check_and_behind_triggers_rebase(self) -> None:
        """Failing check + demonstrably behind → rebase path."""
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.get_behind_by = AsyncMock(return_value=2)
        pr = _BLOCKED_PR.model_copy()

        assert await mgr._blocked_pr_needs_rebase(
            pr, "owner", "repo", "Blocked by failing check: Zizmor Scan \U0001f308"
        )
        # The probe records the evidence on the PR for later steps.
        assert pr.behind_by == 2

    @pytest.mark.asyncio
    async def test_up_to_date_head_does_not_rebase(self) -> None:
        """Failing check but head not behind → no rebase."""
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.get_behind_by = AsyncMock(return_value=0)

        assert not await mgr._blocked_pr_needs_rebase(
            _BLOCKED_PR.model_copy(),
            "owner",
            "repo",
            "Blocked by failing check: Zizmor Scan \U0001f308",
        )

    @pytest.mark.asyncio
    async def test_unknown_staleness_does_not_rebase(self) -> None:
        """Compare failure (None) counts as not behind, not as behind."""
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.get_behind_by = AsyncMock(return_value=None)

        assert not await mgr._blocked_pr_needs_rebase(
            _BLOCKED_PR.model_copy(),
            "owner",
            "repo",
            "Blocked by failing check: Zizmor Scan \U0001f308",
        )

    @pytest.mark.asyncio
    async def test_non_check_blockage_skips_compare_probe(self) -> None:
        """Approval blockage → False without spending the compare call."""
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.get_behind_by = AsyncMock(return_value=5)

        assert not await mgr._blocked_pr_needs_rebase(
            _BLOCKED_PR.model_copy(),
            "owner",
            "repo",
            "Blocked by branch protection (requires approval)",
        )
        client.get_behind_by.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pending_checks_do_not_rebase(self) -> None:
        """Pending checks resolve on their own — never rebase for them.

        A rebase restarts every required check, and in a same-repo
        batch each sibling merge advances the base again — so
        rebasing a PR whose checks are merely *pending* causes the
        CI-restart churn this probe exists to avoid.
        """
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.get_behind_by = AsyncMock(return_value=5)

        assert not await mgr._blocked_pr_needs_rebase(
            _BLOCKED_PR.model_copy(),
            "owner",
            "repo",
            "Blocked by pending required check: DCO",
        )
        client.get_behind_by.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_analysis_does_not_rebase(self) -> None:
        """``None`` block reason (analysis empty) → not rebasable."""
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.get_behind_by = AsyncMock(return_value=5)

        assert not await mgr._blocked_pr_needs_rebase(
            _BLOCKED_PR.model_copy(), "owner", "repo", None
        )
        client.get_behind_by.assert_not_awaited()


# ---------------------------------------------------------------------------
# Step 5 routing in _merge_single_pr
# ---------------------------------------------------------------------------


def _step5_patches(mgr: AsyncMergeManager) -> list:
    """Common patches to drive _merge_single_pr up to Step 5."""
    no_g2g = GitHub2GerritDetectionResult()
    return [
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
    ]


class TestStep5BlockedMasksBehind:
    """Blocked-but-stale PRs take the Step 5 rebase path."""

    @pytest.mark.asyncio
    async def test_blocked_stale_pr_enters_rebase_path(self) -> None:
        """blocked + failing check + behind → perform_step5_rebase runs."""
        mgr, client = make_merge_manager(
            preview_mode=False, fix_out_of_date=True, merge_timeout=0.1
        )
        client.get = AsyncMock(return_value={})
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by failing check: Zizmor Scan 🌈"
        )
        client.get_behind_by = AsyncMock(return_value=3)

        pr = _BLOCKED_PR.model_copy()
        # Returning failed=True makes _merge_single_pr bail right
        # after the rebase call, keeping the test scoped to routing.
        outcome = Step5Outcome(failed=True, error_message="rebase failed")
        with ExitStack() as stack:
            for p in _step5_patches(mgr):
                stack.enter_context(p)
            mock_rebase = stack.enter_context(
                patch(
                    "dependamerge.merge_manager.rebase.perform_step5_rebase",
                    new_callable=AsyncMock,
                    return_value=outcome,
                )
            )
            result = await mgr._merge_single_pr(pr)

        mock_rebase.assert_awaited_once()
        assert result.status == MergeStatus.FAILED
        assert result.error == "rebase failed"

    @pytest.mark.asyncio
    async def test_blocked_pr_without_fix_skips_probe(self) -> None:
        """--no-fix disables the staleness probe entirely."""
        mgr, client = make_merge_manager(
            preview_mode=False, fix_out_of_date=False, merge_timeout=0.1
        )
        client.get = AsyncMock(return_value={})
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by failing check: Zizmor Scan 🌈"
        )
        client.get_behind_by = AsyncMock(return_value=3)

        pr = _BLOCKED_PR.model_copy()
        with ExitStack() as stack:
            for p in _step5_patches(mgr):
                stack.enter_context(p)
            mock_rebase = stack.enter_context(
                patch(
                    "dependamerge.merge_manager.rebase.perform_step5_rebase",
                    new_callable=AsyncMock,
                    return_value=Step5Outcome(),
                )
            )
            stack.enter_context(
                patch.object(
                    mgr,
                    "_merge_pr_with_retry",
                    new_callable=AsyncMock,
                    return_value=True,
                )
            )
            await mgr._merge_single_pr(pr)

        mock_rebase.assert_not_awaited()
        client.get_behind_by.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocked_pr_not_behind_skips_rebase(self) -> None:
        """blocked + failing check but up-to-date → no rebase path."""
        mgr, client = make_merge_manager(
            preview_mode=False, fix_out_of_date=True, merge_timeout=0.1
        )
        client.get = AsyncMock(return_value={})
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by failing check: Zizmor Scan 🌈"
        )
        client.get_behind_by = AsyncMock(return_value=0)

        pr = _BLOCKED_PR.model_copy()
        with ExitStack() as stack:
            for p in _step5_patches(mgr):
                stack.enter_context(p)
            mock_rebase = stack.enter_context(
                patch(
                    "dependamerge.merge_manager.rebase.perform_step5_rebase",
                    new_callable=AsyncMock,
                    return_value=Step5Outcome(),
                )
            )
            stack.enter_context(
                patch.object(
                    mgr,
                    "_merge_pr_with_retry",
                    new_callable=AsyncMock,
                    return_value=True,
                )
            )
            await mgr._merge_single_pr(pr)

        mock_rebase.assert_not_awaited()


# ---------------------------------------------------------------------------
# GitHubAsync.get_behind_by
# ---------------------------------------------------------------------------


class TestGetBehindBy:
    """Compare-API parsing in GitHubAsync.get_behind_by."""

    def _client(self):  # type: ignore[no-untyped-def]
        from dependamerge.github_async import GitHubAsync

        return GitHubAsync(token="test-token")

    @pytest.mark.asyncio
    async def test_returns_behind_by_from_compare(self) -> None:
        client = self._client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"behind_by": 4, "ahead_by": 1},
        ) as mock_get:
            behind = await client.get_behind_by("owner", "repo", "main", "feedface")
        assert behind == 4
        mock_get.assert_awaited_once_with("/repos/owner/repo/compare/main...feedface")

    @pytest.mark.asyncio
    async def test_encodes_base_ref_with_slash(self) -> None:
        client = self._client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"behind_by": 2, "ahead_by": 0},
        ) as mock_get:
            behind = await client.get_behind_by(
                "owner", "repo", "release/1.2", "feedface"
            )
        assert behind == 2
        mock_get.assert_awaited_once_with(
            "/repos/owner/repo/compare/release%2F1.2...feedface"
        )

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self) -> None:
        client = self._client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            assert (
                await client.get_behind_by("owner", "repo", "main", "feedface") is None
            )

    @pytest.mark.asyncio
    async def test_returns_none_on_unexpected_payload(self) -> None:
        client = self._client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"behind_by": "not-an-int"},
        ):
            assert (
                await client.get_behind_by("owner", "repo", "main", "feedface") is None
            )
