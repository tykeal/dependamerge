# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from .bot_identity import canonical_bot_login, is_automation_author
from .github_async import (
    GitHubAsync,
    GraphQLError,
    RateLimitError,
    SecondaryRateLimitError,
)
from .github_graphql import (
    GET_BRANCH_PROTECTION,
    ORG_REPOS_ONLY,
    REPO_OPEN_PRS_PAGE,
    USER_REPOS_ONLY,
)
from .models import (
    ComparisonResult,
    CopilotComment,
    FileChange,
    OrganizationScanResult,
    OrganizationStatus,
    PullRequestInfo,
    RepositoryStatus,
    ReviewInfo,
    UnmergeablePR,
    UnmergeableReason,
)

# GitHub API tuning defaults - optimized for performance and rate limit compliance
DEFAULT_PRS_PAGE_SIZE = 30  # Pull requests per GraphQL page
DEFAULT_FILES_PAGE_SIZE = 50  # Files per pull request
DEFAULT_COMMENTS_PAGE_SIZE = 10  # Comments per pull request
DEFAULT_CONTEXTS_PAGE_SIZE = 20  # Status contexts per pull request

# Automation tools recognized for PR categorization
AUTOMATION_TOOLS = [
    "dependabot",
    "renovate",
    "pre-commit",
    "github-actions",
    "copilot",
    "[bot]",
]


def _str_or_none(value: Any) -> str | None:
    """Return ``value`` as a string when truthy, else None.

    Used when populating optional ``PullRequestInfo`` fields from
    GraphQL responses where the field may be missing entirely or
    explicitly null.
    """
    if isinstance(value, str) and value:
        return value
    return None


def _bool_or_none(value: Any) -> bool | None:
    """Coerce ``value`` to bool when present, else None.

    Mirrors :func:`_str_or_none` for boolean GraphQL fields
    (``isFork``).
    """
    if isinstance(value, bool):
        return value
    return None


def _clone_url_with_git_suffix(url: Any) -> str | None:
    """Synthesise a canonical ``.git`` clone URL from a GraphQL ``url``.

    GraphQL's ``Repository.url`` returns the HTTPS URL without the
    ``.git`` suffix that REST's ``clone_url`` includes.  This
    helper appends ``.git`` so both code paths produce the same
    string and downstream consumers (notably
    :func:`rebase.local_rebase_pr`) can treat them uniformly.

    Returns None when the input is missing or empty so the
    PullRequestInfo field stays unset rather than holding a
    bogus ``".git"`` string.
    """
    if isinstance(url, str) and url:
        return f"{url}.git"
    return None


class GitHubService:
    """
    Asynchronous service orchestrating GraphQL paging and mapping results
    into the project's existing Pydantic models. Designed to be used by a thin
    adapter so the rest of the codebase can keep a stable interface.

    This service:
      - Paginates organization repositories and their open PRs via GraphQL
      - Extracts status rollups, file changes, and Copilot comments
      - Detects common unmergeable reasons
      - Provides helpers to convert GraphQL PR nodes to PullRequestInfo
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        progress_tracker: Any | None = None,
        max_repo_tasks: int = 8,
        max_page_tasks: int = 16,
        debug_matching: bool = False,
    ) -> None:
        """
        Args:
            token: GitHub token; if None, reads from env GITHUB_TOKEN.
            progress_tracker: Optional ProgressTracker-compatible instance.
            max_repo_tasks: Max concurrent repository scans to schedule at once.
            debug_matching: Enable detailed debugging output for PR matching.
        """
        self._api = GitHubAsync(
            token=token,
            on_rate_limited=self._on_rate_limited,
            on_rate_limit_cleared=self._on_rate_limit_cleared,
            on_metrics=self._on_metrics,
        )
        self._progress = progress_tracker
        self._max_repo_tasks = max_repo_tasks
        self._max_page_tasks = max_page_tasks
        self._repo_semaphore = asyncio.Semaphore(self._max_repo_tasks)
        self._page_semaphore = asyncio.Semaphore(self._max_page_tasks)
        # Rate limit awareness
        self._rate_limited = False
        self._debug_matching = debug_matching
        # Cache for branch protection settings to avoid repeated API calls
        self._branch_protection_cache: dict[str, dict[str, Any] | None] = {}
        # Cache for resolved owner account type (organization vs user),
        # keyed by owner login.  Value is a ``(root_key, query)`` tuple so
        # repeated repository-pagination pages do not re-probe.
        self._owner_root_cache: dict[str, tuple[str, str]] = {}
        self.log = logging.getLogger(__name__)

    async def close(self) -> None:
        await self._api.aclose()

    # -----------------------
    # ProgressTracker bridges
    # -----------------------

    async def _on_rate_limited(self, reset_epoch: float) -> None:
        # Mark rate-limited and report current tuning metrics
        self._rate_limited = True
        if self._progress:
            try:
                reset_time = datetime.fromtimestamp(reset_epoch)
                self._progress.set_rate_limited(reset_time)
                # Report current tuning metrics for visibility
                self._progress.update_operation(
                    f"Tuning: prs={DEFAULT_PRS_PAGE_SIZE} files={DEFAULT_FILES_PAGE_SIZE} comments={DEFAULT_COMMENTS_PAGE_SIZE} contexts={DEFAULT_CONTEXTS_PAGE_SIZE}"
                )
            except Exception:
                # Progress display is best-effort; ignore UI errors.
                pass

    async def _on_rate_limit_cleared(self) -> None:
        # Clear rate-limited flag and report current tuning metrics
        self._rate_limited = False
        if not self._progress:
            return
        try:
            self._progress.clear_rate_limited()
            self._progress.update_operation(
                f"Tuning: prs={DEFAULT_PRS_PAGE_SIZE} files={DEFAULT_FILES_PAGE_SIZE} comments={DEFAULT_COMMENTS_PAGE_SIZE} contexts={DEFAULT_CONTEXTS_PAGE_SIZE}"
            )
        except Exception:
            # Progress display is best-effort; ignore UI errors.
            pass

    async def _on_metrics(self, concurrency: int, rps: float) -> None:
        """Receive current concurrency and RPS from the async client and push to progress display."""
        if not self._progress:
            return
        try:
            # Round RPS to a single decimal for display, actual value passed through
            self._progress.update_metrics(concurrency, rps)
        except Exception:
            # Metrics are best-effort; ignore UI errors
            pass

    # -----------------------
    # Public high-level APIs
    # -----------------------

    async def scan_organization(
        self, org: str, include_drafts: bool = False
    ) -> OrganizationScanResult:
        """
        Scan an organization for unmergeable PRs using GraphQL in a batched,
        parallel fashion with bounded concurrency.

        Args:
            org: The organization name to scan.
            include_drafts: If True, include draft PRs in the results. If False (default),
                          filter out PRs that are only blocked due to draft status.

        Returns:
            OrganizationScanResult with aggregated data and errors.
        """
        errors: list[str] = []
        unmergeable_prs: list[UnmergeablePR] = []
        total_repositories = 0
        scanned_repositories = 0
        total_prs = 0

        # Process repositories with bounded parallelism
        # (repo total is set automatically by _iter_org_repositories
        # on the first GraphQL page via totalCount)
        async def process_repo(
            repo_node: dict[str, Any],
        ) -> tuple[list[UnmergeablePR], int, int, list[str]]:
            async with self._repo_semaphore:
                repo_errors: list[str] = []
                repo_full_name = repo_node.get("nameWithOwner", "unknown/unknown")
                if self._progress:
                    self._progress.start_repository(repo_full_name)
                try:
                    owner, name = self._split_owner_repo(repo_full_name)
                    first_nodes, page_info = await self._fetch_repo_prs_first_page(
                        owner, name
                    )
                    prs_nodes: list[dict[str, Any]] = list(first_nodes)
                    has_next = bool(page_info.get("hasNextPage"))
                    end_cursor = page_info.get("endCursor")

                    # Include additional pages of PRs if present
                    if has_next:
                        async for pr_node in self._iter_repo_open_prs_pages(
                            owner, name, end_cursor
                        ):
                            prs_nodes.append(pr_node)

                    repo_total_prs = len(prs_nodes)

                    # Analyze PRs concurrently within this repository
                    tasks = [
                        self._analyze_pr_node(repo_full_name, pr_node, include_drafts)
                        for pr_node in prs_nodes
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    repo_unmergeables: list[UnmergeablePR] = []
                    for r in results:
                        if isinstance(r, Exception):
                            repo_errors.append(
                                f"Error analyzing PR in {repo_full_name}: {r}"
                            )
                            if self._progress:
                                self._progress.add_error()
                            continue
                        if r is not None and isinstance(r, UnmergeablePR):
                            repo_unmergeables.append(r)

                    if self._progress:
                        self._progress.complete_repository(len(repo_unmergeables))

                    # Return: unmergeables, prs count, scanned_repos_inc, errors
                    return repo_unmergeables, repo_total_prs, 1, repo_errors
                except Exception as e:
                    if self._progress:
                        self._progress.add_error()
                    # Return no unmergeables, no prs counted, no scanned increment, but record error
                    return (
                        [],
                        0,
                        0,
                        [f"Error scanning repository {repo_full_name}: {e}"],
                    )

        tasks: list[asyncio.Task[Any]] = []
        async for repo in self._iter_org_repositories_with_open_prs(org):
            tasks.append(asyncio.create_task(process_repo(repo)))

        total_repositories = len(tasks)

        if tasks:
            results = await asyncio.gather(*tasks)
            for repo_unmergeables, repo_prs_count, scanned_inc, repo_errors in results:
                unmergeable_prs.extend(repo_unmergeables)
                total_prs += repo_prs_count
                scanned_repositories += scanned_inc
                if repo_errors:
                    errors.extend(repo_errors)

        return OrganizationScanResult(
            organization=org,
            total_repositories=total_repositories,
            scanned_repositories=scanned_repositories,
            total_prs=total_prs,
            unmergeable_prs=unmergeable_prs,
            scan_timestamp=datetime.now().isoformat(),
            errors=errors,
        )

    # -------------------------------------------------
    # Iterators and pagination for repos and repo PRs
    # -------------------------------------------------

    async def _iter_org_repositories(self, org: str) -> AsyncIterator[dict[str, Any]]:
        """Iterate an owner's non-archived repositories (forks included).

        Despite the historical name, this works for both organizations
        and personal user accounts: the correct GraphQL root
        (``organization`` vs ``user``) is resolved once at runtime via
        :meth:`_resolve_owner_root`, so org-wide *read* operations
        (``status``, ``blocked``, and ``close``'s similar-PR scan) no
        longer fail with a ``NOT_FOUND`` error when handed a user
        account.  This mirrors the owner-aware enumeration the merge
        path already uses via :meth:`_iter_owner_repositories`.

        Unlike :meth:`_iter_owner_repositories`, fork repositories are
        *not* skipped here: the read-only reporting commands want a
        complete picture of every repository the owner has, whereas the
        bulk-merge path deliberately excludes forks.  The progress total
        is published from the first page's ``totalCount``, which counts
        *all* of the owner's repositories — including the archived repos
        this iterator filters out — so the denominator is approximate and
        the percentage can finish below 100%.  It is close enough for a
        progress bar.
        """
        async for repo in self._iter_owner_repositories(org, skip_forks=False):
            yield repo

    async def _iter_org_repositories_with_open_prs(
        self, org: str
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Iterate organization repositories only; PRs are fetched per repository.

        This reduces per-query node pressure. Consumers should fetch PR pages
        using _fetch_repo_prs_first_page and _iter_repo_open_prs_pages.
        """
        async for repo in self._iter_org_repositories(org):
            yield repo

    async def _resolve_owner_root(self, owner: str) -> tuple[str, str]:
        """Resolve whether ``owner`` is an organization or a user account.

        Probes the ``organization(login:)`` repositories query first; if
        that root resolves to null (the login is not an org), falls back
        to the ``user(login:)`` query.  The verdict is cached so repeated
        pagination pages do not re-probe.

        Returns:
            A ``(root_key, query)`` tuple where ``root_key`` is the
            top-level GraphQL field (``"organization"`` or ``"user"``)
            and ``query`` is the matching query document.
        """
        cached = self._owner_root_cache.get(owner)
        if cached is not None:
            return cached

        variables = {"org": owner, "reposCursor": None}
        # GitHub answers ``organization(login:)`` for a *user* login with
        # ``data.organization = null`` AND a NOT_FOUND error in the
        # ``errors`` array ("Could not resolve to an Organization ...").
        # ``GitHubAsync.graphql`` raises ``GraphQLError`` on any non-transient
        # ``errors`` payload, so the null-organization case never reaches the
        # ``data`` check below — it surfaces as an exception instead.  Treat a
        # NOT_FOUND-on-organization error as "not an org" and fall back to the
        # user root; re-raise anything else (e.g. genuine transport or schema
        # errors).
        try:
            data = await self._api.graphql(ORG_REPOS_ONLY, variables)
            is_org = (data or {}).get("organization") is not None
        except GraphQLError as exc:
            if not self._is_not_an_organization_error(exc):
                raise
            is_org = False

        if is_org:
            resolved = ("organization", ORG_REPOS_ONLY)
        else:
            resolved = ("user", USER_REPOS_ONLY)
        self._owner_root_cache[owner] = resolved
        return resolved

    @staticmethod
    def _is_not_an_organization_error(exc: GraphQLError) -> bool:
        """Return True when a GraphQL error means "login is not an org".

        ``GitHubAsync.graphql`` raises ``GraphQLError(json.dumps(errors))``,
        so the exception text is the structured GraphQL ``errors`` array.
        Parse it and match only a ``NOT_FOUND`` error reported against the
        top-level ``organization`` field (``path == ["organization"]``),
        so an unrelated ``NOT_FOUND`` on a nested field that merely mentions
        "organization" cannot trigger the user-account fallback.

        If the payload is not the expected JSON shape (e.g. the
        retries-exhausted sentinel), fall back to the conservative
        substring heuristic so a genuinely-missing org still falls back
        rather than aborting.
        """
        try:
            errors = json.loads(str(exc))
        except (ValueError, TypeError):
            errors = None

        if isinstance(errors, list):
            for error in errors:
                if not isinstance(error, dict):
                    continue
                if str(error.get("type", "")).upper() != "NOT_FOUND":
                    continue
                path = error.get("path")
                if path == ["organization"]:
                    return True
            return False

        # Payload was not the structured errors array; degrade gracefully.
        msg = str(exc).lower()
        return "not_found" in msg and "organization" in msg

    async def _iter_owner_repositories(
        self, owner: str, *, skip_forks: bool = True
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate an owner's non-archived repositories.

        Works for both organizations and personal user accounts: the
        correct GraphQL root is resolved once via
        :meth:`_resolve_owner_root` and reused for every page.

        Archived repositories are always skipped.  Fork repositories are
        skipped by default (``skip_forks=True``): owner-wide bulk merges
        target the owner's own automation PRs, not PRs on mirrored
        forks.  Read-only reporting paths pass ``skip_forks=False`` to
        include forks for a complete picture.  The progress total is
        published from the first page's ``totalCount``, which counts
        *all* of the owner's repositories — including the archived and
        (when filtered) fork repos this iterator skips — so the
        denominator is approximate and the percentage can finish below
        100%.  It is close enough for a progress bar.
        """
        root_key, query = await self._resolve_owner_root(owner)
        cursor: str | None = None
        total_set = False
        while True:
            variables = {"org": owner, "reposCursor": cursor}
            data = await self._api.graphql(query, variables)
            root = (data or {}).get(root_key) or {}
            repos = root.get("repositories") or {}

            if not total_set:
                total_count = repos.get("totalCount")
                if total_count is not None and self._progress:
                    self._progress.update_total_repositories(total_count)
                total_set = True

            nodes: list[dict[str, Any]] = repos.get("nodes", []) or []
            for repo in nodes:
                if repo.get("isArchived"):
                    continue
                if skip_forks and repo.get("isFork"):
                    continue
                yield repo

            page_info = repos.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

    async def _iter_repo_open_prs_pages(
        self, owner: str, name: str, cursor: str | None
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Iterate additional pages of open PRs for a specific repository.
        """
        prs_cursor = cursor
        while prs_cursor:
            prs_size = DEFAULT_PRS_PAGE_SIZE
            files_size = DEFAULT_FILES_PAGE_SIZE
            comments_size = DEFAULT_COMMENTS_PAGE_SIZE
            contexts_size = DEFAULT_CONTEXTS_PAGE_SIZE
            if getattr(self, "_rate_limited", False):
                prs_size = max(10, prs_size // 2)
                files_size = max(20, files_size // 2)
                comments_size = max(5, comments_size // 2)
                contexts_size = max(10, contexts_size // 2)
            variables = {
                "owner": owner,
                "name": name,
                "prsCursor": prs_cursor,
                "prsPageSize": prs_size,
                "filesPageSize": files_size,
                "commentsPageSize": comments_size,
                "contextsPageSize": contexts_size,
            }
            async with self._page_semaphore:
                data = await self._api.graphql(REPO_OPEN_PRS_PAGE, variables)
            repo = (data or {}).get("repository") or {}
            prs = repo.get("pullRequests") or {}
            nodes: list[dict[str, Any]] = prs.get("nodes", []) or []
            for pr in nodes:
                yield pr

            page_info = prs.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            prs_cursor = page_info.get("endCursor")

    async def _fetch_repo_prs_first_page(
        self, owner: str, name: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        Fetch the first page of open PRs for a repository using GraphQL.
        Returns a tuple of (nodes, pageInfo).
        """
        prs_size = DEFAULT_PRS_PAGE_SIZE
        files_size = DEFAULT_FILES_PAGE_SIZE
        comments_size = DEFAULT_COMMENTS_PAGE_SIZE
        contexts_size = DEFAULT_CONTEXTS_PAGE_SIZE
        if getattr(self, "_rate_limited", False):
            prs_size = max(10, prs_size // 2)
            files_size = max(20, files_size // 2)
            comments_size = max(5, comments_size // 2)
            contexts_size = max(10, contexts_size // 2)
        variables = {
            "owner": owner,
            "name": name,
            "prsCursor": None,
            "prsPageSize": prs_size,
            "filesPageSize": files_size,
            "commentsPageSize": comments_size,
            "contextsPageSize": contexts_size,
        }
        async with self._page_semaphore:
            data = await self._api.graphql(REPO_OPEN_PRS_PAGE, variables)
        repo = (data or {}).get("repository") or {}
        prs = repo.get("pullRequests") or {}
        nodes: list[dict[str, Any]] = prs.get("nodes", []) or []
        page_info: dict[str, Any] = prs.get("pageInfo") or {}
        return nodes, page_info

    # -------------------------------
    # PR analysis and model mappings
    # -------------------------------

    async def _analyze_pr_node(
        self, repo_full_name: str, pr: dict[str, Any], include_drafts: bool = False
    ) -> UnmergeablePR | None:
        """
        Analyze a PR GraphQL node and produce UnmergeablePR if any blocking reasons
        are detected. Returns None if mergeable or if insufficient data.

        Args:
            repo_full_name: The full name of the repository (owner/repo).
            pr: The PR GraphQL node data.
            include_drafts: If True, include draft PRs in the results. If False (default),
                          return None for PRs that are only blocked due to draft status.

        This applies code-owners level bypass logic by default (matching merge command behavior).
        PRs that can be merged with standard permissions are not reported as blocked.
        """
        if self._progress:
            try:
                self._progress.analyze_pr(pr.get("number", 0), repo_full_name)
            except Exception:
                # Progress display is best-effort; ignore UI errors.
                pass

        reasons: list[UnmergeableReason] = []

        # Draft status
        if pr.get("isDraft") is True:
            reasons.append(
                UnmergeableReason(
                    type="draft",
                    description="Pull request is in draft state",
                )
            )

        # Mergeability
        mergeable = (
            pr.get("mergeable") or ""
        ).upper()  # MERGEABLE | CONFLICTING | UNKNOWN
        merge_state = (
            pr.get("mergeStateStatus") or ""
        ).lower()  # clean, behind, blocked, draft, dirty, unknown

        if mergeable == "CONFLICTING" or merge_state == "dirty":
            reasons.append(
                UnmergeableReason(
                    type="merge_conflict",
                    description="Pull request has merge conflicts",
                    details="Branch cannot be automatically merged due to conflicts",
                )
            )

        if merge_state == "behind":
            reasons.append(
                UnmergeableReason(
                    type="behind_base",
                    description="Pull request is behind the base branch",
                    details="Branch needs to be updated with latest changes",
                )
            )

        # Status check rollup
        failing_checks = self._extract_failing_checks(pr)
        if failing_checks:
            reasons.append(
                UnmergeableReason(
                    type="failing_checks",
                    description="Required status checks are failing",
                    details=f"Failing checks: {', '.join(sorted(set(failing_checks)))}",
                )
            )

        if not reasons:
            return None

        # Filter out PRs that are only blocked due to draft status if include_drafts is False
        if not include_drafts:
            # Check if draft is the only blocking reason
            if len(reasons) == 1 and reasons[0].type == "draft":
                return None
            # Remove draft reason from the list if there are other blocking reasons
            reasons = [r for r in reasons if r.type != "draft"]
            # If after filtering there are no reasons left, return None
            if not reasons:
                return None

        copilot_comments = self._extract_copilot_comments(pr)
        # File change extraction not required for UnmergeablePR summary here

        return UnmergeablePR(
            repository=repo_full_name,
            pr_number=int(pr.get("number", 0)),
            title=pr.get("title") or "",
            author=canonical_bot_login(
                (pr.get("author") or {}).get("login"),
                (pr.get("author") or {}).get("__typename"),
            ),
            url=pr.get("url") or "",
            reasons=reasons,
            copilot_comments_count=len(copilot_comments),
            copilot_comments=copilot_comments,
            created_at=pr.get("createdAt") or "",
            updated_at=pr.get("updatedAt") or "",
        )

    def to_pull_request_info(
        self, repo_full_name: str, pr: dict[str, Any]
    ) -> PullRequestInfo:
        """
        Convert a PR GraphQL node to PullRequestInfo (for merge workflows).
        """
        files = self._extract_file_changes(pr)
        reviews = self._extract_reviews(pr)

        # Debug logging to see actual GraphQL values
        mergeable_raw = pr.get("mergeable")
        merge_state_raw = pr.get("mergeStateStatus")
        self.log.debug(
            f"GraphQL raw values for PR {pr.get('number', 'unknown')}: "
            f"mergeable='{mergeable_raw}', mergeStateStatus='{merge_state_raw}'"
        )

        return PullRequestInfo(
            number=int(pr.get("number", 0)),
            node_id=pr.get("id"),  # GraphQL node ID for mutations
            title=pr.get("title") or "",
            body=(pr.get("body") or None),
            author=canonical_bot_login(
                (pr.get("author") or {}).get("login"),
                (pr.get("author") or {}).get("__typename"),
            ),
            head_sha=pr.get("headRefOid") or "",
            base_branch=pr.get("baseRefName") or "",
            head_branch=pr.get("headRefName") or "",
            state="open",  # GraphQL query filters for OPEN PRs only, so all results are open
            mergeable=self._map_mergeable_enum(pr.get("mergeable")),
            mergeable_state=self._safe_get_merge_state(pr.get("mergeStateStatus")),
            behind_by=None,  # Not included in GraphQL; could be computed if needed
            files_changed=files,
            repository_full_name=repo_full_name,
            html_url=pr.get("url") or "",
            reviews=reviews,
            # Populate head/base repo identity from the GraphQL
            # ``headRepository`` / ``baseRepository`` fields so the
            # signature-preserving local-rebase path can tell
            # whether the PR is from a fork (and which remote to
            # push to).  Without these, ``rebase.local_rebase_pr()``
            # fails closed to avoid pushing to the wrong repository.
            # GraphQL returns the HTTPS URL via ``url`` (without the
            # ``.git`` suffix), so we synthesise the canonical
            # ``clone_url`` form for parity with REST.
            head_repo_full_name=_str_or_none(
                (pr.get("headRepository") or {}).get("nameWithOwner")
            ),
            head_repo_clone_url=_clone_url_with_git_suffix(
                (pr.get("headRepository") or {}).get("url")
            ),
            base_repo_full_name=_str_or_none(
                (pr.get("baseRepository") or {}).get("nameWithOwner")
            ),
            base_repo_clone_url=_clone_url_with_git_suffix(
                (pr.get("baseRepository") or {}).get("url")
            ),
            is_fork=_bool_or_none((pr.get("headRepository") or {}).get("isFork")),
        )

    async def find_similar_prs(
        self,
        org: str,
        source_pr: PullRequestInfo,
        comparator,
        *,
        only_automation: bool,
    ) -> list[tuple[PullRequestInfo, ComparisonResult]]:
        """
        Find PRs across an organization that are similar to the provided source PR.

        This integrates progress updates:
        - Updates total repositories
        - Starts/completes repository sections
        - Increments PR analysis count per PR
        - Tracks similar PRs found

        Args:
            org: Organization login.
            source_pr: The PR to compare against.
            comparator: Provides compare_pull_requests(source, target) -> ComparisonResult.
            only_automation: If True, restrict candidates to automation PRs; otherwise, same author as source.

        Returns:
            List of (PullRequestInfo, ComparisonResult) tuples for similar PRs.
        """
        results: list[tuple[PullRequestInfo, ComparisonResult]] = []

        # Repo total is set automatically by _iter_org_repositories
        # on the first GraphQL page via totalCount.
        async for repo in self._iter_org_repositories_with_open_prs(org):
            repo_full_name = repo.get("nameWithOwner") or ""
            if not repo_full_name or "/" not in repo_full_name:
                if self._progress:
                    self._progress.add_error()
                continue

            if self._progress:
                self._progress.start_repository(repo_full_name)
                self._progress.update_operation(
                    f"Getting open PRs from {repo_full_name}"
                )

            owner_n, name_n = repo_full_name.split("/", 1)
            first_nodes, page_info = await self._fetch_repo_prs_first_page(
                owner_n, name_n
            )
            prs = list(first_nodes)
            has_next = bool(page_info.get("hasNextPage"))
            end_cursor = page_info.get("endCursor") or None

            # Include additional pages if present
            if has_next:
                async for pr_node in self._iter_repo_open_prs_pages(
                    owner_n, name_n, end_cursor
                ):
                    prs.append(pr_node)

            matching_prs_in_repo: list[tuple[PullRequestInfo, ComparisonResult]] = []

            for pr_node in prs:
                target_pr = self.to_pull_request_info(repo_full_name, pr_node)

                # Skip the source PR itself
                if (
                    target_pr.number == source_pr.number
                    and target_pr.repository_full_name == source_pr.repository_full_name
                ):
                    continue

                # Candidate filtering
                if only_automation:
                    is_auto = any(
                        bot in (target_pr.author or "").lower()
                        for bot in [
                            "dependabot",
                            "renovate",
                            "pre-commit",
                            "github-actions",
                            "bot",
                        ]
                    )
                    if not is_auto:
                        continue
                else:
                    if (target_pr.author or "") != (source_pr.author or ""):
                        continue

                if self._progress:
                    self._progress.analyze_pr(target_pr.number, repo_full_name)

                comparison: ComparisonResult = comparator.compare_pull_requests(
                    source_pr, target_pr, only_automation
                )

                # Debug matching output
                if self._debug_matching:
                    from rich.console import Console

                    debug_console = Console()
                    debug_console.print(
                        f"\n🔍 [bold]Comparing {repo_full_name}#{target_pr.number}[/bold]"
                    )
                    debug_console.print(f"   Title: {target_pr.title}")
                    debug_console.print(f"   Author: {target_pr.author}")

                    # Show individual scores
                    title_score = comparator._compare_titles(
                        source_pr.title, target_pr.title
                    )
                    body_score = comparator._compare_bodies(
                        source_pr.body, target_pr.body
                    )
                    files_score = comparator._compare_file_changes(
                        source_pr.files_changed, target_pr.files_changed
                    )
                    author_score = (
                        1.0
                        if comparator._normalize_author(source_pr.author)
                        == comparator._normalize_author(target_pr.author)
                        else 0.0
                    )

                    debug_console.print(f"   📝 Title score: {title_score:.3f}")
                    debug_console.print(f"   📄 Body score: {body_score:.3f}")
                    debug_console.print(f"   📁 Files score: {files_score:.3f}")
                    debug_console.print(f"   👤 Author score: {author_score:.3f}")
                    debug_console.print(
                        f"   🎯 Overall: {comparison.confidence_score:.3f} (threshold: 0.8)"
                    )

                    if comparison.is_similar:
                        debug_console.print(
                            f"   ✅ [green]SIMILAR[/green] - {', '.join(comparison.reasons)}"
                        )
                    else:
                        debug_console.print("   ❌ [red]NOT SIMILAR[/red]")

                        # Show why it failed
                        if title_score == 0:
                            source_pkg = comparator._extract_package_name(
                                source_pr.title
                            )
                            target_pkg = comparator._extract_package_name(
                                target_pr.title
                            )
                            debug_console.print(
                                f"      📦 Source package: '{source_pkg}'"
                            )
                            debug_console.print(
                                f"      📦 Target package: '{target_pkg}'"
                            )

                        if body_score < 0.6:
                            if target_pr.body is None:
                                debug_console.print("      ⚠️ Target PR has no body")
                            elif source_pr.body is None:
                                debug_console.print("      ⚠️ Source PR has no body")
                            else:
                                debug_console.print(
                                    f"      📄 Body comparison failed (score: {body_score:.3f})"
                                )

                if comparison.is_similar:
                    matching_prs_in_repo.append((target_pr, comparison))
                    if self._progress:
                        # We can reuse 'found_similar_pr' if using MergeProgressTracker,
                        # otherwise this call will be a no-op for ProgressTracker.
                        try:
                            self._progress.found_similar_pr()  # type: ignore[attr-defined]
                        except Exception:
                            # No-op when the tracker lacks this method or
                            # the display update fails; counting is
                            # cosmetic only.
                            pass

            results.extend(matching_prs_in_repo)

            if self._progress:
                self._progress.complete_repository(len(matching_prs_in_repo))

        return results

    async def _collect_repo_open_prs(
        self,
        owner: str,
        repo: str,
        *,
        only_automation: bool,
    ) -> list[PullRequestInfo]:
        """Fetch + convert + filter the open PRs of a single repository.

        This is the shared body used by both :meth:`fetch_repo_open_prs`
        (single-repository bulk merge) and :meth:`fetch_owner_open_prs`
        (owner-wide bulk merge), so the GraphQL-node-to-PullRequestInfo
        conversion and automation filtering live in exactly one place.

        Unlike its callers it does not emit ``start_repository`` /
        ``complete_repository`` progress events; the caller owns the
        per-repository progress lifecycle.
        """
        repo_full_name = f"{owner}/{repo}"

        first_nodes, page_info = await self._fetch_repo_prs_first_page(owner, repo)
        pr_nodes = list(first_nodes)
        has_next = bool(page_info.get("hasNextPage"))
        end_cursor = page_info.get("endCursor") or None

        # Fetch additional pages if present
        if has_next:
            async for pr_node in self._iter_repo_open_prs_pages(
                owner, repo, end_cursor
            ):
                pr_nodes.append(pr_node)

        results: list[PullRequestInfo] = []
        for pr_node in pr_nodes:
            pr_info = self.to_pull_request_info(repo_full_name, pr_node)

            if self._progress:
                self._progress.analyze_pr(pr_info.number, repo_full_name)

            # Filter by automation author if requested
            if only_automation and not self._is_automation_author(pr_info.author):
                continue

            results.append(pr_info)

        return results

    async def fetch_repo_open_prs(
        self,
        owner: str,
        repo: str,
        *,
        only_automation: bool = True,
    ) -> list[PullRequestInfo]:
        """
        Fetch all open PRs for a specific repository.

        This is used for repository-scoped bulk operations where we don't
        need to scan across an organization. It reuses the same GraphQL
        pagination infrastructure used by find_similar_prs.

        Args:
            owner: Repository owner (user or organization).
            repo: Repository name.
            only_automation: If True, only return PRs from automation tools.
                           If False, return all open PRs.

        Returns:
            List of PullRequestInfo for matching open PRs.
        """
        repo_full_name = f"{owner}/{repo}"

        if self._progress:
            self._progress.start_repository(repo_full_name)
            self._progress.update_operation(f"Fetching open PRs from {repo_full_name}")

        results = await self._collect_repo_open_prs(
            owner, repo, only_automation=only_automation
        )

        if self._progress:
            self._progress.complete_repository(len(results))

        return results

    async def fetch_owner_open_prs(
        self,
        owner: str,
        *,
        only_automation: bool = True,
    ) -> tuple[list[PullRequestInfo], list[str]]:
        """Fetch open PRs across every in-scope repository of an owner.

        Enumerates the owner's non-archived, non-fork repositories
        (organization or user account, resolved at runtime) and fetches
        their open PRs with bounded per-repository concurrency, reusing
        the same per-repo body as :meth:`fetch_repo_open_prs`.

        Per-repository failures are isolated: a transient error scanning
        one repository is recorded and enumeration continues, so a single
        bad repository never aborts an owner-wide run.  Global
        rate-limit / secondary-rate-limit errors are *not* swallowed —
        they propagate so the API layer's backoff governs the whole run.

        Args:
            owner: The organization or user login.
            only_automation: If True, only return PRs from automation tools.

        Returns:
            A ``(prs, errors)`` tuple: the collected PRs across all
            repositories, and a list of human-readable per-repository
            error strings (empty when everything succeeded).
        """

        async def process_repo(
            repo_node: dict[str, Any],
        ) -> tuple[list[PullRequestInfo], list[str]]:
            async with self._repo_semaphore:
                repo_full_name = repo_node.get("nameWithOwner", "unknown/unknown")
                if self._progress:
                    self._progress.start_repository(repo_full_name)
                try:
                    repo_owner, repo_name = self._split_owner_repo(repo_full_name)
                    prs = await self._collect_repo_open_prs(
                        repo_owner, repo_name, only_automation=only_automation
                    )
                    if self._progress:
                        self._progress.complete_repository(len(prs))
                    return prs, []
                except (RateLimitError, SecondaryRateLimitError):
                    # Global rate limiting must abort the whole run rather
                    # than be recorded as a per-repo error and skipped.
                    raise
                except Exception as e:
                    if self._progress:
                        # Count the error *and* mark the repository as
                        # processed.  Without the ``complete_repository``
                        # call the per-repo counter would never advance
                        # for a failed repo, leaving the progress fraction
                        # stuck below 100% and the "Scanning <repo>"
                        # label stale once the run finishes.  Passing 0
                        # adds nothing to the unmergeable tally.
                        self._progress.add_error()
                        self._progress.complete_repository(0)
                    return [], [f"Error scanning repository {repo_full_name}: {e}"]

        all_prs: list[PullRequestInfo] = []
        errors: list[str] = []

        # Bounded producer/consumer pipeline.  A fixed pool of workers
        # (sized to the repo-concurrency limit) drains repository nodes
        # from a queue fed by the paginated iterator.  This caps in-flight
        # work — both pending tasks and buffered nodes — instead of
        # materialising one task per repository up front, which matters
        # for owners with thousands of repositories.  A propagated
        # rate-limit error from any worker tears the whole pipeline down
        # (see the teardown below), preserving the global-throttle
        # semantics of aborting the run rather than skipping repos.
        worker_count = max(1, self._max_repo_tasks)
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=worker_count * 2
        )

        async def producer() -> None:
            async for repo in self._iter_owner_repositories(owner):
                await queue.put(repo)
            # One sentinel per worker so each terminates once the backlog
            # drains.
            for _ in range(worker_count):
                await queue.put(None)

        async def worker() -> None:
            while True:
                repo = await queue.get()
                if repo is None:
                    return
                repo_prs, repo_errors = await process_repo(repo)
                # Safe to mutate the shared lists without a lock: asyncio
                # is single-threaded and ``extend`` does not await.
                all_prs.extend(repo_prs)
                errors.extend(repo_errors)

        producer_task = asyncio.create_task(producer())
        worker_tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
        pipeline = [producer_task, *worker_tasks]
        try:
            # No return_exceptions: a propagated rate-limit error aborts
            # the pipeline (the desired behaviour for global throttling).
            await asyncio.gather(*pipeline)
        except BaseException:
            # Tear the pipeline down so no worker is left blocked on the
            # queue, then re-raise the original (e.g. rate-limit) error.
            for task in pipeline:
                task.cancel()
            await asyncio.gather(*pipeline, return_exceptions=True)
            raise

        return all_prs, errors

    async def get_branch_protection_settings(
        self, owner: str, repo: str, branch: str = "main"
    ) -> dict[str, Any] | None:
        """
        Get branch protection settings for a repository branch.

        Args:
            owner: Repository owner
            repo: Repository name
            branch: Branch name (defaults to "main")

        Returns:
            Branch protection settings dict, or None if no protection or error
        """
        cache_key = f"{owner}/{repo}:{branch}"

        # Check cache first
        if cache_key in self._branch_protection_cache:
            return self._branch_protection_cache[cache_key]

        if not self._api:
            return None

        try:
            variables = {"owner": owner, "name": repo, "branch": f"refs/heads/{branch}"}

            response = await self._api.graphql(GET_BRANCH_PROTECTION, variables)

            # Debug: Log the actual response structure
            self.log.debug(f"GraphQL response for {owner}/{repo}: {response}")

            repo_data = response.get("repository")
            if not repo_data:
                self.log.debug(f"No repository data for {owner}/{repo}")
                self._branch_protection_cache[cache_key] = None
                return None

            # Start with repository-level merge settings
            protection = {
                "allowsMergeCommits": repo_data.get("mergeCommitAllowed", True),
                "allowsSquashMerges": repo_data.get("squashMergeAllowed", True),
                "allowsRebaseMerges": repo_data.get("rebaseMergeAllowed", True),
            }

            # Add branch protection rule settings if they exist
            ref_data = repo_data.get("ref")
            if ref_data:
                branch_protection = ref_data.get("branchProtectionRule")
                if branch_protection:
                    protection.update(branch_protection)

            self._branch_protection_cache[cache_key] = protection

            self.log.debug(
                f"Branch protection for {owner}/{repo}:{branch}: "
                f"requiresLinearHistory={protection.get('requiresLinearHistory', False)}, "
                f"allowsMergeCommits={protection.get('allowsMergeCommits')}, "
                f"allowsSquashMerges={protection.get('allowsSquashMerges')}, "
                f"allowsRebaseMerges={protection.get('allowsRebaseMerges')}"
            )

            return protection

        except Exception as e:
            error_str = str(e)
            # Check for permission errors
            if (
                "FORBIDDEN" in error_str
                and "Resource not accessible by personal access token" in error_str
            ):
                self.log.debug(
                    f"Cannot access branch protection for {owner}/{repo}:{branch}: Missing 'Administration: Read-only' permission. "
                    f"For fine-grained tokens, enable 'Administration: Read-only'. For classic tokens, ensure 'repo' scope is enabled."
                )
            else:
                self.log.warning(
                    f"Failed to get branch protection for {owner}/{repo}:{branch}: {e}"
                )
            # Cache the None result to avoid repeated failures
            self._branch_protection_cache[cache_key] = None
            return None

    def determine_merge_method(
        self, branch_protection: dict[str, Any] | None, default_method: str = "merge"
    ) -> str:
        """
        Determine the appropriate merge method based on branch protection settings.

        Args:
            branch_protection: Branch protection settings from GraphQL
            default_method: Default merge method to use if no restrictions

        Returns:
            Recommended merge method: "merge", "squash", or "rebase"
        """
        if not branch_protection:
            return default_method

        # If linear history is required, only rebase merge is allowed
        if branch_protection.get("requiresLinearHistory", False):
            if branch_protection.get("allowsRebaseMerges", True):
                return "rebase"
            else:
                self.log.warning(
                    "Repository requires linear history but doesn't allow rebase merges"
                )
                return default_method

        # Otherwise, prefer the default method if it's allowed
        if default_method == "merge" and branch_protection.get(
            "allowsMergeCommits", True
        ):
            return "merge"
        elif default_method == "squash" and branch_protection.get(
            "allowsSquashMerges", True
        ):
            return "squash"
        elif default_method == "rebase" and branch_protection.get(
            "allowsRebaseMerges", True
        ):
            return "rebase"

        # Fall back to first available method
        if branch_protection.get("allowsMergeCommits", True):
            return "merge"
        elif branch_protection.get("allowsSquashMerges", True):
            return "squash"
        elif branch_protection.get("allowsRebaseMerges", True):
            return "rebase"

        self.log.warning(
            f"No merge methods allowed by branch protection: {branch_protection}"
        )
        return default_method

    # -----------------
    # Helper methods
    # -----------------

    def _split_owner_repo(self, full_name: str) -> tuple[str, str]:
        try:
            owner, name = full_name.split("/", 1)
            return owner, name
        except Exception:
            return "unknown", "unknown"

    def _map_mergeable_enum(self, value: str | None) -> bool | None:
        # GraphQL mergeable: "MERGEABLE" | "CONFLICTING" | "UNKNOWN"
        self.log.debug(f"Mapping mergeable enum: '{value}'")
        if not value:
            self.log.debug("mergeable value is falsy (None, empty, etc.)")
            return None
        v = value.upper()
        if v == "MERGEABLE":
            self.log.debug("Mapped to True (mergeable)")
            return True
        if v == "CONFLICTING":
            self.log.debug("Mapped to False (conflicting)")
            return False
        if v == "UNKNOWN":
            # GitHub is still calculating - treat as potentially mergeable
            self.log.debug("Mapped UNKNOWN to None (still calculating)")
            return None
        # Log unexpected values for debugging
        self.log.warning(f"Unexpected mergeable value from GraphQL: {value}")
        return None

    def _safe_get_merge_state(self, merge_state_status: str | None) -> str | None:
        """Safely extract and normalize mergeStateStatus from GraphQL."""
        if not merge_state_status:
            # Log when we get null/missing mergeStateStatus for debugging
            self.log.debug("GraphQL mergeStateStatus is null or missing")
            return None

        normalized = merge_state_status.lower().strip()
        if not normalized:
            self.log.debug("GraphQL mergeStateStatus is empty string")
            return None

        # Valid states: clean, dirty, blocked, behind, draft, unstable, unknown
        valid_states = {
            "clean",
            "dirty",
            "blocked",
            "behind",
            "draft",
            "unstable",
            "unknown",
        }
        if normalized not in valid_states:
            self.log.warning(
                f"Unexpected mergeStateStatus from GraphQL: {merge_state_status}"
            )

        return normalized

    def _extract_file_changes(self, pr: dict[str, Any]) -> list[FileChange]:
        files = (pr.get("files") or {}).get("nodes", []) or []
        result: list[FileChange] = []
        for f in files:
            additions = int(f.get("additions") or 0)
            deletions = int(f.get("deletions") or 0)
            result.append(
                FileChange(
                    filename=f.get("path") or "",
                    additions=additions,
                    deletions=deletions,
                    changes=additions + deletions,
                    status="modified",  # GraphQL 'files' doesn't include a status; best-effort
                )
            )
        return result

    def _extract_reviews(self, pr: dict[str, Any]) -> list[ReviewInfo]:
        """Extract PR reviews from GraphQL node."""
        reviews = (pr.get("reviews") or {}).get("nodes", []) or []
        result: list[ReviewInfo] = []

        for review in reviews:
            author = (review.get("author") or {}).get("login") or "unknown"
            result.append(
                ReviewInfo(
                    # NOTE: GraphQL returns string node IDs (e.g., "PRR_kwDOGBtQpc4-u-zD")
                    # NOT numeric IDs. Do not convert to int() - it will cause runtime errors.
                    id=review.get("id", ""),
                    user=author,
                    state=review.get("state") or "",
                    submitted_at=review.get("createdAt") or "",
                    body=review.get("body"),
                )
            )
        return result

    def _extract_copilot_comments(self, pr: dict[str, Any]) -> list[CopilotComment]:
        comments = (pr.get("comments") or {}).get("nodes", []) or []
        result: list[CopilotComment] = []
        for c in comments:
            author = ((c.get("author") or {}).get("login") or "").lower()
            if author in ("github-copilot[bot]", "copilot"):
                result.append(
                    CopilotComment(
                        id=0,  # GraphQL doesn't provide numeric IDs in this selection; not critical for reporting
                        body=c.get("body") or "",
                        created_at=c.get("createdAt") or "",
                        state="open",
                    )
                )
        return result

    def _extract_failing_checks(self, pr: dict[str, Any]) -> list[str]:
        """
        Extract failing checks from the statusCheckRollup on the latest commit.
        """
        failing: list[str] = []

        commits = (pr.get("commits") or {}).get("nodes", []) or []
        if not commits:
            return failing

        commit = (commits[0] or {}).get("commit") or {}
        rollup = commit.get("statusCheckRollup") or {}
        contexts = (rollup.get("contexts") or {}).get("nodes", []) or []

        for ctx in contexts:
            typ = ctx.get("__typename")
            if typ == "CheckRun":
                # Consider failure, cancelled, or timed_out as failing
                conclusion = (ctx.get("conclusion") or "").lower()
                if conclusion in ("failure", "cancelled", "timed_out"):
                    name = ctx.get("name") or ""
                    if name:
                        failing.append(name)
            elif typ == "StatusContext":
                state = (ctx.get("state") or "").upper()
                if state in ("FAILURE", "ERROR"):
                    name = ctx.get("context") or ""
                    if name:
                        failing.append(name)

        return failing

    async def gather_organization_status(self, org: str) -> OrganizationStatus:
        """
        Gather repository status information for an organization.

        This collects:
        - Latest tags and releases
        - Open and merged pull requests
        - PRs affecting action files or workflows

        Returns:
            OrganizationStatus with aggregated data and errors.
        """
        errors: list[str] = []
        repository_statuses: list[RepositoryStatus] = []
        total_repositories = 0
        scanned_repositories = 0

        # Process repositories with bounded parallelism
        # (repo total is set automatically by _iter_org_repositories
        # on the first GraphQL page via totalCount)
        async def process_repo_status(
            repo_node: dict[str, Any],
        ) -> tuple[RepositoryStatus | None, int, list[str]]:
            async with self._repo_semaphore:
                repo_errors: list[str] = []
                repo_full_name = repo_node.get("nameWithOwner", "unknown/unknown")
                if self._progress:
                    self._progress.start_repository(repo_full_name)
                try:
                    owner, name = self._split_owner_repo(repo_full_name)

                    # Get tags and releases
                    latest_tag, tag_date = await self._get_latest_tag(owner, name)
                    latest_release, release_date = await self._get_latest_release(
                        owner, name
                    )

                    # Determine status icon
                    status_icon = self._determine_status_icon(
                        latest_tag, latest_release, tag_date, release_date
                    )

                    # Get PR statistics
                    pr_stats = await self._gather_pr_statistics(
                        owner, name, tag_date or release_date
                    )

                    repo_status = RepositoryStatus(
                        repository_name=name,
                        latest_tag=latest_tag,
                        latest_release=latest_release,
                        tag_date=tag_date,
                        release_date=release_date,
                        status_icon=status_icon,
                        **pr_stats,
                    )

                    if self._progress:
                        self._progress.complete_repository(0)

                    return repo_status, 1, repo_errors
                except Exception as e:
                    if self._progress:
                        self._progress.add_error()
                    return None, 0, [f"Error scanning repository {repo_full_name}: {e}"]

        tasks: list[asyncio.Task[Any]] = []
        async for repo in self._iter_org_repositories(org):
            tasks.append(asyncio.create_task(process_repo_status(repo)))

        total_repositories = len(tasks)

        if tasks:
            results = await asyncio.gather(*tasks)
            for repo_status, scanned_inc, repo_errors in results:
                if repo_status:
                    repository_statuses.append(repo_status)
                scanned_repositories += scanned_inc
                if repo_errors:
                    errors.extend(repo_errors)

        return OrganizationStatus(
            organization=org,
            total_repositories=total_repositories,
            scanned_repositories=scanned_repositories,
            repository_statuses=repository_statuses,
            scan_timestamp=datetime.now().isoformat(),
            errors=errors,
        )

    async def _get_latest_tag(
        self, owner: str, name: str
    ) -> tuple[str | None, str | None]:
        """Get the latest tag and its date."""
        try:
            # Use REST API to get tags
            tags_data = await self._api.get(
                f"/repos/{owner}/{name}/tags", params={"per_page": 1}
            )
            if isinstance(tags_data, list) and len(tags_data) > 0:
                tag_name = tags_data[0].get("name")
                # Get commit info for the tag to get date
                commit_sha = tags_data[0].get("commit", {}).get("sha")
                if commit_sha:
                    commit_data = await self._api.get(
                        f"/repos/{owner}/{name}/commits/{commit_sha}"
                    )
                    if isinstance(commit_data, dict):
                        commit_date = (
                            commit_data.get("commit", {})
                            .get("committer", {})
                            .get("date")
                        )
                        if commit_date:
                            # Convert ISO date to YYYY/MM/DD
                            date_obj = datetime.fromisoformat(
                                commit_date.replace("Z", "+00:00")
                            )
                            formatted_date = date_obj.strftime("%Y/%m/%d")
                            return tag_name, formatted_date
                return tag_name, None
            return None, None
        except Exception as e:
            self.log.debug(f"Error getting latest tag for {owner}/{name}: {e}")
            return None, None

    async def _get_latest_release(
        self, owner: str, name: str
    ) -> tuple[str | None, str | None]:
        """Get the latest production release (not draft/pre-release) and its date."""
        try:
            # Use REST API to get releases
            releases_data = await self._api.get(f"/repos/{owner}/{name}/releases")
            if isinstance(releases_data, list):
                # Find first non-draft, non-prerelease
                for release in releases_data:
                    if not release.get("draft") and not release.get("prerelease"):
                        release_name = release.get("tag_name") or release.get("name")
                        published_at = release.get("published_at")
                        if published_at:
                            # Convert ISO date to YYYY/MM/DD
                            date_obj = datetime.fromisoformat(
                                published_at.replace("Z", "+00:00")
                            )
                            formatted_date = date_obj.strftime("%Y/%m/%d")
                            return release_name, formatted_date
                        return release_name, None
            return None, None
        except Exception as e:
            self.log.debug(f"Error getting latest release for {owner}/{name}: {e}")
            return None, None

    def _determine_status_icon(
        self,
        latest_tag: str | None,
        latest_release: str | None,
        tag_date: str | None,
        release_date: str | None,
    ) -> str:
        """
        Determine status icon based on tag and release status.

        ✅ = Tag has matching release
        ⚠️ = Tag exists but no matching release
        ❌ = Release is more recent than tag (or no tag but has release)
        """
        if latest_tag and latest_release:
            # Check if tag and release match
            if latest_tag == latest_release:
                return "✅"
            # Check if release is more recent than tag
            if tag_date and release_date:
                try:
                    tag_dt = datetime.strptime(tag_date, "%Y/%m/%d")
                    release_dt = datetime.strptime(release_date, "%Y/%m/%d")
                    if release_dt > tag_dt:
                        return "❌"
                except Exception:
                    # Date parsing failed, fall through to warning icon
                    pass
            return "⚠️"
        elif latest_tag and not latest_release:
            return "⚠️"
        elif latest_release and not latest_tag:
            return "❌"
        else:
            return "❌"

    async def _gather_pr_statistics(
        self, owner: str, name: str, since_date: str | None
    ) -> dict[str, int]:
        """
        Gather PR statistics for a repository.

        Returns dict with counts for:
        - open_prs_human, open_prs_automation
        - merged_prs_human, merged_prs_automation
        - action_prs_human, action_prs_automation
        - workflow_prs_human, workflow_prs_automation
        """
        stats = {
            "open_prs_human": 0,
            "open_prs_automation": 0,
            "merged_prs_human": 0,
            "merged_prs_automation": 0,
            "action_prs_human": 0,
            "action_prs_automation": 0,
            "workflow_prs_human": 0,
            "workflow_prs_automation": 0,
        }

        try:
            # Get open PRs
            first_nodes, page_info = await self._fetch_repo_prs_first_page(owner, name)
            open_prs = list(first_nodes)

            # Get additional pages if needed
            if page_info.get("hasNextPage"):
                async for pr_node in self._iter_repo_open_prs_pages(
                    owner, name, page_info.get("endCursor")
                ):
                    open_prs.append(pr_node)

            # Count open PRs
            for pr in open_prs:
                author = (pr.get("author") or {}).get("login", "").lower()
                is_automation = self._is_automation_author(author)

                if is_automation:
                    stats["open_prs_automation"] += 1
                else:
                    stats["open_prs_human"] += 1

                # Check if PR affects actions or workflows
                files = (pr.get("files") or {}).get("nodes", []) or []
                affects_action = self._affects_action_files(files)
                affects_workflow = self._affects_workflow_files(files)

                if affects_action:
                    if is_automation:
                        stats["action_prs_automation"] += 1
                    else:
                        stats["action_prs_human"] += 1

                if affects_workflow:
                    if is_automation:
                        stats["workflow_prs_automation"] += 1
                    else:
                        stats["workflow_prs_human"] += 1

            # Get merged PRs since the last tag/release
            if since_date:
                merged_prs = await self._get_merged_prs_since(owner, name, since_date)
                for pr in merged_prs:
                    author = pr.get("user", {}).get("login", "").lower()
                    is_automation = self._is_automation_author(author)

                    if is_automation:
                        stats["merged_prs_automation"] += 1
                    else:
                        stats["merged_prs_human"] += 1

        except Exception as e:
            self.log.debug(f"Error gathering PR statistics for {owner}/{name}: {e}")

        return stats

    def _is_automation_author(self, author: str) -> bool:
        """Check if author is an automation tool.

        Delegates to the shared :func:`bot_identity.is_automation_author`
        so REST and GraphQL login forms are classified identically.
        """
        return is_automation_author(author)

    def _affects_action_files(self, files: list[dict[str, Any]]) -> bool:
        """Check if files include action definition or implementation files."""
        action_patterns = [
            "action.yaml",
            "action.yml",
            "Dockerfile",  # Action Dockerfiles
        ]

        for file_node in files:
            path = file_node.get("path", "")
            filename = path.split("/")[-1] if "/" in path else path

            # Check for action definition files
            if filename.lower() in [p.lower() for p in action_patterns]:
                return True

            # Check for JavaScript action files (in src/ or lib/ directories)
            if path.startswith(("src/", "lib/")) and path.endswith(".js"):
                return True

        return False

    def _affects_workflow_files(self, files: list[dict[str, Any]]) -> bool:
        """Check if files include GitHub workflow or configuration files."""
        for file_node in files:
            path = file_node.get("path", "")

            # Check if file is in .github directory
            if path.startswith(".github/"):
                # Exclude non-workflow files
                if path.endswith((".md", ".txt", ".png", ".jpg", ".gif")):
                    continue

                # Include workflow files and other YAML configs
                if path.startswith(".github/workflows/") or path.endswith(
                    (".yml", ".yaml")
                ):
                    return True

        return False

    async def _get_merged_prs_since(
        self, owner: str, name: str, since_date: str
    ) -> list[dict[str, Any]]:
        """Get merged PRs since a specific date."""
        try:
            # Convert date format from YYYY/MM/DD to ISO format
            date_obj = datetime.strptime(since_date, "%Y/%m/%d")
            iso_date = date_obj.strftime("%Y-%m-%dT%H:%M:%SZ")

            # Use REST API to get merged PRs
            merged_prs = []
            page = 1
            per_page = 100

            while True:
                params = {
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": per_page,
                    "page": page,
                }

                prs_data = await self._api.get(
                    f"/repos/{owner}/{name}/pulls", params=params
                )

                if not isinstance(prs_data, list) or len(prs_data) == 0:
                    break

                for pr in prs_data:
                    # Check if PR was merged
                    merged_at = pr.get("merged_at")
                    if merged_at:
                        # Check if merged after the since_date
                        if merged_at >= iso_date:
                            merged_prs.append(pr)

                # Check if we've reached the last page
                if len(prs_data) < per_page:
                    break

                page += 1

                # Limit to avoid excessive API calls
                if page > 10:
                    break

            return merged_prs

        except Exception as e:
            self.log.debug(f"Error getting merged PRs for {owner}/{name}: {e}")
            return []
