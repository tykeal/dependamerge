# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024 The Linux Foundation

import re
from difflib import SequenceMatcher
from typing import List

from .models import ComparisonResult, PullRequestInfo


class PRComparator:
    """Compare pull requests to determine if they contain similar changes."""

    def __init__(self, similarity_threshold: float = 0.8):
        """Initialize comparator with similarity threshold."""
        self.similarity_threshold = similarity_threshold

    def compare_pull_requests(
        self, source_pr: PullRequestInfo, target_pr: PullRequestInfo
    ) -> ComparisonResult:
        """Compare two pull requests and determine similarity."""
        reasons = []
        scores = []

        # Check if both are from automation tools
        if not self._is_automation_pr(source_pr) or not self._is_automation_pr(
            target_pr
        ):
            return ComparisonResult(
                is_similar=False,
                confidence_score=0.0,
                reasons=["One or both PRs are not from automation tools"],
            )

        # Compare titles
        title_score = self._compare_titles(source_pr.title, target_pr.title)
        scores.append(title_score)
        if title_score > 0.7:
            reasons.append(f"Similar titles (score: {title_score:.2f})")

        # Compare file changes
        files_score = self._compare_file_changes(
            source_pr.files_changed, target_pr.files_changed
        )
        scores.append(files_score)
        if files_score > 0.6:
            reasons.append(f"Similar file changes (score: {files_score:.2f})")

        # Compare authors
        if source_pr.author == target_pr.author:
            scores.append(1.0)
            reasons.append("Same automation author")
        else:
            scores.append(0.0)

        # Calculate overall confidence score
        confidence_score = sum(scores) / len(scores) if scores else 0.0
        is_similar = confidence_score >= self.similarity_threshold

        return ComparisonResult(
            is_similar=is_similar, confidence_score=confidence_score, reasons=reasons
        )

    def _is_automation_pr(self, pr: PullRequestInfo) -> bool:
        """Check if PR is from an automation tool."""
        automation_indicators = [
            "dependabot",
            "pre-commit",
            "renovate",
            "github-actions",
            "auto-update",
            "automated",
            "bot",
        ]

        pr_text = f"{pr.title} {pr.body or ''} {pr.author}".lower()
        return any(indicator in pr_text for indicator in automation_indicators)

    def _compare_titles(self, title1: str, title2: str) -> float:
        """Compare PR titles for similarity."""
        # Normalize titles by removing version numbers and specific details
        normalized1 = self._normalize_title(title1)
        normalized2 = self._normalize_title(title2)

        return SequenceMatcher(None, normalized1, normalized2).ratio()

    def _normalize_title(self, title: str) -> str:
        """Normalize title by removing version-specific information."""
        # Remove version numbers like 1.2.3, v1.2.3, etc.
        title = re.sub(r"v?\d+\.\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9]+)?", "", title)
        # Remove commit hashes
        title = re.sub(r"\b[a-f0-9]{7,40}\b", "", title)
        # Remove dates
        title = re.sub(r"\d{4}-\d{2}-\d{2}", "", title)
        # Normalize whitespace
        title = " ".join(title.split())
        return title.lower()

    def _compare_file_changes(self, files1: List, files2: List) -> float:
        """Compare file changes between PRs."""
        if not files1 or not files2:
            return 0.0

        # Extract filenames and normalize paths
        filenames1 = {self._normalize_filename(f.filename) for f in files1}
        filenames2 = {self._normalize_filename(f.filename) for f in files2}

        # Calculate Jaccard similarity
        intersection = len(filenames1.intersection(filenames2))
        union = len(filenames1.union(filenames2))

        return intersection / union if union > 0 else 0.0

    def _normalize_filename(self, filename: str) -> str:
        """Normalize filename for comparison."""
        # Remove version-specific parts from filenames
        filename = re.sub(r"v?\d+\.\d+\.\d+(?:\.\d+)?", "", filename)
        return filename.lower()
