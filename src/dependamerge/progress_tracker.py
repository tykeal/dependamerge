# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from typing import Any

try:
    from rich.console import Console  # pyright: ignore[reportAssignmentType]
    from rich.live import Live  # pyright: ignore[reportAssignmentType]
    from rich.text import Text  # pyright: ignore[reportAssignmentType]

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

    class Live:  # type: ignore[no-redef]  # pyright: ignore[reportRedefinition]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def update(self, *args: Any) -> None:
            pass

        def refresh(self) -> None:
            pass

    class Text:  # type: ignore[no-redef]  # pyright: ignore[reportRedefinition]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def append(self, *args: Any, **kwargs: Any) -> None:
            pass

    class Console:  # type: ignore[no-redef]  # pyright: ignore[reportRedefinition]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass


class ProgressTracker:
    """Real-time progress tracker for organization blocked PR checking operations."""

    def __init__(self, organization: str, show_pr_stats: bool = True):
        """Initialize progress tracker for an organization blocked PR check.

        Args:
            organization: Name of the GitHub organization being checked
            show_pr_stats: Whether to show PR analysis statistics (default True)
        """
        self.organization = organization
        self.start_time = datetime.now()
        self.console: Any = Console() if RICH_AVAILABLE else None

        # Progress counters
        self.total_repositories = 0
        self.completed_repositories = 0
        self.current_repository = ""
        self.total_prs_analyzed = 0
        self.unmergeable_prs_found = 0
        self.current_operation = "Initializing..."
        self.errors_count = 0

        # Configuration
        self.show_pr_stats = show_pr_stats

        # Rate limiting tracking
        self.rate_limited = False
        self.rate_limit_reset_time: datetime | None = None

        # Rich Live display
        self.live: Any = None
        self.rich_available = RICH_AVAILABLE
        self.paused = False
        # Metrics (optional; displayed when provided)
        self.metrics_concurrency: int | None = None
        self.metrics_rps: float | None = None

        # Fallback for when Rich is not available
        self._last_display = ""
        # Terminal-bound logging handlers silenced while the live
        # display is on screen; each entry is ``(handler, saved_level)``
        # so the original level can be restored on stop.
        self._quieted_handlers: list[tuple[logging.Handler, int]] = []

    @staticmethod
    def _stream_is_tty(stream: Any) -> bool:
        """Return True only when ``stream`` is a real terminal.

        A stream may be a proxy or capture object that lacks ``isatty``
        (or whose ``isatty`` raises); treat a missing or failing
        ``isatty`` as non-TTY rather than letting an ``AttributeError``
        escape and break display setup or teardown.
        """
        isatty = getattr(stream, "isatty", None)
        if not callable(isatty):
            return False
        try:
            return bool(isatty())
        except Exception:
            return False

    @staticmethod
    def _stdout_is_tty() -> bool:
        """Return True only when stdout is a real terminal."""
        return ProgressTracker._stream_is_tty(sys.stdout)

    def _quiet_terminal_logging(self) -> None:
        """Silence terminal-bound logging while the live display runs.

        ``logging.basicConfig`` binds a ``StreamHandler`` to the real
        ``sys.stderr`` at startup, before the Rich ``Live`` display
        swaps in its own stdout/stderr proxies.  Because the handler
        cached the original stream, a ``WARNING``/``ERROR`` logged
        while the live region is on screen writes straight past Rich
        to the terminal and desyncs the region: the top line is
        orphaned and the whole block shifts down a row (the reported
        duplicated-header artifact seen when a merge failed).

        Real-merge progress is conveyed by the live counters and
        explained in the end-of-run summary, so while the display is
        active we raise such handlers above ``CRITICAL`` and restore
        them when it stops.  Only stream handlers writing to a real
        terminal are touched: the stream must be one of the process's
        std streams *and* report ``isatty()`` true.  This leaves file
        handlers, pytest capture, and handlers whose ``stderr`` has been
        redirected to a file untouched, so their warnings/errors are
        never lost (there is no Rich desync risk when the target is not
        a terminal).
        """
        self._quieted_handlers = []
        if not self._stdout_is_tty():
            return
        terminal_streams = {
            stream
            for stream in (sys.__stdout__, sys.__stderr__, sys.stdout, sys.stderr)
            if stream is not None
        }
        try:
            handlers = list(logging.getLogger().handlers)
        except Exception:
            return
        for handler in handlers:
            stream = getattr(handler, "stream", None)
            if (
                isinstance(handler, logging.StreamHandler)
                and stream in terminal_streams
                and self._stream_is_tty(stream)
            ):
                self._quieted_handlers.append((handler, handler.level))
                handler.setLevel(logging.CRITICAL + 1)

    def _restore_terminal_logging(self) -> None:
        """Restore logging handlers quieted by :meth:`_quiet_terminal_logging`."""
        for handler, level in self._quieted_handlers:
            try:
                handler.setLevel(level)
            except Exception:
                # Best-effort restore: never let logging teardown
                # raise out of display teardown.
                pass
        self._quieted_handlers = []

    def start(self) -> None:
        """Start the live progress display."""
        if not self.rich_available:
            return

        try:
            # Pass a callable rather than a static renderable: Rich
            # re-invokes it on every auto-refresh tick, so the elapsed
            # clock keeps advancing even when no progress events fire
            # (long silent API sequences used to freeze the display).
            self.live = Live(
                get_renderable=self._generate_display_text,
                console=self.console,
                refresh_per_second=2,
                transient=False,
            )
            if self.live:
                # Quiet terminal logging *before* starting Live: if quieting
                # raised after the start, the ``except`` path would drop the
                # reference without stopping the already-started display,
                # leaving it orphaned. Ordering it first also closes the
                # window where a log could slip past Rich right after start.
                self._quiet_terminal_logging()
                self.live.start()
        except Exception:
            # Fallback if Rich display fails (e.g., unsupported terminal)
            self._restore_terminal_logging()
            self.live = None
            self.rich_available = False

    def stop(self) -> None:
        """Stop the live progress display."""
        # Stop the live display *before* restoring terminal logging so the
        # teardown itself stays quiet even if ``live.stop()`` raises;
        # restore always runs via ``finally``.
        try:
            if self.live:
                try:
                    self.live.stop()
                except Exception:
                    # Best-effort teardown: ignore errors from Rich when
                    # the terminal no longer accepts control sequences.
                    pass
            else:
                # Non-Rich fallback: emit a final newline so the shell
                # prompt doesn't appear mid-line after carriage-return
                # in-place updates.
                if self._last_display and self._stdout_is_tty():
                    print(flush=True)
        finally:
            self._restore_terminal_logging()
        self.live = None
        self.paused = False

    def suspend(self) -> None:
        """Temporarily suspend the live display (e.g. for interactive prompts)."""
        if self.live:
            # Stop the live display first, then restore logging in
            # ``finally`` so teardown stays quiet even if ``live.stop()``
            # raises and handlers are always restored for the prompt.
            try:
                self.live.stop()
            except Exception:
                # Best-effort suspend: ignore Rich teardown errors so an
                # interactive prompt can still take over the terminal.
                pass
            finally:
                self._restore_terminal_logging()
            self.paused = True

    def resume(self) -> None:
        """Resume the live display after it was suspended."""
        if self.rich_available and self.paused:
            try:
                self.live = Live(
                    get_renderable=self._generate_display_text,
                    console=self.console,
                    refresh_per_second=2,
                    transient=False,
                )
                if self.live:
                    # Quiet before starting Live for the same reason as
                    # ``start()``: avoid orphaning a started display if
                    # quieting raises, and close the log-slip window.
                    self._quiet_terminal_logging()
                    self.live.start()
            except Exception:
                self._restore_terminal_logging()
                self.live = None
                self.rich_available = False
            self.paused = False

    def update_metrics(self, concurrency: int, rps: float) -> None:
        self.metrics_concurrency = concurrency
        self.metrics_rps = rps
        self._refresh_display()

    def clear_metrics(self) -> None:
        self.metrics_concurrency = None
        self.metrics_rps = None
        self._refresh_display()

    def update_total_repositories(self, total: int) -> None:
        """Update the total number of repositories to scan."""
        self.total_repositories = total
        self._refresh_display()

    def start_repository(self, repo_name: str) -> None:
        """Mark the start of scanning a repository."""
        self.current_repository = repo_name
        self.current_operation = f"Scanning {repo_name}..."
        self._refresh_display()

    def complete_repository(self, unmergeable_count: int = 0) -> None:
        """Mark completion of a repository check."""
        self.completed_repositories += 1
        self.unmergeable_prs_found += unmergeable_count
        self.current_operation = ""
        self.current_repository = ""
        self._refresh_display()

    def update_operation(self, operation: str) -> None:
        """Update the current operation description."""
        self.current_operation = operation
        self._refresh_display()

    def analyze_pr(self, pr_number: int, repo_name: str = "") -> None:
        """Mark the start of analyzing a specific PR."""
        self.total_prs_analyzed += 1
        if repo_name:
            self.current_operation = f"Analyzing PR #{pr_number} in {repo_name}"
        else:
            self.current_operation = f"Analyzing PR #{pr_number}..."
        self._refresh_display()

    def add_error(self) -> None:
        """Increment the error counter."""
        self.errors_count += 1
        self._refresh_display()

    def set_rate_limited(self, reset_time: datetime | None = None) -> None:
        """Mark that we're rate limited."""
        self.rate_limited = True
        self.rate_limit_reset_time = reset_time
        self._refresh_display()

    def clear_rate_limited(self) -> None:
        """Clear rate limit state."""
        self.rate_limited = False
        self.rate_limit_reset_time = None
        self._refresh_display()

    def _refresh_display(self) -> None:
        """Repaint the live display with current progress.

        The Live instance renders via ``get_renderable``, so a plain
        ``refresh()`` repaints with current state immediately instead
        of waiting for the next auto-refresh tick.
        """
        if self.live and self.rich_available and not self.paused:
            try:
                self.live.refresh()
            except Exception:
                # If Rich display fails, fall back to simple print
                self._fallback_display()
        elif not self.rich_available:
            self._fallback_display()

    def _generate_display_text(self) -> Any:
        """Generate the current progress display text."""
        if not self.rich_available:
            return Text()

        text = Text()

        # Main progress line
        if self.total_repositories > 0:
            progress_pct = (self.completed_repositories / self.total_repositories) * 100
            text.append("🔍 Checking ", style="bold blue")
            text.append(f"{self.organization} ", style="bold cyan")
            text.append(
                f"({self.completed_repositories}/{self.total_repositories} repos, ",
                style="dim",
            )
            text.append(f"{progress_pct:.0f}%", style="bold green")
            text.append(")", style="dim")
        else:
            text.append("🔍 Checking ", style="bold blue")
            text.append(f"{self.organization} ", style="bold cyan")
            text.append("(initializing...)", style="dim")

        # Current operation
        if self.current_operation:
            text.append(f"\n   {self.current_operation}", style="dim")

        # Stats line (optional)
        if self.show_pr_stats and self.total_prs_analyzed > 0:
            text.append("\n   📊 PRs analyzed: ", style="dim")
            text.append(str(self.total_prs_analyzed), style="bold")
            if self.unmergeable_prs_found > 0:
                text.append(" | ⚠️ Unmergeable: ", style="dim")
                text.append(str(self.unmergeable_prs_found), style="bold yellow")

        # Metrics line (concurrency / requests-per-second)
        if self.metrics_concurrency is not None or self.metrics_rps is not None:
            parts: list[str] = []
            if self.metrics_concurrency is not None:
                parts.append(f"concurrency={self.metrics_concurrency}")
            if self.metrics_rps is not None:
                parts.append(f"rps={self.metrics_rps:.1f}")
            text.append(f"\n   ⚡ {', '.join(parts)}", style="dim")

        # Error count
        if self.errors_count > 0:
            text.append(f"\n   ❌ Errors: {self.errors_count}", style="bold red")

        # Rate limit indicator
        if self.rate_limited:
            text.append("\n   ⏳ Rate limited", style="bold yellow")
            if self.rate_limit_reset_time:
                remaining = self.rate_limit_reset_time - datetime.now()
                if remaining.total_seconds() > 0:
                    text.append(
                        f" (resets in {self._format_duration(remaining)})",
                        style="yellow",
                    )

        # Elapsed time
        elapsed = datetime.now() - self.start_time
        text.append(f"\n   ⏱️ Elapsed: {self._format_duration(elapsed)}", style="dim")

        return text

    def _fallback_display(self) -> None:
        """Simple text fallback when Rich is not available."""
        if self.total_repositories > 0:
            progress_pct = (self.completed_repositories / self.total_repositories) * 100
            display = (
                f"Progress: {self.completed_repositories}/{self.total_repositories} "
                f"repos ({progress_pct:.0f}%)"
            )
        else:
            display = "Initializing..."

        if self.current_operation:
            display += f" | {self.current_operation}"

        if self.total_prs_analyzed > 0:
            display += f" | PRs: {self.total_prs_analyzed}"

        if self.errors_count > 0:
            display += f" | Errors: {self.errors_count}"

        # Only print if display has changed
        if display != self._last_display:
            if self._stdout_is_tty():
                # \033[K clears from cursor to end-of-line so shorter
                # updates don't leave trailing characters from the
                # previous render.
                print(f"\r{display}\033[K", end="", flush=True)
            else:
                print(display)
            self._last_display = display

    def _format_duration(self, td: timedelta) -> str:
        """Format a timedelta as a human-readable duration string."""
        total_seconds = int(td.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds}s"
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        if minutes < 60:
            return f"{minutes}m {seconds}s"
        hours = minutes // 60
        minutes = minutes % 60
        return f"{hours}h {minutes}m {seconds}s"

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of the progress tracking."""
        elapsed = datetime.now() - self.start_time
        formatted = self._format_duration(elapsed)
        return {
            "organization": self.organization,
            "total_repositories": self.total_repositories,
            "completed_repositories": self.completed_repositories,
            "total_prs_analyzed": self.total_prs_analyzed,
            "unmergeable_prs_found": self.unmergeable_prs_found,
            "errors_count": self.errors_count,
            "elapsed_seconds": elapsed.total_seconds(),
            "elapsed_formatted": formatted,
            # Backward-compatible alias used by cli.py
            "elapsed_time": formatted,
        }


class MergeProgressTracker(ProgressTracker):
    """Extended progress tracker with merge-specific metrics.

    In addition to the terminal counters (merged / failed / skipped /
    blocked / pending / closed), PRs move through **transitory**
    display states while the merge pipeline operates on them
    (``rebasing`` → ``rebased`` → ``waiting`` → terminal).  Transitory
    states are keyed by PR so a PR occupies at most one state at a
    time; recording a terminal outcome removes the PR from whatever
    transitory state it was in.
    """

    # Transitory display states in pipeline order.  Each entry is
    # ``(state_key, display_label)``; only non-zero states render.
    _STATE_ORDER: tuple[tuple[str, str], ...] = (
        ("rebasing", "🔄 Rebasing"),
        ("rebased", "⬆️ Rebased"),
        ("recreating", "♻️ Recreating"),
        ("waiting", "⏳ Waiting"),
    )

    def __init__(
        self,
        organization: str,
        is_close_operation: bool = False,
        operation_label: str | None = None,
        operation_icon: str | None = None,
    ):
        """Initialize merge progress tracker.

        Args:
            organization: Name of the GitHub organization or owner.
            is_close_operation: Whether this tracks a close operation.
            operation_label: Custom heading label for the progress
                display.  When ``None``, defaults to
                ``"Searching for similar PRs"`` (merge) or
                ``"Closing PRs"`` (close).
            operation_icon: Custom emoji icon for the heading.  When
                ``None``, defaults to ``"🔀"`` / ``"🔍"`` (merge) or
                ``"🚪"`` (close) depending on context.
        """
        super().__init__(organization, show_pr_stats=True)
        self.similar_prs_found = 0
        self.prs_merged = 0
        self.prs_failed = 0
        self.prs_skipped = 0
        self.prs_closed = 0
        # PRs left with auto-merge armed when the run ended (GitHub
        # completes the merge server-side once checks pass).
        self.prs_pending = 0
        # PRs that are blocked and cannot be merged by this run.
        self.prs_blocked = 0
        self.is_close_operation = is_close_operation
        self._custom_label = operation_label
        self._custom_icon = operation_icon
        # PR-level progress (used for repo-scoped operations)
        self.total_prs = 0
        self.completed_prs = 0
        # Transitory per-PR display states: pr_key -> state key from
        # ``_STATE_ORDER``.  Terminal outcomes remove the entry.
        self._pr_states: dict[str, str] = {}

    def found_similar_pr(self, count: int = 1) -> None:
        """Update count of similar PRs found."""
        self.similar_prs_found += count
        self._refresh_display()

    def set_total_prs(self, total: int) -> None:
        """Set the total number of PRs to process.

        When set, the progress display switches from repo-level
        to PR-level progress (e.g. ``3/9 PRs, 33%``).
        """
        self.total_prs = total
        self._refresh_display()

    def track_pr_state(self, pr_key: str, state: str | None) -> None:
        """Move a PR between transitory display states.

        ``state`` is one of the keys in ``_STATE_ORDER`` (e.g.
        ``"rebasing"``, ``"rebased"``, ``"waiting"``) or ``None`` to
        clear the PR's entry when an operation finishes without
        reaching a terminal outcome.  Terminal outcomes are recorded
        via ``merge_success`` / ``merge_failure`` / ``merge_skipped``
        / ``merge_blocked`` / ``merge_pending``, which also clear the
        transitory entry when given the PR key.
        """
        if state is None:
            self._pr_states.pop(pr_key, None)
        else:
            self._pr_states[pr_key] = state
        self._refresh_display()

    def _finish_pr(self, pr_key: str | None) -> None:
        """Shared terminal-outcome bookkeeping.

        Clears the PR's transitory state (when a key is supplied) and
        advances PR-level completion progress.
        """
        if pr_key is not None:
            self._pr_states.pop(pr_key, None)
        if self.total_prs > 0:
            self.completed_prs += 1

    def merge_success(self, pr_key: str | None = None) -> None:
        """Record a successful merge."""
        self._finish_pr(pr_key)
        self.prs_merged += 1
        self._refresh_display()

    def merge_failure(self, pr_key: str | None = None) -> None:
        """Record a failed merge."""
        self._finish_pr(pr_key)
        self.prs_failed += 1
        self._refresh_display()

    def merge_skipped(self, pr_key: str | None = None) -> None:
        """Record a PR skipped because it was merged externally.

        Distinct from ``merge_failure`` because the operator does
        not need to follow up: the PR is already merged, just not
        by us.  Tracked separately so the final summary can show
        a non-zero ⏭️ Skipped count alongside Merged / Failed.
        """
        self._finish_pr(pr_key)
        self.prs_skipped += 1
        self._refresh_display()

    def merge_blocked(self, pr_key: str | None = None) -> None:
        """Record a PR that is blocked and cannot merge in this run."""
        self._finish_pr(pr_key)
        self.prs_blocked += 1
        self._refresh_display()

    def merge_pending(self, pr_key: str | None = None) -> None:
        """Record a PR left with auto-merge armed at run end.

        GitHub merges the PR server-side once its required checks
        pass; from this run's perspective the PR is terminal but
        neither merged nor failed.
        """
        self._finish_pr(pr_key)
        self.prs_pending += 1
        self._refresh_display()

    def increment_closed(self, pr_key: str | None = None) -> None:
        """Record a successful close."""
        self._finish_pr(pr_key)
        self.prs_closed += 1
        self._refresh_display()

    def pr_completed(self) -> None:
        """Record a PR as processed without changing status counters.

        Use this for terminal outcomes that bypass the dedicated
        counter methods.  Those methods already increment
        ``completed_prs``; this one exists solely to keep the
        progress percentage accurate for terminal states that no
        counter method covers.
        """
        if self.total_prs > 0:
            self.completed_prs += 1
        self._refresh_display()

    def _generate_display_text(self) -> Any:
        """Generate merge-specific display text."""
        if not self.rich_available:
            return Text()

        text = Text()

        # Resolve label and icon — use custom values when provided,
        # otherwise fall back to the default close/merge text.
        default_label = (
            "Closing PRs" if self.is_close_operation else "Searching for similar PRs"
        )
        label = self._custom_label or default_label

        # Main progress line for merge/close operations.
        # PR-level progress takes priority over repo-level progress
        # so repo-scoped merges show "3/9 PRs" instead of "0/1 repos".
        if self.total_prs > 0:
            progress_pct = (self.completed_prs / self.total_prs) * 100
            default_icon = "🚪" if self.is_close_operation else "🔀"
            icon = self._custom_icon or default_icon
            text.append(f"{icon} {label} in ", style="bold blue")
            text.append(f"{self.organization} ", style="bold cyan")
            text.append(
                f"({self.completed_prs}/{self.total_prs} PRs, ",
                style="dim",
            )
            text.append(f"{progress_pct:.0f}%", style="bold green")
            text.append(")", style="dim")
        elif self.total_repositories > 0:
            progress_pct = (self.completed_repositories / self.total_repositories) * 100
            default_icon = "🚪" if self.is_close_operation else "🔀"
            icon = self._custom_icon or default_icon
            text.append(f"{icon} {label} in ", style="bold blue")
            text.append(f"{self.organization} ", style="bold cyan")
            text.append(
                f"({self.completed_repositories}/{self.total_repositories} repos, ",
                style="dim",
            )
            text.append(f"{progress_pct:.0f}%", style="bold green")
            text.append(")", style="dim")
        else:
            default_icon = "🚪" if self.is_close_operation else "🔍"
            icon = self._custom_icon or default_icon
            text.append(f"{icon} {label} in ", style="bold blue")
            text.append(f"{self.organization} ", style="bold cyan")

        # Current operation
        if self.current_operation:
            text.append(f"\n   {self.current_operation}", style="dim")

        # Merge stats — transitory pipeline states first (in flow
        # order), then terminal outcomes.
        stats_parts: list[str] = []
        if self.similar_prs_found > 0:
            stats_parts.append(f"🔁 Similar: {self.similar_prs_found}")
        state_counts: dict[str, int] = {}
        for pr_state in self._pr_states.values():
            state_counts[pr_state] = state_counts.get(pr_state, 0) + 1
        for state_key, label in self._STATE_ORDER:
            count = state_counts.get(state_key, 0)
            if count > 0:
                stats_parts.append(f"{label}: {count}")
        # Defensive: render unknown states too (sorted for stable
        # output) so a new caller-supplied state is never silently
        # dropped from the display.
        known_states = {key for key, _ in self._STATE_ORDER}
        for state_key in sorted(state_counts):
            if state_key not in known_states:
                stats_parts.append(
                    f"{state_key.capitalize()}: {state_counts[state_key]}"
                )
        if self.prs_merged > 0:
            stats_parts.append(f"✅ Merged: {self.prs_merged}")
        if self.prs_pending > 0:
            stats_parts.append(f"🤖 Pending: {self.prs_pending}")
        if self.prs_closed > 0:
            stats_parts.append(f"🚪 Closed: {self.prs_closed}")
        if self.prs_failed > 0:
            stats_parts.append(f"❌ Failed: {self.prs_failed}")
        if self.prs_skipped > 0:
            stats_parts.append(f"⏭️ Skipped: {self.prs_skipped}")
        if self.prs_blocked > 0:
            stats_parts.append(f"🛑 Blocked: {self.prs_blocked}")

        if stats_parts:
            text.append(f"\n   {' | '.join(stats_parts)}", style="dim")

        # Metrics line
        if self.metrics_concurrency is not None or self.metrics_rps is not None:
            parts: list[str] = []
            if self.metrics_concurrency is not None:
                parts.append(f"concurrency={self.metrics_concurrency}")
            if self.metrics_rps is not None:
                parts.append(f"rps={self.metrics_rps:.1f}")
            text.append(f"\n   ⚡ {', '.join(parts)}", style="dim")

        # Error count
        if self.errors_count > 0:
            text.append(f"\n   ❌ Errors: {self.errors_count}", style="bold red")

        # Rate limit indicator
        if self.rate_limited:
            text.append("\n   ⏳ Rate limited", style="bold yellow")

        # Elapsed time
        elapsed = datetime.now() - self.start_time
        text.append(f"\n   ⏱️ Elapsed: {self._format_duration(elapsed)}", style="dim")

        return text

    def get_summary(self) -> dict[str, Any]:
        """Get merge-specific summary."""
        base = super().get_summary()
        base.update(
            {
                "similar_prs_found": self.similar_prs_found,
                "prs_merged": self.prs_merged,
                "prs_failed": self.prs_failed,
                "prs_skipped": self.prs_skipped,
                "prs_blocked": self.prs_blocked,
                "prs_pending": self.prs_pending,
                "prs_closed": self.prs_closed,
                "total_prs": self.total_prs,
                "completed_prs": self.completed_prs,
            }
        )
        return base


class DummyProgressTracker(ProgressTracker):
    """A no-op progress tracker for when progress display is disabled."""

    def __init__(self) -> None:
        # Initialize the base tracker, then neutralize Rich so this
        # stand-in performs no terminal output.
        super().__init__("", show_pr_stats=False)
        self.console = None
        self.rich_available = False
        self.current_operation = ""
        # MergeProgressTracker fields (Dummy stands in for either tracker)
        self.similar_prs_found = 0
        self.prs_merged = 0
        self.prs_failed = 0
        self.prs_skipped = 0
        self.prs_blocked = 0
        self.prs_pending = 0
        self.prs_closed = 0
        self.is_close_operation = False
        self._custom_label: str | None = None
        self._custom_icon: str | None = None
        self.total_prs = 0
        self.completed_prs = 0
        self._pr_states: dict[str, str] = {}

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def update_total_repositories(self, total: int) -> None:
        pass

    def start_repository(self, repo_name: str) -> None:
        pass

    def complete_repository(self, unmergeable_count: int = 0) -> None:
        pass

    def update_operation(self, operation: str) -> None:
        pass

    def analyze_pr(self, pr_number: int, repo_name: str = "") -> None:
        pass

    def add_error(self) -> None:
        pass

    def set_rate_limited(self, reset_time: datetime | None = None) -> None:
        pass

    def clear_rate_limited(self) -> None:
        pass

    def set_total_prs(self, total: int) -> None:
        pass

    def pr_completed(self) -> None:
        pass

    def found_similar_pr(self, count: int = 1) -> None:
        pass

    def track_pr_state(self, pr_key: str, state: str | None) -> None:
        pass

    def merge_success(self, pr_key: str | None = None) -> None:
        pass

    def merge_failure(self, pr_key: str | None = None) -> None:
        pass

    def merge_skipped(self, pr_key: str | None = None) -> None:
        pass

    def merge_blocked(self, pr_key: str | None = None) -> None:
        pass

    def merge_pending(self, pr_key: str | None = None) -> None:
        pass

    def increment_closed(self, pr_key: str | None = None) -> None:
        pass

    def _refresh_display(self) -> None:
        pass

    def _fallback_display(self) -> None:
        pass

    def get_summary(self) -> dict[str, Any]:
        return {}
