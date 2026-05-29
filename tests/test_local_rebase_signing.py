# SPDX-FileCopyrightText: 2026 The Linux Foundation
# SPDX-License-Identifier: Apache-2.0
"""Tests for the signature-preserving local-rebase path.

Covers:

- ``should_use_local_rebase`` decision tree (pre-commit-ci, signed
  base + signed head, signed base + unsigned head, unsigned base,
  ``--no-rebase-local`` opt-out).
- End-to-end Step 5 dispatch: when the gate says ``use_local``, we
  do **not** call the REST ``update-branch`` endpoint regardless of
  whether the local rebase succeeds or fails.
- ``_rebased_prs`` is populated in both local-success and
  local-failure cases so Step 5.5 doesn't double the configured
  ``merge_timeout``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dependamerge import rebase as rebase_module
from dependamerge.github2gerrit_detector import GitHub2GerritDetectionResult
from dependamerge.merge_manager import AsyncMergeManager, MergeStatus
from dependamerge.models import PullRequestInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pr(
    *,
    author: str = "dependabot[bot]",
    mergeable_state: str = "behind",
    head_repo_clone_url: str = "https://github.com/owner/repo.git",
    base_repo_clone_url: str = "https://github.com/owner/repo.git",
) -> PullRequestInfo:
    return PullRequestInfo(
        number=42,
        node_id="PR_node42",
        title="Test PR",
        body="",
        author=author,
        head_sha="abc123",
        base_branch="main",
        head_branch="feature",
        state="open",
        mergeable=True,
        mergeable_state=mergeable_state,
        behind_by=2,
        files_changed=[],
        repository_full_name="owner/repo",
        html_url="https://github.com/owner/repo/pull/42",
        reviews=[],
        review_comments=[],
        head_repo_full_name="owner/repo",
        head_repo_clone_url=head_repo_clone_url,
        base_repo_full_name="owner/repo",
        base_repo_clone_url=base_repo_clone_url,
        is_fork=False,
    )


def _make_mgr(**overrides) -> tuple[AsyncMergeManager, AsyncMock]:
    """Build a manager with an AsyncMock GitHub client.

    Thin wrapper around the shared ``tests.conftest.make_merge_manager``
    so typing/lint fixes and defaults stay centralised. Local
    additions: defaults ``rebase_local=True`` (the production
    default — this module's tests focus on the rebase path).
    """
    from tests.conftest import make_merge_manager

    defaults: dict[str, Any] = {"rebase_local": True}
    defaults.update(overrides)
    return make_merge_manager(**defaults)


# ---------------------------------------------------------------------------
# 1. should_use_local_rebase decision tree
# ---------------------------------------------------------------------------


class TestShouldUseLocalRebaseGate:
    """Pin the gating decisions for the local-rebase path."""

    @pytest.mark.asyncio
    async def test_disabled_by_no_rebase_local_flag(self) -> None:
        """``--no-rebase-local`` short-circuits the gate to False."""
        mgr, client = _make_mgr(rebase_local=False)
        pr = _make_pr(author="pre-commit-ci[bot]")  # would otherwise match
        use_local, reason = await rebase_module.should_use_local_rebase(
            github_client=client,
            pr_info=pr,
            owner="owner",
            repo="repo",
            base_branch="main",
            rebase_local=mgr.rebase_local,
            log=mgr.log,
        )
        assert use_local is False
        assert "--no-rebase-local" in reason

    @pytest.mark.asyncio
    async def test_pre_commit_ci_always_local(self) -> None:
        """``pre-commit-ci[bot]`` PRs always take the local path.

        The bot has no comment macro for recreating a PR with a
        re-signed commit, so we never route it through REST
        update-branch (which would break verification).
        """
        mgr, client = _make_mgr()
        pr = _make_pr(author="pre-commit-ci[bot]")
        # No signature mocks needed: pre-commit-ci shortcut fires
        # before requires_commit_signatures is consulted.
        use_local, reason = await rebase_module.should_use_local_rebase(
            github_client=client,
            pr_info=pr,
            owner="owner",
            repo="repo",
            base_branch="main",
            rebase_local=mgr.rebase_local,
            log=mgr.log,
        )
        assert use_local is True
        assert "pre-commit-ci" in reason
        client.requires_commit_signatures.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_signed_base_signed_head_uses_local(self) -> None:
        """Signed branch + verified head → local path."""
        mgr, client = _make_mgr()
        pr = _make_pr(author="dependabot[bot]")
        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.check_pr_commit_signatures = AsyncMock(return_value=(True, []))
        use_local, reason = await rebase_module.should_use_local_rebase(
            github_client=client,
            pr_info=pr,
            owner="owner",
            repo="repo",
            base_branch="main",
            rebase_local=mgr.rebase_local,
            log=mgr.log,
        )
        assert use_local is True
        assert "PR head is verified" in reason

    @pytest.mark.asyncio
    async def test_signed_base_unsigned_head_uses_rest(self) -> None:
        """Signed branch + unsigned PR head → REST path.

        Signature is already broken on the PR head, so REST
        update-branch can't make verification any worse. Use the
        cheaper REST path.
        """
        mgr, client = _make_mgr()
        pr = _make_pr(author="dependabot[bot]")
        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.check_pr_commit_signatures = AsyncMock(return_value=(False, ["abc123"]))
        use_local, reason = await rebase_module.should_use_local_rebase(
            github_client=client,
            pr_info=pr,
            owner="owner",
            repo="repo",
            base_branch="main",
            rebase_local=mgr.rebase_local,
            log=mgr.log,
        )
        assert use_local is False
        assert "not currently verified" in reason

    @pytest.mark.asyncio
    async def test_unsigned_base_uses_rest(self) -> None:
        """Base branch with no signature requirement → REST path."""
        mgr, client = _make_mgr()
        pr = _make_pr(author="dependabot[bot]")
        client.requires_commit_signatures = AsyncMock(return_value=False)
        use_local, reason = await rebase_module.should_use_local_rebase(
            github_client=client,
            pr_info=pr,
            owner="owner",
            repo="repo",
            base_branch="main",
            rebase_local=mgr.rebase_local,
            log=mgr.log,
        )
        assert use_local is False
        assert "does not require signatures" in reason

    @pytest.mark.asyncio
    async def test_signature_check_failure_uses_rest(self) -> None:
        """If the requirement check raises, fail safely to REST.

        We don't want to risk an unbounded local-rebase attempt
        when we can't determine whether signatures are required.
        """
        mgr, client = _make_mgr()
        pr = _make_pr(author="dependabot[bot]")
        client.requires_commit_signatures = AsyncMock(side_effect=RuntimeError("boom"))
        use_local, reason = await rebase_module.should_use_local_rebase(
            github_client=client,
            pr_info=pr,
            owner="owner",
            repo="repo",
            base_branch="main",
            rebase_local=mgr.rebase_local,
            log=mgr.log,
        )
        assert use_local is False
        assert "signature requirement check failed" in reason

    @pytest.mark.asyncio
    async def test_no_github_client_returns_false(self) -> None:
        """Without a client we cannot consult branch protection."""
        mgr, _client = _make_mgr()
        pr = _make_pr(author="dependabot[bot]")
        use_local, reason = await rebase_module.should_use_local_rebase(
            github_client=None,
            pr_info=pr,
            owner="owner",
            repo="repo",
            base_branch="main",
            rebase_local=mgr.rebase_local,
            log=mgr.log,
        )
        assert use_local is False
        assert "no GitHub client" in reason

    @pytest.mark.asyncio
    async def test_signature_check_truthy_non_true_treated_as_false(
        self,
    ) -> None:
        """Non-strict ``True`` from requires_commit_signatures must not match.

        ``AsyncMock`` defaults return a truthy ``MagicMock``
        instance; if we accepted any truthy value the production
        gate would route real PRs into the local-rebase path on
        mocks that hadn't explicitly set ``return_value``.
        """
        mgr, client = _make_mgr()
        pr = _make_pr(author="dependabot[bot]")
        client.requires_commit_signatures = AsyncMock(return_value=MagicMock())
        use_local, _reason = await rebase_module.should_use_local_rebase(
            github_client=client,
            pr_info=pr,
            owner="owner",
            repo="repo",
            base_branch="main",
            rebase_local=mgr.rebase_local,
            log=mgr.log,
        )
        assert use_local is False

    @pytest.mark.asyncio
    async def test_pr_signature_check_failure_uses_rest(self) -> None:
        """If ``check_pr_commit_signatures()`` raises, fail safely to REST.

        Distinct from ``test_signature_check_failure_uses_rest``
        which covers ``requires_commit_signatures()`` raising.
        Here the base requirement check succeeds (signatures are
        required) but the PR-head verification check raises.
        Failing closed avoids triggering network-touching local
        clones on transient API failures, and keeps the gate
        consistent with its documented invariant ("base requires
        signatures AND PR head is verified").
        """
        mgr, client = _make_mgr()
        pr = _make_pr(author="dependabot[bot]")
        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.check_pr_commit_signatures = AsyncMock(
            side_effect=RuntimeError("transient API error")
        )
        use_local, reason = await rebase_module.should_use_local_rebase(
            github_client=client,
            pr_info=pr,
            owner="owner",
            repo="repo",
            base_branch="main",
            rebase_local=mgr.rebase_local,
            log=mgr.log,
        )
        assert use_local is False
        assert "signature check failed" in reason


# ---------------------------------------------------------------------------
# 2. Step 5 dispatch: local-rebase path skips REST update-branch
# ---------------------------------------------------------------------------


class TestStep5DispatchLocalRebase:
    """Step 5 must not call REST update-branch when use_local is True.

    These tests verify the integration: regardless of whether the
    local rebase succeeds or fails, ``update_branch()`` must not
    fire. This is the whole point of the feature — a REST
    update-branch would replace the verified commit with an
    unsigned one and break branch protection.
    """

    @pytest.mark.asyncio
    async def test_pre_commit_ci_pr_skips_rest_update_branch_on_success(
        self,
    ) -> None:
        """Pre-commit-ci PR + local rebase succeeds → no REST call."""
        mgr, client = _make_mgr(merge_timeout=0.1, fix_out_of_date=True)
        pr = _make_pr(author="pre-commit-ci[bot]", mergeable_state="behind")

        client.update_branch = AsyncMock()
        client.enable_auto_merge = AsyncMock(return_value=True)
        client.post_issue_comment = AsyncMock()
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "clean",
                "state": "open",
            }
        )
        client.analyze_block_reason = AsyncMock(return_value=None)
        client.get_required_status_checks = AsyncMock(return_value=[])
        client.requires_commit_signatures = AsyncMock(return_value=True)

        with (
            patch(
                "dependamerge.rebase.local_rebase_pr",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_local_rebase,
            patch.object(
                mgr,
                "_detect_github2gerrit",
                new_callable=AsyncMock,
                return_value=GitHub2GerritDetectionResult(),
            ),
            patch.object(
                mgr,
                "_get_merge_method_for_repo",
                new_callable=AsyncMock,
                return_value="merge",
            ),
            patch.object(
                mgr,
                "_trigger_stale_precommit_ci",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr,
                "_check_merge_requirements",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
            patch.object(
                mgr,
                "_approve_pr",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await mgr._merge_single_pr(pr)

        # Local rebase was attempted exactly once.
        mock_local_rebase.assert_awaited_once()
        # REST update-branch was NEVER called — this is the key
        # assertion: the verification-preserving path did not
        # fall through to the verification-breaking endpoint.
        client.update_branch.assert_not_awaited()
        # The PR was marked as rebased so Step 5.5 doesn't double
        # the merge timeout.
        assert "owner/repo#42" in mgr._rebased_prs

    @pytest.mark.asyncio
    async def test_pre_commit_ci_pr_skips_rest_update_branch_on_failure(
        self,
    ) -> None:
        """Pre-commit-ci PR + local rebase FAILS → still no REST call.

        This is the most important invariant: when local rebase
        fails (no git, conflicts, network) we must NOT fall back
        to REST update-branch, because doing so would break
        verification — exactly the bug this feature exists to
        prevent.
        """
        mgr, client = _make_mgr(merge_timeout=0.1, fix_out_of_date=True)
        pr = _make_pr(author="pre-commit-ci[bot]", mergeable_state="behind")

        client.update_branch = AsyncMock()
        client.enable_auto_merge = AsyncMock(return_value=True)
        client.post_issue_comment = AsyncMock()
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "behind",
                "state": "open",
            }
        )
        client.analyze_block_reason = AsyncMock(return_value=None)
        client.get_required_status_checks = AsyncMock(return_value=[])
        client.requires_commit_signatures = AsyncMock(return_value=True)

        with (
            patch(
                "dependamerge.rebase.local_rebase_pr",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_local_rebase,
            patch.object(
                mgr,
                "_detect_github2gerrit",
                new_callable=AsyncMock,
                return_value=GitHub2GerritDetectionResult(),
            ),
            patch.object(
                mgr,
                "_get_merge_method_for_repo",
                new_callable=AsyncMock,
                return_value="merge",
            ),
            patch.object(
                mgr,
                "_trigger_stale_precommit_ci",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr,
                "_check_merge_requirements",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
            patch.object(
                mgr,
                "_approve_pr",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await mgr._merge_single_pr(pr)

        mock_local_rebase.assert_awaited_once()
        # REST update-branch must still NOT fire on local failure.
        client.update_branch.assert_not_awaited()
        # PR is marked rebased so Step 5.5 doesn't double-wait.
        assert "owner/repo#42" in mgr._rebased_prs
        # Auto-merge gets enabled by the local-rebase orchestrator
        # (regardless of whether the local rebase itself succeeded
        # or failed) so Step 6's skip gate routes the PR to
        # AUTO_MERGE_PENDING. Without this, marking ``_rebased_prs``
        # would skip Step 5.5 too, and Step 6 would attempt a
        # manual merge that 405s against pending checks — exactly
        # the failure mode this feature exists to prevent.
        assert "owner/repo#42" in mgr._auto_merge_enabled
        assert result.status == MergeStatus.AUTO_MERGE_PENDING

    @pytest.mark.asyncio
    async def test_unsigned_base_uses_rest_update_branch(self) -> None:
        """Unsigned base + non-pre-commit-ci → REST path runs.

        Backward compatibility: when the gate says don't go local,
        Step 5 must still call the REST endpoint (the existing
        behaviour for repos without signature requirements).
        """
        mgr, client = _make_mgr(merge_timeout=0.1, fix_out_of_date=True)
        pr = _make_pr(author="dependabot[bot]", mergeable_state="behind")

        client.update_branch = AsyncMock()
        client.enable_auto_merge = AsyncMock(return_value=True)
        client.post_issue_comment = AsyncMock()
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "clean",
                "state": "open",
            }
        )
        client.analyze_block_reason = AsyncMock(return_value=None)
        client.get_required_status_checks = AsyncMock(return_value=[])
        # Unsigned base → gate returns False → REST path.
        client.requires_commit_signatures = AsyncMock(return_value=False)

        with (
            patch(
                "dependamerge.rebase.local_rebase_pr",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_local_rebase,
            patch.object(
                mgr,
                "_detect_github2gerrit",
                new_callable=AsyncMock,
                return_value=GitHub2GerritDetectionResult(),
            ),
            patch.object(
                mgr,
                "_get_merge_method_for_repo",
                new_callable=AsyncMock,
                return_value="merge",
            ),
            patch.object(
                mgr,
                "_trigger_stale_precommit_ci",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr,
                "_check_merge_requirements",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
            patch.object(
                mgr,
                "_approve_pr",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await mgr._merge_single_pr(pr)

        # Local rebase was NOT attempted.
        mock_local_rebase.assert_not_awaited()
        # REST update-branch WAS called (the legacy path).
        client.update_branch.assert_awaited_once_with("owner", "repo", 42)

    @pytest.mark.asyncio
    async def test_no_rebase_local_flag_uses_rest(self) -> None:
        """``--no-rebase-local`` forces the REST path even for pre-commit-ci.

        Provides a user-visible escape hatch when the local path
        is unavailable (e.g. running in a constrained environment
        with no ``git`` or no network).
        """
        mgr, client = _make_mgr(
            merge_timeout=0.1, fix_out_of_date=True, rebase_local=False
        )
        pr = _make_pr(author="pre-commit-ci[bot]", mergeable_state="behind")

        client.update_branch = AsyncMock()
        client.enable_auto_merge = AsyncMock(return_value=True)
        client.post_issue_comment = AsyncMock()
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "clean",
                "state": "open",
            }
        )
        client.analyze_block_reason = AsyncMock(return_value=None)
        client.get_required_status_checks = AsyncMock(return_value=[])
        client.requires_commit_signatures = AsyncMock(return_value=True)

        with (
            patch(
                "dependamerge.rebase.local_rebase_pr",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_local_rebase,
            patch.object(
                mgr,
                "_detect_github2gerrit",
                new_callable=AsyncMock,
                return_value=GitHub2GerritDetectionResult(),
            ),
            patch.object(
                mgr,
                "_get_merge_method_for_repo",
                new_callable=AsyncMock,
                return_value="merge",
            ),
            patch.object(
                mgr,
                "_trigger_stale_precommit_ci",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr,
                "_check_merge_requirements",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
            patch.object(
                mgr,
                "_approve_pr",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await mgr._merge_single_pr(pr)

        mock_local_rebase.assert_not_awaited()
        client.update_branch.assert_awaited_once_with("owner", "repo", 42)


# ---------------------------------------------------------------------------
# 3. _authed_clone_url helper
# ---------------------------------------------------------------------------


class TestAuthedCloneUrl:
    """Token injection mirrors ``FixOrchestrator._authed_url``."""

    def test_https_url_gets_token(self) -> None:
        url = rebase_module.authed_clone_url(
            "https://github.com/owner/repo.git", "abc123"
        )
        assert url == "https://x-access-token:abc123@github.com/owner/repo.git"

    def test_ssh_url_unchanged(self) -> None:
        ssh = "git@github.com:owner/repo.git"
        assert rebase_module.authed_clone_url(ssh, "abc123") == ssh

    def test_git_protocol_url_unchanged(self) -> None:
        url = "git://github.com/owner/repo.git"
        assert rebase_module.authed_clone_url(url, "abc123") == url


# ---------------------------------------------------------------------------
# 4. Local-rebase path conditionally skips _rebased_prs marking
# ---------------------------------------------------------------------------


class TestLocalRebaseAutoMergeUnavailable:
    """Verify the conditional ``_rebased_prs`` invariant.

    When local rebase succeeds but auto-merge cannot be enabled
    (repo doesn't allow it, branch protection blocks it, etc.),
    we must **not** mark ``_rebased_prs`` — doing so would skip
    Step 5.5 and let Step 6 attempt a manual merge while GitHub
    is still recomputing mergeability after the force-push,
    which would 405 transiently.

    When auto-merge *is* enabled, marking ``_rebased_prs`` is
    correct: Step 5.5's wait would be redundant since auto-merge
    handles the wait server-side, and the marker keeps Step 6's
    skip gate firing for ``AUTO_MERGE_PENDING``.
    """

    @pytest.mark.asyncio
    async def test_auto_merge_unavailable_leaves_pr_unmarked(self) -> None:
        """enable_auto_merge False → PR not in ``_rebased_prs``."""
        mgr, client = _make_mgr(merge_timeout=0.1, fix_out_of_date=True)
        pr = _make_pr(author="pre-commit-ci[bot]", mergeable_state="behind")

        client.update_branch = AsyncMock()
        # Repo doesn't allow auto-merge — enable_auto_merge returns False.
        client.enable_auto_merge = AsyncMock(return_value=False)
        # Refresh keeps the PR ``behind`` (transient post-push state).
        client.get = AsyncMock(
            return_value={
                "mergeable": True,
                "mergeable_state": "behind",
                "state": "open",
            }
        )
        client.analyze_block_reason = AsyncMock(return_value=None)
        client.get_required_status_checks = AsyncMock(return_value=[])
        client.requires_commit_signatures = AsyncMock(return_value=True)
        client.post_issue_comment = AsyncMock()

        with (
            patch(
                "dependamerge.rebase.local_rebase_pr",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                mgr,
                "_detect_github2gerrit",
                new_callable=AsyncMock,
                return_value=GitHub2GerritDetectionResult(),
            ),
            patch.object(
                mgr,
                "_get_merge_method_for_repo",
                new_callable=AsyncMock,
                return_value="merge",
            ),
            patch.object(
                mgr,
                "_trigger_stale_precommit_ci",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                mgr,
                "_check_merge_requirements",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
            patch.object(
                mgr,
                "_approve_pr",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                mgr,
                "_merge_pr_with_retry",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await mgr._merge_single_pr(pr)

        # REST update-branch must still NOT fire (signature
        # preservation invariant).
        client.update_branch.assert_not_awaited()
        # _rebased_prs must NOT be populated, so Step 5.5 still
        # runs its bounded wait. This gives GitHub time to
        # recompute mergeability after the force-push before any
        # manual merge attempt in Step 6.
        assert "owner/repo#42" not in mgr._rebased_prs


# ---------------------------------------------------------------------------
# 5. local_rebase_pr fails closed when head repo identity is unknown
# ---------------------------------------------------------------------------


class TestLocalRebaseFailClosed:
    """Verify local_rebase_pr() refuses to push when head repo is unknown.

    For fork PRs, synthesising a clone URL from the base repo name
    would push to the upstream repo instead of the fork (creating
    or overwriting a branch on someone else's repository). When
    ``head_repo_full_name`` and ``head_repo_clone_url`` are both
    unset we cannot tell whether the PR is from a fork, so we
    fail closed to avoid the dangerous mis-target.
    """

    @pytest.mark.asyncio
    async def test_missing_head_repo_identity_returns_false(
        self,
    ) -> None:
        """head_repo_full_name + head_repo_clone_url both None → False."""
        import logging

        from dependamerge.models import PullRequestInfo

        # Construct a PR with NEITHER head_repo identifier set.
        # This mimics the production state where ``PullRequestInfo``
        # objects from the merge workflow don't populate the
        # optional head/base repo fields (they're only set in the
        # interactive fix flow).
        pr = PullRequestInfo(
            number=42,
            node_id="PR_node42",
            title="Test PR",
            body="",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="behind",
            behind_by=2,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/42",
            reviews=[],
            review_comments=[],
            head_repo_full_name=None,
            head_repo_clone_url=None,
            base_repo_full_name=None,
            base_repo_clone_url=None,
            is_fork=None,
        )

        result = await rebase_module.local_rebase_pr(
            pr_info=pr,
            owner="owner",
            repo="repo",
            token="fake-token",
            log=logging.getLogger("test"),
        )
        # Must NOT attempt the rebase — we'd risk pushing to the
        # base repo for a fork PR. The caller falls through to
        # auto-merge, which is always safe.
        assert result is False
