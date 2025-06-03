# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Test to demonstrate that the dependamerge tool now merges the source PR
when no similar PRs are found in the organization.
"""

from unittest.mock import Mock, patch

from dependamerge.cli import merge
from dependamerge.models import PullRequestInfo


def test_final_verification():
    """Final test to demonstrate the fix is working"""
    with patch("dependamerge.cli.GitHubClient") as mock_client_class, patch(
        "dependamerge.cli.PRComparator"
    ) as mock_comparator_class:
        # Setup mocks
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        mock_comparator = Mock()
        mock_comparator_class.return_value = mock_comparator

        mock_client.parse_pr_url.return_value = ("owner", "repo", 22)
        mock_client.is_automation_author.return_value = True

        # Mock repository with no similar PRs
        mock_repo = Mock()
        mock_repo.full_name = "owner/other-repo"
        mock_repo.owner.login = "owner"
        mock_repo.name = "other-repo"
        mock_client.get_organization_repositories.return_value = [mock_repo]

        # Mock no open PRs (no similar PRs found)
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

        # Mock approve and merge methods
        mock_client.approve_pull_request.return_value = True
        mock_client.merge_pull_request.return_value = True
        mock_client.fix_out_of_date_pr.return_value = True

        # Run the CLI command
        merge(
            pr_url="https://github.com/owner/repo/pull/22",
            dry_run=False,
            similarity_threshold=0.8,
            merge_method="merge",
            token="test_token",
            fix=False,
        )

        # Verify that the source PR was approved and merged
        mock_client.approve_pull_request.assert_called_with("owner", "repo", 22)
        mock_client.merge_pull_request.assert_called_with("owner", "repo", 22, "merge")

        print("✅ SUCCESS: Source PR was merged when no similar PRs were found!")
        print("✅ Fix is working correctly!")


if __name__ == "__main__":
    try:
        test_final_verification()
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
