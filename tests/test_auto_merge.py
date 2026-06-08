# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Unit tests for auto-merge functionality in AsyncMergeManager.

Covers:
- Enabling auto-merge via GraphQL (success, missing node_id, idempotent,
  graceful failure).
- Merge-flow behaviour when auto-merge is active (blocked vs clean).
- Centralised timing defaults and custom merge-timeout computation.
- Invalid merge-timeout fallback to the default.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dependamerge.github2gerrit_detector import GitHub2GerritDetectionResult
from dependamerge.merge_manager import MergeStatus
from dependamerge.models import PullRequestInfo
from tests.conftest import make_merge_manager

# ---------------------------------------------------------------------------
# Module-level default PullRequestInfo instance
# ---------------------------------------------------------------------------

_DEFAULT_PR = PullRequestInfo(
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
# 1. _enable_auto_merge_for_pr - success path
# ---------------------------------------------------------------------------


class TestEnableAutoMergeSuccess:
    """Verify that _enable_auto_merge_for_pr enables auto-merge."""

    @pytest.mark.asyncio
    async def test_enable_auto_merge_success(self) -> None:
        """Enable auto-merge returns True and tracks the PR key."""
        mgr, client = make_merge_manager()
        pr = _DEFAULT_PR.model_copy()

        client.enable_auto_merge = AsyncMock(return_value=True)

        result = await mgr._enable_auto_merge_for_pr(pr, "owner", "repo")

        assert result is True
        assert "owner/repo#42" in mgr._auto_merge_enabled
        client.enable_auto_merge.assert_called_once_with("PR_kwDOTestNode42", "merge")


# ---------------------------------------------------------------------------
# 2. _enable_auto_merge_for_pr - missing node_id
# ---------------------------------------------------------------------------


class TestEnableAutoMergeNoNodeId:
    """When node_id is None, auto-merge cannot be enabled."""

    @pytest.mark.asyncio
    async def test_enable_auto_merge_no_node_id(self) -> None:
        """Return False when the PR has no node_id."""
        mgr, client = make_merge_manager()
        pr = _DEFAULT_PR.model_copy(update={"node_id": None})

        result = await mgr._enable_auto_merge_for_pr(pr, "owner", "repo")

        assert result is False
        assert len(mgr._auto_merge_enabled) == 0


# ---------------------------------------------------------------------------
# 3. _enable_auto_merge_for_pr - idempotent
# ---------------------------------------------------------------------------


class TestEnableAutoMergeIdempotent:
    """Calling enable twice should only hit the API once."""

    @pytest.mark.asyncio
    async def test_enable_auto_merge_idempotent(self) -> None:
        """Second call returns True without calling the API again."""
        mgr, client = make_merge_manager()
        pr = _DEFAULT_PR.model_copy()

        # Pre-populate the tracking set
        mgr._auto_merge_enabled.add("owner/repo#42")

        client.enable_auto_merge = AsyncMock(return_value=True)

        result = await mgr._enable_auto_merge_for_pr(pr, "owner", "repo")

        assert result is True
        client.enable_auto_merge.assert_not_called()


# ---------------------------------------------------------------------------
# 4. _enable_auto_merge_for_pr - GraphQL failure
# ---------------------------------------------------------------------------


class TestEnableAutoMergeFailureGraceful:
    """When the GraphQL call fails, return False without raising."""

    @pytest.mark.asyncio
    async def test_enable_auto_merge_failure_graceful(self) -> None:
        """Return False and do not track the PR when enable fails."""
        mgr, client = make_merge_manager()
        pr = _DEFAULT_PR.model_copy()

        client.enable_auto_merge = AsyncMock(return_value=False)

        result = await mgr._enable_auto_merge_for_pr(pr, "owner", "repo")

        assert result is False
        assert "owner/repo#42" not in mgr._auto_merge_enabled


# ---------------------------------------------------------------------------
# 5. Merge skipped when auto-merge active and blocked
# ---------------------------------------------------------------------------


class TestMergeSkippedWhenAutoMergeActiveAndBlocked:
    """When auto-merge is enabled and PR is blocked, skip manual merge."""

    @pytest.mark.asyncio
    async def test_merge_skipped_when_auto_merge_active_and_blocked(
        self,
    ) -> None:
        """Auto-merge pending: _merge_pr_with_retry is NOT called."""
        mgr, client = make_merge_manager(preview_mode=False, merge_timeout=0.1)
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "blocked",
                "mergeable": True,
                "state": "open",
            }
        )

        # Pre-populate auto-merge tracking
        mgr._auto_merge_enabled.add("owner/repo#42")

        # Stub GitHub client methods used during the flow
        client.get = AsyncMock(return_value={})
        client.get_required_status_checks = AsyncMock(return_value=[])
        # The skip gate consults analyze_block_reason; return a reason
        # that indicates pending required checks so the skip fires.
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by pending required check: pre-commit.ci"
        )

        # Patch the manager methods that _merge_single_pr calls
        # before reaching the auto-merge gate.
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
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge_retry,
        ):
            result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.AUTO_MERGE_PENDING
        mock_merge_retry.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Merge proceeds when auto-merge active and state is clean
# ---------------------------------------------------------------------------


class TestMergeProceedsWhenAutoMergeActiveAndClean:
    """When auto-merge is active but PR is clean, merge proceeds."""

    @pytest.mark.asyncio
    async def test_merge_proceeds_when_auto_merge_active_and_clean(
        self,
    ) -> None:
        """Manual merge still happens when mergeable_state is clean."""
        mgr, client = make_merge_manager(preview_mode=False)
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "clean",
                "mergeable": True,
                "state": "open",
            }
        )

        # Pre-populate auto-merge tracking
        mgr._auto_merge_enabled.add("owner/repo#42")

        client.get = AsyncMock(return_value={})
        client.get_required_status_checks = AsyncMock(return_value=[])

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
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge_retry,
        ):
            result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.MERGED
        mock_merge_retry.assert_called_once()


# ---------------------------------------------------------------------------
# 7. Centralised timing - defaults
# ---------------------------------------------------------------------------


class TestCentralisedTimingDefaults:
    """Verify timing constants are correctly computed from defaults."""

    def test_centralised_timing_defaults(self) -> None:
        """Default timeout, recheck interval, and poll max are correct."""
        mgr, _client = make_merge_manager()

        assert mgr._merge_timeout == 300.0
        assert mgr._merge_recheck_interval == 10.0
        assert mgr._merge_poll_max_attempts == 30


# ---------------------------------------------------------------------------
# 8. Custom merge timeout
# ---------------------------------------------------------------------------


class TestCustomMergeTimeout:
    """Verify a custom merge_timeout correctly computes poll max."""

    def test_custom_merge_timeout(self) -> None:
        """600s timeout with 10s interval yields 60 poll attempts."""
        mgr, _client = make_merge_manager(merge_timeout=600.0)

        assert mgr._merge_timeout == 600.0
        assert mgr._merge_poll_max_attempts == 60


# ---------------------------------------------------------------------------
# 9. Invalid merge-timeout fallback
# ---------------------------------------------------------------------------


class TestInvalidMergeTimeoutFallback:
    """Invalid merge_timeout values must fall back to the default."""

    def test_negative_merge_timeout_fallback(self) -> None:
        """Negative timeout falls back to 300.0."""
        mgr, _client = make_merge_manager(merge_timeout=-1.0)

        assert mgr._merge_timeout == 300.0

    def test_inf_merge_timeout_fallback(self) -> None:
        """Infinity falls back to 300.0."""
        mgr, _client = make_merge_manager(merge_timeout=float("inf"))

        assert mgr._merge_timeout == 300.0

    def test_nan_merge_timeout_fallback(self) -> None:
        """NaN falls back to 300.0."""
        mgr, _client = make_merge_manager(merge_timeout=float("nan"))

        assert mgr._merge_timeout == 300.0


# ---------------------------------------------------------------------------
# 10. Auto-merge skip gate: do NOT skip when mergeable is False
# ---------------------------------------------------------------------------


class TestAutoMergeSkipGateMergeableFalse:
    """Blocked + mergeable=False routes to AUTO_MERGE_PENDING when checks pending.

    Previously this case fell through to a manual merge (which would
    typically 405 against unfinished required checks). The current
    behaviour treats ``mergeable=False`` the same as ``True`` / ``None``
    when ``mergeable_state`` is auto-merge-rescuable: GitHub returns
    ``mergeable=False`` transiently for several reasons (still
    computing, non-required check failed) so we let Step 5.5's
    block-reason pre-check make the routing decision.
    """

    @pytest.mark.asyncio
    async def test_blocked_mergeable_false_pending_checks_routes_to_pending(
        self,
    ) -> None:
        """blocked + mergeable=False + pending checks → AUTO_MERGE_PENDING."""
        mgr, client = make_merge_manager(preview_mode=False, merge_timeout=0.1)
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "blocked",
                "mergeable": False,
                "state": "open",
            }
        )

        mgr._auto_merge_enabled.add("owner/repo#42")

        # Refresh keeps the PR in the same state for the duration
        # of the (very short) wait loop.
        client.get = AsyncMock(
            return_value={
                "mergeable": False,
                "mergeable_state": "blocked",
                "state": "open",
            }
        )
        client.get_required_status_checks = AsyncMock(return_value=[])
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by pending required check: pre-commit.ci"
        )
        client.enable_auto_merge = AsyncMock(return_value=True)
        client.post_issue_comment = AsyncMock()

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
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge_retry,
        ):
            result = await mgr._merge_single_pr(pr)

        # mergeable=False with pending-checks block reason now
        # routes through Step 5.5 → AUTO_MERGE_PENDING instead of
        # the previous manual-merge fall-through. The user gets a
        # friendlier outcome (auto-merge will fire when checks
        # complete) than a 405 hard fail.
        assert result.status == MergeStatus.AUTO_MERGE_PENDING
        mock_merge_retry.assert_not_called()


# ---------------------------------------------------------------------------
# 11. Auto-merge skip gate: force=all must proceed with manual merge
# ---------------------------------------------------------------------------


class TestAutoMergeSkipGateForceAll:
    """--force=all must proceed with manual merge even with auto-merge active."""

    @pytest.mark.asyncio
    async def test_manual_merge_runs_with_force_all(self) -> None:
        """force_level='all': auto-merge gate must not override."""
        mgr, client = make_merge_manager(
            preview_mode=False, force_level="all", merge_timeout=0.1
        )
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "blocked",
                "mergeable": True,
                "state": "open",
            }
        )

        mgr._auto_merge_enabled.add("owner/repo#42")

        client.get = AsyncMock(return_value={})
        client.get_required_status_checks = AsyncMock(return_value=[])

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
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge_retry,
        ):
            result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.MERGED
        mock_merge_retry.assert_called_once()


# ---------------------------------------------------------------------------
# 12. Poll-max ceiling: non-multiple timeouts round up, not down
# ---------------------------------------------------------------------------


class TestMergePollMaxAttemptsCeiling:
    """Non-multiple merge_timeout must round UP via math.ceil, not truncate."""

    def test_poll_max_rounds_up_for_non_multiple_timeout(self) -> None:
        """301s/10s must yield 31 attempts (not 30 via truncation)."""
        mgr, _client = make_merge_manager(merge_timeout=301.0)

        assert mgr._merge_timeout == 301.0
        assert mgr._merge_poll_max_attempts == 31

    def test_poll_max_exact_multiple_unchanged(self) -> None:
        """Exact multiple (300s/10s) still yields 30 attempts."""
        mgr, _client = make_merge_manager(merge_timeout=300.0)

        assert mgr._merge_poll_max_attempts == 30


# ---------------------------------------------------------------------------
# 13. Recheck-interval clamping for small merge_timeout values
# ---------------------------------------------------------------------------


class TestRecheckIntervalClamping:
    """merge_timeout < default interval must clamp the per-iteration sleep."""

    def test_recheck_interval_clamped_for_small_timeout(self) -> None:
        """merge_timeout=3s clamps interval to 3s so loops don't oversleep."""
        mgr, _client = make_merge_manager(merge_timeout=3.0)

        assert mgr._merge_timeout == 3.0
        assert mgr._merge_recheck_interval == 3.0
        # ceil(3/3) = 1 attempt — total wait still equals merge_timeout
        assert mgr._merge_poll_max_attempts == 1

    def test_recheck_interval_unchanged_for_large_timeout(self) -> None:
        """merge_timeout >= default interval keeps the default 10s cadence."""
        mgr, _client = make_merge_manager(merge_timeout=600.0)

        assert mgr._merge_recheck_interval == 10.0
        assert mgr._merge_poll_max_attempts == 60


# ---------------------------------------------------------------------------
# 14. AUTO_MERGE_PENDING triggers progress tracker pr_completed()
# ---------------------------------------------------------------------------


class TestAutoMergePendingCompletesProgress:
    """AUTO_MERGE_PENDING status must bump PR-level progress to completed."""

    @pytest.mark.asyncio
    async def test_auto_merge_pending_calls_pr_completed(self) -> None:
        """pr_completed() is called so PR progress reaches 100%."""
        tracker = MagicMock()
        mgr, client = make_merge_manager(
            preview_mode=False,
            progress_tracker=tracker,
            merge_timeout=0.1,
        )
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "blocked",
                "mergeable": True,
                "state": "open",
            }
        )

        mgr._auto_merge_enabled.add("owner/repo#42")

        client.get = AsyncMock(return_value={})
        client.get_required_status_checks = AsyncMock(return_value=[])
        # The skip gate consults analyze_block_reason; return a reason
        # that indicates pending required checks so the skip fires.
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by pending required check: pre-commit.ci"
        )

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
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await mgr._merge_single_pr_with_semaphore(pr)

        assert result.status == MergeStatus.AUTO_MERGE_PENDING
        tracker.pr_completed.assert_called_once()
        tracker.merge_success.assert_not_called()
        tracker.merge_failure.assert_not_called()


# ---------------------------------------------------------------------------
# 15. Auto-merge skip gate: only skip on pending required checks
# ---------------------------------------------------------------------------


class TestAutoMergeSkipGateBlockReason:
    """Skip gate must consult analyze_block_reason and only skip on pending checks."""

    @pytest.mark.asyncio
    async def test_manual_merge_runs_when_blocked_by_missing_approvals(
        self,
    ) -> None:
        """Block reason = 'requires approval': manual merge must still run."""
        mgr, client = make_merge_manager(preview_mode=False, merge_timeout=0.1)
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "blocked",
                "mergeable": True,
                "state": "open",
            }
        )

        mgr._auto_merge_enabled.add("owner/repo#42")

        client.get = AsyncMock(return_value={})
        client.get_required_status_checks = AsyncMock(return_value=[])
        # Block reason is NOT pending required checks — skip must not fire.
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by branch protection (requires approval)"
        )

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
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge_retry,
        ):
            result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.MERGED
        mock_merge_retry.assert_called_once()
        # analyze_block_reason was consulted before deciding to proceed.
        # It is now called by both the Step 5.5 pre-check (to decide
        # whether to wait) and the Step 6 skip gate, so we assert it
        # was awaited at least once rather than exactly once.
        assert client.analyze_block_reason.await_count >= 1

    @pytest.mark.asyncio
    async def test_manual_merge_runs_when_analyze_block_reason_fails(
        self,
    ) -> None:
        """If analyze_block_reason raises, fall back to manual merge attempt."""
        mgr, client = make_merge_manager(preview_mode=False, merge_timeout=0.1)
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "blocked",
                "mergeable": True,
                "state": "open",
            }
        )

        mgr._auto_merge_enabled.add("owner/repo#42")

        client.get = AsyncMock(return_value={})
        client.get_required_status_checks = AsyncMock(return_value=[])
        client.analyze_block_reason = AsyncMock(side_effect=RuntimeError("boom"))

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
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge_retry,
        ):
            result = await mgr._merge_single_pr(pr)

        # Defensive fallback: proceed with manual merge rather than
        # silently marking as AUTO_MERGE_PENDING.
        assert result.status == MergeStatus.MERGED
        mock_merge_retry.assert_called_once()


# ---------------------------------------------------------------------------
# 16. Step 5.5 end-to-end: auto-merge not yet enabled, gets enabled
# ---------------------------------------------------------------------------


class TestStep5_5EnablesAutoMergeAndTimesOut:
    """Step 5.5 end-to-end coverage.

    Starts with ``_auto_merge_enabled`` empty so the wait phase must
    invoke ``_enable_auto_merge_for_pr`` itself, then times out while
    the PR is still blocked, and asserts the resulting status is
    ``AUTO_MERGE_PENDING`` (not ``FAILED``).
    """

    @pytest.mark.asyncio
    async def test_step_5_5_enables_auto_merge_then_times_out_pending(
        self,
    ) -> None:
        """Step 5.5 enables auto-merge and yields AUTO_MERGE_PENDING."""
        mgr, client = make_merge_manager(preview_mode=False, merge_timeout=0.1)
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "blocked",
                "mergeable": True,
                "state": "open",
            }
        )

        # IMPORTANT: do NOT pre-populate ``_auto_merge_enabled`` —
        # this is the path Copilot flagged as untested. The set
        # stores ``"{owner}/{repo}#{number}"`` keys, not URLs.
        assert "owner/repo#42" not in mgr._auto_merge_enabled

        # GraphQL enable succeeds; subsequent .get() in the wait
        # loop returns the PR still blocked so we time out.
        client.enable_auto_merge = AsyncMock(return_value=True)
        client.post_issue_comment = AsyncMock()
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "blocked",
                "state": "open",
            }
        )
        client.get_required_status_checks = AsyncMock(return_value=[])
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by pending required check: pre-commit.ci"
        )

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
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge_retry,
        ):
            result = await mgr._merge_single_pr(pr)

        # Auto-merge was enabled by Step 5.5 itself.
        client.enable_auto_merge.assert_awaited_once_with("PR_kwDOTestNode42", "merge")
        assert "owner/repo#42" in mgr._auto_merge_enabled

        # Wait timed out while still blocked, so the skip gate
        # routes us to AUTO_MERGE_PENDING and the manual merge
        # is NOT attempted.
        assert result.status == MergeStatus.AUTO_MERGE_PENDING
        mock_merge_retry.assert_not_called()


# ---------------------------------------------------------------------------
# 17. Step 5.5 + skip gate: mergeable=None (still computing) is treated as
#     still-computing rather than 'not mergeable'
# ---------------------------------------------------------------------------


class TestStep5_5HandlesMergeableNone:
    """Both gates must accept mergeable=None as 'still computing'.

    GitHub returns ``mergeable: null`` transiently while it is still
    computing the value. The Step 5.5 entry condition and the Step 6
    auto-merge skip gate must NOT bypass auto-merge handling on this
    transient state — doing so reintroduces the original 405 failure
    mode this PR was raised to fix.
    """

    @pytest.mark.asyncio
    async def test_step_5_5_runs_when_mergeable_is_none(self) -> None:
        """mergeable=None + blocked + pending checks → AUTO_MERGE_PENDING."""
        mgr, client = make_merge_manager(preview_mode=False, merge_timeout=0.1)
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "blocked",
                "mergeable": None,  # GitHub still computing
                "state": "open",
            }
        )

        client.enable_auto_merge = AsyncMock(return_value=True)
        client.post_issue_comment = AsyncMock()
        # Refresh keeps mergeable null + state blocked so the
        # wait loop exhausts the timeout.
        client.get = AsyncMock(
            return_value={
                "mergeable": None,
                "mergeable_state": "blocked",
                "state": "open",
            }
        )
        client.get_required_status_checks = AsyncMock(return_value=[])
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by pending required check: pre-commit.ci"
        )

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
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge_retry,
        ):
            result = await mgr._merge_single_pr(pr)

        # Step 5.5 entry gate accepted mergeable=None and enabled
        # auto-merge.
        client.enable_auto_merge.assert_awaited_once_with("PR_kwDOTestNode42", "merge")
        assert "owner/repo#42" in mgr._auto_merge_enabled
        # Step 6 skip gate also accepted mergeable=None and routed
        # to AUTO_MERGE_PENDING instead of the manual merge
        # fall-through (which would 405 against pending checks).
        assert result.status == MergeStatus.AUTO_MERGE_PENDING
        mock_merge_retry.assert_not_called()


# ---------------------------------------------------------------------------
# 18. Step 5.5: behind + fix_out_of_date=False still routes to auto-merge
# ---------------------------------------------------------------------------


class TestStep5_5BehindWithoutFix:
    """``behind`` PRs route through Step 5.5 regardless of ``fix_out_of_date``.

    A common real-world pattern: Dependabot or pre-commit-ci has just
    rebased the PR. GitHub briefly reports ``mergeable_state: "behind"``
    while it recomputes mergeability, and the new commit's required
    checks are still running. Without this routing, dependamerge would
    immediately fail those PRs with a 405 instead of enabling
    auto-merge and letting GitHub finish the merge once checks pass.

    The user's ``--no-fix`` intent is preserved by *not rebasing the
    branch ourselves*; enabling auto-merge is a separate, non-rewriting
    operation that has no effect unless branch protection is satisfied.
    """

    @pytest.mark.asyncio
    async def test_step_5_5_routes_behind_no_fix_to_auto_merge_pending(
        self,
    ) -> None:
        """behind + fix_out_of_date=False → AUTO_MERGE_PENDING (not FAILED)."""
        mgr, client = make_merge_manager(
            preview_mode=False,
            merge_timeout=0.1,
            fix_out_of_date=False,
        )
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "behind",
                "mergeable": True,
                "state": "open",
            }
        )

        # Auto-merge can be enabled, but the PR stays ``behind`` for
        # the duration of the (very short) wait loop.
        client.enable_auto_merge = AsyncMock(return_value=True)
        client.post_issue_comment = AsyncMock()
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "behind",
                "state": "open",
            }
        )
        client.get_required_status_checks = AsyncMock(return_value=[])
        # ``behind`` PRs do not consult analyze_block_reason in the
        # Step 5.5 pre-check (only ``blocked`` does), but the Step 6
        # skip gate treats ``behind`` as auto-merge-eligible without
        # calling it either, so we don't need a return value.
        client.analyze_block_reason = AsyncMock(return_value="behind base branch")

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
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge_retry,
        ):
            result = await mgr._merge_single_pr(pr)

        # Auto-merge was enabled despite fix_out_of_date=False.
        client.enable_auto_merge.assert_awaited_once_with("PR_kwDOTestNode42", "merge")
        assert "owner/repo#42" in mgr._auto_merge_enabled
        # Wait timed out while still ``behind``; the Step 6 skip gate
        # routes to AUTO_MERGE_PENDING and the manual merge does NOT
        # run (which would otherwise 405 against the unfinished
        # required checks).
        assert result.status == MergeStatus.AUTO_MERGE_PENDING
        mock_merge_retry.assert_not_called()


# ---------------------------------------------------------------------------
# 19. Step 5.5: ``unstable`` (non-required check failed) routes to auto-merge
# ---------------------------------------------------------------------------


class TestStep5_5UnstableRoutesToAutoMerge:
    """``unstable`` PRs route through Step 5.5 (auto-merge can rescue).

    ``mergeable_state == "unstable"`` means a non-required status
    check failed but the PR is otherwise mergeable. Branch protection
    only blocks on *required* checks, so auto-merge can fire — we
    should not hard-fail this case.
    """

    @pytest.mark.asyncio
    async def test_unstable_routes_to_auto_merge_pending(self) -> None:
        """unstable + mergeable=False → AUTO_MERGE_PENDING."""
        mgr, client = make_merge_manager(preview_mode=False, merge_timeout=0.1)
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "unstable",
                "mergeable": False,
                "state": "open",
            }
        )

        client.enable_auto_merge = AsyncMock(return_value=True)
        client.post_issue_comment = AsyncMock()
        client.get = AsyncMock(
            return_value={
                "mergeable": False,
                "mergeable_state": "unstable",
                "state": "open",
            }
        )
        client.get_required_status_checks = AsyncMock(return_value=[])
        # ``unstable`` doesn't trigger the analyze_block_reason
        # pre-check (only ``blocked`` does), so this mock is just
        # defensive.
        client.analyze_block_reason = AsyncMock(
            return_value="a non-required check failed"
        )

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
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_merge_retry,
        ):
            result = await mgr._merge_single_pr(pr)

        # Auto-merge enabled; Step 6 skip gate routes ``unstable``
        # straight to AUTO_MERGE_PENDING (no analyze_block_reason
        # needed for this state).
        client.enable_auto_merge.assert_awaited_once_with("PR_kwDOTestNode42", "merge")
        assert "owner/repo#42" in mgr._auto_merge_enabled
        assert result.status == MergeStatus.AUTO_MERGE_PENDING
        mock_merge_retry.assert_not_called()


# ---------------------------------------------------------------------------
# 20. ``dirty`` PRs are still blocked (genuine merge conflict invariant)
# ---------------------------------------------------------------------------


class TestDirtyStillBlocksAtIsPrMergeable:
    """``dirty`` PRs (real merge conflict) must NOT reach Step 5.5.

    The widening of ``_is_pr_mergeable`` should not lose its hard-skip
    for genuine merge conflicts: those need a human (or ``--fix``) to
    rebase, and auto-merge cannot resolve them.
    """

    def test_dirty_returns_false(self) -> None:
        """_is_pr_mergeable returns False for dirty (merge conflict)."""
        mgr, _client = make_merge_manager()
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "dirty",
                "mergeable": False,
            }
        )
        assert mgr._is_pr_mergeable(pr) is False

    def test_draft_returns_false(self) -> None:
        """_is_pr_mergeable returns False for draft PRs."""
        mgr, _client = make_merge_manager()
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "draft",
                "mergeable": True,
            }
        )
        assert mgr._is_pr_mergeable(pr) is False

    def test_blocked_mergeable_false_returns_true(self) -> None:
        """_is_pr_mergeable now returns True for blocked + mergeable=False.

        This is the rescue path: previously this combination short-
        circuited as 'unmergeable', now it reaches the merge flow so
        Step 5.5 can route to AUTO_MERGE_PENDING.
        """
        mgr, _client = make_merge_manager()
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "blocked",
                "mergeable": False,
            }
        )
        assert mgr._is_pr_mergeable(pr) is True

    def test_behind_mergeable_false_returns_true(self) -> None:
        """_is_pr_mergeable now returns True for behind + mergeable=False."""
        mgr, _client = make_merge_manager()
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "behind",
                "mergeable": False,
            }
        )
        assert mgr._is_pr_mergeable(pr) is True

    def test_unstable_mergeable_false_returns_true(self) -> None:
        """_is_pr_mergeable now returns True for unstable + mergeable=False."""
        mgr, _client = make_merge_manager()
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "unstable",
                "mergeable": False,
            }
        )
        assert mgr._is_pr_mergeable(pr) is True


# ---------------------------------------------------------------------------
# 21. Pending-checks predicate distinguishes pending from failing/missing
# ---------------------------------------------------------------------------


class TestBlockReasonIndicatesPendingChecks:
    """Predicate must classify pending vs. failing/missing accurately.

    ``GitHubAsync.analyze_block_reason()`` produces several phrasings:

    - ``Blocked by pending required check: …``  → pending
    - ``Blocked by failing check: …``           → NOT pending
    - ``Blocked by missing required status: …`` → NOT pending

    A predicate that misclassifies failing or missing reasons as
    pending would route doomed PRs to AUTO_MERGE_PENDING and mask
    real failures, so this test pins the boundary explicitly.
    """

    def test_none_returns_false(self) -> None:
        """None block reason is never pending."""
        from dependamerge.merge_manager import AsyncMergeManager

        assert AsyncMergeManager._block_reason_indicates_pending_checks(None) is False

    def test_pending_required_check_returns_true(self) -> None:
        """Canonical pending phrasings are recognised."""
        from dependamerge.merge_manager import AsyncMergeManager

        for reason in (
            "Blocked by pending required check: pre-commit.ci",
            "Blocked by 2 pending required checks: ci/build, ci/lint",
            "required status check is still pending",
            "waiting for status checks",
            "check queued",
        ):
            assert (
                AsyncMergeManager._block_reason_indicates_pending_checks(reason) is True
            ), reason

    def test_failing_check_returns_false(self) -> None:
        """Failing check is NOT classified as pending."""
        from dependamerge.merge_manager import AsyncMergeManager

        for reason in (
            "Blocked by failing check: pre-commit.ci",
            "Blocked by failing check: ci/build",
            "Blocked by 3 failing checks",
        ):
            assert (
                AsyncMergeManager._block_reason_indicates_pending_checks(reason)
                is False
            ), reason

    def test_missing_required_status_returns_false(self) -> None:
        """Missing required status is NOT classified as pending."""
        from dependamerge.merge_manager import AsyncMergeManager

        for reason in (
            "Blocked by missing required status: pre-commit.ci",
            "Blocked by 2 missing required statuses: pre-commit.ci, ci/lint",
            "Blocked by missing required check: pre-commit.ci",
        ):
            assert (
                AsyncMergeManager._block_reason_indicates_pending_checks(reason)
                is False
            ), reason

    def test_other_block_reasons_return_false(self) -> None:
        """Non-check block reasons (approvals, conflicts, etc.) return False."""
        from dependamerge.merge_manager import AsyncMergeManager

        for reason in (
            "Blocked by branch protection (requires approval)",
            "Blocked by branch protection",
            "Human reviewer requested changes",
            "Blocked by 2 unresolved Copilot comments",
            "",
        ):
            assert (
                AsyncMergeManager._block_reason_indicates_pending_checks(reason)
                is False
            ), reason

    def test_failing_with_pending_keyword_returns_false(self) -> None:
        """Defensive: 'failing' wins even when 'pending' also appears.

        Guards against future GitHub phrasings that combine both
        keywords (e.g. "failing check (pending retry): pre-commit.ci").
        We never want a failing check to be treated as auto-merge
        rescuable.
        """
        from dependamerge.merge_manager import AsyncMergeManager

        assert (
            AsyncMergeManager._block_reason_indicates_pending_checks(
                "Blocked by failing check (pending retry): pre-commit.ci"
            )
            is False
        )


class TestWaitForAutoMergeRecompute:
    """`_wait_for_auto_merge` must survive GitHub's recompute window."""

    @pytest.mark.asyncio
    async def test_transient_unknown_does_not_break_loop_early(self) -> None:
        """A transient 'unknown' must not exit the wait or clobber state."""
        mgr, client = make_merge_manager()
        pr = _DEFAULT_PR.model_copy(
            update={"mergeable_state": "blocked", "state": "open"}
        )
        # 1st poll: GitHub still recomputing (unknown / null).
        # 2nd poll: auto-merge has landed (closed + merged).
        client.get = AsyncMock(
            side_effect=[
                {"mergeable_state": "unknown", "mergeable": None},
                {"state": "closed", "merged": True},
            ]
        )
        with patch("dependamerge.merge_manager.asyncio.sleep", new_callable=AsyncMock):
            closed, merged = await mgr._wait_for_auto_merge(
                pr,
                "owner",
                "repo",
                continue_states=("blocked", "behind", "unstable"),
            )

        # Without the fix the loop would break after the first poll
        # ("unknown" not in continue_states); with it, it waits through
        # the recompute and observes the merge.
        assert client.get.await_count == 2
        assert closed is True
        assert merged is True
        # The transient "unknown" never overwrote the prior state.
        assert pr.mergeable_state == "blocked"

    @pytest.mark.asyncio
    async def test_unstable_mergeable_true_breaks_wait_early(self) -> None:
        """unstable + mergeable=True must exit the wait, not spin to timeout."""
        # Enter the wait while still computing (mergeable=None), then
        # GitHub settles on unstable + mergeable=True.  Because
        # ``unstable`` is in continue_states, the loop would otherwise
        # keep waiting until the deadline; the fix breaks out as soon as
        # the PR is immediately mergeable so the caller can dispatch.
        mgr, client = make_merge_manager(merge_timeout=30.0)
        pr = _DEFAULT_PR.model_copy(
            update={
                "mergeable_state": "blocked",
                "mergeable": None,
                "state": "open",
            }
        )
        client.get = AsyncMock(
            side_effect=[
                {"mergeable_state": "unknown", "mergeable": None},
                {"mergeable_state": "unstable", "mergeable": True},
                # A third poll would only be reached if the loop failed
                # to break; surface that as an explicit stuck state.
                {"mergeable_state": "unstable", "mergeable": True},
            ]
        )
        with patch("dependamerge.merge_manager.asyncio.sleep", new_callable=AsyncMock):
            closed, merged = await mgr._wait_for_auto_merge(
                pr,
                "owner",
                "repo",
                continue_states=("blocked", "behind", "unstable"),
            )

        # Broke out on the 2nd poll (unstable + mergeable=True); never
        # polled a 3rd time or ran to the deadline.
        assert client.get.await_count == 2
        assert closed is False
        assert merged is False
        assert pr.mergeable_state == "unstable"
        assert pr.mergeable is True
