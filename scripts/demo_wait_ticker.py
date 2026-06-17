#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The Linux Foundation
# SPDX-License-Identifier: Apache-2.0
"""Manual demo for the merge-manager wait-status ticker.

Implements **Option C** from
``docs/TESTING_WAIT_STATUS_TICKER.md`` — a self-contained script
that exercises :meth:`AsyncMergeManager._wait_status_ticker` (and
its plain-console fallback) without making any GitHub API calls.

The script:

1. Builds a real :class:`MergeProgressTracker` and starts it.
2. Builds a real :class:`AsyncMergeManager` with a dummy token —
   no network calls are issued, only the ticker method is run.
3. Wires the tracker into the manager.
4. Seeds ``self._waiting_prs`` directly with three staggered
   monotonic deadlines, simulating PRs that are blocked on
   pending required checks.
5. Spawns ``_wait_status_ticker()`` as a background task.
6. Removes entries one by one as their deadlines pass, simulating
   PRs whose checks complete.
7. Cancels the ticker once the last entry is gone and stops the
   tracker.

Usage:

.. code-block:: bash

    uv run python scripts/demo_wait_ticker.py            # Rich
    uv run python scripts/demo_wait_ticker.py --plain    # Plain

In ``--plain`` mode the tracker's ``rich_available`` attribute is
set to ``False`` *before* :meth:`MergeProgressTracker.start` so the
ticker delegates to :meth:`AsyncMergeManager._wait_status_ticker_plain`
(15-second cadence) and Rich's ``Live`` display never starts. This
keeps the plain-console output free of interleaved Rich frames.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make the demo runnable directly (``python scripts/demo_wait_ticker.py``)
# in addition to ``uv run python scripts/demo_wait_ticker.py`` by ensuring
# the package source directory is importable when not installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dependamerge.merge_manager import AsyncMergeManager  # noqa: E402
from dependamerge.progress_tracker import MergeProgressTracker  # noqa: E402

# Three staggered deadlines (seconds from "now") — mirrors the
# example in docs/TESTING_WAIT_STATUS_TICKER.md.
_DEADLINES_FROM_NOW: tuple[tuple[str, float], ...] = (
    ("demo-org/demo-repo#101", 20.0),
    ("demo-org/demo-repo#102", 30.0),
    ("demo-org/demo-repo#103", 40.0),
)


async def _drive_demo(manager: AsyncMergeManager) -> None:
    """Seed ``_waiting_prs`` and drive the ticker through its cycle."""
    loop = asyncio.get_running_loop()
    base = loop.time()

    # Seed three waiting PRs with staggered deadlines. We hold the
    # waiting lock so the ticker can never observe a half-populated
    # snapshot (defensive — the ticker is robust to either case).
    async with manager._waiting_lock:
        for pr_key, offset in _DEADLINES_FROM_NOW:
            manager._waiting_prs[pr_key] = base + offset

    print(f"[demo] seeded {len(_DEADLINES_FROM_NOW)} waiting PRs; starting ticker")

    ticker_task = asyncio.create_task(
        manager._wait_status_ticker(),
        name="demo-wait-ticker",
    )

    try:
        # Walk the deadlines in order, removing each entry shortly
        # after it expires. The ticker's countdown reflects the
        # *latest* (worst-case) deadline, so as we drop entries the
        # remaining seconds stay anchored to the entries that are
        # still present.
        for pr_key, offset in _DEADLINES_FROM_NOW:
            removal_at = base + offset + 0.5
            sleep_for = max(0.0, removal_at - loop.time())
            await asyncio.sleep(sleep_for)
            async with manager._waiting_lock:
                manager._waiting_prs.pop(pr_key, None)
            remaining = len(manager._waiting_prs)
            print(
                f"[demo] simulated completion of {pr_key} ({remaining} still waiting)"
            )

        # Give the ticker a beat to render the empty-state clear
        # before we cancel it, so the user actually sees the line
        # disappear.
        await asyncio.sleep(1.5)
    finally:
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            # Expected: we just cancelled the ticker, so awaiting it
            # re-raises the cancellation. Swallow it to allow a clean
            # teardown of the demo.
            pass


async def _amain(plain: bool) -> int:
    """Entry point coroutine."""
    print(
        "[demo] starting wait-ticker demo "
        f"({'plain console' if plain else 'rich live'} mode)"
    )

    tracker = MergeProgressTracker(
        organization="demo-org",
        operation_label="Demo",
        operation_icon="🔬",
    )
    # Provide some PR-level totals so the heading reads
    # "Demo in demo-org (0/3 PRs, 0%)" — same shape as production.
    tracker.total_prs = len(_DEADLINES_FROM_NOW)
    tracker.completed_prs = 0

    if plain:
        # Force the ticker into its plain-console fallback path.
        # ``_wait_status_ticker`` reads ``rich_available`` once at
        # startup, so we flip it *before* ``tracker.start()`` —
        # otherwise Rich's ``Live`` display would also begin
        # rendering, interleaving with the plain-console lines and
        # making the demo output misleading.
        tracker.rich_available = False

    tracker.start()

    manager = AsyncMergeManager(
        token="demo-token-not-used-no-api-calls",
        progress_tracker=tracker,
    )

    try:
        await _drive_demo(manager)
    finally:
        try:
            tracker.stop()
        except Exception:
            # Best-effort teardown: the demo is exiting regardless, so a
            # failure to stop the progress tracker must not mask the
            # outcome or raise from the ``finally`` block.
            pass

    print("[demo] finished")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Demo the AsyncMergeManager wait-status ticker without "
            "making any GitHub API calls."
        ),
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help=(
            "Force the plain-console fallback ticker (15s cadence) "
            "instead of the Rich Live single-line update."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Synchronous entry point."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return asyncio.run(_amain(plain=args.plain))
    except KeyboardInterrupt:
        print("\n[demo] interrupted by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
