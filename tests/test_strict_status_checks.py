# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the strict up-to-date policy probe and Step 5 gating.

A ``behind`` PR only needs a branch refresh before merging when the
base branch's protection enforces the *strict* status-check policy
("Require branches to be up to date before merging").  These tests
cover:

- ``GitHubAsync.requires_strict_status_checks`` (classic branch
  protection + repository rulesets, caching, reliability semantics)
- ``AsyncMergeManager._behind_pr_requires_rebase`` (the Step 5 gate
  that consults the probe)
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dependamerge.github_async import GitHubAsync
from dependamerge.models import PullRequestInfo
from tests.conftest import make_merge_manager

_BEHIND_PR = PullRequestInfo(
    number=77,
    node_id="PR_kwDOTestNode77",
    title="Chore: Bump step-security/harden-runner from 2.19.4 to 2.20.0",
    body="Dependabot PR",
    author="dependabot[bot]",
    head_sha="abc123",
    base_branch="main",
    head_branch="dependabot/github_actions/harden-runner-2.20.0",
    state="open",
    mergeable=True,
    mergeable_state="behind",
    behind_by=1,
    files_changed=[],
    repository_full_name="owner/repo",
    html_url="https://github.com/owner/repo/pull/77",
    reviews=[],
    review_comments=[],
)


def _api() -> AsyncMock:
    """AsyncMock GitHubAsync with real ruleset-condition matching."""
    api = AsyncMock(spec=GitHubAsync)
    api.log = AsyncMock()
    api.log.debug = lambda *a, **kw: None
    api._ruleset_applies_to_branch = GitHubAsync._ruleset_applies_to_branch
    return api


def _ruleset_get(
    rulesets: list[dict[str, Any]],
    details: dict[int, dict[str, Any]],
):
    """Build a ``get`` side effect serving repo metadata + rulesets."""

    async def _get(path: str, *args: Any, **kwargs: Any) -> Any:
        if path == "/repos/owner/repo":
            return {"default_branch": "main"}
        if "/rulesets?" in path:
            # Single page of rulesets (fewer than per_page entries, so
            # the caller's pagination loop stops after one request).
            return rulesets
        for rs_id, detail in details.items():
            if path.endswith(f"/rulesets/{rs_id}"):
                return detail
        raise AssertionError(f"unexpected GET {path}")

    return _get


class TestRequiresStrictStatusChecks:
    """Classic + ruleset detection of the strict up-to-date policy."""

    @pytest.mark.asyncio
    async def test_classic_strict_policy_detected(self) -> None:
        api = _api()
        api.get_branch_protection = AsyncMock(
            return_value={"required_status_checks": {"strict": True}}
        )
        result, reliable = await GitHubAsync._requires_strict_status_checks_uncached(
            api, "owner", "repo", "main"
        )
        assert result is True
        assert reliable is True

    @pytest.mark.asyncio
    async def test_classic_non_strict_without_rulesets(self) -> None:
        api = _api()
        api.get_branch_protection = AsyncMock(
            return_value={"required_status_checks": {"strict": False}}
        )
        api.get = AsyncMock(side_effect=_ruleset_get([], {}))
        result, reliable = await GitHubAsync._requires_strict_status_checks_uncached(
            api, "owner", "repo", "main"
        )
        assert result is False
        assert reliable is True

    @pytest.mark.asyncio
    async def test_no_protection_at_all(self) -> None:
        api = _api()
        # get_branch_protection maps 404 to {} itself.
        api.get_branch_protection = AsyncMock(return_value={})
        api.get = AsyncMock(side_effect=_ruleset_get([], {}))
        result, reliable = await GitHubAsync._requires_strict_status_checks_uncached(
            api, "owner", "repo", "main"
        )
        assert result is False
        assert reliable is True

    @pytest.mark.asyncio
    async def test_ruleset_strict_policy_detected(self) -> None:
        api = _api()
        api.get_branch_protection = AsyncMock(return_value={})
        detail = {
            "id": 7,
            "enforcement": "active",
            "name": "main protection",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"]}},
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [{"context": "build"}],
                    },
                }
            ],
        }
        api.get = AsyncMock(side_effect=_ruleset_get([{"id": 7}], {7: detail}))
        result, reliable = await GitHubAsync._requires_strict_status_checks_uncached(
            api, "owner", "repo", "main"
        )
        assert result is True
        assert reliable is True

    @pytest.mark.asyncio
    async def test_ruleset_without_strict_policy_is_false(self) -> None:
        api = _api()
        api.get_branch_protection = AsyncMock(return_value={})
        detail = {
            "id": 7,
            "enforcement": "active",
            "name": "main protection",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"]}},
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": False,
                        "required_status_checks": [{"context": "build"}],
                    },
                }
            ],
        }
        api.get = AsyncMock(side_effect=_ruleset_get([{"id": 7}], {7: detail}))
        result, reliable = await GitHubAsync._requires_strict_status_checks_uncached(
            api, "owner", "repo", "main"
        )
        assert result is False
        assert reliable is True

    @pytest.mark.asyncio
    async def test_inactive_ruleset_is_ignored(self) -> None:
        api = _api()
        api.get_branch_protection = AsyncMock(return_value={})
        detail = {
            "id": 7,
            "enforcement": "disabled",
            "conditions": {"ref_name": {"include": ["~ALL"]}},
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {"strict_required_status_checks_policy": True},
                }
            ],
        }
        api.get = AsyncMock(side_effect=_ruleset_get([{"id": 7}], {7: detail}))
        result, reliable = await GitHubAsync._requires_strict_status_checks_uncached(
            api, "owner", "repo", "main"
        )
        assert result is False
        assert reliable is True

    @pytest.mark.asyncio
    async def test_api_errors_yield_unreliable_false(self) -> None:
        api = _api()
        api.get_branch_protection = AsyncMock(side_effect=Exception("boom"))
        api.get = AsyncMock(side_effect=Exception("boom"))
        result, reliable = await GitHubAsync._requires_strict_status_checks_uncached(
            api, "owner", "repo", "main"
        )
        assert result is False
        # Error-derived False verdicts are flagged unreliable so the
        # public method will not cache them.
        assert reliable is False

    @pytest.mark.asyncio
    async def test_reliable_verdict_is_cached(self) -> None:
        api = _api()
        api._requires_strict_checks_cache = {}
        api._requires_strict_status_checks_uncached = AsyncMock(
            return_value=(True, True)
        )
        first = await GitHubAsync.requires_strict_status_checks(api, "owner", "repo")
        second = await GitHubAsync.requires_strict_status_checks(api, "owner", "repo")
        assert first is True
        assert second is True
        api._requires_strict_status_checks_uncached.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unreliable_verdict_not_cached(self) -> None:
        api = _api()
        api._requires_strict_checks_cache = {}
        api._requires_strict_status_checks_uncached = AsyncMock(
            return_value=(False, False)
        )
        await GitHubAsync.requires_strict_status_checks(api, "owner", "repo")
        await GitHubAsync.requires_strict_status_checks(api, "owner", "repo")
        assert api._requires_strict_status_checks_uncached.await_count == 2
        assert api._requires_strict_checks_cache == {}


class TestBehindPrRequiresRebase:
    """Step 5 gate: rebase a behind PR only under the strict policy."""

    @pytest.mark.asyncio
    async def test_strict_policy_requires_rebase(self) -> None:
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.requires_strict_status_checks = AsyncMock(return_value=True)

        assert await mgr._behind_pr_requires_rebase(
            _BEHIND_PR.model_copy(), "owner", "repo"
        )

    @pytest.mark.asyncio
    async def test_non_strict_policy_skips_rebase(self) -> None:
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.requires_strict_status_checks = AsyncMock(return_value=False)

        assert not await mgr._behind_pr_requires_rebase(
            _BEHIND_PR.model_copy(), "owner", "repo"
        )

    @pytest.mark.asyncio
    async def test_probe_failure_skips_rebase(self) -> None:
        """Probe errors fail to 'no rebase': the merge attempt decides."""
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.requires_strict_status_checks = AsyncMock(
            side_effect=RuntimeError("boom")
        )

        assert not await mgr._behind_pr_requires_rebase(
            _BEHIND_PR.model_copy(), "owner", "repo"
        )

    @pytest.mark.asyncio
    async def test_truthy_non_true_verdict_skips_rebase(self) -> None:
        """AsyncMock-default truthy values must not trigger rebases."""
        mgr, client = make_merge_manager(fix_out_of_date=True)
        client.requires_strict_status_checks = AsyncMock(return_value=MagicMock())

        assert not await mgr._behind_pr_requires_rebase(
            _BEHIND_PR.model_copy(), "owner", "repo"
        )
