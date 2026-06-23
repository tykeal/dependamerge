# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for token-scope introspection, the workflow-scope pre-flight
check, the scope-aware workflow-error classifier, and the ruleset-aware
``analyze_block_reason`` fallback.

These cover the behaviour added so the tool no longer:

* reports a merge failure as "missing workflow scope" when the token
  actually carries that scope, and
* asserts "branch protection" for a blocked PR when the real guard is a
  repository ruleset (or no detectable rule at all).
"""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from dependamerge.cli import _MergeContext, _source_pr_modifies_workflows
from dependamerge.github_async import GitHubAsync
from dependamerge.github_async import PermissionError as GitHubPermissionError
from dependamerge.models import FileChange, PullRequestInfo


def _make_api() -> GitHubAsync:
    """Construct a client with a dummy token (no network is performed)."""
    return GitHubAsync(token="test_token")


class _FakeWorkflowResponse:
    """Minimal stand-in for an httpx response carrying a workflow 403 body."""

    status_code = 403
    text = (
        "refusing to allow a Personal Access Token to create or update "
        "workflow `.github/workflows/build.yml` without `workflow` scope"
    )


class _WorkflowForbidden(Exception):
    """403 error whose response body is GitHub's workflow-scope refusal."""

    def __init__(self) -> None:
        super().__init__("403 Client Error: Forbidden")
        self.response = _FakeWorkflowResponse()


class TestTokenScopes:
    """``get_token_scopes`` / ``check_workflow_scope`` introspection."""

    @pytest.mark.asyncio
    async def test_classic_token_scopes_parsed_from_header(self):
        api = _make_api()
        resp = Mock()
        resp.headers = {"X-OAuth-Scopes": "repo, workflow, admin:org"}
        api._request = AsyncMock(return_value=resp)

        scopes = await api.get_token_scopes()

        assert scopes == {"repo", "workflow", "admin:org"}
        # Cached: a second call must not hit the network again.
        await api.get_token_scopes()
        api._request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fine_grained_token_returns_none(self):
        api = _make_api()
        resp = Mock()
        resp.headers = {}  # fine-grained / app tokens omit the header
        api._request = AsyncMock(return_value=resp)

        assert await api.get_token_scopes() is None
        assert await api.check_workflow_scope() is None

    @pytest.mark.asyncio
    async def test_check_workflow_scope_true_and_false(self):
        api = _make_api()
        resp = Mock()
        resp.headers = {"X-OAuth-Scopes": "repo, workflow"}
        api._request = AsyncMock(return_value=resp)
        assert await api.check_workflow_scope() is True

        api2 = _make_api()
        resp2 = Mock()
        resp2.headers = {"X-OAuth-Scopes": "repo"}
        api2._request = AsyncMock(return_value=resp2)
        assert await api2.check_workflow_scope() is False

    @pytest.mark.asyncio
    async def test_probe_failure_is_undeterminable(self):
        api = _make_api()
        api._request = AsyncMock(side_effect=RuntimeError("network down"))
        assert await api.get_token_scopes() is None


class TestWorkflowPreflight:
    """``check_token_permissions`` handling of the ``merge_workflow`` op."""

    @pytest.mark.asyncio
    async def test_missing_workflow_scope_blocks(self):
        api = _make_api()
        api.check_workflow_scope = AsyncMock(return_value=False)

        results = await api.check_token_permissions(["merge_workflow"])

        assert results["merge_workflow"]["has_permission"] is False
        assert "workflow" in results["merge_workflow"]["error"].lower()
        assert results["merge_workflow"]["guidance"]["classic"]

    @pytest.mark.asyncio
    async def test_present_workflow_scope_passes(self):
        api = _make_api()
        api.check_workflow_scope = AsyncMock(return_value=True)
        results = await api.check_token_permissions(["merge_workflow"])
        assert results["merge_workflow"]["has_permission"] is True

    @pytest.mark.asyncio
    async def test_undeterminable_scope_passes(self):
        # Fine-grained tokens cannot be checked up-front; never block them.
        api = _make_api()
        api.check_workflow_scope = AsyncMock(return_value=None)
        results = await api.check_token_permissions(["merge_workflow"])
        assert results["merge_workflow"]["has_permission"] is True


class TestScopeAwareMergeClassifier:
    """``merge_pull_request`` must not blame a scope the token already has."""

    @pytest.mark.asyncio
    async def test_workflow_403_without_scope_keeps_scope_guidance(self):
        api = _make_api()
        api.put = AsyncMock(side_effect=_WorkflowForbidden())
        api.check_workflow_scope = AsyncMock(return_value=False)

        with pytest.raises(GitHubPermissionError) as exc_info:
            await api.merge_pull_request("owner", "repo", 1)

        assert exc_info.value.operation == "merge_workflow"

    @pytest.mark.asyncio
    async def test_workflow_403_with_scope_is_reclassified(self):
        api = _make_api()
        api.put = AsyncMock(side_effect=_WorkflowForbidden())
        # Token DOES carry the workflow scope, so the refusal is not a
        # scope problem — it must be reclassified, not repeated verbatim.
        api.check_workflow_scope = AsyncMock(return_value=True)

        with pytest.raises(GitHubPermissionError) as exc_info:
            await api.merge_pull_request("owner", "repo", 1)

        err = exc_info.value
        assert err.operation == "merge_workflow_restricted"
        assert "already has the 'workflow' scope" in str(err)
        assert "ruleset" in err.token_type_guidance["classic"].lower()


def _pr_with_files(*filenames: str) -> PullRequestInfo:
    """Build a minimal PR whose changed files are *filenames*."""
    return PullRequestInfo(
        number=1,
        title="Bump action",
        body=None,
        author="dependabot[bot]",
        head_sha="abc",
        base_branch="main",
        head_branch="dependabot/x",
        state="open",
        mergeable=True,
        mergeable_state="blocked",
        behind_by=0,
        files_changed=[
            FileChange(
                filename=name,
                additions=1,
                deletions=1,
                changes=2,
                status="modified",
            )
            for name in filenames
        ],
        repository_full_name="owner/repo",
        html_url="https://github.com/owner/repo/pull/1",
    )


class TestSourcePrModifiesWorkflows:
    """CLI detection of workflow-file changes on the source PR."""

    def test_detects_workflow_yaml(self):
        ctx = SimpleNamespace(source_pr=_pr_with_files(".github/workflows/build.yaml"))
        assert _source_pr_modifies_workflows(cast(_MergeContext, ctx)) is True

    def test_detects_workflow_yml(self):
        ctx = SimpleNamespace(
            source_pr=_pr_with_files("README.md", ".github/workflows/ci.yml")
        )
        assert _source_pr_modifies_workflows(cast(_MergeContext, ctx)) is True

    def test_ignores_non_workflow_changes(self):
        ctx = SimpleNamespace(
            source_pr=_pr_with_files("src/app.py", ".github/dependabot.yml")
        )
        assert _source_pr_modifies_workflows(cast(_MergeContext, ctx)) is False

    def test_handles_missing_source_pr(self):
        ctx = SimpleNamespace(source_pr=None)
        assert _source_pr_modifies_workflows(cast(_MergeContext, ctx)) is False


class TestDetectBranchProtectionKind:
    """``_detect_branch_protection_kind`` classification."""

    @pytest.mark.asyncio
    async def test_ruleset_detected(self):
        api = _make_api()

        async def fake_get(path, params=None):
            if "/rules/branches/" in path:
                return [{"type": "pull_request"}]
            raise AssertionError(f"unexpected path {path}")

        api.get = fake_get
        assert await api._detect_branch_protection_kind("o", "r", "main") == "ruleset"

    @pytest.mark.asyncio
    async def test_classic_protection_detected(self):
        api = _make_api()

        async def fake_get(path, params=None):
            if "/rules/branches/" in path:
                return []
            if path.endswith("/protection"):
                return {"required_status_checks": {}}
            raise AssertionError(f"unexpected path {path}")

        api.get = fake_get
        assert (
            await api._detect_branch_protection_kind("o", "r", "main") == "protection"
        )

    @pytest.mark.asyncio
    async def test_none_when_unguarded(self):
        api = _make_api()

        async def fake_get(path, params=None):
            if "/rules/branches/" in path:
                return []
            if path.endswith("/protection"):
                raise RuntimeError("404 Not Found: Branch not protected")
            raise AssertionError(f"unexpected path {path}")

        api.get = fake_get
        assert await api._detect_branch_protection_kind("o", "r", "main") == "none"


class TestAnalyzeBlockReasonFallback:
    """The catch-all fallback must be ruleset-aware and non-asserting."""

    def _wire_clean_approved_pr(self, api: GitHubAsync, kind_rules):
        """Mock a PR that is approved, all checks green, no changes
        requested — so analysis reaches the final fallback — and let the
        caller choose what ``/rules/branches`` and ``/protection`` return.
        """
        api.get_required_status_checks = AsyncMock(return_value=[])  # type: ignore[method-assign]

        async def fake_get(path, params=None):
            if path.endswith("/reviews"):
                return [{"state": "APPROVED", "user": {"login": "reviewer"}}]
            if path.endswith("/comments"):
                return []
            if "/check-runs" in path:
                return {"check_runs": []}
            if path.endswith("/status"):
                return {"statuses": []}
            if "/rules/branches/" in path:
                return kind_rules["rules"]
            if path.endswith("/protection"):
                if kind_rules["protection"]:
                    return {"required_status_checks": {}}
                raise RuntimeError("404 Not Found")
            if "/pulls/" in path:
                return {"base": {"ref": "main"}}
            return {}

        api.get = fake_get  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_fallback_reports_ruleset(self):
        api = _make_api()
        self._wire_clean_approved_pr(
            api, {"rules": [{"type": "required_signatures"}], "protection": False}
        )
        reason = await api.analyze_block_reason("o", "r", 1, "deadbeef")
        assert reason == (
            "Blocked by repository ruleset (no specific failing condition detected)"
        )

    @pytest.mark.asyncio
    async def test_fallback_reports_undetermined_when_unguarded(self):
        api = _make_api()
        self._wire_clean_approved_pr(api, {"rules": [], "protection": False})
        reason = await api.analyze_block_reason("o", "r", 1, "deadbeef")
        assert reason.startswith("Blocked for an undetermined reason")

    @pytest.mark.asyncio
    async def test_requires_approval_still_specific(self):
        api = _make_api()
        api.get_required_status_checks = AsyncMock(return_value=[])

        async def fake_get(path, params=None):
            if path.endswith("/reviews"):
                return []  # not approved
            if path.endswith("/comments"):
                return []
            if "/check-runs" in path:
                return {"check_runs": []}
            if path.endswith("/status"):
                return {"statuses": []}
            if "/pulls/" in path:
                return {"base": {"ref": "main"}}
            return {}

        api.get = fake_get
        reason = await api.analyze_block_reason("o", "r", 1, "deadbeef")
        assert reason == "Blocked by branch protection (requires approval)"
