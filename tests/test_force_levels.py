# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for the force level override system."""

import logging

import pytest

from dependamerge.merge_manager import AsyncMergeManager, MergeStatus
from dependamerge.models import FileChange, PullRequestInfo, ReviewInfo


@pytest.fixture
def sample_pr_info():
    """Create a sample PR info for testing."""
    return PullRequestInfo(
        number=123,
        title="Test PR",
        body="Test body",
        author="dependabot[bot]",
        head_sha="abc123",
        base_branch="main",
        head_branch="update-deps",
        state="open",
        mergeable=True,
        mergeable_state="blocked",
        behind_by=0,
        files_changed=[
            FileChange(
                filename="package.json",
                additions=1,
                deletions=1,
                changes=2,
                status="modified",
            )
        ],
        repository_full_name="test-org/test-repo",
        html_url="https://github.com/test-org/test-repo/pull/123",
        reviews=[],
        review_comments=[],
    )


@pytest.fixture
def pr_with_blocking_review(sample_pr_info):
    """Create PR with a blocking review (changes requested)."""
    pr = sample_pr_info.model_copy(deep=True)
    pr.reviews = [
        ReviewInfo(
            id="REV1",
            user="human-reviewer",
            state="CHANGES_REQUESTED",
            submitted_at="2024-01-01T00:00:00Z",
            body="Please fix this",
        )
    ]
    return pr


@pytest.fixture
def pr_with_conflicts(sample_pr_info):
    """Create PR with merge conflicts."""
    pr = sample_pr_info.model_copy(deep=True)
    pr.mergeable = False
    pr.mergeable_state = "dirty"
    return pr


@pytest.fixture
def pr_behind_base(sample_pr_info):
    """Create PR that is behind base branch."""
    pr = sample_pr_info.model_copy(deep=True)
    pr.mergeable_state = "behind"
    pr.behind_by = 5
    return pr


class TestForceLevelNone:
    """Test default force level (none) - respects all protections."""

    @pytest.mark.asyncio
    async def test_blocks_on_code_owner_requirement(self, sample_pr_info, mocker):
        """Test that code owner requirements block merge with force=none."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {
            "required_pull_request_reviews": {"require_code_owner_reviews": True}
        }

        async with AsyncMergeManager(
            token="fake_token",
            merge_method="squash",
            force_level="none",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            can_merge, reason = await manager._check_merge_requirements(sample_pr_info)

            assert can_merge is False
            assert "code owner reviews are required" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_on_blocking_reviews(self, pr_with_blocking_review):
        """Test that reviews requesting changes block merge with force=none."""
        async with AsyncMergeManager(
            token="fake_token",
            force_level="none",
            preview_mode=True,
        ) as manager:
            result = await manager._merge_single_pr(pr_with_blocking_review)

            assert result.status == MergeStatus.SKIPPED
            assert result.error is not None
            assert "reviews requesting changes" in result.error.lower()

    @pytest.mark.asyncio
    async def test_blocks_on_merge_conflicts(self, pr_with_conflicts, mocker):
        """Test that merge conflicts block merge with force=none."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {}

        async with AsyncMergeManager(
            token="fake_token",
            force_level="none",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            can_merge, reason = await manager._check_merge_requirements(
                pr_with_conflicts
            )

            assert can_merge is False
            assert "merge conflicts" in reason.lower()


class TestForceLevelCodeOwners:
    """Test force=code-owners level - bypasses code owner requirements only."""

    @pytest.mark.asyncio
    async def test_bypasses_code_owner_requirement(self, sample_pr_info, mocker):
        """Test that code owner requirements are bypassed with force=code-owners."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {
            "required_pull_request_reviews": {"require_code_owner_reviews": True}
        }

        async with AsyncMergeManager(
            token="fake_token",
            force_level="code-owners",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            can_merge, reason = await manager._check_merge_requirements(sample_pr_info)

            # Should not return False for code owner requirement
            # It might still return True or check other conditions
            assert (
                "code owner reviews are required" not in reason.lower()
                or can_merge is True
            )

    @pytest.mark.asyncio
    async def test_still_blocks_on_blocking_reviews(self, pr_with_blocking_review):
        """Test that human reviews still block with force=code-owners."""
        async with AsyncMergeManager(
            token="fake_token",
            force_level="code-owners",
            preview_mode=True,
        ) as manager:
            result = await manager._merge_single_pr(pr_with_blocking_review)

            assert result.status == MergeStatus.SKIPPED
            assert result.error is not None
            assert "reviews requesting changes" in result.error.lower()

    @pytest.mark.asyncio
    async def test_still_blocks_on_merge_conflicts(self, pr_with_conflicts, mocker):
        """Test that merge conflicts still block with force=code-owners."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {}

        async with AsyncMergeManager(
            token="fake_token",
            force_level="code-owners",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            can_merge, reason = await manager._check_merge_requirements(
                pr_with_conflicts
            )

            assert can_merge is False
            assert "merge conflicts" in reason.lower()


class TestForceLevelProtectionRules:
    """Test force=protection-rules level - bypasses branch protection checks."""

    @pytest.mark.asyncio
    async def test_bypasses_code_owner_requirement(self, sample_pr_info, mocker):
        """Test that code owner requirements are bypassed with force=protection-rules."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {
            "required_pull_request_reviews": {"require_code_owner_reviews": True}
        }

        async with AsyncMergeManager(
            token="fake_token",
            force_level="protection-rules",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            can_merge, reason = await manager._check_merge_requirements(sample_pr_info)

            # Should not block on code owner requirement
            assert (
                "code owner reviews are required" not in reason.lower()
                or can_merge is True
            )

    @pytest.mark.asyncio
    async def test_bypasses_branch_protection_validation(self, sample_pr_info, mocker):
        """Test that branch protection validation is bypassed."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {
            "required_pull_request_reviews": {"required_approving_review_count": 2}
        }
        # Simulate test merge failure
        mock_github.get.return_value = {
            "mergeable": False,
            "mergeable_state": "blocked",
        }

        async with AsyncMergeManager(
            token="fake_token",
            force_level="protection-rules",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            # The test_merge_capability should be bypassed
            can_merge, reason = await manager._check_merge_requirements(sample_pr_info)

            # Should not fail on protection rules when using force=protection-rules
            # Note: Other conditions might still cause it to fail
            if not can_merge:
                assert "branch protection" not in reason.lower()

    @pytest.mark.asyncio
    async def test_still_blocks_on_blocking_reviews(self, pr_with_blocking_review):
        """Test that human reviews still block with force=protection-rules."""
        async with AsyncMergeManager(
            token="fake_token",
            force_level="protection-rules",
            preview_mode=True,
        ) as manager:
            result = await manager._merge_single_pr(pr_with_blocking_review)

            assert result.status == MergeStatus.SKIPPED
            assert result.error is not None
            assert "reviews requesting changes" in result.error.lower()

    @pytest.mark.asyncio
    async def test_still_blocks_on_merge_conflicts(self, pr_with_conflicts, mocker):
        """Test that merge conflicts still block with force=protection-rules."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {}

        async with AsyncMergeManager(
            token="fake_token",
            force_level="protection-rules",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            can_merge, reason = await manager._check_merge_requirements(
                pr_with_conflicts
            )

            assert can_merge is False
            assert "merge conflicts" in reason.lower()


class TestForceLevelAll:
    """Test force=all level - bypasses most warnings."""

    @pytest.mark.asyncio
    async def test_bypasses_code_owner_requirement(self, sample_pr_info, mocker):
        """Test that code owner requirements are bypassed with force=all."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {
            "required_pull_request_reviews": {"require_code_owner_reviews": True}
        }

        async with AsyncMergeManager(
            token="fake_token",
            force_level="all",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            can_merge, reason = await manager._check_merge_requirements(sample_pr_info)

            # Should not block on code owner requirement
            assert (
                "code owner reviews are required" not in reason.lower()
                or can_merge is True
            )

    @pytest.mark.asyncio
    async def test_bypasses_blocking_reviews(self, pr_with_blocking_review):
        """Test that blocking reviews are bypassed with force=all."""
        async with AsyncMergeManager(
            token="fake_token",
            force_level="all",
            preview_mode=True,
        ) as manager:
            # Should not return SKIPPED for blocking reviews
            result = await manager._merge_single_pr(pr_with_blocking_review)

            # With force=all, should not skip on blocking reviews
            assert result.status != MergeStatus.SKIPPED or (
                result.error is not None
                and "reviews requesting changes" not in result.error.lower()
            )

    @pytest.mark.asyncio
    async def test_bypasses_merge_conflicts(self, pr_with_conflicts, mocker):
        """Test that merge conflicts are bypassed with force=all (will attempt merge)."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {}

        async with AsyncMergeManager(
            token="fake_token",
            force_level="all",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            can_merge, reason = await manager._check_merge_requirements(
                pr_with_conflicts
            )

            # With force=all, should attempt merge despite conflicts
            assert can_merge is True
            assert "forcing merge attempt" in reason.lower()

    @pytest.mark.asyncio
    async def test_bypasses_failing_status_checks(self, sample_pr_info, mocker):
        """Test that failing status checks are bypassed with force=all."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {}

        pr = sample_pr_info.model_copy(deep=True)
        pr.mergeable = False
        pr.mergeable_state = "blocked"

        async with AsyncMergeManager(
            token="fake_token",
            force_level="all",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            can_merge, reason = await manager._check_merge_requirements(pr)

            # With force=all, should attempt merge despite failing checks
            assert can_merge is True
            assert (
                "forcing merge attempt" in reason.lower()
                or "force=all" in reason.lower()
            )

    @pytest.mark.asyncio
    async def test_bypasses_behind_with_no_fix(self, pr_behind_base, mocker):
        """Test that behind PRs are attempted even with --no-fix when using force=all."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {}

        async with AsyncMergeManager(
            token="fake_token",
            force_level="all",
            fix_out_of_date=False,  # --no-fix
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            can_merge, reason = await manager._check_merge_requirements(pr_behind_base)

            # With force=all, should attempt merge despite being behind
            assert can_merge is True
            assert "forcing merge attempt" in reason.lower()


class TestForceValidation:
    """Test force level validation and edge cases."""

    def test_valid_force_levels(self):
        """Test that all valid force levels are accepted."""
        valid_levels = ["none", "code-owners", "protection-rules", "all"]

        for level in valid_levels:
            manager = AsyncMergeManager(
                token="fake_token",
                force_level=level,
                preview_mode=True,
            )
            assert manager.force_level == level

    def test_default_force_level_is_code_owners(self):
        """Test that default force level is 'code-owners'."""
        manager = AsyncMergeManager(
            token="fake_token",
            preview_mode=True,
        )
        assert manager.force_level == "code-owners"


class TestForceLogging:
    """Test that force levels log appropriate warnings."""

    @pytest.mark.asyncio
    async def test_logs_code_owner_bypass(self, sample_pr_info, mocker, caplog):
        """Test that bypassing code owners logs a warning."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {
            "required_pull_request_reviews": {"require_code_owner_reviews": True}
        }

        async with AsyncMergeManager(
            token="fake_token",
            force_level="code-owners",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            await manager._check_merge_requirements(sample_pr_info)

            # Should log a warning about bypassing
            assert any(
                "Bypassing code owner" in record.message for record in caplog.records
            )

    @pytest.mark.asyncio
    async def test_logs_protection_rules_bypass(self, sample_pr_info, mocker, caplog):
        """Test that bypassing protection rules logs a warning."""
        # Configure caplog to capture INFO level from the merge_manager module
        caplog.set_level(logging.INFO, logger="dependamerge.merge_manager")

        mock_github = mocker.AsyncMock()

        # Mock the specific API call that _predict_merge_outcome makes
        def mock_get_side_effect(url):
            if "/pulls/" in url:
                return {
                    "mergeable": False,
                    "mergeable_state": "blocked",
                    "head": {"sha": "abc123"},
                }
            return {}

        mock_github.get.side_effect = mock_get_side_effect

        # Mock analyze_block_reason to return a non-approval blocker
        # (e.g. failing checks) so the force-level bypass path is exercised
        mock_github.analyze_block_reason.return_value = (
            "Blocked by failing check: ci/test"
        )

        async with AsyncMergeManager(
            token="fake_token",
            force_level="protection-rules",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            # Test the bypass directly since _check_merge_requirements
            # only predicts the outcome in preview mode
            result = await manager._predict_merge_outcome(
                "test-org", "test-repo", 123, "merge"
            )

            # Should have bypassed and logged
            assert result[0] is True
            assert "bypassed by force level" in result[1]

            # Should log an info message about bypassing branch protection rules
            assert any(
                "bypassing branch protection rules" in record.message.lower()
                for record in caplog.records
            )

    @pytest.mark.asyncio
    async def test_logs_all_level_bypass(self, pr_with_conflicts, mocker, caplog):
        """Test that force=all logs warnings."""
        mock_github = mocker.AsyncMock()
        mock_github.get_branch_protection.return_value = {}

        async with AsyncMergeManager(
            token="fake_token",
            force_level="all",
            preview_mode=True,
        ) as manager:
            manager._github_client = mock_github

            await manager._check_merge_requirements(pr_with_conflicts)

            # Should log a warning about forcing merge
            assert any(
                "forcing merge attempt" in record.message.lower()
                or "force=all" in record.message.lower()
                for record in caplog.records
            )
