# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Gerrit data models for dependamerge.

This module defines Pydantic models for Gerrit changes and related data,
paralleling the GitHub PR models used elsewhere in the codebase.

These models provide:
- Type-safe representations of Gerrit API responses
- Factory methods for parsing raw API data
- Comparison result structures for similarity matching
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class GerritChangeStatus(str, Enum):
    """Gerrit change status values."""

    NEW = "NEW"
    MERGED = "MERGED"
    ABANDONED = "ABANDONED"


class GerritFileStatus(str, Enum):
    """Status of a file in a Gerrit change."""

    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"
    RENAMED = "R"
    COPIED = "C"
    REWRITE = "W"


class GerritFileChange(BaseModel):
    """
    Represents a file change in a Gerrit change.

    This parallels the FileChange model used for GitHub PRs.
    """

    filename: str
    status: str = "M"  # Default to modified
    lines_inserted: int = 0
    lines_deleted: int = 0
    size_delta: int = 0
    old_path: str | None = None  # For renames/copies

    @classmethod
    def from_api_response(
        cls, filename: str, file_data: dict[str, Any]
    ) -> GerritFileChange:
        """
        Create a GerritFileChange from Gerrit API file info.

        Args:
            filename: The file path.
            file_data: The file info dict from Gerrit API.

        Returns:
            A GerritFileChange instance.
        """
        return cls(
            filename=filename,
            status=file_data.get("status", "M"),
            lines_inserted=file_data.get("lines_inserted", 0),
            lines_deleted=file_data.get("lines_deleted", 0),
            size_delta=file_data.get("size_delta", 0),
            old_path=file_data.get("old_path"),
        )


class GerritLabelInfo(BaseModel):
    """
    Represents label (vote) information for a Gerrit change.

    Labels like Code-Review, Verified, etc.
    """

    name: str
    approved: bool = False
    rejected: bool = False
    value: int | None = None
    blocking: bool = False

    @classmethod
    def from_api_response(
        cls, name: str, label_data: dict[str, Any]
    ) -> GerritLabelInfo:
        """
        Create a GerritLabelInfo from Gerrit API label info.

        Args:
            name: The label name (e.g., "Code-Review").
            label_data: The label info dict from Gerrit API.

        Returns:
            A GerritLabelInfo instance.
        """
        # Gerrit uses "approved" and "rejected" sub-objects
        approved = "approved" in label_data
        rejected = "rejected" in label_data

        # Get the current vote value if present
        value = None
        if "value" in label_data:
            value = label_data["value"]
        elif approved:
            # If approved, typically means max positive vote
            value = 2
        elif rejected:
            # If rejected, typically means max negative vote
            value = -2

        return cls(
            name=name,
            approved=approved,
            rejected=rejected,
            value=value,
            blocking=label_data.get("blocking", False),
        )


class GerritChangeInfo(BaseModel):
    """
    Represents a Gerrit change (parallels PullRequestInfo for GitHub).

    This model contains all the information needed to compare changes
    and perform operations like review and submit.
    """

    # Core identifiers
    number: int = Field(..., description="Gerrit change number")
    change_id: str = Field(..., description="Gerrit Change-Id (I-prefixed)")
    project: str = Field(..., description="Gerrit project name")

    # Content
    subject: str = Field(..., description="First line of commit message")
    message: str | None = Field(None, description="Full commit message")
    topic: str | None = Field(None, description="Change topic (if set)")

    # Author/owner
    owner: str = Field(..., description="Change owner username")
    owner_email: str | None = Field(None, description="Change owner email")

    # Branch info
    branch: str = Field(..., description="Target branch")
    current_revision: str = Field("", description="Current revision SHA")

    # Status
    status: str = Field(..., description="Change status (NEW, MERGED, ABANDONED)")
    submittable: bool = Field(False, description="Whether change can be submitted")
    mergeable: bool | None = Field(None, description="Whether change is mergeable")
    work_in_progress: bool = Field(False, description="Whether change is WIP")

    # Files
    files_changed: list[GerritFileChange] = Field(
        default_factory=list, description="List of changed files"
    )

    # Labels
    labels: list[GerritLabelInfo] = Field(
        default_factory=list, description="Label/vote information"
    )

    # URLs
    url: str = Field("", description="Web URL for the change")

    # Timestamps
    created: str = Field("", description="Creation timestamp")
    updated: str = Field("", description="Last update timestamp")

    # Submit requirements (Gerrit 3.x+)
    submit_requirements_met: bool = Field(
        True, description="Whether all submit requirements are satisfied"
    )

    # Permissions - for checking what the current user can do
    permitted_labels: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Map of label names to permitted voting values for current user",
    )
    actions: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Available actions the caller can perform on the change",
    )

    @classmethod
    def from_api_response(
        cls,
        data: dict[str, Any],
        host: str = "",
        base_path: str | None = None,
    ) -> GerritChangeInfo:
        """
        Create a GerritChangeInfo from Gerrit REST API response.

        Args:
            data: The change info dict from Gerrit API.
            host: Optional host for constructing URLs.
            base_path: Optional base path for the Gerrit server.

        Returns:
            A GerritChangeInfo instance.
        """
        # Extract basic fields
        number = data.get("_number", 0)
        change_id = data.get("change_id", "")
        project = data.get("project", "")
        subject = data.get("subject", "")
        branch = data.get("branch", "")
        status = data.get("status", "NEW")
        topic = data.get("topic")

        # Extract owner info
        owner_data = data.get("owner", {})
        owner = owner_data.get("username") or owner_data.get("name", "unknown")
        owner_email = owner_data.get("email")

        # Extract current revision
        current_revision = data.get("current_revision", "")

        # Extract commit message from current revision
        message = None
        if current_revision and "revisions" in data:
            revision_data = data["revisions"].get(current_revision, {})
            commit_data = revision_data.get("commit", {})
            message = commit_data.get("message")

        # Extract file changes from current revision
        files_changed: list[GerritFileChange] = []
        if current_revision and "revisions" in data:
            revision_data = data["revisions"].get(current_revision, {})
            files_data = revision_data.get("files", {})
            for filename, file_info in files_data.items():
                # Skip the special /COMMIT_MSG file
                if filename == "/COMMIT_MSG":
                    continue
                files_changed.append(
                    GerritFileChange.from_api_response(filename, file_info)
                )

        # Extract labels
        labels: list[GerritLabelInfo] = []
        labels_data = data.get("labels", {})
        for label_name, label_info in labels_data.items():
            labels.append(GerritLabelInfo.from_api_response(label_name, label_info))

        # Submittable and mergeable
        submittable = data.get("submittable", False)
        mergeable = data.get("mergeable")
        work_in_progress = data.get("work_in_progress", False)

        # Extract permitted labels (what the current user can vote on)
        permitted_labels: dict[str, list[str]] = data.get("permitted_labels", {})

        # Extract available actions
        actions: dict[str, dict[str, Any]] = data.get("actions", {})

        # Check submit requirements (Gerrit 3.x+)
        submit_requirements_met = True
        submit_records = data.get("submit_records", [])
        for record in submit_records:
            if record.get("status") != "OK":
                submit_requirements_met = False
                break

        # Construct URL via the centralised builder to ensure base_path
        # is handled consistently (see GerritUrlBuilder).
        url = ""
        if host:
            from dependamerge.gerrit.urls import GerritUrlBuilder

            builder = GerritUrlBuilder(
                host=host, base_path=base_path, auto_discover=False
            )
            url = builder.change_url(project, number)

        # Timestamps
        created = data.get("created", "")
        updated = data.get("updated", "")

        return cls(
            number=number,
            change_id=change_id,
            project=project,
            subject=subject,
            message=message,
            topic=topic,
            owner=owner,
            owner_email=owner_email,
            branch=branch,
            current_revision=current_revision,
            status=status,
            submittable=submittable,
            mergeable=mergeable,
            work_in_progress=work_in_progress,
            files_changed=files_changed,
            labels=labels,
            url=url,
            created=created,
            updated=updated,
            submit_requirements_met=submit_requirements_met,
            permitted_labels=permitted_labels,
            actions=actions,
        )

    @property
    def is_open(self) -> bool:
        """Check if the change is open (NEW status)."""
        return self.status == GerritChangeStatus.NEW.value

    @property
    def is_merged(self) -> bool:
        """Check if the change has been merged."""
        return self.status == GerritChangeStatus.MERGED.value

    @property
    def is_abandoned(self) -> bool:
        """Check if the change has been abandoned."""
        return self.status == GerritChangeStatus.ABANDONED.value

    @property
    def can_submit(self) -> bool:
        """Check if the change can be submitted."""
        return (
            self.is_open
            and self.submittable
            and self.submit_requirements_met
            and not self.work_in_progress
        )

    @property
    def file_count(self) -> int:
        """Get the number of files changed."""
        return len(self.files_changed)

    @property
    def total_lines_changed(self) -> int:
        """Get the total number of lines changed (inserted + deleted)."""
        return sum(f.lines_inserted + f.lines_deleted for f in self.files_changed)

    def get_label_value(self, label_name: str) -> int | None:
        """
        Get the current vote value for a label.

        Args:
            label_name: The label name (e.g., "Code-Review").

        Returns:
            The vote value, or None if not found.
        """
        for label in self.labels:
            if label.name == label_name:
                return label.value
        return None

    def is_label_approved(self, label_name: str) -> bool:
        """
        Check if a label has been approved.

        Args:
            label_name: The label name to check.

        Returns:
            True if the label is approved.
        """
        for label in self.labels:
            if label.name == label_name:
                return label.approved
        return False

    def can_vote_label(self, label_name: str, value: int) -> bool:
        """
        Check if the current user can vote a specific value on a label.

        Args:
            label_name: The label name (e.g., "Code-Review").
            value: The vote value to check (e.g., 2 for +2).

        Returns:
            True if the user can vote this value on the label.
        """
        if label_name not in self.permitted_labels:
            return False
        permitted_values = self.permitted_labels[label_name]
        # Values are strings like "-2", "-1", "0", "+1", "+2"
        value_str = f"+{value}" if value > 0 else str(value)
        return value_str in permitted_values

    def can_code_review_plus_two(self) -> bool:
        """
        Check if the current user can give +2 Code-Review.

        Returns:
            True if the user can vote +2 on Code-Review.
        """
        return self.can_vote_label("Code-Review", 2)

    def can_submit_action(self) -> bool:
        """
        Check if the current user has the submit action available.

        This checks the actions field returned by Gerrit when
        CURRENT_ACTIONS is requested. Gerrit only exposes the submit
        action once a change is submittable.

        Returns:
            True if the submit action is available.
        """
        return "submit" in self.actions

    def get_permission_warnings(self) -> list[str]:
        """
        Get a list of permission warnings for operations on this change.

        Returns:
            List of warning messages about missing permissions.
        """
        warnings = []

        if not self.can_code_review_plus_two():
            warnings.append(
                "You may not have permission to give +2 Code-Review on this change"
            )

        if self.submittable and not self.can_submit_action():
            warnings.append("You may not have permission to submit this change")

        return warnings

    def has_required_permissions(self) -> bool:
        """
        Check if the user has all required permissions for merge operations.

        This checks +2 Code-Review permissions. Gerrit only exposes the
        submit action once a change is already submittable, so submit
        permission is required only for submittable changes.

        Returns:
            True if the user has all required permissions.
        """
        if not self.can_code_review_plus_two():
            return False

        return not self.submittable or self.can_submit_action()


class GerritComparisonResult(BaseModel):
    """
    Result of comparing two Gerrit changes for similarity.

    This parallels the ComparisonResult model used for GitHub PRs.
    """

    is_similar: bool = Field(
        ..., description="Whether the changes are considered similar"
    )
    confidence_score: float = Field(
        ..., description="Similarity confidence score (0.0 to 1.0)"
    )
    reasons: list[str] = Field(
        default_factory=list, description="Reasons for the similarity assessment"
    )

    @classmethod
    def not_similar(cls, reason: str = "") -> GerritComparisonResult:
        """Create a result indicating changes are not similar."""
        reasons = [reason] if reason else []
        return cls(is_similar=False, confidence_score=0.0, reasons=reasons)

    @classmethod
    def similar(
        cls, score: float, reasons: list[str] | None = None
    ) -> GerritComparisonResult:
        """Create a result indicating changes are similar."""
        return cls(
            is_similar=True,
            confidence_score=score,
            reasons=reasons or [],
        )


class GerritSubmitResult(BaseModel):
    """
    Result of attempting to submit a Gerrit change.

    This is used by the submit manager to track operation outcomes.
    """

    change_number: int = Field(..., description="The change number")
    project: str = Field(..., description="The project name")
    success: bool = Field(..., description="Whether submission succeeded")
    reviewed: bool = Field(False, description="Whether review was applied")
    submitted: bool = Field(False, description="Whether change was submitted")
    error: str | None = Field(None, description="Error message if failed")
    duration_seconds: float = Field(0.0, description="Operation duration")

    @classmethod
    def success_result(
        cls,
        change_number: int,
        project: str,
        reviewed: bool = True,
        submitted: bool = True,
        duration: float = 0.0,
    ) -> GerritSubmitResult:
        """Create a successful submit result."""
        return cls(
            change_number=change_number,
            project=project,
            success=True,
            reviewed=reviewed,
            submitted=submitted,
            error=None,
            duration_seconds=duration,
        )

    @classmethod
    def failure_result(
        cls,
        change_number: int,
        project: str,
        error: str,
        reviewed: bool = False,
        duration: float = 0.0,
    ) -> GerritSubmitResult:
        """Create a failed submit result."""
        return cls(
            change_number=change_number,
            project=project,
            success=False,
            reviewed=reviewed,
            submitted=False,
            error=error,
            duration_seconds=duration,
        )


__all__ = [
    "GerritChangeInfo",
    "GerritChangeStatus",
    "GerritComparisonResult",
    "GerritFileChange",
    "GerritFileStatus",
    "GerritLabelInfo",
    "GerritSubmitResult",
]
