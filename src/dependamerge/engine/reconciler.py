# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Reconciler: one batched poller for every parked work item.

Today each waiting PR runs its own polling loop while holding a global
concurrency slot (``_wait_for_auto_merge``, the conflict-recovery
phases, post-rebase polls, recreate waits, …), so a handful of slow
external operations — a five-minute ``@dependabot rebase``, a CI
re-run — can pin the entire run's capacity.

The reconciler inverts that: parked items hold **no** slot.  A single
coroutine ticks at a fixed cadence, refreshes one :class:`Snapshot`
per parked item per tick (batched through the shared HTTP client,
whose own semaphore/RPS limiter is the real API-protection layer),
evaluates each item's wake predicate, and resolves the item's parked
future with:

- ``True``  → the wake condition fired; the engine reschedules the
  item's ``on_wake`` phase.
- ``False`` → the item's deadline (or the run-wide deadline) passed;
  the engine reschedules ``on_timeout``.

API cost is unchanged from the status quo (one GET per waiting PR per
interval); what changes is that waiting consumes zero concurrency.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .model import Park, Snapshot, WorkItem

# A callable that fetches a fresh snapshot for a work item.  Returning
# ``None`` means "refresh failed, keep the previous snapshot" — the
# reconciler never wakes or times an item out based on a failed read
# alone.
SnapshotSource = Callable[[WorkItem], Awaitable[Snapshot | None]]

# Floor for the tick cadence: one snapshot request per parked item is
# issued per tick, so smaller intervals would hot-loop against the API.
MIN_INTERVAL = 0.05


@dataclass
class _Parked:
    item: WorkItem
    park: Park
    deadline: float  # monotonic; already clamped to the run deadline
    waiter: asyncio.Future[bool]


class Reconciler:
    """Watches parked work items and wakes them on state changes.

    Owned and driven by the engine: the engine calls :meth:`park` to
    suspend an item (awaiting the returned future *without* holding a
    slot) and runs :meth:`run` as a background task for the duration
    of the batch.

    ``interval`` is the tick cadence in seconds.  Values below
    :data:`MIN_INTERVAL` are clamped up to it: each tick issues one
    snapshot request per parked item, so an arbitrarily small
    interval would turn the poller into a hot loop against the API.
    """

    def __init__(
        self,
        snapshot_source: SnapshotSource,
        *,
        interval: float,
        log: logging.Logger | None = None,
    ) -> None:
        self._snapshot_source = snapshot_source
        self._interval = max(MIN_INTERVAL, interval)
        self._parked: dict[str, _Parked] = {}
        self._log = log or logging.getLogger(__name__)

    # -- engine-facing API -------------------------------------------------

    async def park(self, item: WorkItem, park: Park, deadline: float) -> bool:
        """Suspend ``item`` until its wake predicate fires or times out.

        Returns True when the wake condition fired, False on timeout.
        The caller must not hold a concurrency slot while awaiting.
        """
        loop = asyncio.get_running_loop()
        # An already-expired deadline resolves immediately as a
        # timeout; this is what makes no-wait/late parks cheap.
        if loop.time() >= deadline:
            return False
        # Defensive: the engine enforces key uniqueness, but if a
        # duplicate park slips through anyway, time the previous
        # waiter out rather than orphaning it (a silently overwritten
        # entry would leave its task suspended forever).
        stale = self._parked.pop(item.key, None)
        if stale is not None and not stale.waiter.done():
            self._log.warning(
                "reconciler: duplicate park for %s; timing out the " "previous waiter",
                item.key,
            )
            stale.waiter.set_result(False)
        waiter: asyncio.Future[bool] = loop.create_future()
        entry = _Parked(item=item, park=park, deadline=deadline, waiter=waiter)
        self._parked[item.key] = entry
        try:
            return await waiter
        finally:
            # Pop only our own entry: a duplicate park may have
            # replaced it while we were suspended.
            if self._parked.get(item.key) is entry:
                self._parked.pop(item.key, None)

    def parked_view(self) -> dict[str, tuple[str, float]]:
        """Snapshot of parked items: key → (reason, deadline).

        Consumed by progress/ticker UIs; the returned dict is a copy.
        """
        return {
            key: (entry.park.reason, entry.deadline)
            for key, entry in self._parked.items()
        }

    def flush(self) -> None:
        """Time out every parked item immediately (run teardown)."""
        for entry in list(self._parked.values()):
            if not entry.waiter.done():
                entry.waiter.set_result(False)

    # -- background loop ---------------------------------------------------

    async def run(self) -> None:
        """Tick until cancelled: refresh, evaluate, wake/expire."""
        while True:
            await asyncio.sleep(self._interval)
            await self._tick()

    async def _tick(self) -> None:
        entries = list(self._parked.values())
        if not entries:
            return
        # Refresh all parked snapshots concurrently; the shared HTTP
        # client's own concurrency/RPS limits pace the actual requests.
        snapshots = await asyncio.gather(
            *(self._refresh(entry) for entry in entries),
            return_exceptions=True,
        )
        loop = asyncio.get_running_loop()
        now = loop.time()
        for entry, snap in zip(entries, snapshots, strict=False):
            if entry.waiter.done():
                continue
            if isinstance(snap, BaseException):
                self._log.debug(
                    "reconciler: snapshot refresh failed for %s: %s",
                    entry.item.key,
                    snap,
                )
            elif snap is not None:
                entry.item.snapshot = snap
            woke = False
            try:
                woke = bool(entry.park.wake(entry.item))
            except Exception as exc:  # predicate bugs must not kill the loop
                self._log.warning(
                    "reconciler: wake predicate failed for %s: %s",
                    entry.item.key,
                    exc,
                )
            if woke:
                entry.waiter.set_result(True)
            elif now >= entry.deadline:
                entry.waiter.set_result(False)

    async def _refresh(self, entry: _Parked) -> Snapshot | None:
        return await self._snapshot_source(entry.item)
