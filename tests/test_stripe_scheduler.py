# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the owner-wide striped merge scheduler.

The load-bearing guarantee of the striped scheduler is that **no two PRs
from the same repository are ever in flight simultaneously**, while
distinct repositories are still processed concurrently.  These tests
assert that invariant with a deterministic fake that records the set of
in-flight repositories at every dispatch.
"""

import asyncio

import pytest

from dependamerge.merge_manager import (
    AsyncMergeManager,
    MergeResult,
    MergeStatus,
)
from dependamerge.models import ComparisonResult, PullRequestInfo

# Work-item type accepted by ``merge_prs_parallel``.  Annotating the test
# fixtures with this (rather than letting the literal narrow to
# ``tuple[PullRequestInfo, None]``) keeps the invariant ``list`` element
# type assignable to the method's parameter.
PRPair = tuple[PullRequestInfo, ComparisonResult | None]


def _make_pr(number: int, repo: str) -> PullRequestInfo:
    """Build a minimal PullRequestInfo for scheduler testing."""
    return PullRequestInfo(
        number=number,
        title=f"Bump dep in {repo} #{number}",
        body="Automated dependency update",
        author="dependabot[bot]",
        head_sha="abc123",
        base_branch="main",
        head_branch=f"dependabot/{repo}/{number}",
        state="open",
        mergeable=True,
        mergeable_state="clean",
        behind_by=0,
        files_changed=[],
        repository_full_name=repo,
        html_url=f"https://github.com/{repo}/pull/{number}",
    )


class _Recorder:
    """Instrumented replacement for ``_merge_single_pr``.

    Records, for every PR dispatched, the set of repositories that were
    in flight at that moment, so tests can assert the per-repo
    single-flight invariant and observe cross-repo concurrency.
    """

    def __init__(self, hold: float = 0.02) -> None:
        self._hold = hold
        self.inflight_by_repo: dict[str, int] = {}
        self.max_concurrent_per_repo: dict[str, int] = {}
        self.max_global_inflight = 0
        self.dispatch_order: list[tuple[str, int]] = []

    async def __call__(self, pr_info: PullRequestInfo) -> MergeResult:
        repo = pr_info.repository_full_name
        self.inflight_by_repo[repo] = self.inflight_by_repo.get(repo, 0) + 1
        self.max_concurrent_per_repo[repo] = max(
            self.max_concurrent_per_repo.get(repo, 0),
            self.inflight_by_repo[repo],
        )
        self.max_global_inflight = max(
            self.max_global_inflight,
            sum(self.inflight_by_repo.values()),
        )
        self.dispatch_order.append((repo, pr_info.number))
        # Hold the "in flight" state long enough that any scheduling
        # violation (two PRs of one repo, or more than `concurrency`
        # repos at once) would overlap and be observed.
        await asyncio.sleep(self._hold)
        self.inflight_by_repo[repo] -= 1
        return MergeResult(pr_info=pr_info, status=MergeStatus.MERGED)


def _manager(concurrency: int = 10) -> AsyncMergeManager:
    return AsyncMergeManager(
        token="test_token",
        concurrency=concurrency,
        preview_mode=True,
    )


@pytest.mark.asyncio
async def test_striped_single_flight_per_repo():
    """No repository ever has more than one PR in flight at a time."""
    pr_list: list[PRPair] = [
        (_make_pr(1, "owner/a"), None),
        (_make_pr(2, "owner/a"), None),
        (_make_pr(3, "owner/a"), None),
        (_make_pr(1, "owner/b"), None),
        (_make_pr(2, "owner/b"), None),
        (_make_pr(1, "owner/c"), None),
    ]
    mgr = _manager(concurrency=10)
    recorder = _Recorder()
    mgr._merge_single_pr = recorder  # type: ignore[assignment]

    results = await mgr.merge_prs_parallel(pr_list, stripe=True)

    # Load-bearing invariant: at most one in-flight PR per repository.
    assert all(count == 1 for count in recorder.max_concurrent_per_repo.values()), (
        recorder.max_concurrent_per_repo
    )
    # Every PR produced a result.
    assert len(results) == len(pr_list)
    assert all(r.status is MergeStatus.MERGED for r in results)


@pytest.mark.asyncio
async def test_striped_runs_distinct_repos_concurrently():
    """Distinct repositories ARE processed concurrently (not serialised)."""
    pr_list: list[PRPair] = [
        (_make_pr(1, "owner/a"), None),
        (_make_pr(1, "owner/b"), None),
        (_make_pr(1, "owner/c"), None),
        (_make_pr(2, "owner/a"), None),
        (_make_pr(2, "owner/b"), None),
        (_make_pr(2, "owner/c"), None),
    ]
    mgr = _manager(concurrency=10)
    recorder = _Recorder()
    mgr._merge_single_pr = recorder  # type: ignore[assignment]

    await mgr.merge_prs_parallel(pr_list, stripe=True)

    # Three distinct repos with available concurrency should overlap.
    assert recorder.max_global_inflight >= 2
    # But still never two PRs of the same repo at once.
    assert all(c == 1 for c in recorder.max_concurrent_per_repo.values())


@pytest.mark.asyncio
async def test_striped_respects_global_concurrency_bound():
    """Global in-flight never exceeds the configured concurrency."""
    pr_list: list[PRPair] = [(_make_pr(1, f"owner/r{i}"), None) for i in range(8)]
    mgr = _manager(concurrency=3)
    recorder = _Recorder()
    mgr._merge_single_pr = recorder  # type: ignore[assignment]

    await mgr.merge_prs_parallel(pr_list, stripe=True)

    assert recorder.max_global_inflight <= 3, recorder.max_global_inflight


@pytest.mark.asyncio
async def test_striped_processes_repo_prs_in_order():
    """Within a repository, PRs are dispatched in first-seen order."""
    pr_list: list[PRPair] = [
        (_make_pr(10, "owner/a"), None),
        (_make_pr(20, "owner/a"), None),
        (_make_pr(30, "owner/a"), None),
    ]
    mgr = _manager(concurrency=5)
    recorder = _Recorder()
    mgr._merge_single_pr = recorder  # type: ignore[assignment]

    await mgr.merge_prs_parallel(pr_list, stripe=True)

    a_order = [num for repo, num in recorder.dispatch_order if repo == "owner/a"]
    assert a_order == [10, 20, 30]


@pytest.mark.asyncio
async def test_striped_preserves_result_ordering():
    """Results are returned in the caller's original input order."""
    pr_list: list[PRPair] = [
        (_make_pr(1, "owner/a"), None),
        (_make_pr(1, "owner/b"), None),
        (_make_pr(2, "owner/a"), None),
        (_make_pr(1, "owner/c"), None),
    ]
    mgr = _manager(concurrency=10)
    recorder = _Recorder()
    mgr._merge_single_pr = recorder  # type: ignore[assignment]

    results = await mgr.merge_prs_parallel(pr_list, stripe=True)

    returned = [(r.pr_info.repository_full_name, r.pr_info.number) for r in results]
    expected = [(pr.repository_full_name, pr.number) for pr, _ in pr_list]
    assert returned == expected


@pytest.mark.asyncio
async def test_striped_isolates_per_pr_failures():
    """A crash on one PR does not lose results for siblings in its repo."""
    pr_list: list[PRPair] = [
        (_make_pr(1, "owner/a"), None),
        (_make_pr(2, "owner/a"), None),
        (_make_pr(3, "owner/a"), None),
    ]
    mgr = _manager(concurrency=5)

    async def flaky(pr_info: PullRequestInfo) -> MergeResult:
        if pr_info.number == 2:
            raise RuntimeError("boom")
        return MergeResult(pr_info=pr_info, status=MergeStatus.MERGED)

    mgr._merge_single_pr = flaky  # type: ignore[assignment]

    results = await mgr.merge_prs_parallel(pr_list, stripe=True)

    assert len(results) == 3
    by_number = {r.pr_info.number: r for r in results}
    assert by_number[1].status is MergeStatus.MERGED
    assert by_number[2].status is MergeStatus.FAILED
    assert by_number[2].error and "boom" in by_number[2].error
    # PR #3 still ran even though #2 (earlier in the same repo) failed.
    assert by_number[3].status is MergeStatus.MERGED
