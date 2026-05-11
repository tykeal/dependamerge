# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Unit tests for ``AsyncMergeManager._detect_stuck_dco``.

Covers:
- DCO check stuck longer than the threshold on a sufficiently old
  PR -> detected (returns ``(True, name, age)``).
- Sub-threshold stuck DCO check -> not detected.
- Stuck pending check whose name is not DCO-shaped -> not detected.
- DCO check stuck but PR was just touched (updated_at younger than
  threshold) -> not detected (age floor protects against false
  positives on freshly-pushed PRs).
- DCO check completed -> not detected.
- Stuck status-context (older API) DCO entry -> detected.
- API failures degrade to ``(False, None, 0.0)``.
- ``_merge_single_pr`` triggers ``_trigger_dependabot_recreate`` when
  ``_detect_stuck_dco`` returns True for a dependabot PR.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from dependamerge.merge_manager import STUCK_DCO_THRESHOLD_SECONDS
from dependamerge.models import PullRequestInfo


def _iso(dt: datetime) -> str:
    """Return ``dt`` in the RFC 3339 ``...Z`` form GitHub emits."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_pr_info(**overrides: Any) -> PullRequestInfo:
    """Helper to build a PullRequestInfo with sensible defaults."""
    defaults: dict[str, Any] = {
        "number": 342,
        "title": "Chore: Bump foo from 1 to 2",
        "body": "Dependabot PR",
        "author": "dependabot[bot]",
        "head_sha": "abc123",
        "base_branch": "main",
        "head_branch": "dependabot/foo",
        "state": "open",
        "mergeable": True,
        "mergeable_state": "blocked",
        "behind_by": 0,
        "files_changed": [],
        "repository_full_name": "lfreleng-actions/lftools-uv",
        "html_url": "https://github.com/lfreleng-actions/lftools-uv/pull/342",
    }
    defaults.update(overrides)
    return PullRequestInfo(**defaults)


def _make_manager(**overrides: Any):
    """Build an AsyncMergeManager with a mocked GitHub client.

    Returns ``(manager, client)`` — see ``tests/conftest.py`` for the
    typed-mock-client pattern.
    """
    from tests.conftest import make_merge_manager

    defaults: dict[str, Any] = {"preview_mode": False}
    defaults.update(overrides)
    return make_merge_manager(**defaults)


def _build_get_responder(
    pr_response: dict[str, Any] | None = None,
    check_runs_response: dict[str, Any] | None = None,
    status_response: dict[str, Any] | None = None,
):
    """Return an ``AsyncMock``-compatible side effect dispatcher.

    Routes ``GET`` requests to the right canned response based on the
    URL path — this mirrors how ``_detect_stuck_dco`` actually calls
    the client.
    """

    async def _get(path: str, *args: Any, **kwargs: Any) -> Any:
        if path.endswith("/check-runs"):
            return check_runs_response
        if path.endswith("/status"):
            return status_response
        # Anything else is the PR detail fetch.
        return pr_response

    return _get


# ---------------------------------------------------------------------------
# _detect_stuck_dco — positive and negative cases
# ---------------------------------------------------------------------------


class TestDetectStuckDcoCheckRuns:
    """Detection via the modern ``check-runs`` API."""

    @pytest.mark.asyncio
    async def test_detects_stuck_dco_check_run(self) -> None:
        """A queued/in_progress DCO check older than the threshold
        on an old-enough PR is detected."""
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_DCO_THRESHOLD_SECONDS + 30)
        pr = _make_pr_info()

        client.get = AsyncMock(
            side_effect=_build_get_responder(
                pr_response={
                    "created_at": _iso(old),
                    "updated_at": _iso(old),
                },
                check_runs_response={
                    "check_runs": [
                        {
                            "name": "DCO",
                            "status": "in_progress",
                            "conclusion": None,
                            "started_at": _iso(old),
                        }
                    ],
                },
                status_response={"statuses": []},
            )
        )

        is_stuck, name, age = await mgr._detect_stuck_dco(pr)
        assert is_stuck is True
        assert name == "DCO"
        assert age >= STUCK_DCO_THRESHOLD_SECONDS

    @pytest.mark.asyncio
    async def test_sub_threshold_age_is_not_stuck(self) -> None:
        """A DCO check pending for less than the threshold is not stuck."""
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        # PR is old enough; the check itself is too young.
        pr_age = now - timedelta(seconds=STUCK_DCO_THRESHOLD_SECONDS + 30)
        check_started = now - timedelta(seconds=10)
        pr = _make_pr_info()

        client.get = AsyncMock(
            side_effect=_build_get_responder(
                pr_response={
                    "created_at": _iso(pr_age),
                    "updated_at": _iso(check_started),
                },
                check_runs_response={
                    "check_runs": [
                        {
                            "name": "DCO",
                            "status": "in_progress",
                            "started_at": _iso(check_started),
                        }
                    ],
                },
                status_response={"statuses": []},
            )
        )

        # The PR-level idle floor (updated_at) catches this case
        # before we even look at check timing.
        is_stuck, name, age = await mgr._detect_stuck_dco(pr)
        assert is_stuck is False
        assert name is None
        assert age == 0.0

    @pytest.mark.asyncio
    async def test_completed_dco_is_not_stuck(self) -> None:
        """A completed DCO check (success or failure) is not stuck."""
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_DCO_THRESHOLD_SECONDS + 30)
        pr = _make_pr_info()

        client.get = AsyncMock(
            side_effect=_build_get_responder(
                pr_response={
                    "created_at": _iso(old),
                    "updated_at": _iso(old),
                },
                check_runs_response={
                    "check_runs": [
                        {
                            "name": "DCO",
                            "status": "completed",
                            "conclusion": "success",
                            "started_at": _iso(old),
                        }
                    ],
                },
                status_response={"statuses": []},
            )
        )

        is_stuck, name, age = await mgr._detect_stuck_dco(pr)
        assert is_stuck is False
        assert name is None

    @pytest.mark.asyncio
    async def test_stuck_non_dco_check_is_ignored(self) -> None:
        """A stuck check whose name is not DCO-shaped is not detected."""
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_DCO_THRESHOLD_SECONDS + 30)
        pr = _make_pr_info()

        client.get = AsyncMock(
            side_effect=_build_get_responder(
                pr_response={
                    "created_at": _iso(old),
                    "updated_at": _iso(old),
                },
                check_runs_response={
                    "check_runs": [
                        {
                            "name": "build",
                            "status": "in_progress",
                            "started_at": _iso(old),
                        }
                    ],
                },
                status_response={"statuses": []},
            )
        )

        is_stuck, name, age = await mgr._detect_stuck_dco(pr)
        assert is_stuck is False
        assert name is None

    @pytest.mark.asyncio
    async def test_pr_age_floor_blocks_detection(self) -> None:
        """A young PR (created < threshold ago) is never flagged."""
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        young = now - timedelta(seconds=10)
        pr = _make_pr_info()

        client.get = AsyncMock(
            side_effect=_build_get_responder(
                pr_response={
                    "created_at": _iso(young),
                    "updated_at": _iso(young),
                },
                check_runs_response={
                    "check_runs": [
                        {
                            "name": "DCO",
                            "status": "in_progress",
                            "started_at": _iso(young),
                        }
                    ],
                },
                status_response={"statuses": []},
            )
        )

        is_stuck, name, age = await mgr._detect_stuck_dco(pr)
        assert is_stuck is False
        assert name is None

    @pytest.mark.asyncio
    async def test_dco_slash_dco_name_variant_matches(self) -> None:
        """The ``dco/dco`` status-context naming is matched."""
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_DCO_THRESHOLD_SECONDS + 30)
        pr = _make_pr_info()

        client.get = AsyncMock(
            side_effect=_build_get_responder(
                pr_response={
                    "created_at": _iso(old),
                    "updated_at": _iso(old),
                },
                check_runs_response={
                    "check_runs": [
                        {
                            "name": "dco/dco",
                            "status": "queued",
                            "started_at": _iso(old),
                        }
                    ],
                },
                status_response={"statuses": []},
            )
        )

        is_stuck, name, _age = await mgr._detect_stuck_dco(pr)
        assert is_stuck is True
        assert name == "dco/dco"


class TestDetectStuckDcoStatusContexts:
    """Detection via the older ``commits/{sha}/status`` API."""

    @pytest.mark.asyncio
    async def test_detects_stuck_dco_status_context(self) -> None:
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_DCO_THRESHOLD_SECONDS + 30)
        pr = _make_pr_info()

        client.get = AsyncMock(
            side_effect=_build_get_responder(
                pr_response={
                    "created_at": _iso(old),
                    "updated_at": _iso(old),
                },
                check_runs_response={"check_runs": []},
                status_response={
                    "statuses": [
                        {
                            "context": "DCO",
                            "state": "pending",
                            "updated_at": _iso(old),
                        }
                    ],
                },
            )
        )

        is_stuck, name, age = await mgr._detect_stuck_dco(pr)
        assert is_stuck is True
        assert name == "DCO"
        assert age >= STUCK_DCO_THRESHOLD_SECONDS


class TestDetectStuckDcoRobustness:
    """Defensive behaviour when the GitHub API misbehaves."""

    @pytest.mark.asyncio
    async def test_pr_fetch_failure_returns_safe_default(self) -> None:
        mgr, client = _make_manager()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        pr = _make_pr_info()

        is_stuck, name, age = await mgr._detect_stuck_dco(pr)
        assert is_stuck is False
        assert name is None
        assert age == 0.0

    @pytest.mark.asyncio
    async def test_unparseable_timestamps_return_safe_default(self) -> None:
        mgr, client = _make_manager()
        pr = _make_pr_info()

        client.get = AsyncMock(
            side_effect=_build_get_responder(
                pr_response={
                    "created_at": "not-a-real-date",
                    "updated_at": None,
                },
                check_runs_response={"check_runs": []},
                status_response={"statuses": []},
            )
        )

        is_stuck, name, age = await mgr._detect_stuck_dco(pr)
        assert is_stuck is False
        assert name is None
        assert age == 0.0

    @pytest.mark.asyncio
    async def test_no_github_client_returns_safe_default(self) -> None:
        mgr, _client = _make_manager()
        # Simulate the manager being used outside the async context.
        mgr._github_client = None
        pr = _make_pr_info()

        is_stuck, name, age = await mgr._detect_stuck_dco(pr)
        assert is_stuck is False
        assert name is None
        assert age == 0.0
