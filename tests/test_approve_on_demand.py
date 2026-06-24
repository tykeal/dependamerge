# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Unit tests for approve-on-demand merge behaviour.

Phase 1b moved PR approval off the unconditional up-front path onto two
on-demand triggers:

1. ``_approve_and_retry_if_review_required`` — after a direct merge is
   rejected *specifically* because our review is missing, approve the
   current head and retry the merge once.
2. ``_enable_auto_merge_with_approval`` — approve the current head (if
   needed) before arming auto-merge, so auto-merge is not left waiting on
   a missing review.

These tests exercise the helpers directly (so they are independent of the
large ``_merge_single_pr`` orchestration) plus the helpers' integration
points.
"""

from unittest.mock import AsyncMock, patch

import pytest

from dependamerge.models import PullRequestInfo
from tests.conftest import make_merge_manager

_BLOCKED_PR = PullRequestInfo(
    number=42,
    node_id="PR_kwDOTestNode42",
    title="Bump foo from 1.0 to 2.0",
    body="Dependabot PR",
    author="dependabot[bot]",
    head_sha="abc123def456",
    base_branch="main",
    head_branch="dependabot/pip/foo-2.0",
    state="open",
    mergeable=True,
    mergeable_state="blocked",
    behind_by=0,
    files_changed=[],
    repository_full_name="owner/repo",
    html_url="https://github.com/owner/repo/pull/42",
    reviews=[],
    review_comments=[],
)


# ---------------------------------------------------------------------------
# Trigger 1: _approve_and_retry_if_review_required
# ---------------------------------------------------------------------------


class TestApproveAndRetryIfReviewRequired:
    """Approve-on-demand recovery after a failed direct merge."""

    @pytest.mark.asyncio
    async def test_approves_and_retries_when_review_required(self) -> None:
        """Blocked-pending-approval failure → approve → retry merges."""
        mgr, client = make_merge_manager()
        mgr._post_approval_delay = 0.0
        pr = _BLOCKED_PR.model_copy()

        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by branch protection (requires approval)"
        )

        with (
            patch.object(
                mgr, "_ensure_pr_approved", new_callable=AsyncMock, return_value=True
            ) as mock_approve,
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_retry,
        ):
            result = await mgr._approve_and_retry_if_review_required(
                pr, "owner", "repo"
            )

        assert result is True
        mock_approve.assert_awaited_once()
        mock_retry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_when_failure_is_not_approval(self) -> None:
        """A non-approval block reason must NOT trigger an approval."""
        mgr, client = make_merge_manager()
        pr = _BLOCKED_PR.model_copy()

        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by failing check: ci/test"
        )

        with (
            patch.object(
                mgr, "_ensure_pr_approved", new_callable=AsyncMock
            ) as mock_approve,
            patch.object(
                mgr, "_merge_pr_with_retry", new_callable=AsyncMock
            ) as mock_retry,
        ):
            result = await mgr._approve_and_retry_if_review_required(
                pr, "owner", "repo"
            )

        assert result is False
        mock_approve.assert_not_called()
        mock_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_not_blocked(self) -> None:
        """A non-blocked state is never an approval problem."""
        mgr, client = make_merge_manager()
        pr = _BLOCKED_PR.model_copy(update={"mergeable_state": "clean"})

        with patch.object(
            mgr, "_ensure_pr_approved", new_callable=AsyncMock
        ) as mock_approve:
            result = await mgr._approve_and_retry_if_review_required(
                pr, "owner", "repo"
            )

        assert result is False
        mock_approve.assert_not_called()
        # The block-reason probe must be skipped entirely for non-blocked PRs.
        client.analyze_block_reason.assert_not_called()

    @pytest.mark.asyncio
    async def test_authoritative_rejection_overrides_lagging_state(self) -> None:
        """GitHub's missing-approval body fires even when state isn't blocked.

        ``mergeable_state`` lags and is blind to repository rulesets, so a
        merge can be rejected for a missing required approval while the
        cached state is still ``clean``.  The authoritative rejection body
        must drive approve-and-retry without consulting the heuristic
        block-reason probe.
        """
        mgr, client = make_merge_manager()
        mgr._post_approval_delay = 0.0
        pr = _BLOCKED_PR.model_copy(update={"mergeable_state": "clean"})
        mgr._last_merge_exception["owner/repo#42"] = Exception(
            "Repository rule violations found Waiting on required "
            "approvals from owner/releng"
        )

        with (
            patch.object(
                mgr, "_ensure_pr_approved", new_callable=AsyncMock, return_value=True
            ) as mock_approve,
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_retry,
        ):
            result = await mgr._approve_and_retry_if_review_required(
                pr, "owner", "repo"
            )

        assert result is True
        mock_approve.assert_awaited_once()
        mock_retry.assert_awaited_once()
        # The authoritative body is sufficient; no heuristic probe needed.
        client.analyze_block_reason.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_already_approved_this_run(self) -> None:
        """If we already approved this run, do not approve again."""
        mgr, client = make_merge_manager()
        pr = _BLOCKED_PR.model_copy()
        mgr._recently_approved.add("owner/repo#42")

        with patch.object(
            mgr, "_ensure_pr_approved", new_callable=AsyncMock
        ) as mock_approve:
            result = await mgr._approve_and_retry_if_review_required(
                pr, "owner", "repo"
            )

        assert result is False
        mock_approve.assert_not_called()
        client.analyze_block_reason.assert_not_called()

    @pytest.mark.asyncio
    async def test_preview_mode_never_approves(self) -> None:
        """Preview mode performs no side effects."""
        mgr, client = make_merge_manager(preview_mode=True)
        pr = _BLOCKED_PR.model_copy()

        with patch.object(
            mgr, "_ensure_pr_approved", new_callable=AsyncMock
        ) as mock_approve:
            result = await mgr._approve_and_retry_if_review_required(
                pr, "owner", "repo"
            )

        assert result is False
        mock_approve.assert_not_called()


# ---------------------------------------------------------------------------
# Trigger 2: _enable_auto_merge_with_approval
# ---------------------------------------------------------------------------


class TestEnableAutoMergeWithApproval:
    """Approve the current head before arming auto-merge."""

    @pytest.mark.asyncio
    async def test_approves_then_enables(self) -> None:
        """Approval happens before auto-merge is enabled."""
        mgr, _client = make_merge_manager()
        pr = _BLOCKED_PR.model_copy()
        order: list[str] = []

        async def fake_approve(*_args, **_kwargs) -> bool:
            order.append("approve")
            return True

        async def fake_enable(*_args, **_kwargs) -> bool:
            order.append("enable")
            return True

        with (
            patch.object(mgr, "_ensure_pr_approved", new=fake_approve),
            patch.object(mgr, "_enable_auto_merge_for_pr", new=fake_enable),
        ):
            result = await mgr._enable_auto_merge_with_approval(pr, "owner", "repo")

        assert result is True
        assert order == ["approve", "enable"]

    @pytest.mark.asyncio
    async def test_preview_mode_skips_approval_but_enables(self) -> None:
        """Preview mode must not approve, but still reports the enable result."""
        mgr, _client = make_merge_manager(preview_mode=True)
        pr = _BLOCKED_PR.model_copy()

        with (
            patch.object(
                mgr, "_ensure_pr_approved", new_callable=AsyncMock
            ) as mock_approve,
            patch.object(
                mgr,
                "_enable_auto_merge_for_pr",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_enable,
        ):
            result = await mgr._enable_auto_merge_with_approval(pr, "owner", "repo")

        assert result is True
        mock_approve.assert_not_called()
        mock_enable.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_enables_even_if_approval_errors(self) -> None:
        """A non-permission approval error must not block arming auto-merge."""
        mgr, _client = make_merge_manager()
        pr = _BLOCKED_PR.model_copy()

        with (
            patch.object(
                mgr,
                "_ensure_pr_approved",
                new_callable=AsyncMock,
                side_effect=RuntimeError("transient approval hiccup"),
            ),
            patch.object(
                mgr,
                "_enable_auto_merge_for_pr",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_enable,
        ):
            result = await mgr._enable_auto_merge_with_approval(pr, "owner", "repo")

        assert result is True
        mock_enable.assert_awaited_once()


# ---------------------------------------------------------------------------
# _ensure_pr_approved bookkeeping
# ---------------------------------------------------------------------------


class TestEnsurePrApproved:
    """The approval wrapper tracks newly-approved PRs."""

    @pytest.mark.asyncio
    async def test_tracks_recently_approved_on_new_approval(self) -> None:
        """A new approval is recorded in _recently_approved."""
        mgr, _client = make_merge_manager()
        mgr._post_approval_delay = 0.0
        pr = _BLOCKED_PR.model_copy()

        with patch.object(
            mgr, "_approve_pr", new_callable=AsyncMock, return_value=True
        ):
            approved = await mgr._ensure_pr_approved(pr, "owner", "repo")

        assert approved is True
        assert "owner/repo#42" in mgr._recently_approved

    @pytest.mark.asyncio
    async def test_no_tracking_when_already_approved(self) -> None:
        """When _approve_pr no-ops, the PR is not added to the set."""
        mgr, _client = make_merge_manager()
        pr = _BLOCKED_PR.model_copy()

        with patch.object(
            mgr, "_approve_pr", new_callable=AsyncMock, return_value=False
        ):
            approved = await mgr._ensure_pr_approved(pr, "owner", "repo")

        assert approved is False
        assert "owner/repo#42" not in mgr._recently_approved
