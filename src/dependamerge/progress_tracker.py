# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

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

    def start(self) -> None:
        """Start the live progress display."""
        if not self.rich_available:
            return

        try:
            self.live = Live(
                self._generate_display_text(),
                console=self.console,
                refresh_per_second=2,
                transient=False,
            )
            if self.live:
                self.live.start()
        except Exception:
            # Fallback if Rich display fails (e.g., unsupported terminal)
            self.live = None
            self.rich_available = False

    def stop(self) -> None:
        """Stop the live progress display."""
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
            if self._last_display and sys.stdout.isatty():
                print(flush=True)
        self.live = None
        self.paused = False

    def suspend(self) -> None:
        """Temporarily suspend the live display (e.g. for interactive prompts)."""
        if self.live:
            try:
                self.live.stop()
            except Exception:
                # Best-effort suspend: ignore Rich teardown errors so an
                # interactive prompt can still take over the terminal.
                pass
            self.paused = True

    def resume(self) -> None:
        """Resume the live display after it was suspended."""
        if self.rich_available and self.paused:
            try:
                self.live = Live(
                    self._generate_display_text(),
                    console=self.console,
                    refresh_per_second=2,
                    transient=False,
                )
                if self.live:
                    self.live.start()
            except Exception:
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
        """Refresh the live display with current progress."""
        if self.live and self.rich_available and not self.paused:
            try:
                self.live.update(self._generate_display_text())
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
            if sys.stdout.isatty():
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
    """Extended progress tracker with merge-specific metrics."""

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
        self.is_close_operation = is_close_operation
        self._custom_label = operation_label
        self._custom_icon = operation_icon
        # PR-level progress (used for repo-scoped operations)
        self.total_prs = 0
        self.completed_prs = 0

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

    def merge_success(self) -> None:
        """Record a successful merge."""
        self.prs_merged += 1
        if self.total_prs > 0:
            self.completed_prs += 1
        self._refresh_display()

    def merge_failure(self) -> None:
        """Record a failed merge."""
        self.prs_failed += 1
        if self.total_prs > 0:
            self.completed_prs += 1
        self._refresh_display()

    def merge_skipped(self) -> None:
        """Record a PR skipped because it was merged externally.

        Distinct from ``merge_failure`` because the operator does
        not need to follow up: the PR is already merged, just not
        by us.  Tracked separately so the final summary can show
        a non-zero ⏭️ Skipped count alongside Merged / Failed.
        """
        self.prs_skipped += 1
        if self.total_prs > 0:
            self.completed_prs += 1
        self._refresh_display()

    def increment_closed(self) -> None:
        """Record a successful close."""
        self.prs_closed += 1
        if self.total_prs > 0:
            self.completed_prs += 1
        self._refresh_display()

    def pr_completed(self) -> None:
        """Record a PR as processed without changing status counters.

        Use this for BLOCKED/SKIPPED outcomes that bypass
        ``merge_success()`` and ``merge_failure()``.  Those
        methods already increment ``completed_prs``; this one
        exists solely to keep the progress percentage accurate
        for terminal states that neither method covers.
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

        # Merge stats
        stats_parts: list[str] = []
        if self.similar_prs_found > 0:
            stats_parts.append(f"🔁 Similar: {self.similar_prs_found}")
        if self.prs_merged > 0:
            stats_parts.append(f"✅ Merged: {self.prs_merged}")
        if self.prs_closed > 0:
            stats_parts.append(f"🚪 Closed: {self.prs_closed}")
        if self.prs_failed > 0:
            stats_parts.append(f"❌ Failed: {self.prs_failed}")
        if self.prs_skipped > 0:
            stats_parts.append(f"⏭️ Skipped: {self.prs_skipped}")

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
        self.prs_closed = 0
        self.is_close_operation = False
        self._custom_label: str | None = None
        self._custom_icon: str | None = None
        self.total_prs = 0
        self.completed_prs = 0

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

    def merge_success(self) -> None:
        pass

    def merge_failure(self) -> None:
        pass

    def merge_skipped(self) -> None:
        pass

    def _refresh_display(self) -> None:
        pass

    def _fallback_display(self) -> None:
        pass

    def get_summary(self) -> dict[str, Any]:
        return {}
