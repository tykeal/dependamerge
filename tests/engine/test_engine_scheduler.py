# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the engine scheduler: lanes, slots, parking, budgets."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from dependamerge.engine.model import (
    Advance,
    Finish,
    ItemState,
    Park,
    Snapshot,
    Transition,
    WorkItem,
)
from dependamerge.engine.scheduler import (
    MAX_PHASE_EXECUTIONS,
    Engine,
    flat_lanes,
)

FAST = {
    "concurrency": 4,
    "default_park_timeout": 0.3,
    "reconcile_interval": 0.02,
}


def make_items(specs: list[tuple[str, str]]) -> list[WorkItem]:
    """Build items from (lane, key) pairs, phase preset to 'start'."""
    return [
        WorkItem(index=i, lane=lane, key=key, payload=None, phase="start")
        for i, (lane, key) in enumerate(specs)
    ]


class ScriptedRunner:
    """PhaseRunner driven by a dict of phase → behaviour callables.

    Records every (key, phase) execution and tracks live concurrency.
    """

    def __init__(self, script: dict[str, Any], *, hold: float = 0.0) -> None:
        self.script = script
        self.hold = hold
        self.calls: list[tuple[str, str]] = []
        self.active = 0
        self.max_active = 0
        self.active_by_lane: dict[str, int] = {}
        self.max_active_by_lane: dict[str, int] = {}

    async def run(self, item: WorkItem, phase: str) -> Transition:
        self.calls.append((item.key, phase))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        lane_active = self.active_by_lane.get(item.lane, 0) + 1
        self.active_by_lane[item.lane] = lane_active
        self.max_active_by_lane[item.lane] = max(
            self.max_active_by_lane.get(item.lane, 0), lane_active
        )
        try:
            if self.hold:
                await asyncio.sleep(self.hold)
            behaviour = self.script[phase]
            result = behaviour(item) if callable(behaviour) else behaviour
            if isinstance(result, Exception):
                raise result
            transition: Transition = result
            return transition
        finally:
            self.active -= 1
            self.active_by_lane[item.lane] -= 1

    def on_error(self, item: WorkItem, exc: Exception) -> Any:
        return f"error:{exc}"


async def no_snapshots(item: WorkItem) -> Snapshot | None:
    return None


class TestBasicExecution:
    async def test_single_item_finishes(self):
        runner = ScriptedRunner({"start": Finish("ok")})
        engine = Engine(runner, no_snapshots, **FAST)
        items = make_items([("r1", "r1#1")])
        done = await engine.run(items)
        assert done[0].outcome == "ok"
        assert done[0].state is ItemState.DONE

    async def test_advance_chains_phases(self):
        runner = ScriptedRunner({"start": Advance("second"), "second": Finish("done")})
        engine = Engine(runner, no_snapshots, **FAST)
        items = make_items([("r1", "r1#1")])
        done = await engine.run(items)
        assert done[0].outcome == "done"
        assert runner.calls == [("r1#1", "start"), ("r1#1", "second")]

    async def test_results_in_input_order(self):
        # Items in different lanes finish out of order (varying hold
        # times) but come back positionally.
        async def _run(item: WorkItem, phase: str) -> Transition:
            await asyncio.sleep(0.05 if item.index == 0 else 0.0)
            return Finish(f"out-{item.index}")

        class Runner:
            run = staticmethod(_run)

            def on_error(self, item, exc):
                return "err"

        engine = Engine(Runner(), no_snapshots, **FAST)
        items = make_items([("r1", "r1#1"), ("r2", "r2#1"), ("r3", "r3#1")])
        done = await engine.run(items)
        assert [i.outcome for i in done] == ["out-0", "out-1", "out-2"]

    async def test_empty_batch(self):
        runner = ScriptedRunner({})
        engine = Engine(runner, no_snapshots, **FAST)
        assert await engine.run([]) == []

    async def test_duplicate_keys_rejected(self):
        # Parked tracking and progress views index items by key, so
        # duplicates would silently shadow each other.
        runner = ScriptedRunner({"start": Finish("ok")})
        engine = Engine(runner, no_snapshots, **FAST)
        items = make_items([("r1", "r1#1"), ("r1", "r1#1")])
        with pytest.raises(ValueError, match="duplicate work item key"):
            await engine.run(items)


class TestLaneSerialisation:
    async def test_one_in_flight_per_lane_fifo(self):
        runner = ScriptedRunner({"start": Finish("ok")}, hold=0.02)
        engine = Engine(runner, no_snapshots, **FAST)
        items = make_items(
            [("r1", "r1#1"), ("r1", "r1#2"), ("r1", "r1#3"), ("r2", "r2#1")]
        )
        await engine.run(items)
        assert runner.max_active_by_lane["r1"] == 1
        r1_calls = [key for key, _ in runner.calls if key.startswith("r1#")]
        assert r1_calls == ["r1#1", "r1#2", "r1#3"]

    async def test_distinct_lanes_overlap(self):
        runner = ScriptedRunner({"start": Finish("ok")}, hold=0.05)
        engine = Engine(runner, no_snapshots, **FAST)
        items = make_items([("r1", "r1#1"), ("r2", "r2#1"), ("r3", "r3#1")])
        await engine.run(items)
        assert runner.max_active >= 2

    async def test_global_concurrency_cap(self):
        runner = ScriptedRunner({"start": Finish("ok")}, hold=0.05)
        engine = Engine(
            runner,
            no_snapshots,
            concurrency=2,
            default_park_timeout=0.3,
            reconcile_interval=0.02,
        )
        items = make_items([(f"r{i}", f"r{i}#1") for i in range(6)])
        await engine.run(items)
        assert runner.max_active <= 2

    async def test_item_waiting_for_slot_reports_queued(self):
        # concurrency=1: while the first item holds the slot, an item
        # blocked on slot acquisition must read QUEUED, not ACTIVE.
        observed: list[ItemState] = []

        items = make_items([("r1", "r1#1"), ("r2", "r2#1")])

        async def _run(item: WorkItem, phase: str) -> Transition:
            if item.key == "r1#1":
                await asyncio.sleep(0.05)
                observed.append(items[1].state)
            return Finish("ok")

        class Runner:
            run = staticmethod(_run)

            def on_error(self, item, exc):
                return "err"

        engine = Engine(
            Runner(),
            no_snapshots,
            concurrency=1,
            default_park_timeout=0.3,
            reconcile_interval=0.02,
        )
        done = await engine.run(items)
        assert observed == [ItemState.QUEUED]
        assert [i.outcome for i in done] == ["ok", "ok"]

    async def test_lane_continues_after_item_exception(self):
        def boom(item: WorkItem) -> Transition:
            raise RuntimeError("phase crashed")

        runner = ScriptedRunner(
            {"start": lambda item: boom(item) if item.index == 0 else Finish("ok")}
        )
        engine = Engine(runner, no_snapshots, **FAST)
        items = make_items([("r1", "r1#1"), ("r1", "r1#2")])
        done = await engine.run(items)
        assert done[0].outcome == "error:phase crashed"
        assert done[0].state is ItemState.DONE
        assert done[1].outcome == "ok"

    async def test_flat_lanes_helper_gives_unique_lanes(self):
        items = make_items([("r1", "r1#1"), ("r1", "r1#2")])
        flat_lanes(items)
        assert items[0].lane != items[1].lane


class TestParking:
    async def test_park_wakes_on_predicate(self):
        # Phase "start" parks until the snapshot says clean; the
        # snapshot source flips state after two ticks.
        ticks = {"n": 0}

        async def snapshots(item: WorkItem) -> Snapshot | None:
            ticks["n"] += 1
            state = "clean" if ticks["n"] >= 2 else "blocked"
            return Snapshot(mergeable_state=state)

        runner = ScriptedRunner(
            {
                "start": Park(
                    reason="waiting for checks",
                    wake=lambda item: (
                        item.snapshot is not None
                        and item.snapshot.mergeable_state == "clean"
                    ),
                    on_wake="merge",
                    on_timeout="timed-out",
                ),
                "merge": Finish("merged"),
                "timed-out": Finish("pending"),
            }
        )
        engine = Engine(runner, snapshots, **FAST)
        items = make_items([("r1", "r1#1")])
        done = await engine.run(items)
        assert done[0].outcome == "merged"
        assert ("r1#1", "merge") in runner.calls

    async def test_park_times_out_to_on_timeout_phase(self):
        runner = ScriptedRunner(
            {
                "start": Park(
                    reason="never wakes",
                    wake=lambda item: False,
                    on_wake="merge",
                    on_timeout="timed-out",
                    timeout=0.05,
                ),
                "merge": Finish("merged"),
                "timed-out": Finish("pending"),
            }
        )
        engine = Engine(runner, no_snapshots, **FAST)
        items = make_items([("r1", "r1#1")])
        done = await engine.run(items)
        assert done[0].outcome == "pending"
        assert ("r1#1", "merge") not in runner.calls

    async def test_parked_items_do_not_hold_slots(self):
        # concurrency=1: item A parks (indefinitely within the test
        # window); item B in another lane must still run to completion
        # while A is parked.  This is the core capacity fix.
        order: list[str] = []

        def start(item: WorkItem) -> Transition:
            if item.key == "r1#1":
                return Park(
                    reason="slow rebase",
                    wake=lambda it: False,
                    on_wake="merge",
                    on_timeout="timed-out",
                    timeout=0.15,
                )
            order.append("b-ran")
            return Finish("b-done")

        runner = ScriptedRunner(
            {
                "start": start,
                "timed-out": lambda item: Finish("a-pending"),
            }
        )
        engine = Engine(
            runner,
            no_snapshots,
            concurrency=1,
            default_park_timeout=0.3,
            reconcile_interval=0.02,
        )
        items = make_items([("r1", "r1#1"), ("r2", "r2#1")])
        done = await engine.run(items)
        assert done[1].outcome == "b-done"
        assert done[0].outcome == "a-pending"
        assert order == ["b-ran"]

    async def test_parked_item_blocks_same_lane_successor(self):
        # Per-repo serialisation survives parking: the next item in
        # the same lane must not start while its predecessor is parked.
        started: list[str] = []

        def start(item: WorkItem) -> Transition:
            started.append(item.key)
            if item.key == "r1#1":
                return Park(
                    reason="rebase",
                    wake=lambda it: False,
                    on_wake="merge",
                    on_timeout="timed-out",
                    timeout=0.1,
                )
            return Finish("second")

        runner = ScriptedRunner(
            {"start": start, "timed-out": lambda item: Finish("first")}
        )
        engine = Engine(runner, no_snapshots, **FAST)
        items = make_items([("r1", "r1#1"), ("r1", "r1#2")])
        await engine.run(items)
        # r1#2 starts only after r1#1 finished (park + timeout phase).
        assert started == ["r1#1", "r1#2"]
        first_done = runner.calls.index(("r1#1", "timed-out"))
        second_start = runner.calls.index(("r1#2", "start"))
        assert second_start > first_done

    async def test_parked_view_exposes_reason(self):
        seen: dict[str, tuple[str, float]] = {}

        engine_ref: list[Engine] = []

        def start(item: WorkItem) -> Transition:
            return Park(
                reason="waiting for dependabot rebase",
                wake=lambda it: bool(seen.update(engine_ref[0].parked_view())) or False,
                on_wake="merge",
                on_timeout="timed-out",
                timeout=0.1,
            )

        runner = ScriptedRunner(
            {"start": start, "timed-out": lambda item: Finish("pending")}
        )
        engine = Engine(runner, no_snapshots, **FAST)
        engine_ref.append(engine)
        await engine.run(make_items([("r1", "r1#1")]))
        assert seen["r1#1"][0] == "waiting for dependabot rebase"


class TestBudgets:
    async def test_no_wait_mode_skips_parks_entirely(self):
        # max_wait=0: the parking phase's side effects happen, then
        # on_timeout runs immediately — nothing sleeps.
        side_effects: list[str] = []

        def start(item: WorkItem) -> Transition:
            side_effects.append("rebase-requested")
            return Park(
                reason="rebase",
                wake=lambda it: True,  # would wake instantly if waited
                on_wake="merge",
                on_timeout="fire-and-forget",
            )

        runner = ScriptedRunner(
            {
                "start": start,
                "fire-and-forget": lambda item: Finish("auto-merge armed"),
                "merge": lambda item: Finish("merged"),
            }
        )
        engine = Engine(
            runner,
            no_snapshots,
            concurrency=2,
            default_park_timeout=60.0,
            reconcile_interval=0.02,
            max_wait=0,
        )
        done = await engine.run(make_items([("r1", "r1#1")]))
        assert side_effects == ["rebase-requested"]
        assert done[0].outcome == "auto-merge armed"

    async def test_run_deadline_clamps_park_timeouts(self):
        # A park asking for 60s is clamped to the 0.1s run deadline.
        runner = ScriptedRunner(
            {
                "start": Park(
                    reason="long wait",
                    wake=lambda item: False,
                    on_wake="merge",
                    on_timeout="timed-out",
                    timeout=60.0,
                ),
                "timed-out": lambda item: Finish("deadline"),
            }
        )
        engine = Engine(
            runner,
            no_snapshots,
            concurrency=2,
            default_park_timeout=60.0,
            reconcile_interval=0.02,
            max_wait=0.1,
        )
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        done = await engine.run(make_items([("r1", "r1#1")]))
        assert done[0].outcome == "deadline"
        assert loop.time() - t0 < 5.0

    async def test_park_after_run_deadline_resolves_immediately(self):
        # Second park starts after the run deadline already passed: it
        # must not wait at all.
        runner = ScriptedRunner(
            {
                "start": Park(
                    reason="first",
                    wake=lambda item: False,
                    on_wake="merge",
                    on_timeout="park-again",
                    timeout=60.0,
                ),
                "park-again": Park(
                    reason="second",
                    wake=lambda item: False,
                    on_wake="merge",
                    on_timeout="timed-out",
                    timeout=60.0,
                ),
                "timed-out": lambda item: Finish("done"),
            }
        )
        engine = Engine(
            runner,
            no_snapshots,
            concurrency=2,
            default_park_timeout=60.0,
            reconcile_interval=0.02,
            max_wait=0.05,
        )
        done = await engine.run(make_items([("r1", "r1#1")]))
        assert done[0].outcome == "done"

    async def test_phase_budget_converts_cycles_to_failures(self):
        runner = ScriptedRunner({"start": Advance("start")})
        engine = Engine(runner, no_snapshots, **FAST)
        done = await engine.run(make_items([("r1", "r1#1")]))
        assert "phase budget exhausted" in str(done[0].outcome)
        assert len(runner.calls) == MAX_PHASE_EXECUTIONS


class TestObservers:
    async def test_on_item_done_fires_per_item(self):
        completed: list[str] = []
        runner = ScriptedRunner({"start": Finish("ok")})
        engine = Engine(
            runner,
            no_snapshots,
            on_item_done=lambda item: completed.append(item.key),
            **FAST,
        )
        await engine.run(make_items([("r1", "r1#1"), ("r2", "r2#1")]))
        assert sorted(completed) == ["r1#1", "r2#1"]

    async def test_observer_exception_does_not_kill_lane(self):
        def observer(item: WorkItem) -> None:
            raise RuntimeError("observer bug")

        runner = ScriptedRunner({"start": Finish("ok")})
        engine = Engine(runner, no_snapshots, on_item_done=observer, **FAST)
        done = await engine.run(make_items([("r1", "r1#1"), ("r1", "r1#2")]))
        assert [i.outcome for i in done] == ["ok", "ok"]
