# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for park-aware concurrency slots (Phase 2 engine wiring).

Covers the ``slot_lease`` primitives in isolation and the production
guarantee they exist for: a PR waiting on an external event releases
its concurrency slot so runnable PRs are never starved by parked ones
(see ``docs/MERGE_ENGINE_DESIGN.md``).
"""

from __future__ import annotations

import asyncio

import pytest

from dependamerge.slot_lease import (
    SlotLease,
    current_lease,
    holding_slot,
    parked,
)

# ---------------------------------------------------------------------------
# SlotLease primitive
# ---------------------------------------------------------------------------


class TestSlotLease:
    async def test_acquire_and_release(self):
        sem = asyncio.Semaphore(1)
        lease = SlotLease(sem)
        assert lease.held is False

        await lease.acquire()
        assert lease.held is True
        assert sem.locked()

        lease.release()
        assert lease.held is False
        assert not sem.locked()

    async def test_release_is_idempotent(self):
        """Double-release must not over-credit the semaphore."""
        sem = asyncio.Semaphore(1)
        lease = SlotLease(sem)
        await lease.acquire()
        lease.release()
        lease.release()  # no-op, not a second release

        await sem.acquire()  # take the single permit back
        assert sem.locked()  # exactly one permit existed

    async def test_acquire_is_idempotent(self):
        sem = asyncio.Semaphore(2)
        lease = SlotLease(sem)
        await lease.acquire()
        await lease.acquire()  # no-op, does not consume a second permit

        # The second permit is still available.
        await asyncio.wait_for(sem.acquire(), timeout=0.5)


# ---------------------------------------------------------------------------
# holding_slot / parked context managers
# ---------------------------------------------------------------------------


class TestHoldingSlot:
    async def test_holds_and_releases(self):
        sem = asyncio.Semaphore(1)
        async with holding_slot(sem) as lease:
            assert lease.held is True
            assert sem.locked()
            assert current_lease() is lease
        assert not sem.locked()
        assert current_lease() is None

    async def test_release_on_exception(self):
        sem = asyncio.Semaphore(1)
        with pytest.raises(RuntimeError):
            async with holding_slot(sem):
                raise RuntimeError("boom")
        assert not sem.locked()

    async def test_lease_is_task_local(self):
        """Concurrent tasks never see each other's lease."""
        sem = asyncio.Semaphore(2)
        seen: dict[str, object] = {}
        barrier_a = asyncio.Event()
        barrier_b = asyncio.Event()

        async def worker(name: str, wait: asyncio.Event, fire: asyncio.Event):
            async with holding_slot(sem):
                seen[name] = current_lease()
                fire.set()
                await asyncio.wait_for(wait.wait(), timeout=2.0)

        task_a = asyncio.create_task(worker("a", barrier_a, barrier_b))
        await asyncio.wait_for(barrier_b.wait(), timeout=2.0)
        task_b = asyncio.create_task(worker("b", barrier_b, barrier_a))
        await asyncio.gather(task_a, task_b)

        assert seen["a"] is not None
        assert seen["b"] is not None
        assert seen["a"] is not seen["b"]


class TestParked:
    async def test_releases_slot_during_wait(self):
        sem = asyncio.Semaphore(1)
        async with holding_slot(sem) as lease:
            assert sem.locked()
            async with parked():
                assert lease.held is False
                assert not sem.locked()
            # Re-acquired on exit.
            assert lease.held is True
            assert sem.locked()

    async def test_noop_without_lease(self):
        """parked() outside holding_slot() must be harmless."""
        assert current_lease() is None
        async with parked():
            pass  # no error, no lease involved

    async def test_nested_park_is_noop(self):
        """An inner parked() inside an already-parked block is a no-op."""
        sem = asyncio.Semaphore(1)
        async with holding_slot(sem) as lease:
            async with parked():
                assert lease.held is False
                async with parked():
                    assert lease.held is False
                # The inner exit must NOT prematurely re-acquire.
                assert lease.held is False
            assert lease.held is True

    async def test_reacquire_on_wait_exception(self):
        """The slot is re-acquired even when the wait body raises."""
        sem = asyncio.Semaphore(1)
        async with holding_slot(sem) as lease:
            with pytest.raises(ValueError):
                async with parked():
                    raise ValueError("wait failed")
            assert lease.held is True
        assert not sem.locked()

    async def test_parked_item_does_not_starve_runnable_work(self):
        """The core Phase 2 guarantee.

        With one slot: worker A parks on a slow external wait; worker
        B must acquire the slot and finish while A is still parked.
        Legacy behaviour (wait inside ``async with semaphore:``) would
        deadline B behind A's full wait.
        """
        sem = asyncio.Semaphore(1)
        a_parked = asyncio.Event()
        b_done = asyncio.Event()
        order: list[str] = []

        async def worker_a():
            async with holding_slot(sem):
                order.append("a-active")
                async with parked():
                    a_parked.set()
                    # Simulate a long external wait; B must complete
                    # well before this deadline.
                    await asyncio.wait_for(b_done.wait(), timeout=5.0)
                order.append("a-resumed")

        async def worker_b():
            await asyncio.wait_for(a_parked.wait(), timeout=2.0)
            async with holding_slot(sem):
                order.append("b-active")
            b_done.set()

        await asyncio.gather(worker_a(), worker_b())
        assert order == ["a-active", "b-active", "a-resumed"]

    async def test_parked_reacquire_competes_fairly(self):
        """A parked worker re-queues for the slot when it wakes."""
        sem = asyncio.Semaphore(1)
        a_parked = asyncio.Event()
        b_started = asyncio.Event()
        release_b = asyncio.Event()
        order: list[str] = []

        async def worker_a():
            async with holding_slot(sem):
                async with parked():
                    a_parked.set()
                    await asyncio.wait_for(b_started.wait(), timeout=2.0)
                    # B holds the slot now; A's re-acquire must block
                    # until B finishes.
                order.append("a-resumed")

        async def worker_b():
            await asyncio.wait_for(a_parked.wait(), timeout=2.0)
            async with holding_slot(sem):
                b_started.set()
                await asyncio.wait_for(release_b.wait(), timeout=2.0)
                order.append("b-done")

        async def releaser():
            await asyncio.wait_for(b_started.wait(), timeout=2.0)
            # Give A's re-acquire a chance to (wrongly) jump the queue.
            await asyncio.sleep(0.05)
            release_b.set()

        await asyncio.gather(worker_a(), worker_b(), releaser())
        assert order == ["b-done", "a-resumed"]


# ---------------------------------------------------------------------------
# Production wiring: waits inside the merge path release the slot
# ---------------------------------------------------------------------------


class TestMergeManagerParksWaits:
    """The auto-merge wait releases the worker's concurrency slot."""

    async def test_wait_for_auto_merge_parks_the_slot(self, mocker):
        from dependamerge.merge_manager import AsyncMergeManager
        from dependamerge.models import PullRequestInfo

        mgr = AsyncMergeManager(
            token="t",
            concurrency=1,
            merge_timeout=30.0,
        )
        # Tighten the poll cadence so the test runs fast.
        mgr._merge_recheck_interval = 0.01

        pr = PullRequestInfo(
            number=1,
            node_id="n1",
            title="t",
            body=None,
            author="dependabot[bot]",
            head_sha="abc",
            base_branch="main",
            head_branch="dep/x",
            state="open",
            mergeable=True,
            mergeable_state="blocked",
            behind_by=None,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/1",
        )

        slot_state_during_wait: list[bool] = []

        async def fake_get(url: str):
            # Observe the semaphore while the wait loop polls: the
            # slot must be free (parked) at this point.
            slot_state_during_wait.append(mgr._merge_semaphore.locked())
            return {
                "state": "closed",
                "merged": True,
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {"sha": "abc"},
            }

        client = mocker.AsyncMock()
        client.get = mocker.AsyncMock(side_effect=fake_get)
        mgr._github_client = client

        async with holding_slot(mgr._merge_semaphore):
            assert mgr._merge_semaphore.locked()
            closed, merged = await mgr._wait_for_auto_merge(
                pr,
                "org",
                "repo",
                continue_states=("blocked",),
            )
            # Slot re-acquired after the wait.
            assert mgr._merge_semaphore.locked()

        assert closed is True
        assert merged is True
        assert slot_state_during_wait  # the poll ran at least once
        assert not any(slot_state_during_wait)  # slot was free every poll
