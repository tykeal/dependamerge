# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Engine: lane-serialised, slot-bounded, park-aware scheduler.

Scheduling model
----------------

- **Lanes** — items sharing a lane key (one lane per repository) run
  strictly sequentially, first-in-first-out.  This preserves the
  invariant that at most one PR per repository is in flight, which
  keeps same-repo merges from racing GitHub's mergeability
  propagation and prevents dependabot rebase storms.
- **Slots** — a global semaphore bounds how many items execute an
  *active* phase concurrently.  Unlike the legacy orchestration, a
  slot is held only while a phase is actually running: a parked item
  (waiting on a rebase, CI, or auto-merge) holds no slot, so waiting
  never starves runnable work.
- **Parking** — a phase that starts a slow external operation returns
  :class:`~dependamerge.engine.model.Park`; the engine releases the
  slot and hands the item to the :class:`Reconciler`, which wakes it
  when its predicate fires or its deadline passes.

Budget model
------------

- ``default_park_timeout`` bounds each individual park (the legacy
  ``--merge-timeout`` role) — but since parked items are free, this is
  a responsiveness bound, not a capacity trade-off.
- ``max_wait`` bounds the whole run: every park deadline is clamped to
  it, and once it passes new parks resolve immediately as timeouts.
  Unlike the legacy code (where only ``_wait_for_auto_merge`` honoured
  the run deadline) the clamp here applies to *every* wait uniformly.
- ``max_wait == 0`` is fire-and-forget: parks never wait — the parking
  phase's side effects (rebase requested, auto-merge armed) still
  happen, then ``on_timeout`` runs immediately to produce the
  fire-and-forget outcome.

Failure model
-------------

A phase that raises does not crash the run: the exception is converted
to a terminal outcome via ``PhaseRunner.on_error`` and the item's lane
continues with its next item.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol

from .model import Advance, Finish, ItemState, Park, Transition, WorkItem
from .reconciler import Reconciler, SnapshotSource


class PhaseRunner(Protocol):
    """The pipeline the engine drives.

    ``run`` executes one named phase for an item and returns the next
    transition.  ``on_error`` converts an unexpected phase exception
    into a terminal outcome (never raises).
    """

    async def run(self, item: WorkItem, phase: str) -> Transition: ...

    def on_error(self, item: WorkItem, exc: Exception) -> Any: ...


# Safety valve: an item may not execute more than this many phases.
# Well-formed pipelines terminate long before this; the cap converts a
# transition cycle bug into a per-item failure instead of a hang.
MAX_PHASE_EXECUTIONS = 100


class Engine:
    """Run a batch of work items through a :class:`PhaseRunner`.

    ``reconcile_interval`` sets the reconciler's tick cadence and is
    clamped to :data:`~dependamerge.engine.reconciler.MIN_INTERVAL`
    (see :class:`Reconciler`).
    """

    def __init__(
        self,
        runner: PhaseRunner,
        snapshot_source: SnapshotSource,
        *,
        concurrency: int,
        default_park_timeout: float,
        reconcile_interval: float,
        max_wait: float | None = None,
        log: logging.Logger | None = None,
        on_item_done: Callable[[WorkItem], None] | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._runner = runner
        self._slots = asyncio.Semaphore(concurrency)
        self._default_park_timeout = default_park_timeout
        self._max_wait = max_wait
        self._no_wait = max_wait is not None and max_wait <= 0
        self._run_deadline: float | None = None
        self._log = log or logging.getLogger(__name__)
        self._on_item_done = on_item_done
        self._reconciler = Reconciler(
            snapshot_source,
            interval=reconcile_interval,
            log=self._log,
        )

    # -- observability -----------------------------------------------------

    @property
    def reconciler(self) -> Reconciler:
        return self._reconciler

    def parked_view(self) -> dict[str, tuple[str, float]]:
        """Parked items: key → (reason, deadline).  For progress UIs."""
        return self._reconciler.parked_view()

    # -- run ---------------------------------------------------------------

    async def run(self, items: list[WorkItem]) -> list[WorkItem]:
        """Process ``items``; returns them in input (index) order.

        Every item is guaranteed to come back with ``state == DONE``
        and an ``outcome`` set (phase exceptions are converted by
        ``PhaseRunner.on_error``).

        Raises ``ValueError`` when two items share a ``key``: the
        reconciler and progress views index parked items by key, so
        duplicates would silently shadow each other.
        """
        if not items:
            return []

        seen_keys: set[str] = set()
        for item in items:
            if item.key in seen_keys:
                raise ValueError(f"duplicate work item key: {item.key!r}")
            seen_keys.add(item.key)

        loop = asyncio.get_running_loop()
        self._run_deadline = None
        if self._max_wait is not None and self._max_wait > 0:
            self._run_deadline = loop.time() + self._max_wait

        lanes: dict[str, list[WorkItem]] = {}
        for item in items:
            lanes.setdefault(item.lane, []).append(item)

        reconciler_task = asyncio.create_task(
            self._reconciler.run(), name="engine-reconciler"
        )
        lane_tasks = [
            asyncio.create_task(
                self._lane_worker(lane_items),
                name=f"engine-lane-{lane}",
            )
            for lane, lane_items in lanes.items()
        ]
        try:
            await asyncio.gather(*lane_tasks)
        finally:
            self._reconciler.flush()
            reconciler_task.cancel()
            try:
                await reconciler_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                self._log.warning("engine reconciler exited unexpectedly: %s", exc)

        return sorted(items, key=lambda item: item.index)

    # -- internals ---------------------------------------------------------

    async def _lane_worker(self, lane_items: list[WorkItem]) -> None:
        """Drive one lane's items strictly sequentially."""
        for item in lane_items:
            try:
                await self._drive(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - _drive is total
                # _drive already routes phase exceptions through
                # on_error; anything surfacing here is an engine bug.
                # Fail the item, keep the lane alive.
                self._log.error("engine: internal error driving %s: %s", item.key, exc)
                item.outcome = self._runner.on_error(item, exc)
                item.state = ItemState.DONE
            if self._on_item_done is not None:
                try:
                    self._on_item_done(item)
                except Exception as exc:  # observer bugs must not kill lanes
                    self._log.warning(
                        "engine: on_item_done observer failed for %s: %s",
                        item.key,
                        exc,
                    )

    async def _drive(self, item: WorkItem) -> None:
        """Run one item to a terminal outcome."""
        loop = asyncio.get_running_loop()
        executions = 0
        while True:
            executions += 1
            if executions > MAX_PHASE_EXECUTIONS:
                item.outcome = self._runner.on_error(
                    item,
                    RuntimeError(
                        f"phase budget exhausted after "
                        f"{MAX_PHASE_EXECUTIONS} transitions "
                        f"(last phase: {item.phase!r})"
                    ),
                )
                item.state = ItemState.DONE
                return

            # Active phase: slot held only for the duration of the
            # call.  The item stays QUEUED while it waits for a slot
            # so observers can tell "waiting for capacity" apart from
            # "executing".
            item.state = ItemState.QUEUED
            async with self._slots:
                item.state = ItemState.ACTIVE
                try:
                    transition: Transition = await self._runner.run(item, item.phase)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    item.outcome = self._runner.on_error(item, exc)
                    item.state = ItemState.DONE
                    return
            item.history.append((item.phase, repr(transition)))

            if isinstance(transition, Finish):
                item.outcome = transition.outcome
                item.state = ItemState.DONE
                return

            if isinstance(transition, Advance):
                item.phase = transition.phase
                continue

            if isinstance(transition, Park):
                # No-wait mode: the parking phase's side effects have
                # already happened; resolve the wait instantly.
                if self._no_wait:
                    item.phase = transition.on_timeout
                    continue
                timeout = (
                    transition.timeout
                    if transition.timeout is not None
                    else self._default_park_timeout
                )
                deadline = loop.time() + max(0.0, timeout)
                if self._run_deadline is not None:
                    deadline = min(deadline, self._run_deadline)
                item.state = ItemState.PARKED
                woke = await self._reconciler.park(item, transition, deadline)
                item.phase = transition.on_wake if woke else transition.on_timeout
                continue

            # Unknown transition type — treat as a pipeline bug.
            item.outcome = self._runner.on_error(
                item,
                RuntimeError(f"unknown transition {transition!r}"),
            )
            item.state = ItemState.DONE
            return


def flat_lanes(items: list[WorkItem]) -> None:
    """Give every item its own lane (legacy flat-scheduler semantics).

    Mutates ``item.lane`` in place.  Use when the batch is known to
    contain at most one PR per repository, or when per-repo
    serialisation is handled elsewhere.
    """
    for item in items:
        item.lane = f"{item.lane}#{item.index}"


__all__ = [
    "Engine",
    "PhaseRunner",
    "flat_lanes",
    "MAX_PHASE_EXECUTIONS",
]
