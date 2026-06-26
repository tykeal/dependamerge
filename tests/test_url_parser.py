# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for URL detection and parsing module.

This module tests the url_parser module's ability to correctly identify
and parse GitHub PR URLs and Gerrit change URLs.
"""

import pytest

from dependamerge.url_parser import (
    ChangeSource,
    ParsedUrl,
    UrlParseError,
    _host_matches,
    detect_source,
    parse_change_url,
    parse_org_url,
    parse_owner_arg,
)


class TestChangeSource:
    """Tests for the ChangeSource enum."""

    def test_github_value(self):
        """Test GitHub enum value."""
        assert ChangeSource.GITHUB.value == "github"

    def test_gerrit_value(self):
        """Test Gerrit enum value."""
        assert ChangeSource.GERRIT.value == "gerrit"


class TestParsedUrl:
    """Tests for the ParsedUrl dataclass."""

    def test_is_github_property(self):
        """Test is_github property returns True for GitHub URLs."""
        parsed = ParsedUrl(
            source=ChangeSource.GITHUB,
            host="github.com",
            base_path=None,
            project="owner/repo",
            change_number=123,
            original_url="https://github.com/owner/repo/pull/123",
        )
        assert parsed.is_github is True
        assert parsed.is_gerrit is False

    def test_is_gerrit_property(self):
        """Test is_gerrit property returns True for Gerrit URLs."""
        parsed = ParsedUrl(
            source=ChangeSource.GERRIT,
            host="gerrit.example.org",
            base_path="infra",
            project="project/name",
            change_number=12345,
            original_url="https://gerrit.example.org/infra/c/project/name/+/12345",
        )
        assert parsed.is_gerrit is True
        assert parsed.is_github is False

    def test_frozen_dataclass(self):
        """Test that ParsedUrl is immutable (frozen)."""
        parsed = ParsedUrl(
            source=ChangeSource.GITHUB,
            host="github.com",
            base_path=None,
            project="owner/repo",
            change_number=123,
            original_url="https://github.com/owner/repo/pull/123",
        )
        with pytest.raises(AttributeError):
            parsed.change_number = 456  # type: ignore[misc]


class TestParseGitHubUrl:
    """Tests for parsing GitHub pull request URLs."""

    def test_standard_github_url(self):
        """Test parsing a standard github.com PR URL."""
        url = "https://github.com/owner/repo/pull/123"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.host == "github.com"
        assert result.base_path is None
        assert result.project == "owner/repo"
        assert result.change_number == 123
        assert result.original_url == url

    def test_github_url_with_trailing_slash(self):
        """Test parsing GitHub URL with trailing slash."""
        url = "https://github.com/owner/repo/pull/456/"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.change_number == 456
        assert result.project == "owner/repo"

    def test_github_url_with_files_tab(self):
        """Test parsing GitHub URL with /files suffix."""
        url = "https://github.com/owner/repo/pull/789/files"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.change_number == 789

    def test_github_url_with_commits_tab(self):
        """Test parsing GitHub URL with /commits suffix."""
        url = "https://github.com/owner/repo/pull/101/commits"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.change_number == 101

    def test_github_enterprise_url(self):
        """Test parsing GitHub Enterprise PR URL."""
        url = "https://github.mycompany.com/team/project/pull/42"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.host == "github.mycompany.com"
        assert result.project == "team/project"
        assert result.change_number == 42

    def test_github_url_without_scheme(self):
        """Test parsing GitHub URL without https:// prefix."""
        url = "github.com/owner/repo/pull/555"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.change_number == 555

    def test_github_url_with_http_scheme(self):
        """Test parsing GitHub URL with http:// prefix."""
        url = "http://github.com/owner/repo/pull/666"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.change_number == 666

    def test_github_url_org_with_dashes(self):
        """Test parsing GitHub URL with dashes in owner/repo."""
        url = "https://github.com/my-org/my-repo-name/pull/777"
        result = parse_change_url(url)

        assert result.project == "my-org/my-repo-name"
        assert result.change_number == 777

    def test_github_url_case_insensitive_host(self):
        """Test that host matching is case-insensitive."""
        url = "https://GitHub.COM/owner/repo/pull/888"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.host == "github.com"


class TestParseGerritUrl:
    """Tests for parsing Gerrit change URLs."""

    def test_gerrit_url_without_base_path(self):
        """Test parsing Gerrit URL without a base path."""
        url = "https://gerrit.example.org/c/project/+/12345"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GERRIT
        assert result.host == "gerrit.example.org"
        assert result.base_path is None
        assert result.project == "project"
        assert result.change_number == 12345
        assert result.original_url == url

    def test_gerrit_url_with_base_path(self):
        """Test parsing Gerrit URL with a base path."""
        url = "https://gerrit.linuxfoundation.org/infra/c/releng/gerrit_to_platform/+/74080"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GERRIT
        assert result.host == "gerrit.linuxfoundation.org"
        assert result.base_path == "infra"
        assert result.project == "releng/gerrit_to_platform"
        assert result.change_number == 74080

    def test_gerrit_url_nested_project(self):
        """Test parsing Gerrit URL with nested project path."""
        url = "https://gerrit.example.org/c/org/team/project/+/99999"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GERRIT
        assert result.project == "org/team/project"
        assert result.change_number == 99999

    def test_gerrit_url_with_trailing_slash(self):
        """Test parsing Gerrit URL with trailing slash."""
        url = "https://gerrit.example.org/c/project/+/11111/"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GERRIT
        assert result.change_number == 11111

    def test_gerrit_url_with_patchset(self):
        """Test parsing Gerrit URL with patchset suffix."""
        url = "https://gerrit.example.org/c/project/+/22222/3"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GERRIT
        assert result.change_number == 22222

    def test_gerrit_url_without_scheme(self):
        """Test parsing Gerrit URL without https:// prefix."""
        url = "gerrit.example.org/c/project/+/33333"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GERRIT
        assert result.change_number == 33333

    def test_gerrit_url_non_gerrit_hostname(self):
        """Test Gerrit URL detection by path pattern, not hostname."""
        url = "https://review.example.org/c/my-project/+/44444"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GERRIT
        assert result.host == "review.example.org"
        assert result.change_number == 44444

    def test_gerrit_url_case_insensitive_host(self):
        """Test that host matching is case-insensitive."""
        url = "https://GERRIT.Example.ORG/c/project/+/55555"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GERRIT
        assert result.host == "gerrit.example.org"


class TestInvalidUrls:
    """Tests for invalid URL handling."""

    def test_empty_url(self):
        """Test that empty URL raises UrlParseError."""
        with pytest.raises(UrlParseError, match="URL cannot be empty"):
            parse_change_url("")

    def test_whitespace_only_url(self):
        """Test that whitespace-only URL raises UrlParseError."""
        with pytest.raises(UrlParseError, match="URL cannot be empty"):
            parse_change_url("   ")

    def test_invalid_github_path(self):
        """Test that invalid GitHub path raises UrlParseError."""
        url = "https://github.com/owner/repo/issues/123"
        with pytest.raises(UrlParseError, match="Invalid GitHub PR URL format"):
            parse_change_url(url)

    def test_github_missing_pr_number(self):
        """Test that GitHub URL without PR number raises error."""
        url = "https://github.com/owner/repo/pull/"
        with pytest.raises(UrlParseError, match="Invalid GitHub PR URL format"):
            parse_change_url(url)

    def test_github_non_numeric_pr_number(self):
        """Test that GitHub URL with non-numeric PR raises error."""
        url = "https://github.com/owner/repo/pull/abc"
        with pytest.raises(UrlParseError, match="Invalid GitHub PR URL format"):
            parse_change_url(url)

    def test_invalid_gerrit_path(self):
        """Test that invalid Gerrit path raises UrlParseError."""
        # This URL has /changes/ prefix so it is detected as Gerrit,
        # but /changes/12345 does not match the /c/.../+/ regex so
        # parsing fails with an invalid format error.
        url = "https://gerrit.example.org/changes/12345"
        with pytest.raises(UrlParseError, match="Invalid Gerrit change URL format"):
            parse_change_url(url)

    def test_gerrit_missing_change_number(self):
        """Test that Gerrit URL without change number raises error."""
        # After trailing-slash stripping the path becomes /c/project/+
        # which no longer contains /+/ so the URL is not recognised as
        # Gerrit at all — it falls through to "Cannot determine platform".
        url = "https://gerrit.example.org/c/project/+/"
        with pytest.raises(UrlParseError, match="Cannot determine platform"):
            parse_change_url(url)

    def test_unknown_platform(self):
        """Test that unknown platform raises UrlParseError."""
        url = "https://unknown-review.example.org/some/path"
        with pytest.raises(UrlParseError, match="Cannot determine platform"):
            parse_change_url(url)

    def test_changes_path_detected_as_gerrit(self):
        """Test that /changes/ path prefix triggers Gerrit detection."""
        # The /changes/ REST API pattern is now a Gerrit heuristic,
        # but it does not match the /c/.../+/ regex so parsing fails.
        url = "https://unknown-review.example.org/changes/123"
        with pytest.raises(UrlParseError, match="Invalid Gerrit change URL format"):
            parse_change_url(url)

    def test_url_without_hostname(self):
        """Test that URL without hostname raises UrlParseError."""
        url = "/owner/repo/pull/123"
        with pytest.raises(UrlParseError, match="URL must include a hostname"):
            parse_change_url(url)


class TestDetectSource:
    """Tests for the detect_source convenience function."""

    def test_detect_github(self):
        """Test detecting GitHub as the source."""
        url = "https://github.com/owner/repo/pull/123"
        assert detect_source(url) == ChangeSource.GITHUB

    def test_detect_gerrit(self):
        """Test detecting Gerrit as the source."""
        url = "https://gerrit.example.org/c/project/+/12345"
        assert detect_source(url) == ChangeSource.GERRIT

    def test_detect_github_by_pull_path(self):
        """Test detecting GitHub by /pull/ path pattern."""
        url = "https://custom-git.example.org/owner/repo/pull/99"
        assert detect_source(url) == ChangeSource.GITHUB

    def test_detect_gerrit_by_change_path(self):
        """Test detecting Gerrit by /c/.../+/ path pattern."""
        url = "https://review.example.org/c/project/+/12345"
        assert detect_source(url) == ChangeSource.GERRIT

    def test_detect_empty_url_raises(self):
        """Test that empty URL raises UrlParseError."""
        with pytest.raises(UrlParseError, match="URL cannot be empty"):
            detect_source("")

    def test_detect_unknown_raises(self):
        """Test that unknown platform raises UrlParseError."""
        url = "https://example.org/some/path"
        with pytest.raises(UrlParseError, match="Cannot determine platform"):
            detect_source(url)


class TestEdgeCases:
    """Tests for edge cases and special scenarios."""

    def test_github_url_with_query_params(self):
        """Test parsing GitHub URL with query parameters."""
        # Note: query params are typically stripped by the browser,
        # but we should handle them gracefully
        url = "https://github.com/owner/repo/pull/123?diff=split"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.change_number == 123

    def test_gerrit_url_with_query_params(self):
        """Test parsing Gerrit URL with query parameters."""
        url = "https://gerrit.example.org/c/project/+/12345?tab=files"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GERRIT
        assert result.change_number == 12345

    def test_github_url_with_fragment(self):
        """Test parsing GitHub URL with fragment identifier."""
        url = "https://github.com/owner/repo/pull/123#discussion_r123456"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.change_number == 123

    def test_gerrit_url_with_fragment(self):
        """Test parsing Gerrit URL with fragment identifier."""
        url = "https://gerrit.example.org/c/project/+/12345#/c/project/+/12345/1"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GERRIT
        assert result.change_number == 12345

    def test_github_url_large_pr_number(self):
        """Test parsing GitHub URL with large PR number."""
        url = "https://github.com/owner/repo/pull/999999999"
        result = parse_change_url(url)

        assert result.change_number == 999999999

    def test_gerrit_url_large_change_number(self):
        """Test parsing Gerrit URL with large change number."""
        url = "https://gerrit.example.org/c/project/+/999999999"
        result = parse_change_url(url)

        assert result.change_number == 999999999

    def test_github_url_preserves_original(self):
        """Test that original URL is preserved in result."""
        original = "https://github.com/owner/repo/pull/123"
        result = parse_change_url(original)

        assert result.original_url == original

    def test_gerrit_url_preserves_original(self):
        """Test that original URL is preserved in result."""
        original = "https://gerrit.example.org/c/project/+/12345"
        result = parse_change_url(original)

        assert result.original_url == original

    def test_url_with_leading_whitespace(self):
        """Test that leading whitespace is stripped."""
        url = "   https://github.com/owner/repo/pull/123"
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.change_number == 123

    def test_url_with_trailing_whitespace(self):
        """Test that trailing whitespace is stripped."""
        url = "https://github.com/owner/repo/pull/123   "
        result = parse_change_url(url)

        assert result.source == ChangeSource.GITHUB
        assert result.change_number == 123

    def test_gerrit_single_segment_project(self):
        """Test Gerrit URL with single-segment project name."""
        url = "https://gerrit.example.org/c/myproject/+/12345"
        result = parse_change_url(url)

        assert result.project == "myproject"

    def test_gerrit_deeply_nested_project(self):
        """Test Gerrit URL with deeply nested project path."""
        url = "https://gerrit.example.org/c/org/division/team/project/+/12345"
        result = parse_change_url(url)

        assert result.project == "org/division/team/project"


class TestHostMatches:
    """Tests for the _host_matches secure hostname comparison.

    SECURITY: These tests verify that hostname matching uses exact
    comparison (not substring checks) to prevent bypass attacks.
    See CodeQL rule py/incomplete-url-substring-sanitization.
    """

    def test_exact_match(self):
        """Test exact hostname match."""
        assert _host_matches("github.com", "github.com") is True

    def test_subdomain_match(self):
        """Test subdomain matching with leading dot."""
        assert _host_matches("api.github.com", "github.com") is True

    def test_deep_subdomain_match(self):
        """Test deeply nested subdomain matching."""
        assert _host_matches("a.b.c.github.com", "github.com") is True

    def test_case_insensitive(self):
        """Test case-insensitive comparison."""
        assert _host_matches("GitHub.COM", "github.com") is True
        assert _host_matches("github.com", "GitHub.COM") is True

    def test_no_subdomain_matching(self):
        """Test disabling subdomain matching."""
        assert (
            _host_matches("api.github.com", "github.com", allow_subdomains=False)
            is False
        )
        assert _host_matches("github.com", "github.com", allow_subdomains=False) is True

    def test_rejects_substring_bypass(self):
        """Test that substring bypass attacks are rejected."""
        # evil-github.com should NOT match github.com
        assert _host_matches("evil-github.com", "github.com") is False

    def test_rejects_prefix_bypass(self):
        """Test that prefix-appended bypass is rejected."""
        # github.com.attacker.net should NOT match github.com
        assert _host_matches("github.com.attacker.net", "github.com") is False

    def test_rejects_suffix_bypass(self):
        """Test that suffix-appended bypass is rejected."""
        # notgithub.com should NOT match github.com
        assert _host_matches("notgithub.com", "github.com") is False

    def test_empty_hostname(self):
        """Test that empty hostname returns False."""
        assert _host_matches("", "github.com") is False

    def test_empty_target(self):
        """Test that empty target returns False."""
        assert _host_matches("github.com", "") is False

    def test_both_empty(self):
        """Test that both empty returns False."""
        assert _host_matches("", "") is False


class TestUrlBypassPrevention:
    """Tests that crafted URLs cannot bypass platform detection.

    SECURITY: These tests verify that the URL parser correctly
    rejects URLs designed to exploit substring matching.
    See CodeQL rule py/incomplete-url-substring-sanitization.
    """

    def test_rejects_evil_github_hostname(self):
        """Test that evil-github.com is not treated as GitHub."""
        url = "https://evil-github.com/owner/repo/issues/123"
        with pytest.raises(UrlParseError, match="Cannot determine platform"):
            parse_change_url(url)

    def test_rejects_github_com_in_path(self):
        """Test that github.com in path does not trick detection."""
        url = "https://evil.com/github.com/owner/repo"
        with pytest.raises(UrlParseError, match="Cannot determine platform"):
            parse_change_url(url)

    def test_github_com_subdomain_suffix_uses_ghe_heuristic(self):
        """Test that github.com.attacker.net is NOT matched as github.com.

        The exact-match check for github.com correctly rejects this
        host.  However, the /pull/ path-based heuristic for GitHub
        Enterprise still matches, so the URL parses as a GHE instance
        with the attacker's hostname.  Callers that care about the
        canonical github.com should compare result.host explicitly.
        """
        url = "https://github.com.attacker.net/owner/repo/pull/1"
        result = parse_change_url(url)
        # Hostname must NOT be normalised to "github.com"
        assert result.host == "github.com.attacker.net"
        # Classified via the GHE /pull/ path heuristic, not the
        # exact github.com host match
        assert result.source == ChangeSource.GITHUB

    def test_rejects_gerrit_substring_in_hostname(self):
        """Test that bare 'gerrit' substring no longer triggers detection."""
        # Previously, "gerrit" in host would match. Now it does not.
        url = "https://not-a-gerrit-server.evil.org/dashboard"
        with pytest.raises(UrlParseError, match="Cannot determine platform"):
            parse_change_url(url)


class TestParseOwnerArg:
    """Tests for ``parse_owner_arg`` owner-login extraction.

    These cover every owner URL form the ``status`` and ``blocked``
    commands advertise support for.  An organization login and a
    personal user login are indistinguishable at parse time (the account
    type is resolved later at runtime), so the same parsing applies to
    both — the cases below intentionally mix org-style and user-style
    logins.
    """

    def test_bare_login(self):
        """A plain login is returned verbatim."""
        assert parse_owner_arg("lfreleng-actions") == "lfreleng-actions"

    def test_bare_login_user_account(self):
        """A personal user login parses identically to an org login."""
        assert (
            parse_owner_arg("ModeSevenIndustrialSolutions")
            == "ModeSevenIndustrialSolutions"
        )

    def test_bare_login_with_surrounding_whitespace(self):
        assert parse_owner_arg("  lfreleng-actions  ") == "lfreleng-actions"

    def test_bare_login_with_trailing_slash(self):
        """A bare login plus a trailing slash stays a login.

        Regression guard: ``status``/``blocked`` historically accepted
        ``owner/`` via ``rstrip("/")``, so it must not be misread as a
        URL whose host is ``owner``.
        """
        assert parse_owner_arg("lfreleng-actions/") == "lfreleng-actions"

    def test_bare_login_with_multiple_trailing_slashes(self):
        assert parse_owner_arg("lfreleng-actions//") == "lfreleng-actions"

    def test_https_owner_url(self):
        assert (
            parse_owner_arg("https://github.com/lfreleng-actions") == "lfreleng-actions"
        )

    def test_https_owner_url_trailing_slash(self):
        assert (
            parse_owner_arg("https://github.com/lfreleng-actions/")
            == "lfreleng-actions"
        )

    def test_https_user_url(self):
        assert (
            parse_owner_arg("https://github.com/ModeSevenIndustrialSolutions")
            == "ModeSevenIndustrialSolutions"
        )

    def test_owner_url_without_scheme(self):
        assert parse_owner_arg("github.com/lfreleng-actions") == "lfreleng-actions"

    def test_orgs_owner_url(self):
        assert (
            parse_owner_arg("https://github.com/orgs/lfreleng-actions")
            == "lfreleng-actions"
        )

    def test_orgs_owner_repositories_url(self):
        """The canonical ``/orgs/owner/repositories`` form must resolve.

        The old naive ``split('/')[-1]`` parsing returned
        ``repositories`` here — this case guards against that regression.
        """
        assert (
            parse_owner_arg("https://github.com/orgs/lfreleng-actions/repositories")
            == "lfreleng-actions"
        )

    def test_empty_string_raises(self):
        with pytest.raises(UrlParseError):
            parse_owner_arg("")

    def test_whitespace_only_raises(self):
        with pytest.raises(UrlParseError):
            parse_owner_arg("   ")

    def test_non_github_host_url_raises(self):
        with pytest.raises(UrlParseError):
            parse_owner_arg("https://gitlab.com/some-owner")

    def test_pr_url_is_rejected(self):
        """A full PR URL is not an owner URL and must be rejected."""
        with pytest.raises(UrlParseError):
            parse_owner_arg("https://github.com/owner/repo/pull/1")


class TestParseOrgUrlForms:
    """Tests for ``parse_org_url`` across the supported owner URL forms."""

    def test_bare_owner(self):
        result = parse_org_url("https://github.com/lfreleng-actions")
        assert result.owner == "lfreleng-actions"
        assert result.host == "github.com"
        assert result.is_github is True

    def test_orgs_owner_repositories(self):
        result = parse_org_url("https://github.com/orgs/lfreleng-actions/repositories")
        assert result.owner == "lfreleng-actions"

    def test_user_account_owner(self):
        result = parse_org_url("https://github.com/ModeSevenIndustrialSolutions")
        assert result.owner == "ModeSevenIndustrialSolutions"

    def test_non_github_host_rejected(self):
        with pytest.raises(UrlParseError):
            parse_org_url("https://gitlab.com/some-owner")
