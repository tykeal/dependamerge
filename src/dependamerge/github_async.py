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
    cast,
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

from .bot_identity import is_copilot

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
        return None
    result = cb(*args, **kwargs)
    if not asyncio.iscoroutine(result):
        return None
    return await cast("Awaitable[None]", result)


class GitHubAsync:
    """
    Asynchronous GitHub API client with:
    - httpx AsyncClient for HTTP/2 support and connection pooling
    - Bounded concurrency via asyncio.Semaphore
    - Request rate limiting via aiolimiter.AsyncLimiter (RPS cap)
    - Robust retry with tenacity on transient errors and rate limits
    - Helpers for GraphQL and REST endpoints used by dependamerge
    """

    # Default ceiling for concurrent in-flight requests. Used both as the
    # constructor default and as the upper bound when adaptive tuning ramps
    # concurrency back up after a period of throttling.
    _DEFAULT_MAX_CONCURRENCY = 20

    # Heuristic used by ``_get_recent_error_rate`` to estimate how many
    # requests accompanied each observed error within the error window.
    # We do not track total request counts, only errors, so we assume each
    # error corresponds to roughly this many requests. With the current
    # value of 10 the estimate is errors / (errors * 10), so any window
    # containing at least one error yields an error_rate of ~0.1. A smaller
    # number raises that estimate and reacts to errors more aggressively,
    # while a larger number lowers it and tolerates more errors before
    # throttling. Tune this if observed throttling behaviour is too eager
    # or too lax.
    _ESTIMATED_REQUESTS_PER_ERROR = 10

    def __init__(
        self,
        token: str | None = None,
        *,
        api_url: str = GITHUB_API,
        graphql_url: str = GITHUB_GQL,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
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
        # Remember the caller-configured ceiling so adaptive tuning ramps
        # concurrency back up to *this* value (not the class default) after
        # a period of throttling, mirroring how ``_base_rps`` bounds the RPS
        # ramp-up.
        self._base_max_concurrency = max_concurrency
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

        # Session caches for repo/branch-scoped configuration.  Branch
        # protection, required status checks, and a repo's default
        # branch are effectively immutable for the lifetime of a merge
        # run, yet the merge pipeline consults them repeatedly — once
        # per PR (or several times per *blocked* PR via
        # ``analyze_block_reason``).  Caching them here collapses those
        # repeats into one fetch per repo/branch.  No locking: a
        # concurrent first miss may fetch twice, which is harmless and
        # no worse than the uncached behaviour.
        self._default_branch_cache: dict[str, str | None] = {}
        self._required_checks_cache: dict[str, list[dict[str, Any]]] = {}
        self._branch_protection_cache: dict[str, dict[str, Any]] = {}
        self._requires_signatures_cache: dict[str, bool] = {}

        # Cache for the token's OAuth scopes.  ``_token_scopes_fetched``
        # distinguishes "not looked up yet" from "looked up, but this token
        # type does not expose scopes" (fine-grained PAT / app token, which
        # leaves ``_token_scopes`` as ``None``).
        self._token_scopes: set[str] | None = None
        self._token_scopes_fetched: bool = False

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
                    "fine_grained": "Check token expiration at: https://github.com/settings/personal-access-tokens",
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
                    # Response object exposes no readable body; fall
                    # back to the empty default and keep classifying.
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
                # Honor a Retry-After header if present (primary rate
                # limits may be reported as 403 or 429).  Parse it up
                # front so that an unparsable value (e.g. an HTTP-date)
                # falls back to the reset/backoff handling below rather
                # than triggering an immediate retry.
                retry_after = r.headers.get("Retry-After")
                retry_after_delay: float | None = None
                if retry_after:
                    try:
                        retry_after_delay = float(retry_after)
                    except (TypeError, ValueError):
                        retry_after_delay = None
                if retry_after_delay is not None:
                    self._last_retry_after = retry_after_delay
                    self.log.warning(
                        "Primary rate limit with Retry-After: %ss",
                        retry_after_delay,
                    )
                    await asyncio.sleep(max(0.0, retry_after_delay))
                    self._apply_retry_after_throttling(retry_after_delay)
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
                retry_after_delay = None
                try:
                    retry_after_delay = float(retry_after)
                except (TypeError, ValueError):
                    # Retry-After was not a numeric delay; fall through
                    # to the standard retry handling.
                    retry_after_delay = None
                if retry_after_delay is not None:
                    self._last_retry_after = retry_after_delay
                    self.log.debug(
                        "HTTP %s with Retry-After: %ss",
                        r.status_code,
                        retry_after_delay,
                    )
                    await asyncio.sleep(max(0.0, retry_after_delay))
                    self._apply_retry_after_throttling(retry_after_delay)

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
                if self._max_concurrency < self._base_max_concurrency:
                    self._max_concurrency = min(
                        self._base_max_concurrency, self._max_concurrency + 1
                    )
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
                # GitHub returns the "refusing to allow ... workflow" 403
                # only when the *classic* token lacks the ``workflow``
                # scope.  Before repeating that guidance, confirm the scope
                # really is absent: if the token already carries it (or is a
                # fine-grained/app token we cannot introspect and which
                # therefore would not produce this classic-PAT message), the
                # true cause is something else — typically a repository
                # ruleset that restricts workflow-file updates, or an
                # un-authorized SSO session.  Telling the user to add a scope
                # they already hold would be an inaccurate diagnosis.
                if perm_error.operation == "merge_workflow":
                    has_workflow = await self.check_workflow_scope()
                    if has_workflow is True:
                        perm_error = PermissionError(
                            operation="merge_workflow_restricted",
                            message=(
                                f"GitHub refused to merge PR in {owner}/{repo} "
                                "even though the token already has the "
                                "'workflow' scope. The workflow-file update is "
                                "being blocked by something other than token "
                                "scope"
                            ),
                            token_type_guidance={
                                "classic": (
                                    "Check for a repository ruleset that "
                                    "restricts updates to .github/workflows/** "
                                    "and confirm the token is SSO-authorized "
                                    "for this organization"
                                ),
                                "fine_grained": (
                                    "Check for a repository ruleset that "
                                    "restricts updates to .github/workflows/**"
                                ),
                                "fix": (
                                    "Review the repository's rulesets and "
                                    "organization SSO authorization for this "
                                    "token"
                                ),
                            },
                        )
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

            github_detail = self._extract_github_error_detail(e)
            if github_detail:
                self.log.debug(
                    f"GitHub merge API response body for #{number}: {github_detail}"
                )

            # Re-check PR state: the merge may have actually succeeded
            # despite the exception (a race where the API call lands
            # but we still see an error from rate-limiting, network, or
            # JSON parsing), and the state adds context to the error we
            # raise.
            return await self._validate_merge_result(
                owner, repo, number, e, github_detail
            )

    @staticmethod
    def _extract_github_error_detail(error: Exception) -> str:
        """Extract GitHub's response-body message from a failed request.

        GitHub puts the *actual* reason here — ruleset violations,
        "Required workflows ... are not satisfied", required-check names,
        etc.  The ``HTTPStatusError`` text only carries the status line
        (e.g. "405 Method Not Allowed"), so without this the real cause is
        silently lost.  Whitespace/newlines are collapsed so the reason
        fits on a single status line.

        Returns an empty string when no detail could be extracted.
        """
        response = getattr(error, "response", None)
        if response is None:
            return ""
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("message"), str):
                return " ".join(body["message"].split())
        except Exception:
            # Response body was not JSON (or .json() failed); fall through
            # to the raw-text extraction below rather than failing here.
            pass
        try:
            raw = getattr(response, "text", "") or ""
            return " ".join(raw.split())[:500]
        except Exception:
            return ""

    async def _validate_merge_result(
        self,
        owner: str,
        repo: str,
        number: int,
        error: Exception,
        github_detail: str,
    ) -> bool:
        """Re-check PR state after a merge attempt raised an exception.

        The merge may have actually succeeded despite the exception (a race
        where the API call lands but we still see an error from
        rate-limiting, network, or JSON parsing).  When the PR is confirmed
        merged, return ``True``.  Otherwise raise an enhanced exception that
        preserves the original error text (its HTTP status line is
        string-matched by ``_merge_pr_with_retry`` to classify retryable vs
        terminal failures) and adds GitHub's actionable response body plus
        PR-state context.
        """
        try:
            pr_data_response = await self.get(f"/repos/{owner}/{repo}/pulls/{number}")
            # PR data should always be a dict, not a list
            pr_data = pr_data_response if isinstance(pr_data_response, dict) else {}

            # Extract relevant state information
            mergeable = pr_data.get("mergeable")
            mergeable_state = pr_data.get("mergeable_state")
            state = pr_data.get("state")
            merged = pr_data.get("merged", False)
            draft = pr_data.get("draft", False)

            # Check if the merge actually succeeded despite the exception.
            # This handles race conditions where the API succeeds but we get
            # an exception due to rate limiting, network issues, JSON
            # parsing, etc.
            if state == "closed" and merged:
                self.log.info(
                    f"PR #{number} in {owner}/{repo} was successfully merged despite exception: {error}"
                )
                return True

            # Enhanced error message.  Always keep the original error text —
            # it carries the HTTP status line (e.g. "405 Method Not
            # Allowed") that ``_merge_pr_with_retry`` string-matches to
            # classify retryable vs terminal failures; dropping it made
            # every blocked/ruleset 405 fall through to the generic retry
            # path (3 attempts + sleeps).  Then *add* GitHub's response body
            # (the actionable reason) when we captured it.
            error_msg = (
                f"Failed to merge PR #{number} in {owner}/{repo}. Error: {str(error)}."
            )
            if github_detail:
                error_msg += f" GitHub: {github_detail}"
            error_msg += (
                f" (PR state: {state}, mergeable: {mergeable}, "
                f"mergeable_state: {mergeable_state})"
            )

            # Note common state-based causes for 405-style errors.
            if mergeable_state == "blocked":
                error_msg += " [blocked by branch protection / required checks]"
            elif mergeable_state == "behind":
                error_msg += " [PR branch is behind base branch]"
            elif mergeable_state == "dirty":
                error_msg += " [PR has merge conflicts]"
            elif draft:
                error_msg += " [cannot merge draft PR]"
            elif state == "closed" and not merged:
                error_msg += " [PR was closed without merging]"
            elif state != "open":
                error_msg += f" [PR is not open, state: {state}]"

            raise Exception(error_msg) from error
        except Exception as inner_e:
            # The enhanced-error path raised successfully (the message
            # starts with "Failed to merge PR") — propagate it unchanged.
            # A bare ``raise`` preserves ``inner_e`` together with its
            # existing ``__cause__`` (set to ``error`` above) and original
            # traceback, whereas ``raise inner_e from error`` would rewrite
            # the chaining.
            if "Failed to merge PR" in str(inner_e):
                raise
            # Otherwise the PR-state re-fetch itself failed.  Still surface
            # GitHub's response body (the actionable reason) when we
            # captured it, rather than dropping back to the bare
            # status-line ``HTTPStatusError``.
            if github_detail:
                raise Exception(
                    f"Failed to merge PR #{number} in {owner}/{repo}. "
                    f"Error: {str(error)}. GitHub: {github_detail}"
                ) from error
            raise error from inner_e

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
                # Unexpected response shape: the API returned 200 OK but
                # not the documented list of commits. We cannot determine
                # signature status from this, so we must not pretend every
                # commit is verified (the old fail-open ``(True, [])``
                # default collided with the signature-preservation gate in
                # ``rebase.py``). Surface the uncertainty to the caller.
                raise RuntimeError(
                    "Unexpected response shape from "
                    f"/repos/{owner}/{repo}/pulls/{number}/commits: "
                    f"expected a list, got {type(commits).__name__}"
                )

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

        Results are cached per ``owner/repo@branch`` for the session:
        the requirement is branch-protection/ruleset configuration that
        does not change while dependamerge runs, and the uncached path
        costs up to 3 + N requests (classic-protection probe, repo
        metadata, ruleset list, one detail GET per ruleset).  Verdicts
        derived from transient API errors are *not* cached, so a
        momentary outage cannot pin a wrong answer for the whole run.
        """
        cache_key = f"{owner}/{repo}@{branch}"
        cached = self._requires_signatures_cache.get(cache_key)
        if cached is not None:
            return cached
        result, reliable = await self._requires_commit_signatures_uncached(
            owner, repo, branch
        )
        if reliable:
            self._requires_signatures_cache[cache_key] = result
        return result

    async def _requires_commit_signatures_uncached(
        self, owner: str, repo: str, branch: str
    ) -> tuple[bool, bool]:
        """Uncached implementation of :meth:`requires_commit_signatures`.

        Returns:
            Tuple of ``(requires_signatures, reliable)``.  ``reliable``
            is False when a transient (non-404) API error prevented a
            definitive verdict — a ``True`` verdict is always reliable
            (positive evidence), but an error-derived ``False`` must
            not be cached because the requirement may simply have been
            unreadable at that moment.
        """
        reliable = True
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
                return True, True
        except Exception as e:
            # 404 → not enabled; other errors → continue checking rulesets
            if "404" not in str(e):
                reliable = False
                self.log.debug(
                    "Error checking classic signature requirement for %s/%s:%s: %s",
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
                    # An unreadable ruleset could hide a
                    # required_signatures rule — the eventual False
                    # verdict is no longer definitive.
                    reliable = False
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
                if isinstance(conditions, dict) and not self._ruleset_applies_to_branch(
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
                            return True, True
        except Exception as e:
            reliable = False
            self.log.debug(
                "Error checking rulesets for signature requirement on %s/%s:%s: %s",
                owner,
                repo,
                branch,
                e,
            )

        return False, reliable

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
        ref_name = conditions.get("ref_name", {})
        if not isinstance(ref_name, dict):
            return True  # No conditions — assume applies

        include = ref_name.get("include", [])
        exclude = ref_name.get("exclude", [])

        full_ref = f"refs/heads/{branch}"

        # Must match at least one include pattern (if any are specified)
        if include and not any(
            GitHubAsync._ref_pattern_matches(p, branch, full_ref, default_branch)
            for p in include
            if isinstance(p, str)
        ):
            return False

        # Must not match any exclude pattern
        if any(
            GitHubAsync._ref_pattern_matches(p, branch, full_ref, default_branch)
            for p in exclude
            if isinstance(p, str)
        ):
            return False

        return True

    @staticmethod
    def _ref_pattern_matches(
        pattern: str,
        branch: str,
        full_ref: str,
        default_branch: str | None,
    ) -> bool:
        """Check whether a single ruleset ref pattern matches *branch*.

        Defined as a static helper method (rather than a closure inside
        ``_ruleset_applies_to_branch``) so it is not re-created on every
        call and can be reused across the include/exclude comprehensions.
        """
        import fnmatch

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

        Results are cached per ``owner/repo@branch`` for the session:
        required-check configuration is repo/branch-level state that does
        not change while dependamerge runs, and the block-reason analysis
        consults it repeatedly (several times per blocked PR).  The
        uncached path costs 2 + N requests (repo + ruleset list + one
        detail GET per ruleset), so the cache saves a burst of API
        traffic on every repeat.  Results assembled while any of those
        requests failed are *not* cached: the fetch treats errors as
        "no required checks", and pinning that error-derived verdict
        for the whole session could misclassify blocked PRs long after
        a transient outage has passed.
        """
        cache_key = f"{owner}/{repo}@{branch}"
        cached = self._required_checks_cache.get(cache_key)
        if cached is not None:
            # Return a copy so callers cannot mutate the cached list.
            return list(cached)

        required_checks: list[dict[str, Any]] = []
        seen_contexts: set[str] = set()
        reliable = True

        # Resolve the repo's actual default branch so that ~DEFAULT_BRANCH
        # ruleset conditions are evaluated correctly (not hardcoded to
        # main/master).
        default_branch = await self._resolve_default_branch(owner, repo)

        # Try rulesets first (org-level and repo-level)
        try:
            rulesets = await self.get(f"/repos/{owner}/{repo}/rulesets?per_page=100")
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
                                for check in params.get("required_status_checks", []):
                                    if isinstance(check, dict) and check.get("context"):
                                        ctx = check["context"]
                                        if ctx not in seen_contexts:
                                            seen_contexts.add(ctx)
                                            required_checks.append(check)
                    except Exception as detail_err:
                        reliable = False
                        self.log.debug(
                            f"Could not fetch ruleset {ruleset_id} details: {detail_err}"
                        )
        except Exception as e:
            reliable = False
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
            except Exception as e:
                # Branch protection may be absent or inaccessible with
                # the current token; treat as no required checks.  A
                # plain 404 is the definitive "no protection" answer;
                # anything else leaves the verdict unreliable.
                if "404" not in str(e):
                    reliable = False

        if reliable:
            self._required_checks_cache[cache_key] = list(required_checks)
        return required_checks

    async def get_branch_protection(
        self, owner: str, repo: str, branch: str
    ) -> dict[str, Any]:
        """
        Get branch protection rules for a branch.

        REST: GET /repos/{owner}/{repo}/branches/{branch}/protection

        Results (including the empty "no protection" result) are cached
        per ``owner/repo@branch`` for the session: the merge pipeline
        calls this once per PR via ``_check_merge_requirements``, but
        protection config is branch-level state that does not change
        mid-run.  Errors other than 404 are not cached so a transient
        failure can succeed on retry.
        """
        cache_key = f"{owner}/{repo}@{branch}"
        cached = self._branch_protection_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            protection_data = await self.get(
                f"/repos/{owner}/{repo}/branches/{branch}/protection"
            )
            # Branch protection data should always be a dict, not a list
            result = protection_data if isinstance(protection_data, dict) else {}
            self._branch_protection_cache[cache_key] = result
            return result
        except Exception as e:
            # Branch protection might not be enabled, return empty dict
            if "404" in str(e):
                self._branch_protection_cache[cache_key] = {}
                return {}
            raise

    async def get_authenticated_user_login(self) -> str | None:
        """Return the authenticated user's login, cached for the session.

        The login never changes for a given token, so the ``/user``
        round-trip is paid at most once per client instance.  Returns
        ``None`` when the lookup fails (callers should degrade
        gracefully); failures are not cached so a transient error can
        recover on the next call.
        """
        if self._authenticated_user_login is None:
            try:
                user_data = await self.get("/user")
            except Exception as e:
                self.log.debug("Could not resolve authenticated user: %s", e)
                return None
            if isinstance(user_data, dict):
                login = user_data.get("login")
                if isinstance(login, str) and login:
                    self._authenticated_user_login = login
        return self._authenticated_user_login

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

    async def get_token_scopes(self) -> set[str] | None:
        """Return the OAuth scopes granted to a classic personal access token.

        Classic PATs advertise their granted scopes in the
        ``X-OAuth-Scopes`` response header on every authenticated request.
        Fine-grained PATs and GitHub App installation tokens do **not** send
        this header — their permission model is per-resource and cannot be
        introspected this way.

        Returns:
            A ``set`` of scope strings for a classic PAT (possibly empty if
            the token was created with no scopes selected), or ``None`` when
            the token type does not expose scopes (fine-grained PAT / app
            token) or the lookup could not be performed.  Callers MUST treat
            ``None`` as "undeterminable", never as "no scopes granted".
        """
        if self._token_scopes_fetched:
            return self._token_scopes

        try:
            # Any authenticated REST endpoint echoes the header.
            # ``/rate_limit`` is the cheapest and is itself exempt from the
            # primary rate limit, so it never consumes quota.
            r = await self._request("GET", f"{self.api_url}/rate_limit")
        except Exception as e:
            # A transient probe failure must NOT be cached as
            # "undeterminable": doing so would let a one-off network error
            # suppress accurate scope diagnosis for the rest of the run
            # (a classic PAT that has ``workflow`` could still be reported
            # as missing it).  Leave the cache unset so a later call can
            # retry and produce an accurate result.
            self.log.debug("Could not determine token scopes: %s", e)
            return None

        raw = r.headers.get("X-OAuth-Scopes")
        if raw is None:
            # Header absent on a successful probe → fine-grained / app
            # token.  The scope set is genuinely undeterminable; cache it.
            self._token_scopes = None
        else:
            # Header present (possibly empty for a scope-less classic PAT).
            self._token_scopes = {s.strip() for s in raw.split(",") if s.strip()}
        self._token_scopes_fetched = True
        return self._token_scopes

    async def check_workflow_scope(self) -> bool | None:
        """Determine whether the token may update GitHub Actions workflows.

        Merging a PR that touches ``.github/workflows/**`` requires the
        classic ``workflow`` scope (or, for fine-grained PATs, the
        ``Workflows: Read and write`` permission).

        Returns:
            ``True``  — classic PAT that carries the ``workflow`` scope.
            ``False`` — classic PAT that is missing the ``workflow`` scope.
            ``None``  — the token type cannot be introspected (fine-grained
            PAT / app token).  The requirement cannot be verified up-front;
            callers should defer to merge-time error handling.
        """
        scopes = await self.get_token_scopes()
        if scopes is None:
            return None
        return "workflow" in scopes

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
                if (
                    operation in ("approve", "merge", "close", "update_branch")
                    and owner
                    and repo
                ):
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
                        repo_data = await self.get(f"/repos/{owner}/{repo}")
                        if isinstance(repo_data, dict):
                            default_branch = repo_data.get("default_branch", "main")
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

                elif operation == "merge_workflow":
                    # Verify the token may merge PRs that modify GitHub
                    # Actions workflow files.  This is only checkable for
                    # classic PATs, which advertise their scopes via the
                    # ``X-OAuth-Scopes`` header.  Fine-grained PATs and app
                    # tokens do not expose scopes, so the check returns
                    # ``None`` and we pass it through here — the requirement
                    # cannot be verified up-front for those token types and
                    # is instead surfaced (with accurate guidance) by the
                    # merge-time handler if it actually bites.
                    has_workflow = await self.check_workflow_scope()
                    if has_workflow is False:
                        perms = OPERATION_PERMISSIONS.get("merge_workflow", {})
                        result["error"] = (
                            "Token is missing the 'workflow' scope, which is "
                            "required to merge pull requests that modify "
                            "GitHub Actions workflow files "
                            "(.github/workflows/**)"
                        )
                        result["guidance"] = {
                            "classic": perms.get("classic"),
                            "fine_grained": perms.get("fine_grained"),
                            "fix": "Run: gh auth refresh -h github.com -s workflow",
                        }
                    else:
                        # ``True`` (scope present) or ``None``
                        # (undeterminable token type) — do not block.
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

    async def get_behind_by(
        self, owner: str, repo: str, base_ref: str, head_sha: str
    ) -> int | None:
        """Return how many commits ``head_sha`` is behind ``base_ref``.

        GitHub's ``mergeable_state`` is a single value, so ``blocked``
        (a failing required check) masks ``behind`` (a stale head).
        This helper answers the staleness question independently via
        the compare API, which works regardless of the reported
        mergeable state and regardless of whether the head lives on a
        fork (the SHA is resolvable in the base repository's network).

        Args:
            owner: Base repository owner
            repo: Base repository name
            base_ref: Base branch name (e.g. ``main``)
            head_sha: Head commit SHA of the pull request

        Returns:
            The ``behind_by`` commit count, or ``None`` when the
            comparison could not be performed (API error, unexpected
            payload).  ``None`` means "unknown": callers must not
            interpret it as ``behind_by == 0`` ("up to date"), and
            staleness-driven write actions (e.g. requesting a rebase)
            should require positive evidence (``behind_by > 0``)
            rather than acting on an unknown — the pattern used by
            ``AsyncMergeManager._blocked_pr_needs_rebase``.
        """
        encoded_base = quote(base_ref, safe="")
        try:
            comparison = await self.get(
                f"/repos/{owner}/{repo}/compare/{encoded_base}...{head_sha}"
            )
        except Exception as exc:
            self.log.debug(
                "Compare %s...%s failed for %s/%s: %s",
                base_ref,
                head_sha,
                owner,
                repo,
                exc,
            )
            return None
        if isinstance(comparison, dict):
            behind = comparison.get("behind_by")
            if isinstance(behind, int):
                return behind
        return None

    async def analyze_block_reason(
        self,
        owner: str,
        repo: str,
        number: int,
        head_sha: str,
        base_branch: str | None = None,
    ) -> str:
        """
        Analyze why a PR is blocked and return appropriate status.

        This is the async version that should be used from async contexts.

        ``base_branch`` lets callers that already know the PR's base ref
        (e.g. the merge pipeline, which carries it on ``PullRequestInfo``)
        skip the PR-detail fetch this method otherwise performs just to
        read ``base.ref`` — one request saved per invocation, and this
        method runs several times per blocked PR.
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
                        if is_copilot(author):
                            unresolved_copilot_reviews += 1
                        else:
                            human_changes_requested = True
        except Exception:
            # Review data is best-effort; on API error leave the
            # approval/changes flags at their safe defaults.
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
                    if is_copilot(author):
                        # Simple heuristic: if comment doesn't have "DISMISSED" or similar resolution text
                        body = comment.get("body", "").lower()
                        if "dismissed" not in body and "resolved" not in body:
                            unresolved_copilot_comments += 1
        except Exception:
            # Review comments are best-effort; ignore fetch errors and
            # leave the Copilot comment count unchanged.
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
            # Check-runs API may be unavailable; proceed with whatever
            # checks were collected so far.
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
            # Status API may be unavailable; proceed with whatever
            # status contexts were collected so far.
            pass

        # Detect missing/pending required status checks (e.g. stale pre-commit.ci)
        missing_required_checks: list[str] = []
        pending_required_checks: list[str] = []
        # Resolve the PR's actual base branch.  It drives both the
        # required status-check lookup and the final guard-kind
        # classification, so a wrong value (e.g. assuming "main" on a repo
        # that defaults to "master") produces a misleading block reason.
        # Prefer the caller-supplied value, then the PR's own base ref; if
        # neither is available, fall back to the repository's real default
        # branch rather than a hardcoded name, and only give up (leaving
        # it ``None``) when nothing can be determined.
        if base_branch is None:
            try:
                pr_data = await self.get(f"/repos/{owner}/{repo}/pulls/{number}")
                if isinstance(pr_data, dict):
                    ref = pr_data.get("base", {}).get("ref")
                    if isinstance(ref, str) and ref:
                        base_branch = ref
            except Exception as pr_err:
                self.log.debug(
                    f"Could not read base branch for {owner}/{repo}#{number}: {pr_err}"
                )

        if base_branch is None:
            base_branch = await self._resolve_default_branch(owner, repo)

        # Only inspect required status checks when we know which branch to
        # query; an assumed branch would yield checks for the wrong ref.
        if base_branch is not None:
            try:
                required_checks = await self.get_required_status_checks(
                    owner, repo, base_branch
                )
                for check in required_checks:
                    ctx = check.get("context", "")
                    if not ctx:
                        continue
                    if ctx in reported_check_names:
                        # Check reported — distinguish pending from completed
                        if (
                            ctx not in completed_check_names
                            and ctx in pending_check_names
                        ):
                            pending_required_checks.append(ctx)
                    else:
                        # Never reported via either API — truly missing
                        missing_required_checks.append(ctx)
            except Exception as req_err:
                self.log.debug(
                    f"Could not check required status checks for "
                    f"{owner}/{repo}#{number}: {req_err}"
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
                return (
                    f"Blocked by missing required status: {missing_required_checks[0]}"
                )
            else:
                names = ", ".join(missing_required_checks)
                return f"Blocked by {len(missing_required_checks)} missing required statuses: {names}"

        if pending_required_checks:
            if len(pending_required_checks) == 1:
                return (
                    f"Blocked by pending required check: {pending_required_checks[0]}"
                )
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

        # No self-describing blocker was found: checks pass, the PR is
        # approved, and no changes are requested — yet GitHub still reports
        # the PR as blocked.  Rather than *asserting* "branch protection"
        # (which is invisible to this code path when the repository uses
        # rulesets), determine what kind of rule actually guards the branch
        # and keep the wording non-committal: we know the branch is guarded,
        # not that a specific condition is failing.
        if base_branch is None:
            # The base branch could not be resolved, so no branch-specific
            # inspection ran.  Say exactly that rather than implying we
            # looked for protection rules and found none.
            return (
                "Blocked for an undetermined reason "
                "(GitHub reports the PR as blocked, but the PR's base "
                "branch could not be determined, so its protection rules "
                "and required checks could not be inspected)"
            )
        kind = await self._detect_branch_protection_kind(owner, repo, base_branch)
        if kind == "ruleset":
            return (
                "Blocked by repository ruleset (no specific failing condition detected)"
            )
        if kind == "protection":
            return (
                "Blocked by branch protection (no specific failing condition detected)"
            )
        return (
            "Blocked for an undetermined reason "
            "(GitHub reports the PR as blocked but no failing checks, "
            "required reviews, or visible protection rules were found; "
            "the repository may use rulesets this token cannot read)"
        )

    async def _resolve_default_branch(self, owner: str, repo: str) -> str | None:
        """Return the repository's actual default branch, or ``None``.

        Many repositories default to ``master`` rather than ``main``, so
        callers must never assume a name.  This reads the authoritative
        ``default_branch`` field from the repository metadata and returns
        ``None`` when it cannot be determined (the repo is unreadable or
        the field is absent), letting callers degrade gracefully instead
        of operating on a wrong branch.

        Successful lookups are cached per ``owner/repo`` for the
        session (a repo's default branch does not change mid-run);
        failures are not cached so a transient error can recover.
        """
        cache_key = f"{owner}/{repo}"
        if cache_key in self._default_branch_cache:
            return self._default_branch_cache[cache_key]
        try:
            repo_data = await self.get(f"/repos/{owner}/{repo}")
        except Exception as e:
            self.log.debug(
                "Could not resolve default branch for %s/%s: %s", owner, repo, e
            )
            return None
        if isinstance(repo_data, dict):
            default_branch = repo_data.get("default_branch")
            if isinstance(default_branch, str) and default_branch:
                self._default_branch_cache[cache_key] = default_branch
                return default_branch
        return None

    async def _detect_branch_protection_kind(
        self, owner: str, repo: str, branch: str
    ) -> str:
        """Best-effort classification of what guards a branch.

        Used by :meth:`analyze_block_reason` to describe an otherwise
        unexplained ``BLOCKED`` state accurately instead of asserting
        "branch protection".

        Returns:
            ``"ruleset"``    — one or more repository rulesets apply to the
            branch (reported in preference to classic protection because
            rulesets are invisible to the GraphQL ``branchProtectionRule``
            field and are what most current repositories use).
            ``"protection"`` — a classic branch protection rule applies.
            ``"none"``       — neither could be found (the branch appears
            unguarded, or the token cannot read the configuration).
        """
        # Repository rulesets (newer API): the effective-rules endpoint
        # returns every rule that applies to the branch from any active
        # ruleset.  A non-empty list means a ruleset guards the branch.
        # Branch names can contain '/' (e.g. ``release/v1``), so they must
        # be URL-encoded before interpolation into the REST path.
        encoded_branch = quote(branch, safe="")
        try:
            rules = await self.get(
                f"/repos/{owner}/{repo}/rules/branches/{encoded_branch}"
            )
            if isinstance(rules, list) and rules:
                return "ruleset"
        except Exception as e:
            self.log.debug(
                "Could not read branch rules for %s/%s:%s: %s",
                owner,
                repo,
                branch,
                e,
            )

        # Classic branch protection: 200 = protected, 404 = no rule.
        try:
            await self.get(
                f"/repos/{owner}/{repo}/branches/{encoded_branch}/protection"
            )
            return "protection"
        except Exception as e:
            if "404" not in str(e):
                self.log.debug(
                    "Could not read branch protection for %s/%s:%s: %s",
                    owner,
                    repo,
                    branch,
                    e,
                )

        return "none"

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

        # Estimate request rate (very rough heuristic): we only track
        # errors, not total requests, so assume each error accompanied
        # ``_ESTIMATED_REQUESTS_PER_ERROR`` requests in the window. See the
        # constant's definition for the rationale and tuning guidance.
        estimated_requests = max(
            len(recent_errors) * self._ESTIMATED_REQUESTS_PER_ERROR, 1
        )
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
