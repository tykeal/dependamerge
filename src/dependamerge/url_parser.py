# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
URL detection and parsing for GitHub PRs and Gerrit changes.

This module provides unified URL parsing that distinguishes between GitHub
pull request URLs and Gerrit change URLs, extracting the necessary components
for each platform.

Supported URL formats:

GitHub:
    https://github.com/owner/repo/pull/123
    https://github.enterprise.com/owner/repo/pull/456

Gerrit:
    https://gerrit.linuxfoundation.org/infra/c/project/name/+/12345
    https://gerrit.example.org/c/project/+/67890
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


class ChangeSource(Enum):
    """Enumeration of supported code review platforms."""

    GITHUB = "github"
    GERRIT = "gerrit"


class UrlParseError(ValueError):
    """Raised when a URL cannot be parsed as a valid change URL."""


@dataclass(frozen=True)
class ParsedUrl:
    """
    Parsed change URL with platform-specific components.

    Attributes:
        source: The code review platform (GitHub or Gerrit).
        host: The hostname of the server.
        base_path: The base path for Gerrit servers (e.g., "infra").
                   None for GitHub or Gerrit without a base path.
        project: The project identifier. For GitHub this is "owner/repo",
                 for Gerrit this is the project path (e.g., "releng/tool").
        change_number: The PR number (GitHub) or change number (Gerrit).
        original_url: The original URL that was parsed.
    """

    source: ChangeSource
    host: str
    base_path: str | None
    project: str
    change_number: int
    original_url: str

    @property
    def is_github(self) -> bool:
        """Check if this URL is from GitHub."""
        return self.source == ChangeSource.GITHUB

    @property
    def is_gerrit(self) -> bool:
        """Check if this URL is from Gerrit."""
        return self.source == ChangeSource.GERRIT


@dataclass(frozen=True)
class ParsedOrgUrl:
    """
    Parsed organization/owner URL (not a specific repo or PR).

    Represents an owner-wide scope, e.g. ``https://github.com/owner``.
    The owner may be either a GitHub organization or a personal user
    account; the two are indistinguishable from the URL alone and are
    disambiguated at runtime when enumerating repositories.

    Attributes:
        source: The code review platform (GitHub only for now).
        host: The hostname of the server.
        owner: The organization or user login.
        original_url: The original URL that was parsed.
    """

    source: ChangeSource
    host: str
    owner: str
    original_url: str

    @property
    def is_github(self) -> bool:
        """Check if this URL is from GitHub."""
        return self.source == ChangeSource.GITHUB


@dataclass(frozen=True)
class ParsedRepoUrl:
    """
    Parsed repository URL (not a specific PR/change).

    Attributes:
        source: The code review platform (GitHub only for now).
        host: The hostname of the server.
        owner: The repository owner/organization.
        repo: The repository name.
        project: The full "owner/repo" string.
        original_url: The original URL that was parsed.
    """

    source: ChangeSource
    host: str
    owner: str
    repo: str
    project: str
    original_url: str

    @property
    def is_github(self) -> bool:
        """Check if this URL is from GitHub."""
        return self.source == ChangeSource.GITHUB


def _host_matches(
    hostname: str,
    target: str,
    *,
    allow_subdomains: bool = True,
) -> bool:
    """Check if hostname matches target using secure comparison.

    Uses exact equality or subdomain matching with a leading dot
    to prevent substring bypass attacks.

    SECURITY: This function is the approved way to check hostnames
    in this codebase. Do NOT use Python's ``in`` operator on hostname
    strings — see CodeQL rule py/incomplete-url-substring-sanitization.

    Args:
        hostname: The parsed hostname to check (lowercase).
        target: The target hostname to match against.
        allow_subdomains: If True, also matches \\*.target.

    Returns:
        True if hostname matches target or is a subdomain of target.
    """
    if not hostname or not target:
        return False
    hostname = hostname.lower()
    target = target.lower()
    if hostname == target:
        return True
    if allow_subdomains and hostname.endswith(f".{target}"):
        return True
    return False


def parse_change_url(url: str) -> ParsedUrl:
    """
    Parse a GitHub PR URL or Gerrit change URL.

    Args:
        url: The URL to parse.

    Returns:
        A ParsedUrl instance with the extracted components.

    Raises:
        UrlParseError: If the URL format is not recognized or invalid.
    """
    url = url.strip()
    if not url:
        raise UrlParseError("URL cannot be empty")

    # Ensure URL has a scheme
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise UrlParseError(f"Invalid URL format: {exc}") from exc

    if not parsed.hostname:
        raise UrlParseError("URL must include a hostname")

    host = parsed.hostname.lower()
    path = parsed.path.rstrip("/")

    # Detect platform based on URL characteristics
    if _is_github_url(host, path):
        return _parse_github_url(host, path, url)
    elif _is_gerrit_url(host, path):
        return _parse_gerrit_url(host, path, url)
    else:
        raise UrlParseError(
            f"Cannot determine platform for URL: {url}. "
            "Expected GitHub PR URL (containing /pull/) or "
            "Gerrit change URL (containing /c/.../+/)."
        )


def _is_github_url(host: str, path: str) -> bool:
    """Check if the URL is a GitHub URL using secure host comparison.

    SECURITY: Uses exact hostname matching via _host_matches(), not
    substring checks, to prevent bypass attacks via crafted hostnames.
    See CodeQL rule py/incomplete-url-substring-sanitization.

    Detection heuristics:
    - Host matches 'github.com' (exact or subdomain)
    - Path contains '/pull/' (for GitHub Enterprise with unknown hosts)
    """
    # SECURITY: Use _host_matches() — never use `"github.com" in host`
    if _host_matches(host, "github.com"):
        return True

    # Path-based detection for GitHub Enterprise with unknown hosts
    if "/pull/" in path:
        return True

    return False


def _is_gerrit_url(host: str, path: str) -> bool:
    """Check if the URL is a Gerrit URL using structural validation.

    SECURITY: Uses Gerrit's distinctive URL path structure rather than
    hostname substring matching. See CodeQL rule
    py/incomplete-url-substring-sanitization.

    Detection heuristics:
    - Path contains '/c/' and '/+/' (Gerrit change URL pattern)
    - Path starts with '/changes/' (Gerrit REST API pattern)
    """
    # Primary: Gerrit change URL structure is definitive
    if "/c/" in path and "/+/" in path:
        return True

    # Secondary: Gerrit REST API pattern
    if path.startswith("/changes/"):
        return True

    return False


def _parse_github_url(host: str, path: str, original_url: str) -> ParsedUrl:
    """
    Parse a GitHub pull request URL.

    Expected format: https://github.com/owner/repo/pull/123
    """
    # Pattern: /owner/repo/pull/number
    match = re.match(r"^/([^/]+)/([^/]+)/pull/(\d+)(?:/.*)?$", path)
    if not match:
        raise UrlParseError(
            f"Invalid GitHub PR URL format. Expected: "
            f"https://{host}/owner/repo/pull/123"
        )

    owner = match.group(1)
    repo = match.group(2)
    pr_number = int(match.group(3))

    return ParsedUrl(
        source=ChangeSource.GITHUB,
        host=host,
        base_path=None,
        project=f"{owner}/{repo}",
        change_number=pr_number,
        original_url=original_url,
    )


def _parse_gerrit_url(host: str, path: str, original_url: str) -> ParsedUrl:
    """
    Parse a Gerrit change URL.

    Expected formats:
        https://gerrit.example.org/c/project/+/12345
        https://gerrit.example.org/infra/c/project/name/+/12345

    The base_path (e.g., "infra") is optional and appears before /c/.
    """
    # Pattern: optional_base_path/c/project_path/+/number
    # The project path can contain multiple segments (e.g., releng/tool)
    match = re.match(r"^(?:/([^/]+))?/c/(.+)/\+/(\d+)(?:/.*)?$", path)

    if not match:
        # Try alternative pattern without base path
        match = re.match(r"^/c/(.+)/\+/(\d+)(?:/.*)?$", path)
        if match:
            base_path = None
            project = match.group(1)
            change_number = int(match.group(2))
        else:
            raise UrlParseError(
                f"Invalid Gerrit change URL format. Expected: "
                f"https://{host}/c/project/+/12345 or "
                f"https://{host}/base/c/project/+/12345"
            )
    else:
        base_path = match.group(1)  # May be None
        project = match.group(2)
        change_number = int(match.group(3))

    # Validate extracted components
    if not project:
        raise UrlParseError("Gerrit URL must include a project name")

    if change_number <= 0:
        raise UrlParseError("Gerrit change number must be positive")

    return ParsedUrl(
        source=ChangeSource.GERRIT,
        host=host,
        base_path=base_path,
        project=project,
        change_number=change_number,
        original_url=original_url,
    )


def detect_source(url: str) -> ChangeSource:
    """
    Detect the source platform from a URL without full parsing.

    This is a convenience function for quick platform detection.

    Args:
        url: The URL to analyze.

    Returns:
        The detected ChangeSource.

    Raises:
        UrlParseError: If the platform cannot be determined.
    """
    url = url.strip()
    if not url:
        raise UrlParseError("URL cannot be empty")

    # Ensure URL has a scheme for parsing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise UrlParseError(f"Invalid URL format: {exc}") from exc

    host = parsed.hostname.lower() if parsed.hostname else ""
    path = parsed.path.rstrip("/")

    if _is_github_url(host, path):
        return ChangeSource.GITHUB
    elif _is_gerrit_url(host, path):
        return ChangeSource.GERRIT
    else:
        raise UrlParseError(f"Cannot determine platform for URL: {url}")


def parse_repo_url(url: str) -> ParsedRepoUrl:
    """
    Parse a GitHub repository URL (not a specific PR).

    Supports formats:
        https://github.com/owner/repo
        https://github.com/owner/repo/
        https://github.com/owner/repo/pulls

    Args:
        url: The URL to parse.

    Returns:
        A ParsedRepoUrl instance with the extracted components.

    Raises:
        UrlParseError: If the URL format is not recognized as a valid repository URL.
    """
    url = url.strip()
    if not url:
        raise UrlParseError("URL cannot be empty")

    # Ensure URL has a scheme
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise UrlParseError(f"Invalid URL format: {exc}") from exc

    if not parsed.hostname:
        raise UrlParseError("URL must include a hostname")

    host = parsed.hostname.lower()
    path = parsed.path.rstrip("/")

    # Only github.com and actual subdomains of github.com (e.g.
    # foo.github.com) are accepted.  _host_matches() checks for an
    # exact match or a *.github.com suffix, so hosts like
    # github.enterprise.com (a subdomain of enterprise.com, NOT
    # github.com) are correctly rejected.
    #
    # GitHub Enterprise Server installations use arbitrary hostnames
    # (e.g. ghe.corp.example.com) that cannot be reliably distinguished
    # from non-GitHub hosts without explicit configuration.  GHE support
    # (both repo-merge and single-PR) requires host-aware API base URL
    # configuration, which is not yet implemented.
    if not _host_matches(host, "github.com"):
        raise UrlParseError(
            f"Repository URL parsing is only supported for "
            f"github.com hosts (got host: {host}). "
            f"Use a direct PR URL for non-GitHub hosts."
        )

    # Try to extract owner/repo from the path
    # Expected: /owner/repo or /owner/repo/pulls
    # Strip the path, remove "pulls" suffix if present
    parts = [p for p in path.split("/") if p]

    # Remove "pulls" suffix if present
    if parts and parts[-1] == "pulls":
        parts = parts[:-1]

    if len(parts) < 2:
        raise UrlParseError(
            f"Invalid GitHub repository URL format. Expected: "
            f"https://{host}/owner/repo"
        )

    # After stripping "pulls", require exactly 2 parts (owner/repo)
    if len(parts) != 2:
        # Check if this is a PR URL (owner/repo/pull/…) before giving a generic error.
        # Match any path starting with /owner/repo/pull/ regardless of whether
        # the PR segment is numeric — /pull/abc is still clearly a PR-shaped URL
        # and deserves the more specific guidance.
        if len(parts) >= 3 and parts[2] == "pull":
            raise UrlParseError(
                "This looks like a pull request URL, not a repository URL. "
                "Pass the full PR URL (…/pull/<number>) directly to merge "
                "a single PR, or use the repository URL (…/owner/repo) for "
                "bulk operations."
            )
        raise UrlParseError(
            f"Invalid GitHub repository URL format. Expected: "
            f"https://{host}/owner/repo"
        )

    owner = parts[0]
    repo = parts[1]

    return ParsedRepoUrl(
        source=ChangeSource.GITHUB,
        host=host,
        owner=owner,
        repo=repo,
        project=f"{owner}/{repo}",
        original_url=url,
    )


def parse_org_url(url: str) -> ParsedOrgUrl:
    """
    Parse a GitHub organization/owner URL (not a specific repo or PR).

    Supports the following owner-wide forms (trailing slashes are
    cosmetic and ignored):
        https://github.com/owner
        https://github.com/owner/
        https://github.com/orgs/owner
        https://github.com/orgs/owner/repositories

    The owner may be an organization or a personal user account; the two
    are indistinguishable here and are disambiguated at runtime when the
    repositories are enumerated.

    Args:
        url: The URL to parse.

    Returns:
        A ParsedOrgUrl instance with the extracted owner.

    Raises:
        UrlParseError: If the URL is not recognised as an owner-wide URL.
    """
    url = url.strip()
    if not url:
        raise UrlParseError("URL cannot be empty")

    # Ensure URL has a scheme
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise UrlParseError(f"Invalid URL format: {exc}") from exc

    if not parsed.hostname:
        raise UrlParseError("URL must include a hostname")

    host = parsed.hostname.lower()
    path = parsed.path.rstrip("/")

    # SECURITY: Only github.com and actual subdomains of github.com are
    # accepted.  GitHub Enterprise Server (GHE) uses arbitrary hostnames
    # that cannot be reliably distinguished from non-GitHub hosts without
    # explicit configuration.  This github.com-only guard is the single
    # choke point to relax when GHE owner-wide support is enabled (see
    # derive_api_urls() and the GHE tracking issue) — do NOT scatter
    # additional host checks elsewhere.
    if not _host_matches(host, "github.com"):
        raise UrlParseError(
            f"Owner-wide URL parsing is only supported for github.com "
            f"hosts (got host: {host}). GitHub Enterprise support is not "
            f"yet enabled — use a direct PR URL for non-github.com hosts."
        )

    parts = [p for p in path.split("/") if p]

    # Normalise the canonical GitHub org forms:
    #   /orgs/owner               -> owner
    #   /orgs/owner/repositories  -> owner
    if parts and parts[0] == "orgs":
        rest = parts[1:]
        if rest and rest[-1] == "repositories":
            rest = rest[:-1]
        if len(rest) != 1:
            raise UrlParseError(
                f"Invalid GitHub organization URL format. Expected: "
                f"https://{host}/orgs/owner"
            )
        owner = rest[0]
        return ParsedOrgUrl(
            source=ChangeSource.GITHUB,
            host=host,
            owner=owner,
            original_url=url,
        )

    # Bare owner form: exactly one path segment.
    if len(parts) != 1:
        raise UrlParseError(
            f"Invalid GitHub owner URL format. Expected: "
            f"https://{host}/owner (an organization or user login)"
        )

    owner = parts[0]
    return ParsedOrgUrl(
        source=ChangeSource.GITHUB,
        host=host,
        owner=owner,
        original_url=url,
    )


def parse_owner_arg(value: str) -> str:
    """Extract an owner login from a CLI argument.

    The owner-wide *report* commands (``status`` and ``blocked``) accept
    either a bare login or any of the GitHub owner URL forms that
    :func:`parse_org_url` understands.  This single helper normalises all
    of them to a plain login so the commands no longer rely on a naive
    ``split("/")[-1]`` that silently mis-parses the canonical
    ``/orgs/owner/repositories`` form (it would return ``repositories``).

    Accepted inputs:
        owner
        owner/
        https://github.com/owner
        https://github.com/owner/
        github.com/owner
        https://github.com/orgs/owner
        https://github.com/orgs/owner/repositories

    A bare token — optionally with one or more trailing slashes but no
    other path separator and no scheme — is treated as a login and
    returned verbatim (minus the trailing slashes).  This preserves the
    long-standing ability to pass just an organization/user name, and to
    pass ``owner/`` (the ``status``/``blocked`` commands historically
    accepted a trailing slash via ``rstrip("/")``).  Anything that still
    looks like a URL (an embedded ``/`` or a scheme) is delegated to
    :func:`parse_org_url`, which enforces the github.com-only guard and
    the canonical forms.

    Args:
        value: The raw CLI argument.

    Returns:
        The extracted owner login.

    Raises:
        UrlParseError: If ``value`` is empty or is a URL that is not a
            recognised github.com owner URL.
    """
    value = (value or "").strip()
    if not value:
        raise UrlParseError("Owner name or URL cannot be empty")

    # A bare login has no scheme and no embedded path separator once any
    # trailing slashes are removed; accept it as-is so plain names like
    # "lfreleng-actions" and the historical "lfreleng-actions/" form keep
    # working.
    bare = value.rstrip("/")
    if not bare:
        # The input was only slashes (e.g. "////"); there is no login to
        # extract, so treat it the same as an empty value.
        raise UrlParseError("Owner name or URL cannot be empty")
    if "/" not in bare and "://" not in bare:
        return bare

    return parse_org_url(value).owner


def derive_api_urls(host: str) -> tuple[str, str]:
    """Derive the (REST, GraphQL) API base URLs for a GitHub host.

    This is the single place that encodes the dotcom-vs-GHE base-URL
    rule.  github.com (and its subdomains) use the dedicated
    ``api.github.com`` host, while GitHub Enterprise Server installs
    serve the API from ``https://HOST/api/v3`` (REST) and
    ``https://HOST/api/graphql`` (GraphQL).

    GHE is not yet wired through the service/client constructors (the
    URL parsers still reject non-github.com hosts), but centralising
    the derivation here means enabling GHE later is a matter of relaxing
    that single guard and threading the returned URLs through — see the
    GHE tracking issue.

    Args:
        host: The hostname (e.g. ``github.com`` or ``ghe.example.com``).

    Returns:
        A ``(api_url, graphql_url)`` tuple.

    Raises:
        ValueError: If ``host`` is empty or whitespace-only, which would
            otherwise yield a subtly broken base URL such as
            ``https:///api/v3``.
    """
    host = (host or "").strip().lower()
    if not host:
        raise ValueError("derive_api_urls requires a non-empty host")
    if _host_matches(host, "github.com"):
        return ("https://api.github.com", "https://api.github.com/graphql")
    # GitHub Enterprise Server base URLs.
    return (f"https://{host}/api/v3", f"https://{host}/api/graphql")


__all__ = [
    "ChangeSource",
    "ParsedOrgUrl",
    "ParsedRepoUrl",
    "ParsedUrl",
    "UrlParseError",
    "_host_matches",
    "derive_api_urls",
    "detect_source",
    "parse_change_url",
    "parse_org_url",
    "parse_repo_url",
]
