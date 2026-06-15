# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
GitHub2Gerrit detection utilities for dependamerge.

This module detects GitHub pull requests that have corresponding Gerrit changes
created by the GitHub2Gerrit workflow. It parses the structured mapping comments
that GitHub2Gerrit posts on PRs to extract Change-IDs, topics, and other
metadata needed to locate and submit the corresponding Gerrit change.

``.gitreview`` parsing and fetching are delegated to :mod:`dependamerge.gitreview`.
This module re-exports :class:`~dependamerge.gitreview.GitReviewInfo`,
:func:`~dependamerge.gitreview.parse_gitreview_text`, and
:func:`~dependamerge.gitreview.fetch_gitreview_from_github` so that existing
callers continue to work without import changes.

The mapping comment format is defined by the github2gerrit-action project and
uses HTML markers for reliable parsing:

    <!-- github2gerrit:change-id-map v1 -->
    PR: https://github.com/owner/repo/pull/41
    Mode: squash
    Topic: GH-repo-41
    Change-Ids:
      I6a9987bd1b1cf1e4975dd5da2fb26b6b35ee0048
    GitHub-Hash: 41b89b8d5055be4e
    ...
    <!-- end github2gerrit:change-id-map -->
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Re-export .gitreview symbols so existing callers don't need to change
# their imports.  The canonical implementation lives in gitreview.py.
from .gitreview import (
    GitReviewInfo,
    fetch_gitreview_from_github,
    parse_gitreview_text,
)

__all__ = [
    # Re-exported .gitreview symbols (backward-compatible API).
    "GitReviewInfo",
    "fetch_gitreview_from_github",
    "parse_gitreview_text",
    # Public API defined in this module.
    "GITHUB2GERRIT_BOT_AUTHORS",
    "GitHub2GerritMode",
    "GitHub2GerritMapping",
    "GitHub2GerritDetectionResult",
    "detect_github2gerrit_comments",
    "detect_github2gerrit_from_graphql_comments",
    "has_github2gerrit_comments",
    "build_gerrit_change_url_from_mapping",
    "build_gerrit_submission_comment",
    "build_gerrit_skip_message",
]

log = logging.getLogger("dependamerge.github2gerrit_detector")

# HTML markers used by github2gerrit-action to delimit mapping blocks
_START_MARKER = "<!-- github2gerrit:change-id-map v1 -->"
_END_MARKER = "<!-- end github2gerrit:change-id-map -->"

# Fallback heuristic patterns for comments that lack the HTML markers but
# are clearly from the github2gerrit bot (e.g., older versions).
_CHANGE_ID_PATTERN = re.compile(r"\bI[0-9a-f]{40}\b")
_TOPIC_PATTERN = re.compile(r"Topic:\s*(GH-\S+)")
_MODE_PATTERN = re.compile(r"Mode:\s*(squash|multi-commit)")
_GITHUB_HASH_PATTERN = re.compile(r"GitHub-Hash:\s*([0-9a-f]+)")

# Author names used by the GitHub Actions bot that posts mapping comments
GITHUB2GERRIT_BOT_AUTHORS = frozenset(
    {
        "github-actions",
        "github-actions[bot]",
    }
)


class GitHub2GerritMode(str, Enum):
    """GitHub2Gerrit submission mode."""

    SQUASH = "squash"
    MULTI_COMMIT = "multi-commit"


@dataclass(frozen=True)
class GitHub2GerritMapping:
    """
    Parsed GitHub2Gerrit mapping extracted from a PR comment.

    Attributes:
        pr_url: The GitHub PR URL recorded in the mapping comment.
        mode: The submission mode (squash or multi-commit).
        topic: The Gerrit topic name (e.g., ``GH-repo-41``).
        change_ids: Ordered list of Gerrit Change-IDs (I-prefixed SHA-1).
        github_hash: The GitHub-Hash trailer value used for verification.
        raw_comment_body: The full comment body the mapping was extracted from.
    """

    pr_url: str
    mode: str
    topic: str
    change_ids: tuple[str, ...]
    github_hash: str = ""
    raw_comment_body: str = ""

    @property
    def primary_change_id(self) -> str:
        """Return the first (primary) Change-ID."""
        return self.change_ids[0] if self.change_ids else ""

    @property
    def is_valid(self) -> bool:
        """Check whether the mapping has the minimum required fields."""
        return bool(self.topic and self.change_ids and self.mode)


@dataclass
class GitHub2GerritDetectionResult:
    """
    Result of scanning a pull request for GitHub2Gerrit mapping comments.

    Attributes:
        has_mapping: True if at least one valid mapping comment was found.
        mapping: The latest valid mapping (if any).
        comment_indices: Indices into the comment list that contained mappings.
        detection_source: How the mapping was detected ("marker" or "heuristic").
    """

    has_mapping: bool = False
    mapping: GitHub2GerritMapping | None = None
    comment_indices: list[int] = field(default_factory=list)
    detection_source: str = ""


def detect_github2gerrit_comments(
    comments: list[dict[str, Any]],
) -> GitHub2GerritDetectionResult:
    """
    Scan PR comments for GitHub2Gerrit mapping blocks.

    This function examines issue comments (not review comments) on a pull
    request to find structured mapping comments posted by the GitHub2Gerrit
    action bot.

    It first tries to find comments with the well-known HTML markers.  If
    none are found, it falls back to a heuristic that looks for comments
    from ``github-actions[bot]`` that contain Change-ID patterns and other
    GitHub2Gerrit metadata.

    Args:
        comments: List of comment dicts as returned by the GitHub REST API
                  ``GET /repos/{owner}/{repo}/issues/{number}/comments`` or
                  the GraphQL ``comments`` connection nodes.  Each dict is
                  expected to have at least ``body`` (str) and optionally
                  ``author``/``user`` with a ``login`` field.

    Returns:
        A ``GitHub2GerritDetectionResult`` indicating whether a mapping was
        found and, if so, the parsed mapping data.
    """
    if not comments:
        return GitHub2GerritDetectionResult()

    # Extract comment bodies with their indices
    bodies_with_index: list[tuple[int, str, dict[str, Any]]] = []
    for idx, comment in enumerate(comments):
        body = _extract_body(comment)
        if body:
            bodies_with_index.append((idx, body, comment))

    if not bodies_with_index:
        return GitHub2GerritDetectionResult()

    # --- Pass 1: look for structured HTML markers ---
    result = _detect_via_markers(bodies_with_index)
    if result.has_mapping:
        return result

    # --- Pass 2: heuristic detection ---
    return _detect_via_heuristic(bodies_with_index)


def detect_github2gerrit_from_graphql_comments(
    pr_node: dict[str, Any],
) -> GitHub2GerritDetectionResult:
    """
    Convenience wrapper for GraphQL PR nodes.

    The dependamerge GraphQL queries already fetch ``comments`` for each PR.
    This function extracts the comment nodes and delegates to
    :func:`detect_github2gerrit_comments`.

    Args:
        pr_node: A PR node dict from the GraphQL response, expected to have
                 ``comments.nodes`` with ``author.login`` and ``body``.

    Returns:
        Detection result.
    """
    comments_connection = pr_node.get("comments") or {}
    comment_nodes = comments_connection.get("nodes") or []
    return detect_github2gerrit_comments(comment_nodes)


def has_github2gerrit_comments(comments: list[dict[str, Any]]) -> bool:
    """
    Quick boolean check for GitHub2Gerrit mapping comments.

    This is a lightweight alternative to :func:`detect_github2gerrit_comments`
    when only a yes/no answer is needed.

    Args:
        comments: List of comment dicts (same format as
                  :func:`detect_github2gerrit_comments`).

    Returns:
        True if any comment contains a GitHub2Gerrit mapping.
    """
    for comment in comments:
        body = _extract_body(comment)
        if not body:
            continue
        # Fast path: check for the HTML marker
        if _START_MARKER in body:
            return True
        # Slower heuristic path
        author = _extract_author(comment)
        if author in GITHUB2GERRIT_BOT_AUTHORS and _looks_like_mapping(body):
            return True
    return False


# ---------------------------------------------------------------------------
# Gerrit change URL construction helpers
# ---------------------------------------------------------------------------


def build_gerrit_change_url_from_mapping(
    mapping: GitHub2GerritMapping,
    gerrit_host: str,
    gerrit_base_path: str | None = None,
) -> str:
    """
    Build a Gerrit web change URL from mapping metadata.

    This constructs a URL suitable for posting as a comment on the GitHub PR
    after the Gerrit change has been submitted.  URL construction is delegated
    to :class:`~dependamerge.gerrit.urls.GerritUrlBuilder` to ensure the
    base path is handled consistently.

    Args:
        mapping: The parsed mapping containing Change-IDs and topic.
        gerrit_host: Gerrit server hostname.
        gerrit_base_path: Optional base path (e.g., ``"infra"``).

    Returns:
        A Gerrit change URL string.  If the exact change number is not
        available in the mapping, returns a search URL using the Change-ID.
    """
    from dependamerge.gerrit.urls import GerritUrlBuilder

    builder = GerritUrlBuilder(
        host=gerrit_host, base_path=gerrit_base_path, auto_discover=False
    )

    # Use the primary Change-ID for the search URL
    change_id = mapping.primary_change_id
    if change_id:
        return builder.web_url(f"q/{change_id}")
    return builder.web_url()


def build_gerrit_submission_comment(
    mapping: GitHub2GerritMapping,
    gerrit_url: str | None = None,
) -> str:
    """
    Build the GitHub PR comment body to post after submitting in Gerrit.

    This follows the comment conventions established by github2gerrit-action
    for consistency.

    Args:
        mapping: The parsed mapping.
        gerrit_url: Optional Gerrit change URL to include.

    Returns:
        Formatted comment body.
    """
    lines = [
        "**Automated PR Closure**",
        "",
        "This pull request has been automatically closed by dependamerge.",
        "",
    ]

    if gerrit_url:
        lines.extend(
            [
                "The corresponding Gerrit change has been reviewed (+2) "
                + "and submitted ✅",
                "",
                f"Gerrit change URL: {gerrit_url}",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "The corresponding Gerrit change has been reviewed (+2) "
                + "and submitted ✅",
                "",
            ]
        )

    lines.extend(
        [
            "The changes from this PR are now part of the main codebase "
            + "in Gerrit.",
            "",
            "---",
            "*This is an automated action performed by dependamerge "
            + "(GitHub2Gerrit awareness).*",
        ]
    )

    return "\n".join(lines)


def build_gerrit_skip_message(
    mapping: GitHub2GerritMapping,
) -> str:
    """
    Build a human-readable skip reason for PRs with GitHub2Gerrit mappings.

    Args:
        mapping: The parsed mapping.

    Returns:
        A short descriptive string for log/UI output.
    """
    change_id_short = mapping.primary_change_id[:12] if mapping.primary_change_id else "unknown"
    return (
        f"GitHub2Gerrit PR (topic: {mapping.topic}, "
        f"Change-Id: {change_id_short}...)"
    )


# ---------------------------------------------------------------------------
# .gitreview fetching via GitHub API
# ---------------------------------------------------------------------------


# NOTE: fetch_gitreview_from_github is re-exported at the top of this
# module from dependamerge.gitreview — no inline implementation needed.


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_body(comment: dict[str, Any]) -> str:
    """Extract the comment body text from various dict shapes."""
    body = comment.get("body") or ""
    if isinstance(body, str):
        return body.strip()
    return ""


def _extract_author(comment: dict[str, Any]) -> str:
    """Extract the comment author login from various dict shapes."""
    # GraphQL shape: { author: { login: "..." } }
    author_obj = comment.get("author") or {}
    if isinstance(author_obj, dict):
        login = author_obj.get("login") or ""
        if login:
            return login.lower()

    # REST shape: { user: { login: "..." } }
    user_obj = comment.get("user") or {}
    if isinstance(user_obj, dict):
        login = user_obj.get("login") or ""
        if login:
            return login.lower()

    return ""


def _looks_like_mapping(body: str) -> bool:
    """
    Heuristic check: does this comment body look like a GitHub2Gerrit
    mapping comment even without HTML markers?
    """
    has_change_id = bool(_CHANGE_ID_PATTERN.search(body))
    has_topic = bool(_TOPIC_PATTERN.search(body))
    has_mode = bool(_MODE_PATTERN.search(body))

    # Require at least Change-ID + one of topic or mode
    return has_change_id and (has_topic or has_mode)


def _detect_via_markers(
    bodies_with_index: list[tuple[int, str, dict[str, Any]]],
) -> GitHub2GerritDetectionResult:
    """Detect mapping comments using the well-known HTML markers."""
    latest_mapping: GitHub2GerritMapping | None = None
    indices: list[int] = []

    for idx, body, _comment in bodies_with_index:
        if _START_MARKER not in body or _END_MARKER not in body:
            continue

        mapping = _parse_marker_block(body)
        if mapping and mapping.is_valid:
            latest_mapping = mapping
            indices.append(idx)
            log.debug(
                "Found GitHub2Gerrit mapping (marker) at comment index %d: "
                "topic=%s, change_ids=%d",
                idx,
                mapping.topic,
                len(mapping.change_ids),
            )

    if latest_mapping:
        return GitHub2GerritDetectionResult(
            has_mapping=True,
            mapping=latest_mapping,
            comment_indices=indices,
            detection_source="marker",
        )

    return GitHub2GerritDetectionResult()


def _detect_via_heuristic(
    bodies_with_index: list[tuple[int, str, dict[str, Any]]],
) -> GitHub2GerritDetectionResult:
    """Detect mapping comments using heuristic pattern matching."""
    latest_mapping: GitHub2GerritMapping | None = None
    indices: list[int] = []

    for idx, body, comment in bodies_with_index:
        author = _extract_author(comment)
        if author not in GITHUB2GERRIT_BOT_AUTHORS:
            continue

        if not _looks_like_mapping(body):
            continue

        mapping = _parse_heuristic(body)
        if mapping and mapping.is_valid:
            latest_mapping = mapping
            indices.append(idx)
            log.debug(
                "Found GitHub2Gerrit mapping (heuristic) at comment index %d: "
                "topic=%s, change_ids=%d",
                idx,
                mapping.topic,
                len(mapping.change_ids),
            )

    if latest_mapping:
        return GitHub2GerritDetectionResult(
            has_mapping=True,
            mapping=latest_mapping,
            comment_indices=indices,
            detection_source="heuristic",
        )

    return GitHub2GerritDetectionResult()


def _parse_marker_block(body: str) -> GitHub2GerritMapping | None:
    """Parse a mapping from a comment body containing HTML markers."""
    start_idx = body.find(_START_MARKER)
    end_idx = body.find(_END_MARKER)

    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None

    block = body[start_idx + len(_START_MARKER) : end_idx].strip()
    return _parse_block_lines(block, body)


def _parse_heuristic(body: str) -> GitHub2GerritMapping | None:
    """Parse mapping fields from a comment body using regex patterns."""
    change_ids = _CHANGE_ID_PATTERN.findall(body)
    if not change_ids:
        return None

    topic_match = _TOPIC_PATTERN.search(body)
    topic = topic_match.group(1) if topic_match else ""

    mode_match = _MODE_PATTERN.search(body)
    mode = mode_match.group(1) if mode_match else "squash"

    hash_match = _GITHUB_HASH_PATTERN.search(body)
    github_hash = hash_match.group(1) if hash_match else ""

    # Try to extract PR URL
    pr_url = ""
    pr_match = re.search(r"PR:\s*(https?://\S+)", body)
    if pr_match:
        pr_url = pr_match.group(1)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_ids: list[str] = []
    for cid in change_ids:
        if cid not in seen:
            seen.add(cid)
            unique_ids.append(cid)

    return GitHub2GerritMapping(
        pr_url=pr_url,
        mode=mode,
        topic=topic,
        change_ids=tuple(unique_ids),
        github_hash=github_hash,
        raw_comment_body=body,
    )


def _parse_block_lines(block: str, full_body: str) -> GitHub2GerritMapping | None:
    """Parse the key-value lines inside a mapping block."""
    lines = [line.strip() for line in block.split("\n")]

    pr_url = ""
    mode = ""
    topic = ""
    change_ids: list[str] = []
    github_hash = ""
    in_change_ids = False

    for line in lines:
        if not line:
            continue

        # Strip any markdown formatting (italic markers etc.)
        clean_line = line.strip("_*")

        if clean_line.startswith("PR:"):
            pr_url = clean_line[3:].strip()
            in_change_ids = False
        elif clean_line.startswith("Mode:"):
            mode = clean_line[5:].strip()
            in_change_ids = False
        elif clean_line.startswith("Topic:"):
            topic = clean_line[6:].strip()
            in_change_ids = False
        elif clean_line.startswith("Change-Id"):
            # "Change-Ids:" or "Change-Id:"
            in_change_ids = True
        elif clean_line.startswith("GitHub-Hash:"):
            github_hash = clean_line[12:].strip()
            in_change_ids = False
        elif clean_line.startswith("Digest:"):
            in_change_ids = False
        elif clean_line.startswith("Note:"):
            in_change_ids = False
        elif in_change_ids and clean_line.startswith("I"):
            cid = clean_line.split()[0]
            if _CHANGE_ID_PATTERN.match(cid) and cid not in change_ids:
                change_ids.append(cid)

    if not all([mode, topic, change_ids]):
        log.debug(
            "Incomplete mapping block: mode=%s, topic=%s, change_ids=%d",
            bool(mode),
            bool(topic),
            len(change_ids),
        )
        return None

    return GitHub2GerritMapping(
        pr_url=pr_url,
        mode=mode,
        topic=topic,
        change_ids=tuple(change_ids),
        github_hash=github_hash,
        raw_comment_body=full_body,
    )
