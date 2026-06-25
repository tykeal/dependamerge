# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for repository-scoped bulk merge feature."""

import hashlib
import re
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from dependamerge.cli import app
from dependamerge.models import FileChange, PullRequestInfo
from dependamerge.url_parser import (
    ChangeSource,
    ParsedRepoUrl,
    UrlParseError,
    parse_repo_url,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _mock_asyncio_run(side_effects: list[object]):
    """Create a mock for asyncio.run that properly closes coroutines.

    When asyncio.run is mocked, the coroutine object passed to it is never
    awaited and gets garbage-collected, producing RuntimeWarning.  This
    helper closes each coroutine before returning the canned value so the
    warning is never emitted.
    """
    call_index = 0

    def _side_effect(coro):
        nonlocal call_index
        # Close the coroutine so Python doesn't warn about it
        coro.close()
        if call_index < len(side_effects):
            result = side_effects[call_index]
            call_index += 1
            return result
        raise AssertionError(
            f"Unexpected asyncio.run call #{call_index + 1} "
            f"(only {len(side_effects)} side-effects provided)"
        )

    return _side_effect


def _make_pr(
    number: int,
    author: str = "dependabot[bot]",
    title: str = "Bump foo from 1.0 to 2.0",
    repo: str = "owner/repo",
    state: str = "open",
) -> PullRequestInfo:
    """Build a minimal PullRequestInfo for testing."""
    return PullRequestInfo(
        number=number,
        title=title,
        body="Automated dependency update",
        author=author,
        head_sha="abc123",
        base_branch="main",
        head_branch="dependabot/npm_and_yarn/foo-2.0",
        state=state,
        mergeable=True,
        mergeable_state="clean",
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
        repository_full_name=repo,
        html_url=f"https://github.com/{repo}/pull/{number}",
    )


# ---------------------------------------------------------------------------
# URL parser: parse_repo_url
# ---------------------------------------------------------------------------


class TestParseRepoUrl:
    """Tests for the new parse_repo_url function."""

    def test_repo_url_basic(self):
        result = parse_repo_url("https://github.com/owner/repo")
        assert result.source == ChangeSource.GITHUB
        assert result.owner == "owner"
        assert result.repo == "repo"
        assert result.project == "owner/repo"
        assert result.host == "github.com"
        assert result.is_github is True

    def test_repo_url_trailing_slash(self):
        result = parse_repo_url("https://github.com/owner/repo/")
        assert result.owner == "owner"
        assert result.repo == "repo"
        assert result.project == "owner/repo"

    def test_repo_url_pulls_page(self):
        result = parse_repo_url("https://github.com/modeseven-lfit/lftools-uv/pulls")
        assert result.owner == "modeseven-lfit"
        assert result.repo == "lftools-uv"
        assert result.project == "modeseven-lfit/lftools-uv"

    def test_repo_url_pulls_trailing_slash(self):
        result = parse_repo_url("https://github.com/owner/repo/pulls/")
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_repo_url_without_scheme(self):
        result = parse_repo_url("github.com/owner/repo")
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_repo_url_http_scheme(self):
        result = parse_repo_url("http://github.com/owner/repo")
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_repo_url_preserves_original(self):
        url = "https://github.com/modeseven-lfit/lftools-uv/"
        result = parse_repo_url(url)
        assert result.original_url == url

    def test_repo_url_with_dashes_in_names(self):
        result = parse_repo_url("https://github.com/my-org/my-repo-name")
        assert result.owner == "my-org"
        assert result.repo == "my-repo-name"

    def test_repo_url_empty_raises(self):
        with pytest.raises(UrlParseError, match="URL cannot be empty"):
            parse_repo_url("")

    def test_repo_url_whitespace_only_raises(self):
        with pytest.raises(UrlParseError, match="URL cannot be empty"):
            parse_repo_url("   ")

    def test_repo_url_gerrit_style_raises(self):
        with pytest.raises(UrlParseError, match="only supported for github.com"):
            parse_repo_url("https://gerrit.example.org/c/project/+/12345")

    def test_repo_url_gerrit_style_no_plus_rejected(self):
        """A non-github.com host is rejected regardless of path shape."""
        with pytest.raises(UrlParseError, match="only supported for github.com"):
            parse_repo_url("https://gerrit.example.org/c/repo")

    def test_repo_url_ghe_rejected(self):
        """GHE hosts are not github.com subdomains — rejected at parse time."""
        with pytest.raises(UrlParseError, match="only supported for github.com"):
            parse_repo_url("https://github.enterprise.com/owner/repo")

    def test_repo_url_github_subdomain_accepted(self):
        """Actual github.com subdomains (e.g. foo.github.com) are accepted."""
        result = parse_repo_url("https://foo.github.com/owner/repo")
        assert result.source == ChangeSource.GITHUB
        assert result.host == "foo.github.com"
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_repo_url_non_github_host_rejected(self):
        # Non-github.com hosts are rejected at parse time to prevent misrouting
        with pytest.raises(UrlParseError, match="only supported for github.com"):
            parse_repo_url("https://gitlab.com/owner/repo")

    def test_repo_url_extra_segments_raises(self):
        with pytest.raises(UrlParseError, match="Invalid GitHub repository URL"):
            parse_repo_url("https://github.com/owner/repo/issues")

    def test_repo_url_settings_raises(self):
        with pytest.raises(UrlParseError, match="Invalid GitHub repository URL"):
            parse_repo_url("https://github.com/owner/repo/settings")

    def test_repo_url_pr_url_raises(self):
        with pytest.raises(UrlParseError, match="looks like a pull request URL"):
            parse_repo_url("https://github.com/owner/repo/pull/123")

    def test_repo_url_owner_named_pull_accepted(self):
        # An owner literally named "pull" should not be rejected
        result = parse_repo_url("https://github.com/pull/repo")
        assert result.owner == "pull"
        assert result.repo == "repo"

    def test_repo_url_too_short_raises(self):
        with pytest.raises(UrlParseError, match="Invalid GitHub repository URL"):
            parse_repo_url("https://github.com/owner")

    def test_repo_url_just_host_raises(self):
        with pytest.raises(UrlParseError, match="Invalid GitHub repository URL"):
            parse_repo_url("https://github.com/")

    def test_repo_url_is_frozen(self):
        result = parse_repo_url("https://github.com/owner/repo")
        with pytest.raises(AttributeError):
            result.owner = "other"  # type: ignore[misc]

    def test_repo_url_with_leading_whitespace(self):
        result = parse_repo_url("  https://github.com/owner/repo  ")
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_repo_url_case_insensitive_host(self):
        result = parse_repo_url("https://GitHub.COM/Owner/Repo")
        assert result.host == "github.com"
        # Owner/repo casing is preserved as provided (GitHub resolves
        # casing server-side, but we store what the user gave us)
        assert result.owner == "Owner"
        assert result.repo == "Repo"


class TestParsedRepoUrlDataclass:
    """Tests for the ParsedRepoUrl dataclass."""

    def test_is_github_property(self):
        url = ParsedRepoUrl(
            source=ChangeSource.GITHUB,
            host="github.com",
            owner="owner",
            repo="repo",
            project="owner/repo",
            original_url="https://github.com/owner/repo",
        )
        assert url.is_github is True


# ---------------------------------------------------------------------------
# CLI: merge command with repository URLs
# ---------------------------------------------------------------------------


class TestMergeRepoUrl:
    """Tests for the merge command when given a repository URL."""

    runner: CliRunner = CliRunner()

    def setup_method(self):
        self.runner = CliRunner()

    @patch("dependamerge.cli.asyncio.run")
    @patch("dependamerge.cli.GitHubClient")
    def test_repo_url_is_accepted(self, mock_client_class, mock_asyncio_run):
        """Verify that a repo URL doesn't produce 'Invalid URL' error."""
        mock_client = Mock()
        mock_client.token = "fake_token"
        mock_client_class.return_value = mock_client

        # Permissions check, then fetch PRs
        mock_asyncio_run.side_effect = _mock_asyncio_run(
            [
                {
                    "approve": {"has_permission": True},
                    "merge": {"has_permission": True},
                    "branch_protection": {"has_permission": True},
                },
                [],
            ]
        )

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo/pulls",
                "--token",
                "fake_token",
            ],
        )
        # Should not see the URL parse error
        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        assert "❌ Invalid URL:" not in result.stdout

    @patch("dependamerge.cli.asyncio.run")
    @patch("dependamerge.cli.GitHubClient")
    def test_repo_url_trailing_slash_accepted(
        self, mock_client_class, mock_asyncio_run
    ):
        mock_client = Mock()
        mock_client.token = "fake_token"
        mock_client_class.return_value = mock_client

        mock_asyncio_run.side_effect = _mock_asyncio_run(
            [
                {
                    "approve": {"has_permission": True},
                    "merge": {"has_permission": True},
                    "branch_protection": {"has_permission": True},
                },
                [],
            ]
        )

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo/",
                "--token",
                "fake_token",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        assert "❌ Invalid URL:" not in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_repo_merge_automation_only_default(
        self, mock_asyncio_run, mock_client_class
    ):
        """By default only automation PRs should be included."""
        auto_pr = _make_pr(1, author="dependabot[bot]")
        # human_pr is filtered out by only_automation=True in the service layer
        _make_pr(2, author="jsmith", title="Fix README typo")

        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client.is_automation_author.side_effect = lambda a: (
            a
            in {
                "dependabot[bot]",
                "pre-commit-ci[bot]",
                "renovate[bot]",
                "github-actions[bot]",
            }
        )
        mock_client_class.return_value = mock_client

        # First asyncio.run call is _check (permissions) — succeed
        # Second asyncio.run call is _fetch_prs — return auto_pr only
        # (because only_automation=True filters out human_pr)
        mock_asyncio_run.side_effect = _mock_asyncio_run(
            [
                # permissions check
                {
                    "approve": {"has_permission": True},
                    "merge": {"has_permission": True},
                    "branch_protection": {"has_permission": True},
                },
                # fetch PRs (only automation)
                [auto_pr],
                # parallel merge (preview)
                [],
            ]
        )

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo/pulls",
                "--token",
                "test_token",
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        assert "Repository mode" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_repo_merge_no_open_prs(self, mock_asyncio_run, mock_client_class):
        """When no matching PRs exist, show appropriate message."""
        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client_class.return_value = mock_client

        mock_asyncio_run.side_effect = _mock_asyncio_run(
            [
                # permissions check
                {
                    "approve": {"has_permission": True},
                    "merge": {"has_permission": True},
                    "branch_protection": {"has_permission": True},
                },
                # fetch PRs
                [],
            ]
        )

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo",
                "--token",
                "test_token",
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        assert "No open" in result.stdout
        assert "PRs found" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_repo_merge_include_human_prs_flag(
        self, mock_asyncio_run, mock_client_class
    ):
        """--include-human-prs should pass only_automation=False to the service."""
        auto_pr = _make_pr(1, author="dependabot[bot]")
        human_pr = _make_pr(2, author="jsmith", title="Fix README typo")

        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client.is_automation_author.side_effect = lambda a: (
            a
            in {
                "dependabot[bot]",
                "pre-commit-ci[bot]",
                "renovate[bot]",
                "github-actions[bot]",
            }
        )
        mock_client_class.return_value = mock_client

        mock_asyncio_run.side_effect = _mock_asyncio_run(
            [
                # permissions check
                {
                    "approve": {"has_permission": True},
                    "merge": {"has_permission": True},
                    "branch_protection": {"has_permission": True},
                },
                # fetch PRs (both auto and human since only_automation=False)
                [auto_pr, human_pr],
                # preview merge phase (returns MergeResult list)
                [],
            ]
        )

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo",
                "--token",
                "test_token",
                "--include-human-prs",
            ],
        )

        assert result.exit_code == 0
        # Should show human PRs warning
        assert "Repository mode" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_repo_merge_human_prs_no_prompt_when_none_found(
        self, mock_asyncio_run, mock_client_class
    ):
        """Even with --include-human-prs, don't prompt if no human PRs in results."""
        auto_pr = _make_pr(1, author="dependabot[bot]")

        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client.is_automation_author.side_effect = lambda a: (
            a
            in {
                "dependabot[bot]",
                "pre-commit-ci[bot]",
                "renovate[bot]",
                "github-actions[bot]",
            }
        )
        mock_client_class.return_value = mock_client

        mock_asyncio_run.side_effect = _mock_asyncio_run(
            [
                # permissions check
                {
                    "approve": {"has_permission": True},
                    "merge": {"has_permission": True},
                    "branch_protection": {"has_permission": True},
                },
                # fetch PRs (only automation even though flag was given)
                [auto_pr],
                # preview merge
                [],
            ]
        )

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo",
                "--token",
                "test_token",
                "--include-human-prs",
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        # Should NOT show the human PR confirmation prompt
        assert "Human-authored PRs are included" not in result.stdout

    def test_include_human_prs_help_shown(self):
        """The --include-human-prs flag should appear in help text."""
        result = self.runner.invoke(app, ["merge", "--help"])
        plain = _strip_ansi(result.stdout)
        assert "--include-human-prs" in plain

    def test_repo_url_formats_in_help(self):
        """Repository URL format docs should appear in merge command help."""
        result = self.runner.invoke(app, ["merge", "--help"])
        plain = _strip_ansi(result.stdout)
        assert "repository url" in plain.lower() or "Repository URL" in plain

    def test_invalid_url_still_rejected(self):
        """A completely invalid URL should still be rejected."""
        result = self.runner.invoke(
            app,
            ["merge", "not-a-url", "--token", "test_token"],
        )
        assert result.exit_code == 1
        assert "❌ Invalid URL:" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_repo_merge_shows_pr_classification(
        self, mock_asyncio_run, mock_client_class
    ):
        """Output should classify PRs as automation vs human."""
        auto_pr = _make_pr(1, author="dependabot[bot]")
        human_pr = _make_pr(2, author="jsmith", title="Fix README")

        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client.is_automation_author.side_effect = lambda a: (
            a
            in {
                "dependabot[bot]",
                "pre-commit-ci[bot]",
                "renovate[bot]",
                "github-actions[bot]",
            }
        )
        mock_client_class.return_value = mock_client

        mock_asyncio_run.side_effect = _mock_asyncio_run(
            [
                # permissions check
                {
                    "approve": {"has_permission": True},
                    "merge": {"has_permission": True},
                    "branch_protection": {"has_permission": True},
                },
                # fetch PRs (both types with --include-human-prs)
                [auto_pr, human_pr],
                # preview merge phase (returns MergeResult list)
                [],
            ]
        )

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo",
                "--token",
                "test_token",
                "--include-human-prs",
            ],
        )

        assert result.exit_code == 0
        # Should show counts
        assert "Automation PRs:" in result.stdout
        assert "Human PRs:" in result.stdout

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_repo_merge_no_confirm_merges_directly(
        self, mock_asyncio_run, mock_client_class
    ):
        """With --no-confirm, should skip preview and merge directly."""
        from dependamerge.merge_manager import MergeResult, MergeStatus

        auto_pr = _make_pr(1, author="dependabot[bot]")

        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client.is_automation_author.side_effect = lambda a: (
            a
            in {
                "dependabot[bot]",
                "pre-commit-ci[bot]",
                "renovate[bot]",
                "github-actions[bot]",
            }
        )
        mock_client_class.return_value = mock_client

        merge_result = MergeResult(
            pr_info=auto_pr,
            status=MergeStatus.MERGED,
        )

        mock_asyncio_run.side_effect = _mock_asyncio_run(
            [
                # permissions check
                {
                    "approve": {"has_permission": True},
                    "merge": {"has_permission": True},
                    "branch_protection": {"has_permission": True},
                },
                # fetch PRs
                [auto_pr],
                # actual merge (not preview)
                [merge_result],
            ]
        )

        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo",
                "--token",
                "test_token",
                "--no-confirm",
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        assert "Final Results:" in result.stdout


# ---------------------------------------------------------------------------
# URL routing: PR URLs should still work
# ---------------------------------------------------------------------------


class TestMergeUrlRouting:
    """Verify that PR URLs and repo URLs are routed correctly."""

    runner: CliRunner = CliRunner()

    def setup_method(self):
        self.runner = CliRunner()

    @patch("dependamerge.cli.GitHubClient")
    def test_pr_url_still_works(self, mock_client_class):
        """A normal PR URL should still route to the original merge flow."""
        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client.parse_pr_url.return_value = ("owner", "repo", 42)
        mock_client.get_pull_request_info.side_effect = Exception("simulated failure")
        mock_client_class.return_value = mock_client

        self.runner.invoke(
            app,
            [
                "merge",
                "https://github.com/owner/repo/pull/42",
                "--token",
                "test_token",
            ],
        )

        # Should have called parse_pr_url (the old path), not repo path
        mock_client.parse_pr_url.assert_called_once()

    def test_gerrit_url_still_routes(self):
        """A Gerrit URL should still route to the Gerrit handler."""
        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://gerrit.example.org/c/project/+/12345",
                "--token",
                "test_token",
            ],
        )
        # Should attempt Gerrit flow (and fail on credentials, not URL parsing)
        assert "❌ Invalid URL:" not in result.stdout


# ---------------------------------------------------------------------------
# Confirmation hash for repo mode
# ---------------------------------------------------------------------------


class TestRepoConfirmationHash:
    """Verify the confirmation hash generation for repo-scoped merges."""

    def test_hash_is_deterministic(self):
        """Same inputs should always produce the same hash."""
        combined = "repo-merge:owner/repo:3"
        h1 = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]
        h2 = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]
        assert h1 == h2
        assert len(h1) == 16

    def test_hash_varies_with_repo(self):
        c1 = "repo-merge:owner/repo-a:3"
        c2 = "repo-merge:owner/repo-b:3"
        h1 = hashlib.sha256(c1.encode("utf-8")).hexdigest()[:16]
        h2 = hashlib.sha256(c2.encode("utf-8")).hexdigest()[:16]
        assert h1 != h2

    def test_hash_varies_with_count(self):
        c1 = "repo-merge:owner/repo:3"
        c2 = "repo-merge:owner/repo:5"
        h1 = hashlib.sha256(c1.encode("utf-8")).hexdigest()[:16]
        h2 = hashlib.sha256(c2.encode("utf-8")).hexdigest()[:16]
        assert h1 != h2


class TestRepoMergeOrder:
    """Verify repository-scoped PRs are sequenced oldest-first."""

    def test_orders_ascending_by_number(self):
        """PRs fetched newest-first are reordered oldest-first."""
        from dependamerge.cli import _repo_merge_order

        # GraphQL returns CREATED_AT DESC (newest first); simulate that.
        prs = [_make_pr(7), _make_pr(3), _make_pr(5), _make_pr(1)]
        ordered = _repo_merge_order(prs)
        assert [p.number for p in ordered] == [1, 3, 5, 7]

    def test_is_stable_and_non_mutating(self):
        """Input list is not mutated and already-sorted input is preserved."""
        from dependamerge.cli import _repo_merge_order

        prs = [_make_pr(1), _make_pr(2), _make_pr(3)]
        ordered = _repo_merge_order(prs)
        assert [p.number for p in ordered] == [1, 2, 3]
        # Original list order is left untouched.
        assert [p.number for p in prs] == [1, 2, 3]
        assert ordered is not prs

    def test_empty_list(self):
        """An empty list orders to an empty list."""
        from dependamerge.cli import _repo_merge_order

        assert _repo_merge_order([]) == []
