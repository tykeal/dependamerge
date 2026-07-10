# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the reconciler: parking, waking, expiry, resilience."""

from __future__ import annotations

import asyncio

from dependamerge.engine.model import Park, Snapshot, WorkItem
from dependamerge.engine.reconciler import Reconciler


def make_item(key: str = "o/r#1") -> WorkItem:
    return WorkItem(index=0, lane="o/r", key=key, payload=None, phase="p")


def make_park(wake, reason: str = "test") -> Park:
    return Park(reason=reason, wake=wake, on_wake="wake", on_timeout="timeout")


class TestWake:
    async def test_wakes_when_predicate_fires(self):
        flips = {"clean": False}

        async def snapshots(item: WorkItem) -> Snapshot | None:
            return Snapshot(mergeable_state="clean" if flips["clean"] else "blocked")

        reconciler = Reconciler(snapshots, interval=0.02)
        task = asyncio.create_task(reconciler.run())
        try:
            item = make_item()
            park = make_park(
                lambda it: (
                    it.snapshot is not None and it.snapshot.mergeable_state == "clean"
                )
            )
            loop = asyncio.get_running_loop()

            async def flip_soon():
                await asyncio.sleep(0.05)
                flips["clean"] = True

            flip = asyncio.create_task(flip_soon())
            woke = await reconciler.park(item, park, loop.time() + 5.0)
            await flip
            assert woke is True
            assert item.snapshot is not None
            assert item.snapshot.mergeable_state == "clean"
            assert reconciler.parked_view() == {}
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_times_out_at_deadline(self):
        async def snapshots(item: WorkItem) -> Snapshot | None:
            return Snapshot(mergeable_state="blocked")

        reconciler = Reconciler(snapshots, interval=0.02)
        task = asyncio.create_task(reconciler.run())
        try:
            loop = asyncio.get_running_loop()
            woke = await reconciler.park(
                make_item(), make_park(lambda it: False), loop.time() + 0.08
            )
            assert woke is False
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_expired_deadline_resolves_without_waiting(self):
        async def snapshots(item: WorkItem) -> Snapshot | None:
            return None

        reconciler = Reconciler(snapshots, interval=60.0)
        loop = asyncio.get_running_loop()
        # No reconciler task running at all: an already-expired
        # deadline must resolve synchronously.
        woke = await reconciler.park(
            make_item(), make_park(lambda it: True), loop.time() - 1.0
        )
        assert woke is False


class TestResilience:
    async def test_failed_snapshot_keeps_waiting_and_previous_snapshot(self):
        calls = {"n": 0}

        async def snapshots(item: WorkItem) -> Snapshot | None:
            calls["n"] += 1
            if calls["n"] == 1:
                return Snapshot(mergeable_state="blocked")
            if calls["n"] == 2:
                raise RuntimeError("transient API failure")
            return Snapshot(mergeable_state="clean")

        reconciler = Reconciler(snapshots, interval=0.02)
        task = asyncio.create_task(reconciler.run())
        try:
            item = make_item()
            loop = asyncio.get_running_loop()
            woke = await reconciler.park(
                item,
                make_park(
                    lambda it: (
                        it.snapshot is not None
                        and it.snapshot.mergeable_state == "clean"
                    )
                ),
                loop.time() + 5.0,
            )
            assert woke is True
            assert calls["n"] >= 3
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_predicate_exception_does_not_kill_loop(self):
        async def snapshots(item: WorkItem) -> Snapshot | None:
            return Snapshot(mergeable_state="blocked")

        boom_item = make_item("o/r#1")
        ok_item = make_item("o/r#2")

        def bad_predicate(it: WorkItem) -> bool:
            raise RuntimeError("predicate bug")

        reconciler = Reconciler(snapshots, interval=0.02)
        task = asyncio.create_task(reconciler.run())
        try:
            loop = asyncio.get_running_loop()
            results = await asyncio.gather(
                reconciler.park(boom_item, make_park(bad_predicate), loop.time() + 0.1),
                reconciler.park(ok_item, make_park(lambda it: True), loop.time() + 5.0),
            )
            # The buggy predicate times out; the healthy one wakes.
            assert results == [False, True]
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_flush_times_out_all_parked(self):
        async def snapshots(item: WorkItem) -> Snapshot | None:
            return None

        reconciler = Reconciler(snapshots, interval=60.0)
        loop = asyncio.get_running_loop()
        parked = [
            asyncio.create_task(
                reconciler.park(
                    make_item(f"o/r#{i}"),
                    make_park(lambda it: False),
                    loop.time() + 60.0,
                )
            )
            for i in range(3)
        ]
        await asyncio.sleep(0.02)
        assert len(reconciler.parked_view()) == 3
        reconciler.flush()
        assert await asyncio.gather(*parked) == [False, False, False]
        assert reconciler.parked_view() == {}

    async def test_parked_view_reports_reason_and_deadline(self):
        async def snapshots(item: WorkItem) -> Snapshot | None:
            return None

        reconciler = Reconciler(snapshots, interval=60.0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60.0
        parked = asyncio.create_task(
            reconciler.park(
                make_item("o/r#7"),
                make_park(lambda it: False, reason="waiting for rebase"),
                deadline,
            )
        )
        await asyncio.sleep(0.02)
        view = reconciler.parked_view()
        assert view["o/r#7"] == ("waiting for rebase", deadline)
        reconciler.flush()
        await parked

    async def test_duplicate_park_times_out_previous_waiter(self):
        # The engine enforces key uniqueness, so this is a defensive
        # path: a second park with the same key must not orphan the
        # first waiter (which would suspend its task forever).
        async def snapshots(item: WorkItem) -> Snapshot | None:
            return None

        reconciler = Reconciler(snapshots, interval=60.0)
        loop = asyncio.get_running_loop()
        first = asyncio.create_task(
            reconciler.park(
                make_item("o/r#1"),
                make_park(lambda it: False, reason="first"),
                loop.time() + 60.0,
            )
        )
        await asyncio.sleep(0.02)
        second = asyncio.create_task(
            reconciler.park(
                make_item("o/r#1"),
                make_park(lambda it: False, reason="second"),
                loop.time() + 60.0,
            )
        )
        # The first waiter resolves as a timeout instead of hanging.
        assert await asyncio.wait_for(first, timeout=1.0) is False
        await asyncio.sleep(0.02)
        # The second park is the one now tracked.
        assert reconciler.parked_view()["o/r#1"][0] == "second"
        reconciler.flush()
        assert await second is False
        assert reconciler.parked_view() == {}
