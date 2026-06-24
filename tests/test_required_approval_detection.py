# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for proactive required-approval detection.

Some organizations enforce a repository ruleset that mandates an approving
review before *any* merge (e.g. the ``lfreleng-actions`` "Base Protections"
ruleset).  Under "merge first, approve on demand" every such PR would incur
a guaranteed-to-fail merge attempt before recovery.  The merge manager now
detects the requirement **org-first** — enumerating the org's rulesets once
and evaluating their conditions locally — and approves proactively, falling
back to GitHub's authoritative per-repo ``rules/branches`` endpoint only for
conditions it cannot evaluate locally.

These tests cover the pure condition-matching helpers, the org enumeration,
the org-first resolution (including its caching and fallback), and the
reactive merge-error approval signal.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from dependamerge.models import PullRequestInfo
from tests.conftest import make_merge_manager

# A faithful copy of the real lfreleng-actions "Base Protections" ruleset:
# requires one approving review, applies to every repo *except*
# ``project-reporting-artifacts``, and only on the default branch.
_BASE_PROTECTIONS_DETAIL: dict[str, Any] = {
    "id": 4129117,
    "name": "Base Protections",
    "target": "branch",
    "enforcement": "active",
    "conditions": {
        "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
        "repository_name": {
            "include": ["*"],
            "exclude": ["project-reporting-artifacts"],
        },
    },
    "rules": [
        {
            "type": "pull_request",
            "parameters": {"required_approving_review_count": 1},
        },
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "DCO"}]},
        },
    ],
}

_RULESETS_LIST = [
    {
        "id": 4129117,
        "name": "Base Protections",
        "target": "branch",
        "enforcement": "active",
    },
    {
        "id": 17275268,
        "name": "Mandatory workflows",
        "target": "branch",
        "enforcement": "active",
    },
]

# "Mandatory workflows" carries no pull_request approval rule.
_MANDATORY_WORKFLOWS_DETAIL = {
    "id": 17275268,
    "name": "Mandatory workflows",
    "target": "branch",
    "enforcement": "active",
    "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
    "rules": [{"type": "workflows", "parameters": {}}],
}


def _org_get_router(*, list_payload=_RULESETS_LIST, details=None):
    """Build an AsyncMock side_effect routing org ruleset GETs by URL.

    The list endpoint is fetched page by page (``?per_page=...&page=N``);
    the whole payload is returned for page 1 and an empty list for any
    later page, so the manager's pagination loop terminates.
    """
    details = details or {
        4129117: _BASE_PROTECTIONS_DETAIL,
        17275268: _MANDATORY_WORKFLOWS_DETAIL,
    }

    async def _get(path: str):
        base, _, query = path.partition("?")
        if base.endswith("/rulesets"):
            page = 1
            for part in query.split("&"):
                if part.startswith("page="):
                    page = int(part.split("=", 1)[1])
            return list_payload if page == 1 else []
        for rid, detail in details.items():
            if base.endswith(f"/rulesets/{rid}"):
                return detail
        raise AssertionError(f"unexpected GET {path}")

    return _get


# ---------------------------------------------------------------------------
# _rules_require_approval
# ---------------------------------------------------------------------------


class TestRulesRequireApproval:
    def test_detects_pull_request_rule_with_count(self) -> None:
        mgr, _ = make_merge_manager()
        rules = [
            {
                "type": "pull_request",
                "parameters": {"required_approving_review_count": 1},
            }
        ]
        assert mgr._rules_require_approval(rules) is True

    def test_zero_required_reviews_is_not_a_requirement(self) -> None:
        mgr, _ = make_merge_manager()
        rules = [
            {
                "type": "pull_request",
                "parameters": {"required_approving_review_count": 0},
            }
        ]
        assert mgr._rules_require_approval(rules) is False

    def test_no_pull_request_rule(self) -> None:
        mgr, _ = make_merge_manager()
        rules = [{"type": "required_status_checks", "parameters": {}}]
        assert mgr._rules_require_approval(rules) is False

    def test_pull_request_rule_without_params_assumes_requirement(self) -> None:
        mgr, _ = make_merge_manager()
        assert mgr._rules_require_approval([{"type": "pull_request"}]) is True

    def test_non_list_input(self) -> None:
        mgr, _ = make_merge_manager()
        assert mgr._rules_require_approval(None) is False
        assert mgr._rules_require_approval({}) is False


# ---------------------------------------------------------------------------
# Condition matching helpers
# ---------------------------------------------------------------------------


class TestConditionMatching:
    def test_repository_name_include_all_exclude_one(self) -> None:
        mgr, _ = make_merge_manager()
        include = ["*"]
        exclude = ["project-reporting-artifacts"]
        assert mgr._ruleset_name_matches("lftools-uv", include, exclude) is True
        assert (
            mgr._ruleset_name_matches("project-reporting-artifacts", include, exclude)
            is False
        )

    def test_repository_name_all_sentinel(self) -> None:
        mgr, _ = make_merge_manager()
        assert mgr._ruleset_name_matches("anything", ["~ALL"], []) is True

    def test_repository_name_empty_include_matches_nothing(self) -> None:
        mgr, _ = make_merge_manager()
        assert mgr._ruleset_name_matches("repo", [], []) is False

    def test_ref_default_branch_is_in_scope(self) -> None:
        mgr, _ = make_merge_manager()
        assert mgr._ruleset_ref_matches("main", ["~DEFAULT_BRANCH"], []) is True

    def test_ref_all_sentinel(self) -> None:
        mgr, _ = make_merge_manager()
        assert mgr._ruleset_ref_matches("any-branch", ["~ALL"], []) is True

    def test_ref_explicit_pattern(self) -> None:
        mgr, _ = make_merge_manager()
        assert (
            mgr._ruleset_ref_matches("release/1.2", ["refs/heads/release/*"], [])
            is True
        )
        assert (
            mgr._ruleset_ref_matches("feature/x", ["refs/heads/release/*"], []) is False
        )

    def test_ref_exclude_wins(self) -> None:
        mgr, _ = make_merge_manager()
        assert mgr._ruleset_ref_matches("main", ["~ALL"], ["refs/heads/main"]) is False

    def test_ref_empty_include_is_undecidable(self) -> None:
        mgr, _ = make_merge_manager()
        assert mgr._ruleset_ref_matches("main", [], []) is None

    def test_condition_applies_for_governed_repo_branch(self) -> None:
        mgr, _ = make_merge_manager()
        conditions = _BASE_PROTECTIONS_DETAIL["conditions"]
        assert mgr._ruleset_condition_applies(conditions, "lftools-uv", "main") is True

    def test_condition_excludes_listed_repo(self) -> None:
        mgr, _ = make_merge_manager()
        conditions = _BASE_PROTECTIONS_DETAIL["conditions"]
        assert (
            mgr._ruleset_condition_applies(
                conditions, "project-reporting-artifacts", "main"
            )
            is False
        )

    def test_unknown_condition_type_is_undecidable(self) -> None:
        mgr, _ = make_merge_manager()
        conditions: dict[str, Any] = {"repository_property": {"include": []}}
        assert mgr._ruleset_condition_applies(conditions, "repo", "main") is None


# ---------------------------------------------------------------------------
# _org_approval_rulesets
# ---------------------------------------------------------------------------


class TestOrgApprovalRulesets:
    @pytest.mark.asyncio
    async def test_enumerates_only_approval_rulesets(self) -> None:
        mgr, client = make_merge_manager()
        client.get = AsyncMock(side_effect=_org_get_router())

        result = await mgr._org_approval_rulesets("lfreleng-actions")

        assert result is not None
        names = {r["name"] for r in result}
        assert names == {"Base Protections"}

    @pytest.mark.asyncio
    async def test_result_is_cached(self) -> None:
        mgr, client = make_merge_manager()
        client.get = AsyncMock(side_effect=_org_get_router())

        await mgr._org_approval_rulesets("lfreleng-actions")
        calls_after_first = client.get.await_count
        await mgr._org_approval_rulesets("lfreleng-actions")

        assert client.get.await_count == calls_after_first

    @pytest.mark.asyncio
    async def test_skips_inactive_rulesets(self) -> None:
        mgr, client = make_merge_manager()
        inactive = [
            {
                "id": 4129117,
                "name": "Base Protections",
                "target": "branch",
                "enforcement": "evaluate",
            }
        ]
        client.get = AsyncMock(side_effect=_org_get_router(list_payload=inactive))

        result = await mgr._org_approval_rulesets("lfreleng-actions")

        assert result == []

    @pytest.mark.asyncio
    async def test_enumeration_failure_returns_none(self) -> None:
        mgr, client = make_merge_manager()
        client.get = AsyncMock(side_effect=RuntimeError("403 Forbidden"))

        result = await mgr._org_approval_rulesets("lfreleng-actions")

        assert result is None

    @pytest.mark.asyncio
    async def test_paginates_past_first_page(self) -> None:
        """An approval ruleset beyond the first page must still be found.

        The list endpoint is paginated (the manager requests pages of 100
        and stops on the first short page).  Return a full first page of
        inactive filler rulesets to force a second request, and place the
        approval-mandating ruleset on page two; it must not be dropped.
        """
        mgr, client = make_merge_manager()
        # A full page (== the manager's per_page of 100) forces another
        # request.  Inactive rulesets are skipped before any detail fetch.
        page_one = [
            {
                "id": 900000 + i,
                "name": f"Filler {i}",
                "target": "branch",
                "enforcement": "disabled",
            }
            for i in range(100)
        ]
        page_two = [
            {
                "id": 4129117,
                "name": "Base Protections",
                "target": "branch",
                "enforcement": "active",
            }
        ]

        async def _get(path: str):
            base, _, query = path.partition("?")
            if base.endswith("/rulesets"):
                page = 1
                for part in query.split("&"):
                    if part.startswith("page="):
                        page = int(part.split("=", 1)[1])
                return {1: page_one, 2: page_two}.get(page, [])
            if base.endswith("/rulesets/4129117"):
                return _BASE_PROTECTIONS_DETAIL
            raise AssertionError(f"unexpected GET {path}")

        client.get = AsyncMock(side_effect=_get)

        result = await mgr._org_approval_rulesets("lfreleng-actions")

        assert result is not None
        names = {r["name"] for r in result}
        assert names == {"Base Protections"}


# ---------------------------------------------------------------------------
# _branch_requires_approval (org-first resolution)
# ---------------------------------------------------------------------------


class TestBranchRequiresApproval:
    @pytest.mark.asyncio
    async def test_governed_repo_resolved_without_per_repo_call(self) -> None:
        """A locally-decidable org ruleset must not hit the per-repo endpoint."""
        mgr, client = make_merge_manager()
        client.get = AsyncMock(side_effect=_org_get_router())

        with patch.object(
            mgr, "_effective_branch_requires_approval", new_callable=AsyncMock
        ) as mock_effective:
            requires = await mgr._branch_requires_approval(
                "lfreleng-actions", "lftools-uv", "main"
            )

        assert requires is True
        mock_effective.assert_not_called()

    @pytest.mark.asyncio
    async def test_excluded_repo_is_not_required(self) -> None:
        mgr, client = make_merge_manager()
        client.get = AsyncMock(side_effect=_org_get_router())

        requires = await mgr._branch_requires_approval(
            "lfreleng-actions", "project-reporting-artifacts", "main"
        )

        assert requires is False

    @pytest.mark.asyncio
    async def test_org_without_approval_ruleset_skips_per_repo_call(self) -> None:
        mgr, client = make_merge_manager()
        client.get = AsyncMock(
            side_effect=_org_get_router(
                list_payload=[
                    {
                        "id": 17275268,
                        "name": "Mandatory workflows",
                        "target": "branch",
                        "enforcement": "active",
                    }
                ]
            )
        )

        with patch.object(
            mgr, "_effective_branch_requires_approval", new_callable=AsyncMock
        ) as mock_effective:
            requires = await mgr._branch_requires_approval(
                "lfreleng-actions", "lftools-uv", "main"
            )

        assert requires is False
        mock_effective.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_when_enumeration_fails(self) -> None:
        mgr, client = make_merge_manager()
        client.get = AsyncMock(side_effect=RuntimeError("403 Forbidden"))

        with patch.object(
            mgr,
            "_effective_branch_requires_approval",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_effective:
            requires = await mgr._branch_requires_approval(
                "lfreleng-actions", "lftools-uv", "main"
            )

        assert requires is True
        mock_effective.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_falls_back_for_unknown_condition(self) -> None:
        mgr, client = make_merge_manager()
        detail = {
            "id": 99,
            "name": "Property-scoped",
            "target": "branch",
            "enforcement": "active",
            "conditions": {"repository_property": {"include": []}},
            "rules": [
                {
                    "type": "pull_request",
                    "parameters": {"required_approving_review_count": 1},
                }
            ],
        }
        client.get = AsyncMock(
            side_effect=_org_get_router(
                list_payload=[
                    {
                        "id": 99,
                        "name": "Property-scoped",
                        "target": "branch",
                        "enforcement": "active",
                    }
                ],
                details={99: detail},
            )
        )

        with patch.object(
            mgr,
            "_effective_branch_requires_approval",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_effective:
            requires = await mgr._branch_requires_approval(
                "lfreleng-actions", "lftools-uv", "main"
            )

        assert requires is True
        mock_effective.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_verdict_is_cached_per_repo_branch(self) -> None:
        mgr, client = make_merge_manager()
        client.get = AsyncMock(side_effect=_org_get_router())

        await mgr._branch_requires_approval("lfreleng-actions", "lftools-uv", "main")
        calls = client.get.await_count
        await mgr._branch_requires_approval("lfreleng-actions", "lftools-uv", "main")

        assert client.get.await_count == calls


# ---------------------------------------------------------------------------
# _effective_branch_requires_approval (authoritative per-repo fallback)
# ---------------------------------------------------------------------------


class TestEffectiveBranchRequiresApproval:
    @pytest.mark.asyncio
    async def test_branch_name_is_url_encoded(self) -> None:
        """A branch with "/" must be encoded so the endpoint resolves.

        An unencoded "release/v1" would split the REST path and 404,
        which the method would misread as "no rules" and skip a required
        proactive approval.
        """
        mgr, client = make_merge_manager()
        seen: list[str] = []

        async def _get(path: str):
            seen.append(path)
            return [
                {
                    "type": "pull_request",
                    "parameters": {"required_approving_review_count": 1},
                }
            ]

        client.get = AsyncMock(side_effect=_get)

        requires = await mgr._effective_branch_requires_approval(
            "lfreleng-actions", "lftools-uv", "release/v1"
        )

        assert requires is True
        assert seen == [
            "/repos/lfreleng-actions/lftools-uv/rules/branches/release%2Fv1"
        ]


# ---------------------------------------------------------------------------
# _approve_if_review_mandated (proactive approval wiring)
# ---------------------------------------------------------------------------


_PR = PullRequestInfo(
    number=484,
    node_id="PR_kwDOTest484",
    title="Bump boto3",
    body="Dependabot PR",
    author="dependabot[bot]",
    head_sha="abc123",
    base_branch="main",
    head_branch="dependabot/pip/boto3",
    state="open",
    mergeable=True,
    mergeable_state="blocked",
    behind_by=0,
    files_changed=[],
    repository_full_name="lfreleng-actions/lftools-uv",
    html_url="https://github.com/lfreleng-actions/lftools-uv/pull/484",
    reviews=[],
    review_comments=[],
)


class TestApproveIfReviewMandated:
    @pytest.mark.asyncio
    async def test_approves_when_branch_requires_review(self) -> None:
        mgr, _ = make_merge_manager()
        with (
            patch.object(
                mgr,
                "_branch_requires_approval",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                mgr, "_ensure_pr_approved", new_callable=AsyncMock
            ) as mock_approve,
        ):
            await mgr._approve_if_review_mandated(
                _PR, "lfreleng-actions", "lftools-uv", "lfreleng-actions/lftools-uv#484"
            )

        mock_approve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_approval_when_not_required(self) -> None:
        mgr, _ = make_merge_manager()
        with (
            patch.object(
                mgr,
                "_branch_requires_approval",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr, "_ensure_pr_approved", new_callable=AsyncMock
            ) as mock_approve,
        ):
            await mgr._approve_if_review_mandated(
                _PR, "lfreleng-actions", "lftools-uv", "lfreleng-actions/lftools-uv#484"
            )

        mock_approve.assert_not_called()

    @pytest.mark.asyncio
    async def test_preview_mode_no_op(self) -> None:
        mgr, _ = make_merge_manager(preview_mode=True)
        with (
            patch.object(
                mgr, "_branch_requires_approval", new_callable=AsyncMock
            ) as mock_requires,
            patch.object(
                mgr, "_ensure_pr_approved", new_callable=AsyncMock
            ) as mock_approve,
        ):
            await mgr._approve_if_review_mandated(
                _PR, "lfreleng-actions", "lftools-uv", "lfreleng-actions/lftools-uv#484"
            )

        mock_requires.assert_not_called()
        mock_approve.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_already_approved(self) -> None:
        mgr, _ = make_merge_manager()
        pr_key = "lfreleng-actions/lftools-uv#484"
        mgr._recently_approved.add(pr_key)
        with (
            patch.object(
                mgr, "_branch_requires_approval", new_callable=AsyncMock
            ) as mock_requires,
            patch.object(
                mgr, "_ensure_pr_approved", new_callable=AsyncMock
            ) as mock_approve,
        ):
            await mgr._approve_if_review_mandated(
                _PR, "lfreleng-actions", "lftools-uv", pr_key
            )

        mock_requires.assert_not_called()
        mock_approve.assert_not_called()


# ---------------------------------------------------------------------------
# Reactive merge-error approval signal
# ---------------------------------------------------------------------------


class TestMergeErrorIndicatesMissingApproval:
    def test_ruleset_required_approvals_phrasing(self) -> None:
        mgr, _ = make_merge_manager()
        msg = (
            "Failed to merge PR #484. Error: 405. GitHub: Repository rule "
            "violations found Waiting on required approvals from "
            "lfreleng-actions/releng."
        )
        assert mgr._merge_error_indicates_missing_approval(msg) is True

    def test_branch_protection_phrasing(self) -> None:
        mgr, _ = make_merge_manager()
        msg = "At least 1 approving review is required by reviewers with write access."
        assert mgr._merge_error_indicates_missing_approval(msg) is True

    def test_unrelated_failure_is_not_approval(self) -> None:
        mgr, _ = make_merge_manager()
        msg = "Required status check 'ci/test' is expected."
        assert mgr._merge_error_indicates_missing_approval(msg) is False

    def test_empty_text(self) -> None:
        mgr, _ = make_merge_manager()
        assert mgr._merge_error_indicates_missing_approval("") is False
