# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from unittest.mock import Mock, patch

from typer.testing import CliRunner

from dependamerge.cli import app
from dependamerge.models import PullRequestInfo


class TestCLI:
    def setup_method(self):
        self.runner = CliRunner()

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.PRComparator")
    def test_merge_command_dry_run(self, mock_comparator_class, mock_client_class):
        # Setup mocks
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_comparator = Mock()
        mock_comparator_class.return_value = mock_comparator

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

        mock_client.get_pull_request_info.side_effect = get_pr_info_side_effect
        mock_client.get_pr_status_details.return_value = "Ready to merge"

        # Mock comparison result
        from dependamerge.models import ComparisonResult

        comparison_result = ComparisonResult(
            is_similar=True,
            confidence_score=0.95,
            reasons=["Same title pattern", "Same author"],
        )
        mock_comparator.compare_pull_requests.return_value = comparison_result

        result = self.runner.invoke(
            app,
            [
                "https://github.com/owner/repo/pull/22",
                "--dry-run",
                "--token",
                "test_token",
            ],
        )

        assert result.exit_code == 0
        assert "Dry run mode" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    def test_merge_command_invalid_url(self, mock_client_class):
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.parse_pr_url.side_effect = ValueError("Invalid GitHub PR URL")

        result = self.runner.invoke(
            app, ["https://invalid-url.com", "--token", "test_token"]
        )

        assert result.exit_code == 1
        assert "Error:" in result.stdout

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

        result = self.runner.invoke(
            app, ["https://github.com/owner/repo/pull/22", "--token", "test_token"]
        )

        assert result.exit_code == 1
        assert "not from a recognized automation tool" in result.stdout
