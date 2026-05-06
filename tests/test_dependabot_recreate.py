# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Unit tests for the dependabot recreate logic in AsyncMergeManager.

Covers:
- Unsigned dependabot commit detected -> posts @dependabot recreate comment
- Non-dependabot PR -> no recreate triggered
- Signed commits -> no recreate triggered
- Branch does not require signatures -> no recreate triggered
- Duplicate recreate comment already exists -> no new comment
- Old PR closes and new PR appears -> returns new PullRequestInfo
- Timeout waiting for old PR to close
- Timeout waiting for new PR checks to pass
- New PR has merge conflicts -> returns None
- Preview mode -> no recreate triggered
- No GitHub client -> early return
- check_pr_commit_signatures API method
- requires_commit_signatures API method (classic + rulesets)
- Integration with _merge_single_pr failure path
"""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from dependamerge.github_async import GitHubAsync
from dependamerge.merge_manager import MergeStatus
from dependamerge.models import PullRequestInfo


async def _async_gen(*pages) -> AsyncIterator[list[Any]]:
    """Yield each *page* as an async-generator step.

    Usage::

        mock.get_paginated = lambda *a, **kw: _async_gen([commit1, commit2])
    """
    for page in pages:
        yield page


def _make_pr_info(**overrides):
    """Helper to build a PullRequestInfo with sensible defaults."""
    defaults = {
        "number": 106,
        "title": "Chore: Bump lfreleng-actions/python-build-action from 1.0.3 to 1.0.4",
        "body": "Dependabot PR",
        "author": "dependabot[bot]",
        "head_sha": "a4355a87b5b6d86b7fa1305771982853d827e796",
        "base_branch": "main",
        "head_branch": "dependabot/github_actions/lfreleng-actions/python-build-action-1.0.4",
        "state": "open",
        "mergeable": True,
        "mergeable_state": "blocked",
        "behind_by": 0,
        "files_changed": [],
        "repository_full_name": "lfreleng-actions/gerrit-clone-action",
        "html_url": "https://github.com/lfreleng-actions/gerrit-clone-action/pull/106",
    }
    defaults.update(overrides)
    return PullRequestInfo(**defaults)


def _make_manager(**overrides):
    """Build an AsyncMergeManager with a mocked GitHub client.

    Returns ``(manager, client)`` — see ``tests/conftest.py`` for the
    typed-mock-client pattern and rationale.  Use ``client`` (not
    ``mgr._github_client``) for all mock setup and assertions.
    """
    # Typed mock client pattern — see tests/conftest.py
    from tests.conftest import make_merge_manager

    defaults: dict[str, Any] = {"preview_mode": False}
    defaults.update(overrides)
    return make_merge_manager(**defaults)


# ---------------------------------------------------------------------------
# GitHubAsync.check_pr_commit_signatures
# ---------------------------------------------------------------------------
class TestCheckPrCommitSignatures:
    """Tests for the check_pr_commit_signatures method."""

    @pytest.mark.asyncio
    async def test_all_commits_verified(self):
        api = AsyncMock(spec=GitHubAsync)
        commits_page = [
            {
                "sha": "abc123def456",
                "commit": {
                    "verification": {"verified": True, "reason": "valid"},
                },
            },
            {
                "sha": "789012345678",
                "commit": {
                    "verification": {"verified": True, "reason": "valid"},
                },
            },
        ]
        api.get_paginated = lambda *a, **kw: _async_gen(commits_page)
        # Call the real method
        result = await GitHubAsync.check_pr_commit_signatures(api, "owner", "repo", 42)
        assert result == (True, [])

    @pytest.mark.asyncio
    async def test_some_commits_unverified(self):
        api = AsyncMock(spec=GitHubAsync)
        commits_page = [
            {
                "sha": "abc123def456",
                "commit": {
                    "verification": {"verified": True, "reason": "valid"},
                },
            },
            {
                "sha": "deadbeef1234",
                "commit": {
                    "verification": {"verified": False, "reason": "unsigned"},
                },
            },
        ]
        api.get_paginated = lambda *a, **kw: _async_gen(commits_page)
        result = await GitHubAsync.check_pr_commit_signatures(api, "owner", "repo", 42)
        all_verified, unverified = result
        assert all_verified is False
        assert len(unverified) == 1
        assert "deadbeef" in unverified[0]

    @pytest.mark.asyncio
    async def test_missing_verification_field(self):
        api = AsyncMock(spec=GitHubAsync)
        commits_page = [
            {
                "sha": "abc123def456",
                "commit": {},  # no verification field
            },
        ]
        api.get_paginated = lambda *a, **kw: _async_gen(commits_page)
        result = await GitHubAsync.check_pr_commit_signatures(api, "owner", "repo", 42)
        all_verified, unverified = result
        assert all_verified is False
        assert len(unverified) == 1

    @pytest.mark.asyncio
    async def test_api_error_propagates(self):
        """On API errors, the exception now propagates to callers.

        The previous fail-open default (returning ``(True, [])`` on
        error) collided with the signature-preservation gate in
        ``rebase.py``, which interprets ``all_verified=True`` as a
        positive confirmation. Surfacing the error lets each caller
        choose its own fail-open / fail-closed semantics.
        """
        api = AsyncMock(spec=GitHubAsync)

        async def _raise_gen(*a, **kw):
            raise RuntimeError("API error")
            yield  # pragma: no cover – makes this an async generator

        api.get_paginated = _raise_gen
        api.log = AsyncMock()
        api.log.debug = lambda *a, **kw: None
        with pytest.raises(RuntimeError, match="API error"):
            await GitHubAsync.check_pr_commit_signatures(
                api, "owner", "repo", 42
            )

    @pytest.mark.asyncio
    async def test_unexpected_response_shape(self):
        """Non-list response should be treated as all verified."""
        api = AsyncMock(spec=GitHubAsync)
        api.get_paginated = lambda *a, **kw: _async_gen({"unexpected": "dict"})
        result = await GitHubAsync.check_pr_commit_signatures(api, "owner", "repo", 42)
        assert result == (True, [])


# ---------------------------------------------------------------------------
# GitHubAsync.requires_commit_signatures
# ---------------------------------------------------------------------------
class TestRequiresCommitSignatures:
    """Tests for the requires_commit_signatures method."""

    @pytest.mark.asyncio
    async def test_classic_protection_enabled(self):
        api = AsyncMock(spec=GitHubAsync)
        api.get = AsyncMock(return_value={"enabled": True})
        api.log = AsyncMock()
        api.log.debug = lambda *a, **kw: None
        result = await GitHubAsync.requires_commit_signatures(
            api, "owner", "repo", "main"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_classic_protection_disabled(self):
        api = AsyncMock(spec=GitHubAsync)
        # Call sequence: classic protection disabled, repo data, rulesets (empty)
        api.get = AsyncMock(
            side_effect=[
                {"enabled": False},
                {"default_branch": "main"},
                [],
            ]
        )
        api.log = AsyncMock()
        api.log.debug = lambda *a, **kw: None
        result = await GitHubAsync.requires_commit_signatures(
            api, "owner", "repo", "main"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_classic_protection_404_falls_through_to_rulesets(self):
        api = AsyncMock(spec=GitHubAsync)
        # Call sequence:
        #   1. classic 404
        #   2. repo data (default_branch)
        #   3. rulesets list page 1 (contains id)
        #   4. ruleset detail fetch for id=1
        api.get = AsyncMock(
            side_effect=[
                Exception("404 Not Found"),
                {"default_branch": "main"},
                [{"id": 1}],
                {
                    "id": 1,
                    "enforcement": "active",
                    "conditions": {},
                    "rules": [{"type": "required_signatures"}],
                },
            ]
        )
        api.log = AsyncMock()
        api.log.debug = lambda *a, **kw: None
        api._ruleset_applies_to_branch = GitHubAsync._ruleset_applies_to_branch
        result = await GitHubAsync.requires_commit_signatures(
            api, "owner", "repo", "main"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_ruleset_inactive_is_ignored(self):
        api = AsyncMock(spec=GitHubAsync)
        # Call sequence:
        #   1. classic 404
        #   2. repo data
        #   3. rulesets list (contains id)
        #   4. ruleset detail (enforcement=disabled)
        api.get = AsyncMock(
            side_effect=[
                Exception("404 Not Found"),
                {"default_branch": "main"},
                [{"id": 1}],
                {
                    "id": 1,
                    "enforcement": "disabled",
                    "conditions": {},
                    "rules": [{"type": "required_signatures"}],
                },
            ]
        )
        api.log = AsyncMock()
        api.log.debug = lambda *a, **kw: None
        api._ruleset_applies_to_branch = GitHubAsync._ruleset_applies_to_branch
        result = await GitHubAsync.requires_commit_signatures(
            api, "owner", "repo", "main"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_both_apis_error_returns_false(self):
        api = AsyncMock(spec=GitHubAsync)
        api.get = AsyncMock(side_effect=Exception("Server error"))
        api.log = AsyncMock()
        api.log.debug = lambda *a, **kw: None
        result = await GitHubAsync.requires_commit_signatures(
            api, "owner", "repo", "main"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_branch_with_slash_is_url_encoded(self):
        """Branch names containing '/' must be URL-encoded in the REST path."""
        api = AsyncMock(spec=GitHubAsync)
        api.get = AsyncMock(return_value={"enabled": True})
        api.log = AsyncMock()
        api.log.debug = lambda *a, **kw: None
        result = await GitHubAsync.requires_commit_signatures(
            api, "owner", "repo", "release/v1"
        )
        assert result is True
        # The REST call must URL-encode the slash in the branch name
        called_path = api.get.call_args_list[0][0][0]
        assert "release%2Fv1" in called_path
        assert "release/v1" not in called_path


# ---------------------------------------------------------------------------
# _trigger_dependabot_recreate — basic conditions
# ---------------------------------------------------------------------------
class TestTriggerDependabotRecreateConditions:
    """Tests for conditions that gate whether recreate is attempted."""

    @pytest.mark.asyncio
    async def test_non_dependabot_author_returns_none(self):
        mgr, _client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info(author="some-human")
        result = await mgr._trigger_dependabot_recreate(pr)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_github_client_returns_none(self):
        mgr, _client = _make_manager()  # typed mock client pattern (see conftest.py)
        mgr._github_client = None  # intentionally set to None for this test
        pr = _make_pr_info()
        result = await mgr._trigger_dependabot_recreate(pr)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_signature_requirement_returns_none(self):
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info()
        client.requires_commit_signatures = AsyncMock(return_value=False)
        result = await mgr._trigger_dependabot_recreate(pr)
        assert result is None
        client.requires_commit_signatures.assert_called_once_with(
            "lfreleng-actions", "gerrit-clone-action", "main"
        )

    @pytest.mark.asyncio
    async def test_all_commits_verified_returns_none(self):
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info()
        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.check_pr_commit_signatures = AsyncMock(return_value=(True, []))
        result = await mgr._trigger_dependabot_recreate(pr)
        assert result is None

    @pytest.mark.asyncio
    async def test_duplicate_recreate_comment_returns_none(self):
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info()
        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.check_pr_commit_signatures = AsyncMock(
            return_value=(False, ["a4355a87"])
        )
        # Existing comments include a recreate comment
        client.get = AsyncMock(
            return_value=[
                {"body": "@dependabot recreate", "user": {"login": "someuser"}},
            ]
        )
        result = await mgr._trigger_dependabot_recreate(pr)
        assert result is None

    @pytest.mark.asyncio
    async def test_signature_check_error_returns_none(self):
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info()
        client.requires_commit_signatures = AsyncMock(
            side_effect=Exception("API error")
        )
        result = await mgr._trigger_dependabot_recreate(pr)
        assert result is None


# ---------------------------------------------------------------------------
# _trigger_dependabot_recreate — posts comment and polls
# ---------------------------------------------------------------------------
class TestTriggerDependabotRecreateHappyPath:
    """Tests for the happy path where recreate is triggered and succeeds."""

    @pytest.mark.asyncio
    async def test_posts_recreate_and_finds_new_pr(self):
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info()

        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.check_pr_commit_signatures = AsyncMock(
            return_value=(False, ["a4355a87"])
        )
        client.post_issue_comment = AsyncMock()

        # Sequence of get calls:
        # 1. Check for duplicate comments (issue comments)
        # 2-N. Polling: old PR state, then search for new PR
        old_pr_open = {"state": "open", "number": 106}
        old_pr_closed = {"state": "closed", "number": 106}
        new_pr_list = [
            {
                "number": 107,
                "user": {"login": "dependabot[bot]"},
                "html_url": "https://github.com/lfreleng-actions/gerrit-clone-action/pull/107",
                "head": {
                    "ref": "dependabot/github_actions/lfreleng-actions/python-build-action-1.0.4",
                    "sha": "newsha123",
                },
                "base": {"ref": "main"},
            }
        ]
        new_pr_ready = {
            "number": 107,
            "title": "Chore: Bump lfreleng-actions/python-build-action from 1.0.3 to 1.0.4",
            "body": "Recreated PR",
            "user": {"login": "dependabot[bot]"},
            "head": {
                "ref": "dependabot/github_actions/lfreleng-actions/python-build-action-1.0.4",
                "sha": "newsha123",
            },
            "base": {"ref": "main"},
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
            "html_url": "https://github.com/lfreleng-actions/gerrit-clone-action/pull/107",
        }

        call_sequence = [
            # 1. duplicate comment check
            [],
            # 2. poll 1: old PR still open
            old_pr_open,
            # 3. poll 2: old PR closed
            old_pr_closed,
            # 4. search for new PR
            new_pr_list,
            # 5. poll new PR checks (wait_for_recreated_pr_checks)
            new_pr_ready,
        ]
        client.get = AsyncMock(side_effect=call_sequence)
        # Files are now fetched via get_paginated
        client.get_paginated = lambda *a, **kw: _async_gen([])

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr._trigger_dependabot_recreate(pr)

        assert result is not None
        assert result.number == 107
        assert result.mergeable is True
        assert result.mergeable_state == "clean"
        client.post_issue_comment.assert_called_once_with(
            "lfreleng-actions",
            "gerrit-clone-action",
            106,
            "@dependabot recreate",
        )

    @pytest.mark.asyncio
    async def test_post_comment_failure_returns_none(self):
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info()

        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.check_pr_commit_signatures = AsyncMock(
            return_value=(False, ["a4355a87"])
        )
        client.get = AsyncMock(return_value=[])  # no duplicate comments
        client.post_issue_comment = AsyncMock(side_effect=Exception("403 Forbidden"))

        result = await mgr._trigger_dependabot_recreate(pr)
        assert result is None


# ---------------------------------------------------------------------------
# _trigger_dependabot_recreate — timeout scenarios
# ---------------------------------------------------------------------------
class TestTriggerDependabotRecreateTimeout:
    """Tests for timeout scenarios during dependabot recreate."""

    @pytest.mark.asyncio
    async def test_timeout_waiting_for_old_pr_to_close(self):
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info()

        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.check_pr_commit_signatures = AsyncMock(
            return_value=(False, ["a4355a87"])
        )
        client.post_issue_comment = AsyncMock()

        # Always return the old PR as open
        client.get = AsyncMock(
            side_effect=[
                [],  # no duplicate comments
            ]
            + [{"state": "open", "number": 106}] * 36  # all polls: still open
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr._trigger_dependabot_recreate(pr)

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_waiting_for_new_pr_checks(self):
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info()

        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.check_pr_commit_signatures = AsyncMock(
            return_value=(False, ["a4355a87"])
        )
        client.post_issue_comment = AsyncMock()

        old_pr_closed = {"state": "closed", "number": 106}
        new_pr_list = [
            {
                "number": 107,
                "user": {"login": "dependabot[bot]"},
                "html_url": "https://github.com/lfreleng-actions/gerrit-clone-action/pull/107",
                "head": {
                    "ref": "dependabot/github_actions/lfreleng-actions/python-build-action-1.0.4",
                    "sha": "newsha123",
                },
                "base": {"ref": "main"},
            }
        ]
        new_pr_blocked = {
            "number": 107,
            "state": "open",
            "mergeable": None,
            "mergeable_state": "blocked",
            "user": {"login": "dependabot[bot]"},
            "head": {"ref": "dependabot/foo", "sha": "newsha123"},
            "base": {"ref": "main"},
        }

        call_sequence = [
            [],  # no duplicate comments
            old_pr_closed,  # poll 1: old PR closed
            new_pr_list,  # search for new PR
        ] + [new_pr_blocked] * 36  # all check polls: still blocked

        client.get = AsyncMock(side_effect=call_sequence)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr._trigger_dependabot_recreate(pr)

        assert result is None


# ---------------------------------------------------------------------------
# _wait_for_recreated_pr_checks
# ---------------------------------------------------------------------------
class TestWaitForRecreatedPrChecks:
    """Tests for _wait_for_recreated_pr_checks."""

    @pytest.mark.asyncio
    async def test_returns_pr_info_when_clean(self):
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr_data = {
            "number": 107,
            "html_url": "https://github.com/owner/repo/pull/107",
            "user": {"login": "dependabot[bot]"},
            "head": {"ref": "dependabot/foo", "sha": "newsha"},
            "base": {"ref": "main"},
        }
        refreshed = {
            "number": 107,
            "title": "Bump foo",
            "body": "Bump",
            "user": {"login": "dependabot[bot]"},
            "head": {"ref": "dependabot/foo", "sha": "newsha"},
            "base": {"ref": "main"},
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
            "html_url": "https://github.com/owner/repo/pull/107",
        }
        client.get = AsyncMock(return_value=refreshed)
        # Files are now fetched via get_paginated
        client.get_paginated = lambda *a, **kw: _async_gen([])

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr._wait_for_recreated_pr_checks(
                "owner", "repo", 107, pr_data
            )

        assert result is not None
        assert result.number == 107
        assert result.mergeable_state == "clean"

    @pytest.mark.asyncio
    async def test_returns_none_on_dirty(self):
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr_data = {
            "number": 107,
            "html_url": "https://github.com/owner/repo/pull/107",
        }
        dirty_pr = {
            "number": 107,
            "mergeable": False,
            "mergeable_state": "dirty",
        }
        client.get = AsyncMock(return_value=dirty_pr)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr._wait_for_recreated_pr_checks(
                "owner", "repo", 107, pr_data
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_without_client(self):
        mgr, _client = _make_manager()  # typed mock client pattern (see conftest.py)
        mgr._github_client = None  # intentionally set to None for this test
        result = await mgr._wait_for_recreated_pr_checks("owner", "repo", 107, {})
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_pr_info_on_unstable_with_mergeable_true(self):
        """unstable + mergeable=True should still be accepted."""
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr_data = {
            "number": 107,
            "html_url": "https://github.com/owner/repo/pull/107",
        }
        unstable_pr = {
            "number": 107,
            "title": "Bump foo",
            "body": None,
            "user": {"login": "dependabot[bot]"},
            "head": {"ref": "dependabot/foo", "sha": "newsha"},
            "base": {"ref": "main"},
            "state": "open",
            "mergeable": True,
            "mergeable_state": "unstable",
            "html_url": "https://github.com/owner/repo/pull/107",
        }
        client.get = AsyncMock(return_value=unstable_pr)
        # Files are now fetched via get_paginated
        client.get_paginated = lambda *a, **kw: _async_gen([])

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr._wait_for_recreated_pr_checks(
                "owner", "repo", 107, pr_data
            )

        assert result is not None
        assert result.number == 107


# ---------------------------------------------------------------------------
# Integration with _merge_single_pr — recreate fallback on merge failure
# ---------------------------------------------------------------------------
class TestMergeSinglePrRecreateIntegration:
    """Tests that _merge_single_pr invokes dependabot recreate on failure."""

    @pytest.mark.asyncio
    async def test_recreate_triggered_on_branch_protection_failure(self):
        """When a dependabot merge fails due to branch protection and
        the commit is unsigned, the recreate path should be triggered."""
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info(mergeable=True, mergeable_state="blocked")

        # Make the PR pass initial checks but fail the merge
        client.get_branch_protection = AsyncMock(return_value={})
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by branch protection (requires approval)"
        )
        # get() calls during _test_merge_capability etc.
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "blocked",
                "head": {"sha": "a4355a87"},
                "state": "open",
            }
        )
        client.approve_pull_request = AsyncMock()
        client.merge_pull_request = AsyncMock(return_value=False)
        client.check_user_can_bypass_protection = AsyncMock(
            return_value=(False, "no bypass")
        )

        # Set up a mock GitHubService
        mock_service = AsyncMock()
        mock_service.get_branch_protection_settings = AsyncMock(return_value={})
        mock_service.determine_merge_method = lambda protection, default: default
        mgr._github_service = mock_service

        # Patch _trigger_dependabot_recreate to verify it's called
        recreated_pr = _make_pr_info(
            number=107,
            html_url="https://github.com/lfreleng-actions/gerrit-clone-action/pull/107",
            head_sha="newsha123",
            mergeable=True,
            mergeable_state="clean",
        )
        # Need to mock _get_failure_summary to return branch protection error
        with (
            patch.object(
                mgr,
                "_trigger_dependabot_recreate",
                new_callable=AsyncMock,
                return_value=recreated_pr,
            ) as mock_recreate,
            patch.object(
                mgr,
                "_get_failure_summary",
                return_value="branch protection rules prevent merge",
            ),
        ):
            # Make the new PR merge succeed
            client.merge_pull_request = AsyncMock(side_effect=[False, True])

            result = await mgr._merge_single_pr(pr)

        # The recreate should have been attempted
        mock_recreate.assert_called_once_with(pr)
        assert result.status == MergeStatus.MERGED
        assert result.pr_info.number == 107

    @pytest.mark.asyncio
    async def test_no_recreate_for_non_dependabot(self):
        """Non-dependabot PRs should not trigger the recreate path."""
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info(
            author="some-human", mergeable=True, mergeable_state="blocked"
        )

        client.get_branch_protection = AsyncMock(return_value={})
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by branch protection (requires approval)"
        )
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "blocked",
                "head": {"sha": "abc123"},
                "state": "open",
            }
        )
        client.approve_pull_request = AsyncMock()
        client.merge_pull_request = AsyncMock(return_value=False)
        client.check_user_can_bypass_protection = AsyncMock(
            return_value=(False, "no bypass")
        )

        mock_service = AsyncMock()
        mock_service.get_branch_protection_settings = AsyncMock(return_value={})
        mock_service.determine_merge_method = lambda protection, default: default
        mgr._github_service = mock_service

        with patch.object(
            mgr,
            "_trigger_dependabot_recreate",
            new_callable=AsyncMock,
        ) as mock_recreate:
            result = await mgr._merge_single_pr(pr)

        # Should NOT trigger recreate for non-dependabot PRs
        mock_recreate.assert_not_called()
        assert result.status == MergeStatus.FAILED

    @pytest.mark.asyncio
    async def test_no_recreate_in_preview_mode(self):
        """Preview mode should not trigger the recreate path."""
        mgr, client = _make_manager(
            preview_mode=True
        )  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info(mergeable=True, mergeable_state="blocked")

        client.get_branch_protection = AsyncMock(return_value={})
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by branch protection (requires approval)"
        )
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "blocked",
                "head": {"sha": "abc123"},
                "state": "open",
            }
        )
        client.check_user_can_bypass_protection = AsyncMock(
            return_value=(False, "no bypass")
        )

        mock_service = AsyncMock()
        mock_service.get_branch_protection_settings = AsyncMock(return_value={})
        mock_service.determine_merge_method = lambda protection, default: default
        mgr._github_service = mock_service

        with patch.object(
            mgr,
            "_trigger_dependabot_recreate",
            new_callable=AsyncMock,
        ) as mock_recreate:
            _ = await mgr._merge_single_pr(pr)

        # Preview mode doesn't actually merge, so recreate should not be called
        mock_recreate.assert_not_called()

    @pytest.mark.asyncio
    async def test_recreate_returns_none_falls_through_to_failure(self):
        """When recreate returns None, the original failure path is followed."""
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info(mergeable=True, mergeable_state="blocked")

        client.get_branch_protection = AsyncMock(return_value={})
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by branch protection (requires approval)"
        )
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "blocked",
                "head": {"sha": "abc123"},
                "state": "open",
            }
        )
        client.approve_pull_request = AsyncMock()
        client.merge_pull_request = AsyncMock(return_value=False)
        client.check_user_can_bypass_protection = AsyncMock(
            return_value=(False, "no bypass")
        )

        mock_service = AsyncMock()
        mock_service.get_branch_protection_settings = AsyncMock(return_value={})
        mock_service.determine_merge_method = lambda protection, default: default
        mgr._github_service = mock_service

        with (
            patch.object(
                mgr,
                "_trigger_dependabot_recreate",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                mgr,
                "_get_failure_summary",
                return_value="branch protection rules prevent merge",
            ),
        ):
            result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.FAILED
        assert "Failed to merge" in (result.error or "")

    @pytest.mark.asyncio
    async def test_recreate_new_pr_merge_fails(self):
        """When the recreated PR also fails to merge, report failure."""
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info(mergeable=True, mergeable_state="blocked")

        client.get_branch_protection = AsyncMock(return_value={})
        client.analyze_block_reason = AsyncMock(
            return_value="Blocked by branch protection (requires approval)"
        )
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "blocked",
                "head": {"sha": "abc123"},
                "state": "open",
            }
        )
        client.approve_pull_request = AsyncMock()
        # All merge attempts fail
        client.merge_pull_request = AsyncMock(return_value=False)
        client.check_user_can_bypass_protection = AsyncMock(
            return_value=(False, "no bypass")
        )

        mock_service = AsyncMock()
        mock_service.get_branch_protection_settings = AsyncMock(return_value={})
        mock_service.determine_merge_method = lambda protection, default: default
        mgr._github_service = mock_service

        recreated_pr = _make_pr_info(
            number=107,
            html_url="https://github.com/lfreleng-actions/gerrit-clone-action/pull/107",
            head_sha="newsha123",
            mergeable=True,
            mergeable_state="clean",
        )

        with (
            patch.object(
                mgr,
                "_trigger_dependabot_recreate",
                new_callable=AsyncMock,
                return_value=recreated_pr,
            ),
            patch.object(
                mgr,
                "_get_failure_summary",
                return_value="branch protection rules prevent merge",
            ),
        ):
            result = await mgr._merge_single_pr(pr)

        assert result.status == MergeStatus.FAILED
        assert "recreated" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# New PR discovery — edge cases
# ---------------------------------------------------------------------------
class TestNewPrDiscoveryEdgeCases:
    """Tests for edge cases when searching for the recreated PR."""

    @pytest.mark.asyncio
    async def test_ignores_prs_from_other_authors(self):
        """Only dependabot PRs should be considered as replacements."""
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info()

        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.check_pr_commit_signatures = AsyncMock(
            return_value=(False, ["a4355a87"])
        )
        client.post_issue_comment = AsyncMock()

        old_pr_closed = {"state": "closed", "number": 106}
        # New PR from a different author
        new_pr_list = [
            {
                "number": 200,
                "user": {"login": "some-human"},
                "html_url": "https://github.com/lfreleng-actions/gerrit-clone-action/pull/200",
                "head": {
                    "ref": "dependabot/github_actions/lfreleng-actions/python-build-action-1.0.4",
                    "sha": "othsha",
                },
                "base": {"ref": "main"},
            }
        ]

        # The PR closes quickly but no valid replacement found
        call_sequence = [
            [],  # no duplicate comments
            old_pr_closed,  # poll 1: old PR closed
        ] + [new_pr_list] * 35  # keep finding wrong-author PRs

        client.get = AsyncMock(side_effect=call_sequence)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr._trigger_dependabot_recreate(pr)

        assert result is None

    @pytest.mark.asyncio
    async def test_ignores_same_pr_number(self):
        """The old PR number should not be considered as a replacement."""
        mgr, client = _make_manager()  # typed mock client pattern (see conftest.py)
        pr = _make_pr_info()

        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.check_pr_commit_signatures = AsyncMock(
            return_value=(False, ["a4355a87"])
        )
        client.post_issue_comment = AsyncMock()

        old_pr_closed = {"state": "closed", "number": 106}
        # Same PR number appearing in the open PR list (shouldn't happen but edge case)
        new_pr_list = [
            {
                "number": 106,
                "user": {"login": "dependabot[bot]"},
                "html_url": "https://github.com/lfreleng-actions/gerrit-clone-action/pull/106",
                "head": {
                    "ref": "dependabot/github_actions/lfreleng-actions/python-build-action-1.0.4",
                    "sha": "a4355a87",
                },
                "base": {"ref": "main"},
            }
        ]

        call_sequence = [
            [],  # no duplicate comments
            old_pr_closed,  # poll 1: old PR closed
        ] + [new_pr_list] * 35

        client.get = AsyncMock(side_effect=call_sequence)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr._trigger_dependabot_recreate(pr)

        assert result is None


# ---------------------------------------------------------------------------
# GraphQL query includes requiresCommitSignatures
# ---------------------------------------------------------------------------
class TestGraphQLQueryIncludesSignatures:
    """Verify the GraphQL query was updated."""

    def test_query_contains_requires_commit_signatures(self):
        from dependamerge.github_graphql import GET_BRANCH_PROTECTION

        assert "requiresCommitSignatures" in GET_BRANCH_PROTECTION
