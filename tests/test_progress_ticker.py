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

import logging
import sys
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


class TestTerminalLoggingQuieted:
    """While the live display is on screen, terminal-bound logging
    handlers are silenced so a ``WARNING``/``ERROR`` (e.g. the
    ``❌ Failed`` per-PR line) cannot write past Rich and desync the
    live region (an orphaned header line, the block shifted down a
    row).  Handlers are restored on stop/suspend and re-silenced on
    resume.
    """

    def _make_terminal_handler(self) -> tuple[Any, int]:
        # Bind to sys.stdout so the single stdout ``isatty`` monkeypatch
        # in each test governs both the top-level guard and the
        # per-stream TTY check in ``_quiet_terminal_logging``.
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.WARNING)
        return handler, handler.level

    def test_start_quiets_and_stop_restores(self, monkeypatch) -> None:
        monkeypatch.setattr(pt, "Live", _RecordingLive)
        # Pretend we are attached to a real TTY so quieting engages.
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        handler, original_level = self._make_terminal_handler()
        root = logging.getLogger()
        root.addHandler(handler)
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        try:
            tracker.start()
            # Quieted above CRITICAL while the display is live.
            assert handler.level > logging.CRITICAL
            tracker.stop()
            # Restored to the original level after teardown.
            assert handler.level == original_level
        finally:
            tracker.stop()
            root.removeHandler(handler)

    def test_suspend_restores_and_resume_requiets(self, monkeypatch) -> None:
        monkeypatch.setattr(pt, "Live", _RecordingLive)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        handler, original_level = self._make_terminal_handler()
        root = logging.getLogger()
        root.addHandler(handler)
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        try:
            tracker.start()
            assert handler.level > logging.CRITICAL
            tracker.suspend()
            assert handler.level == original_level
            tracker.resume()
            assert handler.level > logging.CRITICAL
            tracker.stop()
            assert handler.level == original_level
        finally:
            tracker.stop()
            root.removeHandler(handler)

    def test_non_tty_leaves_handlers_untouched(self, monkeypatch) -> None:
        monkeypatch.setattr(pt, "Live", _RecordingLive)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
        handler, original_level = self._make_terminal_handler()
        root = logging.getLogger()
        root.addHandler(handler)
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        try:
            tracker.start()
            # Captured (non-tty) output must never be disturbed.
            assert handler.level == original_level
            tracker.stop()
            assert handler.level == original_level
        finally:
            tracker.stop()
            root.removeHandler(handler)

    def test_file_handler_is_not_quieted(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(pt, "Live", _RecordingLive)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        log_file = tmp_path / "run.log"
        handler = logging.FileHandler(log_file)
        handler.setLevel(logging.WARNING)
        root = logging.getLogger()
        root.addHandler(handler)
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        try:
            tracker.start()
            # File logging must keep flowing at its configured level.
            assert handler.level == logging.WARNING
            tracker.stop()
        finally:
            tracker.stop()
            root.removeHandler(handler)
            handler.close()

    def test_stderr_redirected_to_file_is_not_quieted(self, monkeypatch) -> None:
        """A stderr handler is left alone when stderr is not a real TTY.

        If stderr is redirected to a file, silencing its handler would
        drop warnings/errors with no Rich desync benefit (Rich renders
        to stdout). Only handlers whose stream is an actual terminal are
        quieted, so a non-TTY stderr handler must keep its level even
        while the live display is active.
        """
        monkeypatch.setattr(pt, "Live", _RecordingLive)
        # stdout is a real terminal (top-level guard passes)...
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        # ...but stderr has been redirected to a file (not a TTY).
        monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.WARNING)
        original_level = handler.level
        root = logging.getLogger()
        root.addHandler(handler)
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        try:
            tracker.start()
            # The non-terminal stderr handler must keep its level.
            assert handler.level == original_level
        finally:
            tracker.stop()
            root.removeHandler(handler)

    def test_teardown_stops_live_before_restoring_handlers(self, monkeypatch) -> None:
        """``live.stop()`` must run while logging is still quieted.

        Restoring terminal handlers before the live region is torn down
        reopens the window where a stray log record can write past Rich.
        The recorded handler level captured inside ``live.stop()`` must
        therefore still be above CRITICAL, with restoration happening
        only afterward.
        """
        handler, original_level = self._make_terminal_handler()
        recorded: dict[str, int] = {}

        class _StopRecordingLive(_RecordingLive):
            def stop(self) -> None:
                recorded["level_at_stop"] = handler.level
                super().stop()

        monkeypatch.setattr(pt, "Live", _StopRecordingLive)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        root = logging.getLogger()
        root.addHandler(handler)
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        try:
            tracker.start()
            assert handler.level > logging.CRITICAL
            tracker.stop()
            # The live display was torn down while still quieted...
            assert recorded["level_at_stop"] > logging.CRITICAL
            # ...and the handler was restored only afterward.
            assert handler.level == original_level
        finally:
            tracker.stop()
            root.removeHandler(handler)

    def test_suspend_stops_live_before_restoring_handlers(self, monkeypatch) -> None:
        """``suspend()`` has the same ordering guarantee as ``stop()``."""
        handler, original_level = self._make_terminal_handler()
        recorded: dict[str, int] = {}

        class _StopRecordingLive(_RecordingLive):
            def stop(self) -> None:
                recorded["level_at_stop"] = handler.level
                super().stop()

        monkeypatch.setattr(pt, "Live", _StopRecordingLive)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        root = logging.getLogger()
        root.addHandler(handler)
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        try:
            tracker.start()
            tracker.suspend()
            assert recorded["level_at_stop"] > logging.CRITICAL
            assert handler.level == original_level
            tracker.stop()
        finally:
            tracker.stop()
            root.removeHandler(handler)


class TestSafeStdoutIsTty:
    """``_stdout_is_tty`` must never raise when stdout lacks ``isatty``.

    Some capture/proxy setups swap ``sys.stdout`` for an object without
    an ``isatty`` method (or whose ``isatty`` raises). A direct
    ``sys.stdout.isatty()`` call would then raise ``AttributeError`` and
    break display start/teardown; the helper must degrade to non-TTY.
    """

    def test_missing_isatty_degrades_to_non_tty(self, monkeypatch) -> None:
        class _NoIsattyStream:
            def write(self, *args: Any) -> None:
                pass

            def flush(self) -> None:
                pass

        monkeypatch.setattr(sys, "stdout", _NoIsattyStream())
        assert ProgressTracker._stdout_is_tty() is False

    def test_raising_isatty_degrades_to_non_tty(self, monkeypatch) -> None:
        class _RaisingStream:
            def isatty(self) -> bool:
                raise ValueError("no tty here")

        monkeypatch.setattr(sys, "stdout", _RaisingStream())
        assert ProgressTracker._stdout_is_tty() is False

    def test_start_does_not_raise_when_isatty_missing(self, monkeypatch) -> None:
        """Quieting must no-op (not crash) when stdout has no ``isatty``."""

        class _NoIsattyStream:
            def write(self, *args: Any) -> None:
                pass

            def flush(self) -> None:
                pass

        monkeypatch.setattr(pt, "Live", _RecordingLive)
        monkeypatch.setattr(sys, "stdout", _NoIsattyStream())
        tracker = MergeProgressTracker("owner")
        tracker.rich_available = True
        try:
            # Must not raise AttributeError from a missing isatty.
            tracker.start()
            assert tracker._quieted_handlers == []
        finally:
            tracker.stop()
