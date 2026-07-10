# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Merge orchestration engine.

A lane-serialised, slot-bounded, park-aware scheduler for bulk PR
merging, plus the central recovery ladder.  See
``docs/MERGE_ENGINE_DESIGN.md`` for the architecture and migration
plan.
"""

from .ladder import Action, ActionKind, LadderInput, decide
from .model import (
    Advance,
    Finish,
    ItemState,
    Park,
    Snapshot,
    Transition,
    WorkItem,
)
from .reconciler import Reconciler, SnapshotSource
from .scheduler import Engine, PhaseRunner, flat_lanes

__all__ = [
    "Action",
    "ActionKind",
    "Advance",
    "Engine",
    "Finish",
    "ItemState",
    "LadderInput",
    "Park",
    "PhaseRunner",
    "Reconciler",
    "Snapshot",
    "SnapshotSource",
    "Transition",
    "WorkItem",
    "decide",
    "flat_lanes",
]
