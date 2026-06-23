# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for organization-level settings caching and authenticated user login caching.

These tests verify that:

1. ``AsyncMergeManager._get_org_settings()`` caches the result of ``GET /orgs/{owner}``
   so that the same organization is only queried once per merge session, regardless
   of how many PRs belong to that org.

2. ``GitHubAsync.check_user_can_bypass_protection()`` caches the authenticated
   user's login (``GET /user``) so it is only fetched once per session.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from dependamerge.github_async import GitHubAsync
from dependamerge.merge_manager import AsyncMergeManager
from tests.conftest import make_merge_manager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager_with_client() -> tuple[AsyncMergeManager, AsyncMock]:
    """Convenience wrapper around the shared helper."""
    return make_merge_manager()


# ---------------------------------------------------------------------------
# _get_org_settings cache tests
# ---------------------------------------------------------------------------


class TestOrgSettingsCache:
    """Tests for AsyncMergeManager._get_org_settings caching."""

    @pytest.mark.asyncio
    async def test_first_call_queries_api(self):
        """The first call for an org should hit the API."""
        mgr, client = _make_manager_with_client()
        client.get = AsyncMock(
            return_value={"web_commit_signoff_required": True, "login": "test-org"}
        )

        result = await mgr._get_org_settings("test-org")

        assert result is not None
        assert result["web_commit_signoff_required"] is True
        client.get.assert_called_once_with("/orgs/test-org")

    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self):
        """Subsequent calls for the same org should NOT hit the API again."""
        mgr, client = _make_manager_with_client()
        client.get = AsyncMock(
            return_value={"web_commit_signoff_required": False, "login": "test-org"}
        )

        first = await mgr._get_org_settings("test-org")
        second = await mgr._get_org_settings("test-org")

        assert first is second  # same cached object
        client.get.assert_called_once_with("/orgs/test-org")

    @pytest.mark.asyncio
    async def test_different_orgs_each_query_once(self):
        """Different orgs should each be queried exactly once."""
        mgr, client = _make_manager_with_client()

        call_count: dict[str, int] = {}

        async def mock_get(url: str):
            call_count[url] = call_count.get(url, 0) + 1
            return {"login": url.split("/")[-1], "web_commit_signoff_required": False}

        client.get = AsyncMock(side_effect=mock_get)

        await mgr._get_org_settings("org-a")
        await mgr._get_org_settings("org-b")
        await mgr._get_org_settings("org-a")  # should be cached
        await mgr._get_org_settings("org-b")  # should be cached

        assert call_count["/orgs/org-a"] == 1
        assert call_count["/orgs/org-b"] == 1

    @pytest.mark.asyncio
    async def test_api_failure_is_cached_as_none(self):
        """If the org lookup fails, the failure (None) should be cached to avoid retries."""
        mgr, client = _make_manager_with_client()
        client.get = AsyncMock(side_effect=Exception("network error"))

        first = await mgr._get_org_settings("flaky-org")
        second = await mgr._get_org_settings("flaky-org")

        assert first is None
        assert second is None
        # Only one API call despite two invocations
        client.get.assert_called_once_with("/orgs/flaky-org")

    @pytest.mark.asyncio
    async def test_non_dict_response_cached_as_none(self):
        """If the API returns a non-dict value, it should be cached as None."""
        mgr, client = _make_manager_with_client()
        client.get = AsyncMock(return_value="unexpected string")

        result = await mgr._get_org_settings("weird-org")

        assert result is None
        assert mgr._org_settings_cache["weird-org"] is None

    @pytest.mark.asyncio
    async def test_no_client_returns_none(self):
        """If _github_client is None, should return None without caching."""
        mgr, _client = _make_manager_with_client()
        mgr._github_client = None

        result = await mgr._get_org_settings("any-org")

        assert result is None
        assert "any-org" not in mgr._org_settings_cache

    @pytest.mark.asyncio
    async def test_signoff_logged_once(self, caplog):
        """The commit signoff debug message should appear only once per org."""
        caplog.set_level(logging.DEBUG, logger="dependamerge.merge_manager")

        mgr, client = _make_manager_with_client()
        client.get = AsyncMock(
            return_value={"web_commit_signoff_required": True, "login": "sign-org"}
        )

        await mgr._get_org_settings("sign-org")
        await mgr._get_org_settings("sign-org")
        await mgr._get_org_settings("sign-org")

        signoff_messages = [
            r
            for r in caplog.records
            if "requires commit signoff" in r.message and "sign-org" in r.message
        ]
        assert len(signoff_messages) == 1


# ---------------------------------------------------------------------------
# _predict_merge_outcome uses the org cache
# ---------------------------------------------------------------------------


class TestPredictMergeOutcomeUsesOrgCache:
    """Verify that _predict_merge_outcome routes through the cached helper."""

    @pytest.mark.asyncio
    async def test_multiple_prs_same_org_single_org_query(self):
        """Calling _predict_merge_outcome for multiple PRs in the same org
        should only produce one GET /orgs/{owner} call."""
        mgr, client = _make_manager_with_client()

        org_call_count = 0

        async def mock_get(url: str):
            nonlocal org_call_count
            if url.startswith("/orgs/"):
                org_call_count += 1
                return {"web_commit_signoff_required": True}
            if "/pulls/" in url:
                return {
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "head": {"sha": "abc123"},
                }
            return {}

        client.get = AsyncMock(side_effect=mock_get)

        # Simulate processing 5 PRs across 3 repos in the same org
        for pr_num in range(1, 6):
            await mgr._predict_merge_outcome(
                "same-org", f"repo-{pr_num}", pr_num, "merge"
            )

        assert org_call_count == 1

    @pytest.mark.asyncio
    async def test_org_failure_does_not_block_merge_check(self):
        """If the org settings lookup fails, _predict_merge_outcome should
        still proceed to check the PR's merge status."""
        mgr, client = _make_manager_with_client()

        async def mock_get(url: str):
            if url.startswith("/orgs/"):
                raise Exception("org lookup failed")
            if "/pulls/" in url:
                return {
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "head": {"sha": "def456"},
                }
            return {}

        client.get = AsyncMock(side_effect=mock_get)

        can_merge, reason = await mgr._predict_merge_outcome(
            "broken-org", "some-repo", 1, "merge"
        )

        assert can_merge is True
        assert "passed" in reason.lower()


# ---------------------------------------------------------------------------
# GitHubAsync._authenticated_user_login cache tests
# ---------------------------------------------------------------------------


class TestAuthenticatedUserLoginCache:
    """Tests for GitHubAsync._authenticated_user_login caching."""

    def _make_github_async(self) -> GitHubAsync:
        """Create a GitHubAsync instance for testing."""
        return GitHubAsync(token="fake-token")

    @pytest.mark.asyncio
    async def test_user_login_fetched_once(self, mocker):
        """GET /user should only be called once across multiple bypass checks."""
        async with self._make_github_async() as gh:
            call_log: list[str] = []

            async def mock_get(url: str):
                call_log.append(url)
                if url == "/user":
                    return {"login": "test-user"}
                if url.startswith("/repos/") and url.endswith("/permission"):
                    return {"permission": "write"}
                if url.startswith("/repos/"):
                    return {"permissions": {"admin": False, "push": True}}
                return {}

            mocker.patch.object(gh, "get", side_effect=mock_get)

            # Check bypass permissions for 3 different repos
            await gh.check_user_can_bypass_protection("org", "repo-1")
            await gh.check_user_can_bypass_protection("org", "repo-2")
            await gh.check_user_can_bypass_protection("org", "repo-3")

            user_calls = [c for c in call_log if c == "/user"]
            assert len(user_calls) == 1
            assert gh._authenticated_user_login == "test-user"

    @pytest.mark.asyncio
    async def test_cached_login_used_for_collaborator_check(self, mocker):
        """The cached username should be used in the collaborator permission URL."""
        async with self._make_github_async() as gh:
            collaborator_urls: list[str] = []

            async def mock_get(url: str):
                if url == "/user":
                    return {"login": "cached-user"}
                if "/collaborators/" in url:
                    collaborator_urls.append(url)
                    return {"permission": "write"}
                if url.startswith("/repos/"):
                    return {"permissions": {"admin": False, "push": True}}
                return {}

            mocker.patch.object(gh, "get", side_effect=mock_get)

            await gh.check_user_can_bypass_protection("org", "my-repo")

            assert len(collaborator_urls) == 1
            assert "/collaborators/cached-user/permission" in collaborator_urls[0]

    @pytest.mark.asyncio
    async def test_user_api_failure_does_not_break_bypass_check(self, mocker):
        """If GET /user fails, the bypass check should still complete gracefully."""
        async with self._make_github_async() as gh:

            async def mock_get(url: str):
                if url == "/user":
                    raise Exception("user endpoint unavailable")
                if url.startswith("/repos/"):
                    return {"permissions": {"admin": False, "push": True}}
                return {}

            mocker.patch.object(gh, "get", side_effect=mock_get)

            can_bypass, reason = await gh.check_user_can_bypass_protection(
                "org", "repo"
            )

            # Should fall through to the push-permissions path
            assert can_bypass is False
            assert "push" in reason.lower() or "admin" in reason.lower()

    @pytest.mark.asyncio
    async def test_admin_shortcircuits_before_user_call(self, mocker):
        """If the repo permissions already show admin, GET /user should never be called."""
        async with self._make_github_async() as gh:
            call_log: list[str] = []

            async def mock_get(url: str):
                call_log.append(url)
                if url.startswith("/repos/"):
                    return {"permissions": {"admin": True, "push": True}}
                if url == "/user":
                    return {"login": "should-not-reach"}
                return {}

            mocker.patch.object(gh, "get", side_effect=mock_get)

            can_bypass, reason = await gh.check_user_can_bypass_protection(
                "org", "repo"
            )

            assert can_bypass is True
            assert "admin" in reason.lower()
            assert "/user" not in call_log

    @pytest.mark.asyncio
    async def test_login_cache_survives_collaborator_exception(self, mocker):
        """If the collaborator check raises, the cached login should persist
        for subsequent calls."""
        async with self._make_github_async() as gh:
            call_count = {"user": 0}

            async def mock_get(url: str):
                if url == "/user":
                    call_count["user"] += 1
                    return {"login": "persistent-user"}
                if "/collaborators/" in url:
                    raise Exception("collaborator endpoint error")
                if url.startswith("/repos/"):
                    return {"permissions": {"admin": False, "push": True}}
                return {}

            mocker.patch.object(gh, "get", side_effect=mock_get)

            await gh.check_user_can_bypass_protection("org", "repo-1")
            await gh.check_user_can_bypass_protection("org", "repo-2")

            assert call_count["user"] == 1
            assert gh._authenticated_user_login == "persistent-user"
