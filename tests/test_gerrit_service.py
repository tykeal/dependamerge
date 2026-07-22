# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for Gerrit service layer.

This module tests the GerritService class for querying changes,
finding similar changes, and handling pagination.
"""

from unittest.mock import MagicMock, patch

import pytest

from dependamerge.gerrit.comparator import GerritChangeComparator
from dependamerge.gerrit.models import (
    GerritChangeInfo,
    GerritComparisonResult,
    GerritFileChange,
)
from dependamerge.gerrit.service import (
    DEFAULT_CHANGE_OPTIONS,
    DEFAULT_LIST_OPTIONS,
    GerritService,
    GerritServiceError,
    create_gerrit_service,
)


@pytest.fixture
def mock_client():
    """Create a mock Gerrit REST client."""
    client = MagicMock()
    client.is_authenticated = False
    return client


@pytest.fixture
def sample_change_data():
    """Sample Gerrit change API response data."""
    return {
        "_number": 12345,
        "change_id": "I1234567890abcdef",
        "project": "my-project",
        "subject": "Chore: Bump actions/checkout from 4.1.0 to 4.2.0",
        "branch": "main",
        "status": "NEW",
        "submittable": True,
        "owner": {"username": "dependabot", "email": "bot@example.com"},
        "current_revision": "abc123",
        "revisions": {
            "abc123": {
                "commit": {
                    "message": "Chore: Bump actions/checkout from 4.1.0 to 4.2.0"
                },
                "files": {
                    ".github/workflows/ci.yml": {
                        "status": "M",
                        "lines_inserted": 1,
                        "lines_deleted": 1,
                    }
                },
            }
        },
        "labels": {
            "Code-Review": {},
            "Verified": {},
        },
        "created": "2024-01-15 10:00:00.000000000",
        "updated": "2024-01-15 12:00:00.000000000",
    }


@pytest.fixture
def sample_change_info():
    """Sample GerritChangeInfo instance."""
    return GerritChangeInfo(
        number=12345,
        change_id="I1234567890abcdef",
        project="my-project",
        subject="Chore: Bump actions/checkout from 4.1.0 to 4.2.0",
        owner="dependabot",
        branch="main",
        status="NEW",
        submittable=True,
        files_changed=[
            GerritFileChange(
                filename=".github/workflows/ci.yml",
                status="M",
                lines_inserted=1,
                lines_deleted=1,
            )
        ],
    )


class TestGerritServiceInit:
    """Tests for GerritService initialization."""

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_basic_init(self, mock_url_builder, mock_build_client, mock_client):
        """Test basic service initialization."""
        mock_build_client.return_value = mock_client

        service = GerritService(host="gerrit.example.org")

        assert service.host == "gerrit.example.org"
        assert service.base_path is None
        mock_build_client.assert_called_once()

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_init_with_base_path(
        self, mock_url_builder, mock_build_client, mock_client
    ):
        """Test service initialization with base path."""
        mock_build_client.return_value = mock_client

        service = GerritService(host="gerrit.example.org", base_path="infra")

        assert service.host == "gerrit.example.org"
        assert service.base_path == "infra"

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_init_with_credentials(
        self, mock_url_builder, mock_build_client, mock_client
    ):
        """Test service initialization with credentials."""
        mock_client.is_authenticated = True
        mock_build_client.return_value = mock_client

        service = GerritService(
            host="gerrit.example.org",
            username="testuser",
            password="testpass",
        )

        assert service.is_authenticated is True

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_url_builder_property(
        self, mock_url_builder, mock_build_client, mock_client
    ):
        """Test url_builder property."""
        mock_build_client.return_value = mock_client
        mock_builder_instance = MagicMock()
        mock_url_builder.return_value = mock_builder_instance

        service = GerritService(host="gerrit.example.org")

        assert service.url_builder == mock_builder_instance


class TestGerritServiceGetChangeInfo:
    """Tests for get_change_info method."""

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_change_info_success(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_data,
    ):
        """Test successful change info fetch."""
        mock_build_client.return_value = mock_client
        # Return change data for first call, mergeable data for second call
        mock_client.get.side_effect = [
            sample_change_data,
            {"mergeable": True, "submit_type": "MERGE_IF_NECESSARY"},
        ]

        service = GerritService(host="gerrit.example.org")
        change = service.get_change_info(12345)

        assert change.number == 12345
        assert change.project == "my-project"
        # Should be called twice: once for change info, once for mergeable status
        assert mock_client.get.call_count == 2

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_change_info_with_options(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_data,
    ):
        """Test change info fetch with custom options."""
        mock_build_client.return_value = mock_client
        # Return change data for first call, mergeable data for second call
        mock_client.get.side_effect = [
            sample_change_data,
            {"mergeable": True, "submit_type": "MERGE_IF_NECESSARY"},
        ]

        service = GerritService(host="gerrit.example.org")
        service.get_change_info(12345, options=["CURRENT_REVISION"])

        # First call should be for change info with options
        first_call_args = mock_client.get.call_args_list[0][0][0]
        assert "o=CURRENT_REVISION" in first_call_args

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_change_info_without_mergeable_check(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_data,
    ):
        """Test change info fetch with check_mergeable=False."""
        mock_build_client.return_value = mock_client
        mock_client.get.return_value = sample_change_data

        service = GerritService(host="gerrit.example.org")
        change = service.get_change_info(12345, check_mergeable=False)

        assert change.number == 12345
        # Should only be called once when check_mergeable=False
        mock_client.get.assert_called_once()

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_change_info_not_found(
        self, mock_url_builder, mock_build_client, mock_client
    ):
        """Test change info fetch when change not found."""
        from dependamerge.gerrit.client import GerritNotFoundError

        mock_build_client.return_value = mock_client
        mock_client.get.side_effect = GerritNotFoundError("Not found", 404)

        service = GerritService(host="gerrit.example.org")

        with pytest.raises(GerritNotFoundError):
            service.get_change_info(99999)

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_change_info_error(
        self, mock_url_builder, mock_build_client, mock_client
    ):
        """Test change info fetch with REST error."""
        from dependamerge.gerrit.client import GerritRestError

        mock_build_client.return_value = mock_client
        mock_client.get.side_effect = GerritRestError("Server error", 500)

        service = GerritService(host="gerrit.example.org")

        with pytest.raises(GerritServiceError, match="Failed to fetch change"):
            service.get_change_info(12345)


class TestGerritServiceGetOpenChanges:
    """Tests for get_open_changes method."""

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_open_changes_basic(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_data,
    ):
        """Test basic open changes query."""
        mock_build_client.return_value = mock_client
        mock_client.get.return_value = [sample_change_data]

        service = GerritService(host="gerrit.example.org")
        changes = service.get_open_changes()

        assert len(changes) == 1
        assert changes[0].number == 12345

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_open_changes_with_project(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_data,
    ):
        """Test open changes query filtered by project."""
        mock_build_client.return_value = mock_client
        mock_client.get.return_value = [sample_change_data]

        service = GerritService(host="gerrit.example.org")
        service.get_open_changes(project="my-project")

        call_args = mock_client.get.call_args[0][0]
        assert "project:my-project" in call_args

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_open_changes_with_branch(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_data,
    ):
        """Test open changes query filtered by branch."""
        mock_build_client.return_value = mock_client
        mock_client.get.return_value = [sample_change_data]

        service = GerritService(host="gerrit.example.org")
        service.get_open_changes(branch="main")

        call_args = mock_client.get.call_args[0][0]
        assert "branch:main" in call_args

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_open_changes_with_owner(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_data,
    ):
        """Test open changes query filtered by owner."""
        mock_build_client.return_value = mock_client
        mock_client.get.return_value = [sample_change_data]

        service = GerritService(host="gerrit.example.org")
        service.get_open_changes(owner="dependabot")

        call_args = mock_client.get.call_args[0][0]
        assert "owner:dependabot" in call_args

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_open_changes_empty_result(
        self, mock_url_builder, mock_build_client, mock_client
    ):
        """Test open changes query with empty result."""
        mock_build_client.return_value = mock_client
        mock_client.get.return_value = []

        service = GerritService(host="gerrit.example.org")
        changes = service.get_open_changes()

        assert changes == []


class TestGerritServicePagination:
    """Tests for pagination handling."""

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_pagination_multiple_pages(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
    ):
        """Test pagination fetches multiple pages."""
        mock_build_client.return_value = mock_client

        # Create 150 sample changes (page size is 100)
        page1 = [
            {
                "_number": i,
                "change_id": f"I{i:040d}",
                "project": "proj",
                "subject": "Test",
                "branch": "main",
                "status": "NEW",
                "owner": {"username": "user"},
            }
            for i in range(100)
        ]
        page2 = [
            {
                "_number": i,
                "change_id": f"I{i:040d}",
                "project": "proj",
                "subject": "Test",
                "branch": "main",
                "status": "NEW",
                "owner": {"username": "user"},
            }
            for i in range(100, 150)
        ]

        mock_client.get.side_effect = [page1, page2]

        service = GerritService(host="gerrit.example.org")
        changes = service.get_open_changes(limit=200)

        assert len(changes) == 150
        assert mock_client.get.call_count == 2

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_pagination_respects_limit(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
    ):
        """Test pagination respects the limit parameter."""
        mock_build_client.return_value = mock_client

        # Create more changes than the limit
        all_changes = [
            {
                "_number": i,
                "change_id": f"I{i:040d}",
                "project": "proj",
                "subject": "Test",
                "branch": "main",
                "status": "NEW",
                "owner": {"username": "user"},
            }
            for i in range(100)
        ]

        mock_client.get.return_value = all_changes

        service = GerritService(host="gerrit.example.org")
        changes = service.get_open_changes(limit=50)

        assert len(changes) == 50


class TestGerritServiceGetChangesByTopic:
    """Tests for get_changes_by_topic method."""

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_changes_by_topic(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_data,
    ):
        """Test fetching changes by topic."""
        mock_build_client.return_value = mock_client
        mock_client.get.return_value = [sample_change_data]

        service = GerritService(host="gerrit.example.org")
        _ = service.get_changes_by_topic("my-topic")

        call_args = mock_client.get.call_args[0][0]
        assert "topic:my-topic" in call_args
        assert "status:open" in call_args

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_changes_by_topic_include_merged(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_data,
    ):
        """Test fetching changes by topic including merged."""
        mock_build_client.return_value = mock_client
        mock_client.get.return_value = [sample_change_data]

        service = GerritService(host="gerrit.example.org")
        service.get_changes_by_topic("my-topic", include_merged=True)

        call_args = mock_client.get.call_args[0][0]
        assert "topic:my-topic" in call_args
        assert "status:merged" in call_args


class TestGerritServiceGetProjects:
    """Tests for get_projects method."""

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_projects(self, mock_url_builder, mock_build_client, mock_client):
        """Test fetching project list."""
        mock_build_client.return_value = mock_client
        mock_client.get.return_value = {
            "project-a": {},
            "project-b": {},
            "project-c": {},
        }

        service = GerritService(host="gerrit.example.org")
        projects = service.get_projects()

        assert projects == ["project-a", "project-b", "project-c"]

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_get_projects_error(self, mock_url_builder, mock_build_client, mock_client):
        """Test project fetch with error returns empty list."""
        from dependamerge.gerrit.client import GerritRestError

        mock_build_client.return_value = mock_client
        mock_client.get.side_effect = GerritRestError("Error", 500)

        service = GerritService(host="gerrit.example.org")
        projects = service.get_projects()

        assert projects == []


class TestGerritServiceFindSimilarChanges:
    """Tests for find_similar_changes method."""

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_find_similar_changes_with_comparator(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_info,
    ):
        """Test finding similar changes with a comparator."""
        mock_build_client.return_value = mock_client

        # Create another change to compare
        other_change_data = {
            "_number": 12346,
            "change_id": "I9876543210fedcba",
            "project": "other-project",
            "subject": "Chore: Bump actions/checkout from 4.1.0 to 4.2.0",
            "branch": "main",
            "status": "NEW",
            "owner": {"username": "dependabot"},
        }

        mock_client.get.return_value = [other_change_data]

        # Create a mock comparator
        mock_comparator = MagicMock()
        mock_comparator.compare_gerrit_changes.return_value = (
            GerritComparisonResult.similar(0.95, ["Same author", "Similar subject"])
        )

        service = GerritService(host="gerrit.example.org")
        similar = service.find_similar_changes(sample_change_info, mock_comparator)

        assert len(similar) == 1
        assert similar[0][0].number == 12346
        assert similar[0][1].confidence_score == 0.95

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_find_similar_changes_skips_source(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_info,
    ):
        """Test that find_similar_changes skips the source change."""
        mock_build_client.return_value = mock_client

        # Include the source change in results
        source_data = {
            "_number": sample_change_info.number,
            "change_id": sample_change_info.change_id,
            "project": sample_change_info.project,
            "subject": sample_change_info.subject,
            "branch": sample_change_info.branch,
            "status": sample_change_info.status,
            "owner": {"username": sample_change_info.owner},
        }

        mock_client.get.return_value = [source_data]

        mock_comparator = MagicMock()
        mock_comparator.compare_gerrit_changes.return_value = (
            GerritComparisonResult.similar(1.0, ["Identical"])
        )

        service = GerritService(host="gerrit.example.org")
        similar = service.find_similar_changes(sample_change_info, mock_comparator)

        # Source should be skipped
        assert len(similar) == 0

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_find_similar_changes_sorts_by_score(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
        sample_change_info,
    ):
        """Test that results are sorted by confidence score."""
        mock_build_client.return_value = mock_client

        changes_data = [
            {
                "_number": i,
                "change_id": f"I{i:040d}",
                "project": "proj",
                "subject": "Test",
                "branch": "main",
                "status": "NEW",
                "owner": {"username": "user"},
            }
            for i in range(1, 4)
        ]

        mock_client.get.return_value = changes_data

        # Return different scores for each change
        scores = {1: 0.7, 2: 0.95, 3: 0.85}
        mock_comparator = MagicMock()

        def compare_side_effect(source, target, **kwargs):
            score = scores.get(target.number, 0.5)
            return GerritComparisonResult.similar(score, [f"Score: {score}"])

        mock_comparator.compare_gerrit_changes.side_effect = compare_side_effect

        service = GerritService(host="gerrit.example.org")
        similar = service.find_similar_changes(sample_change_info, mock_comparator)

        # Should be sorted by score descending
        assert len(similar) == 3
        assert similar[0][0].number == 2  # 0.95
        assert similar[1][0].number == 3  # 0.85
        assert similar[2][0].number == 1  # 0.70

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_find_similar_changes_filters_other_owners_when_not_automation_only(
        self,
        mock_url_builder,
        mock_build_client,
        mock_client,
    ):
        """Test human override scans hard-filter changes to the same owner."""
        mock_build_client.return_value = mock_client

        source = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="CI: Bump github2gerrit workflow to v1.4.3",
            message="CI: Bump github2gerrit workflow to v1.4.3",
            owner="human-user",
            branch="main",
            status="NEW",
            files_changed=[
                GerritFileChange(filename=".github/workflows/github2gerrit.yaml")
            ],
        )
        mock_client.get.return_value = [
            {
                "_number": 2,
                "change_id": "I2",
                "project": "other-project",
                "subject": "CI: Bump github2gerrit workflow to v1.4.3",
                "branch": "main",
                "status": "NEW",
                "owner": {"username": "other-human"},
                "current_revision": "rev2",
                "revisions": {
                    "rev2": {
                        "commit": {
                            "message": "CI: Bump github2gerrit workflow to v1.4.3"
                        },
                        "files": {
                            ".github/workflows/github2gerrit.yaml": {"status": "M"}
                        },
                    }
                },
            }
        ]

        service = GerritService(host="gerrit.example.org")
        comparator = GerritChangeComparator(similarity_threshold=0.6)
        similar = service.find_similar_changes(
            source, comparator, only_automation=False
        )

        assert similar == []


class TestGerritServiceBasicCompare:
    """Tests for internal basic comparison logic."""

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_basic_compare_automation_check(
        self, mock_url_builder, mock_build_client, mock_client
    ):
        """Test basic comparison checks automation."""
        mock_build_client.return_value = mock_client

        service = GerritService(host="gerrit.example.org")

        # Non-automation change
        source = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Regular change",
            owner="human-user",
            branch="main",
            status="NEW",
        )

        target = GerritChangeInfo(
            number=2,
            change_id="I2",
            project="proj",
            subject="Another change",
            owner="another-user",
            branch="main",
            status="NEW",
        )

        result = service._basic_compare(source, target, only_automation=True)

        assert result.is_similar is False
        assert "not from automation" in result.reasons[0]

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_basic_compare_same_author(
        self, mock_url_builder, mock_build_client, mock_client
    ):
        """Test basic comparison with same author (no automation check)."""
        mock_build_client.return_value = mock_client

        service = GerritService(host="gerrit.example.org")

        # Use automation owner and matching subjects for high similarity
        source = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Bump package from 1.0 to 2.0",
            owner="dependabot",
            branch="main",
            status="NEW",
            files_changed=[GerritFileChange(filename="package.json", lines_inserted=1)],
        )

        target = GerritChangeInfo(
            number=2,
            change_id="I2",
            project="other",
            subject="Bump package from 1.0 to 2.0",
            owner="dependabot",
            branch="main",
            status="NEW",
            files_changed=[GerritFileChange(filename="package.json", lines_inserted=1)],
        )

        # Test without automation check to focus on author matching
        result = service._basic_compare(source, target, only_automation=False)

        assert "Same author" in result.reasons

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_basic_compare_requires_same_author_when_not_automation_only(
        self, mock_url_builder, mock_build_client, mock_client
    ):
        """Test fallback comparison rejects cross-owner human override matches."""
        mock_build_client.return_value = mock_client

        service = GerritService(
            host="gerrit.example.org",
            similarity_threshold=0.6,
        )
        source = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Bump package from 1.0 to 2.0",
            message="Bump package from 1.0 to 2.0",
            owner="human-user",
            branch="main",
            status="NEW",
            files_changed=[GerritFileChange(filename="package.json", lines_inserted=1)],
        )
        target = source.model_copy(
            update={
                "number": 2,
                "change_id": "I2",
                "owner": "other-human",
            }
        )

        result = service._basic_compare(source, target, only_automation=False)

        assert result.is_similar is False
        assert "owner does not match" in result.reasons[0]


class TestCreateGerritService:
    """Tests for create_gerrit_service factory function."""

    @patch("dependamerge.gerrit.service.build_client")
    @patch("dependamerge.gerrit.service.create_url_builder")
    def test_create_gerrit_service(
        self, mock_url_builder, mock_build_client, mock_client
    ):
        """Test factory function creates service correctly."""
        mock_build_client.return_value = mock_client

        service = create_gerrit_service(
            host="gerrit.example.org",
            base_path="infra",
            username="user",
            password="pass",
        )

        assert isinstance(service, GerritService)
        assert service.host == "gerrit.example.org"
        assert service.base_path == "infra"


class TestDefaultOptions:
    """Tests for default query options."""

    def test_default_change_options(self):
        """Test default options for change queries."""
        assert "CURRENT_REVISION" in DEFAULT_CHANGE_OPTIONS
        assert "CURRENT_FILES" in DEFAULT_CHANGE_OPTIONS
        assert "SUBMITTABLE" in DEFAULT_CHANGE_OPTIONS

    def test_default_list_options(self):
        """Test default options for list queries."""
        assert "CURRENT_REVISION" in DEFAULT_LIST_OPTIONS
        assert "LABELS" in DEFAULT_LIST_OPTIONS


class TestParseConflictFiles:
    """Tests for _parse_conflict_files defensive parsing."""

    def test_parse_conflict_files_standard_format(self, mock_client):
        """Test parsing with standard Gerrit conflict response format."""
        service = GerritService(host="gerrit.example.org")
        service._client = mock_client

        response_body = """The change could not be rebased due to a conflict during merge.

merge conflict(s):
path/to/file1.txt
path/to/file2.txt"""

        files = service._parse_conflict_files(response_body)
        assert files == ["path/to/file1.txt", "path/to/file2.txt"]

    def test_parse_conflict_files_empty_response(self, mock_client, caplog):
        """Test parsing with empty response body."""
        import logging

        service = GerritService(host="gerrit.example.org")
        service._client = mock_client

        with caplog.at_level(logging.DEBUG, logger="dependamerge.gerrit.service"):
            files = service._parse_conflict_files("")

        assert files == []
        # Filter to only look at records from the service module
        service_records = [
            r for r in caplog.records if r.name == "dependamerge.gerrit.service"
        ]
        # Should log at debug level about empty response
        assert any("empty" in r.message.lower() for r in service_records)

    def test_parse_conflict_files_no_marker(self, mock_client, caplog):
        """Test parsing when merge conflict marker is missing."""
        import logging

        service = GerritService(host="gerrit.example.org")
        service._client = mock_client

        response_body = "Some unexpected error message without conflict marker"

        with caplog.at_level(logging.WARNING):
            files = service._parse_conflict_files(response_body)

        assert files == []
        assert "Failed to find 'merge conflict' marker" in caplog.text

    def test_parse_conflict_files_marker_but_no_files(self, mock_client, caplog):
        """Test parsing when marker exists but no files follow."""
        import logging

        service = GerritService(host="gerrit.example.org")
        service._client = mock_client

        response_body = """The change could not be rebased.

merge conflict(s):

"""

        with caplog.at_level(logging.WARNING):
            files = service._parse_conflict_files(response_body)

        assert files == []
        assert "No conflicting files parsed" in caplog.text

    def test_parse_conflict_files_blank_line_ends_section(self, mock_client):
        """Test that blank line after files ends the conflict section."""
        service = GerritService(host="gerrit.example.org")
        service._client = mock_client

        response_body = """merge conflict(s):
path/to/file1.txt
path/to/file2.txt

Some additional message that should not be included"""

        files = service._parse_conflict_files(response_body)
        assert files == ["path/to/file1.txt", "path/to/file2.txt"]
        assert "Some additional message" not in files

    def test_parse_conflict_files_case_insensitive_marker(self, mock_client):
        """Test that marker matching is case-insensitive."""
        service = GerritService(host="gerrit.example.org")
        service._client = mock_client

        response_body = """MERGE CONFLICT(S):
path/to/file.txt"""

        files = service._parse_conflict_files(response_body)
        assert files == ["path/to/file.txt"]

    def test_parse_conflict_files_strips_whitespace(self, mock_client):
        """Test that file paths are stripped of whitespace."""
        service = GerritService(host="gerrit.example.org")
        service._client = mock_client

        response_body = """merge conflict(s):
  path/to/file1.txt
    path/to/file2.txt"""

        files = service._parse_conflict_files(response_body)
        assert files == ["path/to/file1.txt", "path/to/file2.txt"]

    def test_parse_conflict_files_single_file(self, mock_client):
        """Test parsing with a single conflicting file."""
        service = GerritService(host="gerrit.example.org")
        service._client = mock_client

        response_body = """merge conflict(s):
only-one-file.txt"""

        files = service._parse_conflict_files(response_body)
        assert files == ["only-one-file.txt"]
