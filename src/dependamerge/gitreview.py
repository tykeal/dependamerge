# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Shared ``.gitreview`` file parsing and fetching utilities.

This module provides the **single source of truth** for reading,
parsing, and remotely fetching ``.gitreview`` files across the entire
``dependamerge`` package.  The implementation was previously inlined in
``github2gerrit_detector.py``; it is now extracted here for clarity and
to stay in sync with the sister ``github2gerrit-action`` project.

Design goals
~~~~~~~~~~~~

* **Pure parser** — :func:`parse_gitreview` is a zero-I/O function that
  accepts a string and returns a :class:`GitReviewInfo`.
* **Async GitHub API fetch** — :func:`fetch_gitreview_from_github` uses
  the ``GitHubAsync`` client already available in ``dependamerge`` to
  retrieve the file without a local clone.
* **Consistent regex** — a single set of precompiled patterns that
  tolerates optional whitespace around ``=`` and is case-insensitive on
  keys (both forms seen in the wild).
* **base_path derivation** — a static known-hosts table maps well-known
  Gerrit hostnames to their REST API base paths at parse time.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# ───────────────────────────────────────────────────────────────────────
# Constants
# ───────────────────────────────────────────────────────────────────────

DEFAULT_GERRIT_PORT: int = 29418
"""Default Gerrit SSH port when the ``port=`` line is absent."""

# ───────────────────────────────────────────────────────────────────────
# Precompiled regex patterns for INI-style .gitreview files
#
# These patterns:
#   • are multiline (``(?m)``)
#   • are case-insensitive on the key name (``(?i)``)
#   • tolerate optional horizontal whitespace around ``=``
#   • strip trailing whitespace / ``\r`` so Windows line endings are handled
# ───────────────────────────────────────────────────────────────────────

_HOST_RE = re.compile(r"(?mi)^host[ \t]*=[ \t]*(.+?)[ \t\r]*$")
_PORT_RE = re.compile(r"(?mi)^port[ \t]*=[ \t]*(\d+)[ \t\r]*$")
_PROJECT_RE = re.compile(r"(?mi)^project[ \t]*=[ \t]*(.+?)[ \t\r]*$")

# ───────────────────────────────────────────────────────────────────────
# Well-known Gerrit base paths
# ───────────────────────────────────────────────────────────────────────

_KNOWN_BASE_PATHS: dict[str, str] = {
    "gerrit.linuxfoundation.org": "infra",
}

# ───────────────────────────────────────────────────────────────────────
# Data model
# ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GitReviewInfo:
    """Parsed contents of a ``.gitreview`` file.

    A typical ``.gitreview`` looks like::

        [gerrit]
        host=gerrit.linuxfoundation.org
        port=29418
        project=releng/gerrit_to_platform.git

    Attributes:
        host: Gerrit server hostname
            (e.g. ``gerrit.linuxfoundation.org``).
        port: Gerrit SSH port (default 29418).  Not used for REST,
            but kept for completeness.
        project: Gerrit project path **without** the ``.git`` suffix.
        base_path: Optional REST API base path derived from well-known
            host conventions (e.g. ``"infra"`` for
            ``gerrit.linuxfoundation.org``).  ``None`` when the host is
            not in the known-hosts table.
    """

    host: str
    port: int = DEFAULT_GERRIT_PORT
    project: str = ""
    base_path: str | None = None

    @property
    def is_valid(self) -> bool:
        """Minimum validity: *host* must be non-empty."""
        return bool(self.host)


# ───────────────────────────────────────────────────────────────────────
# Base-path derivation
# ───────────────────────────────────────────────────────────────────────


def derive_base_path(host: str) -> str | None:
    """Return the REST API base path for well-known Gerrit hosts.

    Some Gerrit deployments (notably the Linux Foundation's) serve their
    REST API and web UI under a sub-path such as ``/infra``.

    This performs a **static, zero-I/O** lookup against a built-in table
    of known hosts.  For hosts not in the table it returns ``None`` —
    callers that need a definitive answer should fall back to dynamic
    discovery.

    Args:
        host: Gerrit server hostname (will be lowercased and stripped
            for lookup).

    Returns:
        Base path string (e.g. ``"infra"``) or ``None`` if the host is
        not in the known-hosts table.
    """
    return _KNOWN_BASE_PATHS.get(host.lower().strip())


# ───────────────────────────────────────────────────────────────────────
# Pure parser
# ───────────────────────────────────────────────────────────────────────


def parse_gitreview(text: str) -> GitReviewInfo | None:
    """Parse the raw text of a ``.gitreview`` file.

    The format is a simple INI-style file with a ``[gerrit]`` section
    containing ``host=``, ``port=``, and ``project=`` keys.

    This parser is intentionally lenient:

    * Keys are matched case-insensitively.
    * Optional whitespace around ``=`` is tolerated.
    * The ``[gerrit]`` section header itself is **not** required —
      the parser matches the key lines directly.

    Inline comments are **not** supported. The ``.gitreview`` format
    (as consumed by ``git-review``) is not a commented INI dialect, so
    ``#`` and ``;`` are ordinary characters. Consequently:

    * ``host=`` and ``project=`` capture the entire remainder of the
      line as the value, so any trailing ``# comment`` becomes part of
      the value (e.g. ``host=h # primary`` yields ``"h # primary"``).
    * ``port=`` matches digits only, so a line carrying a trailing
      comment fails to match and the parser falls back to
      :data:`DEFAULT_GERRIT_PORT` rather than raising.

    This mirrors ``git-review``'s own behaviour and is deliberately
    distinct from the netrc parser, which *does* strip inline comments.

    Args:
        text: Raw text content of a ``.gitreview`` file.

    Returns:
        A :class:`GitReviewInfo` if at least ``host`` is present and
        non-empty, otherwise ``None``.
    """
    host_match = _HOST_RE.search(text)
    if not host_match:
        log.debug(".gitreview: no host= line found")
        return None

    host = host_match.group(1).strip()
    if not host:
        log.debug(".gitreview: host= line is empty")
        return None

    port_match = _PORT_RE.search(text)
    port = int(port_match.group(1)) if port_match else DEFAULT_GERRIT_PORT

    project = ""
    project_match = _PROJECT_RE.search(text)
    if project_match:
        project = project_match.group(1).strip().removesuffix(".git")

    base_path = derive_base_path(host)

    info = GitReviewInfo(
        host=host,
        port=port,
        project=project,
        base_path=base_path,
    )
    log.debug(
        "Parsed .gitreview: host=%s, port=%d, project=%s, base_path=%s",
        info.host,
        info.port,
        info.project,
        info.base_path,
    )
    return info


# Backward-compatible alias so that existing code importing
# ``parse_gitreview_text`` from ``github2gerrit_detector`` continues
# to work after the delegation switch.
parse_gitreview_text = parse_gitreview
"""Alias for :func:`parse_gitreview`.

The previous inline implementation in ``github2gerrit_detector.py``
was named ``parse_gitreview_text``.  This alias preserves backward
compatibility for any callers still using the old name.
"""


# ───────────────────────────────────────────────────────────────────────
# Async remote fetch: GitHub Contents API
# ───────────────────────────────────────────────────────────────────────


async def fetch_gitreview_from_github(
    github_client: Any,
    owner: str,
    repo: str,
    ref: str | None = None,
) -> GitReviewInfo | None:
    """Fetch and parse the ``.gitreview`` file from a GitHub repository.

    This uses the GitHub REST API (``GET /repos/{owner}/{repo}/contents/``
    endpoint) to retrieve the file without needing a local clone.

    Args:
        github_client: An initialised :class:`~dependamerge.github_async.GitHubAsync`
            instance (or any object with an async ``get(endpoint)`` method).
        owner: Repository owner (org or user).
        repo: Repository name.
        ref: Optional git ref (branch/tag/SHA) to fetch from.  Defaults
            to the repository's default branch.

    Returns:
        A :class:`GitReviewInfo` if the file exists and is parseable,
        otherwise ``None``.
    """
    try:
        endpoint = f"/repos/{owner}/{repo}/contents/.gitreview"
        if ref:
            endpoint += f"?ref={ref}"

        data = await github_client.get(endpoint)

        if not isinstance(data, dict):
            log.debug(
                ".gitreview API response is not a dict for %s/%s", owner, repo
            )
            return None

        # The contents API returns base64-encoded content
        content_b64 = data.get("content", "")
        if not content_b64:
            log.debug(".gitreview has no content for %s/%s", owner, repo)
            return None

        text = base64.b64decode(content_b64).decode("utf-8")
        return parse_gitreview(text)

    except Exception as exc:
        # 404 (file not found) is expected for repos without .gitreview
        exc_str = str(exc)
        if "404" in exc_str or "Not Found" in exc_str:
            log.debug("No .gitreview file in %s/%s", owner, repo)
        else:
            log.debug(
                "Failed to fetch .gitreview from %s/%s: %s",
                owner,
                repo,
                exc,
            )
        return None
