# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

import hashlib
from unittest.mock import AsyncMock, Mock, patch

import pytest
from typer.testing import CliRunner

from dependamerge.cli import (
    _format_failure_reason,
    _generate_override_sha,
    _MergeContext,
    _restart_merge_progress_tracker,
    _validate_override_sha,
    app,
)
from dependamerge.models import PullRequestInfo


class TestCLI:
    runner: CliRunner = CliRunner()

    @pytest.fixture(autouse=True)
    def _mock_pre_flight_permission_check(self):
        """Auto-mock the pre-flight token permission check.

        The real check makes live GitHub API calls and aborts the
        command with ``typer.Exit(3)`` when the configured token
        lacks the required scopes.  Tests in this class use mock
        ``GitHubClient`` / ``GitHubAsync`` instances and a dummy
        token, so the live check would always fail and abort
        before exercising the code under test.  Patch it out for
        the duration of every test in the class.
        """
        with patch("dependamerge.cli._check_merge_permissions") as mock_check:
            yield mock_check

    def setup_method(self):
        self.runner = CliRunner()

    def test_top_level_version(self):
        result = self.runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        # Should contain the version banner
        assert "dependamerge version" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.PRComparator")
    @patch("dependamerge.github_service.GitHubService")
    def test_merge_command_interactive_default(
        self,
        mock_service_class,
        mock_comparator_class,
        mock_client_class,
    ):
        # Setup mocks
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_comparator = Mock()
        mock_comparator_class.return_value = mock_comparator

        mock_service = Mock()
        mock_service_class.return_value = mock_service

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = True

        # Mock a repository with a similar PR
        mock_repo = Mock()
        mock_repo.full_name = "owner/other-repo"
        mock_client.get_organization_repositories.return_value = [mock_repo]

        # Mock a similar PR
        mock_open_pr = Mock()
        mock_open_pr.number = 5
        mock_open_pr.user.login = "dependabot[bot]"
        mock_client.get_open_pull_requests.return_value = [mock_open_pr]

        # Mock the similar PR info
        similar_pr = PullRequestInfo(
            number=5,
            title="Bump requests from 2.28.0 to 2.28.1",
            body="Test body",
            author="dependabot[bot]",
            head_sha="def456",
            base_branch="main",
            head_branch="dependabot/pip/requests-2.28.1",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/other-repo",
            html_url="https://github.com/owner/other-repo/pull/5",
        )

        mock_pr = PullRequestInfo(
            number=22,
            title="Bump requests from 2.28.0 to 2.28.1",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="dependabot/pip/requests-2.28.1",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )

        def get_pr_info_side_effect(owner, repo, pr_number):
            if pr_number == 22:
                return mock_pr
            elif pr_number == 5:
                return similar_pr
            return None

        mock_client.get_pull_request_info.side_effect = get_pr_info_side_effect
        mock_client.get_pr_status_details.return_value = "Ready to merge"
        mock_client.get_pull_request_commits.return_value = [
            "Bump requests from 2.28.0 to 2.28.1"
        ]

        # Mock comparison result
        from dependamerge.models import ComparisonResult

        comparison_result = ComparisonResult(
            is_similar=True,
            confidence_score=0.95,
            reasons=["Same title pattern", "Same author"],
        )
        mock_comparator.compare_pull_requests.return_value = comparison_result

        # Mock the GitHubService.find_similar_prs method as async
        async def mock_find_similar_prs(*args, **kwargs):
            return [(similar_pr, comparison_result)]

        async def mock_close():
            return None

        mock_service.find_similar_prs = mock_find_similar_prs
        mock_service.close = mock_close

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo/pull/22",
                "--token",
                "test_token",
            ],
        )

        assert result.exit_code == 0
        assert "Dependamerge Evaluation" in result.stdout

    def test_merge_command_invalid_url(self):
        """Test that invalid URLs are caught by the URL parser."""
        result = self.runner.invoke(
            app, ["merge", "https://invalid-url.com", "--token", "test_token"]
        )

        assert result.exit_code == 1
        assert "❌ Invalid URL:" in result.stdout

    def test_merge_command_rejects_negative_max_wait(self):
        """Negative --max-wait must fail fast before any URL routing.

        Regression for the Copilot finding: ``--max-wait`` documents
        ``0`` (fire-and-forget) and ``> 0`` (wall-clock ceiling), but a
        negative value was silently accepted and coerced into a
        surprising instant no-wait run (``max_wait <= 0``).  The command
        now rejects negatives with exit code 1.
        """
        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo/pull/22",
                "--token",
                "test_token",
                "--max-wait",
                "-1",
            ],
        )

        assert result.exit_code == 1
        assert "Invalid --max-wait" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    def test_merge_command_non_automation_pr(self, mock_client_class):
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = False

        mock_pr = PullRequestInfo(
            number=22,
            title="Fix bug",
            body="Test body",
            author="human-user",
            head_sha="abc123",
            base_branch="main",
            head_branch="fix-bug",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )
        mock_client.get_pull_request_info.return_value = mock_pr
        mock_client.get_pull_request_commits.return_value = [
            "Fix bug\n\nDetailed description"
        ]
        mock_client.get_pr_status_details.return_value = "Ready to merge"

        result = self.runner.invoke(
            app,
            ["merge", "https://github.com/owner/repo/pull/22", "--token", "test_token"],
        )

        assert result.exit_code == 0
        assert "not from a recognized automation tool" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.PRComparator")
    @patch("dependamerge.github_service.GitHubService")
    @patch("dependamerge.merge_manager.GitHubAsync")
    def test_merge_command_no_similar_prs_merges_source(
        self,
        mock_async_class,
        mock_service_class,
        mock_comparator_class,
        mock_client_class,
    ):
        """Test that when no similar PRs are found, the source PR is still merged."""
        # Setup mocks
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_comparator = Mock()
        mock_comparator_class.return_value = mock_comparator

        mock_service = Mock()
        mock_service_class.return_value = mock_service

        # Setup GitHubAsync mock for AsyncMergeManager
        mock_async = AsyncMock()
        mock_async.approve_pull_request = AsyncMock()
        mock_async.merge_pull_request = AsyncMock(return_value=True)
        mock_async.update_branch = AsyncMock()

        # Mock the GitHubAsync class to return our mock instance
        mock_async_instance = AsyncMock()
        mock_async_instance.__aenter__ = AsyncMock(return_value=mock_async)
        mock_async_instance.__aexit__ = AsyncMock(return_value=None)
        mock_async_class.return_value = mock_async_instance

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = True

        # Mock repository with no similar PRs
        mock_repo = Mock()
        mock_repo.full_name = "owner/other-repo"
        mock_repo.owner.login = "owner"
        mock_repo.name = "other-repo"
        mock_client.get_organization_repositories.return_value = [mock_repo]

        # Mock no open PRs (or none that are similar)
        mock_client.get_open_pull_requests.return_value = []

        # Mock the source PR
        mock_pr = PullRequestInfo(
            number=22,
            title="pre-commit autoupdate",
            body="Update pre-commit hooks",
            author="pre-commit-ci[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="pre-commit-ci-update-config",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )
        mock_client.get_pull_request_info.return_value = mock_pr
        mock_client.get_pr_status_details.return_value = "Ready to merge"
        mock_client.get_pull_request_commits.return_value = [
            "pre-commit autoupdate\n\nUpdate pre-commit hooks"
        ]

        # Mock approve and merge methods
        mock_client.approve_pull_request.return_value = True
        mock_client.merge_pull_request.return_value = True
        mock_client.fix_out_of_date_pr.return_value = True

        # Mock the GitHubService.find_similar_prs method as async to return no similar PRs
        async def mock_find_similar_prs(*args, **kwargs):
            return []

        async def mock_close():
            return None

        mock_service.find_similar_prs = mock_find_similar_prs
        mock_service.close = mock_close

        # Mock the _check_merge_requirements method to avoid async issues
        with patch(
            "dependamerge.merge_manager.AsyncMergeManager._check_merge_requirements",
            new_callable=AsyncMock,
            return_value=(True, "Ready to merge"),
        ):
            result = self.runner.invoke(
                app,
                [
                    "merge",
                    "https://github.com/owner/repo/pull/22",
                    "--no-confirm",
                    "--token",
                    "test_token",
                ],
            )

            # Debug output
            if result.exit_code != 0:
                print(f"Exit code: {result.exit_code}")
                print(f"Stdout: {result.stdout}")
                if result.exception:
                    print(f"Exception: {result.exception}")
                    import traceback

                    print(
                        f"Traceback: {traceback.format_exception(type(result.exception), result.exception, result.exception.__traceback__)}"
                    )

            assert result.exit_code == 0
            assert "No similar PRs found" in result.stdout
            # Check for merge message (may include Rich color codes)
            assert "22" in result.stdout
            # Check for merge success message (may include Rich color codes)
            assert "✅ Merged:" in result.stdout
            assert "1" in result.stdout and "PRs" in result.stdout

            # Test passes if we reach this point - the merge was successful
        # The mocking prevented actual HTTP calls and the CLI completed successfully
        # Check that the PR URL appears in the success message
        assert "https://github.com/owner/repo/pull/22" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    def test_merge_command_non_automation_pr_no_override(self, mock_client_class):
        """Test that non-automation PR without override shows SHA and exits."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = False

        mock_pr = PullRequestInfo(
            number=22,
            title="Fix bug in authentication",
            body="Test body",
            author="human-user",
            head_sha="abc123",
            base_branch="main",
            head_branch="fix-bug",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )
        mock_client.get_pull_request_info.return_value = mock_pr
        mock_client.get_pull_request_commits.return_value = [
            "Fix bug in authentication\n\nDetailed description"
        ]
        mock_client.get_pr_status_details.return_value = "Ready to merge"

        result = self.runner.invoke(
            app,
            ["merge", "https://github.com/owner/repo/pull/22", "--token", "test_token"],
        )

        assert result.exit_code == 0
        assert "not from a recognized automation tool" in result.stdout
        assert "--override" in result.stdout
        assert "human-user" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    def test_merge_command_non_automation_pr_invalid_override(self, mock_client_class):
        """Test that non-automation PR with invalid override SHA fails."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = False

        mock_pr = PullRequestInfo(
            number=22,
            title="Fix bug in authentication",
            body="Test body",
            author="human-user",
            head_sha="abc123",
            base_branch="main",
            head_branch="fix-bug",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )
        mock_client.get_pull_request_info.return_value = mock_pr
        mock_client.get_pull_request_commits.return_value = [
            "Fix bug in authentication\n\nDetailed description"
        ]
        mock_client.get_pr_status_details.return_value = "Ready to merge"

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo/pull/22",
                "--token",
                "test_token",
                "--override",
                "wrongsha",
            ],
        )

        assert result.exit_code == 8
        assert "Invalid override SHA" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.PRComparator")
    @patch("dependamerge.github_service.GitHubService")
    def test_merge_command_non_automation_pr_valid_override(
        self, mock_service_class, mock_comparator_class, mock_client_class
    ):
        """Test that non-automation PR with valid override SHA proceeds."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_comparator = Mock()
        mock_comparator_class.return_value = mock_comparator

        mock_service = Mock()
        mock_service_class.return_value = mock_service

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = False

        mock_pr = PullRequestInfo(
            number=22,
            title="Fix bug in authentication",
            body="Test body",
            author="human-user",
            head_sha="abc123",
            base_branch="main",
            head_branch="fix-bug",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )

        mock_client.get_pull_request_info.return_value = mock_pr
        mock_client.get_pull_request_commits.return_value = [
            "Fix bug in authentication\n\nDetailed description"
        ]
        mock_client.get_organization_repositories.return_value = []
        mock_client.get_pr_status_details.return_value = "Ready to merge"
        mock_client.approve_pull_request.return_value = True
        mock_client.merge_pull_request.return_value = True

        # Mock the GitHubService.find_similar_prs method as async to return no similar PRs
        async def mock_find_similar_prs(*args, **kwargs):
            return []

        async def mock_close():
            return None

        mock_service.find_similar_prs = mock_find_similar_prs
        mock_service.close = mock_close

        # Calculate the expected SHA for this test case
        combined_data = "human-user:Fix bug in authentication"
        expected_sha = hashlib.sha256(combined_data.encode("utf-8")).hexdigest()[:16]

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo/pull/22",
                "--token",
                "test_token",
                "--override",
                expected_sha,
            ],
        )

        assert result.exit_code == 0
        assert "Override SHA validated" in result.stdout

    def test_generate_override_sha(self):
        """Test SHA generation functionality."""
        mock_pr = PullRequestInfo(
            number=22,
            title="Fix bug in authentication",
            body="Test body",
            author="human-user",
            head_sha="abc123",
            base_branch="main",
            head_branch="fix-bug",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )

        commit_message = "Fix bug in authentication"
        sha = _generate_override_sha(mock_pr, commit_message)

        # Check that SHA is generated and has expected length
        assert len(sha) == 16
        assert isinstance(sha, str)

        # Check that same inputs generate same SHA
        sha2 = _generate_override_sha(mock_pr, commit_message)
        assert sha == sha2

        # Check that different inputs generate different SHA
        mock_pr2 = mock_pr.model_copy()
        mock_pr2.author = "different-user"
        sha3 = _generate_override_sha(mock_pr2, commit_message)
        assert sha != sha3

    def test_validate_override_sha(self):
        """Test SHA validation functionality."""
        mock_pr = PullRequestInfo(
            number=22,
            title="Fix bug in authentication",
            body="Test body",
            author="human-user",
            head_sha="abc123",
            base_branch="main",
            head_branch="fix-bug",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )

        commit_message = "Fix bug in authentication"
        correct_sha = _generate_override_sha(mock_pr, commit_message)

        # Valid SHA should validate successfully
        assert _validate_override_sha(correct_sha, mock_pr, commit_message) is True

        # Invalid SHA should fail validation
        assert _validate_override_sha("invalid_sha", mock_pr, commit_message) is False
        assert _validate_override_sha("", mock_pr, commit_message) is False

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.PRComparator")
    @patch("dependamerge.github_service.GitHubService")
    @patch("dependamerge.merge_manager.GitHubAsync")
    def test_merge_command_no_confirm_flag(
        self,
        mock_async_class,
        mock_service_class,
        mock_comparator_class,
        mock_client_class,
    ):
        """Test that --no-confirm flag skips confirmation and merges immediately."""
        # Setup mocks
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_comparator = Mock()
        mock_comparator_class.return_value = mock_comparator

        mock_service = Mock()
        mock_service_class.return_value = mock_service

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = True

        # Mock a repository with no similar PRs to test direct merge
        mock_repo = Mock()
        mock_repo.full_name = "owner/other-repo"
        mock_client.get_organization_repositories.return_value = [mock_repo]
        mock_client.get_open_pull_requests.return_value = []

        mock_pr = PullRequestInfo(
            number=22,
            title="Bump requests from 2.28.0 to 2.28.1",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="dependabot/pip/requests-2.28.1",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )

        mock_client.get_pull_request_info.return_value = mock_pr
        mock_client.get_pr_status_details.return_value = "Ready to merge"

        # Mock GitHubAsync for AsyncMergeManager
        mock_github_async = Mock()
        mock_async_class.return_value = mock_github_async
        mock_github_async.__aenter__ = AsyncMock(return_value=mock_github_async)
        mock_github_async.__aexit__ = AsyncMock(return_value=None)
        mock_github_async.merge_pull_request = AsyncMock(return_value=True)

        # Mock no similar PRs found
        async def mock_find_similar_prs(*args, **kwargs):
            return []

        async def mock_close():
            return None

        mock_service.find_similar_prs = mock_find_similar_prs
        mock_service.close = mock_close

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo/pull/22",
                "--no-confirm",
                "--token",
                "test_token",
            ],
        )

        assert result.exit_code == 0
        # Should NOT contain the interactive evaluation message
        assert "Dependamerge Evaluation" not in result.stdout
        # Should show direct merge output
        assert "📈 Final Results:" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.PRComparator")
    @patch("dependamerge.github_service.GitHubService")
    def test_close_command_automation_pr(
        self, mock_service_class, mock_comparator_class, mock_client_class
    ):
        """Test close command with automation PR."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_comparator = Mock()
        mock_comparator_class.return_value = mock_comparator

        mock_service = Mock()
        mock_service_class.return_value = mock_service

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = True

        mock_pr = PullRequestInfo(
            number=22,
            title="Bump package from 1.0 to 2.0",
            body="Bumps package from 1.0 to 2.0",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="dependabot/package",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )

        mock_client.get_pull_request_info.return_value = mock_pr
        mock_client.get_pr_status_details.return_value = "Ready to merge"

        # Mock async methods
        async def mock_find_similar_prs(*args, **kwargs):
            return []

        async def mock_close():
            return None

        mock_service.find_similar_prs = mock_find_similar_prs
        mock_service.close = mock_close

        result = self.runner.invoke(
            app,
            [
                "close",
                "https://github.com/owner/repo/pull/22",
                "--no-confirm",
                "--token",
                "test_token",
            ],
        )

        assert result.exit_code == 0
        assert "closed" in result.stdout.lower()

    @patch("dependamerge.cli.GitHubClient")
    def test_close_command_non_automation_pr_no_override(self, mock_client_class):
        """Test that close command for non-automation PR without override shows SHA."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = False

        mock_pr = PullRequestInfo(
            number=22,
            title="Manual update",
            body="Test body",
            author="human-user",
            head_sha="abc123",
            base_branch="main",
            head_branch="fix-bug",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )
        mock_client.get_pull_request_info.return_value = mock_pr
        mock_client.get_pull_request_commits.return_value = ["Manual update"]
        mock_client.get_pr_status_details.return_value = "Ready"

        result = self.runner.invoke(
            app,
            ["close", "https://github.com/owner/repo/pull/22", "--token", "test_token"],
        )

        assert result.exit_code == 0
        assert "not from a recognized automation tool" in result.stdout
        assert "--override" in result.stdout


class TestFormatFailureReason:
    """Tests for the failed-PR summary reason formatter."""

    def test_plain_reason_is_unchanged(self):
        assert _format_failure_reason("merge conflicts") == ["merge conflicts"]

    def test_ruleset_not_satisfied_is_expanded(self):
        reason = (
            "Repository rule violations found Required workflows "
            "'Autolabeler, Semantic Pull Request 🛠️, "
            "Audit GitHub Actions 📌' are not satisfied"
        )
        assert _format_failure_reason(reason) == [
            "Repository rule violations found / Required workflows not satisfied",
            "• Autolabeler",
            "• Semantic Pull Request 🛠️",
            "• Audit GitHub Actions 📌",
        ]

    def test_ruleset_failed_variant_uses_failed_header(self):
        reason = (
            "Repository rule violations found Required workflows 'Autolabeler' failed"
        )
        assert _format_failure_reason(reason) == [
            "Repository rule violations found / Required workflows failed",
            "• Autolabeler",
        ]

    def test_ruleset_status_check_failing_is_expanded(self):
        # Single-line GitHub status-check violation -> type line + bullet,
        # consistent with the Required workflows shape.
        reason = (
            "Repository rule violations found Required status check "
            '"pre-commit.ci - pr" is failing.'
        )
        assert _format_failure_reason(reason) == [
            "Repository rule violations found / Required status checks failed",
            "• pre-commit.ci - pr",
        ]

    def test_ruleset_status_check_multiple_names(self):
        reason = (
            "Repository rule violations found Required status checks "
            '"lint", "build" are failing.'
        )
        assert _format_failure_reason(reason) == [
            "Repository rule violations found / Required status checks failed",
            "• lint",
            "• build",
        ]

    def test_ruleset_status_check_not_satisfied_variant(self):
        # A pending/expected (non-failing) required check uses the
        # "not satisfied" verb rather than "failed".
        reason = (
            "Repository rule violations found Required status check "
            '"pre-commit.ci - pr" is expected.'
        )
        assert _format_failure_reason(reason) == [
            "Repository rule violations found / Required status checks not satisfied",
            "• pre-commit.ci - pr",
        ]

    def test_non_ruleset_message_left_alone(self):
        # Without the ruleset prefix, leave the reason unchanged.
        reason = "Required workflows 'X' are not satisfied"
        assert _format_failure_reason(reason) == [reason]


def _make_merge_context(show_progress: bool) -> _MergeContext:
    """Build a minimal ``_MergeContext`` for tracker-lifecycle tests."""
    ctx = _MergeContext(
        pr_url="https://github.com/owner/repo/pull/1",
        no_confirm=True,
        similarity_threshold=0.8,
        merge_method="merge",
        token="fake-token",
        override=None,
        no_fix=False,
        merge_timeout=300.0,
        show_progress=show_progress,
        debug_matching=False,
        dismiss_copilot=False,
        force="none",
        verbose=False,
        no_netrc=False,
        netrc_file=None,
        netrc_optional=True,
        github2gerrit_mode="ignore",
    )
    ctx.owner = "owner"
    ctx.repo_name = "repo"
    return ctx


class TestRestartMergeProgressTracker:
    """``_restart_merge_progress_tracker`` revives the merge-phase display.

    The org-wide scan stops the tracker it used (tearing down its Rich
    ``Live``).  Reusing that stopped tracker for the real merge means
    the background wait-status ticker pushes updates into a dead
    ``Live`` — silently dropped — so the user sees no countdown while
    PRs sit in the Step 5.5 auto-merge wait.  This helper must stand up
    a fresh, *started* tracker so the ticker has somewhere to render.
    """

    def test_replaces_stopped_tracker_with_started_one(self) -> None:
        from dependamerge.progress_tracker import MergeProgressTracker

        ctx = _make_merge_context(show_progress=True)
        # Simulate the post-scan state: a tracker that has been stopped
        # (its Live torn down -> ``live is None``).
        stopped = MergeProgressTracker("owner")
        stopped.start()
        stopped.stop()
        assert stopped.live is None
        ctx.progress_tracker = stopped

        _restart_merge_progress_tracker(ctx, total_prs=3)

        # A brand-new tracker is installed and started.
        assert ctx.progress_tracker is not stopped
        assert ctx.progress_tracker is not None
        assert ctx.progress_tracker.total_prs == 3
        # When Rich is available the Live display is active again; when
        # it is not (e.g. non-TTY CI), ``start()`` is a no-op and there
        # is nothing to assert about ``live``.
        if ctx.progress_tracker.rich_available:
            assert ctx.progress_tracker.live is not None
            # Avoid leaking an active Rich Live into other tests.
            ctx.progress_tracker.stop()

    def test_no_op_when_progress_disabled(self) -> None:
        ctx = _make_merge_context(show_progress=False)
        ctx.progress_tracker = None

        _restart_merge_progress_tracker(ctx, total_prs=5)

        # ``--no-progress`` keeps the tracker absent; the plain-text
        # ticker provides feedback instead.
        assert ctx.progress_tracker is None


class TestMergeDryRun:
    """``merge --dry-run`` previews without writing or checking scopes.

    Dry run is the mode the CI test matrix uses: it must run under a
    read-only token (so the write-permission pre-flight is skipped) and
    must never execute a real merge (so ``_run_parallel_merge`` is always
    asked for a preview).
    """

    def setup_method(self):
        self.runner = CliRunner()

    @patch("dependamerge.cli._run_parallel_merge")
    @patch("dependamerge.cli._check_merge_permissions")
    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.PRComparator")
    @patch("dependamerge.github_service.GitHubService")
    def test_dry_run_skips_permission_check_and_previews(
        self,
        mock_service_class,
        mock_comparator_class,
        mock_client_class,
        mock_check,
        mock_run_parallel,
    ):
        from dependamerge.merge_manager import MergeResult, MergeStatus

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_comparator_class.return_value = Mock()
        mock_service = Mock()
        mock_service_class.return_value = mock_service

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = True
        mock_client.get_pr_status_details.return_value = "Ready to merge"

        source_pr = PullRequestInfo(
            number=22,
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
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )
        mock_client.get_pull_request_info.return_value = source_pr

        async def mock_find_similar_prs(*args, **kwargs):
            return []

        async def mock_close():
            return None

        mock_service.find_similar_prs = mock_find_similar_prs
        mock_service.close = mock_close

        captured: dict[str, object] = {}

        def _capture(ctx, prs, *, preview, **kwargs):
            captured["preview"] = preview
            return [MergeResult(pr_info=source_pr, status=MergeStatus.MERGED)]

        mock_run_parallel.side_effect = _capture

        result = self.runner.invoke(
            app,
            [
                "merge",
                "--dry-run",
                "https://github.com/owner/repo/pull/22",
                "--token",
                "test_token",
            ],
        )

        assert result.exit_code == 0, result.stdout
        # The write-permission pre-flight must be skipped under dry run.
        mock_check.assert_not_called()
        # The merge must run in preview mode (no real merge).
        assert captured.get("preview") is True
        assert "Dry run" in result.stdout

    @patch("dependamerge.cli._run_parallel_merge")
    @patch("dependamerge.cli._check_merge_permissions")
    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.PRComparator")
    @patch("dependamerge.github_service.GitHubService")
    def test_dry_run_forces_preview_even_with_no_confirm(
        self,
        mock_service_class,
        mock_comparator_class,
        mock_client_class,
        mock_check,
        mock_run_parallel,
    ):
        """``--dry-run --no-confirm`` must still preview, never merge."""
        from dependamerge.merge_manager import MergeResult, MergeStatus

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_comparator_class.return_value = Mock()
        mock_service = Mock()
        mock_service_class.return_value = mock_service

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = True
        mock_client.get_pr_status_details.return_value = "Ready to merge"

        source_pr = PullRequestInfo(
            number=22,
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
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )
        mock_client.get_pull_request_info.return_value = source_pr

        async def mock_find_similar_prs(*args, **kwargs):
            return []

        async def mock_close():
            return None

        mock_service.find_similar_prs = mock_find_similar_prs
        mock_service.close = mock_close

        captured: dict[str, object] = {}

        def _capture(ctx, prs, *, preview, **kwargs):
            captured["preview"] = preview
            return [MergeResult(pr_info=source_pr, status=MergeStatus.MERGED)]

        mock_run_parallel.side_effect = _capture

        result = self.runner.invoke(
            app,
            [
                "merge",
                "--dry-run",
                "--no-confirm",
                "https://github.com/owner/repo/pull/22",
                "--token",
                "test_token",
            ],
        )

        assert result.exit_code == 0, result.stdout
        mock_check.assert_not_called()
        assert captured.get("preview") is True
        assert "Dry run" in result.stdout


class TestCloseDryRun:
    """``close --dry-run`` previews without closing and without prompting."""

    def setup_method(self):
        self.runner = CliRunner()

    @patch("dependamerge.cli.AsyncCloseManager")
    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.PRComparator")
    @patch("dependamerge.github_service.GitHubService")
    def test_close_dry_run_previews_without_closing(
        self,
        mock_service_class,
        mock_comparator_class,
        mock_client_class,
        mock_close_manager_class,
    ):
        from dependamerge.close_manager import CloseResult, CloseStatus

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.token = "test_token"
        mock_comparator_class.return_value = Mock()
        mock_service = Mock()
        mock_service_class.return_value = mock_service

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = True
        mock_client.get_pr_status_details.return_value = "Ready to merge"

        source_pr = PullRequestInfo(
            number=22,
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
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/22",
        )
        mock_client.get_pull_request_info.return_value = source_pr

        async def mock_find_similar_prs(*args, **kwargs):
            return []

        async def mock_close():
            return None

        mock_service.find_similar_prs = mock_find_similar_prs
        mock_service.close = mock_close

        captured: dict[str, object] = {}

        # Stub the async context-manager close manager so we can assert
        # it is only ever asked to *preview* (never to actually close).
        close_manager = AsyncMock()
        close_manager.close_prs_parallel = AsyncMock(
            return_value=[CloseResult(pr_info=source_pr, status=CloseStatus.CLOSED)]
        )

        def _capture_close_manager(*args, **kwargs):
            captured["preview_mode"] = kwargs.get("preview_mode")
            return close_manager

        mock_close_manager_class.side_effect = _capture_close_manager

        result = self.runner.invoke(
            app,
            [
                "close",
                "--dry-run",
                "--no-progress",
                "https://github.com/owner/repo/pull/22",
                "--token",
                "test_token",
            ],
        )

        assert result.exit_code == 0, result.stdout
        assert "Dry run" in result.stdout
        # The close manager must have been constructed in preview mode.
        assert captured.get("preview_mode") is True
