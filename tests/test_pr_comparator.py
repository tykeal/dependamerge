# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from dependamerge.models import FileChange, PullRequestInfo
from dependamerge.pr_comparator import PRComparator


class TestPRComparator:
    def test_init_default_threshold(self):
        comparator = PRComparator()
        assert comparator.similarity_threshold == 0.8

    def test_init_custom_threshold(self):
        comparator = PRComparator(0.9)
        assert comparator.similarity_threshold == 0.9

    def test_normalize_title_removes_versions(self):
        comparator = PRComparator()

        original = "Bump dependency from 1.2.3 to 1.2.4"
        normalized = comparator._normalize_title(original)
        assert "1.2.3" not in normalized
        assert "1.2.4" not in normalized
        assert "bump dependency from to" in normalized

    def test_normalize_title_removes_commit_hashes(self):
        comparator = PRComparator()

        original = "Update to commit abc123def456"
        normalized = comparator._normalize_title(original)
        assert "abc123def456" not in normalized
        assert "update to commit" in normalized

    def test_compare_titles_identical(self):
        comparator = PRComparator()

        title1 = "Bump requests from 2.28.0 to 2.28.1"
        title2 = "Bump requests from 2.27.0 to 2.28.1"

        score = comparator._compare_titles(title1, title2)
        assert score > 0.8  # Should be very similar after normalization

    def test_compare_file_changes_identical(self):
        comparator = PRComparator()

        files1 = [
            FileChange(
                filename="requirements.txt",
                additions=1,
                deletions=1,
                changes=2,
                status="modified",
            ),
            FileChange(
                filename="setup.py",
                additions=1,
                deletions=1,
                changes=2,
                status="modified",
            ),
        ]
        files2 = [
            FileChange(
                filename="requirements.txt",
                additions=2,
                deletions=1,
                changes=3,
                status="modified",
            ),
            FileChange(
                filename="setup.py",
                additions=1,
                deletions=2,
                changes=3,
                status="modified",
            ),
        ]

        score = comparator._compare_file_changes(files1, files2)
        assert score == 1.0  # Same files changed

    def test_compare_file_changes_partial_overlap(self):
        comparator = PRComparator()

        files1 = [
            FileChange(
                filename="requirements.txt",
                additions=1,
                deletions=1,
                changes=2,
                status="modified",
            ),
            FileChange(
                filename="setup.py",
                additions=1,
                deletions=1,
                changes=2,
                status="modified",
            ),
        ]
        files2 = [
            FileChange(
                filename="requirements.txt",
                additions=1,
                deletions=1,
                changes=2,
                status="modified",
            ),
            FileChange(
                filename="package.json",
                additions=1,
                deletions=1,
                changes=2,
                status="modified",
            ),
        ]

        score = comparator._compare_file_changes(files1, files2)
        assert 0.3 < score < 0.7  # Partial overlap

    def test_is_automation_pr_dependabot(self):
        comparator = PRComparator()

        pr = PullRequestInfo(
            number=1,
            title="Bump requests from 2.28.0 to 2.28.1",
            body="Bumps requests from 2.28.0 to 2.28.1",
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
            html_url="https://github.com/owner/repo/pull/1",
        )

        assert comparator._is_automation_pr(pr)

    def test_is_automation_pr_human(self):
        comparator = PRComparator()

        pr = PullRequestInfo(
            number=1,
            title="Fix bug in user authentication",
            body="This PR fixes a critical bug",
            author="human-developer",
            head_sha="abc123",
            base_branch="main",
            head_branch="fix-auth-bug",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/1",
        )

        assert not comparator._is_automation_pr(pr)

    def test_compare_similar_automation_prs(self):
        comparator = PRComparator(0.7)

        pr1 = PullRequestInfo(
            number=1,
            title="Bump requests from 2.28.0 to 2.28.1",
            body="Bumps requests from 2.28.0 to 2.28.1",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="dependabot/pip/requests-2.28.1",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[
                FileChange(
                    filename="requirements.txt",
                    additions=1,
                    deletions=1,
                    changes=2,
                    status="modified",
                )
            ],
            repository_full_name="owner/repo1",
            html_url="https://github.com/owner/repo1/pull/1",
        )

        pr2 = PullRequestInfo(
            number=2,
            title="Bump requests from 2.27.0 to 2.28.1",
            body="Bumps requests from 2.27.0 to 2.28.1",
            author="dependabot[bot]",
            head_sha="def456",
            base_branch="main",
            head_branch="dependabot/pip/requests-2.28.1",
            state="open",
            mergeable=True,
            mergeable_state="clean",
            behind_by=0,
            files_changed=[
                FileChange(
                    filename="requirements.txt",
                    additions=1,
                    deletions=1,
                    changes=2,
                    status="modified",
                )
            ],
            repository_full_name="owner/repo2",
            html_url="https://github.com/owner/repo2/pull/2",
        )

        result = comparator.compare_pull_requests(pr1, pr2)
        assert result.is_similar
        assert result.confidence_score >= 0.7
        assert len(result.reasons) > 0
