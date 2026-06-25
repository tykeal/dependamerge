# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

import re
from difflib import SequenceMatcher

from .bot_identity import normalize_bot_login
from .models import ComparisonResult, FileChange, PullRequestInfo


class PRComparator:
    """Compare pull requests to determine if they contain similar changes."""

    def __init__(self, similarity_threshold: float = 0.8):
        """Initialize comparator with similarity threshold."""
        self.similarity_threshold = similarity_threshold

    def compare_pull_requests(
        self,
        source_pr: PullRequestInfo,
        target_pr: PullRequestInfo,
        only_automation: bool = True,
    ) -> ComparisonResult:
        """Compare two pull requests and determine similarity."""
        reasons = []
        scores = []

        # Check automation requirements based on mode
        if only_automation:
            # Both PRs must be from automation tools
            if not self._is_automation_pr(source_pr) or not self._is_automation_pr(
                target_pr
            ):
                return ComparisonResult(
                    is_similar=False,
                    confidence_score=0.0,
                    reasons=["One or both PRs are not from automation tools"],
                )
        else:
            # For non-automation mode, we expect the service already filtered by same author
            # so we don't need additional automation checks here
            pass

        # Compare titles
        title_score = self._compare_titles(source_pr.title, target_pr.title)
        scores.append(title_score)
        if title_score > 0.7:
            reasons.append(f"Similar titles (score: {title_score:.2f})")

        # Compare PR bodies for additional context
        body_score = self._compare_bodies(source_pr.body, target_pr.body)
        scores.append(body_score)
        if body_score > 0.6:
            reasons.append(f"Similar PR descriptions (score: {body_score:.2f})")

        # Compare file changes
        files_score = self._compare_file_changes(
            source_pr.files_changed, target_pr.files_changed
        )
        scores.append(files_score)
        if files_score > 0.6:
            reasons.append(f"Similar file changes (score: {files_score:.2f})")

        # Compare authors (normalize bot names to handle API differences)
        source_author = self._normalize_author(source_pr.author)
        target_author = self._normalize_author(target_pr.author)
        if source_author == target_author:
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
        # For dependency updates, check if they're updating the same package
        package1 = self._extract_package_name(title1)
        package2 = self._extract_package_name(title2)

        # If both are dependency updates, they must update the same package
        if package1 and package2:
            if package1 == package2:
                return 1.0  # Same package update - very similar
            else:
                return 0.0  # Different packages - not similar

        # Fall back to original logic for non-dependency updates
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

    def _compare_file_changes(self, files1: list[FileChange], files2: list[FileChange]) -> float:
        """Compare file changes between PRs."""
        if not files1 or not files2:
            return 0.0

        # Extract filenames and normalize paths
        filenames1 = {self._normalize_filename(f.filename) for f in files1}
        filenames2 = {self._normalize_filename(f.filename) for f in files2}

        # Calculate Jaccard similarity
        intersection = len(filenames1.intersection(filenames2))
        union = len(filenames1.union(filenames2))

        # For GitHub Actions workflows, consider them similar if both modify workflow files
        # This handles cases where different repos have different workflow names
        workflows1 = {f for f in filenames1 if f.startswith(".github/workflows/")}
        workflows2 = {f for f in filenames2 if f.startswith(".github/workflows/")}

        if workflows1 and workflows2:
            # Both PRs modify GitHub Actions workflows - consider this a partial match
            return max(intersection / union if union > 0 else 0.0, 0.5)

        return intersection / union if union > 0 else 0.0

    def _normalize_filename(self, filename: str) -> str:
        """Normalize filename for comparison."""
        # Remove version-specific parts from filenames
        filename = re.sub(r"v?\d+\.\d+\.\d+(?:\.\d+)?", "", filename)
        return filename.lower()

    def _extract_package_name(self, title: str) -> str:
        """Extract package name from dependency update titles.

        Returns empty string if not a recognized dependency update pattern.
        """
        title_lower = title.lower()

        # Common dependency update patterns
        patterns = [
            # "Bump package from X to Y" or "Chore: Bump package from X to Y"
            r"(?:chore:\s*)?bump\s+([^\s]+)\s+from\s+",
            # "Update package from X to Y"
            r"(?:chore:\s*)?update\s+([^\s]+)\s+from\s+",
            # "Upgrade package from X to Y"
            r"(?:chore:\s*)?upgrade\s+([^\s]+)\s+from\s+",
        ]

        for pattern in patterns:
            match = re.search(pattern, title_lower)
            if match:
                package = match.group(1)
                # Clean up the package name
                package = package.strip()
                # Remove common prefixes that might vary
                package = re.sub(r'^["\']|["\']$', "", package)  # Remove quotes
                return package

        return ""

    def _compare_bodies(self, body1: str | None, body2: str | None) -> float:
        """Compare PR bodies for similarity in automation patterns."""
        if not body1 or not body2:
            return 0.0

        # Normalize both bodies
        normalized1 = self._normalize_body(body1)
        normalized2 = self._normalize_body(body2)

        # For very short bodies, use exact matching
        if len(normalized1) < 50 or len(normalized2) < 50:
            return 1.0 if normalized1 == normalized2 else 0.0

        # Check for specific automation patterns
        automation_score = self._compare_automation_patterns(body1, body2)
        if automation_score > 0:
            return automation_score

        # Fall back to sequence matching for general similarity
        return SequenceMatcher(None, normalized1, normalized2).ratio()

    def _normalize_body(self, body: str | None) -> str:
        """Normalize PR body by removing version-specific and variable content."""
        if not body:
            return ""

        # Convert to lowercase
        body = body.lower()

        # Remove URLs (they often contain version-specific paths)
        body = re.sub(r"https?://[^\s]+", "", body)

        # Remove version numbers
        body = re.sub(r"v?\d+\.\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9.-]+)?", "VERSION", body)

        # Remove commit hashes
        body = re.sub(r"\b[a-f0-9]{7,40}\b", "COMMIT", body)

        # Remove dates
        body = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", body)

        # Remove specific numbers that might be build/PR numbers
        body = re.sub(r"#\d+", "#NUMBER", body)

        # Normalize whitespace
        body = re.sub(r"\s+", " ", body).strip()

        return body

    def _compare_automation_patterns(
        self, body1: str | None, body2: str | None
    ) -> float:
        """Compare bodies for specific automation tool patterns."""

        if not body1 or not body2:
            return 0.0

        # Dependabot patterns
        if self._is_dependabot_body(body1) and self._is_dependabot_body(body2):
            # Extract package information from both bodies
            package1 = self._extract_dependabot_package(body1)
            package2 = self._extract_dependabot_package(body2)

            if package1 and package2 and package1 == package2:
                return 0.95  # Very high confidence for same package updates
            elif package1 and package2:
                return 0.1  # Different packages, low similarity

        # Pre-commit patterns
        if self._is_precommit_body(body1) and self._is_precommit_body(body2):
            return 0.9  # Pre-commit updates are usually similar

        # GitHub Actions patterns
        if self._is_github_actions_body(body1) and self._is_github_actions_body(body2):
            action1 = self._extract_github_action(body1)
            action2 = self._extract_github_action(body2)

            if action1 and action2 and action1 == action2:
                return 0.9
            elif action1 and action2:
                return 0.2

        return 0.0

    def _is_dependabot_body(self, body: str | None) -> bool:
        """Check if body contains Dependabot-specific patterns."""
        if not body:
            return False

        dependabot_patterns = [
            "dependabot",
            "bumps",
            "from .* to",
            "release notes",
            "changelog",
            "commits",
            "dependency-name:",
        ]

        body_lower = body.lower()
        return sum(1 for pattern in dependabot_patterns if pattern in body_lower) >= 2

    def _extract_dependabot_package(self, body: str | None) -> str:
        """Extract package name from Dependabot PR body."""
        if not body:
            return ""

        # Look for "dependency-name: package" pattern in YAML frontmatter
        yaml_match = re.search(r"dependency-name:\s*([^\s\n]+)", body, re.IGNORECASE)
        if yaml_match:
            return yaml_match.group(1).strip()

        # Look for "Bumps [package]" pattern
        bump_match = re.search(r"bumps\s+\[([^\]]+)\]", body, re.IGNORECASE)
        if bump_match:
            return bump_match.group(1).strip()

        return ""

    def _normalize_author(self, author: str | None) -> str:
        """Normalize author name to handle differences between REST and GraphQL APIs.

        GitHub's REST API returns 'dependabot[bot]' while GraphQL returns
        'dependabot'.  Delegates to the shared
        :func:`bot_identity.normalize_bot_login` so author comparison uses
        the same canonical form as the rest of the codebase.
        """
        return normalize_bot_login(author)

    def _is_precommit_body(self, body: str | None) -> bool:
        """Check if body contains pre-commit specific patterns."""
        if not body:
            return False

        precommit_patterns = [
            "pre-commit",
            "autoupdate",
            "hooks",
            ".pre-commit-config.yaml",
        ]

        body_lower = body.lower()
        return any(pattern in body_lower for pattern in precommit_patterns)

    def _is_github_actions_body(self, body: str | None) -> bool:
        """Check if body contains GitHub Actions specific patterns."""
        if not body:
            return False

        actions_patterns = [
            "github actions",
            "workflow",
            "action",
            ".github/workflows",
            "uses:",
        ]

        body_lower = body.lower()
        return any(pattern in body_lower for pattern in actions_patterns)

    def _extract_github_action(self, body: str | None) -> str:
        """Extract action name from GitHub Actions PR body."""
        if not body:
            return ""

        # Look for "uses: action/name@version" pattern
        uses_match = re.search(r"uses:\s*([^@\s]+)", body, re.IGNORECASE)
        if uses_match:
            return uses_match.group(1).strip()

        return ""
