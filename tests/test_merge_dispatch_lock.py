# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Unit tests for ``AsyncMergeManager._get_merge_dispatch_lock``.

The dispatch lock decouples the actual ``merge_pull_request`` API
call from the Step 5.5 auto-merge wait loop:

* Workers targeting the same repository serialise on the lock so
  back-to-back merges don't race GitHub's branch-protection
  propagation.
* Workers targeting different repositories receive distinct locks
  and can dispatch in parallel.
* The wait loops in Step 5.5 do not hold the lock, so a PR parked
  waiting for required checks no longer head-of-line blocks the
  rest of the worker pool.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


def _make_manager(**overrides: Any):
    """Build an AsyncMergeManager using the project's typed mock helper."""
    from tests.conftest import make_merge_manager

    defaults: dict[str, Any] = {"preview_mode": False}
    defaults.update(overrides)
    return make_merge_manager(**defaults)


class TestMergeDispatchLockIdentity:
    """The lock map is keyed by ``owner/repo`` and shared correctly."""

    @pytest.mark.asyncio
    async def test_same_repo_returns_same_lock(self) -> None:
        mgr, _client = _make_manager()
        a = await mgr._get_merge_dispatch_lock("acme", "widgets")
        b = await mgr._get_merge_dispatch_lock("acme", "widgets")
        assert a is b

    @pytest.mark.asyncio
    async def test_different_repos_return_distinct_locks(self) -> None:
        mgr, _client = _make_manager()
        a = await mgr._get_merge_dispatch_lock("acme", "widgets")
        b = await mgr._get_merge_dispatch_lock("acme", "gizmos")
        c = await mgr._get_merge_dispatch_lock("other-org", "widgets")
        assert a is not b
        assert a is not c
        assert b is not c

    @pytest.mark.asyncio
    async def test_concurrent_first_acquire_does_not_duplicate_lock(self) -> None:
        """Two coroutines requesting the same lock at the same time
        must receive the *same* ``asyncio.Lock`` instance.

        Without the outer ``_merge_dispatch_locks_lock`` guard this
        would race and produce two distinct locks for the same repo,
        which would defeat the serialisation guarantee.
        """
        mgr, _client = _make_manager()
        results = await asyncio.gather(
            mgr._get_merge_dispatch_lock("acme", "widgets"),
            mgr._get_merge_dispatch_lock("acme", "widgets"),
            mgr._get_merge_dispatch_lock("acme", "widgets"),
        )
        assert results[0] is results[1] is results[2]


class TestMergeDispatchLockSerialisation:
    """The lock genuinely serialises critical sections per repo."""

    @pytest.mark.asyncio
    async def test_same_repo_critical_sections_serialise(self) -> None:
        """Two workers entering the lock for the same repo must run
        their critical sections strictly one-after-the-other."""
        mgr, _client = _make_manager()
        active = 0
        peak = 0

        async def critical(owner: str, repo: str) -> None:
            nonlocal active, peak
            lock = await mgr._get_merge_dispatch_lock(owner, repo)
            async with lock:
                active += 1
                peak = max(peak, active)
                # Yield to the event loop so a competing coroutine
                # would have a chance to enter the section if the
                # lock failed to serialise.
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(
            critical("acme", "widgets"),
            critical("acme", "widgets"),
            critical("acme", "widgets"),
        )
        assert peak == 1

    @pytest.mark.asyncio
    async def test_different_repos_run_in_parallel(self) -> None:
        """Workers on distinct repos hold distinct locks, so their
        critical sections must overlap when run concurrently."""
        mgr, _client = _make_manager()
        active = 0
        peak = 0

        async def critical(owner: str, repo: str) -> None:
            nonlocal active, peak
            lock = await mgr._get_merge_dispatch_lock(owner, repo)
            async with lock:
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(
            critical("acme", "alpha"),
            critical("acme", "beta"),
            critical("acme", "gamma"),
        )
        # All three should be in their critical sections at once.
        assert peak == 3
