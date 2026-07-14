# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for merge progress tracker state transitions and counters.

The live merge display moved from per-PR console lines to counter-based
reporting: PRs travel through transitory states (rebasing → rebased →
waiting) rendered live on the stats line, then land in exactly one
terminal counter (merged / pending / closed / failed / skipped /
blocked).  ``AsyncMergeManager._record_terminal_outcome`` is the single
accounting point mapping ``MergeStatus`` onto those counters.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from dependamerge.merge_manager import MergeStatus
from dependamerge.models import PullRequestInfo
from dependamerge.progress_tracker import (
    DummyProgressTracker,
    MergeProgressTracker,
)
from tests.conftest import make_merge_manager


def _make_pr(**overrides: Any) -> PullRequestInfo:
    defaults: dict[str, Any] = {
        "number": 7,
        "title": "CI: Bump foo from 1 to 2",
        "body": "Dependabot PR",
        "author": "dependabot[bot]",
        "head_sha": "abc123",
        "base_branch": "main",
        "head_branch": "dependabot/foo",
        "state": "open",
        "mergeable": True,
        "mergeable_state": "clean",
        "behind_by": 0,
        "files_changed": [],
        "repository_full_name": "org/repo",
        "html_url": "https://github.com/org/repo/pull/7",
    }
    defaults.update(overrides)
    return PullRequestInfo(**defaults)


class TestTransitoryStates:
    """PRs move between transitory display states, one state at a time."""

    def test_track_pr_state_moves_between_states(self) -> None:
        tracker = MergeProgressTracker("org")
        tracker.track_pr_state("org/repo#1", "rebasing")
        assert tracker._pr_states == {"org/repo#1": "rebasing"}

        # Transition: the PR occupies exactly one state at a time.
        tracker.track_pr_state("org/repo#1", "rebased")
        assert tracker._pr_states == {"org/repo#1": "rebased"}

        tracker.track_pr_state("org/repo#1", "waiting")
        assert tracker._pr_states == {"org/repo#1": "waiting"}

    def test_track_pr_state_none_clears(self) -> None:
        tracker = MergeProgressTracker("org")
        tracker.track_pr_state("org/repo#1", "waiting")
        tracker.track_pr_state("org/repo#1", None)
        assert tracker._pr_states == {}

    def test_clear_unknown_key_is_noop(self) -> None:
        tracker = MergeProgressTracker("org")
        tracker.track_pr_state("org/repo#1", None)
        assert tracker._pr_states == {}

    def test_terminal_outcome_clears_transitory_state(self) -> None:
        tracker = MergeProgressTracker("org")
        tracker.set_total_prs(2)
        tracker.track_pr_state("org/repo#1", "rebasing")
        tracker.track_pr_state("org/repo#2", "waiting")

        tracker.merge_success("org/repo#1")
        tracker.merge_failure("org/repo#2")

        assert tracker._pr_states == {}
        assert tracker.prs_merged == 1
        assert tracker.prs_failed == 1
        assert tracker.completed_prs == 2

    def test_terminal_outcome_without_key_keeps_states(self) -> None:
        """Legacy no-arg calls still count but cannot clear a state."""
        tracker = MergeProgressTracker("org")
        tracker.track_pr_state("org/repo#1", "rebasing")
        tracker.merge_success()
        assert tracker.prs_merged == 1
        assert tracker._pr_states == {"org/repo#1": "rebasing"}


class TestTerminalCounters:
    """New pending/blocked counters and completion accounting."""

    def test_merge_pending_counts_and_completes(self) -> None:
        tracker = MergeProgressTracker("org")
        tracker.set_total_prs(1)
        tracker.merge_pending("org/repo#1")
        assert tracker.prs_pending == 1
        assert tracker.completed_prs == 1

    def test_merge_blocked_counts_and_completes(self) -> None:
        tracker = MergeProgressTracker("org")
        tracker.set_total_prs(1)
        tracker.merge_blocked("org/repo#1")
        assert tracker.prs_blocked == 1
        assert tracker.completed_prs == 1

    def test_summary_includes_new_counters(self) -> None:
        tracker = MergeProgressTracker("org")
        tracker.merge_pending()
        tracker.merge_blocked()
        summary = tracker.get_summary()
        assert summary["prs_pending"] == 1
        assert summary["prs_blocked"] == 1

    def test_dummy_tracker_mirrors_surface(self) -> None:
        """DummyProgressTracker accepts the whole new surface."""
        dummy = DummyProgressTracker()
        dummy.track_pr_state("org/repo#1", "rebasing")
        dummy.merge_success("org/repo#1")
        dummy.merge_failure("org/repo#1")
        dummy.merge_skipped("org/repo#1")
        dummy.merge_blocked("org/repo#1")
        dummy.merge_pending("org/repo#1")
        dummy.increment_closed("org/repo#1")


class TestDisplayRendering:
    """Stats line renders transitory states then terminal counters."""

    def test_states_render_in_pipeline_order(self) -> None:
        tracker = MergeProgressTracker("org")
        tracker.rich_available = True
        tracker.set_total_prs(6)
        tracker.track_pr_state("org/repo#1", "waiting")
        tracker.track_pr_state("org/repo#2", "rebasing")
        tracker.track_pr_state("org/repo#3", "rebased")
        tracker.merge_success("org/repo#4")
        tracker.merge_pending("org/repo#5")
        tracker.merge_failure("org/repo#6")

        plain = tracker._generate_display_text().plain
        assert "🔄 Rebasing: 1" in plain
        assert "⬆️ Rebased: 1" in plain
        assert "⏳ Waiting: 1" in plain
        assert "✅ Merged: 1" in plain
        assert "🤖 Pending: 1" in plain
        assert "❌ Failed: 1" in plain
        # Pipeline order: transitory states precede terminal counters.
        assert plain.index("Rebasing") < plain.index("Rebased")
        assert plain.index("Rebased") < plain.index("Waiting")
        assert plain.index("Waiting") < plain.index("Merged")

    def test_zero_counters_do_not_render(self) -> None:
        tracker = MergeProgressTracker("org")
        tracker.rich_available = True
        tracker.set_total_prs(1)
        tracker.merge_success("org/repo#1")
        plain = tracker._generate_display_text().plain
        assert "Pending" not in plain
        assert "Blocked" not in plain
        assert "Rebasing" not in plain

    def test_unknown_state_rendered_defensively(self) -> None:
        tracker = MergeProgressTracker("org")
        tracker.rich_available = True
        tracker.track_pr_state("org/repo#1", "polishing")
        plain = tracker._generate_display_text().plain
        assert "Polishing: 1" in plain


class TestRecordTerminalOutcome:
    """_record_terminal_outcome maps MergeStatus onto tracker methods."""

    @pytest.mark.parametrize(
        ("status", "method"),
        [
            (MergeStatus.MERGED, "merge_success"),
            (MergeStatus.FAILED, "merge_failure"),
            (MergeStatus.SKIPPED, "merge_skipped"),
            (MergeStatus.BLOCKED, "merge_blocked"),
            (MergeStatus.CLOSED, "increment_closed"),
            (MergeStatus.AUTO_MERGE_PENDING, "merge_pending"),
        ],
    )
    def test_status_maps_to_counter(self, status: MergeStatus, method: str) -> None:
        tracker = MagicMock()
        mgr, _client = make_merge_manager(progress_tracker=tracker)
        pr = _make_pr()

        mgr._record_terminal_outcome(pr, status)

        getattr(tracker, method).assert_called_once_with("org/repo#7")
        # Exactly one terminal method fires per outcome.
        all_methods = {
            "merge_success",
            "merge_failure",
            "merge_skipped",
            "merge_blocked",
            "merge_pending",
            "increment_closed",
            "pr_completed",
        }
        for other in all_methods - {method}:
            getattr(tracker, other).assert_not_called()

    def test_unexpected_status_falls_back_to_pr_completed(self) -> None:
        tracker = MagicMock()
        mgr, _client = make_merge_manager(progress_tracker=tracker)
        pr = _make_pr()

        mgr._record_terminal_outcome(pr, MergeStatus.PENDING)

        tracker.pr_completed.assert_called_once()
        tracker.merge_success.assert_not_called()
        tracker.merge_failure.assert_not_called()

    def test_no_tracker_is_noop(self) -> None:
        mgr, _client = make_merge_manager(progress_tracker=None)
        # Must not raise.
        mgr._record_terminal_outcome(_make_pr(), MergeStatus.MERGED)

    def test_track_pr_state_delegates_with_pr_key(self) -> None:
        tracker = MagicMock()
        mgr, _client = make_merge_manager(progress_tracker=tracker)
        pr = _make_pr()

        mgr._track_pr_state(pr, "rebasing")
        tracker.track_pr_state.assert_called_once_with("org/repo#7", "rebasing")

        tracker.track_pr_state.reset_mock()
        mgr._track_pr_state(pr, None)
        tracker.track_pr_state.assert_called_once_with("org/repo#7", None)
