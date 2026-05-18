# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for external-merge race handling in ``AsyncMergeManager``.

These tests verify that a PR which is merged externally (by a
concurrent ``dependamerge`` run, a human admin, or auto-merge
landing mid-flight) is classified as ``SKIPPED`` rather than
``FAILED`` so the operator does not see spurious ``❌ Failed``
entries that need follow-up.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from dependamerge.merge_manager import MergeStatus
from dependamerge.models import PullRequestInfo
from tests.conftest import make_merge_manager


def _make_pr(
    *,
    state: str = "open",
    number: int = 59,
    repo: str = "lfreleng-actions/git-commit-message-action",
) -> PullRequestInfo:
    return PullRequestInfo(
        number=number,
        title="Chore: Bump some dep from 0.3.3 to 0.3.4",
        body="bump",
        author="dependabot[bot]",
        head_sha="deadbeef" * 5,
        base_branch="main",
        head_branch="dependabot/x",
        state=state,
        mergeable=True,
        mergeable_state="clean",
        behind_by=None,
        files_changed=[],
        repository_full_name=repo,
        html_url=f"https://github.com/{repo}/pull/{number}",
        reviews=[],
        review_comments=[],
    )


class TestIsPrAlreadyMerged:
    """Direct unit tests for ``_is_pr_already_merged``."""

    @pytest.mark.asyncio
    async def test_returns_true_when_state_closed_and_merged(self) -> None:
        """A closed+merged PR returns True (the external-merge race)."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(return_value={"state": "closed", "merged": True})
        pr = _make_pr()

        result = await mgr._is_pr_already_merged(pr, "owner", "repo")

        assert result is True
        client.get.assert_awaited_once_with("/repos/owner/repo/pulls/59")

    @pytest.mark.asyncio
    async def test_returns_false_when_closed_without_merge(self) -> None:
        """A PR closed without being merged is not an external merge."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(return_value={"state": "closed", "merged": False})

        result = await mgr._is_pr_already_merged(_make_pr(), "o", "r")

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_still_open(self) -> None:
        """An open PR is definitely not externally merged."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(return_value={"state": "open", "merged": False})

        result = await mgr._is_pr_already_merged(_make_pr(), "o", "r")

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_api_error(self) -> None:
        """API errors during the recheck degrade to False (fail safe).

        We must not mask a genuine merge failure as a skip just
        because the recheck itself errored.
        """
        mgr, client = make_merge_manager()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))

        result = await mgr._is_pr_already_merged(_make_pr(), "o", "r")

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_unexpected_payload(self) -> None:
        """Non-dict payloads degrade to False rather than crashing."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(return_value=["unexpected", "list"])

        result = await mgr._is_pr_already_merged(_make_pr(), "o", "r")

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_client_missing(self) -> None:
        """No GitHub client means we cannot recheck; fail safe to False."""
        mgr, _client = make_merge_manager()
        mgr._github_client = None

        result = await mgr._is_pr_already_merged(_make_pr(), "o", "r")

        assert result is False


class TestEarlyExitClosedPrPath:
    """``_merge_single_pr`` PR-already-closed branch tests.

    When the PR fetched at the start of ``_merge_single_pr`` is
    already closed, the manager should distinguish between
    "closed+merged" (skip) and "closed without merging" (fail).
    """

    @pytest.mark.asyncio
    async def test_closed_and_merged_is_skipped(self) -> None:
        mgr, client = make_merge_manager()
        # Recheck call returns closed+merged.
        client.get = AsyncMock(return_value={"state": "closed", "merged": True})
        pr = _make_pr(state="closed")

        result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.SKIPPED
        assert result.error == "already merged externally"

    @pytest.mark.asyncio
    async def test_closed_without_merge_is_failed(self) -> None:
        mgr, client = make_merge_manager()
        # Recheck call returns closed but not merged.
        client.get = AsyncMock(return_value={"state": "closed", "merged": False})
        pr = _make_pr(state="closed")

        result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.FAILED
        assert result.error == "PR is already closed"


class TestProgressTrackerMergeSkipped:
    """Confirm the progress tracker exposes ``merge_skipped``."""

    def test_increments_skipped_counter(self) -> None:
        from dependamerge.progress_tracker import MergeProgressTracker

        tracker = MergeProgressTracker(organization="lfreleng-actions")
        tracker.set_total_prs(3)

        assert tracker.prs_skipped == 0
        tracker.merge_skipped()
        tracker.merge_skipped()
        assert tracker.prs_skipped == 2
        # Skips count toward overall completion so the percentage
        # progresses past parked PRs in repo-scoped batches.
        assert tracker.completed_prs == 2

    def test_summary_includes_skipped(self) -> None:
        from dependamerge.progress_tracker import MergeProgressTracker

        tracker = MergeProgressTracker(organization="lfreleng-actions")
        tracker.merge_skipped()

        summary = tracker.get_summary()

        assert summary["prs_skipped"] == 1

    def test_dummy_tracker_has_merge_skipped(self) -> None:
        """DummyProgressTracker must implement the same surface."""
        from dependamerge.progress_tracker import DummyProgressTracker

        tracker = DummyProgressTracker()
        # Must not raise.
        tracker.merge_skipped()
