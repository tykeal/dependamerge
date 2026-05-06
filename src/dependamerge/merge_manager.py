# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

from rich.console import Console

from .copilot_handler import CopilotCommentHandler
from .gerrit import (
    GerritAuthError,
    GerritRestError,
    create_gerrit_service,
    create_submit_manager,
)
from .github2gerrit_detector import (
    GitHub2GerritDetectionResult,
    GitHub2GerritMapping,
    build_gerrit_change_url_from_mapping,
    build_gerrit_skip_message,
    build_gerrit_submission_comment,
    detect_github2gerrit_comments,
    fetch_gitreview_from_github,
)
from .github_async import GitHubAsync
from .github_async import PermissionError as GitHubPermissionError
from .github_service import GitHubService
from .models import ComparisonResult, PullRequestInfo
from .netrc import NetrcParseError, resolve_gerrit_credentials
from .output_utils import log_and_print
from .progress_tracker import MergeProgressTracker

# ---------------------------------------------------------------------------
# Centralised timing constants for all async merge operations.
#
# Every polling loop in this module (post-rebase status checks,
# pre-commit.ci re-runs, @dependabot recreate, recreated-PR readiness)
# derives its iteration count from these two values so that the timeout
# is consistent and easy to adjust from a single place or via the
# ``--merge-timeout`` CLI flag.
# ---------------------------------------------------------------------------
DEFAULT_MERGE_TIMEOUT: float = 300.0  # seconds (5 minutes)
DEFAULT_MERGE_RECHECK_INTERVAL: float = 10.0  # seconds between polls


class MergeStatus(Enum):
    """Status of a PR merge operation."""

    PENDING = "pending"
    APPROVING = "approving"
    APPROVED = "approved"
    MERGING = "merging"
    MERGED = "merged"
    AUTO_MERGE_PENDING = "auto_merge_pending"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclass
class MergeResult:
    """Result of a PR merge operation."""

    pr_info: PullRequestInfo
    status: MergeStatus
    error: str | None = None
    attempts: int = 0
    duration: float = 0.0


class AsyncMergeManager:
    """
    Manages parallel approval and merging of pull requests.

    This class handles:
    - Concurrent approval of PRs
    - Concurrent merging with retry logic
    - Progress tracking and error handling
    - Rate limit-aware processing
    """

    def __init__(
        self,
        token: str,
        merge_method: str = "merge",
        max_retries: int = 2,
        concurrency: int = 5,
        fix_out_of_date: bool = False,
        merge_timeout: float = DEFAULT_MERGE_TIMEOUT,
        progress_tracker: MergeProgressTracker | None = None,
        preview_mode: bool = False,
        dismiss_copilot: bool = False,
        force_level: str = "code-owners",
        github2gerrit_mode: str = "submit",
        no_netrc: bool = False,
        netrc_file: Path | None = None,
        rebase_local: bool = True,
    ):
        self.token = token
        self.default_merge_method = merge_method
        self.max_retries = max_retries
        self.concurrency = concurrency
        self.fix_out_of_date = fix_out_of_date
        self.progress_tracker = progress_tracker
        self.preview_mode = preview_mode
        self.dismiss_copilot = dismiss_copilot
        self.force_level = force_level
        self.github2gerrit_mode = github2gerrit_mode
        self.no_netrc = no_netrc
        self.netrc_file = netrc_file
        # When True (the default), Step 5's rebase path uses a local
        # ``git`` clone + rebase + force-push-with-lease workflow
        # for PRs whose verification status would otherwise be lost
        # by the GitHub REST ``update-branch`` endpoint. The local
        # workflow inherits the user's ``~/.gitconfig`` and so
        # respects ``commit.gpgsign`` / ``gpg.format`` /
        # ``user.signingkey`` automatically. Set to False to force
        # the legacy REST-only path (simpler but loses signature
        # verification on signed branches).
        self.rebase_local = rebase_local
        self.log = logging.getLogger(__name__)

        # Centralised merge-operation timing
        # Coerce merge_timeout to float and validate, guarding against Typer
        # OptionInfo objects that leak through when the CLI function is called
        # directly (e.g. from tests) without the Typer argument parser.
        try:
            _mt = float(merge_timeout)
            if not math.isfinite(_mt) or _mt <= 0:
                raise ValueError(f"out of range: {_mt}")
            self._merge_timeout = _mt
        except (TypeError, ValueError):
            self.log.warning(
                "Invalid merge_timeout=%r; falling back to default of %.0f seconds",
                merge_timeout,
                DEFAULT_MERGE_TIMEOUT,
            )
            self._merge_timeout = DEFAULT_MERGE_TIMEOUT
        # Clamp the per-iteration sleep so a small ``merge_timeout``
        # (< DEFAULT_MERGE_RECHECK_INTERVAL) does not over-sleep and
        # blow past the user-specified total timeout. For typical
        # values (>= 10s), this is a no-op and keeps the default
        # 10s polling cadence.
        self._merge_recheck_interval = min(
            DEFAULT_MERGE_RECHECK_INTERVAL, self._merge_timeout
        )
        # Use math.ceil so the effective poll window is at least
        # the configured ``merge_timeout`` — plain ``int()`` would
        # truncate (e.g. 301/10 -> 30 attempts -> only 300s).
        self._merge_poll_max_attempts = max(
            1, math.ceil(self._merge_timeout / self._merge_recheck_interval)
        )

        # Track merge operations
        self._merge_semaphore = asyncio.Semaphore(concurrency)
        self._results: list[MergeResult] = []
        self._github_client: GitHubAsync | None = None
        self._github_service: GitHubService | None = None
        self._copilot_handler: CopilotCommentHandler | None = None
        self._console = Console()

        # Track merge methods per repository
        self._pr_merge_methods: dict[str, str] = {}

        # Cache for organization-level settings to avoid repeated API calls
        # Key: org name, Value: org settings dict (or None on failure)
        self._org_settings_cache: dict[str, dict[str, Any] | None] = {}
        self._org_settings_locks: dict[str, asyncio.Lock] = {}
        self._org_settings_locks_lock = asyncio.Lock()

        # Track last merge exception per PR for better error reporting
        self._last_merge_exception: dict[str, Exception] = {}

        # Track PRs that were just approved (for post-approval merge retry)
        self._recently_approved: set[str] = set()

        # Track PRs where auto-merge has been enabled so that
        # post-timeout merge attempts can be skipped gracefully.
        self._auto_merge_enabled: set[str] = set()

        # Track PRs that have already gone through Step 5's
        # rebase + poll path so Step 5.5 can skip them and avoid
        # doubling the configured ``merge_timeout``. Set after
        # Step 5 completes its wait, regardless of whether the
        # final state is ``clean``, ``blocked``, or ``behind``.
        self._rebased_prs: set[str] = set()

        # Track PRs currently waiting for required checks to complete.
        # Maps ``pr_key`` -> deadline (monotonic seconds) so the
        # parallel merge ticker can render an aggregate countdown
        # without poking inside individual worker tasks.
        self._waiting_prs: dict[str, float] = {}
        self._waiting_lock = asyncio.Lock()

        # Delay (seconds) after submitting a new approval before attempting merge.
        # GitHub needs time to propagate the approval to branch-protection evaluation.
        default_post_approval_delay = 3.0
        env_post_approval_delay = os.getenv(
            "DEPENDAMERGE_POST_APPROVAL_DELAY",
            str(default_post_approval_delay),
        )
        try:
            parsed_delay = float(env_post_approval_delay)
            if not math.isfinite(parsed_delay) or parsed_delay < 0:
                raise ValueError(f"out of range: {parsed_delay}")
            self._post_approval_delay = parsed_delay
        except ValueError:
            self.log.warning(
                "Invalid DEPENDAMERGE_POST_APPROVAL_DELAY=%r; "
                "falling back to default of %.1f seconds",
                env_post_approval_delay,
                default_post_approval_delay,
            )
            self._post_approval_delay = default_post_approval_delay

    def __repr__(self) -> str:
        """Safe repr that never exposes the token value."""
        return "AsyncMergeManager(token=***)"

    def _get_mergeability_icon_and_style(
        self, mergeable_state: str | None
    ) -> tuple[str, str | None]:
        """Get appropriate icon and style for mergeable state."""
        if mergeable_state == "dirty":
            return "🛑", "red"
        elif mergeable_state == "behind":
            return "⚠️", "yellow"
        elif mergeable_state == "clean":
            return "✅", "green"
        elif mergeable_state == "draft":
            return "📝", "blue"
        else:
            return "🔍", None

    async def __aenter__(self):
        """Async context manager entry."""
        self._github_client = GitHubAsync(token=self.token)
        await self._github_client.__aenter__()

        # Initialize GitHubService for branch protection detection
        self._github_service = GitHubService(token=self.token)

        # Initialize Copilot handler if dismissal is enabled
        if self.dismiss_copilot:
            self._copilot_handler = CopilotCommentHandler(
                self._github_client, preview_mode=self.preview_mode, debug=True
            )

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._github_service:
            await self._github_service.close()
        if self._github_client:
            await self._github_client.__aexit__(exc_type, exc_val, exc_tb)

    async def merge_prs_parallel(
        self,
        pr_list: list[tuple[PullRequestInfo, ComparisonResult | None]],
    ) -> list[MergeResult]:
        """
        Merge multiple PRs in parallel.

        Args:
            pr_list: List of (PullRequestInfo, ComparisonResult) tuples

        Returns:
            List of MergeResult objects with operation results
        """
        if not pr_list:
            return []

        if self.preview_mode:
            self.log.info(f"🔍 PREVIEW: Would merge {len(pr_list)} PRs")
        else:
            self.log.debug(f"Starting parallel merge of {len(pr_list)} PRs")

        # Create tasks for all PRs
        tasks = []
        for pr_info, _comparison in pr_list:
            task = asyncio.create_task(
                self._merge_single_pr_with_semaphore(pr_info),
                name=f"merge-{pr_info.repository_full_name}#{pr_info.number}",
            )
            tasks.append(task)

        # Background ticker that surfaces a single-line countdown
        # whenever one or more workers are waiting for required
        # checks to complete (auto-merge wait loop). The countdown
        # uses the worst-case (latest) deadline across all waiting
        # PRs so the user sees the longest remaining wait.
        ticker_task = asyncio.create_task(
            self._wait_status_ticker(),
            name="merge-wait-ticker",
        )

        try:
            # Wait for all tasks to complete
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            ticker_task.cancel()
            try:
                await ticker_task
            except asyncio.CancelledError:
                # Expected during normal shutdown.
                pass
            except Exception as ticker_exc:
                # Unexpected: log so we can debug ticker crashes
                # without swallowing them silently. The merge run
                # itself has already completed at this point, so
                # we still continue to results processing.
                self.log.warning(
                    "wait-status ticker exited unexpectedly: %s",
                    ticker_exc,
                    exc_info=True,
                )

        # Process results and handle exceptions
        final_results: list[MergeResult] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                pr_info = pr_list[i][0]
                error_result = MergeResult(
                    pr_info=pr_info, status=MergeStatus.FAILED, error=str(result)
                )
                final_results.append(error_result)
                self.log.error(
                    f"Unexpected error merging PR {pr_info.repository_full_name}#{pr_info.number}: {result}"
                )
            else:
                # result is guaranteed to be MergeResult here since it's not an Exception
                final_results.append(cast(MergeResult, result))

        self._results = final_results
        return final_results

    async def _merge_single_pr_with_semaphore(
        self, pr_info: PullRequestInfo
    ) -> MergeResult:
        """Merge a single PR with concurrency control."""
        async with self._merge_semaphore:
            result = await self._merge_single_pr(pr_info)
            # merge_success() and merge_failure() already increment
            # completed_prs for MERGED/FAILED outcomes.  Catch the
            # remaining terminal states here so PR-level progress
            # reaches 100% even when some PRs are blocked, skipped,
            # or have auto-merge pending (where neither merge_success
            # nor merge_failure is called because the merge hasn't
            # actually happened yet — GitHub will merge it later).
            if (
                self.progress_tracker
                and result.status
                in (
                    MergeStatus.BLOCKED,
                    MergeStatus.SKIPPED,
                    MergeStatus.AUTO_MERGE_PENDING,
                )
            ):
                self.progress_tracker.pr_completed()
            return result

    async def _wait_status_ticker(self) -> None:
        """Update the progress display while PRs wait for required checks.

        Runs as a background task for the lifetime of
        ``merge_prs_parallel``. Once per second it samples
        ``self._waiting_prs`` and pushes a single-line status
        message into the progress tracker (which uses Rich Live
        for in-place updates) so the user can see how much
        longer the tool will block before returning the shell
        prompt.

        The countdown uses the latest (worst-case) deadline across
        all waiting PRs so the displayed value reflects the longest
        remaining wait, not an arbitrary one. When no PRs are
        waiting, the message is cleared.

        When the progress tracker is in non-Rich (fallback) mode,
        the per-update line would print to stdout, which would spam
        logs every second. In that case we delegate to the plain
        ticker (15s cadence) instead.
        """
        if not self.progress_tracker:
            # Fallback: emit a periodic plain console line so the
            # user still gets feedback even without Rich progress.
            await self._wait_status_ticker_plain()
            return

        # If the tracker exists but Rich is unavailable (non-TTY,
        # no Rich library, etc.), it falls back to per-update
        # ``print()`` calls. Updating every second would spam the
        # user's terminal/logs, so use the slower plain ticker
        # cadence instead.
        rich_available = bool(
            getattr(self.progress_tracker, "rich_available", False)
        )
        if not rich_available:
            await self._wait_status_ticker_plain()
            return

        last_message: str | None = None
        try:
            while True:
                async with self._waiting_lock:
                    snapshot = dict(self._waiting_prs)

                if snapshot:
                    now = asyncio.get_running_loop().time()
                    remaining = max(
                        0.0,
                        max(snapshot.values()) - now,
                    )
                    count = len(snapshot)
                    noun = "PR" if count == 1 else "PRs"
                    message = (
                        f"⏳ Waiting for {count} {noun} "
                        f"to complete checks [{int(remaining)}s]"
                    )
                else:
                    message = ""

                if message != last_message:
                    try:
                        self.progress_tracker.update_operation(message)
                    except Exception:
                        # Defensive: a failing tracker must never
                        # take down the whole merge run.
                        pass
                    last_message = message

                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            # Best-effort clear on shutdown so the final tracker
            # state isn't stuck on a stale countdown.
            if last_message:
                try:
                    self.progress_tracker.update_operation("")
                except Exception:
                    pass
            raise

    async def _wait_status_ticker_plain(self) -> None:
        """Plain-text countdown when no Rich progress tracker is present.

        Emits one console line every 15 seconds while PRs are
        waiting on required checks. Less granular than the Rich
        in-place update, but still gives the user visibility into
        why the tool is blocking.
        """
        last_emit: float = 0.0
        try:
            while True:
                async with self._waiting_lock:
                    snapshot = dict(self._waiting_prs)

                if snapshot:
                    now = asyncio.get_running_loop().time()
                    if now - last_emit >= 15.0:
                        remaining = max(
                            0.0, max(snapshot.values()) - now
                        )
                        count = len(snapshot)
                        noun = "PR" if count == 1 else "PRs"
                        try:
                            self._console.print(
                                f"⏳ Waiting for {count} {noun} "
                                f"to complete checks "
                                f"[{int(remaining)}s remaining]"
                            )
                        except Exception:
                            pass
                        last_emit = now
                else:
                    last_emit = 0.0

                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise

    async def _detect_github2gerrit(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
    ) -> GitHub2GerritDetectionResult:
        """
        Fetch issue comments for a PR and check for GitHub2Gerrit mapping.

        Args:
            repo_owner: Repository owner.
            repo_name: Repository name.
            pr_number: Pull request number.

        Returns:
            Detection result with mapping data if found.
        """
        try:
            if self._github_client is None:
                raise RuntimeError("GitHub client not initialized")

            # Fetch issue comments (not review comments) via REST API
            comments = await self._github_client.get(
                f"/repos/{repo_owner}/{repo_name}/issues/{pr_number}/comments"
            )

            if not isinstance(comments, list):
                return GitHub2GerritDetectionResult()

            return detect_github2gerrit_comments(comments)

        except Exception as exc:
            self.log.debug(
                "Failed to check GitHub2Gerrit comments for %s/%s#%d: %s",
                repo_owner,
                repo_name,
                pr_number,
                exc,
            )
            return GitHub2GerritDetectionResult()

    async def _submit_gerrit_change(
        self,
        mapping: GitHub2GerritMapping,
        pr_info: PullRequestInfo,
        repo_owner: str,
        repo_name: str,
    ) -> bool:
        """
        Submit the corresponding Gerrit change for a GitHub2Gerrit PR.

        Resolves Gerrit credentials, looks up the change by Change-ID,
        applies +2 Code-Review, and submits it.

        Args:
            mapping: The parsed GitHub2Gerrit mapping.
            pr_info: The GitHub pull request info.
            repo_owner: Repository owner (org or user).
            repo_name: Repository name.

        Returns:
            True if the Gerrit change was successfully submitted.
        """
        # We need to figure out the Gerrit host.  The mapping's topic name
        # follows the pattern "GH-<repo>-<number>" which doesn't embed the
        # host.  We look for a Gerrit change URL in the mapping comment body,
        # or fall back to well-known hosts.
        gerrit_host, gerrit_base_path = await self._resolve_gerrit_host(
            mapping, repo_owner, repo_name
        )

        if not gerrit_host:
            self.log.warning(
                "Cannot determine Gerrit host for GitHub2Gerrit mapping "
                "(topic: %s). Skipping Gerrit submission.",
                mapping.topic,
            )
            return False

        # Resolve credentials
        try:
            credentials = resolve_gerrit_credentials(
                host=gerrit_host,
                use_netrc=not self.no_netrc,
                netrc_file=self.netrc_file,
            )
        except NetrcParseError as exc:
            self.log.warning("Error parsing .netrc for Gerrit: %s", exc)
            credentials = None

        if credentials is None or not credentials.is_valid:
            self.log.warning(
                "No Gerrit credentials found for %s. Cannot submit "
                "GitHub2Gerrit change (topic: %s).",
                gerrit_host,
                mapping.topic,
            )
            return False

        try:
            # Create service and look up the change by Change-ID
            service = create_gerrit_service(
                host=gerrit_host,
                base_path=gerrit_base_path,
                username=credentials.username,
                password=credentials.password,
            )

            # Query Gerrit for the change using the primary Change-ID
            change_id = mapping.primary_change_id
            changes = service._query_changes(
                query=f"change:{change_id} status:open",
                limit=5,
                offset=0,
                options=[
                    "CURRENT_REVISION",
                    "LABELS",
                    "DETAILED_LABELS",
                    "SUBMIT_REQUIREMENTS",
                ],
            )

            if not changes:
                self.log.warning(
                    "No open Gerrit change found for Change-Id %s on %s",
                    change_id,
                    gerrit_host,
                )
                return False

            # Use the first matching change
            gerrit_change = changes[0]
            self.log.info(
                "Found Gerrit change %s #%d for Change-Id %s",
                gerrit_change.project,
                gerrit_change.number,
                change_id,
            )

            # Create submit manager and submit the change
            submit_manager = create_submit_manager(
                host=gerrit_host,
                base_path=gerrit_base_path,
                username=credentials.username,
                password=credentials.password,
            )

            results = submit_manager.submit_changes(
                [(gerrit_change, None)],
                review_labels={"Code-Review": 2},
                dry_run=self.preview_mode,
            )

            if results and results[0].submitted:
                self.log.info(
                    "Successfully submitted Gerrit change %s #%d",
                    gerrit_change.project,
                    gerrit_change.number,
                )

                # Post a comment on the GitHub PR and close it
                gerrit_url = build_gerrit_change_url_from_mapping(
                    mapping, gerrit_host, gerrit_base_path
                )
                await self._close_github_pr_after_gerrit_submit(
                    pr_info, mapping, gerrit_url
                )

                return True

            if results and results[0].success and self.preview_mode:
                # Dry-run succeeded
                return True

            error_msg = results[0].error if results else "Unknown error"
            self.log.warning(
                "Failed to submit Gerrit change %s #%d: %s",
                gerrit_change.project,
                gerrit_change.number,
                error_msg,
            )
            return False

        except (GerritAuthError, GerritRestError) as exc:
            self.log.warning(
                "Gerrit error submitting change for topic %s: %s",
                mapping.topic,
                exc,
            )
            return False
        except Exception as exc:
            self.log.warning(
                "Unexpected error submitting Gerrit change for topic %s: %s",
                mapping.topic,
                exc,
            )
            return False

    async def _close_github_pr_after_gerrit_submit(
        self,
        pr_info: PullRequestInfo,
        mapping: GitHub2GerritMapping,
        gerrit_url: str,
    ) -> None:
        """
        Close the GitHub PR and post a comment after Gerrit submission.

        Args:
            pr_info: The GitHub pull request.
            mapping: The parsed mapping.
            gerrit_url: URL of the submitted Gerrit change.
        """
        if self.preview_mode:
            return

        repo_owner, repo_name = pr_info.repository_full_name.split("/", 1)

        try:
            if self._github_client is None:
                raise RuntimeError("GitHub client not initialized")

            # Post comment following GitHub2Gerrit conventions
            comment_body = build_gerrit_submission_comment(mapping, gerrit_url)
            await self._github_client.post_issue_comment(
                repo_owner, repo_name, pr_info.number, comment_body
            )

            # Close the PR
            await self._github_client.close_pull_request(
                repo_owner, repo_name, pr_info.number
            )

            self.log.info(
                "Closed GitHub PR %s#%d after Gerrit submission",
                pr_info.repository_full_name,
                pr_info.number,
            )
        except Exception as exc:
            self.log.warning(
                "Failed to close GitHub PR %s#%d after Gerrit submission: %s",
                pr_info.repository_full_name,
                pr_info.number,
                exc,
            )

    async def _resolve_gerrit_host(
        self,
        mapping: GitHub2GerritMapping,
        repo_owner: str,
        repo_name: str,
    ) -> tuple[str | None, str | None]:
        """
        Determine the Gerrit host and base path for a GitHub2Gerrit PR.

        Resolution priority (highest first):

        1. ``.gitreview`` file in the repository (canonical source of truth)
        2. ``GERRIT_HOST`` / ``GERRIT_BASE_PATH`` environment variables
        3. Gerrit URL embedded in the mapping comment body
        4. Well-known host conventions (e.g. ``lfit`` → LF Gerrit)
        5. ``GERRIT_URL`` environment variable

        The ``.gitreview`` file is treated as definitive because every
        repository that uses GitHub2Gerrit is required to have one, and it
        records the exact Gerrit host, port, and project path.

        Args:
            mapping: The parsed GitHub2Gerrit mapping from the PR comment.
            repo_owner: Repository owner (org or user).
            repo_name: Repository name.

        Returns:
            Tuple of (host, base_path) or (None, None) if unresolvable.
        """
        # 1. .gitreview file — highest priority / source of truth
        if self._github_client is not None:
            gitreview_info = await fetch_gitreview_from_github(
                self._github_client, repo_owner, repo_name
            )
            if gitreview_info and gitreview_info.is_valid:
                self.log.info(
                    "Resolved Gerrit host from .gitreview in %s/%s: %s (base_path=%s)",
                    repo_owner,
                    repo_name,
                    gitreview_info.host,
                    gitreview_info.base_path,
                )
                return gitreview_info.host, gitreview_info.base_path

        # 2. Explicit environment variables
        env_host = os.getenv("GERRIT_HOST", "").strip()
        env_base_path = os.getenv("GERRIT_BASE_PATH", "").strip() or None
        if env_host:
            return env_host, env_base_path

        # 3. Gerrit URL embedded in the mapping comment body
        if mapping.raw_comment_body:
            gerrit_url_match = re.search(
                r"https?://([^/\s]+)(?:/([\w-]+))?/c/",
                mapping.raw_comment_body,
            )
            if gerrit_url_match:
                host = gerrit_url_match.group(1)
                base_path = (
                    gerrit_url_match.group(2) if gerrit_url_match.group(2) else None
                )
                return host, base_path

        # 4. Well-known LF Gerrit host
        if (
            mapping.pr_url and "github.com/lfit/" in mapping.pr_url
        ) or repo_owner == "lfit":
            return "gerrit.linuxfoundation.org", "infra"

        # 5. GERRIT_URL environment variable (catch-all)
        gerrit_url = os.getenv("GERRIT_URL", "").strip()
        if gerrit_url:
            url_match = re.match(r"https?://([^/]+)(?:/([\w-]+))?/?$", gerrit_url)
            if url_match:
                return url_match.group(1), url_match.group(2) if url_match.group(
                    2
                ) else None

        return None, None

    async def _merge_single_pr(self, pr_info: PullRequestInfo) -> MergeResult:
        """
        Merge a single pull request with retry logic.

        Args:
            pr_info: Pull request information

        Returns:
            MergeResult with operation status and details
        """
        start_time = time.time()
        repo_owner, repo_name = pr_info.repository_full_name.split("/", 1)

        # Early determination of merge method based on repository settings
        merge_method = await self._get_merge_method_for_repo(repo_owner, repo_name)

        # Store the determined merge method for this PR
        self._pr_merge_methods[f"{repo_owner}/{repo_name}"] = merge_method

        result = MergeResult(pr_info=pr_info, status=MergeStatus.PENDING)

        try:
            # --- GitHub2Gerrit detection (before any merge attempt) ---
            if self.github2gerrit_mode != "ignore":
                g2g_result = await self._detect_github2gerrit(
                    repo_owner, repo_name, pr_info.number
                )

                if g2g_result.has_mapping and g2g_result.mapping:
                    mapping = g2g_result.mapping
                    skip_msg = build_gerrit_skip_message(mapping)

                    if self.github2gerrit_mode == "skip":
                        # Skip this PR entirely
                        result.status = MergeStatus.SKIPPED
                        result.error = f"Skipped: {skip_msg}"
                        log_and_print(
                            self.log,
                            self._console,
                            f"⏩ Skipped: {pr_info.html_url} [{skip_msg}]",
                            level="info",
                        )
                        return result

                    # Default: "submit" mode - submit the Gerrit change
                    if self.preview_mode:
                        log_and_print(
                            self.log,
                            self._console,
                            f"🔄 Gerrit submit: {pr_info.html_url} [{skip_msg}]",
                            level="info",
                        )
                        result.status = MergeStatus.MERGED
                        if self.progress_tracker:
                            self.progress_tracker.merge_success()
                        return result

                    # Attempt to submit the Gerrit change
                    self._console.print(
                        f"🔄 Submitting Gerrit change for {pr_info.html_url} "
                        f"[{skip_msg}]"
                    )
                    submitted = await self._submit_gerrit_change(
                        mapping, pr_info, repo_owner, repo_name
                    )

                    if submitted:
                        result.status = MergeStatus.MERGED
                        if self.progress_tracker:
                            self.progress_tracker.merge_success()
                        log_and_print(
                            self.log,
                            self._console,
                            f"✅ Gerrit submitted: {pr_info.html_url}",
                            level="info",
                        )
                        return result

                    # Gerrit submission failed - report as failed
                    result.status = MergeStatus.FAILED
                    result.error = f"Failed to submit Gerrit change ({skip_msg})"
                    if self.progress_tracker:
                        self.progress_tracker.merge_failure()
                    self._console.print(
                        f"❌ Failed: {pr_info.html_url} "
                        f"[Gerrit submit failed for {skip_msg}]"
                    )
                    return result

            # Check if PR is closed before processing
            if pr_info.state != "open":
                result.status = MergeStatus.FAILED
                result.error = "PR is already closed"
                self._console.print(f"🛑 Failed: {pr_info.html_url} [already closed]")
                return result

            if not self._is_pr_mergeable(pr_info):
                # Get detailed status for a more informative skip message
                # Use async method to avoid event loop conflicts
                repo_owner, repo_name = pr_info.repository_full_name.split("/")

                # Check if blocked to get more detailed status
                if pr_info.mergeable_state == "blocked" and self._github_client:
                    try:
                        detailed_status = (
                            await self._github_client.analyze_block_reason(
                                repo_owner, repo_name, pr_info.number, pr_info.head_sha
                            )
                        )
                    except Exception:
                        detailed_status = f"Blocked (state: {pr_info.mergeable_state})"
                else:
                    # For non-blocked states, provide basic status
                    if pr_info.mergeable_state == "dirty":
                        detailed_status = "Merge conflicts"
                    elif pr_info.mergeable_state == "behind":
                        detailed_status = "Rebase required (out of date)"
                    elif pr_info.mergeable_state == "draft":
                        detailed_status = "Draft PR"
                    else:
                        detailed_status = (
                            f"Not mergeable (state: {pr_info.mergeable_state})"
                        )

                # Use the detailed status as the skip reason, with fallback
                if detailed_status and detailed_status != "Status unclear":
                    skip_reason = detailed_status.lower()
                else:
                    # Fallback to basic mapping if detailed status is unclear
                    if pr_info.mergeable_state == "dirty":
                        skip_reason = "merge conflicts"
                    elif pr_info.mergeable_state == "behind":
                        skip_reason = "behind"
                    elif pr_info.mergeable_state == "blocked":
                        if pr_info.mergeable is True:
                            skip_reason = "blocked, requires review"
                        else:
                            skip_reason = "blocked by failing checks"
                    elif pr_info.mergeable_state == "unstable":
                        skip_reason = "unstable"
                    elif pr_info.mergeable is False:
                        skip_reason = "not mergeable"
                    else:
                        skip_reason = "unknown"

                # Determine if this is truly blocked (unmergeable) or just skipped
                if pr_info.mergeable_state == "dirty" or (
                    pr_info.mergeable_state == "behind" and pr_info.mergeable is False
                ):
                    result.status = MergeStatus.BLOCKED
                    icon = "🛑"
                    status = "Blocked"
                else:
                    result.status = MergeStatus.SKIPPED
                    icon = "⏭️"
                    status = "Skipped"

                log_and_print(
                    self.log,
                    self._console,
                    f"{icon} {status}: {pr_info.html_url} [{skip_reason}]",
                    level="info",
                )

                result.error = f"PR is not mergeable (state: {pr_info.mergeable_state}, mergeable: {pr_info.mergeable})"

                # For the result error (used in CLI output), use the detailed status if it's more informative
                if detailed_status and detailed_status != "Status unclear":
                    result.error = detailed_status

                return result

            # Check for blocking reviews (changes requested)
            if self._has_blocking_reviews(pr_info):
                # Only skip if not forcing with 'all' level
                if self.force_level != "all":
                    result.status = MergeStatus.SKIPPED
                    result.error = "PR has reviews requesting changes - will not override human feedback"
                    log_and_print(
                        self.log,
                        self._console,
                        f"⏭️ Skipped: {pr_info.html_url} [has reviews requesting changes]",
                        level="debug",
                    )
                    return result
                else:
                    # Only log during preview evaluation to avoid duplicate messages
                    if self.preview_mode:
                        self.log.warning(
                            f"⚠️  Overriding blocking reviews for {pr_info.repository_full_name}#{pr_info.number} (--force=all)"
                        )

            # Step 0.5: If the PR is blocked, check for stale pre-commit.ci
            # and trigger a re-run before evaluating merge requirements.
            # Avoid triggering side effects when running in preview mode.
            if (
                not self.preview_mode
                and pr_info.mergeable_state == "blocked"
                and self._github_client
            ):
                precommit_fixed = await self._trigger_stale_precommit_ci(pr_info)
                if precommit_fixed:
                    # Re-fetch PR state now that pre-commit.ci has passed
                    try:
                        updated = await self._github_client.get(
                            f"/repos/{repo_owner}/{repo_name}/pulls/{pr_info.number}"
                        )
                        if isinstance(updated, dict):
                            pr_info.mergeable = updated.get("mergeable")
                            pr_info.mergeable_state = updated.get("mergeable_state")
                    except Exception as e:
                        self.log.debug(
                            "Failed to refresh PR %s mergeable state after "
                            "pre-commit.ci rerun: %s",
                            f"{pr_info.repository_full_name}#{pr_info.number}",
                            e,
                        )

            # Step 1: Check merge requirements (including branch protection)
            can_merge, merge_check_reason = await self._check_merge_requirements(
                pr_info
            )

            if not can_merge:
                result.status = MergeStatus.SKIPPED
                result.error = f"Merge requirements not met: {merge_check_reason}"
                log_and_print(
                    self.log,
                    self._console,
                    f"⏭️ Skipped: {pr_info.html_url} [{merge_check_reason.lower()}]",
                    level="debug",
                )
                return result

            # Step 2: Dismiss Copilot comments if enabled
            copilot_processing_successful = True
            if self.dismiss_copilot and self._copilot_handler:
                # Analyze what types of reviews we have
                self._copilot_handler.analyze_copilot_review_dismissibility(pr_info)

                try:
                    (
                        processed_count,
                        total_count,
                    ) = await self._copilot_handler.dismiss_copilot_comments_for_pr(
                        pr_info
                    )
                    if total_count > 0:
                        # Silent processing in background
                        pass
                except Exception as e:
                    self.log.warning(
                        f"⚠️  Failed to process Copilot items for PR {pr_info.number}: {e}"
                    )
                    copilot_processing_successful = False

            # Step 3: Only approve if Copilot processing was successful
            if not copilot_processing_successful:
                result.status = MergeStatus.FAILED
                result.error = "Copilot review processing incomplete - not approving to avoid pollution"
                self._console.print(
                    f"❌ Failed: {pr_info.html_url} [copilot processing incomplete]"
                )
                return result

            result.status = MergeStatus.APPROVING

            if self.progress_tracker:
                self.progress_tracker.update_operation(
                    f"Approving PR {pr_info.number} in {pr_info.repository_full_name}"
                )

            if not self.preview_mode:
                approval_added = await self._approve_pr(
                    repo_owner, repo_name, pr_info.number
                )
                if approval_added:
                    # Track that we just approved this PR so that the merge
                    # retry logic knows a propagation delay may be needed.
                    pr_key = f"{repo_owner}/{repo_name}#{pr_info.number}"
                    self._recently_approved.add(pr_key)

                    # Give GitHub time to propagate the approval to branch
                    # protection evaluation before attempting the merge.
                    if self._post_approval_delay > 0:
                        self.log.debug(
                            f"Waiting {self._post_approval_delay}s for approval propagation on {pr_key}"
                        )
                        await asyncio.sleep(self._post_approval_delay)
            result.status = MergeStatus.APPROVED

            # Step 5: Handle rebase if needed before merge
            if pr_info.mergeable_state == "behind" and self.fix_out_of_date:
                if self.preview_mode:
                    # NOTE: In preview mode, we should NOT print here as it breaks single-line reporting
                    # The preview output should only be a single line per PR in the evaluation section
                    pass
                else:
                    log_and_print(
                        self.log,
                        self._console,
                        f"🔄 Rebasing: {pr_info.html_url} [behind base branch]",
                        level="debug",
                    )

                    # Decide between the local ``git`` workflow and
                    # the REST ``update-branch`` endpoint. The local
                    # path preserves verified signatures (because it
                    # respects the user's ``~/.gitconfig`` signing
                    # config) at the cost of a per-PR clone; the
                    # REST path is faster but its server-side merge
                    # commit is unsigned, which can break branch
                    # protection rules that require verification.
                    use_local, local_reason = await self._should_use_local_rebase(
                        pr_info,
                        repo_owner,
                        repo_name,
                        pr_info.base_branch or "main",
                    )

                    if use_local:
                        # Local-rebase path. Run our git-based
                        # rebase + force-push-with-lease, then
                        # mark the PR as rebased and let Step 5.5
                        # take over (it will enable auto-merge and
                        # run its own bounded wait against the new
                        # head).
                        log_and_print(
                            self.log,
                            self._console,
                            f"🛡️  Local rebase: {pr_info.html_url} "
                            f"[{local_reason}]",
                            level="debug",
                        )
                        try:
                            local_rebase_ok = await self._local_git_rebase_pr(
                                pr_info, repo_owner, repo_name
                            )
                        except Exception as exc:
                            self.log.debug(
                                "Local rebase raised unexpectedly for %s: %s",
                                pr_info.html_url,
                                exc,
                            )
                            local_rebase_ok = False

                        # Whether the local rebase succeeded or
                        # failed, mark this PR as having been
                        # through Step 5 so Step 5.5's
                        # ``_rebased_prs`` gate fires and we don't
                        # double the configured merge_timeout. We
                        # deliberately do *not* fall through to
                        # REST update-branch on local failure —
                        # doing so would destroy verification,
                        # exactly the bug this code path exists
                        # to prevent.
                        self._rebased_prs.add(
                            f"{repo_owner}/{repo_name}#{pr_info.number}"
                        )
                        if local_rebase_ok:
                            log_and_print(
                                self.log,
                                self._console,
                                f"✅ Rebased (local): {pr_info.html_url}",
                                level="debug",
                            )
                        else:
                            log_and_print(
                                self.log,
                                self._console,
                                f"🛡️  Local rebase failed; deferring to "
                                f"auto-merge: {pr_info.html_url}",
                                level="debug",
                            )
                    else:
                        # Legacy REST path. Use ``update-branch``
                        # then poll the PR until checks complete or
                        # ``merge_timeout`` elapses.
                        try:
                            if self._github_client is None:
                                raise RuntimeError("GitHub client not initialized")
                            await self._github_client.update_branch(
                                repo_owner, repo_name, pr_info.number
                            )

                            # Enable auto-merge so the PR merges even if we
                            # time out waiting for status checks.
                            auto_merge_ok = await self._enable_auto_merge_for_pr(
                                pr_info, repo_owner, repo_name
                            )
                            if auto_merge_ok:
                                self.log.debug(
                                    "Auto-merge enabled after rebase for %s/%s#%s",
                                    repo_owner,
                                    repo_name,
                                    pr_info.number,
                                )

                            # Wait for GitHub to process the update and run checks
                            self._console.print(
                                f"⏳ Waiting: {pr_info.html_url}"
                            )
                            await asyncio.sleep(self._merge_recheck_interval)

                            # Re-check PR status after rebase with extended retry logic
                            # Initialize variables before the loop
                            updated_mergeable: bool | None = pr_info.mergeable
                            updated_mergeable_state: str | None = pr_info.mergeable_state

                            for check_attempt in range(self._merge_poll_max_attempts):
                                updated_pr_data = await self._github_client.get(
                                    f"/repos/{repo_owner}/{repo_name}/pulls/{pr_info.number}"
                                )

                                if isinstance(updated_pr_data, dict):
                                    updated_mergeable = updated_pr_data.get("mergeable")
                                    updated_mergeable_state = updated_pr_data.get(
                                        "mergeable_state"
                                    )
                                    # Capture the new head SHA so later
                                    # block-reason analysis queries the
                                    # rebased commit, not the pre-rebase
                                    # one. update_branch() advances
                                    # head.sha, and analyze_block_reason()
                                    # uses head_sha to query check runs.
                                    updated_head = (
                                        updated_pr_data.get("head") or {}
                                    ).get("sha")
                                    if updated_head:
                                        pr_info.head_sha = updated_head
                                else:
                                    updated_mergeable = None
                                    updated_mergeable_state = None

                                if updated_mergeable_state == "clean":
                                    break
                                elif updated_mergeable_state == "behind":
                                    if check_attempt < self._merge_poll_max_attempts - 1:
                                        self.log.debug(
                                            "PR still processing rebase, waiting... "
                                            "(attempt %d/%d)",
                                            check_attempt + 1,
                                            self._merge_poll_max_attempts,
                                        )
                                        await asyncio.sleep(self._merge_recheck_interval)
                                elif updated_mergeable_state == "blocked":
                                    if check_attempt < self._merge_poll_max_attempts - 1:
                                        self.log.debug(
                                            "PR status checks running after rebase, "
                                            "waiting... (attempt %d/%d)",
                                            check_attempt + 1,
                                            self._merge_poll_max_attempts,
                                        )
                                        await asyncio.sleep(self._merge_recheck_interval)
                                    else:
                                        if auto_merge_ok:
                                            log_and_print(
                                                self.log,
                                                self._console,
                                                f"⏳ Auto-merge will complete: "
                                                f"{pr_info.html_url} "
                                                "[timeout waiting for checks]",
                                                level="warning",
                                            )
                                        else:
                                            log_and_print(
                                                self.log,
                                                self._console,
                                                f"⚠️ Proceeding without checks: "
                                                f"{pr_info.html_url} "
                                                "[timeout waiting for checks]",
                                                level="warning",
                                            )
                                        break
                                elif updated_mergeable_state is None:
                                    # GitHub is still computing mergeability
                                    # (typically right after update_branch).
                                    # Treat as transient and keep polling
                                    # until the deadline or a concrete state
                                    # arrives — breaking here would otherwise
                                    # exit prematurely and (if auto-merge
                                    # enablement failed) fall through to a
                                    # manual merge attempt against the
                                    # still-resolving PR state.
                                    if check_attempt < self._merge_poll_max_attempts - 1:
                                        self.log.debug(
                                            "PR mergeable_state still computing "
                                            "after rebase, waiting... "
                                            "(attempt %d/%d)",
                                            check_attempt + 1,
                                            self._merge_poll_max_attempts,
                                        )
                                        await asyncio.sleep(self._merge_recheck_interval)
                                    else:
                                        break
                                else:
                                    break

                            # Update our PR info with the latest state.
                            # Preserve the previous non-None mergeable
                            # value when the refresh returns ``null``
                            # (GitHub is still computing). The Step 6
                            # auto-merge skip gate now accepts both
                            # ``True`` and ``None`` (it excludes only
                            # the explicit ``False`` case), so a
                            # transient null no longer blocks the
                            # auto-merge path on its own. We still
                            # preserve the prior known ``True`` here
                            # so downstream logging and any future
                            # tightening of that predicate get an
                            # accurate state to work with.
                            if updated_mergeable is not None:
                                pr_info.mergeable = updated_mergeable
                            # Preserve the previous non-None mergeable_state
                            # for the same reason as ``mergeable`` above:
                            # GitHub returns ``null`` while still computing,
                            # and the post-rebase reporting / Step 5.5 logic
                            # branches on this value (e.g. "clean" vs
                            # "blocked" vs "behind"). A transient ``None``
                            # would otherwise be classified as the catch-all
                            # "other state" branch.
                            if updated_mergeable_state is not None:
                                pr_info.mergeable_state = updated_mergeable_state

                            # Mark this PR as having gone through the
                            # Step 5 rebase + poll path. Step 5.5 will
                            # consult ``_rebased_prs`` to avoid doubling
                            # the merge_timeout when the rebase exits
                            # in ``blocked`` or ``behind`` state.
                            self._rebased_prs.add(
                                f"{repo_owner}/{repo_name}#{pr_info.number}"
                            )

                            # Report post-rebase status
                            if pr_info.mergeable_state == "clean":
                                log_and_print(
                                    self.log,
                                    self._console,
                                    f"✅ Rebased: {pr_info.html_url}",
                                    level="debug",
                                )
                            elif pr_info.mergeable_state == "behind":
                                log_and_print(
                                    self.log,
                                    self._console,
                                    f"⚠️  Rebased: {pr_info.html_url} [still behind after rebase]",
                                    level="debug",
                                )
                            elif pr_info.mergeable_state == "blocked":
                                log_and_print(
                                    self.log,
                                    self._console,
                                    f"⬆️ Rebased: {pr_info.html_url} [waiting for status checks]",
                                    level="debug",
                                )
                            else:
                                log_and_print(
                                    self.log,
                                    self._console,
                                    f"ℹ️  Rebased: {pr_info.html_url}",
                                    level="debug",
                                )

                        except Exception as e:
                            result.status = MergeStatus.FAILED
                            result.error = f"Failed to rebase PR: {e}"

                            if self.progress_tracker:
                                self.progress_tracker.merge_failure()
                            self._console.print(
                                f"❌ Failed: {pr_info.html_url} [rebase error: {e}]"
                            )
                            return result

            # Step 5.5: If the PR is still blocked (e.g. by a pending
            # required status check such as pre-commit.ci), behind
            # base branch, or unstable (a non-required check failed),
            # enable auto-merge and wait for required checks to
            # complete. Skipped when:
            #   * preview_mode (no side effects)
            #   * force_level == "all" (force semantics bypass wait)
            #   * Step 5 already ran a rebase + wait for this PR
            #     (avoid doubling the configured merge_timeout)
            #   * mergeable_state == "blocked" for a reason that
            #     cannot resolve on its own (e.g. "requires approval",
            #     missing code-owner reviews) — waiting would just
            #     delay the inevitable failure/merge by up to
            #     merge_timeout.
            #
            # Note that ``behind`` PRs go through Step 5.5 regardless
            # of ``fix_out_of_date``: even when we will not rebase the
            # PR ourselves, enabling auto-merge gives GitHub the chance
            # to finish merging once required checks land, and the
            # resulting AUTO_MERGE_PENDING outcome is friendlier than
            # a 405 manual-merge failure. This also covers the brief
            # window after Dependabot/pre-commit-ci rebase a PR where
            # GitHub still reports ``behind`` while it recomputes
            # mergeability.
            #
            # We accept any ``mergeable`` value (including ``False``)
            # when the state is one of these auto-merge-rescuable
            # states, because GitHub returns ``mergeable=False``
            # transiently while computing the value or when a
            # non-required check failed. The block-reason pre-check
            # below still weeds out genuinely-stuck cases (missing
            # approvals, etc.) so we don't burn ``merge_timeout`` on
            # them.
            pr_key_for_wait = (
                f"{repo_owner}/{repo_name}#{pr_info.number}"
            )
            already_rebased = pr_key_for_wait in self._rebased_prs
            base_should_wait = (
                not self.preview_mode
                and self._github_client is not None
                and pr_info.mergeable_state in ("blocked", "behind", "unstable")
                and self.force_level != "all"
                and not already_rebased
            )

            # For ``blocked`` PRs (but not ``behind``, which only
            # needs a rebase to clear), consult analyze_block_reason
            # before entering the wait loop so we don't burn the
            # full merge_timeout on PRs blocked for reasons that
            # cannot resolve on their own.
            should_wait = base_should_wait
            if (
                base_should_wait
                and pr_info.mergeable_state == "blocked"
                and self._github_client is not None
            ):
                try:
                    pre_block_reason = (
                        await self._github_client.analyze_block_reason(
                            repo_owner,
                            repo_name,
                            pr_info.number,
                            pr_info.head_sha,
                        )
                    )
                except Exception as exc:
                    self.log.debug(
                        "analyze_block_reason failed for %s during "
                        "Step 5.5 pre-check: %s",
                        pr_key_for_wait,
                        exc,
                    )
                    # Treat analysis failures as 'do not wait' so we
                    # don't burn the full ``merge_timeout`` on a PR
                    # whose block reason we cannot classify. The PR
                    # will fall through to the Step 6 skip gate (which
                    # also calls ``analyze_block_reason``) and either
                    # defer to auto-merge or surface a manual-merge
                    # error promptly.
                    pre_block_reason = None
                    should_wait = False

                if should_wait and pre_block_reason is not None:
                    if not self._block_reason_indicates_pending_checks(
                        pre_block_reason
                    ):
                        self.log.debug(
                            "Skipping Step 5.5 wait for %s: block "
                            "reason '%s' will not resolve on its own",
                            pr_key_for_wait,
                            pre_block_reason,
                        )
                        should_wait = False

            if should_wait:
                if pr_key_for_wait not in self._auto_merge_enabled:
                    auto_ok_pre = await self._enable_auto_merge_for_pr(
                        pr_info, repo_owner, repo_name
                    )
                    if auto_ok_pre:
                        log_and_print(
                            self.log,
                            self._console,
                            f"🤖 Auto-merge: {pr_info.html_url}",
                            level="debug",
                        )

                # Drive the wait loop directly off a monotonic
                # deadline so the total wait is bounded by
                # ``merge_timeout`` even if a single iteration
                # over-sleeps slightly.
                wait_deadline = (
                    asyncio.get_running_loop().time()
                    + self._merge_timeout
                )
                # Local alias so the type checker can narrow
                # ``self._github_client`` across the await boundary.
                wait_client = self._github_client
                assert wait_client is not None
                async with self._waiting_lock:
                    self._waiting_prs[pr_key_for_wait] = wait_deadline
                # Track whether the PR was closed during the wait
                # and, if so, whether it was actually merged. The
                # REST PR payload includes a ``merged`` boolean that
                # distinguishes auto-merge success from
                # closed-without-merge (the user closed the PR
                # while we were waiting, dependabot superseded it,
                # etc.).
                closed_during_wait = False
                merged_during_wait = False
                try:
                    while (
                        asyncio.get_running_loop().time() < wait_deadline
                    ):
                        if pr_info.mergeable_state == "clean":
                            break
                        # Sleep no longer than the time remaining
                        # so we don't overshoot ``wait_deadline``.
                        remaining = (
                            wait_deadline
                            - asyncio.get_running_loop().time()
                        )
                        await asyncio.sleep(
                            min(self._merge_recheck_interval, remaining)
                        )
                        try:
                            refreshed_wait = await wait_client.get(
                                f"/repos/{repo_owner}/{repo_name}/pulls/"
                                f"{pr_info.number}"
                            )
                        except Exception as wait_exc:
                            self.log.debug(
                                "Failed to refresh PR state during auto-merge "
                                "wait for %s/%s#%s: %s",
                                repo_owner,
                                repo_name,
                                pr_info.number,
                                wait_exc,
                            )
                            continue
                        if isinstance(refreshed_wait, dict):
                            # Only overwrite when the keys are present, so a
                            # partial/empty API response does not blank out
                            # the existing state. Additionally, preserve the
                            # previous non-None ``mergeable`` value when the
                            # refresh returns ``null`` — GitHub returns null
                            # while still computing the value. The Step 6
                            # skip gate accepts ``True`` and ``None`` (only
                            # ``False`` falls through to the manual merge
                            # attempt), but preserving the prior known
                            # ``True`` keeps the post-wait state
                            # informative for downstream logging and any
                            # future tightening of that predicate.
                            if "mergeable" in refreshed_wait:
                                refreshed_mergeable = refreshed_wait.get(
                                    "mergeable"
                                )
                                if refreshed_mergeable is not None:
                                    pr_info.mergeable = refreshed_mergeable
                            if "mergeable_state" in refreshed_wait:
                                # Preserve the previous non-None
                                # ``mergeable_state`` for the same
                                # reason as ``mergeable`` above. The
                                # subsequent ``not in ("blocked",
                                # "behind", "unstable")`` check would
                                # otherwise break the wait loop early
                                # on a transient ``null`` returned
                                # while GitHub is still computing.
                                refreshed_state = refreshed_wait.get(
                                    "mergeable_state"
                                )
                                if refreshed_state is not None:
                                    pr_info.mergeable_state = refreshed_state
                            # Capture the current head SHA so any
                            # subsequent analyze_block_reason()
                            # call queries the right commit. The
                            # head can change while we wait
                            # (rebase, dependabot force-push, etc.).
                            refreshed_head = (
                                refreshed_wait.get("head") or {}
                            ).get("sha")
                            if refreshed_head:
                                pr_info.head_sha = refreshed_head
                            if refreshed_wait.get("state") == "closed":
                                # PR was closed during the wait —
                                # capture the ``merged`` boolean so
                                # we can distinguish auto-merge
                                # success from closed-without-merge.
                                closed_during_wait = True
                                merged_during_wait = bool(
                                    refreshed_wait.get("merged", False)
                                )
                                pr_info.state = "closed"
                                break
                        # Continue waiting only while the PR is in a
                        # state that auto-merge can still rescue. The
                        # set must mirror the ``base_should_wait``
                        # entry condition above (blocked / behind /
                        # unstable); any other value means the PR has
                        # either become mergeable, has been closed,
                        # or has hit a state Step 5.5 cannot help
                        # with, so we should exit the wait loop and
                        # let downstream steps decide.
                        if pr_info.mergeable_state not in (
                            "blocked",
                            "behind",
                            "unstable",
                        ):
                            break
                finally:
                    async with self._waiting_lock:
                        self._waiting_prs.pop(pr_key_for_wait, None)

                # If the wait revealed the PR is already closed,
                # short-circuit before attempting a manual merge.
                # Distinguish auto-merge success from
                # closed-without-merge using the ``merged`` boolean
                # captured from the refresh payload.
                if closed_during_wait:
                    if merged_during_wait:
                        result.status = MergeStatus.MERGED
                        if self.progress_tracker:
                            self.progress_tracker.merge_success()
                        log_and_print(
                            self.log,
                            self._console,
                            f"✅ Merged (auto-merge): {pr_info.html_url}",
                            level="debug",
                        )
                    else:
                        result.status = MergeStatus.FAILED
                        result.error = "PR closed without merging during auto-merge wait"
                        if self.progress_tracker:
                            self.progress_tracker.merge_failure()
                        self._console.print(
                            f"🛑 Closed without merging: "
                            f"{pr_info.html_url}"
                        )
                    return result

            # Step 6: Attempt merge
            result.status = MergeStatus.MERGING
            if self.preview_mode:
                # IMPORTANT: Preview output must be SINGLE LINE per PR for clean evaluation display
                # Each PR should have exactly one line of output under "🔍 Dependamerge Evaluation"

                # In preview mode, simulate what would happen based on current PR state
                if pr_info.mergeable_state == "behind" and not self.fix_out_of_date:
                    result.status = MergeStatus.SKIPPED
                    result.error = "PR is behind base branch and --no-fix option is set"
                    self._console.print(
                        f"⏭️ Skipped: {pr_info.html_url} [behind, rebase disabled]"
                    )
                elif pr_info.mergeable_state == "behind" and self.fix_out_of_date:
                    # For behind PRs with fix enabled, show warning with rebase info
                    result.status = MergeStatus.MERGED  # Would succeed after rebase
                    result.error = "behind base branch"
                    if self.progress_tracker:
                        self.progress_tracker.merge_success()
                    self._console.print(
                        f"⚠️  Rebase/merge: {pr_info.html_url} [behind base branch]"
                    )
                elif pr_info.mergeable_state == "dirty":
                    result.status = MergeStatus.BLOCKED
                    result.error = "PR has merge conflicts"
                    self._console.print(
                        f"🛑 Blocked: {pr_info.html_url} [merge conflicts]"
                    )
                elif (
                    pr_info.mergeable is False and pr_info.mergeable_state == "blocked"
                ):
                    result.status = MergeStatus.BLOCKED
                    result.error = "PR blocked by failing checks"
                    self._console.print(
                        f"🛑 Blocked: {pr_info.html_url} [blocked by failing checks]"
                    )
                else:
                    # Simulate successful merge in preview mode
                    result.status = MergeStatus.MERGED
                    if self.progress_tracker:
                        self.progress_tracker.merge_success()
                    # Single line summary for successful preview
                    log_and_print(
                        self.log,
                        self._console,
                        f"☑️ Approve/merge: {pr_info.html_url}",
                        level="debug",
                    )
            else:
                if self.progress_tracker:
                    self.progress_tracker.update_operation(
                        f"Merging PR {pr_info.number} in {pr_info.repository_full_name}"
                    )

                # If auto-merge is enabled and the PR is in a state
                # that auto-merge can rescue (blocked, behind, or
                # unstable), skip the manual merge attempt — GitHub
                # will merge automatically once branch protection is
                # satisfied.
                #
                # We accept any ``mergeable`` value (including
                # ``False``) here for the same reason Step 5.5 does:
                # ``mergeable=False`` from the API can mean
                # "definitely failing", "still computing", or "a
                # non-required check failed". Letting auto-merge
                # decide whether the failing thing actually blocks
                # merge is more accurate than us treating
                # ``False`` as terminal here.
                #
                # For ``blocked`` PRs we still consult
                # ``analyze_block_reason()`` to weed out cases
                # auto-merge cannot resolve (missing approvals,
                # code-owner reviews, etc.). For ``behind`` and
                # ``unstable`` we accept directly: ``behind``
                # resolves once GitHub re-runs checks against the
                # rebased commit, and ``unstable`` means a
                # non-required check failed (which doesn't actually
                # block auto-merge).
                #
                # Do NOT skip when:
                #   * force_level == "all" — force semantics
                #     intentionally proceed despite the blocked
                #     state and must not be overridden by
                #     auto-merge.
                #   * the block reason (for ``blocked`` PRs) is
                #     something other than pending required
                #     checks (e.g. missing approvals).
                pr_key = f"{repo_owner}/{repo_name}#{pr_info.number}"
                auto_merge_pending_checks = False
                if (
                    pr_key in self._auto_merge_enabled
                    and pr_info.mergeable_state
                    in ("blocked", "behind", "unstable")
                    and self.force_level != "all"
                ):
                    if pr_info.mergeable_state in ("behind", "unstable"):
                        # ``behind``: still behind after rebase polling
                        # timed out; auto-merge will pick the PR up
                        # once GitHub finishes rebase + required
                        # checks.
                        # ``unstable``: a non-required check failed but
                        # required checks may still be pending or
                        # passing; auto-merge will fire when branch
                        # protection allows.
                        auto_merge_pending_checks = True
                    else:
                        block_reason: str | None = None
                        if self._github_client is not None:
                            try:
                                block_reason = (
                                    await self._github_client.analyze_block_reason(
                                        repo_owner,
                                        repo_name,
                                        pr_info.number,
                                        pr_info.head_sha,
                                    )
                                )
                            except Exception as exc:
                                self.log.debug(
                                    "analyze_block_reason failed for %s: %s",
                                    pr_key,
                                    exc,
                                )
                        # Treat any pending-checks-style block reason
                        # as auto-merge eligible. We previously matched
                        # only the literal substring "pending required
                        # check", but analyze_block_reason returns a
                        # range of phrasings (e.g. "required status
                        # check… pending") depending on which
                        # checks are outstanding. The same predicate is
                        # used by the Step 5.5 pre-check.
                        auto_merge_pending_checks = (
                            self._block_reason_indicates_pending_checks(
                                block_reason
                            )
                        )

                if auto_merge_pending_checks:
                    merged = None  # Sentinel: auto-merge pending
                else:
                    merged = await self._merge_pr_with_retry(
                        pr_info, repo_owner, repo_name
                    )

                if merged is None:
                    # Auto-merge is active — PR will merge asynchronously.
                    # The enable was already announced earlier ("🤖 Auto-merge:
                    # <url>" or via the rebase path); use a neutral
                    # "Waiting" line here to avoid duplicating that
                    # announcement. Tailor the bracketed reason to
                    # the actual ``mergeable_state`` so users see
                    # what auto-merge is waiting on, rather than
                    # always reporting "pending checks".
                    result.status = MergeStatus.AUTO_MERGE_PENDING
                    if pr_info.mergeable_state == "behind":
                        wait_reason = "behind base branch"
                    elif pr_info.mergeable_state == "unstable":
                        wait_reason = "non-required check failure"
                    else:
                        # ``blocked`` (the only other state that
                        # reaches this branch) routed through
                        # ``analyze_block_reason()`` and was
                        # classified as pending required checks by
                        # ``_block_reason_indicates_pending_checks``.
                        wait_reason = "pending checks"
                    log_and_print(
                        self.log,
                        self._console,
                        f"⏳ Waiting: {pr_info.html_url} [{wait_reason}]",
                        level="debug",
                    )
                elif merged:
                    result.status = MergeStatus.MERGED
                    if self.progress_tracker:
                        self.progress_tracker.merge_success()
                    log_and_print(
                        self.log,
                        self._console,
                        f"✅ Merged: {pr_info.html_url}",
                        level="debug",
                    )
                else:
                    # Compute failure summary once — used for both the
                    # recreate decision and the final error reporting.
                    failure_reason = self._get_failure_summary(pr_info)

                    # Before giving up, check if this is a dependabot PR
                    # that failed due to unsigned commits.  If so, ask
                    # dependabot to recreate the PR and merge the new one.
                    recreated_pr = None
                    if pr_info.author == "dependabot[bot]" and not self.preview_mode:
                        if "branch protection" in failure_reason.lower():
                            recreated_pr = await self._trigger_dependabot_recreate(
                                pr_info
                            )

                    if recreated_pr is not None:
                        # We have a fresh PR — approve and merge it
                        new_owner, new_repo = recreated_pr.repository_full_name.split(
                            "/", 1
                        )
                        await self._approve_pr(new_owner, new_repo, recreated_pr.number)

                        new_merge_method = self._pr_merge_methods.get(
                            f"{new_owner}/{new_repo}", self.default_merge_method
                        )
                        try:
                            if self._github_client is None:
                                raise RuntimeError("GitHub client not initialized")
                            new_merged = await self._github_client.merge_pull_request(
                                new_owner,
                                new_repo,
                                recreated_pr.number,
                                new_merge_method,
                            )
                        except Exception as merge_err:
                            self.log.error(
                                "Failed to merge recreated PR %s#%s: %s",
                                recreated_pr.repository_full_name,
                                recreated_pr.number,
                                merge_err,
                            )
                            new_merged = False

                        if new_merged:
                            result.status = MergeStatus.MERGED
                            result.pr_info = recreated_pr
                            if self.progress_tracker:
                                self.progress_tracker.merge_success()
                            log_and_print(
                                self.log,
                                self._console,
                                f"✅ Merged (recreated): {recreated_pr.html_url}",
                                level="debug",
                            )
                        else:
                            result.status = MergeStatus.FAILED
                            result.error = (
                                f"Dependabot recreated PR #{recreated_pr.number} "
                                "but merge still failed"
                            )
                            if self.progress_tracker:
                                self.progress_tracker.merge_failure()
                            self.log.error(
                                "Failed to merge recreated PR %s#%s",
                                recreated_pr.repository_full_name,
                                recreated_pr.number,
                            )
                            self._console.print(
                                f"❌ Failed: {recreated_pr.html_url} "
                                "[recreated PR merge failed]"
                            )
                    else:
                        result.status = MergeStatus.FAILED
                        result.error = "Failed to merge after all retry attempts"
                        if self.progress_tracker:
                            self.progress_tracker.merge_failure()
                        self._console.print(
                            f"❌ Failed: {pr_info.html_url} [{failure_reason}]"
                        )

        except GitHubPermissionError as e:
            # Handle permission errors with detailed guidance
            result.status = MergeStatus.FAILED
            result.error = str(e)
            if self.progress_tracker:
                self.progress_tracker.merge_failure()

            # Extract operation-specific error message
            operation_desc = e.operation.replace("_", " ")
            self._console.print(
                f"❌ Failed: {pr_info.html_url} [permission denied: {operation_desc}]"
            )

            # Provide token-specific guidance
            self._console.print("\n💡 Token Permission Issue:")
            self._console.print(f"   Problem: {e}")

            if e.token_type_guidance:
                self._console.print("\n   For Classic Tokens:")
                self._console.print(
                    f"   • {e.token_type_guidance.get('classic', 'Check token scopes')}"
                )
                self._console.print("\n   For Fine-Grained Tokens:")
                self._console.print(
                    f"   • {e.token_type_guidance.get('fine_grained', 'Check token permissions')}"
                )
                if "fix" in e.token_type_guidance:
                    self._console.print("\n   Quick Fix:")
                    self._console.print(f"   • {e.token_type_guidance['fix']}")

            self._console.print()

        except Exception as e:
            result.status = MergeStatus.FAILED
            result.error = str(e)
            if self.progress_tracker:
                self.progress_tracker.merge_failure()

            # Provide clean single-line error messages for other errors.
            # Also log at error level with the stack trace so users
            # debugging via log files (where stdout isn't captured)
            # have full context.
            self.log.error(
                "Failed to process PR %s: %s",
                pr_info.html_url,
                e,
                exc_info=True,
            )
            self._console.print(
                f"❌ Failed: {pr_info.html_url} [processing error: {e}]"
            )

        finally:
            result.duration = time.time() - start_time
            # Clean up recently-approved tracking to avoid unbounded growth
            pr_key = f"{repo_owner}/{repo_name}#{pr_info.number}"
            self._recently_approved.discard(pr_key)

        return result

    @staticmethod
    def _block_reason_indicates_pending_checks(
        block_reason: str | None,
    ) -> bool:
        """Return True if a block reason indicates pending required checks.

        Both Step 5.5 (whether to enter the wait loop) and Step 6
        (whether to defer to auto-merge instead of attempting a
        manual merge) need to recognise the same set of phrasings
        returned by ``GitHubAsync.analyze_block_reason()``.
        Centralising the predicate here keeps the two call sites
        consistent so a new phrasing only has to be added once.

        The predicate matches **only** wording that explicitly
        signals a check is still in progress / waiting to start.
        It deliberately excludes:

        - ``Blocked by failing check: …`` — the check has run and
          reported a non-pending failure; auto-merge will not
          rescue this.
        - ``Blocked by missing required status: …`` — the check
          has not been registered against the commit at all;
          auto-merge will not retry it on its own.
        - any reason where ``failing`` or ``missing`` appears
          before a service name (defensive: covers future GitHub
          phrasing changes that include both keywords).

        Args:
            block_reason: The string returned by
                ``analyze_block_reason()``, or ``None`` if the
                analysis failed or returned nothing.

        Returns:
            True when the reason mentions pending required checks
            in any of the recognised phrasings; False otherwise
            (including when ``block_reason`` is ``None``).
        """
        if block_reason is None:
            return False
        reason_lower = block_reason.lower()

        # Defensive negative gate: never classify a reason as
        # 'pending' if it explicitly says the check has failed or
        # is missing. This guards the bare-substring matches
        # below against future phrasings that combine both terms
        # (e.g. "failing check (pending retry): pre-commit.ci").
        if "failing check" in reason_lower:
            return False
        if "missing required status" in reason_lower:
            return False
        if "missing required check" in reason_lower:
            return False

        return (
            "pending required check" in reason_lower
            or ("required" in reason_lower and "pending" in reason_lower)
            or "waiting for status" in reason_lower
            or "queued" in reason_lower
        )

    def _is_pr_mergeable(self, pr_info: PullRequestInfo) -> bool:
        """Check whether a PR is worth attempting to merge.

        This returns ``True`` for any state where dependamerge can
        plausibly make progress — either by approving + merging,
        rebasing, or enabling auto-merge and waiting (Step 5.5).
        We deliberately err on the side of letting Step 5.5 see the
        PR: it has finer-grained logic (block-reason analysis,
        merge-timeout-bounded waits) than this gate, so a False here
        denies a PR the chance to be auto-merge-rescued.

        Returns False only for states where no amount of waiting,
        approving, or auto-merging can help:

        - ``dirty``: real merge conflict; the branch must be
          rebased by a human (or by ``--fix``).
        - ``draft``: GitHub blocks merging draft PRs by design.

        For all other states (``blocked``, ``behind``, ``unstable``,
        empty/``"unknown"``) we return True regardless of the
        ``mergeable`` boolean. ``mergeable=False`` from the API can
        mean "definitely failing", but it can also mean "GitHub is
        still computing" or "a non-required check failed" — the
        downstream Step 5.5 + Step 6 gates have the context to make
        the right call.
        """
        # Hard skips: states where merging is impossible regardless
        # of mergeable value or downstream rescue logic.
        if pr_info.mergeable_state == "dirty":
            self.log.debug(
                "🛑 Skipping PR %s/%s#%s: merge conflict (dirty)",
                pr_info.repository_full_name.split("/", 1)[0]
                if "/" in pr_info.repository_full_name
                else pr_info.repository_full_name,
                pr_info.repository_full_name.split("/", 1)[-1],
                pr_info.number,
            )
            return False
        if pr_info.mergeable_state == "draft":
            self.log.debug(
                "⏭️ Skipping draft PR %s#%s",
                pr_info.repository_full_name,
                pr_info.number,
            )
            return False

        # Everything else — ``blocked``, ``behind``, ``unstable``,
        # ``clean``, empty/None state, plus any ``mergeable`` value
        # — reaches the merge flow. Step 5.5 will route
        # not-yet-merge-ready cases to AUTO_MERGE_PENDING after
        # consulting block-reason analysis and bounded by
        # ``merge_timeout``, which is a much friendlier outcome than
        # a hard skip from here.
        self.log.debug(
            "✅ PR %s#%s eligible for merge flow (mergeable=%s, state=%s)",
            pr_info.repository_full_name,
            pr_info.number,
            pr_info.mergeable,
            pr_info.mergeable_state,
        )
        return True

    def _has_blocking_reviews(self, pr_info: PullRequestInfo) -> bool:
        """
        Check if a PR has reviews that would block automatic approval.

        Args:
            pr_info: Pull request information

        Returns:
            True if there are blocking reviews (changes requested), False otherwise
        """
        for review in pr_info.reviews:
            if review.state == "CHANGES_REQUESTED":
                self.log.info(
                    f"⚠️  PR {pr_info.number} has changes requested by {review.user} - will not override human feedback"
                )
                return True
        return False

    async def _post_pr_comment_with_retry(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        html_url: str,
        body: str,
    ) -> bool:
        """Post a PR comment with one retry after a 5s pause.

        Used for the auto-merge audit-trail comment so the PR
        conversation reflects that dependamerge enabled auto-merge.
        Approval comments take a different path: the approval body
        is passed directly to ``approve_pull_request()``, which
        creates a review (not an issue comment), so this helper is
        not used there. If both attempts fail, emit a single
        user-visible warning to the console rather than silently
        failing.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            html_url: Full PR URL, used for the warning message.
            body: Markdown body of the comment.

        Returns:
            True if the comment posted successfully (first or
            second attempt), False otherwise.
        """
        if not self._github_client:
            return False

        for attempt in (1, 2):
            try:
                await self._github_client.post_issue_comment(
                    owner, repo, pr_number, body
                )
                return True
            except GitHubPermissionError as exc:
                # Permission errors (typically HTTP 403) are not
                # transient — the token lacks the required scope or
                # the repo's branch protection forbids comments.
                # Skip the retry to avoid a pointless 5s delay per
                # PR and surface the failure right away.
                self.log.debug(
                    "Audit comment post denied (permission) for %s: %s",
                    html_url,
                    exc,
                )
                break
            except Exception as exc:
                # Heuristic: treat 4xx (other than 408/429) as
                # permanent and skip the retry. 5xx, 429 (rate
                # limit), 408 (timeout), and network/transport
                # errors get one retry after a short pause.
                #
                # We check several attribute paths so the
                # heuristic works across the various exception
                # shapes we may see:
                #   * ``exc.status_code`` — some custom wrappers
                #   * ``exc.status`` — ``aiohttp.ClientResponseError``
                #   * ``exc.response.status_code`` — ``httpx`` raises
                #     ``HTTPStatusError`` whose status lives on the
                #     attached ``Response`` object (this is what
                #     ``httpx.Response.raise_for_status()`` produces).
                response = getattr(exc, "response", None)
                status_code = (
                    getattr(exc, "status_code", None)
                    or getattr(exc, "status", None)
                    or getattr(response, "status_code", None)
                    or getattr(response, "status", None)
                )
                permanent = (
                    isinstance(status_code, int)
                    and 400 <= status_code < 500
                    and status_code not in (408, 429)
                )
                self.log.debug(
                    "Audit comment post attempt %d failed for %s: %s"
                    " (status=%r, permanent=%s)",
                    attempt,
                    html_url,
                    exc,
                    status_code,
                    permanent,
                )
                if permanent:
                    break
                if attempt == 1:
                    await asyncio.sleep(5.0)

        # Both attempts failed — surface a single line so the
        # user knows the PR-side audit trail is incomplete.
        try:
            self._console.print(
                f"⚠️ Unable to add pull request comment: {html_url}"
            )
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Signature-preserving local rebase
    # ------------------------------------------------------------------
    #
    # The GitHub REST ``PUT /repos/{owner}/{repo}/pulls/{n}/update-branch``
    # endpoint creates a *server-side* merge commit whose committer is
    # the calling token's GitHub user. That committer is not signed
    # with the user's local SSH/GPG key, so the resulting commit
    # loses its ``Verified`` badge — if the base branch's protection
    # rules require verified signatures, the PR becomes
    # un-mergeable until a human intervenes.
    #
    # Worse, automation bots that *would* normally recover from this
    # have no comment-macro hook for it: ``@dependabot recreate``
    # exists, but ``pre-commit-ci`` has no equivalent (see issue
    # https://github.com/pre-commit-ci/issues/issues/41), so any
    # pre-commit-ci PR we break this way is stuck until somebody
    # closes it manually.
    #
    # The fix is to do the rebase *locally*: clone, rebase onto the
    # base branch, force-push-with-lease. Because we shell out to
    # ``git``, the user's ``~/.gitconfig`` (``commit.gpgsign``,
    # ``gpg.format``, ``user.signingkey``) is honoured and the new
    # commits remain verified.
    #
    # We only take this path when:
    #   * ``self.rebase_local`` is True (it is by default; the CLI
    #     exposes ``--no-rebase-local`` to opt out), AND
    #   * either the PR is from ``pre-commit-ci[bot]`` (always —
    #     it has no recovery macro), OR the base branch requires
    #     verified signatures AND the current PR head commit is
    #     itself verified (so REST update-branch *would* break
    #     verification).
    #
    # On any failure (no ``git`` on PATH, conflict during rebase,
    # network error, push rejected) we abort cleanly and return
    # False; the caller then falls through to Step 5.5 so auto-merge
    # can take over server-side. Auto-merge produces a properly
    # verified merge commit signed by GitHub itself.

    async def _should_use_local_rebase(
        self,
        pr_info: PullRequestInfo,
        owner: str,
        repo: str,
        base_branch: str,
    ) -> tuple[bool, str]:
        """Decide whether Step 5 should rebase locally instead of via REST.

        Returns:
            ``(use_local, reason)``. ``reason`` is a short
            human-readable string suitable for debug logging or a
            user-visible note when ``use_local`` is True.
        """
        if not self.rebase_local:
            return False, "--no-rebase-local set"

        # Always use local rebase for pre-commit.ci PRs. The bot has
        # no comment macro to recover from a verification break, so
        # we treat it as opt-in regardless of branch protection.
        if pr_info.author == "pre-commit-ci[bot]":
            return True, "pre-commit-ci[bot] has no recreate/rebase macro"

        if self._github_client is None:
            return False, "no GitHub client"

        # Branch-protection signature requirement (classic + rulesets)
        try:
            requires_signatures = await self._github_client.requires_commit_signatures(
                owner, repo, base_branch
            )
        except Exception as exc:
            self.log.debug(
                "Could not determine signature requirement for %s/%s:%s: %s",
                owner,
                repo,
                base_branch,
                exc,
            )
            return False, "signature requirement check failed"

        # Strict ``is True`` rather than truthy check: ``AsyncMock``
        # default returns evaluate as truthy, and we explicitly do
        # not want to enter the network-touching local-rebase path
        # in test mocks that haven't been set up to handle it.
        if requires_signatures is not True:
            return False, "base branch does not require signatures"

        # Base does require verified signatures. Check whether the
        # current PR head is itself verified — if it isn't, REST
        # update-branch can't make things worse, so we don't need
        # the local-rebase machinery.
        try:
            all_verified, _unverified = (
                await self._github_client.check_pr_commit_signatures(
                    owner, repo, pr_info.number
                )
            )
        except Exception as exc:
            self.log.debug(
                "Could not check PR commit signatures for %s/%s#%s: %s",
                owner,
                repo,
                pr_info.number,
                exc,
            )
            # Conservative: if we can't tell, prefer the local path
            # (worst case: minor extra work; best case: preserve a
            # signature we couldn't see).
            return True, "signature check failed; preferring local path"

        if all_verified:
            return True, "base requires signatures and PR head is verified"
        return False, "PR head is not currently verified"

    async def _local_git_rebase_pr(
        self,
        pr_info: PullRequestInfo,
        owner: str,
        repo: str,
    ) -> bool:
        """Rebase a PR locally and force-push the result.

        Clones the head repo into a secure temp workspace, fetches
        the base branch (from upstream when the PR is from a fork),
        runs ``git rebase``, and force-pushes with lease back to the
        head repo. All git invocations inherit the user's
        ``~/.gitconfig``, so signing config is respected.

        Returns True only if every step succeeds. On any failure
        (no ``git`` on PATH, conflict during rebase, network error,
        push rejected) the workspace is cleaned up and False is
        returned; the caller falls through to the auto-merge path
        so we never leave a half-applied state.
        """
        # Local imports keep test collection cheap when the merge
        # path is exercised purely through mocks.
        from . import git_ops
        from .git_ops import (
            GitError,
            add_remote,
            checkout,
            clone,
            create_secure_tempdir,
            ensure_git_available,
            fetch,
            push_force_with_lease,
            rebase,
            rebase_abort,
            secure_rmtree,
        )

        # Ensure ``git`` is on PATH before we start. ``GitError``
        # is also raised when git is missing entirely.
        try:
            ensure_git_available()
        except Exception as exc:
            self.log.debug(
                "Local rebase unavailable (no git on PATH?): %s", exc
            )
            return False

        # We need the head/base clone URLs. They are populated for
        # PRs surfaced by recent versions of the find-similar / merge
        # flows; if missing we cannot proceed locally.
        head_clone_url = pr_info.head_repo_clone_url
        base_clone_url = pr_info.base_repo_clone_url
        head_full = pr_info.head_repo_full_name
        base_full = pr_info.base_repo_full_name or f"{owner}/{repo}"
        if not head_clone_url:
            head_clone_url = f"https://github.com/{head_full or base_full}.git"
        if not base_clone_url:
            base_clone_url = f"https://github.com/{base_full}.git"
        head_full = head_full or base_full

        head_branch = pr_info.head_branch
        base_branch = pr_info.base_branch or "main"
        if not head_branch:
            self.log.debug(
                "Local rebase: PR %s/%s#%s missing head_branch",
                owner,
                repo,
                pr_info.number,
            )
            return False

        origin_url = self._authed_clone_url(head_clone_url, self.token)
        upstream_url = self._authed_clone_url(base_clone_url, self.token)

        # Use a per-PR workspace under a secure temp parent so
        # concurrent rebases (--concurrency=N) don't collide.
        workspace_parent = Path(
            git_ops.create_secure_tempdir(
                prefix="dependamerge-localrebase-"
            )
        )
        workspace = (
            workspace_parent
            / f"{(head_full or base_full).replace('/', '__')}__pr_{pr_info.number}"
        )
        workspace.mkdir(parents=True, exist_ok=True)

        try:
            # Clone the head repo at the PR's head branch. Shallow
            # clone keeps disk + network footprint low for what
            # are typically tiny dependency-update PRs.
            try:
                clone(
                    origin_url,
                    workspace,
                    branch=head_branch,
                    depth=50,
                    single_branch=True,
                    no_tags=True,
                    filter_blobs=True,
                    logger=self.log.debug,
                )
            except GitError as exc:
                self.log.debug(
                    "Local rebase: clone failed for %s: %s",
                    pr_info.html_url,
                    exc,
                )
                return False

            # Fetch the base branch — from upstream when the PR
            # is from a fork, from origin otherwise. We need it
            # available locally before we can rebase onto it.
            try:
                if (head_full or base_full) != base_full:
                    add_remote(
                        "upstream",
                        upstream_url,
                        cwd=workspace,
                        logger=self.log.debug,
                    )
                    fetch(
                        "upstream",
                        base_branch,
                        cwd=workspace,
                        depth=50,
                        logger=self.log.debug,
                    )
                    rebase_onto = f"upstream/{base_branch}"
                else:
                    fetch(
                        "origin",
                        base_branch,
                        cwd=workspace,
                        depth=50,
                        logger=self.log.debug,
                    )
                    rebase_onto = f"origin/{base_branch}"
            except GitError as exc:
                self.log.debug(
                    "Local rebase: fetch failed for %s: %s",
                    pr_info.html_url,
                    exc,
                )
                return False

            # Make sure we are on the head branch (defensive against
            # detached HEAD after clone --branch).
            try:
                checkout(
                    head_branch,
                    cwd=workspace,
                    create=False,
                    logger=self.log.debug,
                )
            except GitError:
                # Already on the branch, or branch missing locally;
                # rebase will surface the real problem if any.
                pass

            # Rebase. ``git rebase`` here is non-interactive; if
            # there are conflicts the command exits non-zero and
            # leaves the working tree in conflict state, which we
            # explicitly abort and treat as failure.
            try:
                rebase_result = rebase(
                    rebase_onto,
                    cwd=workspace,
                    autostash=False,
                    interactive=False,
                    logger=self.log.debug,
                )
            except GitError as exc:
                self.log.debug(
                    "Local rebase: rebase failed for %s: %s",
                    pr_info.html_url,
                    exc,
                )
                return False

            if rebase_result.returncode != 0:
                self.log.debug(
                    "Local rebase: conflicts during rebase of %s; aborting.",
                    pr_info.html_url,
                )
                try:
                    rebase_abort(cwd=workspace, logger=self.log.debug)
                except Exception:
                    pass
                return False

            # Force-push with lease to the head repo. We push back
            # to ``origin`` because the head ref always lives there
            # (even for forks, the head repo *is* the fork).
            try:
                push_force_with_lease(
                    "origin",
                    head_branch,
                    head_branch,
                    cwd=workspace,
                    logger=self.log.debug,
                )
            except GitError as exc:
                self.log.debug(
                    "Local rebase: force-push failed for %s: %s",
                    pr_info.html_url,
                    exc,
                )
                return False

            self.log.debug(
                "Local rebase succeeded for %s", pr_info.html_url
            )
            return True

        finally:
            # Always clean up. The workspace contains a clone of
            # the user's repository, so we want it gone even on
            # success.
            try:
                secure_rmtree(workspace_parent)
            except Exception as exc:
                self.log.debug(
                    "Local rebase: failed to clean up workspace %s: %s",
                    workspace_parent,
                    exc,
                )

    @staticmethod
    def _authed_clone_url(clone_url: str, token: str) -> str:
        """Return an HTTPS clone URL with the token injected for auth.

        Mirrors ``FixOrchestrator._authed_url`` so the token never
        appears in command-line arguments (the URL goes through
        the standard ``git clone`` machinery, which redacts it from
        log output via ``git_ops._redact``). Non-HTTPS URLs (SSH,
        etc.) are returned unchanged.
        """
        if clone_url.startswith("https://"):
            return clone_url.replace(
                "https://", f"https://x-access-token:{token}@"
            )
        return clone_url

    async def _enable_auto_merge_for_pr(
        self, pr_info: PullRequestInfo, owner: str, repo: str
    ) -> bool:
        """Enable auto-merge on a PR so it merges when checks pass.

        Idempotent and safe to call when auto-merge may already be
        active. Outcomes:

        - GraphQL ``enablePullRequestAutoMerge`` mutation succeeds:
          add the PR to ``_auto_merge_enabled``, post the audit
          comment, and return ``True``.
        - GraphQL mutation reports failure (commonly because
          auto-merge is *already* active on the PR — the response
          omits ``autoMergeRequest`` or the request raises): fall
          back to a REST GET on the PR and inspect ``auto_merge``.
          If non-null, treat as already-enabled — add the PR to
          ``_auto_merge_enabled`` (so the Step 6 skip gate still
          routes it to ``AUTO_MERGE_PENDING`` rather than
          attempting a 405-prone manual merge) and return ``True``,
          but skip the audit comment so re-runs against an
          already-configured PR don't post duplicates.
        - GraphQL mutation reports failure AND the PR has no
          ``auto_merge`` set: auto-merge is genuinely unavailable
          (e.g. the repository setting is off, the PR has
          conflicts, no required-checks are configured). Return
          ``False`` and let the caller fall through to manual
          polling/merge.
        - The PR has no ``node_id`` or there is no GitHub client:
          return ``False`` without making any API calls.

        Args:
            pr_info: Pull request information (must have ``node_id``).
            owner: Repository owner.
            repo: Repository name.

        Returns:
            True if auto-merge is active on the PR after this call
            (whether enabled by this call or already-active before
            it). False if auto-merge is unavailable.
        """
        if not self._github_client:
            return False

        if not pr_info.node_id:
            self.log.debug(
                "Cannot enable auto-merge for %s/%s#%s: missing node_id",
                owner,
                repo,
                pr_info.number,
            )
            return False

        pr_key = f"{owner}/{repo}#{pr_info.number}"

        # Already enabled in this run — skip duplicate API call
        if pr_key in self._auto_merge_enabled:
            return True

        cache_key = f"{owner}/{repo}"
        merge_method = self._pr_merge_methods.get(
            cache_key, self.default_merge_method
        )

        enabled = await self._github_client.enable_auto_merge(
            pr_info.node_id, merge_method
        )
        if not enabled:
            # The GraphQL mutation reports failure when auto-merge
            # is already active on the PR (the response omits
            # ``autoMergeRequest`` or the request raises). Check
            # the PR's current auto-merge state via REST so the
            # Step 6 skip gate still routes the PR to
            # ``AUTO_MERGE_PENDING`` instead of falling through to
            # a manual merge attempt that would 405 on pending
            # required checks.
            try:
                pr_payload = await self._github_client.get(
                    f"/repos/{owner}/{repo}/pulls/{pr_info.number}"
                )
            except Exception as exc:
                self.log.debug(
                    "Could not refresh PR %s to check existing "
                    "auto-merge state: %s",
                    pr_key,
                    exc,
                )
                pr_payload = None

            if (
                isinstance(pr_payload, dict)
                and pr_payload.get("auto_merge") is not None
            ):
                self._auto_merge_enabled.add(pr_key)
                self.log.debug(
                    "Auto-merge already active for %s; treating "
                    "as enabled (no audit comment posted)",
                    pr_key,
                )
                # Skip the audit comment in this branch —
                # someone (a previous run, the author, or the
                # repo's auto-merge bot) already enabled it; we
                # don't want to post a duplicate comment every
                # time dependamerge runs.
                return True
            return False

        self._auto_merge_enabled.add(pr_key)
        self.log.debug(
            "Auto-merge enabled for %s (method=%s)",
            pr_key,
            merge_method,
        )
        # Post a visible audit-trail comment so reviewers can
        # see at a glance that dependamerge enabled auto-merge
        # on the PR.
        audit_comment = (
            "🤖 Dependamerge\n"
            "Enabled auto-merge due to pending updates/checks ⏳"
        )
        await self._post_pr_comment_with_retry(
            owner, repo, pr_info.number, pr_info.html_url, audit_comment
        )
        return True

    async def _check_merge_requirements(
        self, pr_info: PullRequestInfo
    ) -> tuple[bool, str]:
        """
        Check if a PR meets all requirements for merging, including branch protection rules.

        Args:
            pr_info: Pull request information

        Returns:
            Tuple of (can_merge: bool, reason: str)
        """
        if not self._github_client:
            return False, "GitHub client not initialized"

        repo_owner, repo_name = pr_info.repository_full_name.split("/")

        try:
            # Check branch protection rules
            base_branch = pr_info.base_branch or "main"
            protection_rules = await self._github_client.get_branch_protection(
                repo_owner, repo_name, base_branch
            )

            if protection_rules:
                # Check required reviews
                required_reviews = protection_rules.get(
                    "required_pull_request_reviews", {}
                )
                if required_reviews:
                    require_code_owner = required_reviews.get(
                        "require_code_owner_reviews", False
                    )

                    # If code owner reviews are required, our automated approval might not be sufficient
                    if require_code_owner:
                        # Check if user wants to bypass code owner checks
                        if self.force_level in [
                            "code-owners",
                            "protection-rules",
                            "all",
                        ]:
                            # Only log during preview evaluation to avoid duplicate messages
                            if self.preview_mode:
                                self.log.warning(
                                    f"⚠️  Bypassing code owner review requirement for {repo_owner}/{repo_name}#{pr_info.number} (--force={self.force_level})"
                                )
                            return (
                                True,
                                "code owner review requirement bypassed by force level",
                            )
                        else:
                            return (
                                False,
                                "code owner reviews are required - cannot auto-approve",
                            )

        except Exception:
            # Don't fail the merge attempt if we can't check protection rules
            pass

        # Test merge capability to detect hidden branch protection rules
        try:
            # Use pre-determined merge method for this repository
            cache_key = f"{repo_owner}/{repo_name}"
            merge_method = self._pr_merge_methods.get(
                cache_key, self.default_merge_method
            )

            # Attempt a test merge to detect hidden branch protection rules
            test_result = await self._test_merge_capability(
                repo_owner, repo_name, pr_info.number, merge_method
            )
            if not test_result[0]:
                # Check if we should bypass protection rules
                if self.force_level in ["code-owners", "protection-rules", "all"]:
                    # Check if user has permissions to bypass before attempting
                    if self._github_client:
                        self.log.debug(
                            f"Checking bypass permissions for {repo_owner}/{repo_name} with force_level={self.force_level}"
                        )
                        (
                            can_bypass,
                            bypass_reason,
                        ) = await self._github_client.check_user_can_bypass_protection(
                            repo_owner, repo_name, self.force_level
                        )
                        self.log.debug(
                            f"Bypass check result: can_bypass={can_bypass}, reason={bypass_reason}"
                        )
                        if not can_bypass:
                            self.log.warning(
                                f"Cannot bypass branch protection for {repo_owner}/{repo_name}#{pr_info.number}: {bypass_reason}"
                            )
                            return (
                                False,
                                f"cannot bypass branch protection: {bypass_reason}",
                            )

                    # Only log during preview evaluation to avoid duplicate messages
                    if self.preview_mode:
                        self.log.warning(
                            f"⚠️  Bypassing branch protection check for {repo_owner}/{repo_name}#{pr_info.number}: {test_result[1]} (--force={self.force_level})"
                        )
                    # When bypassing, return early to allow merge to proceed
                    return (
                        True,
                        f"branch protection check bypassed (--force={self.force_level})",
                    )
                else:
                    return False, test_result[1]

        except Exception as e:
            # If we can't test merge, continue with other checks
            self.log.debug(
                f"Could not test merge capability for {repo_owner}/{repo_name}#{pr_info.number}: {e}"
            )

        # Additional checks based on PR state
        if pr_info.mergeable_state == "blocked":
            # Check if Copilot comments might be the blocker
            if self.dismiss_copilot and self._copilot_handler:
                has_copilot_comments = (
                    self._copilot_handler.has_blocking_copilot_comments(pr_info)
                )
                if has_copilot_comments:
                    return (
                        True,
                        "PR blocked but has Copilot comments that will be dismissed",
                    )

            # For blocked PRs, if mergeable is True, it just needs approval - we can handle that
            if pr_info.mergeable is True:
                return True, "PR ready for approval and merge"
            else:
                # If mergeable is False and state is blocked, it's blocked by failing checks
                if self.force_level == "all":
                    # Only log during preview evaluation to avoid duplicate messages
                    if self.preview_mode:
                        self.log.warning(
                            f"⚠️  Bypassing failing status checks for {repo_owner}/{repo_name}#{pr_info.number} (--force=all)"
                        )
                    return True, "PR blocked but forcing merge attempt (--force=all)"
                else:
                    # Don't hard-fail here: let Step 5.5 enable
                    # auto-merge and route to AUTO_MERGE_PENDING.
                    # The block reason might be "failing required
                    # check" right now but the check could still
                    # complete successfully — GitHub returns
                    # ``mergeable=False`` transiently for several
                    # reasons (still computing, non-required check
                    # failed). Step 5.5's analyze_block_reason
                    # pre-check still weeds out genuinely-stuck
                    # cases (missing approvals, etc.).
                    return (
                        True,
                        "PR blocked — Step 5.5 will enable auto-merge",
                    )
        elif pr_info.mergeable_state == "behind":
            if not self.fix_out_of_date:
                if self.force_level == "all":
                    self.log.warning(
                        f"⚠️  Attempting merge despite being behind for {repo_owner}/{repo_name}#{pr_info.number} (--force=all)"
                    )
                    return True, "PR behind but forcing merge attempt (--force=all)"
                else:
                    # Don't hard-fail when behind + --no-fix: the
                    # user opted out of *us* rebasing the branch,
                    # but enabling auto-merge in Step 5.5 is a
                    # separate, non-rewriting operation. If a third
                    # party (Dependabot, pre-commit-ci) rebases the
                    # PR while we wait, auto-merge will fire.
                    return (
                        True,
                        "PR behind — Step 5.5 will enable auto-merge",
                    )
            else:
                return True, "PR is behind - will rebase before merge"
        elif pr_info.mergeable_state == "unstable":
            # ``unstable`` means a non-required check failed but
            # the PR is otherwise mergeable. Auto-merge can still
            # fire because non-required checks don't block branch
            # protection. Let Step 5.5 handle it.
            return (
                True,
                "PR unstable — Step 5.5 will enable auto-merge",
            )
        elif pr_info.mergeable_state == "dirty":
            if self.force_level == "all":
                self.log.warning(
                    f"⚠️  Attempting merge despite conflicts for {repo_owner}/{repo_name}#{pr_info.number} (--force=all)"
                )
                return True, "PR has conflicts but forcing merge attempt (--force=all)"
            else:
                return (False, "merge conflicts")

        return True, "All merge requirements appear to be met"

    async def _trigger_stale_precommit_ci(self, pr_info: PullRequestInfo) -> bool:
        """
        Detect and retrigger a stuck pre-commit.ci run by posting a comment.

        pre-commit.ci uses the commit status API and sometimes fails to report
        any status at all, leaving the PR permanently blocked when
        'pre-commit.ci - pr' is a required status check. Posting the comment
        ``pre-commit.ci run`` triggers a fresh run.

        Args:
            pr_info: Pull request information

        Returns:
            True if a retrigger comment was posted and the status check
            subsequently completed, False otherwise.
        """
        if not self._github_client:
            return False

        repo_owner, repo_name = pr_info.repository_full_name.split("/", 1)
        precommit_context = "pre-commit.ci - pr"

        # 1. Check whether pre-commit.ci is a required status check
        try:
            required_checks = await self._github_client.get_required_status_checks(
                repo_owner, repo_name, pr_info.base_branch or "main"
            )
            required_contexts = [
                c.get("context", "") for c in required_checks if isinstance(c, dict)
            ]
            if precommit_context not in required_contexts:
                return False
        except Exception:
            return False

        # 2. Check whether the status has already been reported
        try:
            status_data = await self._github_client.get(
                f"/repos/{repo_owner}/{repo_name}/commits/{pr_info.head_sha}/status"
            )
            if isinstance(status_data, dict):
                for s in status_data.get("statuses", []):
                    if isinstance(s, dict) and s.get("context") == precommit_context:
                        # Status exists (success, pending, failure, etc.) — not stale
                        return False
        except Exception as e:
            self.log.debug(
                "Failed to fetch commit status for pre-commit.ci check on %s#%s "
                "(sha=%s); skipping retrigger: %s",
                pr_info.repository_full_name,
                pr_info.number,
                pr_info.head_sha,
                e,
            )
            return False

        # 3. Status is missing entirely — check for an existing trigger comment
        # before posting a duplicate (avoids spam if dependamerge runs repeatedly
        # while the status is still missing).
        try:
            comments = await self._github_client.get(
                f"/repos/{repo_owner}/{repo_name}/issues/{pr_info.number}/comments?per_page=100"
            )
            if isinstance(comments, list):
                for c in comments:
                    if not isinstance(c, dict):
                        continue
                    body = c.get("body")
                    if isinstance(body, str) and body.strip() == "pre-commit.ci run":
                        self.log.info(
                            "Found existing pre-commit.ci trigger comment on "
                            f"{pr_info.repository_full_name}#{pr_info.number}; "
                            "skipping duplicate comment."
                        )
                        return False
        except Exception:
            # If we fail to list comments, continue and attempt to post the
            # trigger anyway.
            pass

        log_and_print(
            self.log,
            self._console,
            f"⏳ Triggering pre-commit.ci re-run: {pr_info.html_url} "
            "[status never reported]",
            level="info",
        )

        try:
            await self._github_client.post_issue_comment(
                repo_owner, repo_name, pr_info.number, "pre-commit.ci run"
            )
        except Exception as e:
            self.log.warning(
                f"Failed to post pre-commit.ci trigger comment on "
                f"{pr_info.repository_full_name}#{pr_info.number}: {e}"
            )
            return False

        # 4. Poll for the status to appear (up to ~5 minutes)
        # pre-commit.ci can take up to five minutes to run and report back,
        # so we need a generous timeout to avoid prematurely marking PRs as
        # unmergeable when the check simply hasn't finished yet.
        max_polls = self._merge_poll_max_attempts
        for attempt in range(max_polls):
            await asyncio.sleep(self._merge_recheck_interval)
            try:
                status_data = await self._github_client.get(
                    f"/repos/{repo_owner}/{repo_name}/commits/{pr_info.head_sha}/status"
                )
                if isinstance(status_data, dict):
                    for s in status_data.get("statuses", []):
                        if not isinstance(s, dict):
                            continue
                        if s.get("context") != precommit_context:
                            continue
                        state = s.get("state")
                        if state == "success":
                            log_and_print(
                                self.log,
                                self._console,
                                f"✅ pre-commit.ci passed: {pr_info.html_url}",
                                level="info",
                            )
                            return True
                        elif state in ("failure", "error"):
                            log_and_print(
                                self.log,
                                self._console,
                                f"❌ pre-commit.ci failed: {pr_info.html_url}",
                                level="warning",
                            )
                            return False
                        # state == "pending" — keep polling
            except Exception as e:
                self.log.debug(
                    "Failed to poll pre-commit.ci status for %s: %s",
                    f"{pr_info.repository_full_name}#{pr_info.number}",
                    e,
                )

            if attempt == max_polls - 1:
                self.log.debug(
                    f"Still waiting for pre-commit.ci on "
                    f"{pr_info.repository_full_name}#{pr_info.number} "
                    f"({(attempt + 1) * self._merge_recheck_interval:.0f}s elapsed)"
                )

        self.log.warning(
            f"Timed out waiting for pre-commit.ci on "
            f"{pr_info.repository_full_name}#{pr_info.number}"
        )
        return False

    async def _trigger_dependabot_recreate(
        self, pr_info: PullRequestInfo
    ) -> PullRequestInfo | None:
        """
        Detect an unsigned dependabot commit and ask dependabot to recreate
        the pull request so that the new commit is properly signed.

        When a repository's branch protection requires commit signatures,
        dependabot PRs can end up with unverified commits (e.g. after a
        rebase or force-push by GitHub).  Posting ``@dependabot recreate``
        causes dependabot to close the current PR and open a fresh one
        whose commit is signed by GitHub.

        Args:
            pr_info: Pull request information for the failing PR.

        Returns:
            A new ``PullRequestInfo`` for the recreated PR if the recreate
            was triggered, the old PR was closed, and a replacement was
            found.  Returns ``None`` if the recreate was not applicable or
            did not succeed within the polling window.
        """
        if not self._github_client:
            return None

        repo_owner, repo_name = pr_info.repository_full_name.split("/", 1)

        # 1. Only applies to dependabot PRs
        if pr_info.author != "dependabot[bot]":
            return None

        # 2. Check whether the branch requires signed commits
        try:
            requires_signatures = await self._github_client.requires_commit_signatures(
                repo_owner, repo_name, pr_info.base_branch or "main"
            )
            if not requires_signatures:
                self.log.debug(
                    "Branch %s/%s:%s does not require commit signatures; "
                    "skipping dependabot recreate.",
                    repo_owner,
                    repo_name,
                    pr_info.base_branch or "main",
                )
                return None
        except Exception as e:
            self.log.debug(
                "Could not determine signature requirement for %s: %s",
                pr_info.repository_full_name,
                e,
            )
            return None

        # 3. Check whether any commits are unverified
        try:
            (
                all_verified,
                unverified_shas,
            ) = await self._github_client.check_pr_commit_signatures(
                repo_owner, repo_name, pr_info.number
            )
            if all_verified:
                self.log.debug(
                    "All commits on %s#%s are verified; recreate not needed.",
                    pr_info.repository_full_name,
                    pr_info.number,
                )
                return None
        except Exception as e:
            self.log.debug(
                "Could not check commit signatures for %s#%s: %s",
                pr_info.repository_full_name,
                pr_info.number,
                e,
            )
            return None

        # 4. Guard against duplicate recreate comments
        try:
            comments = await self._github_client.get(
                f"/repos/{repo_owner}/{repo_name}/issues/{pr_info.number}/comments"
                f"?per_page=100&direction=desc"
            )
            if isinstance(comments, list):
                for c in comments:
                    if not isinstance(c, dict):
                        continue
                    body = c.get("body")
                    if isinstance(body, str) and "@dependabot recreate" in body:
                        self.log.info(
                            "Found existing @dependabot recreate comment on "
                            "%s#%s; skipping duplicate.",
                            pr_info.repository_full_name,
                            pr_info.number,
                        )
                        return None
        except Exception as e:
            self.log.warning(
                "Could not list comments for %s#%s to check for existing "
                "@dependabot recreate comment: %s",
                pr_info.repository_full_name,
                pr_info.number,
                e,
            )
            return None

        # 5. Post the recreate comment
        log_and_print(
            self.log,
            self._console,
            f"🔄 Requesting dependabot recreate: {pr_info.html_url} "
            f"[unverified commits: {', '.join(unverified_shas)}]",
            level="info",
        )

        try:
            await self._github_client.post_issue_comment(
                repo_owner, repo_name, pr_info.number, "@dependabot recreate"
            )
        except Exception as e:
            self.log.warning(
                "Failed to post @dependabot recreate comment on %s#%s: %s",
                pr_info.repository_full_name,
                pr_info.number,
                e,
            )
            return None

        # 6. Poll for the old PR to close and a replacement to appear.
        #    Dependabot typically responds within 30-90 seconds.
        #    We poll using the centralised merge timeout.
        max_polls = self._merge_poll_max_attempts
        old_pr_closed = False

        for attempt in range(max_polls):
            await asyncio.sleep(self._merge_recheck_interval)

            # 6a. Check if the old PR has been closed
            if not old_pr_closed:
                try:
                    old_pr_data = await self._github_client.get(
                        f"/repos/{repo_owner}/{repo_name}/pulls/{pr_info.number}"
                    )
                    if isinstance(old_pr_data, dict):
                        if old_pr_data.get("state") == "closed":
                            old_pr_closed = True
                            log_and_print(
                                self.log,
                                self._console,
                                f"✅ Old PR closed by dependabot: "
                                f"{pr_info.html_url} "
                                f"({(attempt + 1) * self._merge_recheck_interval:.0f}s elapsed)",
                                level="info",
                            )
                except Exception as e:
                    self.log.debug(
                        "Error polling old PR state for %s#%s: %s",
                        pr_info.repository_full_name,
                        pr_info.number,
                        e,
                    )

            # 6b. Once the old PR is closed, look for the replacement
            if old_pr_closed:
                try:
                    # Search for open PRs from dependabot on the same head branch
                    prs = await self._github_client.get(
                        f"/repos/{repo_owner}/{repo_name}/pulls"
                        f"?state=open&head={repo_owner}:{pr_info.head_branch}&per_page=5"
                    )
                    if isinstance(prs, list):
                        for pr_data in prs:
                            if not isinstance(pr_data, dict):
                                continue
                            pr_author = pr_data.get("user", {}).get("login", "")
                            if pr_author != "dependabot[bot]":
                                continue

                            new_number = pr_data.get("number")
                            if new_number is None or new_number == pr_info.number:
                                continue

                            # Verify the replacement targets the same base branch
                            new_base = pr_data.get("base", {}).get("ref", "")
                            if new_base != (pr_info.base_branch or "main"):
                                self.log.debug(
                                    "Skipping candidate PR #%s: targets %s, "
                                    "expected %s",
                                    new_number,
                                    new_base,
                                    pr_info.base_branch or "main",
                                )
                                continue

                            # Found a replacement — now wait for checks to pass
                            new_pr_info = await self._wait_for_recreated_pr_checks(
                                repo_owner, repo_name, new_number, pr_data
                            )
                            # Always return after the first wait attempt to avoid
                            # performing multiple long waits for the same PR.
                            return new_pr_info
                except Exception as e:
                    self.log.debug(
                        "Error searching for replacement PR for %s#%s: %s",
                        pr_info.repository_full_name,
                        pr_info.number,
                        e,
                    )

            if attempt % 3 == 2:
                self.log.debug(
                    "Still waiting for dependabot recreate on %s#%s (%.0fs elapsed, old_pr_closed=%s)",
                    pr_info.repository_full_name,
                    pr_info.number,
                    (attempt + 1) * self._merge_recheck_interval,
                    old_pr_closed,
                )

        self.log.warning(
            "Timed out waiting for dependabot to recreate %s#%s",
            pr_info.repository_full_name,
            pr_info.number,
        )
        return None

    async def _wait_for_recreated_pr_checks(
        self,
        repo_owner: str,
        repo_name: str,
        new_number: int,
        pr_data: dict[str, Any],
    ) -> PullRequestInfo | None:
        """
        Wait for the recreated PR's status checks to complete.

        Polls the new PR using the shared merge timeout settings. The
        total wait here is controlled by ``self._merge_poll_max_attempts
        * self._merge_recheck_interval`` (default: ~5 minutes), so
        ``--merge-timeout`` also affects this loop.

        Args:
            repo_owner: Repository owner.
            repo_name: Repository name.
            new_number: The PR number of the recreated pull request.
            pr_data: The initial PR data dict from the GitHub API.

        Returns:
            A ``PullRequestInfo`` if the PR became mergeable, None on timeout.
        """
        if not self._github_client:
            return None

        full_name = f"{repo_owner}/{repo_name}"
        html_url = pr_data.get(
            "html_url", f"https://github.com/{full_name}/pull/{new_number}"
        )

        log_and_print(
            self.log,
            self._console,
            f"🔍 Found recreated PR, waiting for checks: {html_url}",
            level="info",
        )

        # Enable auto-merge on the recreated PR so it merges
        # even if we time out waiting for status checks.
        if pr_data.get("node_id"):
            # We don't have a full PullRequestInfo yet, but we can
            # construct a minimal one for the auto-merge helper.
            _tmp_pr = PullRequestInfo(
                number=new_number,
                node_id=pr_data.get("node_id"),
                title=pr_data.get("title", ""),
                body=pr_data.get("body"),
                author=((pr_data.get("user") or {}).get("login", "")),
                head_sha=((pr_data.get("head") or {}).get("sha", "")),
                base_branch=((pr_data.get("base") or {}).get("ref", "")),
                head_branch=((pr_data.get("head") or {}).get("ref", "")),
                state="open",
                mergeable=None,
                mergeable_state=None,
                behind_by=None,
                files_changed=[],
                repository_full_name=full_name,
                html_url=html_url,
            )
            await self._enable_auto_merge_for_pr(_tmp_pr, repo_owner, repo_name)

        # Poll for the new PR to become mergeable
        max_check_polls = self._merge_poll_max_attempts
        for check_attempt in range(max_check_polls):
            await asyncio.sleep(self._merge_recheck_interval)
            try:
                refreshed = await self._github_client.get(
                    f"/repos/{repo_owner}/{repo_name}/pulls/{new_number}"
                )
                if not isinstance(refreshed, dict):
                    continue

                mergeable = refreshed.get("mergeable")
                mergeable_state = refreshed.get("mergeable_state")

                if mergeable_state == "clean" or (
                    mergeable is True and mergeable_state in ("clean", "unstable")
                ):
                    log_and_print(
                        self.log,
                        self._console,
                        f"✅ Recreated PR is ready to merge: {html_url}",
                        level="info",
                    )
                    # Build a PullRequestInfo for the new PR
                    from .models import FileChange

                    files_changed: list[FileChange] = []
                    try:
                        async for files_data in self._github_client.get_paginated(
                            f"/repos/{repo_owner}/{repo_name}/pulls/{new_number}/files",
                            per_page=100,
                        ):
                            if isinstance(files_data, list):
                                for f in files_data:
                                    if isinstance(f, dict):
                                        files_changed.append(
                                            FileChange(
                                                filename=f.get("filename", ""),
                                                additions=int(f.get("additions", 0)),
                                                deletions=int(f.get("deletions", 0)),
                                                changes=int(f.get("changes", 0)),
                                                status=f.get("status", "modified"),
                                            )
                                        )
                    except Exception as e:
                        self.log.debug(
                            "Failed to fetch files for recreated PR %s#%s: %s",
                            f"{repo_owner}/{repo_name}",
                            new_number,
                            e,
                        )

                    return PullRequestInfo(
                        number=new_number,
                        node_id=refreshed.get("node_id"),
                        title=refreshed.get("title", ""),
                        body=refreshed.get("body"),
                        author=((refreshed.get("user") or {}).get("login", "")),
                        head_sha=((refreshed.get("head") or {}).get("sha", "")),
                        base_branch=((refreshed.get("base") or {}).get("ref", "")),
                        head_branch=((refreshed.get("head") or {}).get("ref", "")),
                        state=refreshed.get("state", "open"),
                        mergeable=mergeable,
                        mergeable_state=mergeable_state,
                        behind_by=None,
                        files_changed=files_changed,
                        repository_full_name=full_name,
                        html_url=html_url,
                    )

                if mergeable_state == "dirty":
                    self.log.warning(
                        "Recreated PR %s#%s has merge conflicts; aborting wait.",
                        full_name,
                        new_number,
                    )
                    return None

                # blocked / behind / unknown — keep polling
                if check_attempt % 3 == 2:
                    self.log.debug(
                        "Waiting for checks on recreated PR %s#%s "
                        "(state=%s, %.0fs elapsed)",
                        full_name,
                        new_number,
                        mergeable_state,
                        (check_attempt + 1) * self._merge_recheck_interval,
                    )

            except Exception as e:
                self.log.debug(
                    "Error polling recreated PR %s#%s: %s",
                    full_name,
                    new_number,
                    e,
                )

        self.log.warning(
            "Timed out waiting for checks on recreated PR %s#%s",
            full_name,
            new_number,
        )
        return None

    async def _approve_pr(self, owner: str, repo: str, pr_number: int) -> bool:
        """
        Approve a pull request if not already approved by the current user or sufficiently approved.

        Args:
            owner: Repository owner
            repo: Repository name
            pr_number: Pull request number

        Returns:
            True if approval was added, False if already approved/sufficient

        Raises:
            Exception: If approval fails
        """
        if not self._github_client:
            raise RuntimeError("GitHub client not initialized")

        try:
            # Check if current user has already approved this PR
            pr_data = await self._github_client.get(
                f"/repos/{owner}/{repo}/pulls/{pr_number}"
            )

            if isinstance(pr_data, dict):
                # Get current user login
                user_data = await self._github_client.get("/user")
                current_user = (
                    user_data.get("login") if isinstance(user_data, dict) else None
                )

                if current_user:
                    # Check existing reviews
                    reviews_data = await self._github_client.get(
                        f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
                    )

                    if isinstance(reviews_data, list):
                        # Look for existing approval by current user
                        for review in reviews_data:
                            if (
                                review.get("user", {}).get("login") == current_user
                                and review.get("state") == "APPROVED"
                            ):
                                self.log.debug(
                                    f"⏩ Already approved: {owner}/{repo}#{pr_number} [{current_user}]"
                                )
                                return False

                        # Check if PR already has sufficient approvals from others
                        approved_reviews = [
                            review
                            for review in reviews_data
                            if review.get("state") == "APPROVED"
                            and review.get("user", {}).get("login") != current_user
                        ]

                        if (
                            approved_reviews
                            and pr_data.get("mergeable_state") == "clean"
                        ):
                            # Get list of approvers
                            approvers = [
                                review.get("user", {}).get("login", "unknown")
                                for review in approved_reviews
                            ]
                            approvers_str = ", ".join(approvers)
                            self.log.debug(
                                f"⏩ Already approved: {owner}/{repo}#{pr_number} [{approvers_str}]"
                            )
                            return False

            await self._github_client.approve_pull_request(
                owner,
                repo,
                pr_number,
                "🤖 Dependamerge\nApproved this pull request ✅",
            )
            return True
        except Exception as e:
            # Handle specific error codes
            error_str = str(e)

            # Check for 403 Forbidden - missing pull request review permissions
            if "403" in error_str and "Forbidden" in error_str:
                raise RuntimeError(
                    f"Failed to approve PR {owner}/{repo}#{pr_number}: Missing 'Pull requests: Read and write' permission. "
                    f"For fine-grained tokens, enable 'Pull requests: Read and write' access. "
                    f"For classic tokens, ensure 'repo' scope is enabled."
                ) from e
            elif "422" in error_str and "Unprocessable Entity" in error_str:
                # This usually means the PR can't be approved (e.g., already approved by user, or other restrictions)
                self.log.debug(
                    f"⏩ Already approved: {owner}/{repo}#{pr_number} [cannot approve - already approved or restricted]"
                )
                return False
            else:
                raise RuntimeError(
                    f"Failed to approve PR {owner}/{repo}#{pr_number}: {e}"
                ) from e

    async def _merge_pr_with_retry(
        self, pr_info: PullRequestInfo, owner: str, repo: str
    ) -> bool:
        """
        Merge a PR with retry logic for transient failures.

        Args:
            pr_info: Pull request information
            owner: Repository owner
            repo: Repository name

        Returns:
            True if merged successfully, False otherwise
        """
        if not self._github_client:
            raise RuntimeError("GitHub client not initialized")

        for attempt in range(self.max_retries + 1):
            try:
                # Check if PR has already been closed/merged before attempting
                if attempt > 0:
                    # Re-fetch PR state to check if it was merged by a previous attempt
                    # or by external processes
                    try:
                        current_pr_data = await self._github_client.get(
                            f"/repos/{owner}/{repo}/pulls/{pr_info.number}"
                        )
                        if isinstance(current_pr_data, dict):
                            current_state = current_pr_data.get("state")
                            current_merged = current_pr_data.get("merged", False)

                            if current_state == "closed" and current_merged:
                                self.log.info(
                                    f"✅ PR {owner}/{repo}#{pr_info.number} was already merged, skipping retry"
                                )
                                return True
                            elif current_state == "closed" and not current_merged:
                                self.log.info(
                                    f"⚠️ PR {owner}/{repo}#{pr_info.number} was closed without merging, aborting retry"
                                )
                                # This will be caught by the outer merge logic and formatted consistently
                                return False
                    except Exception as state_check_error:
                        self.log.debug(
                            f"Failed to check PR state before retry {attempt + 1}: {state_check_error}"
                        )

                # Use pre-determined merge method for this repository
                cache_key = f"{owner}/{repo}"
                merge_method = self._pr_merge_methods.get(
                    cache_key, self.default_merge_method
                )

                # Attempt the merge
                self.log.debug(
                    f"Attempting merge for {owner}/{repo}#{pr_info.number} with method={merge_method}"
                )
                merged = await self._github_client.merge_pull_request(
                    owner, repo, pr_info.number, merge_method
                )
                self.log.debug(
                    f"Merge API returned {merged} for {owner}/{repo}#{pr_info.number}"
                )

                if merged:
                    return True

                # Merge failed, check if we can fix it
                self.log.warning(
                    f"⚠️ Merge API returned false for PR {owner}/{repo}#{pr_info.number} (attempt {attempt + 1})"
                )
                if attempt < self.max_retries:
                    should_retry = await self._handle_merge_failure(
                        pr_info, owner, repo
                    )
                    if should_retry:
                        self.log.info(
                            f"Retrying merge for PR {owner}/{repo}#{pr_info.number} (attempt {attempt + 2})"
                        )
                        continue
                    else:
                        self.log.info(
                            f"Not retrying PR {owner}/{repo}#{pr_info.number} - no fixable issues found"
                        )
                        break

            except Exception as e:
                error_msg = str(e)

                # Store exception for better error reporting
                pr_key = f"{owner}/{repo}#{pr_info.number}"
                self._last_merge_exception[pr_key] = e
                self.log.debug(
                    f"Stored exception for {pr_key}: {type(e).__name__}: {str(e)[:200]}"
                )

                # Enhanced error handling with specific status code checks
                if "405" in error_msg and "Method Not Allowed" in error_msg:
                    # Don't log here - will be handled in failure summary
                    if "behind" in error_msg.lower() and self.fix_out_of_date:
                        # Allow retry for behind PRs
                        pass
                    elif pr_info.mergeable_state in ("clean", "unstable"):
                        # The PR should be mergeable but GitHub returned 405 —
                        # this is a transient API error (often follows a 502
                        # during GitHub degradation).  Re-fetch state and retry.
                        if attempt < self.max_retries:
                            retry_delay = 3.0 * (attempt + 1)
                            self.log.info(
                                f"Transient 405 on mergeable PR {pr_key} "
                                f"(state={pr_info.mergeable_state}), "
                                f"waiting {retry_delay}s before retry "
                                f"(attempt {attempt + 1}/{self.max_retries + 1})…"
                            )
                            await asyncio.sleep(retry_delay)
                            # Refresh PR state in case something changed
                            try:
                                if self._github_client:
                                    refreshed = await self._github_client.get(
                                        f"/repos/{owner}/{repo}/pulls/{pr_info.number}"
                                    )
                                    if isinstance(refreshed, dict):
                                        pr_info.mergeable = refreshed.get("mergeable")
                                        pr_info.mergeable_state = refreshed.get(
                                            "mergeable_state"
                                        )
                                        self.log.debug(
                                            f"Refreshed {pr_key}: mergeable={pr_info.mergeable}, "
                                            f"mergeable_state={pr_info.mergeable_state}"
                                        )
                            except Exception as refresh_err:
                                self.log.debug(
                                    f"Failed to refresh PR state for {pr_key}: {refresh_err}"
                                )
                            continue
                        else:
                            break
                    elif pr_info.mergeable_state == "blocked":
                        # If we just approved this PR, the branch protection
                        # evaluator may not have caught up yet.  Re-fetch the
                        # PR state and, if it has become "clean", allow a retry
                        # instead of giving up immediately.
                        if (
                            pr_key in self._recently_approved
                            and attempt < self.max_retries
                        ):
                            try:
                                if self._github_client:
                                    if self._post_approval_delay <= 0:
                                        retry_delay = 0.0
                                    else:
                                        retry_delay = self._post_approval_delay + 2.0
                                    self.log.info(
                                        f"Post-approval propagation retry for {pr_key}, "
                                        f"waiting {retry_delay}s before re-checking…"
                                    )
                                    if retry_delay > 0:
                                        await asyncio.sleep(retry_delay)
                                    refreshed = await self._github_client.get(
                                        f"/repos/{owner}/{repo}/pulls/{pr_info.number}"
                                    )
                                    if isinstance(refreshed, dict):
                                        new_state = refreshed.get("mergeable_state")
                                        new_mergeable = refreshed.get("mergeable")
                                        self.log.info(
                                            f"Refreshed {pr_key}: mergeable={new_mergeable}, "
                                            f"mergeable_state={new_state}"
                                        )
                                        pr_info.mergeable = new_mergeable
                                        pr_info.mergeable_state = new_state
                                        if new_state == "clean":
                                            # Approval has propagated — retry the merge
                                            continue
                            except Exception as refresh_err:
                                self.log.debug(
                                    f"Failed to refresh PR state for {pr_key}: {refresh_err}"
                                )
                            # Remove from recently-approved so we don't loop forever
                            self._recently_approved.discard(pr_key)
                        # Still blocked after re-check (or not recently approved)
                        break
                    else:
                        # Don't retry 405 errors unless they're "behind" issues
                        break
                elif "403" in error_msg and "Forbidden" in error_msg:
                    break
                elif "422" in error_msg:
                    break
                else:
                    # Only log for debugging purposes
                    self.log.debug(
                        f"Merge attempt {attempt + 1} failed for PR {owner}/{repo}#{pr_info.number}: {e}"
                    )

                if attempt >= self.max_retries:
                    break

                # Don't retry certain error types that are unlikely to be transient
                # Exception: Allow retry for 405 errors on "behind" PRs if fix_out_of_date is enabled
                if ("405" in error_msg and "behind" not in error_msg.lower()) or (
                    "422" in error_msg and "not mergeable" in error_msg.lower()
                ):
                    self.log.info(
                        f"Not retrying PR {owner}/{repo}#{pr_info.number} due to permanent error condition"
                    )
                    break
                elif (
                    "405" in error_msg
                    and "behind" in error_msg.lower()
                    and not self.fix_out_of_date
                ):
                    self.log.info(
                        f"Not retrying PR {owner}/{repo}#{pr_info.number} - behind base branch but --no-fix is set"
                    )
                    break

                # Wait a bit before retrying
                await asyncio.sleep(1.0)

        self.log.debug(
            f"_merge_pr_with_retry returning False for {owner}/{repo}#{pr_info.number} after all retries"
        )
        return False

    def _get_failure_summary(self, pr_info: PullRequestInfo) -> str:
        """
        Generate a detailed failure summary based on PR state.

        Args:
            pr_info: Pull request information

        Returns:
            Detailed description of why the merge failed
        """
        # Check if we have a stored exception for this PR
        pr_key = f"{pr_info.repository_full_name}#{pr_info.number}"
        last_exception = self._last_merge_exception.get(pr_key)
        self.log.debug(
            f"_get_failure_summary called for {pr_key}, mergeable_state={pr_info.mergeable_state}, mergeable={pr_info.mergeable}, has_exception={last_exception is not None}"
        )
        if last_exception:
            error_msg = str(last_exception)
            self.log.debug(f"Last exception for {pr_key}: {error_msg[:200]}")
            # Check for workflow scope error - be very specific to avoid false positives
            # Only match the exact error message pattern we raise in github_async.py
            if "Missing 'workflow' scope" in error_msg:
                return "missing 'workflow' token scope"
            # Check for other permission errors
            elif "403" in error_msg and "forbidden" in error_msg.lower():
                return "insufficient permissions"
            # Surface transient HTTP errors (502, 405 etc.) accurately instead
            # of falling through to infer a reason from mergeable_state, which
            # may be stale or misleading (e.g. "clean" → "branch protection").
            elif "405" in error_msg and "Method Not Allowed" in error_msg:
                if pr_info.mergeable_state in ("clean", "unstable"):
                    return (
                        "GitHub API returned transient 405 error "
                        "(PR appears mergeable — GitHub may be experiencing issues, "
                        "see https://www.githubstatus.com)"
                    )
                # For non-clean states, fall through to state-based analysis below
            elif "502" in error_msg:
                return (
                    "GitHub API returned 502 Bad Gateway "
                    "(GitHub may be experiencing issues, "
                    "see https://www.githubstatus.com)"
                )

        if pr_info.mergeable_state == "behind":
            return "behind base branch"
        elif pr_info.mergeable_state == "blocked":
            # Use detailed block analysis for blocked PRs
            try:
                from .github_client import GitHubClient

                client = GitHubClient(token=self.token)
                detailed_reason = client._analyze_block_reason(pr_info)
                # Convert the detailed reason to a more concise format for console output
                if detailed_reason.startswith("Blocked by failing check:"):
                    check_name = detailed_reason.replace(
                        "Blocked by failing check: ", ""
                    )
                    return f"failing check: {check_name}"
                elif (
                    detailed_reason.startswith("Blocked by")
                    and "failing checks" in detailed_reason
                ):
                    return detailed_reason.replace("Blocked by ", "").lower()
                elif "Human reviewer requested changes" in detailed_reason:
                    return "human reviewer requested changes"
                elif "Copilot" in detailed_reason:
                    return detailed_reason.replace("Blocked by ", "").lower()
                elif "branch protection" in detailed_reason.lower():
                    return "branch protection rules prevent merge"
                else:
                    return detailed_reason.replace("Blocked by ", "").lower()
            except Exception as e:
                self.log.debug(f"Failed to get detailed block reason: {e}")
                # Fallback to generic message
                pass

            # Fallback logic when detailed analysis fails
            if pr_info.mergeable is True:
                return "branch protection rules prevent merge"
            else:
                return "blocked by failing status checks"
        elif pr_info.mergeable_state == "dirty":
            return "merge conflicts"
        elif pr_info.mergeable_state == "draft":
            return "draft PR"
        elif pr_info.mergeable is False:
            return "cannot update protected ref - organization or branch protection rules prevent merge"
        elif pr_info.mergeable_state == "unknown":
            # For unknown state, try to get more details using the GitHub client
            try:
                from .github_client import GitHubClient

                client = GitHubClient(token=self.token)
                detailed_reason = client._analyze_block_reason(pr_info)
                if "failing check" in detailed_reason.lower():
                    if detailed_reason.startswith("Blocked by failing check:"):
                        check_name = detailed_reason.replace(
                            "Blocked by failing check: ", ""
                        )
                        return f"failing check: {check_name}"
                    else:
                        return detailed_reason.replace("Blocked by ", "").lower()
                else:
                    return detailed_reason.replace("Blocked by ", "").lower()
            except Exception as e:
                self.log.debug(f"Failed to analyze unknown state: {e}")
                return "status checks pending or failed"
        else:
            return f"merge failed: {pr_info.mergeable_state}"

    async def _get_merge_method_for_repo(self, owner: str, repo: str) -> str:
        """
        Get the appropriate merge method for a specific repository based on branch protection settings.

        Args:
            owner: Repository owner
            repo: Repository name

        Returns:
            Merge method to use: "merge", "squash", or "rebase"
        """
        if not self._github_service:
            self.log.warning("GitHubService not available, using default merge method")
            return self.default_merge_method

        try:
            # Get branch protection settings for main branch
            protection_settings = (
                await self._github_service.get_branch_protection_settings(
                    owner, repo, "main"
                )
            )

            # Determine appropriate merge method
            merge_method = self._github_service.determine_merge_method(
                protection_settings, self.default_merge_method
            )

            if merge_method != self.default_merge_method:
                self.log.debug(
                    f"Repository {owner}/{repo} requires '{merge_method}' merge method "
                    f"(protection: requiresLinearHistory={protection_settings and protection_settings.get('requiresLinearHistory', False)})"
                )

            return merge_method

        except Exception as e:
            self.log.warning(
                f"Failed to determine merge method for {owner}/{repo}, using default '{self.default_merge_method}': {e}"
            )
            return self.default_merge_method

    async def _handle_merge_failure(
        self, pr_info: PullRequestInfo, owner: str, repo: str
    ) -> bool:
        """
        Handle a merge failure and determine if we should retry.

        Args:
            pr_info: Pull request information
            owner: Repository owner
            repo: Repository name

        Returns:
            True if we should retry, False otherwise
        """
        if not self._github_client:
            return False

        # Check if the branch is out of date and we can fix it
        if self.fix_out_of_date and pr_info.mergeable_state == "behind":
            try:
                self.log.info(
                    f"PR {owner}/{repo}#{pr_info.number} is behind - updating branch"
                )
                await self._github_client.update_branch(owner, repo, pr_info.number)
                # Wait a moment for GitHub to process the update
                await asyncio.sleep(min(2.0, self._merge_recheck_interval))
                return True
            except Exception as e:
                self.log.error(
                    f"Failed to update branch for PR {owner}/{repo}#{pr_info.number}: {e}"
                )

        # For other failure types, don't retry
        return False

    async def _get_org_settings(self, owner: str) -> dict[str, Any] | None:
        """
        Get organization-level settings, with caching.

        Organization settings (e.g. web_commit_signoff_required) don't change
        between PRs in the same org, so we cache the result for the lifetime
        of the merge session.

        Args:
            owner: Organization/owner name

        Returns:
            Organization settings dict, or None if the lookup failed
        """
        # Fast path: no lock needed if already cached
        if owner in self._org_settings_cache:
            return self._org_settings_cache[owner]

        # Acquire a per-owner lock so concurrent lookups for the same
        # org are serialised, but lookups for *different* orgs proceed
        # in parallel without blocking each other.
        async with self._org_settings_locks_lock:
            if owner not in self._org_settings_locks:
                self._org_settings_locks[owner] = asyncio.Lock()
            owner_lock = self._org_settings_locks[owner]

        async with owner_lock:
            # Re-check after acquiring the per-owner lock (another
            # task may have populated the cache while we waited).
            if owner in self._org_settings_cache:
                return self._org_settings_cache[owner]

            if not self._github_client:
                return None

            try:
                org_data = await self._github_client.get(f"/orgs/{owner}")
                if isinstance(org_data, dict):
                    self._org_settings_cache[owner] = org_data
                    # Log org-level details once, not per-PR
                    web_commit_signoff = org_data.get(
                        "web_commit_signoff_required", False
                    )
                    if web_commit_signoff:
                        self.log.debug(
                            f"Organization {owner} requires commit signoff"
                        )
                    return org_data
                else:
                    self._org_settings_cache[owner] = None
                    return None
            except Exception as e:
                self.log.debug(f"Could not check organization settings for {owner}: {e}")
                self._org_settings_cache[owner] = None
                return None

    async def _test_merge_capability(
        self, owner: str, repo: str, pr_number: int, merge_method: str
    ) -> tuple[bool, str]:
        """
        Test if a PR can be merged by validating merge requirements.

        This helps detect branch protection rules that aren't visible through the API,
        such as organization-level restrictions.

        Args:
            owner: Repository owner
            repo: Repository name
            pr_number: Pull request number
            merge_method: Merge method to test

        Returns:
            Tuple of (can_merge: bool, reason: str)
        """
        if not self._github_client:
            return False, "GitHub client not initialized"

        try:
            # Check organization-level restrictions (cached per org)
            await self._get_org_settings(owner)

            # Note: Removed DCO signoff check as web_commit_signoff_required only affects
            # web-based commits, not PR merges. DCO enforcement for PRs is handled by
            # status checks/apps, not repository settings.

            # Check the PR's merge status through the API
            pr_data = await self._github_client.get(
                f"/repos/{owner}/{repo}/pulls/{pr_number}"
            )

            if isinstance(pr_data, dict):
                mergeable_state = pr_data.get("mergeable_state", "unknown")
                mergeable = pr_data.get("mergeable")
                head_sha = pr_data.get("head", {}).get("sha", "")

                self.log.debug(
                    f"PR {owner}/{repo}#{pr_number} REST API status: mergeable={mergeable}, mergeable_state={mergeable_state}"
                )

                # Check for specific blocking conditions that indicate protection rules
                if mergeable_state == "blocked" and mergeable is False:
                    # Before declaring the PR unmergeable, analyze WHY it's blocked.
                    # If the only blocker is "requires approval", the tool is about to
                    # provide that approval — so we should allow the merge to proceed.
                    # Note: we only call analyze_block_reason when mergeable is False
                    # to avoid unnecessary API traffic; when mergeable is True/None the
                    # code falls through to the pass-through return at the end.
                    block_reason = ""
                    if head_sha and self._github_client:
                        try:
                            block_reason = (
                                await self._github_client.analyze_block_reason(
                                    owner, repo, pr_number, head_sha
                                )
                            )
                            self.log.debug(
                                f"PR {owner}/{repo}#{pr_number} block reason: {block_reason}"
                            )
                        except Exception as analyze_err:
                            self.log.debug(
                                f"Could not analyze block reason for {owner}/{repo}#{pr_number}: {analyze_err}"
                            )

                    # If the PR is only blocked because it needs approval, allow it
                    # through — the tool will approve it before attempting merge.
                    if "requires approval" in block_reason.lower():
                        self.log.info(
                            f"PR {owner}/{repo}#{pr_number} is blocked pending approval — tool will approve before merge"
                        )
                        return True, "PR blocked pending approval (tool will approve)"

                    # For other blocking reasons, check force level
                    if self.force_level in ["code-owners", "protection-rules", "all"]:
                        self.log.info(
                            f"Force level '{self.force_level}' bypassing branch protection rules for {owner}/{repo}#{pr_number}"
                        )
                        return True, "branch protection bypassed by force level"
                    return (
                        False,
                        f"branch protection rules prevent merge ({block_reason or 'blocked'})",
                    )
                elif mergeable_state == "dirty":
                    return False, "merge conflicts"
                elif mergeable_state == "behind":
                    if not self.fix_out_of_date:
                        return (
                            False,
                            "PR is behind base branch and --no-fix option is set",
                        )
                    # Otherwise it's fixable
                elif mergeable is False and mergeable_state == "unknown":
                    # This often indicates hidden branch protection rules
                    if self.force_level in ["code-owners", "protection-rules", "all"]:
                        self.log.info(
                            f"Force level '{self.force_level}' bypassing hidden branch protection rules for {owner}/{repo}#{pr_number}"
                        )
                        return True, "hidden branch protection bypassed by force level"
                    return (
                        False,
                        "cannot update protected ref - organization or branch protection rules prevent merge",
                    )

            return True, "merge capability test passed"

        except Exception as e:
            error_msg = str(e)
            self.log.debug(
                f"Exception in _test_merge_capability for {owner}/{repo}#{pr_number}: {error_msg}"
            )

            # Look for specific DCO-related errors in the GitHub API response
            # DCO errors typically come as 422 validation errors with specific messages
            is_dco_error = False
            if "422" in error_msg and (
                "commit signoff required" in error_msg.lower()
                or "commits must have verified signatures" in error_msg.lower()
                or (
                    "dco" in error_msg.lower()
                    and ("required" in error_msg.lower() or "sign" in error_msg.lower())
                )
            ):
                is_dco_error = True
            elif "commit signoff required" in error_msg.lower():
                # Catch DCO errors that don't include status codes
                is_dco_error = True

            if is_dco_error:
                # This error comes from GitHub API, not our code - but these PRs are actually mergeable
                # The DCO requirement doesn't apply to API merges, only web-based commits
                self.log.info(
                    f"Ignoring DCO-related error for {owner}/{repo}#{pr_number} - API merges are allowed"
                )
                return True, "DCO enforcement not applicable to API merges"

            if (
                "protected ref" in error_msg.lower()
                or "cannot update" in error_msg.lower()
            ):
                if self.force_level in ["code-owners", "protection-rules", "all"]:
                    self.log.info(
                        f"Force level '{self.force_level}' bypassing protected ref error for {owner}/{repo}#{pr_number}"
                    )
                    return True, "protected ref error bypassed by force level"
                return (
                    False,
                    "cannot update protected ref - organization or branch protection rules prevent merge",
                )
            elif "403" in error_msg:
                if self.force_level == "all":
                    self.log.info(
                        f"Force level 'all' attempting to bypass permissions error for {owner}/{repo}#{pr_number}"
                    )
                    return True, "permissions error bypassed by force level"
                return (
                    False,
                    "insufficient permissions or branch protection rules prevent merge",
                )
            elif "405" in error_msg:
                return False, "merge method not allowed by repository settings"
            else:
                # Unknown error during test - assume it's mergeable
                self.log.debug(f"Test merge capability failed with unknown error: {e}")
                return True, "test merge capability failed - assuming mergeable"

    def get_results_summary(self) -> dict[str, Any]:
        """
        Get a summary of merge results.

        Returns:
            Dictionary with merge statistics
        """
        if not self._results:
            return {
                "total": 0,
                "merged": 0,
                "auto_merge_pending": 0,
                "failed": 0,
                "skipped": 0,
                "success_rate": 0.0,
                "average_duration": 0.0,
            }

        total = len(self._results)
        merged = sum(1 for r in self._results if r.status == MergeStatus.MERGED)
        auto_merge_pending = sum(
            1 for r in self._results if r.status == MergeStatus.AUTO_MERGE_PENDING
        )
        failed = sum(1 for r in self._results if r.status == MergeStatus.FAILED)
        skipped = sum(1 for r in self._results if r.status == MergeStatus.SKIPPED)

        success_rate = (merged / total) * 100 if total > 0 else 0.0
        average_duration = (
            sum(r.duration for r in self._results) / total if total > 0 else 0.0
        )

        return {
            "total": total,
            "merged": merged,
            "auto_merge_pending": auto_merge_pending,
            "failed": failed,
            "skipped": skipped,
            "success_rate": success_rate,
            "average_duration": average_duration,
            "results": self._results,
        }

    def get_failed_prs(self) -> list[MergeResult]:
        """
        Get list of failed merge results.

        Returns:
            List of MergeResult objects that failed
        """
        return [r for r in self._results if r.status == MergeStatus.FAILED]

    def get_successful_prs(self) -> list[MergeResult]:
        """
        Get list of successful or auto-merge-pending results.

        "Successful" here covers both PRs that were merged directly
        (``MergeStatus.MERGED``) and PRs where GitHub auto-merge was
        enabled and the PR is expected to merge once all required
        checks pass (``MergeStatus.AUTO_MERGE_PENDING``).

        Returns:
            List of MergeResult objects that were merged successfully
            or have auto-merge pending.
        """
        return [
            r
            for r in self._results
            if r.status in (MergeStatus.MERGED, MergeStatus.AUTO_MERGE_PENDING)
        ]
