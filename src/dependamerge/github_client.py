# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

import os
from typing import List, Optional

from github import Github, GithubException
from github.PullRequest import PullRequest
from github.Repository import Repository

from .models import FileChange, PullRequestInfo


class GitHubClient:
    """GitHub API client for managing pull requests."""

    def __init__(self, token: Optional[str] = None):
        """Initialize GitHub client with token."""
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            raise ValueError(
                "GitHub token is required. Set GITHUB_TOKEN environment variable."
            )
        self.github = Github(self.token)

    def parse_pr_url(self, url: str) -> tuple[str, str, int]:
        """Parse GitHub PR URL to extract owner, repo, and PR number."""
        # Expected format: https://github.com/owner/repo/pull/123[/files|/commits|etc]
        parts = url.rstrip("/").split("/")
        if len(parts) < 7 or "github.com" not in url or "pull" not in parts:
            raise ValueError(f"Invalid GitHub PR URL: {url}")

        # Find the 'pull' segment and get the PR number from the next segment
        try:
            pull_index = parts.index("pull")
            if pull_index + 1 >= len(parts):
                raise ValueError("PR number not found after 'pull'")

            owner = parts[pull_index - 2]
            repo = parts[pull_index - 1]
            pr_number = int(parts[pull_index + 1])

            return owner, repo, pr_number
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid GitHub PR URL: {url}") from e

    def get_pull_request_info(
        self, owner: str, repo: str, pr_number: int
    ) -> PullRequestInfo:
        """Get detailed information about a pull request."""
        try:
            repository = self.github.get_repo(f"{owner}/{repo}")
            pr = repository.get_pull(pr_number)

            # Get file changes
            files_changed = []
            for file in pr.get_files():
                files_changed.append(
                    FileChange(
                        filename=file.filename,
                        additions=file.additions,
                        deletions=file.deletions,
                        changes=file.changes,
                        status=file.status,
                    )
                )

            return PullRequestInfo(
                number=pr.number,
                title=pr.title,
                body=pr.body,
                author=pr.user.login,
                head_sha=pr.head.sha,
                base_branch=pr.base.ref,
                head_branch=pr.head.ref,
                state=pr.state,
                mergeable=pr.mergeable,
                mergeable_state=pr.mergeable_state,
                behind_by=getattr(pr, "behind_by", None),
                files_changed=files_changed,
                repository_full_name=repository.full_name,
                html_url=pr.html_url,
            )
        except GithubException as e:
            raise RuntimeError(f"Failed to fetch PR info: {e}") from e

    def get_organization_repositories(self, org_name: str) -> List[Repository]:
        """Get all repositories in an organization."""
        try:
            org = self.github.get_organization(org_name)
            return list(org.get_repos())
        except GithubException as e:
            raise RuntimeError(f"Failed to fetch organization repositories: {e}") from e

    def get_open_pull_requests(self, repository: Repository) -> List[PullRequest]:
        """Get all open pull requests for a repository."""
        try:
            return list(repository.get_pulls(state="open"))
        except GithubException as e:
            print(f"Warning: Failed to fetch PRs for {repository.full_name}: {e}")
            return []

    def approve_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        message: str = "Auto-approved by dependamerge",
    ) -> bool:
        """Approve a pull request."""
        try:
            repository = self.github.get_repo(f"{owner}/{repo}")
            pr = repository.get_pull(pr_number)
            pr.create_review(body=message, event="APPROVE")
            return True
        except GithubException as e:
            print(f"Failed to approve PR {pr_number}: {e}")
            return False

    def merge_pull_request(
        self, owner: str, repo: str, pr_number: int, merge_method: str = "merge"
    ) -> bool:
        """Merge a pull request."""
        try:
            repository = self.github.get_repo(f"{owner}/{repo}")
            pr = repository.get_pull(pr_number)

            if not pr.mergeable:
                print(f"PR {pr_number} is not mergeable")
                return False

            result = pr.merge(merge_method=merge_method)
            return bool(result.merged)
        except GithubException as e:
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

        return "Ready to merge"

    def fix_out_of_date_pr(self, owner: str, repo: str, pr_number: int) -> bool:
        """Fix an out-of-date PR by updating the branch."""
        try:
            repository = self.github.get_repo(f"{owner}/{repo}")
            pr = repository.get_pull(pr_number)

            if pr.mergeable_state != "behind":
                print(f"PR {pr_number} is not behind the base branch")
                return False

            # Update the branch using GitHub's update branch API
            pr.update_branch()
            return True
        except GithubException as e:
            print(f"Failed to update PR {pr_number}: {e}")
            return False
