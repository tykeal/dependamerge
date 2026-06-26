# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Regression tests for owner-wide commands against personal user accounts.

The owner-wide *read* commands (``status``, ``blocked``, and the
similar-PR scan behind ``close``) historically assumed the owner login
resolved to a GitHub *organization*.  When handed a personal user
account, the GraphQL ``organization(login:)`` root resolves to null and
GitHub returns a ``NOT_FOUND`` error, which aborted the whole scan with::

    Could not resolve to an Organization with the login of '<user>'.

The merge command never hit this because it enumerates repositories via
:meth:`GitHubService._iter_owner_repositories`, which probes
``organization`` first and falls back to ``user``.  These tests lock in
the fix that routes the read commands through the same owner-aware
enumeration, so a regression that re-introduces the org-only assumption
fails fast in CI rather than in production.

The GraphQL transport is mocked to emulate the org→user fallback, so no
network access or token is required.
"""

from __future__ import annotations

import json

import pytest

from dependamerge.github_async import GraphQLError
from dependamerge.github_service import GitHubService
from dependamerge.models import PullRequestInfo


def _not_an_org_error(login: str) -> GraphQLError:
    """Build the GraphQL error GitHub returns for a user login.

    Mirrors the structured ``errors`` payload that
    ``GitHubAsync.graphql`` re-raises as a ``GraphQLError`` whose string
    form is the JSON-encoded array.
    """
    return GraphQLError(
        json.dumps(
            [
                {
                    "type": "NOT_FOUND",
                    "path": ["organization"],
                    "locations": [{"line": 3, "column": 3}],
                    "message": (
                        "Could not resolve to an Organization with the "
                        f"login of '{login}'."
                    ),
                }
            ]
        )
    )


def _repos_connection(nodes: list[dict]) -> dict:
    return {
        "totalCount": len(nodes),
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": nodes,
    }


def _make_user_account_graphql(login: str, repo_nodes: list[dict]):
    """Return an async ``graphql`` stub that behaves like a user account.

    The ``organization(login:)`` probe raises ``NOT_FOUND`` (as GitHub
    does for a user login); the ``user(login:)`` query returns the
    supplied repositories.  Anything else returns an empty payload.
    """

    async def _graphql(query: str, variables: dict | None = None):
        if "organization(login:" in query:
            raise _not_an_org_error(login)
        if "user(login:" in query:
            return {"user": {"repositories": _repos_connection(repo_nodes)}}
        return {}

    return _graphql


class TestIterReposUserFallback:
    """Direct tests for the owner-aware repository iterators."""

    @pytest.mark.asyncio
    async def test_iter_org_repositories_falls_back_to_user(self, mocker):
        """``_iter_org_repositories`` must enumerate a *user* account.

        This is the core regression: the historical org-only iterator
        raised ``NOT_FOUND`` here, taking ``status``/``blocked``/``close``
        down with it.
        """
        service = GitHubService(token="test_token")
        try:
            repo_nodes = [
                {"nameWithOwner": "auser/repo-a", "isArchived": False, "isFork": False},
                {"nameWithOwner": "auser/repo-b", "isArchived": False, "isFork": False},
            ]
            mocker.patch.object(
                service._api,
                "graphql",
                side_effect=_make_user_account_graphql("auser", repo_nodes),
            )

            seen = [repo async for repo in service._iter_org_repositories("auser")]

            names = [r["nameWithOwner"] for r in seen]
            assert names == ["auser/repo-a", "auser/repo-b"]
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_iter_org_repositories_includes_forks(self, mocker):
        """Read paths include forks; the merge path excludes them.

        ``_iter_org_repositories`` (read commands) keeps forks for a
        complete picture, whereas ``_iter_owner_repositories`` defaults
        to ``skip_forks=True`` for owner-wide bulk merges.
        """
        service = GitHubService(token="test_token")
        try:
            repo_nodes = [
                {"nameWithOwner": "auser/own", "isArchived": False, "isFork": False},
                {"nameWithOwner": "auser/forked", "isArchived": False, "isFork": True},
                {"nameWithOwner": "auser/old", "isArchived": True, "isFork": False},
            ]
            mocker.patch.object(
                service._api,
                "graphql",
                side_effect=_make_user_account_graphql("auser", repo_nodes),
            )

            read_repos = [
                r["nameWithOwner"]
                async for r in service._iter_org_repositories("auser")
            ]
            # Archived always skipped; fork retained for read commands.
            assert read_repos == ["auser/own", "auser/forked"]

            # Reset the cached owner-root verdict so the second pass
            # re-probes cleanly with a fresh stub.
            service._owner_root_cache.clear()
            mocker.patch.object(
                service._api,
                "graphql",
                side_effect=_make_user_account_graphql("auser", repo_nodes),
            )
            merge_repos = [
                r["nameWithOwner"]
                async for r in service._iter_owner_repositories("auser")
            ]
            # Both archived and fork skipped for the bulk-merge path.
            assert merge_repos == ["auser/own"]
        finally:
            await service.close()


class TestStatusUserAccount:
    """``gather_organization_status`` against a personal user account."""

    @pytest.mark.asyncio
    async def test_status_scans_user_repositories(self, mocker):
        service = GitHubService(token="test_token")
        try:
            repo_nodes = [
                {"nameWithOwner": "auser/repo-a", "isArchived": False, "isFork": False},
            ]
            mocker.patch.object(
                service._api,
                "graphql",
                side_effect=_make_user_account_graphql("auser", repo_nodes),
            )
            # Short-circuit the per-repo metadata gathering so the test
            # exercises only the owner-aware enumeration path.
            mocker.patch.object(
                service, "_get_latest_tag", return_value=("v1.0.0", "2026/01/01")
            )
            mocker.patch.object(
                service, "_get_latest_release", return_value=("v1.0.0", "2026/01/01")
            )
            mocker.patch.object(
                service,
                "_gather_pr_statistics",
                return_value={
                    "open_prs_human": 0,
                    "open_prs_automation": 1,
                    "merged_prs_human": 0,
                    "merged_prs_automation": 0,
                    "action_prs_human": 0,
                    "action_prs_automation": 0,
                    "workflow_prs_human": 0,
                    "workflow_prs_automation": 0,
                },
            )

            result = await service.gather_organization_status("auser")

            assert result.organization == "auser"
            assert result.scanned_repositories == 1
            assert [s.repository_name for s in result.repository_statuses] == ["repo-a"]
            assert result.errors == []
        finally:
            await service.close()


class TestBlockedUserAccount:
    """``scan_organization`` (the ``blocked`` command) against a user."""

    @pytest.mark.asyncio
    async def test_blocked_scans_user_repositories(self, mocker):
        service = GitHubService(token="test_token")
        try:
            repo_nodes = [
                {"nameWithOwner": "auser/repo-a", "isArchived": False, "isFork": False},
            ]
            mocker.patch.object(
                service._api,
                "graphql",
                side_effect=_make_user_account_graphql("auser", repo_nodes),
            )
            # No open PRs in the repo: the scan should complete cleanly
            # rather than aborting with NOT_FOUND.
            mocker.patch.object(
                service,
                "_fetch_repo_prs_first_page",
                return_value=([], {"hasNextPage": False, "endCursor": None}),
            )

            result = await service.scan_organization("auser")

            assert result.organization == "auser"
            assert result.scanned_repositories == 1
            assert result.unmergeable_prs == []
            assert result.errors == []
        finally:
            await service.close()


class TestFindSimilarUserAccount:
    """``find_similar_prs`` (behind ``close``) against a user account."""

    @pytest.mark.asyncio
    async def test_find_similar_scans_user_repositories(self, mocker):
        service = GitHubService(token="test_token")
        try:
            repo_nodes = [
                {"nameWithOwner": "auser/repo-a", "isArchived": False, "isFork": False},
            ]
            mocker.patch.object(
                service._api,
                "graphql",
                side_effect=_make_user_account_graphql("auser", repo_nodes),
            )
            mocker.patch.object(
                service,
                "_fetch_repo_prs_first_page",
                return_value=([], {"hasNextPage": False, "endCursor": None}),
            )

            source_pr = PullRequestInfo(
                number=1,
                title="Chore: Bump actions/checkout from 4 to 5",
                body="Bumps actions/checkout",
                author="dependabot[bot]",
                head_sha="abc123",
                base_branch="main",
                head_branch="dependabot/github_actions/actions/checkout-5",
                state="open",
                mergeable=True,
                mergeable_state="clean",
                behind_by=0,
                files_changed=[],
                repository_full_name="auser/repo-a",
                html_url="https://github.com/auser/repo-a/pull/1",
            )

            class _Comparator:
                def compare_pull_requests(self, source, target, only_automation):
                    raise AssertionError("no candidate PRs expected")

            result = await service.find_similar_prs(
                "auser", source_pr, _Comparator(), only_automation=True
            )

            # No candidates in the user's single empty repo, but crucially
            # the owner-wide scan completed without a NOT_FOUND error.
            assert result == []
        finally:
            await service.close()
