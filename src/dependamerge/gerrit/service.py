# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Gerrit service layer for dependamerge.

This module provides a high-level service class for querying and operating
on Gerrit changes. It abstracts the REST API interactions and provides
methods for:

- Fetching change details
- Enumerating open changes across a server
- Finding similar changes for bulk operations
- Pagination handling for large result sets
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from dependamerge.gerrit.client import (
    GerritNotFoundError,
    GerritRestError,
    build_client,
)
from dependamerge.gerrit.models import (
    GerritChangeInfo,
    GerritComparisonResult,
)
from dependamerge.gerrit.urls import GerritUrlBuilder, create_url_builder

if TYPE_CHECKING:
    from dependamerge.progress_tracker import ProgressTracker


log = logging.getLogger("dependamerge.gerrit.service")


# Default query options for fetching change details
DEFAULT_CHANGE_OPTIONS: list[str] = [
    "CURRENT_REVISION",
    "CURRENT_FILES",
    "CURRENT_COMMIT",
    "DETAILED_LABELS",
    "DETAILED_ACCOUNTS",
    "SUBMITTABLE",
    "CURRENT_ACTIONS",  # Include available actions for permission checking
]

# Default query options for listing changes
DEFAULT_LIST_OPTIONS: list[str] = [
    "CURRENT_REVISION",
    "CURRENT_FILES",
    "CURRENT_COMMIT",
    "LABELS",
    "DETAILED_ACCOUNTS",
]


class GerritServiceError(Exception):
    """Raised for service-level errors."""


class GerritService:
    """
    High-level service for Gerrit operations.

    This class provides methods for querying changes, finding similar
    changes, and managing change operations across a Gerrit server.
    """

    # Default similarity threshold for fallback comparison
    DEFAULT_SIMILARITY_THRESHOLD: float = 0.8

    def __init__(
        self,
        host: str,
        base_path: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 15.0,
        max_attempts: int = 5,
        progress_tracker: ProgressTracker | None = None,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        """
        Initialize the Gerrit service.

        Args:
            host: Gerrit server hostname.
            base_path: Optional base path (e.g., "infra").
            username: Optional HTTP username for authentication.
            password: Optional HTTP password for authentication.
            timeout: Request timeout in seconds.
            max_attempts: Maximum retry attempts for transient failures.
            progress_tracker: Optional progress tracker for UI feedback.
            similarity_threshold: Minimum confidence score (0.0 to 1.0) for
                changes to be considered similar in the fallback comparison.
                This is used by _basic_compare when no external comparator
                is provided. Should match the threshold used by
                GerritChangeComparator for consistent behavior.
        """
        self.host = host
        self.base_path = base_path
        self._progress_tracker = progress_tracker
        self._similarity_threshold = similarity_threshold

        # Build URL helper
        self._url_builder = create_url_builder(
            host, base_path=base_path, auto_discover=False
        )

        # Build REST client
        self._client = build_client(
            host,
            base_path=base_path,
            timeout=timeout,
            max_attempts=max_attempts,
            username=username,
            password=password,
        )

        log.debug(
            "GerritService initialized: host=%s, base_path=%s, auth=%s",
            host,
            base_path,
            "yes" if self._client.is_authenticated else "no",
        )

    @property
    def is_authenticated(self) -> bool:
        """Check if the service has authentication credentials."""
        return self._client.is_authenticated

    @property
    def url_builder(self) -> GerritUrlBuilder:
        """Get the URL builder for constructing URLs."""
        return self._url_builder

    def get_mergeable_status(
        self,
        change_number: int,
    ) -> dict[str, Any]:
        """
        Fetch the mergeable status for a change.

        This makes an explicit API call to compute merge status,
        which is not included in the standard change info query.

        Args:
            change_number: The Gerrit change number.

        Returns:
            A dict with mergeable info including:
            - mergeable: bool - whether the change can be merged
            - submit_type: str - the submit type (e.g., MERGE_IF_NECESSARY)
            - commit_merged: bool - whether commit is already merged
            - content_merged: bool - whether content is already merged

        Raises:
            GerritServiceError: If the status cannot be fetched.
        """
        endpoint = f"/changes/{change_number}/revisions/current/mergeable"
        log.debug("Fetching mergeable status: %s", endpoint)

        try:
            result: dict[str, Any] = self._client.get(endpoint)
            return result
        except GerritNotFoundError:
            # Change doesn't exist or has no current revision
            return {"mergeable": None}
        except GerritRestError as exc:
            log.warning(
                "Failed to fetch mergeable status for %d: %s", change_number, exc
            )
            return {"mergeable": None}

    def get_change_info(
        self,
        change_number: int,
        options: list[str] | None = None,
        check_mergeable: bool = True,
    ) -> GerritChangeInfo:
        """
        Fetch detailed information about a specific change.

        Args:
            change_number: The Gerrit change number.
            options: Optional list of query options. Defaults to
                    DEFAULT_CHANGE_OPTIONS.
            check_mergeable: If True, make an additional API call to
                           fetch the actual mergeable status.

        Returns:
            A GerritChangeInfo instance with full change details.

        Raises:
            GerritServiceError: If the change cannot be fetched.
            GerritNotFoundError: If the change does not exist.
        """
        if options is None:
            options = DEFAULT_CHANGE_OPTIONS

        # Build query URL
        endpoint = f"/changes/{change_number}"
        if options:
            params = "&".join(f"o={opt}" for opt in options)
            endpoint += "?" + params

        log.debug("Fetching change info: %s", endpoint)

        try:
            data = self._client.get(endpoint)
            change_info = GerritChangeInfo.from_api_response(
                data, host=self.host, base_path=self.base_path
            )

            # Fetch actual mergeable status if requested and change is open
            if check_mergeable and change_info.status == "NEW":
                mergeable_data = self.get_mergeable_status(change_number)
                if mergeable_data.get("mergeable") is not None:
                    # Update the change info with actual mergeable status
                    change_info = change_info.model_copy(
                        update={"mergeable": mergeable_data.get("mergeable")}
                    )

            return change_info
        except GerritNotFoundError:
            raise
        except GerritRestError as exc:
            msg = f"Failed to fetch change {change_number}: {exc}"
            log.error(msg)
            raise GerritServiceError(msg) from exc

    def rebase_change(
        self,
        change_number: int,
        base: str | None = None,
    ) -> dict[str, Any]:
        """
        Attempt to rebase a change onto the target branch.

        This calls the Gerrit rebase endpoint which will:
        - Succeed if the change can be cleanly rebased
        - Return HTTP 409 with conflict details if there are merge conflicts

        Args:
            change_number: The Gerrit change number.
            base: Optional base revision to rebase onto. If None, rebases
                 onto the target branch HEAD.

        Returns:
            A dict with rebase result:
            - success: bool - whether rebase succeeded
            - change_info: dict | None - updated change info if successful
            - conflict: bool - whether there was a merge conflict
            - conflicting_files: list[str] - list of files with conflicts
            - error: str | None - error message if failed

        Note:
            Unlike GitHub's update_branch, Gerrit's rebase creates a new
            patchset with the rebased content if successful.
        """
        endpoint = f"/changes/{change_number}/rebase"
        log.debug("Attempting rebase: %s", endpoint)

        data: dict[str, Any] = {}
        if base:
            data["base"] = base

        try:
            result = self._client.post(endpoint, data=data if data else None)
            log.info("Successfully rebased change %d", change_number)
            return {
                "success": True,
                "change_info": result,
                "conflict": False,
                "conflicting_files": [],
                "error": None,
            }
        except GerritRestError as exc:
            # HTTP 409 indicates a merge conflict
            if exc.status_code == 409:
                # Parse conflict details from response body
                conflicting_files = self._parse_conflict_files(exc.response_body or "")
                log.warning(
                    "Rebase failed for change %d: merge conflict in %s",
                    change_number,
                    conflicting_files,
                )
                return {
                    "success": False,
                    "change_info": None,
                    "conflict": True,
                    "conflicting_files": conflicting_files,
                    "error": exc.response_body or "Merge conflict during rebase",
                }
            # Other errors
            log.error("Rebase failed for change %d: %s", change_number, exc)
            return {
                "success": False,
                "change_info": None,
                "conflict": False,
                "conflicting_files": [],
                "error": str(exc),
            }

    def _parse_conflict_files(self, response_body: str) -> list[str]:
        """
        Parse conflicting file names from Gerrit's 409 response.

        The response format is typically:
        "The change could not be rebased due to a conflict during merge.

        merge conflict(s):
        path/to/file1.txt
        path/to/file2.txt"

        Returns:
            List of conflicting file paths. May be empty if parsing fails
            or the response format is unexpected.
        """
        files: list[str] = []
        if not response_body:
            # Nothing to parse; log at debug level to aid diagnostics without being noisy.
            log.debug(
                "Gerrit conflict response body is empty when parsing conflict files."
            )
            return files

        # Look for the "merge conflict(s):" marker
        lines = response_body.strip().splitlines()
        in_conflict_section = False
        marker_found = False

        for line in lines:
            line = line.strip()
            if not line:
                # Skip empty lines; if we're already in the conflict section,
                # treat a blank line as the end of that section.
                if in_conflict_section:
                    break
                continue
            if "merge conflict" in line.lower():
                in_conflict_section = True
                marker_found = True
                continue
            if in_conflict_section:
                # Each subsequent non-empty line is treated as a conflicting file.
                files.append(line)

        if not marker_found:
            # The response did not contain the expected marker; format may have changed.
            log.warning(
                "Failed to find 'merge conflict' marker in Gerrit response when "
                "parsing conflict files. Raw body: %r",
                response_body,
            )
        elif not files:
            # Marker was present but no files were parsed – response format may differ.
            log.warning(
                "No conflicting files parsed from Gerrit response after the "
                "'merge conflict' marker. Raw body: %r",
                response_body,
            )

        return files

    def get_open_changes(
        self,
        project: str | None = None,
        branch: str | None = None,
        owner: str | None = None,
        limit: int = 500,
        offset: int = 0,
        options: list[str] | None = None,
    ) -> list[GerritChangeInfo]:
        """
        Get open changes, optionally filtered by project/branch/owner.

        Args:
            project: Optional project name to filter by.
            branch: Optional branch name to filter by.
            owner: Optional owner username to filter by.
            limit: Maximum number of changes to return.
            offset: Starting offset for pagination.
            options: Optional list of query options.

        Returns:
            List of GerritChangeInfo for matching open changes.
        """
        if options is None:
            options = DEFAULT_LIST_OPTIONS

        # Build query string
        query_parts = ["status:open"]
        if project:
            query_parts.append(f"project:{project}")
        if branch:
            query_parts.append(f"branch:{branch}")
        if owner:
            query_parts.append(f"owner:{owner}")

        query = " ".join(query_parts)
        return self._query_changes(query, limit, offset, options)

    def get_all_open_changes(
        self,
        limit: int = 1000,
        options: list[str] | None = None,
    ) -> list[GerritChangeInfo]:
        """
        Get all open changes across the entire Gerrit server.

        This method handles pagination automatically to fetch up to
        the specified limit of changes.

        Args:
            limit: Maximum number of changes to return.
            options: Optional list of query options.

        Returns:
            List of GerritChangeInfo for all open changes.
        """
        return self.get_open_changes(limit=limit, options=options)

    def get_changes_by_topic(
        self,
        topic: str,
        include_merged: bool = False,
        limit: int = 100,
        options: list[str] | None = None,
    ) -> list[GerritChangeInfo]:
        """
        Get changes with a specific topic.

        Args:
            topic: The topic name to search for.
            include_merged: Whether to include merged changes.
            limit: Maximum number of changes to return.
            options: Optional list of query options.

        Returns:
            List of GerritChangeInfo for matching changes.
        """
        if options is None:
            options = DEFAULT_LIST_OPTIONS

        if include_merged:
            query = f"topic:{topic} (status:open OR status:merged)"
        else:
            query = f"topic:{topic} status:open"

        return self._query_changes(query, limit, 0, options)

    def get_projects(self, limit: int = 500) -> list[str]:
        """
        Get a list of project names from the Gerrit server.

        Args:
            limit: Maximum number of projects to return.

        Returns:
            List of project names.
        """
        log.debug("Fetching project list (limit=%d)", limit)

        try:
            endpoint = f"/projects/?n={limit}"
            data = self._client.get(endpoint)

            # Gerrit returns a dict with project names as keys
            if isinstance(data, dict):
                return sorted(data.keys())
            return []

        except GerritRestError as exc:
            log.warning("Failed to fetch projects: %s", exc)
            return []

    def find_similar_changes(
        self,
        source_change: GerritChangeInfo,
        comparator: Any,
        only_automation: bool = True,
        limit: int = 500,
    ) -> list[tuple[GerritChangeInfo, GerritComparisonResult]]:
        """
        Find changes similar to the source change.

        This method fetches all open changes and uses the provided
        comparator to identify similar changes.

        Args:
            source_change: The change to find similar changes for.
            comparator: A comparator object with a compare_gerrit_changes()
                       method (or compare_pull_requests for compatibility).
            only_automation: Whether to only match automation changes.
            limit: Maximum number of changes to scan.

        Returns:
            List of (change_info, comparison_result) tuples for similar
            changes, sorted by confidence score descending.
        """
        log.info(
            "Finding similar changes for %s #%d",
            source_change.project,
            source_change.number,
        )

        # Fetch all open changes
        all_changes = self.get_all_open_changes(limit=limit)

        log.debug("Scanning %d open changes for similarity", len(all_changes))

        similar_changes: list[tuple[GerritChangeInfo, GerritComparisonResult]] = []

        for change in all_changes:
            # Skip the source change itself
            if change.number == source_change.number:
                continue

            if not only_automation and self._owners_differ(source_change, change):
                log.debug(
                    "Skipping change %d because owner %r does not match source owner %r",
                    change.number,
                    change.owner,
                    source_change.owner,
                )
                continue

            # Compare using the provided comparator
            try:
                if hasattr(comparator, "compare_gerrit_changes"):
                    result = comparator.compare_gerrit_changes(
                        source_change, change, only_automation=only_automation
                    )
                else:
                    # Fall back to generic comparison if available
                    result = self._basic_compare(source_change, change, only_automation)
            except Exception as exc:
                log.debug("Error comparing change %d: %s", change.number, exc)
                continue

            if result.is_similar:
                similar_changes.append((change, result))
                log.debug(
                    "Found similar change: %s #%d (score=%.2f)",
                    change.project,
                    change.number,
                    result.confidence_score,
                )

        # Sort by confidence score descending
        similar_changes.sort(key=lambda x: x[1].confidence_score, reverse=True)

        log.info("Found %d similar changes", len(similar_changes))
        return similar_changes

    def _query_changes(
        self,
        query: str,
        limit: int,
        offset: int,
        options: list[str],
    ) -> list[GerritChangeInfo]:
        """Execute a change query with pagination."""
        all_changes: list[GerritChangeInfo] = []
        page_size = min(limit, 100)
        current_offset = offset

        while len(all_changes) < limit:
            remaining = limit - len(all_changes)
            current_limit = min(page_size, remaining)

            # Build query URL
            params = [
                f"q={query}",
                f"n={current_limit}",
                f"S={current_offset}",
            ]
            for opt in options:
                params.append(f"o={opt}")

            endpoint = "/changes/?" + "&".join(params)
            log.debug("Querying changes: %s", endpoint)

            try:
                data = self._client.get(endpoint)
            except GerritRestError as exc:
                log.warning(
                    "Failed to query changes (offset=%d): %s",
                    current_offset,
                    exc,
                )
                break

            if not data or not isinstance(data, list):
                break

            # Parse each change
            page_changes = []
            for item in data:
                try:
                    change = GerritChangeInfo.from_api_response(
                        item, host=self.host, base_path=self.base_path
                    )
                    page_changes.append(change)
                except Exception as exc:
                    log.debug("Skipping malformed change: %s", exc)
                    continue

            all_changes.extend(page_changes)

            # Check if we've reached the end
            if len(page_changes) < current_limit:
                break

            current_offset += len(page_changes)

        return all_changes[:limit]

    def _basic_compare(
        self,
        source: GerritChangeInfo,
        target: GerritChangeInfo,
        only_automation: bool,
    ) -> GerritComparisonResult:
        """
        Perform basic comparison between two changes.

        This is a fallback when no external comparator is provided.
        Uses the similarity_threshold configured at initialization
        (default: 0.8) to determine if changes are similar.
        """
        reasons: list[str] = []
        scores: list[float] = []

        # Check automation if required
        if only_automation:
            if not self._is_automation_change(source) or not self._is_automation_change(
                target
            ):
                return GerritComparisonResult.not_similar(
                    "One or both changes are not from automation"
                )
        elif self._owners_differ(source, target):
            return GerritComparisonResult.not_similar(
                "Change owner does not match source owner"
            )

        # Compare owners
        if self._normalize_owner(source.owner) == self._normalize_owner(target.owner):
            scores.append(1.0)
            reasons.append("Same author")
        else:
            scores.append(0.0)

        # Compare subjects (titles)
        subject_score = self._compare_subjects(source.subject, target.subject)
        scores.append(subject_score)
        if subject_score > 0.7:
            reasons.append(f"Similar subjects (score: {subject_score:.2f})")

        # Compare files
        files_score = self._compare_files(source, target)
        scores.append(files_score)
        if files_score > 0.5:
            reasons.append(f"Similar files (score: {files_score:.2f})")

        # Calculate overall score
        confidence = sum(scores) / len(scores) if scores else 0.0
        is_similar = confidence >= self._similarity_threshold

        if is_similar:
            return GerritComparisonResult.similar(confidence, reasons)
        return GerritComparisonResult.not_similar()

    def _is_automation_change(self, change: GerritChangeInfo) -> bool:
        """Check if a change is from automation."""
        automation_indicators = [
            "dependabot",
            "pre-commit",
            "renovate",
            "github-actions",
            "auto-update",
            "automated",
            "bot",
        ]

        text = f"{change.subject} {change.message or ''} {change.owner}".lower()
        return any(indicator in text for indicator in automation_indicators)

    def _owners_differ(
        self,
        source: GerritChangeInfo,
        target: GerritChangeInfo,
    ) -> bool:
        """Return True when normalized Gerrit change owners differ."""
        return self._normalize_owner(source.owner) != self._normalize_owner(
            target.owner
        )

    def _normalize_owner(self, owner: str) -> str:
        """Normalize owner name using the Gerrit comparator rules."""
        if not owner:
            return ""

        normalized = owner.lower().strip()

        if normalized.endswith("[bot]"):
            normalized = normalized[:-5]

        for suffix in ("-bot", "_bot", ".bot"):
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break

        return normalized

    def _compare_subjects(self, subject1: str, subject2: str) -> float:
        """Compare two change subjects for similarity."""
        # Normalize subjects
        s1 = subject1.lower().strip()
        s2 = subject2.lower().strip()

        if s1 == s2:
            return 1.0

        # Check for common patterns
        patterns = [
            "bump",
            "update",
            "upgrade",
            "chore:",
            "build(deps):",
        ]

        s1_pattern = None
        s2_pattern = None

        for pattern in patterns:
            if pattern in s1:
                s1_pattern = pattern
            if pattern in s2:
                s2_pattern = pattern

        if s1_pattern and s2_pattern and s1_pattern == s2_pattern:
            return 0.8

        return 0.3

    def _compare_files(
        self,
        source: GerritChangeInfo,
        target: GerritChangeInfo,
    ) -> float:
        """Compare file changes between two changes."""
        if not source.files_changed or not target.files_changed:
            return 0.0

        source_files = {f.filename for f in source.files_changed}
        target_files = {f.filename for f in target.files_changed}

        intersection = len(source_files & target_files)
        union = len(source_files | target_files)

        if union == 0:
            return 0.0

        return intersection / union


def create_gerrit_service(
    host: str,
    base_path: str | None = None,
    username: str | None = None,
    password: str | None = None,
    progress_tracker: ProgressTracker | None = None,
) -> GerritService:
    """
    Factory function to create a GerritService instance.

    Args:
        host: Gerrit server hostname.
        base_path: Optional base path.
        username: Optional HTTP username.
        password: Optional HTTP password.
        progress_tracker: Optional progress tracker.

    Returns:
        Configured GerritService instance.
    """
    return GerritService(
        host=host,
        base_path=base_path,
        username=username,
        password=password,
        progress_tracker=progress_tracker,
    )


__all__ = [
    "DEFAULT_CHANGE_OPTIONS",
    "DEFAULT_LIST_OPTIONS",
    "GerritService",
    "GerritServiceError",
    "create_gerrit_service",
]
