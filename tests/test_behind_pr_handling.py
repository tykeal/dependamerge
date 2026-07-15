# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Tests for behind PR handling and preview accuracy.

This module tests the improved handling of PRs that are "behind" the base branch,
including proper rebase logic and accurate preview simulation.
"""

from unittest.mock import AsyncMock, patch

import pytest

from dependamerge.merge_manager import AsyncMergeManager, MergeStatus
from dependamerge.models import PullRequestInfo


class TestBehindPRHandling:
    """Test cases for behind PR handling and preview accuracy."""

    @pytest.mark.asyncio
    async def test_preview_detects_behind_pr_with_fix_enabled(self):
        """Test that preview correctly identifies behind PRs when fix is enabled."""

        pr_info = PullRequestInfo(
            number=123,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="behind",  # This is the key state
            behind_by=2,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/123",
            reviews=[],
            review_comments=[],
        )

        # Create merge manager with fix enabled
        async with AsyncMergeManager(
            token="fake_token",
            merge_method="squash",
            max_retries=1,
            concurrency=1,
            fix_out_of_date=True,  # Rebase enabled
            progress_tracker=None,
            preview_mode=True,  # This is preview
            dismiss_copilot=False,
        ) as merge_manager:
            # Mock the github client
            mock_client = AsyncMock()
            merge_manager._github_client = mock_client

            # Mock other required methods
            with patch.object(
                merge_manager,
                "_check_merge_requirements",
                return_value=(True, "PR is behind - will rebase before merge"),
            ):
                with patch.object(merge_manager, "_approve_pr", return_value=None):
                    result = await merge_manager._merge_single_pr(pr_info)

        # Should succeed with indication that rebase would happen.
        # A MERGED result must not carry an ``error``; the behind-base
        # note is surfaced via the dedicated ``warning`` field instead.
        assert result.status == MergeStatus.MERGED
        assert result.error is None
        assert result.warning == "behind base branch"

    @pytest.mark.asyncio
    async def test_preview_detects_behind_pr_with_fix_disabled(self):
        """Test that preview correctly blocks behind PRs when fix is disabled."""

        pr_info = PullRequestInfo(
            number=123,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="behind",  # This is the key state
            behind_by=2,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/123",
            reviews=[],
            review_comments=[],
        )

        # Create merge manager with fix disabled
        async with AsyncMergeManager(
            token="fake_token",
            merge_method="squash",
            max_retries=1,
            concurrency=1,
            fix_out_of_date=False,  # Rebase disabled
            progress_tracker=None,
            preview_mode=True,  # This is preview
            dismiss_copilot=False,
        ) as merge_manager:
            # Mock the github client
            mock_client = AsyncMock()
            merge_manager._github_client = mock_client

            # Mock other required methods
            with patch.object(
                merge_manager,
                "_check_merge_requirements",
                return_value=(
                    False,
                    "PR is behind base branch and --no-fix option is set",
                ),
            ):
                with patch.object(merge_manager, "_approve_pr", return_value=None):
                    result = await merge_manager._merge_single_pr(pr_info)

        # Should be skipped with appropriate message
        assert result.status == MergeStatus.SKIPPED
        assert "behind" in result.error or "no-fix" in result.error

    @pytest.mark.asyncio
    async def test_actual_run_performs_rebase_for_behind_pr(self):
        """Behind PR + strict up-to-date policy → rebase before merge."""

        pr_info = PullRequestInfo(
            number=123,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="behind",  # This is the key state
            behind_by=2,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/123",
            reviews=[],
            review_comments=[],
        )

        # Mock updated PR info after rebase
        updated_pr_info = PullRequestInfo(
            number=123,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="def456",  # Different SHA after rebase
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="clean",  # Now clean after rebase
            behind_by=0,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/123",
            reviews=[],
            review_comments=[],
        )

        # Create merge manager with fix enabled
        async with AsyncMergeManager(
            token="fake_token",
            merge_method="squash",
            max_retries=1,
            concurrency=1,
            fix_out_of_date=True,  # Rebase enabled
            progress_tracker=None,
            preview_mode=False,  # Actual run
            dismiss_copilot=False,
        ) as merge_manager:
            # Mock the github client
            mock_client = AsyncMock()
            mock_client.update_branch.return_value = None  # Successful rebase
            mock_client.merge_pull_request.return_value = True  # Successful merge
            # Branch protection demands up-to-date heads — the only
            # case where Step 5 still rebases proactively.
            mock_client.requires_strict_status_checks = AsyncMock(return_value=True)
            merge_manager._github_client = mock_client

            # Mock the async get method for PR info refresh
            mock_client.get.return_value = {
                "mergeable": updated_pr_info.mergeable,
                "mergeable_state": updated_pr_info.mergeable_state,
            }
            with patch.object(
                merge_manager,
                "_check_merge_requirements",
                return_value=(True, "PR is behind - will rebase before merge"),
            ):
                with patch.object(merge_manager, "_approve_pr", return_value=None):
                    result = await merge_manager._merge_single_pr(pr_info)

        # Should succeed after rebase
        assert result.status == MergeStatus.MERGED
        assert result.error is None

        # Verify rebase was attempted
        mock_client.update_branch.assert_called_once_with("org", "repo", 123)

        # Verify merge was attempted after rebase
        mock_client.merge_pull_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_behind_pr_without_strict_policy_merges_directly(self):
        """Behind PR + no strict policy → direct merge, no rebase.

        GitHub merges a behind-but-green PR unless branch protection
        requires up-to-date heads, so a proactive rebase would only
        restart CI for nothing.  This is the common case that made
        bulk merges slow: every sibling merge put the remaining PRs
        ``behind``, and each was rebased (and its checks re-run)
        without GitHub ever requiring it.
        """

        pr_info = PullRequestInfo(
            number=123,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="behind",
            behind_by=2,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/123",
            reviews=[],
            review_comments=[],
        )

        async with AsyncMergeManager(
            token="fake_token",
            merge_method="squash",
            max_retries=1,
            concurrency=1,
            fix_out_of_date=True,
            progress_tracker=None,
            preview_mode=False,
            dismiss_copilot=False,
        ) as merge_manager:
            mock_client = AsyncMock()
            mock_client.merge_pull_request.return_value = True
            mock_client.requires_strict_status_checks = AsyncMock(return_value=False)
            merge_manager._github_client = mock_client
            mock_client.get.return_value = {
                "mergeable": True,
                "mergeable_state": "behind",
                "state": "open",
            }
            with patch.object(
                merge_manager,
                "_check_merge_requirements",
                return_value=(True, ""),
            ):
                with patch.object(merge_manager, "_approve_pr", return_value=None):
                    result = await merge_manager._merge_single_pr(pr_info)

        assert result.status == MergeStatus.MERGED
        # No branch refresh and no auto-merge wait — the merge call
        # itself is the arbiter.
        mock_client.update_branch.assert_not_called()
        mock_client.enable_auto_merge.assert_not_called()
        mock_client.merge_pull_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_rebase_failure_handling(self):
        """Test that rebase failures are properly handled."""

        pr_info = PullRequestInfo(
            number=123,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="behind",
            behind_by=2,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/123",
            reviews=[],
            review_comments=[],
        )

        # Create merge manager with fix enabled
        async with AsyncMergeManager(
            token="fake_token",
            merge_method="squash",
            max_retries=1,
            concurrency=1,
            fix_out_of_date=True,
            progress_tracker=None,
            preview_mode=False,
            dismiss_copilot=False,
        ) as merge_manager:
            # Mock the github client
            mock_client = AsyncMock()
            # Make rebase fail
            mock_client.update_branch.side_effect = Exception("Rebase conflict")
            # Strict policy forces the Step 5 rebase path.
            mock_client.requires_strict_status_checks = AsyncMock(return_value=True)
            merge_manager._github_client = mock_client

            # Mock other required methods
            with patch.object(
                merge_manager,
                "_check_merge_requirements",
                return_value=(True, "PR is behind - will rebase before merge"),
            ):
                with patch.object(merge_manager, "_approve_pr", return_value=None):
                    result = await merge_manager._merge_single_pr(pr_info)

        # Should fail with rebase error
        assert result.status == MergeStatus.FAILED
        assert "Failed to rebase PR" in result.error

        # Rollback behaviour: a failed rebase must abort the flow before
        # the merge step, so merge_pull_request is never called.
        mock_client.merge_pull_request.assert_not_called()

        # Verify rebase was attempted exactly once
        mock_client.update_branch.assert_called_once_with("org", "repo", 123)

    @pytest.mark.asyncio
    async def test_merge_requirements_check_for_behind_pr(self):
        """Test the merge requirements check for behind PRs."""

        pr_info = PullRequestInfo(
            number=123,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="behind",
            behind_by=2,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/123",
            reviews=[],
            review_comments=[],
        )

        # Test with fix enabled
        async with AsyncMergeManager(
            token="fake_token",
            merge_method="squash",
            max_retries=1,
            concurrency=1,
            fix_out_of_date=True,
            progress_tracker=None,
            preview_mode=False,
            dismiss_copilot=False,
        ) as merge_manager:
            mock_client = AsyncMock()
            mock_client.get_branch_protection.return_value = None
            merge_manager._github_client = mock_client

            can_merge, reason = await merge_manager._check_merge_requirements(pr_info)

            assert can_merge is True
            assert "rebase" in reason.lower()

        # Test with fix disabled
        async with AsyncMergeManager(
            token="fake_token",
            merge_method="squash",
            max_retries=1,
            concurrency=1,
            fix_out_of_date=False,
            progress_tracker=None,
            preview_mode=False,
            dismiss_copilot=False,
        ) as merge_manager:
            mock_client = AsyncMock()
            mock_client.get_branch_protection.return_value = None
            merge_manager._github_client = mock_client

            can_merge, reason = await merge_manager._check_merge_requirements(pr_info)

            # behind + --no-fix no longer hard-fails. _check_merge_requirements
            # now returns can_merge=True so the PR reaches Step 5.5, where
            # auto-merge gets enabled and the wait loop runs (auto-merge
            # is a non-rewriting operation, so it doesn't violate the
            # user's --no-fix intent).
            assert can_merge is True
            assert "behind" in reason.lower() or "auto-merge" in reason.lower()

    @pytest.mark.asyncio
    async def test_proactive_rebase_success(self):
        """Strict up-to-date policy → proactive rebase then merge."""

        pr_info = PullRequestInfo(
            number=123,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="behind",  # This will trigger proactive rebase
            behind_by=2,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/123",
            reviews=[],
            review_comments=[],
        )

        # Updated PR after rebase
        updated_pr_info = PullRequestInfo(
            number=123,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="def456",
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="clean",  # Clean after rebase
            behind_by=0,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/123",
            reviews=[],
            review_comments=[],
        )

        # Create merge manager with fix enabled
        async with AsyncMergeManager(
            token="fake_token",
            merge_method="squash",
            max_retries=1,
            concurrency=1,
            fix_out_of_date=True,
            progress_tracker=None,
            preview_mode=False,
            dismiss_copilot=False,
        ) as merge_manager:
            mock_client = AsyncMock()

            # Mock successful rebase and merge
            mock_client.update_branch.return_value = None
            mock_client.merge_pull_request.return_value = True
            # Strict policy forces the Step 5 rebase path.
            mock_client.requires_strict_status_checks = AsyncMock(return_value=True)
            merge_manager._github_client = mock_client

            # Mock PR info refresh after rebase
            mock_client.get.return_value = {
                "mergeable": updated_pr_info.mergeable,
                "mergeable_state": updated_pr_info.mergeable_state,
            }
            with patch.object(
                merge_manager,
                "_check_merge_requirements",
                return_value=(True, "PR is behind - will rebase before merge"),
            ):
                with patch.object(merge_manager, "_approve_pr", return_value=None):
                    result = await merge_manager._merge_single_pr(pr_info)

            assert result.status == MergeStatus.MERGED
            assert result.error is None

            # Should have attempted rebase once (proactively)
            mock_client.update_branch.assert_called_once_with("org", "repo", 123)

            # Should have attempted merge once (after successful rebase)
            mock_client.merge_pull_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_pr_status_monitoring_after_rebase(self):
        """Test that PR status is properly monitored after rebase."""

        pr_info = PullRequestInfo(
            number=123,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="behind",
            behind_by=2,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/123",
            reviews=[],
            review_comments=[],
        )

        # Simulate PR state progression: behind -> blocked -> clean
        pr_states = [
            # First few checks: still processing
            {"mergeable": True, "mergeable_state": "behind"},
            {"mergeable": True, "mergeable_state": "blocked"},
            # Final check: ready
            {"mergeable": True, "mergeable_state": "clean"},
        ]

        call_count = 0

        def mock_get_api_response(*args):
            nonlocal call_count
            result = pr_states[min(call_count, len(pr_states) - 1)]
            call_count += 1
            return result

        async with AsyncMergeManager(
            token="fake_token",
            merge_method="squash",
            max_retries=1,
            concurrency=1,
            fix_out_of_date=True,
            progress_tracker=None,
            preview_mode=False,
            dismiss_copilot=False,
        ) as merge_manager:
            mock_client = AsyncMock()
            mock_client.update_branch.return_value = None
            mock_client.merge_pull_request.return_value = True
            # Strict policy forces the Step 5 rebase path.
            mock_client.requires_strict_status_checks = AsyncMock(return_value=True)
            merge_manager._github_client = mock_client

            # Mock the async get method for PR status polling
            mock_client.get.side_effect = mock_get_api_response
            with patch.object(
                merge_manager,
                "_check_merge_requirements",
                return_value=(True, "PR is behind - will rebase before merge"),
            ):
                with patch.object(merge_manager, "_approve_pr", return_value=None):
                    result = await merge_manager._merge_single_pr(pr_info)

        # Should succeed after monitoring shows PR is clean
        assert result.status == MergeStatus.MERGED

        # Should have called get API multiple times for monitoring
        assert call_count > 1

    @pytest.mark.asyncio
    async def test_single_line_preview_output_format(self):
        """Test that preview produces exactly one line of output per PR."""

        pr_info = PullRequestInfo(
            number=123,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
            state="open",
            mergeable=True,
            mergeable_state="behind",
            behind_by=2,
            files_changed=[],
            repository_full_name="org/repo",
            html_url="https://github.com/org/repo/pull/123",
            reviews=[],
            review_comments=[],
        )

        # Capture console output
        from unittest.mock import Mock

        mock_console = Mock()

        async with AsyncMergeManager(
            token="fake_token",
            merge_method="squash",
            max_retries=1,
            concurrency=1,
            fix_out_of_date=True,
            progress_tracker=None,
            preview_mode=True,
            dismiss_copilot=False,
        ) as merge_manager:
            # Replace console with mock
            merge_manager._console = mock_console

            mock_client = AsyncMock()
            merge_manager._github_client = mock_client

            with patch.object(
                merge_manager,
                "_check_merge_requirements",
                return_value=(True, "PR is behind - will rebase before merge"),
            ):
                with patch.object(merge_manager, "_approve_pr", return_value=None):
                    await merge_manager._merge_single_pr(pr_info)

        # Verify exactly one print call was made (single-line output)
        assert mock_console.print.call_count == 1

        # Verify the *semantic content* of the single-line preview rather
        # than its exact structure/wording. This keeps the test robust to
        # cosmetic output changes (reordering, relabelling, emoji tweaks):
        # the line must be surfaced as a warning, identify the PR, and
        # explain that it is behind its base branch.
        call_args = mock_console.print.call_args[0][0]
        assert "\u26a0\ufe0f" in call_args  # surfaced as a warning
        assert pr_info.html_url in call_args  # identifies the PR
        assert "behind" in call_args.lower()  # explains the reason
