# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for Gerrit change comparator.

This module tests the GerritChangeComparator class for comparing changes
and detecting similarity based on author, subject, files, and automation
patterns.
"""

import pytest

from dependamerge.gerrit.comparator import (
    AUTOMATION_INDICATORS,
    GerritChangeComparator,
    create_gerrit_comparator,
)
from dependamerge.gerrit.models import GerritChangeInfo, GerritFileChange


@pytest.fixture
def comparator():
    """Create a GerritChangeComparator with default threshold."""
    return GerritChangeComparator(similarity_threshold=0.8)


@pytest.fixture
def automation_change():
    """Create a sample automation change (Dependabot)."""
    return GerritChangeInfo(
        number=12345,
        change_id="I1234567890abcdef",
        project="my-project",
        subject="Chore: Bump actions/checkout from 4.1.0 to 4.2.0",
        message="Bumps [actions/checkout](https://github.com/actions/checkout) "
        "from 4.1.0 to 4.2.0.\n\nRelease notes...",
        owner="dependabot",
        branch="main",
        status="NEW",
        files_changed=[
            GerritFileChange(
                filename=".github/workflows/ci.yml",
                status="M",
                lines_inserted=1,
                lines_deleted=1,
            )
        ],
    )


@pytest.fixture
def similar_automation_change():
    """Create another automation change similar to the first."""
    return GerritChangeInfo(
        number=12346,
        change_id="I9876543210fedcba",
        project="other-project",
        subject="Chore: Bump actions/checkout from 4.1.0 to 4.2.0",
        message="Bumps [actions/checkout](https://github.com/actions/checkout) "
        "from 4.1.0 to 4.2.0.\n\nRelease notes...",
        owner="dependabot",
        branch="main",
        status="NEW",
        files_changed=[
            GerritFileChange(
                filename=".github/workflows/build.yml",
                status="M",
                lines_inserted=1,
                lines_deleted=1,
            )
        ],
    )


@pytest.fixture
def human_change():
    """Create a non-automation change."""
    return GerritChangeInfo(
        number=99999,
        change_id="Iabcdef1234567890",
        project="my-project",
        subject="Fix: Resolve login issue",
        message="Fixed the login issue by updating the auth handler.",
        owner="john.doe",
        branch="main",
        status="NEW",
        files_changed=[
            GerritFileChange(
                filename="src/auth/handler.py",
                status="M",
                lines_inserted=15,
                lines_deleted=3,
            )
        ],
    )


class TestGerritChangeComparatorInit:
    """Tests for GerritChangeComparator initialization."""

    def test_default_threshold(self):
        """Test default similarity threshold."""
        comparator = GerritChangeComparator()
        assert comparator.similarity_threshold == 0.8

    def test_custom_threshold(self):
        """Test custom similarity threshold."""
        comparator = GerritChangeComparator(similarity_threshold=0.9)
        assert comparator.similarity_threshold == 0.9


class TestAutomationDetection:
    """Tests for automation change detection."""

    def test_detects_dependabot(self, comparator):
        """Test detection of Dependabot changes."""
        change = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Bump package",
            owner="dependabot",
            branch="main",
            status="NEW",
        )
        assert comparator._is_automation_change(change) is True
        assert comparator.is_automation_change(change) is True

    def test_detects_renovate(self, comparator):
        """Test detection of Renovate changes."""
        change = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Update dependency",
            owner="renovate[bot]",
            branch="main",
            status="NEW",
        )
        assert comparator._is_automation_change(change) is True

    def test_detects_precommit_ci(self, comparator):
        """Test detection of pre-commit-ci changes."""
        change = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="[pre-commit.ci] autoupdate",
            owner="pre-commit-ci",
            branch="main",
            status="NEW",
        )
        assert comparator._is_automation_change(change) is True

    def test_detects_automation_in_subject(self, comparator):
        """Test detection of automation patterns in subject."""
        change = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="chore(deps): bump typescript from 4.9 to 5.0",
            owner="regular-user",
            branch="main",
            status="NEW",
        )
        assert comparator._is_automation_change(change) is True

    def test_non_automation_change(self, comparator, human_change):
        """Test that human changes are not detected as automation."""
        assert comparator._is_automation_change(human_change) is False
        assert comparator.is_automation_change(human_change) is False


class TestOwnerComparison:
    """Tests for owner (author) comparison."""

    def test_same_owner(self, comparator):
        """Test comparison with same owner."""
        change1 = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Test",
            owner="dependabot",
            branch="main",
            status="NEW",
        )
        change2 = GerritChangeInfo(
            number=2,
            change_id="I2",
            project="proj",
            subject="Test",
            owner="dependabot",
            branch="main",
            status="NEW",
        )
        assert comparator._compare_owners(change1, change2) == 1.0

    def test_normalized_owner_bot_suffix(self, comparator):
        """Test that [bot] suffix is normalized."""
        change1 = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Test",
            owner="dependabot",
            branch="main",
            status="NEW",
        )
        change2 = GerritChangeInfo(
            number=2,
            change_id="I2",
            project="proj",
            subject="Test",
            owner="dependabot[bot]",
            branch="main",
            status="NEW",
        )
        assert comparator._compare_owners(change1, change2) == 1.0

    def test_different_owners(self, comparator):
        """Test comparison with different owners."""
        change1 = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Test",
            owner="dependabot",
            branch="main",
            status="NEW",
        )
        change2 = GerritChangeInfo(
            number=2,
            change_id="I2",
            project="proj",
            subject="Test",
            owner="renovate",
            branch="main",
            status="NEW",
        )
        assert comparator._compare_owners(change1, change2) == 0.0


class TestSubjectComparison:
    """Tests for subject (title) comparison."""

    def test_identical_subjects(self, comparator):
        """Test comparison of identical subjects."""
        score = comparator._compare_subjects(
            "Bump actions/checkout from 4.1.0 to 4.2.0",
            "Bump actions/checkout from 4.1.0 to 4.2.0",
        )
        assert score == 1.0

    def test_same_package_different_versions(self, comparator):
        """Test that same package with different versions matches."""
        score = comparator._compare_subjects(
            "Bump actions/checkout from 4.1.0 to 4.2.0",
            "Bump actions/checkout from 4.0.0 to 4.1.0",
        )
        assert score == 1.0

    def test_different_packages(self, comparator):
        """Test that different packages do not match."""
        score = comparator._compare_subjects(
            "Bump actions/checkout from 4.1.0 to 4.2.0",
            "Bump actions/setup-python from 4.0 to 5.0",
        )
        assert score == 0.0

    def test_non_dependency_subjects(self, comparator):
        """Test comparison of non-dependency subjects."""
        score = comparator._compare_subjects(
            "Fix login issue in auth handler",
            "Fix logout issue in auth handler",
        )
        # Should use sequence matching
        assert 0.0 < score < 1.0


class TestPackageNameExtraction:
    """Tests for package name extraction from subjects."""

    def test_extract_bump_pattern(self, comparator):
        """Test extraction from 'Bump X from Y to Z' pattern."""
        package = comparator._extract_package_name(
            "Bump actions/checkout from 4.1.0 to 4.2.0"
        )
        assert package == "actions/checkout"

    def test_extract_chore_bump_pattern(self, comparator):
        """Test extraction from 'Chore: Bump X' pattern."""
        package = comparator._extract_package_name(
            "Chore: Bump typescript from 4.9.0 to 5.0.0"
        )
        assert package == "typescript"

    def test_extract_build_deps_pattern(self, comparator):
        """Test extraction from 'build(deps): bump X' pattern."""
        package = comparator._extract_package_name(
            "build(deps): bump pytest from 7.0 to 8.0"
        )
        assert package == "pytest"

    def test_no_package_in_subject(self, comparator):
        """Test extraction from non-dependency subject."""
        package = comparator._extract_package_name("Fix login issue")
        assert package == ""


class TestMessageComparison:
    """Tests for commit message comparison."""

    def test_identical_messages(self, comparator):
        """Test comparison of identical messages."""
        score = comparator._compare_messages(
            "This is a test message",
            "This is a test message",
        )
        assert score == 1.0

    def test_none_messages(self, comparator):
        """Test comparison with None messages."""
        assert comparator._compare_messages(None, "test") == 0.0
        assert comparator._compare_messages("test", None) == 0.0
        assert comparator._compare_messages(None, None) == 0.0

    def test_dependabot_same_package(self, comparator):
        """Test Dependabot messages for same package."""
        msg1 = """Bumps [actions/checkout](https://github.com/actions/checkout)
        from 4.1.0 to 4.2.0.
        Release notes available at the link.
        dependency-name: actions/checkout"""

        msg2 = """Bumps [actions/checkout](https://github.com/actions/checkout)
        from 4.0.0 to 4.1.0.
        Changelog and release notes available.
        dependency-name: actions/checkout"""

        score = comparator._compare_messages(msg1, msg2)
        assert score >= 0.9

    def test_precommit_messages(self, comparator):
        """Test pre-commit autoupdate messages."""
        msg1 = """[pre-commit.ci] autoupdate

Updates the following hooks in .pre-commit-config.yaml:
- repo: https://github.com/pre-commit/pre-commit-hooks
  rev: v4.4.0 -> v4.5.0

This is an automated pre-commit update."""

        msg2 = """[pre-commit.ci] autoupdate

Updates the following hooks in .pre-commit-config.yaml:
- repo: https://github.com/pre-commit/pre-commit-hooks
  rev: v4.3.0 -> v4.4.0

This is an automated pre-commit update."""

        score = comparator._compare_messages(msg1, msg2)
        assert score >= 0.9


class TestFileComparison:
    """Tests for file change comparison."""

    def test_identical_files(self, comparator):
        """Test comparison with identical file changes."""
        change1 = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Test",
            owner="bot",
            branch="main",
            status="NEW",
            files_changed=[
                GerritFileChange(filename="src/main.py"),
                GerritFileChange(filename="src/util.py"),
            ],
        )
        change2 = GerritChangeInfo(
            number=2,
            change_id="I2",
            project="proj",
            subject="Test",
            owner="bot",
            branch="main",
            status="NEW",
            files_changed=[
                GerritFileChange(filename="src/main.py"),
                GerritFileChange(filename="src/util.py"),
            ],
        )
        score = comparator._compare_files(change1, change2)
        assert score == 1.0

    def test_overlapping_files(self, comparator):
        """Test comparison with overlapping file changes."""
        change1 = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Test",
            owner="bot",
            branch="main",
            status="NEW",
            files_changed=[
                GerritFileChange(filename="src/main.py"),
                GerritFileChange(filename="src/util.py"),
            ],
        )
        change2 = GerritChangeInfo(
            number=2,
            change_id="I2",
            project="proj",
            subject="Test",
            owner="bot",
            branch="main",
            status="NEW",
            files_changed=[
                GerritFileChange(filename="src/main.py"),
                GerritFileChange(filename="src/other.py"),
            ],
        )
        score = comparator._compare_files(change1, change2)
        # Jaccard: intersection=1, union=3, score=0.33
        assert 0.3 < score < 0.4

    def test_no_overlapping_files(self, comparator):
        """Test comparison with no overlapping files."""
        change1 = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Test",
            owner="bot",
            branch="main",
            status="NEW",
            files_changed=[GerritFileChange(filename="src/a.py")],
        )
        change2 = GerritChangeInfo(
            number=2,
            change_id="I2",
            project="proj",
            subject="Test",
            owner="bot",
            branch="main",
            status="NEW",
            files_changed=[GerritFileChange(filename="src/b.py")],
        )
        score = comparator._compare_files(change1, change2)
        assert score == 0.0

    def test_empty_files(self, comparator):
        """Test comparison with empty file lists."""
        change1 = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Test",
            owner="bot",
            branch="main",
            status="NEW",
            files_changed=[],
        )
        change2 = GerritChangeInfo(
            number=2,
            change_id="I2",
            project="proj",
            subject="Test",
            owner="bot",
            branch="main",
            status="NEW",
            files_changed=[GerritFileChange(filename="src/a.py")],
        )
        score = comparator._compare_files(change1, change2)
        assert score == 0.0

    def test_workflow_files_boost(self, comparator):
        """Test that workflow files get similarity boost."""
        change1 = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Test",
            owner="bot",
            branch="main",
            status="NEW",
            files_changed=[GerritFileChange(filename=".github/workflows/ci.yml")],
        )
        change2 = GerritChangeInfo(
            number=2,
            change_id="I2",
            project="proj",
            subject="Test",
            owner="bot",
            branch="main",
            status="NEW",
            files_changed=[GerritFileChange(filename=".github/workflows/build.yml")],
        )
        score = comparator._compare_files(change1, change2)
        # Both have workflow files, should get boosted to at least 0.5
        assert score >= 0.5


class TestCompareGerritChanges:
    """Tests for the main compare_gerrit_changes method."""

    def test_similar_automation_changes(
        self, comparator, automation_change, similar_automation_change
    ):
        """Test that similar automation changes are matched."""
        result = comparator.compare_gerrit_changes(
            automation_change, similar_automation_change
        )

        assert result.is_similar is True
        assert result.confidence_score >= 0.8
        assert len(result.reasons) > 0

    def test_non_automation_rejected_when_required(
        self, comparator, automation_change, human_change
    ):
        """Test that non-automation changes are rejected when required."""
        result = comparator.compare_gerrit_changes(
            automation_change, human_change, only_automation=True
        )

        assert result.is_similar is False
        assert "not from automation" in result.reasons[0]

    def test_non_automation_allowed_when_disabled(self, comparator, human_change):
        """Test that non-automation comparison works when check disabled."""
        # Create a similar human change
        similar_human = GerritChangeInfo(
            number=99998,
            change_id="Ifedcba0987654321",
            project="other-project",
            subject="Fix: Resolve login issue",
            message="Fixed the login issue by updating the auth handler.",
            owner="john.doe",
            branch="main",
            status="NEW",
            files_changed=[
                GerritFileChange(
                    filename="src/auth/handler.py",
                    status="M",
                    lines_inserted=10,
                    lines_deleted=2,
                )
            ],
        )

        result = comparator.compare_gerrit_changes(
            human_change, similar_human, only_automation=False
        )

        # Should evaluate similarity without automation check
        assert result.confidence_score > 0.0

    def test_non_automation_requires_same_owner_when_disabled(self):
        """Test human overrides never match changes from a different owner."""
        comparator = GerritChangeComparator(similarity_threshold=0.6)
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
        target = source.model_copy(
            update={
                "number": 2,
                "change_id": "I2",
                "owner": "other-human",
            }
        )

        result = comparator.compare_gerrit_changes(
            source, target, only_automation=False
        )

        assert result.is_similar is False
        assert "owner does not match" in result.reasons[0]

    def test_different_package_updates_not_similar(self, comparator):
        """Test that different package updates are not similar."""
        change1 = GerritChangeInfo(
            number=1,
            change_id="I1",
            project="proj",
            subject="Bump actions/checkout from 4.1.0 to 4.2.0",
            owner="dependabot",
            branch="main",
            status="NEW",
        )
        change2 = GerritChangeInfo(
            number=2,
            change_id="I2",
            project="proj",
            subject="Bump actions/setup-python from 4.0 to 5.0",
            owner="dependabot",
            branch="main",
            status="NEW",
        )

        result = comparator.compare_gerrit_changes(change1, change2)

        # Same author but different packages
        assert result.confidence_score < 0.8


class TestCreateGerritComparator:
    """Tests for the create_gerrit_comparator factory function."""

    def test_create_with_defaults(self):
        """Test factory with default threshold."""
        comparator = create_gerrit_comparator()
        assert comparator.similarity_threshold == 0.8

    def test_create_with_custom_threshold(self):
        """Test factory with custom threshold."""
        comparator = create_gerrit_comparator(similarity_threshold=0.9)
        assert comparator.similarity_threshold == 0.9


class TestAutomationIndicators:
    """Tests for the AUTOMATION_INDICATORS constant."""

    def test_common_indicators_present(self):
        """Test that common automation tools are in the list."""
        assert "dependabot" in AUTOMATION_INDICATORS
        assert "renovate" in AUTOMATION_INDICATORS
        assert "pre-commit" in AUTOMATION_INDICATORS
        assert "bot" in AUTOMATION_INDICATORS

    def test_indicators_are_lowercase(self):
        """Test that all indicators are lowercase for matching."""
        for indicator in AUTOMATION_INDICATORS:
            assert indicator == indicator.lower()
