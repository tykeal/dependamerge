# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

import asyncio
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlparse

from .models import (
    FileChange,
    OrganizationScanResult,
    PullRequestInfo,
    ReviewInfo,
)
from .url_parser import _host_matches

if TYPE_CHECKING:
    from .progress_tracker import ProgressTracker


class GitHubClient:
    """GitHub API client for managing pull requests."""

    def __init__(self, token: str | None = None):
        """Initialize GitHub client with token."""
        resolved = token or os.getenv("GITHUB_TOKEN")
        if not resolved:
            raise ValueError(
                "GitHub token is required. Set GITHUB_TOKEN environment variable."
            )
        self.token: str = resolved

    def __repr__(self) -> str:
        """Safe repr that never exposes the token value."""
        return "GitHubClient(token=***)"

    def parse_pr_url(self, url: str) -> tuple[str, str, int]:
        """Parse GitHub PR URL to extract owner, repo, and PR number."""
        # SECURITY: Use urlparse for host extraction, not substring checks.
        # See CodeQL rule py/incomplete-url-substring-sanitization.
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not _host_matches(host, "github.com"):
            raise ValueError(f"Invalid GitHub PR URL: {url}")

        # Use parsed.path to ignore query strings and fragments
        # when splitting.
        parts = parsed.path.strip("/").split("/")
        if "pull" not in parts:
            raise ValueError(f"Invalid GitHub PR URL: {url}")

        # Find the 'pull' segment and get the PR number
        try:
            pull_index = parts.index("pull")
            if pull_index + 1 >= len(parts):
                raise ValueError(
                    "PR number not found after 'pull'"
                )

            owner = parts[pull_index - 2]
            repo = parts[pull_index - 1]
            pr_number = int(parts[pull_index + 1])

            return owner, repo, pr_number
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid GitHub PR URL: {url}") from e

    def get_pull_request_info(
        self, owner: str, repo: str, pr_number: int
    ) -> PullRequestInfo:
        """Get detailed information about a pull request using the async REST client."""
        from .github_async import GitHubAsync

        async def _run() -> PullRequestInfo:
            async with GitHubAsync(token=self.token) as api:
                pr_response = await api.get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
                assert isinstance(pr_response, dict), (
                    "PR endpoint should return a dictionary"
                )
                pr: dict[str, Any] = pr_response
                files_changed: list[FileChange] = []
                try:
                    async for page in api.get_paginated(
                        f"/repos/{owner}/{repo}/pulls/{pr_number}/files", per_page=100
                    ):
                        for f in page:
                            file_data = f
                            assert isinstance(file_data, dict)
                            files_changed.append(
                                FileChange(
                                    filename=file_data.get("filename", ""),
                                    additions=int(file_data.get("additions", 0)),
                                    deletions=int(file_data.get("deletions", 0)),
                                    changes=int(
                                        file_data.get(
                                            "changes",
                                            (file_data.get("additions", 0) or 0)
                                            + (file_data.get("deletions", 0) or 0),
                                        )
                                    ),
                                    status=file_data.get("status", "modified"),
                                )
                            )
                except Exception:
                    # If pagination of files fails, continue with what we have
                    pass

                # Fetch reviews
                reviews: list[ReviewInfo] = []
                try:
                    review_response = await api.get(
                        f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
                    )
                    assert isinstance(review_response, list), (
                        "Reviews endpoint should return a list"
                    )
                    review_data = review_response
                    # review_data is a list of review dictionaries
                    for review in review_data:
                        if review.get("user") and review.get("state"):
                            reviews.append(
                                ReviewInfo(
                                    # NOTE: REST API returns string IDs that look numeric but may be node IDs
                                    # Do not convert to int() - keep as string to match GraphQL behavior
                                    id=review.get("id", ""),
                                    user=review.get("user", {}).get("login", ""),
                                    state=review.get("state", ""),
                                    submitted_at=review.get("submitted_at", ""),
                                    body=review.get("body"),
                                )
                            )
                except Exception:
                    # If review fetching fails, continue without reviews
                    pass

                return PullRequestInfo(
                    number=int(pr.get("number", pr_number)),
                    node_id=pr.get("node_id"),  # REST API uses "node_id" key
                    title=pr.get("title") or "",
                    body=pr.get("body"),
                    author=((pr.get("user") or {}).get("login") or ""),
                    head_sha=((pr.get("head") or {}).get("sha") or ""),
                    base_branch=((pr.get("base") or {}).get("ref") or ""),
                    head_branch=((pr.get("head") or {}).get("ref") or ""),
                    state=pr.get("state") or "open",
                    mergeable=pr.get("mergeable"),
                    mergeable_state=pr.get("mergeable_state"),
                    behind_by=None,
                    files_changed=files_changed,
                    repository_full_name=f"{owner}/{repo}",
                    html_url=pr.get("html_url") or "",
                    reviews=reviews,
                    # Populate head/base repo identity so the
                    # signature-preserving local-rebase path can
                    # tell whether the PR is from a fork (and
                    # which remote to push to).  Without these,
                    # ``rebase.local_rebase_pr()`` fails closed
                    # to avoid pushing to the wrong repository.
                    head_repo_full_name=(
                        ((pr.get("head") or {}).get("repo") or {}).get("full_name")
                    ),
                    head_repo_clone_url=(
                        ((pr.get("head") or {}).get("repo") or {}).get("clone_url")
                    ),
                    base_repo_full_name=(
                        ((pr.get("base") or {}).get("repo") or {}).get("full_name")
                    ),
                    base_repo_clone_url=(
                        ((pr.get("base") or {}).get("repo") or {}).get("clone_url")
                    ),
                    is_fork=(
                        ((pr.get("head") or {}).get("repo") or {}).get("fork")
                    ),
                )

        return asyncio.run(_run())  # type: ignore[no-any-return]

    def get_pull_request_commits(
        self, owner: str, repo: str, pr_number: int
    ) -> list[str]:
        """Get commit messages from a pull request using the async REST client."""
        from .github_async import GitHubAsync

        async def _run() -> list[str]:
            messages: list[str] = []
            async with GitHubAsync(token=self.token) as api:
                async for page in api.get_paginated(
                    f"/repos/{owner}/{repo}/pulls/{pr_number}/commits", per_page=100
                ):
                    for c in page:
                        commit_data = c
                        assert isinstance(commit_data, dict)
                        msg = (commit_data.get("commit") or {}).get("message") or ""
                        if msg:
                            messages.append(msg)
            return messages

        return asyncio.run(_run())

    def get_organization_repositories(self, org_name: str) -> list[str]:
        """Get all repositories in an organization using REST API. Returns list of full_name strings."""
        from .github_async import GitHubAsync

        async def _run() -> list[str]:
            repos: list[str] = []
            async with GitHubAsync(token=self.token) as api:
                try:
                    async for page in api.get_paginated(
                        f"/orgs/{org_name}/repos", per_page=100
                    ):
                        for r in page:
                            repo_data = r
                            assert isinstance(repo_data, dict)
                            full = repo_data.get("full_name")
                            if full:
                                repos.append(full)
                except Exception:
                    # Fall back to empty list on pagination issues
                    pass
            return repos

        return asyncio.run(_run())

    def get_open_pull_requests(self, repository) -> list[Any]:
        """Legacy method not supported in async-only client. Use async service for PR enumeration."""
        return []

    def approve_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        message: str = "Auto-approved by dependamerge",
    ) -> bool:
        """Approve a pull request using the async REST client."""
        try:
            from .github_async import GitHubAsync

            async def _run():
                async with GitHubAsync(token=self.token) as api:
                    await api.approve_pull_request(owner, repo, pr_number, message)
                    return True

            return bool(asyncio.run(_run()))
        except Exception as e:
            print(f"Failed to approve PR {pr_number}: {e}")
            return False

    def merge_pull_request(
        self, owner: str, repo: str, pr_number: int, merge_method: str = "merge"
    ) -> bool:
        """Merge a pull request using the async REST client."""
        try:
            from .github_async import GitHubAsync

            async def _run():
                async with GitHubAsync(token=self.token) as api:
                    return await api.merge_pull_request(
                        owner, repo, pr_number, merge_method
                    )

            return bool(asyncio.run(_run()))
        except Exception as e:
            print(f"Failed to merge PR {pr_number}: {e}")
            return False

    def is_automation_author(self, author: str) -> bool:
        """Check if the author is a known automation tool."""
        automation_authors = {
            "dependabot[bot]",
            "pre-commit-ci[bot]",
            "renovate[bot]",
            "github-actions[bot]",
            "allcontributors[bot]",
        }
        return author in automation_authors

    def get_pr_status_details(self, pr_info: PullRequestInfo) -> str:
        """Get detailed status information for a PR."""
        if pr_info.state != "open":
            return f"Closed ({pr_info.state})"

        # Check for draft status first
        if pr_info.mergeable_state == "draft":
            return "Draft PR"

        # Handle blocked state - need to determine why it's blocked
        if pr_info.mergeable_state == "blocked" and pr_info.mergeable is True:
            # This means technically mergeable but blocked by branch protection
            # We need to check what's blocking it to provide intelligent status
            block_reason = self._analyze_block_reason(pr_info)
            return block_reason

        if pr_info.mergeable is False:
            # Check for specific reasons why it's not mergeable
            if pr_info.mergeable_state == "dirty":
                return "Merge conflicts"
            elif pr_info.mergeable_state == "behind":
                return "Rebase required"
            elif pr_info.mergeable_state == "blocked":
                return "Blocked by checks"
            else:
                return f"Not mergeable ({pr_info.mergeable_state or 'unknown'})"

        if pr_info.mergeable_state == "behind":
            return "Rebase required"

        # If mergeable is True and mergeable_state is clean, it's ready
        if pr_info.mergeable is True and pr_info.mergeable_state == "clean":
            return "Ready to merge"

        # Handle unstable state - this usually means CI is running but PR is mergeable
        if pr_info.mergeable is True and pr_info.mergeable_state == "unstable":
            return "Ready to merge"

        # For any other combination where mergeable is True but state is unclear
        if pr_info.mergeable is True:
            return "Ready to merge"

        # Fallback for unclear states
        return f"Status unclear ({pr_info.mergeable_state or 'unknown'})"

    def _analyze_block_reason(self, pr_info: PullRequestInfo) -> str:
        """Analyze why a PR is blocked and return appropriate status using REST."""
        try:
            from .github_async import GitHubAsync

            repo_owner, repo_name = pr_info.repository_full_name.split("/")

            # Check if we're already in an event loop
            try:
                asyncio.get_running_loop()
                # We're in an async context - can't use asyncio.run()
                # Return a basic status message to avoid the coroutine warning
                # The caller should use the async version instead
                return "Blocked by branch protection"
            except RuntimeError:
                # No event loop running - safe to use asyncio.run()
                pass

            async def _run():
                async with GitHubAsync(token=self.token) as api:
                    return await api.analyze_block_reason(
                        repo_owner, repo_name, pr_info.number, pr_info.head_sha
                    )

            return asyncio.run(_run())  # type: ignore[no-any-return]
        except Exception:
            return "Blocked"

    def _should_attempt_merge(self, pr) -> bool:
        """
        Determine if we should attempt to merge a PR based on its mergeable state.

        Returns True if merge should be attempted, False otherwise.
        """
        # If mergeable is explicitly False, only attempt merge for blocked state
        # where branch protection might resolve after approval
        if pr.mergeable is False:
            # For blocked state, we can attempt merge as approval might resolve the block
            # For other states (dirty, behind), don't attempt as they need manual fixes
            return bool(pr.mergeable_state == "blocked")

        # If mergeable is None, GitHub is still calculating - be conservative
        if pr.mergeable is None:
            # Only attempt if state suggests it might work
            return bool(pr.mergeable_state in ["clean", "blocked"])

        # If mergeable is True, attempt merge for most states except draft
        if pr.mergeable is True:
            return bool(pr.mergeable_state != "draft")

        # Fallback to False for any unexpected cases
        return False

    def fix_out_of_date_pr(self, owner: str, repo: str, pr_number: int) -> bool:
        """Fix an out-of-date PR by updating the branch."""
        try:
            from .github_async import GitHubAsync

            async def _run():
                async with GitHubAsync(token=self.token) as api:
                    await api.update_branch(owner, repo, pr_number)
                    return True

            return bool(asyncio.run(_run()))
        except Exception as e:
            print(f"Failed to update PR {pr_number}: {e}")
            return False

    def scan_organization_for_unmergeable_prs(
        self,
        org_name: str,
        progress_tracker: Optional["ProgressTracker"] = None,
        include_drafts: bool = False,
    ) -> OrganizationScanResult:
        """Scan an entire GitHub organization for unmergeable pull requests using the async service.

        Args:
            org_name: The organization name to scan.
            progress_tracker: Optional progress tracker for UI updates.
            include_drafts: If True, include draft PRs in results. If False (default),
                          filter out PRs that are only blocked due to draft status.
        """
        scan_timestamp = datetime.now().isoformat()
        from .github_service import GitHubService

        async def _run():
            svc = GitHubService(token=self.token, progress_tracker=progress_tracker)
            try:
                return await svc.scan_organization(
                    org_name, include_drafts=include_drafts
                )
            finally:
                await svc.close()

        try:
            return asyncio.run(_run())  # type: ignore[no-any-return]
        except Exception as e:
            return OrganizationScanResult(
                organization=org_name,
                total_repositories=0,
                scanned_repositories=0,
                total_prs=0,
                unmergeable_prs=[],
                scan_timestamp=scan_timestamp,
                errors=[f"{e}"],
            )
