# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import (
    Any,
)
from urllib.parse import quote

import httpx
from aiolimiter import AsyncLimiter
from tenacity import (
    AsyncRetrying,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

__all__ = [
    "GitHubAsync",
    "RateLimitError",
    "SecondaryRateLimitError",
    "GraphQLError",
    "PermissionError",
]

GITHUB_API = "https://api.github.com"
GITHUB_GQL = "https://api.github.com/graphql"


class RateLimitError(Exception):
    """Raised when the primary GitHub API rate limit is reached."""


class SecondaryRateLimitError(Exception):
    """Raised when GitHub's secondary rate limit (abuse detection) triggers."""


class GraphQLError(Exception):
    """Raised for GraphQL errors returned by GitHub."""


class PermissionError(Exception):
    """Raised when GitHub API returns a permission/authorization error.

    Attributes:
        operation: The operation that failed (e.g., 'approve', 'merge', 'close')
        message: Human-readable error message
        token_type_guidance: Guidance for both classic and fine-grained tokens
    """

    def __init__(
        self,
        operation: str,
        message: str,
        token_type_guidance: dict[str, str] | None = None,
    ):
        self.operation = operation
        self.token_type_guidance = token_type_guidance or {}
        super().__init__(message)


class RetryableError(Exception):
    """Internal exception to signal tenacity that a retry should occur."""


def _now() -> float:
    return time.time()


def _is_secondary_rate_limited(body_text: str) -> bool:
    text = body_text.lower()
    # GitHub may return messages like:
    # "You have exceeded a secondary rate limit. Please wait a few minutes..."
    # Or "abuse detection mechanism"
    return "secondary rate limit" in text or "abuse detection" in text


def _is_primary_rate_limited(body_text: str) -> bool:
    text = body_text.lower()
    return "api rate limit exceeded" in text


def _is_transient_graphql_error(errors: Any) -> bool:
    try:
        # The structure is usually a list of dicts with "message".
        message_blob = json.dumps(errors).lower()
    except Exception:
        message_blob = str(errors).lower()
    # Heuristics for retryable GraphQL responses
    return any(
        needle in message_blob
        for needle in [
            "rate limit",  # may appear in graphql errors as well
            "something went wrong",  # generic GH error
            "timeout",
            "internal server error",
            "network timeout",
        ]
    )


def _is_retryable_status(status: int) -> bool:
    # Treat common transient statuses as retryable.
    return status in (429, 502, 503, 504)


# Permission requirements mapping for operations
OPERATION_PERMISSIONS = {
    "list_repos": {
        "classic": "read:org scope",
        "fine_grained": "Organization members: Read access",
        "description": "List organization repositories",
    },
    "approve": {
        "classic": "repo scope",
        "fine_grained": "Pull requests: Read and write",
        "description": "Approve pull requests",
    },
    "merge": {
        "classic": "repo scope",
        "fine_grained": "Contents: Read and write",
        "description": "Merge pull requests",
    },
    "merge_workflow": {
        "classic": "workflow scope (in addition to repo)",
        "fine_grained": "Workflows: Read and write",
        "description": "Merge pull requests that modify GitHub Actions workflows",
    },
    "update_branch": {
        "classic": "repo scope",
        "fine_grained": "Contents: Read and write, Pull requests: Read and write",
        "description": "Update/rebase pull request branches",
    },
    "close": {
        "classic": "repo scope",
        "fine_grained": "Pull requests: Read and write",
        "description": "Close pull requests",
    },
    "branch_protection": {
        "classic": "repo scope",
        "fine_grained": "Administration: Read access",
        "description": "Read branch protection rules",
    },
    "checks": {
        "classic": "repo scope (or workflow for actions)",
        "fine_grained": "Actions: Read access, Workflows: Read access",
        "description": "Read status checks and workflow runs",
    },
}


async def _maybe_await(
    cb: Callable[..., None | Awaitable[None]] | None, *args, **kwargs
) -> None:
    if cb is None:
        return
    result = cb(*args, **kwargs)
    if asyncio.iscoroutine(result):
        await result


class GitHubAsync:
    """
    Asynchronous GitHub API client with:
    - httpx AsyncClient for HTTP/2 support and connection pooling
    - Bounded concurrency via asyncio.Semaphore
    - Request rate limiting via aiolimiter.AsyncLimiter (RPS cap)
    - Robust retry with tenacity on transient errors and rate limits
    - Helpers for GraphQL and REST endpoints used by dependamerge
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        api_url: str = GITHUB_API,
        graphql_url: str = GITHUB_GQL,
        max_concurrency: int = 20,
        requests_per_second: float = 8.0,
        timeout: float = 20.0,
        user_agent: str = "dependamerge/async-client",
        verify: bool | str = True,
        proxies: dict[str, str] | None = None,
        logger: logging.Logger | None = None,
        on_rate_limited: Callable[[float], None | Awaitable[None]] | None = None,
        on_rate_limit_cleared: Callable[[], None | Awaitable[None]] | None = None,
        on_metrics: Callable[[int, float], None | Awaitable[None]] | None = None,
    ):
        """
        Initialize the async client.

        Args:
            token: GitHub token. If None, reads from GITHUB_TOKEN env var.
            api_url: Base REST API URL (set to your GHE base if needed).
            graphql_url: GraphQL endpoint URL.
            max_concurrency: Max concurrent in-flight requests.
            requests_per_second: Max requests per second (token bucket).
            timeout: Per-request timeout (seconds).
            user_agent: User-Agent header.
            verify: TLS verify flag or path to CA bundle.
            proxies: Optional httpx proxies mapping.
            logger: Optional logger for client messages.
            on_rate_limited: Callback invoked with reset_epoch when primary limit hit.
            on_rate_limit_cleared: Callback invoked when resuming after rate limit.
        """
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            raise ValueError("GitHub token is required. Set GITHUB_TOKEN.")

        self.api_url = api_url.rstrip("/")
        self.graphql_url = graphql_url
        self._max_concurrency = max_concurrency
        self.semaphore = asyncio.Semaphore(self._max_concurrency)
        self._base_rps = requests_per_second
        self._current_rps = requests_per_second
        self.limiter = AsyncLimiter(max_rate=self._current_rps, time_period=1.0)
        self.log = logger or logging.getLogger(__name__)
        self._timeout = timeout

        self.on_rate_limited = on_rate_limited
        self.on_rate_limit_cleared = on_rate_limit_cleared
        self.on_metrics = on_metrics

        # Error tracking for adaptive throttling
        self._error_history: list[
            tuple[float, str]
        ] = []  # List of (timestamp, error_type) tuples
        self._error_window = 300  # 5 minutes
        self._last_retry_after: float | None = None
        self._adaptive_delay = 0.0
        self._last_adaptive_update: float | None = None

        # Cache for the authenticated user's login (never changes during a session)
        self._authenticated_user_login: str | None = None

        mounts = None
        if proxies:
            mounts = {}
            if "http" in proxies and proxies["http"]:
                mounts["http://"] = httpx.AsyncHTTPTransport(proxy=proxies["http"])
            if "https" in proxies and proxies["https"]:
                mounts["https://"] = httpx.AsyncHTTPTransport(proxy=proxies["https"])
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": user_agent,
            },
            http2=True,
            timeout=timeout,
            verify=verify,
            mounts=mounts,
        )

    def __repr__(self) -> str:
        """Safe repr that never exposes the token value."""
        return f"GitHubAsync(api_url={self.api_url!r}, token=***)"

    async def __aenter__(self) -> GitHubAsync:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close underlying httpx client."""
        await self._client.aclose()

    def _parse_permission_error(
        self, error: Exception, operation: str, owner: str = "", repo: str = ""
    ) -> PermissionError | None:
        """Parse HTTP error to determine if it's a permission issue.

        Args:
            error: The exception that was raised
            operation: The operation being performed (e.g., 'approve', 'merge')
            owner: Repository owner (for context in error messages)
            repo: Repository name (for context in error messages)

        Returns:
            PermissionError if this is a permission issue, None otherwise
        """
        error_str = str(error)

        # Check for 401 (unauthorized/expired token)
        if "401" in error_str or "Unauthorized" in error_str:
            return PermissionError(
                operation=operation,
                message="Token authentication failed - token may be expired or invalid",
                token_type_guidance={
                    "classic": "Regenerate your token at: https://github.com/settings/tokens",
                    "fine_grained": "Check token expiration at: https://github.com/settings/tokens?type=beta",
                    "fix": "Run: gh auth refresh -h github.com",
                },
            )

        # Check for 403 (forbidden/permission denied)
        if "403" in error_str or "Forbidden" in error_str:
            # Try to get more detailed error info from response
            response_text = ""
            response = getattr(error, "response", None)
            if response is not None:
                try:
                    response_text = str(getattr(response, "text", "")).lower()
                except AttributeError:
                    pass

            error_lower = error_str.lower()

            # Check for specific permission scenarios

            # 1. Workflow scope (already handled but included for completeness)
            if (
                "refusing to allow" in response_text
                and "workflow" in response_text
                and operation == "merge"
            ):
                perms = OPERATION_PERMISSIONS.get("merge_workflow", {})
                return PermissionError(
                    operation="merge_workflow",
                    message=f"Missing workflow permissions to merge PR in {owner}/{repo} that modifies GitHub Actions workflows",
                    token_type_guidance={
                        "classic": f"Add scope: {perms.get('classic', 'workflow')}",
                        "fine_grained": f"Enable: {perms.get('fine_grained', 'Workflows: Read and write')}",
                        "fix": "Run: gh auth refresh -h github.com -s workflow",
                    },
                )

            # 2. Fine-grained token repository scope
            if (
                "resource not accessible" in response_text
                or "not in scope" in error_lower
            ):
                return PermissionError(
                    operation=operation,
                    message=f"Repository {owner}/{repo} is not accessible with this token",
                    token_type_guidance={
                        "classic": "Token should have 'repo' scope for private repositories, or 'public_repo' for public repositories",
                        "fine_grained": f"Add {owner}/{repo} to the token's repository access list at: https://github.com/settings/tokens",
                        "fix": f"Edit your fine-grained token and add '{owner}/{repo}' to repository access",
                    },
                )

            # 3. Operation-specific permission errors
            perms = OPERATION_PERMISSIONS.get(operation, {})
            if perms:
                location = f" in {owner}/{repo}" if owner and repo else ""
                return PermissionError(
                    operation=operation,
                    message=f"Insufficient permissions to {perms.get('description', operation)}{location}",
                    token_type_guidance={
                        "classic": f"Required scope: {perms.get('classic', 'repo')}",
                        "fine_grained": f"Required permission: {perms.get('fine_grained', 'unknown')}",
                        "fix": "Update your token permissions at: https://github.com/settings/tokens",
                    },
                )

            # 4. Generic 403
            return PermissionError(
                operation=operation,
                message=f"Permission denied for {operation} operation{' in ' + owner + '/' + repo if owner and repo else ''}",
                token_type_guidance={
                    "classic": "Ensure token has 'repo' scope for full repository access",
                    "fine_grained": "Check that token has appropriate permissions and repository access",
                    "fix": "Review and update token permissions at: https://github.com/settings/tokens",
                },
            )

        # Check for 422 (unprocessable entity - often approval restrictions)
        if "422" in error_str and operation == "approve":
            if (
                "review cannot be requested from pull request author"
                in error_str.lower()
            ):
                return PermissionError(
                    operation=operation,
                    message="Cannot approve your own pull request",
                    token_type_guidance={
                        "classic": "GitHub does not allow self-approval of pull requests",
                        "fine_grained": "GitHub does not allow self-approval of pull requests",
                        "fix": "Request review from another team member",
                    },
                )
            elif "unprocessable entity" in error_str.lower():
                return PermissionError(
                    operation=operation,
                    message="Pull request approval failed - repository may have approval restrictions",
                    token_type_guidance={
                        "classic": "Check repository settings for review requirements",
                        "fine_grained": "Check repository settings for review requirements",
                        "fix": "Contact repository administrator to review branch protection rules",
                    },
                )

        # Not a permission error we recognize
        return None

    # --------------------------
    # Core request functionality
    # --------------------------

    def _parse_rate_limit_headers(
        self, r: httpx.Response
    ) -> tuple[int, int, float | None]:
        """
        Parse GitHub rate limit headers.

        Returns:
            (remaining, limit, reset_epoch)
        """
        remaining = int(r.headers.get("X-RateLimit-Remaining", "1"))
        limit = int(r.headers.get("X-RateLimit-Limit", "60"))
        reset = r.headers.get("X-RateLimit-Reset")
        reset_epoch = float(reset) if reset else None
        return remaining, limit, reset_epoch

    async def _sleep_until(self, reset_epoch: float) -> None:
        now = _now()
        delay = max(0.0, reset_epoch - now)
        if delay > 0:
            await _maybe_await(self.on_rate_limited, reset_epoch)
            try:
                await asyncio.sleep(delay)
            finally:
                await _maybe_await(self.on_rate_limit_cleared)

    @retry(
        reraise=True,
        stop=stop_after_attempt(6),
        wait=wait_random_exponential(multiplier=0.5, max=10.0),
        retry=retry_if_exception_type(
            (
                httpx.TransportError,
                httpx.ReadTimeout,
                RetryableError,
                SecondaryRateLimitError,
            )
        ),
    )
    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """
        Low-level request with concurrency limit, RPS limit, and retry handling.
        Handles primary/secondary rate limits and transient statuses.
        """
        async with self.semaphore:
            async with self.limiter:
                r = await self._client.request(method, url, **kwargs)

        # 401 should not be retried (bad credentials)
        if r.status_code == 401:
            r.raise_for_status()

        # Primary rate limit: examine headers and body
        if r.status_code == 403:
            # Parse body defensively
            body_text: str
            try:
                body_text = r.text or ""
            except Exception:
                body_text = ""

            remaining, _, reset_epoch = self._parse_rate_limit_headers(r)

            # Secondary rate limit (abuse detection)
            if _is_secondary_rate_limited(body_text):
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    # Sleep the advised duration and signal retry
                    try:
                        delay = float(retry_after)
                        self._last_retry_after = delay
                        # Apply adaptive throttling based on Retry-After
                        self._apply_retry_after_throttling(delay)
                    except Exception:
                        delay = 5.0
                    self.log.warning(
                        "Secondary rate limit hit. Sleeping for %ss", delay
                    )
                    await asyncio.sleep(max(0.0, delay))
                else:
                    # Fallback wait when no explicit Retry-After
                    delay = 10.0
                    self.log.warning(
                        "Secondary rate limit hit. Sleeping fallback %ss", delay
                    )
                    await asyncio.sleep(delay)

                # Track error for adaptive throttling
                self._track_error("secondary_rate_limit")
                raise SecondaryRateLimitError("Secondary rate limit encountered")

            # Primary rate limit exhausted
            if remaining == 0 or _is_primary_rate_limited(body_text):
                # Check for Retry-After header on 429 responses
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                        self._last_retry_after = delay
                        self.log.warning(
                            "Primary rate limit with Retry-After: %ss", delay
                        )
                        await asyncio.sleep(max(0.0, delay))
                        self._apply_retry_after_throttling(delay)
                    except Exception:
                        pass
                elif reset_epoch:
                    self.log.warning(
                        "Primary rate limit exhausted. Waiting until reset: %s",
                        reset_epoch,
                    )
                    await self._sleep_until(reset_epoch)
                else:
                    # If no reset header, backoff and retry
                    self.log.warning(
                        "Primary rate limit suspected without reset header; backing off"
                    )
                    await asyncio.sleep(5.0)

                # Track error for adaptive throttling
                self._track_error("primary_rate_limit")
                raise RetryableError("Primary rate limit reset waited; retrying")

        # Retryable transient statuses
        if _is_retryable_status(r.status_code):
            # Check for Retry-After on 429 or 503 responses
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                    self._last_retry_after = delay
                    self.log.debug(
                        "HTTP %s with Retry-After: %ss", r.status_code, delay
                    )
                    await asyncio.sleep(max(0.0, delay))
                    self._apply_retry_after_throttling(delay)
                except Exception:
                    pass

            self._track_error("transient_error")
            self.log.debug("Retryable HTTP status %s received", r.status_code)
            raise RetryableError(f"Transient HTTP status: {r.status_code}")

        # All other errors -> raise
        r.raise_for_status()

        # Apply adaptive delay based on recent error patterns
        if self._adaptive_delay > 0:
            await asyncio.sleep(self._adaptive_delay)

        # Dynamic concurrency and RPS tuning based on latest headers and error history
        try:
            remaining, limit, reset_epoch = self._parse_rate_limit_headers(r)
            error_rate = self._get_recent_error_rate()

            # More aggressive throttling if we have recent errors or low rate limit remaining
            if limit > 0:
                remaining_ratio = remaining / max(1, limit)
                should_throttle = remaining_ratio < 0.1 or error_rate > 0.1

                if should_throttle:
                    # Reduce concurrency but keep a floor of 2
                    throttle_factor = 0.3 if error_rate > 0.2 else 0.5
                    new_concurrency = max(
                        2, int(self._max_concurrency * throttle_factor)
                    )
                    if new_concurrency != self._max_concurrency:
                        self._max_concurrency = new_concurrency
                        self.semaphore = asyncio.Semaphore(self._max_concurrency)

                    # Reduce RPS but keep a floor of 1
                    new_rps = max(1.0, self._current_rps * throttle_factor)
                    if abs(new_rps - self._current_rps) >= 0.5:
                        self._current_rps = new_rps
                        self.limiter = AsyncLimiter(
                            max_rate=self._current_rps, time_period=1.0
                        )
            else:
                # Gradually increase limits when healthy, up to configured base values
                if self._max_concurrency < 20:
                    self._max_concurrency = min(20, self._max_concurrency + 1)
                    self.semaphore = asyncio.Semaphore(self._max_concurrency)
                if self._current_rps < self._base_rps:
                    self._current_rps = min(self._base_rps, self._current_rps + 1.0)
                    self.limiter = AsyncLimiter(
                        max_rate=self._current_rps, time_period=1.0
                    )
        except Exception:
            # Tuning is best-effort; never fail the request on tuning errors
            pass
        # Push current metrics to progress tracker (if provided)
        try:
            await _maybe_await(
                getattr(self, "on_metrics", None),
                self._max_concurrency,
                float(self._current_rps),
            )
        except Exception:
            # Metrics reporting is best-effort
            pass
        return r

    # -------------
    # Public helpers
    # -------------

    async def get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        r = await self._request("GET", f"{self.api_url}{path}", params=params)
        return r.json()  # type: ignore[no-any-return]

    async def post(
        self, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        r = await self._request("POST", f"{self.api_url}{path}", json=json)
        if r.status_code == 204:
            return {}
        return r.json()  # type: ignore[no-any-return]

    async def put(
        self, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        r = await self._request("PUT", f"{self.api_url}{path}", json=json)
        if r.status_code == 204:
            return {}
        return r.json()  # type: ignore[no-any-return]

    async def patch(
        self, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        r = await self._request("PATCH", f"{self.api_url}{path}", json=json)
        if r.status_code == 204:
            return {}
        return r.json()  # type: ignore[no-any-return]

    async def graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Execute a GraphQL query with retry for transient GraphQL errors.

        Note: HTTP-level issues are handled by _request's retry. Here we add
        retry for 200 OK responses that include GraphQL-level transient errors.
        """
        payload = {"query": query, "variables": variables or {}}

        async for attempt in AsyncRetrying(
            reraise=True,
            stop=stop_after_attempt(5),
            wait=wait_random_exponential(multiplier=0.5, max=10.0),
            retry=retry_if_exception_type(
                (RetryableError, httpx.TransportError, httpx.ReadTimeout)
            ),
        ):
            with attempt:
                r = await self._request("POST", self.graphql_url, json=payload)
                data = r.json()
                if "errors" in data and data["errors"]:
                    # Retry on transient errors, otherwise raise
                    if _is_transient_graphql_error(data["errors"]):
                        self.log.debug(
                            "Transient GraphQL error encountered; retrying: %s",
                            data["errors"],
                        )
                        raise RetryableError("Transient GraphQL error")
                    # Non-transient; raise detailed error
                    raise GraphQLError(json.dumps(data["errors"]))
                if "data" not in data:
                    # Unexpected shape; treat as transient
                    self.log.debug("GraphQL response missing 'data'; retrying")
                    raise RetryableError("Malformed GraphQL response")
                return data["data"]  # type: ignore[no-any-return]

        # Should not be reached due to reraise=True; keep mypy happy
        raise GraphQLError("GraphQL request failed after retries")

    # -----------------------
    # GitHub operation helpers
    # -----------------------

    async def approve_pull_request(
        self, owner: str, repo: str, number: int, body: str
    ) -> None:
        """
        Approve a pull request.

        REST: POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews

        Raises:
            PermissionError: If token lacks required permissions
        """
        try:
            await self.post(
                f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                json={"event": "APPROVE", "body": body},
            )
        except Exception as e:
            perm_error = self._parse_permission_error(e, "approve", owner, repo)
            if perm_error:
                raise perm_error from e
            raise

    async def merge_pull_request(
        self, owner: str, repo: str, number: int, merge_method: str = "merge"
    ) -> bool:
        """
        Merge a pull request.

        REST: PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge

        Raises:
            PermissionError: If token lacks required permissions
        """
        try:
            self.log.debug(
                f"Attempting to merge PR #{number} in {owner}/{repo} with method={merge_method}"
            )
            data = await self.put(
                f"/repos/{owner}/{repo}/pulls/{number}/merge",
                json={"merge_method": merge_method},
            )
            # The API returns {"merged": true/false, ...}
            merged = bool(data.get("merged", False))
            if merged:
                self.log.debug(f"Successfully merged PR #{number} in {owner}/{repo}")
            else:
                self.log.warning(
                    f"GitHub API returned merged=false for PR #{number} in {owner}/{repo}: {data}"
                )
            return merged
        except Exception as e:
            # Check for permission errors first (includes workflow scope check)
            perm_error = self._parse_permission_error(e, "merge", owner, repo)
            if perm_error:
                # Log the detailed error for debugging
                self.log.debug(
                    f"Permission error merging PR #{number} in {owner}/{repo}: {perm_error}"
                )
                raise perm_error from e

            # Log other errors
            error_type = type(e).__name__
            error_msg = str(e)
            self.log.debug(
                f"Merge API error for PR #{number} in {owner}/{repo}: {error_type}: {error_msg}"
            )
            raise
            # Try to extract response body if available (for HTTPStatusError)
            if hasattr(e, "response") and hasattr(e.response, "text"):
                try:
                    response_text = e.response.text
                    self.log.debug(f"GitHub API response body: {response_text}")
                except Exception:
                    pass
            # Get PR details to check if the merge actually succeeded despite the exception
            try:
                pr_data_response = await self.get(
                    f"/repos/{owner}/{repo}/pulls/{number}"
                )
                # PR data should always be a dict, not a list
                pr_data = pr_data_response if isinstance(pr_data_response, dict) else {}

                # Extract relevant state information
                mergeable = pr_data.get("mergeable")
                mergeable_state = pr_data.get("mergeable_state")
                state = pr_data.get("state")
                merged = pr_data.get("merged", False)
                draft = pr_data.get("draft", False)

                # Check if the merge actually succeeded despite the exception
                # This handles race conditions where the API succeeds but we get an exception
                # due to rate limiting, network issues, JSON parsing, etc.
                if state == "closed" and merged:
                    self.log.info(
                        f"PR #{number} in {owner}/{repo} was successfully merged despite exception: {e}"
                    )
                    return True

                # Enhanced error message with PR state context
                error_msg = (
                    f"Failed to merge PR #{number} in {owner}/{repo}. "
                    f"PR state: {state}, mergeable: {mergeable}, mergeable_state: {mergeable_state}. "
                    f"Error: {str(e)}"
                )

                # Check for common issues that cause 405 errors
                if mergeable_state == "blocked":
                    error_msg += " (Likely blocked by branch protection rules or required status checks)"
                elif mergeable_state == "behind":
                    error_msg += " (PR branch is behind base branch)"
                elif mergeable_state == "dirty":
                    error_msg += " (PR has merge conflicts)"
                elif draft:
                    error_msg += " (Cannot merge draft PR)"
                elif state == "closed" and not merged:
                    error_msg += " (PR was closed without merging)"
                elif state != "open":
                    error_msg += f" (PR is not open, state: {state})"

                raise Exception(error_msg) from e
            except Exception as inner_e:
                # If we can't get PR details, just re-raise the original error
                if isinstance(inner_e, Exception) and "Failed to merge PR" in str(
                    inner_e
                ):
                    raise inner_e from e
                else:
                    raise e from inner_e


    async def enable_auto_merge(
        self, pull_request_node_id: str, merge_method: str = "MERGE"
    ) -> bool:
        """
        Enable auto-merge on a pull request via GraphQL.

        Auto-merge will automatically merge the PR once all required
        branch protection rules are satisfied.

        Args:
            pull_request_node_id: The GraphQL node ID of the pull request.
            merge_method: Merge method - "MERGE", "SQUASH", or "REBASE".
                Lowercase values ("merge", "squash", "rebase") are
                automatically uppercased.

        Returns:
            True if auto-merge was successfully enabled, False otherwise.
        """
        from .github_graphql import ENABLE_AUTO_MERGE

        # Normalise to the GraphQL enum (uppercase)
        graphql_method = merge_method.upper()
        if graphql_method not in ("MERGE", "SQUASH", "REBASE"):
            self.log.warning(
                "Invalid merge method for auto-merge: %s; defaulting to MERGE",
                merge_method,
            )
            graphql_method = "MERGE"

        try:
            result = await self.graphql(
                ENABLE_AUTO_MERGE,
                {
                    "pullRequestId": pull_request_node_id,
                    "mergeMethod": graphql_method,
                },
            )
            auto_merge_data = (
                result.get("enablePullRequestAutoMerge", {})
                .get("pullRequest", {})
                .get("autoMergeRequest")
            )
            if auto_merge_data:
                self.log.debug(
                    "Auto-merge enabled for PR %s (method=%s, enabledAt=%s)",
                    pull_request_node_id,
                    auto_merge_data.get("mergeMethod"),
                    auto_merge_data.get("enabledAt"),
                )
                return True
            self.log.debug(
                "Auto-merge response missing autoMergeRequest for PR %s",
                pull_request_node_id,
            )
            return False
        except Exception as e:
            error_msg = str(e)
            # Common reasons auto-merge can't be enabled:
            # - Repository doesn't have auto-merge enabled in settings
            # - PR has conflicts
            # - Required status checks not configured
            self.log.debug(
                "Could not enable auto-merge for PR %s: %s",
                pull_request_node_id,
                error_msg,
            )
            return False

    async def get_pull_request_review_comments(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        """
        Get review comments for a pull request.

        REST: GET /repos/{owner}/{repo}/pulls/{pull_number}/comments
        """
        try:
            data = await self.get(f"/repos/{owner}/{repo}/pulls/{number}/comments")
            return data if isinstance(data, list) else []
        except Exception as e:
            # If we can't get review comments, return empty list
            self.log.debug(f"Could not fetch review comments for PR {number}: {e}")
            return []

    async def post_issue_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> dict[str, Any]:
        """
        Post a comment on an issue or pull request.

        REST: POST /repos/{owner}/{repo}/issues/{issue_number}/comments

        Raises:
            PermissionError: If token lacks required permissions
        """
        try:
            data = await self.post(
                f"/repos/{owner}/{repo}/issues/{number}/comments",
                json={"body": body},
            )
        except Exception as e:
            perm_error = self._parse_permission_error(
                e, f"post a comment on issue or pull request #{number}", owner, repo
            )
            if perm_error:
                raise perm_error from e
            raise
        return data if isinstance(data, dict) else {}

    async def check_pr_commit_signatures(
        self, owner: str, repo: str, number: int
    ) -> tuple[bool, list[str]]:
        """Check whether all commits on a pull request have verified signatures.

        REST: GET /repos/{owner}/{repo}/pulls/{pull_number}/commits

        Returns:
            Tuple of ``(all_verified, unverified_shas)``.
            ``all_verified`` is True when every commit carries a
            valid signature according to GitHub.
            ``unverified_shas`` contains the abbreviated SHAs of
            any commits whose verification failed.

        Raises:
            Exception: surfaces the underlying API/network error
            on failure rather than silently returning a default.
            Callers that want fail-open or fail-closed semantics
            should wrap the call in ``try``/``except`` and decide
            for themselves — the previous fail-open default
            (returning ``(True, [])``) collided with the
            signature-preservation gate in ``rebase.py``, which
            documents "verified" as a positive confirmation.
        """
        unverified: list[str] = []
        # Iterate over all pages of commits to ensure we don't miss
        # unverified commits on pull requests with >100 commits.
        async for commits in self.get_paginated(
            f"/repos/{owner}/{repo}/pulls/{number}/commits",
            per_page=100,
        ):
            if not isinstance(commits, list):
                # Unexpected response shape — assume OK to avoid
                # false positives. The API returned 200 OK but in
                # an unexpected shape, which is distinct from a
                # network/HTTP error and arguably means the page
                # is empty.
                return True, []

            for commit_data in commits:
                if not isinstance(commit_data, dict):
                    continue
                raw_sha = commit_data.get("sha")
                sha = str(raw_sha)[:8] if isinstance(raw_sha, str) else "unknown"
                commit_obj = commit_data.get("commit")
                if not isinstance(commit_obj, dict):
                    unverified.append(sha)
                    continue
                verification = commit_obj.get("verification")
                if not isinstance(verification, dict):
                    unverified.append(sha)
                    continue
                if not verification.get("verified", False):
                    unverified.append(sha)

        all_verified = len(unverified) == 0
        return all_verified, unverified

    async def requires_commit_signatures(
        self, owner: str, repo: str, branch: str = "main"
    ) -> bool:
        """
        Check whether a branch requires signed (verified) commits.

        Uses two complementary sources:

        1. **Classic branch protection** – the ``required_signatures``
           sub-resource of the branch protection REST endpoint.
        2. **Repository rulesets** (newer API) – any active ruleset that
           targets the given branch and contains a ``required_signatures``
           rule.

        Returns:
            True if signed commits are required by *either* mechanism.
        """
        # --- 1. Classic branch protection ---
        try:
            # The signatures endpoint returns 200 with {"enabled": true/false}
            # or 404 when branch protection / signature requirement is absent.
            encoded_branch = quote(branch, safe="")
            sig_data = await self.get(
                f"/repos/{owner}/{repo}/branches/{encoded_branch}/protection/required_signatures"
            )
            if isinstance(sig_data, dict) and sig_data.get("enabled"):
                self.log.debug(
                    "Branch %s/%s:%s requires commit signatures (classic protection)",
                    owner,
                    repo,
                    branch,
                )
                return True
        except Exception as e:
            # 404 → not enabled; other errors → continue checking rulesets
            if "404" not in str(e):
                self.log.debug(
                    "Error checking classic signature requirement for "
                    "%s/%s:%s: %s",
                    owner,
                    repo,
                    branch,
                    e,
                )

        # --- 2. Repository rulesets ---
        try:
            # Resolve the repo's actual default branch so that
            # ~DEFAULT_BRANCH ruleset conditions are evaluated correctly.
            default_branch: str | None = None
            try:
                repo_data = await self.get(f"/repos/{owner}/{repo}")
                if isinstance(repo_data, dict):
                    default_branch = repo_data.get("default_branch")
            except Exception:
                pass  # Will fall through to conservative matching

            # Paginate through all rulesets to collect their IDs.
            # The list endpoint may not include full rules/conditions,
            # so we fetch each ruleset's detail individually (matching
            # the pattern in get_required_status_checks).
            ruleset_ids: list[int] = []
            page = 1
            per_page = 100
            while True:
                page_rulesets = await self.get(
                    f"/repos/{owner}/{repo}/rulesets?per_page={per_page}&page={page}"
                )
                if not isinstance(page_rulesets, list) or not page_rulesets:
                    break
                for rs in page_rulesets:
                    if isinstance(rs, dict):
                        rs_id = rs.get("id")
                        if rs_id is not None:
                            ruleset_ids.append(int(rs_id))
                if len(page_rulesets) < per_page:
                    break
                page += 1

            for ruleset_id in ruleset_ids:
                try:
                    detail = await self.get(
                        f"/repos/{owner}/{repo}/rulesets/{ruleset_id}"
                    )
                    if not isinstance(detail, dict):
                        continue
                except Exception as detail_err:
                    self.log.debug(
                        "Could not fetch ruleset %s for %s/%s: %s",
                        ruleset_id,
                        owner,
                        repo,
                        detail_err,
                    )
                    continue

                # Only consider active rulesets
                if detail.get("enforcement") != "active":
                    continue
                # Check if this ruleset applies to our branch
                conditions = detail.get("conditions", {})
                if isinstance(
                    conditions, dict
                ) and not self._ruleset_applies_to_branch(
                    conditions, branch, default_branch
                ):
                    continue
                # Check for required_signatures rule
                rules = detail.get("rules", [])
                if isinstance(rules, list):
                    for rule in rules:
                        if (
                            isinstance(rule, dict)
                            and rule.get("type") == "required_signatures"
                        ):
                            self.log.debug(
                                "Branch %s/%s:%s requires commit "
                                "signatures (ruleset: %s)",
                                owner,
                                repo,
                                branch,
                                detail.get("name", "unknown"),
                            )
                            return True
        except Exception as e:
            self.log.debug(
                "Error checking rulesets for signature requirement on "
                "%s/%s:%s: %s",
                owner,
                repo,
                branch,
                e,
            )

        return False

    @staticmethod
    def _ruleset_applies_to_branch(
        conditions: dict[str, Any],
        branch: str,
        default_branch: str | None = None,
    ) -> bool:
        """Check whether a ruleset's ref_name conditions match *branch*.

        Ruleset conditions use ``conditions.ref_name.include`` /
        ``conditions.ref_name.exclude`` arrays.  Recognised patterns:

        * ``~DEFAULT_BRANCH`` — matches when *branch* equals *default_branch*.
          If *default_branch* is not supplied, the match is treated as
          ``True`` (conservative) to avoid silently filtering out rulesets
          for repos whose default branch is something other than
          ``main``/``master``.
        * ``~ALL``            — matches every branch.
        * ``refs/heads/<name>`` — exact ref match.
        * Bare branch name   — treated as ``refs/heads/<name>``.

        If the conditions dict is empty or missing ``ref_name`` the
        ruleset is assumed to apply (conservative).
        """
        import fnmatch

        ref_name = conditions.get("ref_name", {})
        if not isinstance(ref_name, dict):
            return True  # No conditions — assume applies

        include = ref_name.get("include", [])
        exclude = ref_name.get("exclude", [])

        full_ref = f"refs/heads/{branch}"

        def _matches(pattern: str) -> bool:
            if pattern == "~ALL":
                return True
            if pattern == "~DEFAULT_BRANCH":
                if default_branch is None:
                    # Unknown default branch — conservatively assume match
                    return True
                return branch == default_branch
            # Normalise bare branch names to full refs
            pat = pattern if pattern.startswith("refs/") else f"refs/heads/{pattern}"
            return fnmatch.fnmatchcase(full_ref, pat)

        # Must match at least one include pattern (if any are specified)
        if include and not any(_matches(p) for p in include if isinstance(p, str)):
            return False

        # Must not match any exclude pattern
        if any(_matches(p) for p in exclude if isinstance(p, str)):
            return False

        return True

    async def get_required_status_checks(
        self, owner: str, repo: str, branch: str
    ) -> list[dict[str, Any]]:
        """
        Get required status checks for a branch by inspecting rulesets.

        Only rulesets whose ``conditions.ref_name`` patterns match *branch*
        are considered.  Falls back to branch protection rules if rulesets
        are not available.
        Returns a list of dicts with 'context' and optionally 'integration_id'.
        Results are deduplicated by ``context``.
        """
        required_checks: list[dict[str, Any]] = []
        seen_contexts: set[str] = set()

        # Resolve the repo's actual default branch so that ~DEFAULT_BRANCH
        # ruleset conditions are evaluated correctly (not hardcoded to
        # main/master).
        default_branch: str | None = None
        try:
            repo_data = await self.get(f"/repos/{owner}/{repo}")
            if isinstance(repo_data, dict):
                default_branch = repo_data.get("default_branch")
        except Exception:
            pass  # Will fall through to conservative matching

        # Try rulesets first (org-level and repo-level)
        try:
            rulesets = await self.get(
                f"/repos/{owner}/{repo}/rulesets?per_page=100"
            )
            if isinstance(rulesets, list):
                for ruleset in rulesets:
                    if not isinstance(ruleset, dict):
                        continue
                    ruleset_id = ruleset.get("id")
                    if not ruleset_id:
                        continue
                    # Fetch full ruleset details to get the rules
                    try:
                        detail = await self.get(
                            f"/repos/{owner}/{repo}/rulesets/{ruleset_id}"
                        )
                        if not isinstance(detail, dict):
                            continue

                        # Filter: skip rulesets that do not target this branch
                        conditions = detail.get("conditions", {})
                        if isinstance(
                            conditions, dict
                        ) and not self._ruleset_applies_to_branch(
                            conditions, branch, default_branch
                        ):
                            self.log.debug(
                                f"Ruleset {ruleset_id} does not apply to branch '{branch}'; skipping"
                            )
                            continue

                        for rule in detail.get("rules", []):
                            if not isinstance(rule, dict):
                                continue
                            if rule.get("type") == "required_status_checks":
                                params = rule.get("parameters", {})
                                for check in params.get(
                                    "required_status_checks", []
                                ):
                                    if isinstance(check, dict) and check.get(
                                        "context"
                                    ):
                                        ctx = check["context"]
                                        if ctx not in seen_contexts:
                                            seen_contexts.add(ctx)
                                            required_checks.append(check)
                    except Exception as detail_err:
                        self.log.debug(
                            f"Could not fetch ruleset {ruleset_id} details: {detail_err}"
                        )
        except Exception as e:
            self.log.debug(f"Could not fetch rulesets for {owner}/{repo}: {e}")

        # Fall back to branch protection if no ruleset checks found
        if not required_checks:
            try:
                data = await self.get(
                    f"/repos/{owner}/{repo}/branches/{branch}/protection/required_status_checks"
                )
                if isinstance(data, dict):
                    for ctx in data.get("contexts", []):
                        if ctx not in seen_contexts:
                            seen_contexts.add(ctx)
                            required_checks.append({"context": ctx})
                    for check in data.get("checks", []):
                        if isinstance(check, dict) and check.get("context"):
                            ctx = check["context"]
                            if ctx not in seen_contexts:
                                seen_contexts.add(ctx)
                                required_checks.append(check)
            except Exception:
                pass

        return required_checks

    async def get_branch_protection(
        self, owner: str, repo: str, branch: str
    ) -> dict[str, Any]:
        """
        Get branch protection rules for a branch.

        REST: GET /repos/{owner}/{repo}/branches/{branch}/protection
        """
        try:
            protection_data = await self.get(
                f"/repos/{owner}/{repo}/branches/{branch}/protection"
            )
            # Branch protection data should always be a dict, not a list
            return protection_data if isinstance(protection_data, dict) else {}
        except Exception as e:
            # Branch protection might not be enabled, return empty dict
            if "404" in str(e):
                return {}
            raise

    async def check_user_can_bypass_protection(
        self, owner: str, repo: str, force_level: str = "code-owners"
    ) -> tuple[bool, str]:
        """
        Check if the authenticated user has permissions to bypass branch protection.

        Args:
            owner: Repository owner
            repo: Repository name
            force_level: The force level being used ("code-owners", "protection-rules", "all")

        Returns:
            Tuple of (can_bypass: bool, reason: str)
        """
        try:
            # Get repository info including permissions
            repo_data = await self.get(f"/repos/{owner}/{repo}")
            if not isinstance(repo_data, dict):
                return False, "Could not fetch repository information"

            permissions = repo_data.get("permissions", {})
            self.log.debug(
                f"Repository permissions for {owner}/{repo}: admin={permissions.get('admin')}, push={permissions.get('push')}, pull={permissions.get('pull')}"
            )

            # Check if user has admin permissions (which includes bypass)
            if permissions.get("admin"):
                self.log.debug(f"User has admin permissions for {owner}/{repo}")
                return True, "User has admin permissions"

            # Try to get more detailed permission info from user's repository membership
            try:
                # For organization repos, check if user has bypass permissions
                # This requires checking the user's role/permissions
                # Use cached login to avoid repeated /user calls
                if self._authenticated_user_login is None:
                    user_data = await self.get("/user")
                    if isinstance(user_data, dict):
                        self._authenticated_user_login = user_data.get("login")

                username = self._authenticated_user_login
                if username:
                    # Check collaborator permissions
                    collab_data = await self.get(
                        f"/repos/{owner}/{repo}/collaborators/{username}/permission"
                    )
                    if isinstance(collab_data, dict):
                        permission_level = collab_data.get("permission")
                        # admin permission can bypass
                        if permission_level == "admin":
                            return True, "User has admin collaborator permissions"
            except Exception as e:
                # If we can't check detailed permissions, continue with basic check
                self.log.debug(
                    f"Could not check detailed collaborator permissions: {e}"
                )
                pass

            # If we have push permissions but not admin
            if permissions.get("push"):
                # All force levels require admin permissions to actually bypass branch protection
                # at the GitHub API level. Push permissions alone are not sufficient.
                self.log.debug(
                    f"User has push permissions for {owner}/{repo} but not admin (required to bypass branch protection at GitHub API level)"
                )
                return (
                    False,
                    "User has push permissions but not admin/bypass permissions (admin required to bypass branch protection)",
                )

            self.log.debug(
                f"User does not have sufficient permissions for {owner}/{repo}"
            )
            return False, "User does not have bypass permissions"

        except Exception as e:
            # If we can't determine permissions, return conservative result
            self.log.debug(f"Could not check bypass permissions: {e}")
            return False, f"Could not verify permissions: {str(e)}"

    async def update_branch(self, owner: str, repo: str, number: int) -> None:
        """
        Update a pull request branch (rebase).

        REST: PUT /repos/{owner}/{repo}/pulls/{pull_number}/update-branch

        Raises:
            PermissionError: If token lacks required permissions
        """
        try:
            await self.put(f"/repos/{owner}/{repo}/pulls/{number}/update-branch")
        except Exception as e:
            perm_error = self._parse_permission_error(e, "update_branch", owner, repo)
            if perm_error:
                raise perm_error from e
            raise

    async def check_token_permissions(
        self, operations: list[str], owner: str = "", repo: str = ""
    ) -> dict[str, dict[str, Any]]:
        """Pre-flight check for token permissions.

        Tests whether the token has the necessary permissions for the specified
        operations without actually performing them. This allows failing fast
        with clear error messages before attempting bulk operations.

        Args:
            operations: List of operations to check (e.g., ['approve', 'merge', 'close'])
            owner: Repository owner (required for repository-specific checks)
            repo: Repository name (required for repository-specific checks)

        Returns:
            Dictionary mapping operation names to check results:
            {
                'operation_name': {
                    'has_permission': bool,
                    'error': str | None,
                    'guidance': dict | None
                }
            }

        Example:
            >>> results = await client.check_token_permissions(['approve', 'merge'], 'owner', 'repo')
            >>> if not results['approve']['has_permission']:
            ...     print(results['approve']['error'])
        """
        results: dict[str, dict[str, Any]] = {}

        for operation in operations:
            result: dict[str, Any] = {
                "has_permission": False,
                "error": None,
                "guidance": None,
            }

            try:
                # Perform a lightweight check for each operation
                if operation in ("approve", "merge", "close", "update_branch") and owner and repo:
                    # Use the collaborator permission endpoint to verify
                    # the token has write access to this specific repo.
                    #
                    # The previous approach (GET /repos/{owner}/{repo} and
                    # inspecting permissions.push) is unreliable for
                    # fine-grained PATs: GitHub returns the *user's*
                    # org-level permissions regardless of token scope,
                    # producing false positives when the token is scoped
                    # to a different org.
                    #
                    # The collaborator endpoint correctly returns 403
                    # ("Resource not accessible by personal access token")
                    # when the token doesn't cover the target repo.

                    # Resolve authenticated username (cached after first call)
                    if self._authenticated_user_login is None:
                        user_data = await self.get("/user")
                        if isinstance(user_data, dict):
                            self._authenticated_user_login = user_data.get("login")

                    username = self._authenticated_user_login
                    if not username:
                        result["error"] = "Could not determine authenticated user"
                    else:
                        collab_data = await self.get(
                            f"/repos/{owner}/{repo}/collaborators/{username}/permission"
                        )
                        if isinstance(collab_data, dict):
                            perm_level = collab_data.get("permission", "none")
                            # write, maintain, or admin is required for approve/merge/close/update
                            if perm_level in ("write", "maintain", "admin"):
                                result["has_permission"] = True
                            else:
                                result["error"] = (
                                    f"Token has '{perm_level}' access to "
                                    f"{owner}/{repo} — write, maintain, or admin is required"
                                )
                                perms = OPERATION_PERMISSIONS.get(operation, {})
                                result["guidance"] = {
                                    "classic": perms.get("classic"),
                                    "fine_grained": perms.get("fine_grained"),
                                }
                        else:
                            result["error"] = (
                                "Could not determine collaborator permissions"
                            )

                elif operation == "branch_protection" and owner and repo:
                    # Verify Administration: Read permission by probing
                    # the branch protection endpoint.  A token with this
                    # permission receives either 200 (rules exist) or
                    # 404 "Branch not protected"; without it GitHub
                    # returns 403 "Resource not accessible".
                    #
                    # The repo metadata fetch is separated from the
                    # branch-protection probe so that a 404 from
                    # GET /repos/{owner}/{repo} (repo doesn't exist or
                    # token can't see it) is NOT silently treated as
                    # success.
                    default_branch = "main"
                    try:
                        repo_data = await self.get(
                            f"/repos/{owner}/{repo}"
                        )
                        if isinstance(repo_data, dict):
                            default_branch = repo_data.get(
                                "default_branch", "main"
                            )
                    except Exception:
                        # Repo metadata fetch failed — token may lack
                        # access.  Let the error propagate to the outer
                        # handler which will surface it as a permission
                        # error.  Do NOT fall through to treat this as
                        # success.
                        raise

                    try:
                        await self.get(
                            f"/repos/{owner}/{repo}/branches/"
                            f"{default_branch}/protection"
                        )
                        result["has_permission"] = True
                    except Exception as e:
                        if "404" in str(e):
                            # 404 = branch exists but has no protection
                            # rules — the token still has the permission.
                            result["has_permission"] = True
                        else:
                            raise

                elif operation == "list_repos":
                    # Check organization access
                    if owner:
                        await self.get(f"/orgs/{owner}/repos?per_page=1")
                    result["has_permission"] = True

                else:
                    result["error"] = f"Unknown operation: {operation}"

            except Exception as e:
                perm_error = self._parse_permission_error(e, operation, owner, repo)
                if perm_error:
                    result["has_permission"] = False
                    result["error"] = str(perm_error)
                    result["guidance"] = perm_error.token_type_guidance
                else:
                    # Unexpected error - be conservative
                    result["has_permission"] = False
                    result["error"] = f"Could not verify permissions: {str(e)}"

            results[operation] = result

        return results

    async def close_pull_request(
        self, owner: str, repo: str, number: int
    ) -> dict[str, Any]:
        """
        Close a pull request.

        Args:
            owner: Repository owner
            repo: Repository name
            number: Pull request number

        Returns:
            Updated pull request data

        Raises:
            PermissionError: If token lacks required permissions
        """
        try:
            return await self.patch(
                f"/repos/{owner}/{repo}/pulls/{number}", json={"state": "closed"}
            )
        except Exception as e:
            perm_error = self._parse_permission_error(e, "close", owner, repo)
            if perm_error:
                raise perm_error from e
            raise

    async def analyze_block_reason(
        self, owner: str, repo: str, number: int, head_sha: str
    ) -> str:
        """
        Analyze why a PR is blocked and return appropriate status.

        This is the async version that should be used from async contexts.
        """
        # Reviews
        approved = False
        human_changes_requested = False
        unresolved_copilot_reviews = 0
        unresolved_copilot_comments = 0

        try:
            reviews = await self.get(f"/repos/{owner}/{repo}/pulls/{number}/reviews")
            if isinstance(reviews, list):
                for review in reviews:
                    if not isinstance(review, dict):
                        continue
                    state = review.get("state")
                    author = review.get("user", {}).get("login", "")

                    if state == "APPROVED":
                        approved = True
                    elif state == "CHANGES_REQUESTED":
                        if author == "github-copilot[bot]":
                            unresolved_copilot_reviews += 1
                        else:
                            human_changes_requested = True
        except Exception:
            pass

        # Check for unresolved review comments
        try:
            comments = await self.get(f"/repos/{owner}/{repo}/pulls/{number}/comments")
            if isinstance(comments, list):
                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    author = comment.get("user", {}).get("login", "")
                    # Count unresolved Copilot comments (those without replies dismissing them)
                    if author == "github-copilot[bot]":
                        # Simple heuristic: if comment doesn't have "DISMISSED" or similar resolution text
                        body = comment.get("body", "").lower()
                        if "dismissed" not in body and "resolved" not in body:
                            unresolved_copilot_comments += 1
        except Exception:
            pass

        # Check runs and status contexts - look for failing (check this first as it's most specific)
        failing_checks = []
        completed_check_names: set[str] = set()
        # Track all reported check names regardless of status so that
        # queued/in_progress checks are not misclassified as "missing".
        reported_check_names: set[str] = set()
        pending_check_names: set[str] = set()
        try:
            # Check runs (newer GitHub Apps API)
            runs = await self.get(
                f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs"
            )
            if isinstance(runs, dict):
                for run in runs.get("check_runs") or []:
                    if not isinstance(run, dict):
                        continue
                    name = run.get("name", "unknown")
                    status = run.get("status")
                    conclusion = run.get("conclusion")
                    reported_check_names.add(name)
                    if status == "completed":
                        completed_check_names.add(name)
                    elif status in ("queued", "in_progress"):
                        pending_check_names.add(name)
                    if conclusion in ["failure", "cancelled", "timed_out"]:
                        failing_checks.append(name)
        except Exception:
            pass

        try:
            # Status contexts (older status API, used by services like pre-commit.ci)
            statuses = await self.get(
                f"/repos/{owner}/{repo}/commits/{head_sha}/status"
            )
            if isinstance(statuses, dict):
                for s in statuses.get("statuses") or []:
                    if not isinstance(s, dict):
                        continue
                    context = s.get("context", "unknown")
                    state = s.get("state")
                    reported_check_names.add(context)
                    if state in ["success", "neutral"]:
                        completed_check_names.add(context)
                    elif state == "pending":
                        pending_check_names.add(context)
                    if state in ["failure", "error"]:
                        # Avoid duplicates if both check-run and status exist for same service
                        if context not in failing_checks:
                            failing_checks.append(context)
        except Exception:
            pass

        # Detect missing/pending required status checks (e.g. stale pre-commit.ci)
        missing_required_checks: list[str] = []
        pending_required_checks: list[str] = []
        try:
            # Determine the base branch for this PR
            pr_data = await self.get(f"/repos/{owner}/{repo}/pulls/{number}")
            base_branch = (
                pr_data.get("base", {}).get("ref", "main")
                if isinstance(pr_data, dict)
                else "main"
            )
            required_checks = await self.get_required_status_checks(
                owner, repo, base_branch
            )
            for check in required_checks:
                ctx = check.get("context", "")
                if not ctx:
                    continue
                if ctx in reported_check_names:
                    # Check has been reported — distinguish pending from completed
                    if ctx not in completed_check_names and ctx in pending_check_names:
                        pending_required_checks.append(ctx)
                else:
                    # Never reported via either API — truly missing
                    missing_required_checks.append(ctx)
        except Exception as req_err:
            self.log.debug(
                f"Could not check required status checks for {owner}/{repo}#{number}: {req_err}"
            )

        # Prioritize blocking conditions by specificity
        # Most specific blockers first
        if failing_checks:
            if len(failing_checks) == 1:
                return f"Blocked by failing check: {failing_checks[0]}"
            else:
                return f"Blocked by {len(failing_checks)} failing checks"

        if missing_required_checks:
            if len(missing_required_checks) == 1:
                return f"Blocked by missing required status: {missing_required_checks[0]}"
            else:
                names = ", ".join(missing_required_checks)
                return f"Blocked by {len(missing_required_checks)} missing required statuses: {names}"

        if pending_required_checks:
            if len(pending_required_checks) == 1:
                return f"Blocked by pending required check: {pending_required_checks[0]}"
            else:
                names = ", ".join(pending_required_checks)
                return f"Blocked by {len(pending_required_checks)} pending required checks: {names}"

        if human_changes_requested:
            return "Human reviewer requested changes"

        if unresolved_copilot_reviews > 0:
            if unresolved_copilot_comments > 0:
                return f"Blocked by {unresolved_copilot_reviews} Copilot reviews, {unresolved_copilot_comments} comments"
            else:
                return f"Blocked by {unresolved_copilot_reviews} unresolved Copilot reviews"

        if unresolved_copilot_comments > 0:
            return (
                f"Blocked by {unresolved_copilot_comments} unresolved Copilot comments"
            )

        if not approved:
            return "Blocked by branch protection (requires approval)"

        return "Blocked by branch protection"

    # -----------------------
    # Optional REST pagination
    # -----------------------

    async def get_paginated(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        per_page: int = 100,
        max_pages: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Iterate through a paginated REST collection.

        Yields JSON arrays/items for each page. Caller can flatten as needed.
        """
        page = 1
        while True:
            q = dict(params or {})
            q.update({"per_page": per_page, "page": page})
            r = await self._request("GET", f"{self.api_url}{path}", params=q)
            data = r.json()
            if not data:
                return
            yield data
            page += 1
            if max_pages and page > max_pages:
                return
            # Stop when Link header doesn't include 'rel="next"'
            link = r.headers.get("Link", "")
            if 'rel="next"' not in link:
                return

    # -----------------------
    # Error tracking and adaptive throttling
    # -----------------------

    def _track_error(self, error_type: str) -> None:
        """Track an error for adaptive throttling calculations."""
        current_time = _now()
        self._error_history.append((current_time, error_type))

        # Clean old entries outside the error window
        cutoff = current_time - self._error_window
        self._error_history = [(t, e) for t, e in self._error_history if t > cutoff]

    def _get_recent_error_rate(self) -> float:
        """Calculate the error rate in the recent window."""
        if not self._error_history:
            return 0.0

        current_time = _now()
        cutoff = current_time - self._error_window
        recent_errors = [e for t, e in self._error_history if t > cutoff]

        # Estimate request rate (very rough heuristic)
        # Assume we made approximately len(history) * 10 requests in the window
        estimated_requests = max(len(recent_errors) * 10, 1)
        return len(recent_errors) / estimated_requests

    def _apply_retry_after_throttling(self, retry_after_seconds: float) -> None:
        """Apply adaptive throttling based on Retry-After header values."""
        # If we're getting Retry-After frequently, add adaptive delay
        if retry_after_seconds > 30:
            # Long retry-after suggests we're hitting limits hard
            self._adaptive_delay = min(5.0, retry_after_seconds * 0.1)
        elif retry_after_seconds > 10:
            # Medium retry-after suggests moderate pressure
            self._adaptive_delay = min(2.0, retry_after_seconds * 0.05)
        else:
            # Short retry-after is normal, minimal delay
            self._adaptive_delay = min(1.0, retry_after_seconds * 0.02)

        # Gradually reduce adaptive delay over time
        if self._last_adaptive_update is not None:
            time_since_update = _now() - self._last_adaptive_update
            if time_since_update > 60:  # Reduce delay after 1 minute
                self._adaptive_delay *= 0.8

        self._last_adaptive_update = _now()
