# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from typing import List, Optional

from pydantic import BaseModel


class FileChange(BaseModel):
    """Represents a file change in a pull request."""

    filename: str
    additions: int
    deletions: int
    changes: int
    status: str  # added, modified, removed, renamed


class PullRequestInfo(BaseModel):
    """Represents pull request information."""

    number: int
    title: str
    body: Optional[str]
    author: str
    head_sha: str
    base_branch: str
    head_branch: str
    state: str
    mergeable: Optional[bool]
    mergeable_state: Optional[str]  # Additional state information from GitHub
    behind_by: Optional[int]  # Number of commits behind the base branch
    files_changed: List[FileChange]
    repository_full_name: str
    html_url: str


class ComparisonResult(BaseModel):
    """Result of comparing two pull requests."""

    is_similar: bool
    confidence_score: float
    reasons: List[str]
