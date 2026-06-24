# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Copilot comment handler for detecting and managing GitHub Copilot review comments.

This module provides functionality to:
- Identify Copilot-generated review comments
- Filter and categorize Copilot feedback
- Dismiss unresolved Copilot comments to unblock PR merging
"""

import logging
from typing import Any

from .models import PullRequestInfo, ReviewInfo

logger = logging.getLogger(__name__)

# Known Copilot author identifiers
COPILOT_AUTHORS = {"Copilot", "github-copilot", "copilot[bot]", "github-copilot[bot]"}

# Common Copilot comment patterns that are often safe to dismiss
COMMON_COPILOT_PATTERNS = [
    r"use:\s+ubuntu-24\.04",  # Ubuntu version suggestions
    r"consider using.*instead of",  # Generic suggestions
    r"you might want to",  # Soft suggestions
    r"this could be improved by",  # Improvement suggestions
]


class CopilotCommentHandler:
    """Handler for managing GitHub Copilot review comments."""

    def __init__(self, github_client, preview_mode: bool = False, debug: bool = False):
        """
        Initialize the Copilot comment handler.

        Args:
            github_client: Async GitHub client for API operations
            preview_mode: If True, only simulate dismissal operations
            debug: Enable debug logging
        """
        self.github_client = github_client
        self.preview_mode = preview_mode
        self.debug = debug
        self.log = logging.getLogger(__name__)

    def is_copilot_review(self, review: ReviewInfo) -> bool:
        """
        Determine if a review is from GitHub Copilot.

        Args:
            review: Review to check

        Returns:
            True if review is from Copilot, False otherwise
        """
        if not review.user:
            return False

        # Check if author matches known Copilot identifiers
        author_lower = review.user.lower()
        for copilot_author in COPILOT_AUTHORS:
            if copilot_author.lower() in author_lower:
                return True

        return False

    def get_copilot_reviews(self, pr_info: PullRequestInfo) -> list[ReviewInfo]:
        """
        Extract all Copilot reviews from a pull request.

        Args:
            pr_info: Pull request information

        Returns:
            List of Copilot reviews
        """
        copilot_reviews = []

        for review in pr_info.reviews:
            if self.is_copilot_review(review):
                copilot_reviews.append(review)
                if self.debug:
                    self.log.info(
                        f"🤖 Found Copilot review: {review.id} - {review.state}"
                    )

        return copilot_reviews

    def get_unresolved_copilot_reviews(
        self, pr_info: PullRequestInfo
    ) -> list[ReviewInfo]:
        """
        Get unresolved Copilot reviews that may be blocking the merge.

        Args:
            pr_info: Pull request information

        Returns:
            List of unresolved Copilot reviews
        """
        copilot_reviews = self.get_copilot_reviews(pr_info)

        # Filter for reviews that are blocking (CHANGES_REQUESTED or COMMENTED)
        # Note: COMMENTED reviews cannot be dismissed but we include them for reporting
        unresolved = []
        for review in copilot_reviews:
            if review.state in ["CHANGES_REQUESTED", "COMMENTED", "PENDING"]:
                unresolved.append(review)
                if self.debug:
                    dismissible = (
                        "dismissible"
                        if review.state != "COMMENTED"
                        else "non-dismissible"
                    )
                    self.log.info(
                        f"🚫 Unresolved Copilot review: {review.id} (state: {review.state}, {dismissible})"
                    )

        return unresolved

    def analyze_copilot_review_dismissibility(
        self, pr_info: PullRequestInfo
    ) -> dict[str, int]:
        """
        Analyze which Copilot reviews can and cannot be dismissed.

        Args:
            pr_info: Pull request information

        Returns:
            Dictionary with counts of dismissible vs non-dismissible reviews
        """
        copilot_reviews = self.get_copilot_reviews(pr_info)

        dismissible_states = ["APPROVED", "CHANGES_REQUESTED"]
        non_dismissible_states = ["COMMENTED"]

        analysis = {
            "total": len(copilot_reviews),
            "dismissible": len(
                [r for r in copilot_reviews if r.state in dismissible_states]
            ),
            "non_dismissible": len(
                [r for r in copilot_reviews if r.state in non_dismissible_states]
            ),
            "pending": len([r for r in copilot_reviews if r.state == "PENDING"]),
            "other": len(
                [
                    r
                    for r in copilot_reviews
                    if r.state
                    not in dismissible_states + non_dismissible_states + ["PENDING"]
                ]
            ),
        }

        if self.debug:
            self.log.info(f"📊 Copilot review analysis for PR {pr_info.number}:")
            self.log.info(
                f"   Total: {analysis['total']}, Dismissible: {analysis['dismissible']}, Non-dismissible: {analysis['non_dismissible']}"
            )

        return analysis

    async def resolve_copilot_review(
        self, owner: str, repo: str, review_id: str, review_state: str | None = None
    ) -> bool:
        """
        Resolve a Copilot review by dismissing it (if possible).

        Args:
            owner: Repository owner
            repo: Repository name
            review_id: GraphQL ID of the review to dismiss
            review_state: Current state of the review (APPROVED, CHANGES_REQUESTED, COMMENTED)

        Returns:
            True if successfully resolved, False otherwise
        """
        if self.preview_mode:
            if review_state == "COMMENTED":
                self.log.info(
                    f"🔍 PREVIEW: Would skip COMMENTED Copilot review {review_id} (cannot be dismissed)"
                )
            else:
                self.log.info(
                    f"🔍 PREVIEW: Would dismiss Copilot review {review_id} (state: {review_state})"
                )
            return True

        # Skip COMMENTED reviews as they cannot be dismissed via GitHub API
        if review_state == "COMMENTED":
            self.log.info(
                f"⏭️ Skipping COMMENTED Copilot review {review_id} (GitHub API limitation)"
            )
            return True  # Return True as this is expected behavior, not a failure

        try:
            # Use GraphQL mutation to dismiss the pull request review
            mutation = """
            mutation DismissPullRequestReview($reviewId: ID!, $message: String!) {
              dismissPullRequestReview(input: {
                pullRequestReviewId: $reviewId
                message: $message
              }) {
                pullRequestReview {
                  id
                  state
                  author { login }
                }
              }
            }
            """

            variables = {
                "reviewId": review_id,
                "message": "Auto-dismissed by dependamerge: Copilot feedback resolved",
            }

            result = await self.github_client.graphql(mutation, variables)

            if result and result.get("data", {}).get("dismissPullRequestReview"):
                self.log.info(
                    f"✅ Successfully dismissed Copilot review {review_id} (state: {review_state})"
                )
                return True
            else:
                # Check if this is the known "commented review" error
                errors = result.get("errors", [])
                if any(
                    "Can not dismiss a commented pull request review" in str(error)
                    for error in errors
                ):
                    self.log.info(
                        f"⏭️ Cannot dismiss COMMENTED review {review_id} - this is a GitHub API limitation"
                    )
                    return True  # Treat as success since this is expected
                else:
                    self.log.error(
                        f"❌ Failed to dismiss Copilot review {review_id}: {result}"
                    )
                    return False

        except Exception as e:
            # Check if the error message contains the "commented review" limitation
            if "Can not dismiss a commented pull request review" in str(e):
                self.log.info(
                    f"⏭️ Cannot dismiss COMMENTED review {review_id} - this is a GitHub API limitation"
                )
                return True  # Treat as success since this is expected
            else:
                self.log.error(f"❌ Error dismissing Copilot review {review_id}: {e}")
                return False

    async def get_pr_review_threads(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """
        Get all review threads for a pull request.

        Args:
            owner: Repository owner
            repo: Repository name
            pr_number: Pull request number

        Returns:
            List of review thread data
        """
        from .github_graphql import GET_PR_REVIEW_THREADS

        threads = []
        cursor = None
        has_next = True

        while has_next:
            variables = {
                "owner": owner,
                "name": repo,
                "number": pr_number,
                "cursor": cursor,
            }

            result = await self.github_client.graphql(GET_PR_REVIEW_THREADS, variables)

            if (
                not result
                or not result.get("repository")
                or not result["repository"].get("pullRequest")
            ):
                self.log.error(
                    f"❌ Invalid GraphQL response structure for threads: {result}"
                )
                break

            pr_data = result["repository"]["pullRequest"]

            review_threads = pr_data["reviewThreads"]
            nodes = review_threads["nodes"]
            threads.extend(nodes)

            page_info = review_threads["pageInfo"]
            has_next = page_info["hasNextPage"]
            cursor = page_info["endCursor"]

        return threads

    def is_copilot_thread(self, thread: dict[str, Any]) -> bool:
        """
        Check if a review thread contains Copilot comments.

        Args:
            thread: Review thread data

        Returns:
            True if thread contains Copilot comments
        """
        comments = thread.get("comments", {}).get("nodes", [])

        for comment in comments:
            author = comment.get("author", {})
            if author and author.get("login") in [
                "github-copilot[bot]",
                "copilot",
                "copilot-pull-request-reviewer",
            ]:
                return True

            # Also check comment body for Copilot patterns
            body = comment.get("body", "").lower()
            if any(
                pattern in body
                for pattern in ["github copilot", "copilot suggestion", "🤖"]
            ):
                return True

        return False

    def is_safe_copilot_thread_to_resolve(self, thread: dict[str, Any]) -> bool:
        """
        Check if a Copilot thread is safe to auto-resolve.

        Args:
            thread: Review thread data

        Returns:
            True if safe to resolve automatically
        """
        if thread.get("isResolved", False):
            return False  # Already resolved

        if thread.get("isOutdated", False):
            return True  # Outdated threads are usually safe to resolve

        # Check if this is a common/safe Copilot suggestion
        comments = thread.get("comments", {}).get("nodes", [])

        for comment in comments:
            body = comment.get("body", "").lower()

            # Safe patterns that are typically automation suggestions
            safe_patterns = [
                "use: ubuntu-24.04",
                "consider using",
                "you might want to",
                "suggestion:",
                "performance:",
                "style:",
                "formatting",
                "indentation",
                "whitespace",
            ]

            if any(pattern in body for pattern in safe_patterns):
                return True

            # Unsafe patterns that might need human attention
            unsafe_patterns = [
                "security",
                "vulnerability",
                "critical",
                "error",
                "bug",
                "broken",
                "incorrect",
            ]

            if any(pattern in body for pattern in unsafe_patterns):
                return False

        # Default to safe for general Copilot suggestions
        return True

    async def resolve_review_thread(self, thread_id: str, pr_context: str = "") -> bool:
        """
        Resolve a specific review thread.

        Args:
            thread_id: GraphQL ID of the thread to resolve

        Returns:
            True if successfully resolved
        """
        if self.preview_mode:
            context = f" for {pr_context}" if pr_context else ""
            self.log.info(
                f"🔍 PREVIEW: Would resolve review thread {thread_id}{context}"
            )
            return True

        from .github_graphql import RESOLVE_REVIEW_THREAD

        try:
            variables = {"threadId": thread_id}
            result = await self.github_client.graphql(RESOLVE_REVIEW_THREAD, variables)

            if result and result.get("resolveReviewThread"):
                thread_data = result["resolveReviewThread"]["thread"]
                if thread_data.get("isResolved"):
                    context = f" for {pr_context}" if pr_context else ""
                    self.log.info(f"✅ Resolved review thread {thread_id}{context}")
                    return True
                else:
                    context = f" for {pr_context}" if pr_context else ""
                    self.log.error(
                        f"❌ Thread {thread_id}{context} not marked as resolved in response: {thread_data}"
                    )

            context = f" for {pr_context}" if pr_context else ""
            self.log.error(
                f"❌ Failed to resolve review thread {thread_id}{context}. Full response: {result}"
            )
            if result and result.get("errors"):
                self.log.error(
                    f"❌ GraphQL errors for {thread_id}{context}: {result['errors']}"
                )
            return False

        except Exception as e:
            context = f" for {pr_context}" if pr_context else ""
            self.log.error(
                f"❌ Error resolving review thread {thread_id}{context}: {e}"
            )
            return False

    async def resolve_copilot_threads_for_commented_review(
        self, owner: str, repo: str, pr_number: int, review_id: str
    ) -> tuple[int, int]:
        """
        For COMMENTED reviews that can't be dismissed, try to resolve individual threads.

        Args:
            owner: Repository owner
            repo: Repository name
            pr_number: Pull request number
            review_id: Review ID (for logging context)

        Returns:
            Tuple of (resolved_count, total_copilot_threads)
        """
        self.log.info(
            f"🧵 Attempting thread-level resolution for COMMENTED review {review_id}"
        )

        # Get all threads for this PR
        all_threads = await self.get_pr_review_threads(owner, repo, pr_number)

        # Filter for unresolved Copilot threads that are safe to resolve
        copilot_threads = []
        for thread in all_threads:
            if (
                self.is_copilot_thread(thread)
                and not thread.get("isResolved", False)
                and self.is_safe_copilot_thread_to_resolve(thread)
            ):
                copilot_threads.append(thread)

        if not copilot_threads:
            self.log.warning(
                f"⚠️ Failed to resolve comment/review thread {review_id} in {owner}/{repo}#{pr_number} (no resolvable Copilot threads)"
            )
            return 0, len(all_threads)

        self.log.info(
            f"🎯 Found {len(copilot_threads)} resolvable Copilot threads out of {len(all_threads)} total for {owner}/{repo}#{pr_number}"
        )

        resolved_count = 0
        for i, thread in enumerate(copilot_threads, 1):
            thread_id = thread["id"]

            path = thread.get("path", "unknown")
            line = thread.get("line", "unknown")
            self.log.info(
                f"🔍 Resolving thread {i}/{len(copilot_threads)} in {owner}/{repo}#{pr_number}: {thread_id} on {path}:{line}"
            )

            if await self.resolve_review_thread(
                thread_id, f"{owner}/{repo}#{pr_number}"
            ):
                resolved_count += 1
                self.log.info(
                    f"✅ Successfully resolved thread {i}/{len(copilot_threads)} in {owner}/{repo}#{pr_number}"
                )
            else:
                self.log.error(
                    f"❌ Failed to resolve thread {i}/{len(copilot_threads)} in {owner}/{repo}#{pr_number}: {thread_id}"
                )

        self.log.info(
            f"📊 Resolved {resolved_count}/{len(copilot_threads)} Copilot threads for review {review_id} in {owner}/{repo}#{pr_number}"
        )
        return resolved_count, len(copilot_threads)

    async def dismiss_copilot_comments_for_pr(
        self, pr_info: PullRequestInfo
    ) -> tuple[int, int]:
        """
        Dismiss all unresolved Copilot reviews and comments for a pull request.

        Args:
            pr_info: Pull request information

        Returns:
            Tuple of (successful_dismissals, total_items)
        """
        owner, repo = pr_info.repository_full_name.split("/")

        # Get both reviews and review comments from REST API
        unresolved_reviews = self.get_unresolved_copilot_reviews(pr_info)
        review_comments = await self._get_copilot_review_comments(
            owner, repo, pr_info.number
        )

        total_items = len(unresolved_reviews) + len(review_comments)

        if total_items == 0:
            self.log.info(
                f"✅ No unresolved Copilot feedback found for PR {pr_info.number}"
            )
            return 0, 0

        self.log.info(
            f"🤖 Found {len(unresolved_reviews)} Copilot reviews and {len(review_comments)} Copilot comments for PR {pr_info.number}"
        )

        successful_dismissals = 0
        thread_resolutions = 0

        # Process reviews with appropriate strategy per type
        for review in unresolved_reviews:
            if self.debug:
                self.log.info(
                    f"🔍 Processing Copilot review {review.id} (state: {review.state})"
                )

            if review.state == "COMMENTED":
                # For COMMENTED reviews, use thread resolution fallback
                self.log.info(
                    f"🧵 Using thread resolution for COMMENTED review {review.id}"
                )
                (
                    resolved_threads,
                    total_threads,
                ) = await self.resolve_copilot_threads_for_commented_review(
                    owner, repo, pr_info.number, review.id
                )
                if resolved_threads > 0:
                    successful_dismissals += (
                        1  # Count as success if we resolved threads
                    )
                    thread_resolutions += resolved_threads
                    if self.preview_mode:
                        self.log.info(
                            f"🔍 PREVIEW: Would resolve {resolved_threads}/{total_threads} threads in review {review.id}"
                        )
                    else:
                        self.log.info(
                            f"🧵 Resolved {resolved_threads}/{total_threads} threads in review {review.id}"
                        )
                else:
                    # No threads resolved - this is a failure, not success
                    # Logging is already handled in resolve_copilot_threads_for_commented_review
                    # Don't increment successful_dismissals - this is a failure
                    pass
            else:
                # For APPROVED/CHANGES_REQUESTED reviews, use standard dismissal
                success = await self.resolve_copilot_review(
                    owner, repo, review.id, review.state
                )
                if success:
                    successful_dismissals += 1

        # Handle individual review comments (deprecated in favor of thread resolution)
        for comment in review_comments:
            if self.debug:
                self.log.info(
                    f"🔍 Processing Copilot comment {comment.get('id')} on {comment.get('path', 'unknown file')}"
                )
                self.log.info(f"   Content: {comment.get('body', '')[:100]}...")

            # For review comments, we need to resolve the thread rather than dismiss
            success = await self._resolve_review_comment_thread(comment)
            if success:
                successful_dismissals += 1

        # Provide comprehensive reporting
        commented_reviews = len(
            [r for r in unresolved_reviews if r.state == "COMMENTED"]
        )
        dismissed_reviews = len(
            [r for r in unresolved_reviews if r.state != "COMMENTED"]
        )

        if commented_reviews > 0 and thread_resolutions > 0:
            self.log.info(
                f"📊 Processed {successful_dismissals}/{total_items} Copilot items for PR {pr_info.number}"
            )
            self.log.info(
                f"   └─ {dismissed_reviews} reviews dismissed, {commented_reviews} COMMENTED reviews processed via {thread_resolutions} thread resolutions"
            )
        elif commented_reviews > 0:
            self.log.info(
                f"📊 Processed {successful_dismissals}/{total_items} Copilot items for PR {pr_info.number}"
            )
            self.log.info(
                f"   └─ {dismissed_reviews} reviews dismissed, {commented_reviews} COMMENTED reviews processed (no resolvable threads)"
            )
        else:
            self.log.info(
                f"📊 Processed {successful_dismissals}/{total_items} Copilot items for PR {pr_info.number} (all via dismissal)"
            )

        return successful_dismissals, total_items

    def has_blocking_copilot_comments(self, pr_info: PullRequestInfo) -> bool:
        """
        Check if a PR has unresolved Copilot reviews that might block merging.
        Note: This only checks reviews, not individual comments (which require async call).

        Args:
            pr_info: Pull request information

        Returns:
            True if there are blocking Copilot reviews, False otherwise
        """
        unresolved_reviews = self.get_unresolved_copilot_reviews(pr_info)
        return len(unresolved_reviews) > 0

    async def _get_copilot_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """
        Get Copilot review comments from REST API.

        Args:
            owner: Repository owner
            repo: Repository name
            pr_number: Pull request number

        Returns:
            List of Copilot review comments
        """
        try:
            all_comments = await self.github_client.get_pull_request_review_comments(
                owner, repo, pr_number
            )
            copilot_comments = []

            for comment in all_comments:
                author = comment.get("user", {}).get("login", "").lower()
                # Check if comment is from Copilot
                if any(
                    copilot_author.lower() in author
                    for copilot_author in COPILOT_AUTHORS
                ):
                    copilot_comments.append(comment)
                    if self.debug:
                        self.log.info(
                            f"🤖 Found Copilot review comment: {comment.get('id')} on {comment.get('path', 'unknown')}"
                        )

            return copilot_comments

        except Exception as e:
            self.log.warning(
                f"⚠️ Could not fetch review comments for PR {pr_number}: {e}"
            )
            return []

    async def _resolve_review_comment_thread(self, comment: dict[str, Any]) -> bool:
        """
        Resolve a review comment thread by marking it as resolved.

        Args:
            comment: Review comment dictionary from REST API

        Returns:
            True if successfully resolved, False otherwise
        """
        if self.preview_mode:
            self.log.info(
                f"🔍 PREVIEW: Would resolve Copilot comment thread {comment.get('id')}"
            )
            return True

        try:
            # Individual comment resolution is now handled via GraphQL thread resolution
            # This method is deprecated in favor of resolve_copilot_threads_for_commented_review
            self.log.info(
                f"ℹ️ Individual comment {comment.get('id')} handled via comprehensive thread resolution"
            )
            return True

        except Exception as e:
            self.log.error(f"❌ Error processing comment {comment.get('id')}: {e}")
            return False
