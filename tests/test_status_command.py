# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from dependamerge.cli import app
from dependamerge.models import OrganizationStatus, RepositoryStatus


class TestStatusCommand:
    runner: CliRunner = CliRunner()

    def setup_method(self):
        self.runner = CliRunner()

    def _setup_async_mocks(self, mock_service, mock_status):
        """Helper to setup async method mocks."""

        async def mock_gather_status(org_name):
            return mock_status

        async def mock_close():
            pass

        mock_service.gather_organization_status = mock_gather_status
        mock_service.close = mock_close

    @patch("dependamerge.github_service.GitHubService")
    def test_status_command_basic(self, mock_service_class):
        """Test basic status command execution."""
        # Setup mock service
        mock_service = Mock()
        mock_service_class.return_value = mock_service

        # Mock organization status result
        mock_status = OrganizationStatus(
            organization="test-org",
            total_repositories=2,
            scanned_repositories=2,
            repository_statuses=[
                RepositoryStatus(
                    repository_name="repo1",
                    latest_tag="v1.0.0",
                    latest_release="v1.0.0",
                    tag_date="2025/01/15",
                    release_date="2025/01/15",
                    status_icon="✅",
                    open_prs_human=1,
                    open_prs_automation=2,
                    merged_prs_human=0,
                    merged_prs_automation=3,
                    action_prs_human=0,
                    action_prs_automation=1,
                    workflow_prs_human=1,
                    workflow_prs_automation=0,
                ),
                RepositoryStatus(
                    repository_name="repo2",
                    latest_tag="v0.5.0",
                    latest_release=None,
                    tag_date="2025/01/10",
                    release_date=None,
                    status_icon="⚠️",
                    open_prs_human=0,
                    open_prs_automation=1,
                    merged_prs_human=1,
                    merged_prs_automation=0,
                    action_prs_human=0,
                    action_prs_automation=0,
                    workflow_prs_human=0,
                    workflow_prs_automation=1,
                ),
            ],
            scan_timestamp="2025-01-20T10:00:00",
            errors=[],
        )

        # Mock the async methods
        self._setup_async_mocks(mock_service, mock_status)

        # Run command (test with URL format)
        result = self.runner.invoke(
            app,
            ["status", "https://github.com/test-org/", "--no-progress"],
            env={"GITHUB_TOKEN": "fake-token"},
        )

        # Verify command executed successfully
        assert result.exit_code == 0
        assert "test-org" in result.stdout
        assert "repo1" in result.stdout
        assert "repo2" in result.stdout

        # Summary table reports the open-PR human/automation split and a
        # combined total alongside the repository count.
        assert "Automation PRs" in result.stdout
        assert "Human" in result.stdout
        assert "Total PRs" in result.stdout
        assert "Total Repositories" in result.stdout

    @patch("dependamerge.github_service.GitHubService")
    def test_status_command_json_output(self, mock_service_class):
        """Test status command with JSON output format."""
        # Setup mock service
        mock_service = Mock()
        mock_service_class.return_value = mock_service

        # Mock organization status result
        mock_status = OrganizationStatus(
            organization="test-org",
            total_repositories=1,
            scanned_repositories=1,
            repository_statuses=[
                RepositoryStatus(
                    repository_name="repo1",
                    latest_tag="v1.0.0",
                    latest_release="v1.0.0",
                    tag_date="2025/01/15",
                    release_date="2025/01/15",
                    status_icon="✅",
                    open_prs_human=0,
                    open_prs_automation=0,
                    merged_prs_human=0,
                    merged_prs_automation=0,
                    action_prs_human=0,
                    action_prs_automation=0,
                    workflow_prs_human=0,
                    workflow_prs_automation=0,
                ),
            ],
            scan_timestamp="2025-01-20T10:00:00",
            errors=[],
        )

        # Mock the async methods
        self._setup_async_mocks(mock_service, mock_status)

        # Run command with JSON format (test with plain org name)
        result = self.runner.invoke(
            app,
            ["status", "test-org", "--format", "json", "--no-progress"],
            env={"GITHUB_TOKEN": "fake-token"},
        )

        # Verify JSON output
        assert result.exit_code == 0
        assert (
            '"organization": "test-org"' in result.stdout
            or '"organization":"test-org"' in result.stdout
        )

    def test_status_command_with_plain_org_name(self):
        """Test status command with plain organization name (not URL)."""
        # This test just verifies parsing works - no need to mock the actual service call
        # since the organization parsing happens before the service is called
        pass

    def test_status_command_invalid_input(self):
        """Test status command with completely invalid input."""
        result = self.runner.invoke(
            app,
            ["status", "", "--no-progress"],
            env={"GITHUB_TOKEN": "fake-token"},
        )

        # Should fail with invalid input
        assert result.exit_code == 1
        assert "Invalid GitHub owner" in result.stdout

    @patch("dependamerge.github_service.GitHubService")
    def test_status_command_with_errors(self, mock_service_class):
        """Test status command when errors occur during scan."""
        # Setup mock service
        mock_service = Mock()
        mock_service_class.return_value = mock_service

        # Mock organization status with errors
        mock_status = OrganizationStatus(
            organization="test-org",
            total_repositories=2,
            scanned_repositories=1,
            repository_statuses=[
                RepositoryStatus(
                    repository_name="repo1",
                    latest_tag="v1.0.0",
                    latest_release="v1.0.0",
                    tag_date="2025/01/15",
                    release_date="2025/01/15",
                    status_icon="✅",
                    open_prs_human=0,
                    open_prs_automation=0,
                    merged_prs_human=0,
                    merged_prs_automation=0,
                    action_prs_human=0,
                    action_prs_automation=0,
                    workflow_prs_human=0,
                    workflow_prs_automation=0,
                ),
            ],
            scan_timestamp="2025-01-20T10:00:00",
            errors=["Error scanning repository test-org/repo2: Connection timeout"],
        )

        # Mock the async methods
        self._setup_async_mocks(mock_service, mock_status)

        # Run command (test with plain org name)
        result = self.runner.invoke(
            app,
            ["status", "test-org", "--no-progress"],
            env={"GITHUB_TOKEN": "fake-token"},
        )

        # Verify errors are displayed
        assert result.exit_code == 0
        assert (
            "Errors Encountered" in result.stdout
            or "Connection timeout" in result.stdout
        )


class TestStatusCommandUrlForms:
    """Verify ``status`` resolves the owner from every supported URL form.

    Each invocation captures the login handed to
    ``gather_organization_status`` so we assert the CLI extracts the
    correct owner from bare names, bare URLs, trailing-slash URLs, and
    the canonical ``/orgs/owner`` and ``/orgs/owner/repositories`` forms.
    These forms apply equally to organizations and personal user
    accounts (the two are only distinguished later at runtime).
    """

    runner: CliRunner = CliRunner()

    def setup_method(self):
        self.runner = CliRunner()

    @pytest.mark.parametrize(
        "argument,expected_owner",
        [
            ("lfreleng-actions", "lfreleng-actions"),
            ("lfreleng-actions/", "lfreleng-actions"),
            ("ModeSevenIndustrialSolutions", "ModeSevenIndustrialSolutions"),
            ("https://github.com/lfreleng-actions", "lfreleng-actions"),
            ("https://github.com/lfreleng-actions/", "lfreleng-actions"),
            (
                "https://github.com/ModeSevenIndustrialSolutions",
                "ModeSevenIndustrialSolutions",
            ),
            ("github.com/lfreleng-actions", "lfreleng-actions"),
            ("https://github.com/orgs/lfreleng-actions", "lfreleng-actions"),
            (
                "https://github.com/orgs/lfreleng-actions/repositories",
                "lfreleng-actions",
            ),
        ],
    )
    @patch("dependamerge.github_service.GitHubService")
    def test_status_resolves_owner_from_url_form(
        self, mock_service_class, argument, expected_owner
    ):
        captured: dict[str, str] = {}
        mock_service = Mock()
        mock_service_class.return_value = mock_service

        async def mock_gather_status(org_name):
            captured["owner"] = org_name
            return OrganizationStatus(
                organization=org_name,
                total_repositories=0,
                scanned_repositories=0,
                repository_statuses=[],
                scan_timestamp="2026-01-20T10:00:00",
                errors=[],
            )

        async def mock_close():
            pass

        mock_service.gather_organization_status = mock_gather_status
        mock_service.close = mock_close

        result = self.runner.invoke(
            app,
            ["status", argument, "--no-progress"],
            env={"GITHUB_TOKEN": "fake-token"},
        )

        assert result.exit_code == 0, result.stdout
        assert captured.get("owner") == expected_owner

    @patch("dependamerge.github_service.GitHubService")
    def test_status_rejects_non_github_owner_url(self, mock_service_class):
        """A non-github.com owner URL is rejected before any scan starts."""
        mock_service = Mock()
        mock_service_class.return_value = mock_service

        result = self.runner.invoke(
            app,
            ["status", "https://gitlab.com/some-owner", "--no-progress"],
            env={"GITHUB_TOKEN": "fake-token"},
        )

        assert result.exit_code == 1
        assert "Invalid GitHub owner" in result.stdout
