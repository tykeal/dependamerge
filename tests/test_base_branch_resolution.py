# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for base-branch resolution in block-reason analysis.

Many repositories use ``master`` (not ``main``) as their default branch.
``analyze_block_reason`` previously hardcoded ``main`` as both the initial
value and the fallback for the PR's base branch, so when the PR base ref
could not be read it would inspect required status checks and classify the
guarding rule for the *wrong* branch — producing a misleading block reason
on ``master`` repositories.

It now prefers the PR's own base ref, falls back to the repository's real
default branch, and only gives up (skipping branch-specific inspection)
when neither can be determined.
"""

from unittest.mock import AsyncMock, patch

import pytest

from dependamerge.github_async import GitHubAsync


def _block_reason_router(*, pr_data, repo_data=None, reviews=None):
    """Route ``analyze_block_reason``'s GETs.

    The PR is approved with no failing/required checks so the method
    reaches the final guard-kind classification, where the resolved base
    branch is used.
    """
    reviews = reviews if reviews is not None else [{"state": "APPROVED", "user": {}}]

    async def _get(url: str):
        if url.endswith("/reviews"):
            return reviews
        if url.endswith("/comments"):
            return []
        if "/check-runs" in url:
            return {"check_runs": []}
        if url.endswith("/status"):
            return {"statuses": []}
        if url == "/repos/owner/repo":
            if repo_data is None:
                raise RuntimeError("403 Forbidden")
            return repo_data
        if url.endswith("/pulls/123"):
            if pr_data is None:
                raise RuntimeError("404 Not Found")
            return pr_data
        return {}

    return _get


class TestResolveDefaultBranch:
    @pytest.mark.asyncio
    async def test_returns_actual_default_branch(self) -> None:
        async with GitHubAsync(token="t") as api:
            api.get = AsyncMock(return_value={"default_branch": "master"})  # type: ignore[method-assign]
            assert await api._resolve_default_branch("owner", "repo") == "master"
            api.get.assert_awaited_once_with("/repos/owner/repo")

    @pytest.mark.asyncio
    async def test_returns_none_when_field_absent(self) -> None:
        async with GitHubAsync(token="t") as api:
            api.get = AsyncMock(return_value={})  # type: ignore[method-assign]
            assert await api._resolve_default_branch("owner", "repo") is None
            api.get.assert_awaited_once_with("/repos/owner/repo")

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self) -> None:
        async with GitHubAsync(token="t") as api:
            api.get = AsyncMock(side_effect=RuntimeError("403 Forbidden"))  # type: ignore[method-assign]
            assert await api._resolve_default_branch("owner", "repo") is None
            api.get.assert_awaited_once_with("/repos/owner/repo")


class TestAnalyzeBlockReasonBaseBranch:
    @pytest.mark.asyncio
    async def test_uses_pr_base_ref_not_assumed_main(self) -> None:
        """The PR's own base ref (e.g. master) drives guard detection."""
        async with GitHubAsync(token="t") as api:
            # Wrap the router in an AsyncMock so the call list can be
            # inspected: a readable PR base ref must satisfy resolution
            # without falling back to a repository-metadata lookup.
            api.get = AsyncMock(  # type: ignore[method-assign]
                side_effect=_block_reason_router(pr_data={"base": {"ref": "master"}})
            )
            api.get_required_status_checks = AsyncMock(return_value=[])  # type: ignore[method-assign]

            with patch.object(
                api,
                "_detect_branch_protection_kind",
                new_callable=AsyncMock,
                return_value="ruleset",
            ) as mock_kind:
                result = await api.analyze_block_reason("owner", "repo", 123, "abc123")

            assert "ruleset" in result.lower()
            mock_kind.assert_awaited_once_with("owner", "repo", "master")
            api.get_required_status_checks.assert_awaited_once_with(
                "owner", "repo", "master"
            )
            # The base ref came straight from the PR, so the repository
            # default-branch fallback must not have been triggered.
            repo_meta_calls = [
                call
                for call in api.get.await_args_list
                if call.args == ("/repos/owner/repo",)
            ]
            assert not repo_meta_calls

    @pytest.mark.asyncio
    async def test_falls_back_to_repo_default_branch(self) -> None:
        """When the PR base ref is unreadable, use the repo default branch."""
        async with GitHubAsync(token="t") as api:
            # PR data has no usable base ref; repo defaults to master.
            router = _block_reason_router(
                pr_data={}, repo_data={"default_branch": "master"}
            )
            api.get = router  # type: ignore[method-assign]
            api.get_required_status_checks = AsyncMock(return_value=[])  # type: ignore[method-assign]

            with patch.object(
                api,
                "_detect_branch_protection_kind",
                new_callable=AsyncMock,
                return_value="protection",
            ) as mock_kind:
                result = await api.analyze_block_reason("owner", "repo", 123, "abc123")

            assert "protection" in result.lower()
            mock_kind.assert_awaited_once_with("owner", "repo", "master")
            api.get_required_status_checks.assert_awaited_once_with(
                "owner", "repo", "master"
            )

    @pytest.mark.asyncio
    async def test_undetermined_when_branch_cannot_be_resolved(self) -> None:
        """No PR ref and no readable repo metadata: skip branch inspection."""
        async with GitHubAsync(token="t") as api:
            api.get = _block_reason_router(pr_data=None, repo_data=None)  # type: ignore[method-assign]
            api.get_required_status_checks = AsyncMock(return_value=[])  # type: ignore[method-assign]

            with patch.object(
                api,
                "_detect_branch_protection_kind",
                new_callable=AsyncMock,
            ) as mock_kind:
                result = await api.analyze_block_reason("owner", "repo", 123, "abc123")

            # The guard-kind probe must not run against an assumed branch.
            mock_kind.assert_not_awaited()
            # Required-status inspection must also be skipped.
            api.get_required_status_checks.assert_not_awaited()
            # The message must say the branch could not be determined,
            # not imply protection rules were inspected and found absent.
            assert "undetermined" in result.lower()
            assert "base" in result.lower() and "branch" in result.lower()
