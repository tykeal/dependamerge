# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the live progress display's self-updating clock.

Regression tests for the frozen elapsed counter: the tracker used to
hand Rich ``Live`` a static text snapshot, so the clock only advanced
when a progress event fired.  During long silent API sequences the
display froze for 10+ seconds and the tool appeared to hang.  The
tracker now passes ``get_renderable`` so every auto-refresh tick
re-renders with a current elapsed time.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from dependamerge import progress_tracker as pt
from dependamerge.progress_tracker import MergeProgressTracker, ProgressTracker


class _RecordingLive:
    """Stand-in for rich.live.Live that records construction and calls."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.refresh_calls = 0
        self.update_calls = 0
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def refresh(self) -> None:
        self.refresh_calls += 1

    def update(self, *args: Any) -> None:
        self.update_calls += 1


class TestLiveRenderableIsCallable:
    def test_start_passes_get_renderable(self, monkeypatch) -> None:
        monkeypatch.setattr(pt, "Live", _RecordingLive)
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        tracker.start()
        live = tracker.live
        assert isinstance(live, _RecordingLive)
        assert live.started
        # Rich re-invokes the callable on every auto-refresh tick, so
        # the elapsed clock advances without progress events.
        assert live.kwargs["get_renderable"] == tracker._generate_display_text
        tracker.stop()

    def test_resume_passes_get_renderable(self, monkeypatch) -> None:
        monkeypatch.setattr(pt, "Live", _RecordingLive)
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        tracker.start()
        tracker.suspend()
        tracker.resume()
        live = tracker.live
        assert isinstance(live, _RecordingLive)
        assert live.kwargs["get_renderable"] == tracker._generate_display_text
        tracker.stop()

    def test_progress_events_repaint_via_refresh(self, monkeypatch) -> None:
        # Event-driven repaints go through refresh() (re-render via the
        # callable), never update(<static text>), which would reinstate
        # a frozen snapshot.
        monkeypatch.setattr(pt, "Live", _RecordingLive)
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        tracker.start()
        live = tracker.live
        tracker.set_total_prs(3)
        tracker.merge_success()
        tracker.merge_failure()
        assert live.refresh_calls >= 3
        assert live.update_calls == 0
        tracker.stop()


class TestElapsedRecomputesPerRender:
    def test_merge_tracker_elapsed_reflects_current_time(self) -> None:
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        tracker.start_time = datetime.now() - timedelta(seconds=90)
        text = tracker._generate_display_text()
        assert "Elapsed: 1m" in text.plain

    def test_base_tracker_elapsed_reflects_current_time(self) -> None:
        tracker = ProgressTracker("owner")
        tracker.rich_available = True
        tracker.start_time = datetime.now() - timedelta(seconds=90)
        text = tracker._generate_display_text()
        assert "Elapsed: 1m" in text.plain
