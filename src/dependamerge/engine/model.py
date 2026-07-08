# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Core data model for the merge orchestration engine.

The engine executes a batch of *work items* (one per pull request)
through a pipeline of named *phases*.  Each phase performs bounded
active work (API calls) and returns a :class:`Transition` telling the
engine what to do next:

- :class:`Advance` — run another phase immediately.
- :class:`Park` — the item is waiting on an external event (a
  dependabot rebase, CI checks, auto-merge).  The engine releases the
  item's concurrency slot while it waits; the reconciler wakes it when
  its wake predicate fires or its deadline passes.
- :class:`Finish` — the item reached a terminal outcome.

The engine is deliberately generic: it does not import GitHub types or
``MergeResult``.  Payloads and outcomes are opaque to it, which keeps
the scheduling kernel free of import cycles and independently testable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ItemState(Enum):
    """Lifecycle state of a work item inside the engine."""

    QUEUED = "queued"
    ACTIVE = "active"
    PARKED = "parked"
    DONE = "done"


@dataclass
class Snapshot:
    """A point-in-time view of a pull request's merge-relevant state.

    Produced by the reconciler's snapshot source once per tick for
    every parked item, and consumed by :class:`Park` wake predicates.
    All fields are optional so a failed or partial refresh degrades
    gracefully — predicates must treat ``None`` as "unknown, keep
    waiting".
    """

    state: str | None = None  # "open" / "closed"
    merged: bool | None = None
    mergeable: bool | None = None
    mergeable_state: str | None = None  # clean/dirty/blocked/behind/...
    head_sha: str | None = None


@dataclass
class WorkItem:
    """One unit of work (a single pull request) tracked by the engine.

    ``index`` preserves the caller's input ordering so results can be
    reassembled positionally even when items complete out of order.
    ``lane`` groups items that must run strictly sequentially (one
    in-flight PR per repository).  ``payload`` carries the caller's PR
    object; ``outcome`` carries the terminal result.  Both are opaque
    to the engine.
    """

    index: int
    lane: str
    key: str  # stable identity, e.g. "owner/repo#123"
    payload: Any
    phase: str = ""
    state: ItemState = ItemState.QUEUED
    outcome: Any = None
    snapshot: Snapshot | None = None
    # Names of recovery actions already attempted for this item, used
    # by the recovery ladder to guarantee forward progress (each rung
    # fires at most once per item per run).
    attempts: set[str] = field(default_factory=set)
    # Diagnostic trail of (phase, transition-repr) tuples.
    history: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Advance:
    """Immediately run ``phase`` next (the item stays active)."""

    phase: str

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Advance({self.phase!r})"


@dataclass
class Park:
    """Release the slot and wait for an external event.

    ``wake`` is evaluated by the reconciler against a fresh
    :class:`Snapshot` each tick; when it returns True the item is
    rescheduled on ``on_wake``.  When ``timeout`` elapses first (or
    the run-wide deadline passes, or the engine runs in no-wait mode)
    the item is rescheduled on ``on_timeout`` instead.  ``on_timeout``
    phases should be cheap: in no-wait mode they run immediately after
    the parking phase, and at the run deadline many of them may run
    back-to-back.
    """

    reason: str
    wake: Callable[[WorkItem], bool]
    on_wake: str
    on_timeout: str
    timeout: float | None = None  # None → engine default (merge timeout)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Park({self.reason!r}, on_wake={self.on_wake!r}, "
            f"on_timeout={self.on_timeout!r})"
        )


@dataclass
class Finish:
    """The item reached a terminal outcome."""

    outcome: Any

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "Finish(...)"


Transition = Advance | Park | Finish
