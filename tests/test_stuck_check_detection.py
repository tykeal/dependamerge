# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Unit tests for ``AsyncMergeManager._detect_stuck_required_check``.

Covers:
- DCO check stuck longer than the threshold on a sufficiently old
  PR -> detected (returns ``(True, name, age)``).
- Sub-threshold stuck DCO check -> not detected.
- Stuck pending check whose name is not DCO-shaped and not required
  -> not detected.
- A stuck *required* non-DCO check (e.g. ``build``) -> detected.
- A stuck *required* pre-commit.ci check -> not detected (it has its
  own recovery via ``_trigger_stale_precommit_ci``).
- DCO check stuck but PR was just touched (updated_at younger than
  threshold) -> not detected (age floor protects against false
  positives on freshly-pushed PRs).
- DCO check completed -> not detected.
- Stuck status-context (older API) DCO entry -> detected.
- API failures degrade to ``(False, None, 0.0)``.
- ``_merge_single_pr`` triggers ``_trigger_dependabot_recreate`` when
  ``_detect_stuck_required_check`` returns True for a dependabot PR.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from dependamerge.merge_manager import (
    STUCK_CHECK_THRESHOLD_SECONDS,
    MergeResult,
    MergeStatus,
)
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
    URL path — this mirrors how ``_detect_stuck_required_check`` actually calls
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
# _detect_stuck_required_check — positive and negative cases
# ---------------------------------------------------------------------------


class TestDetectStuckDcoCheckRuns:
    """Detection via the modern ``check-runs`` API."""

    @pytest.mark.asyncio
    async def test_detects_stuck_dco_check_run(self) -> None:
        """A queued/in_progress DCO check older than the threshold
        on an old-enough PR is detected."""
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_CHECK_THRESHOLD_SECONDS + 30)
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

        is_stuck, name, age = await mgr._detect_stuck_required_check(pr)
        assert is_stuck is True
        assert name == "DCO"
        assert age >= STUCK_CHECK_THRESHOLD_SECONDS

    @pytest.mark.asyncio
    async def test_sub_threshold_age_is_not_stuck(self) -> None:
        """A DCO check pending for less than the threshold is not stuck."""
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        # PR is old enough; the check itself is too young.
        pr_age = now - timedelta(seconds=STUCK_CHECK_THRESHOLD_SECONDS + 30)
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
        is_stuck, name, age = await mgr._detect_stuck_required_check(pr)
        assert is_stuck is False
        assert name is None
        assert age == 0.0

    @pytest.mark.asyncio
    async def test_completed_dco_is_not_stuck(self) -> None:
        """A completed DCO check (success or failure) is not stuck."""
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_CHECK_THRESHOLD_SECONDS + 30)
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

        is_stuck, name, age = await mgr._detect_stuck_required_check(pr)
        assert is_stuck is False
        assert name is None

    @pytest.mark.asyncio
    async def test_stuck_non_dco_non_required_check_is_ignored(self) -> None:
        """A stuck check that is neither DCO-shaped nor required on
        the base branch is not detected."""
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_CHECK_THRESHOLD_SECONDS + 30)
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

        is_stuck, name, age = await mgr._detect_stuck_required_check(pr)
        assert is_stuck is False
        assert name is None

    @pytest.mark.asyncio
    async def test_detects_stuck_required_non_dco_check(self) -> None:
        """A stuck *required* non-DCO check (e.g. ``build``) is detected.

        Generalises the original DCO-only behaviour: any required
        verification check that stalls indefinitely should drive the
        dependabot recreate recovery.
        """
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_CHECK_THRESHOLD_SECONDS + 30)
        pr = _make_pr_info()

        # ``build`` is required on the base branch.
        client.get_required_status_checks = AsyncMock(
            return_value=[{"context": "build"}]
        )
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

        is_stuck, name, age = await mgr._detect_stuck_required_check(pr)
        assert is_stuck is True
        assert name == "build"
        assert age >= STUCK_CHECK_THRESHOLD_SECONDS

    @pytest.mark.asyncio
    async def test_stuck_required_precommit_ci_is_ignored(self) -> None:
        """A stuck *required* pre-commit.ci check is not detected here.

        pre-commit.ci has its own recovery path
        (``_trigger_stale_precommit_ci`` posts ``pre-commit.ci run``);
        dependabot's ``recreate`` macro does not retrigger it, so it
        must be excluded from the recreate-driving detector even when
        it is a required, stuck check.
        """
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_CHECK_THRESHOLD_SECONDS + 30)
        pr = _make_pr_info()

        client.get_required_status_checks = AsyncMock(
            return_value=[{"context": "pre-commit.ci - pr"}]
        )
        client.get = AsyncMock(
            side_effect=_build_get_responder(
                pr_response={
                    "created_at": _iso(old),
                    "updated_at": _iso(old),
                },
                check_runs_response={
                    "check_runs": [
                        {
                            "name": "pre-commit.ci - pr",
                            "status": "in_progress",
                            "started_at": _iso(old),
                        }
                    ],
                },
                status_response={"statuses": []},
            )
        )

        is_stuck, name, _age = await mgr._detect_stuck_required_check(pr)
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

        is_stuck, name, age = await mgr._detect_stuck_required_check(pr)
        assert is_stuck is False
        assert name is None

    @pytest.mark.asyncio
    async def test_naive_pr_timestamp_does_not_crash(self) -> None:
        """A tz-naive PR timestamp fails closed instead of crashing.

        ``_parse_ts`` would otherwise return a naive datetime that
        raises ``TypeError`` when subtracted from the tz-aware ``now``;
        it must degrade to ``None`` so the detector returns
        ``(False, None, 0.0)`` rather than aborting the merge run.
        """
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_CHECK_THRESHOLD_SECONDS + 30)
        pr = _make_pr_info()

        client.get = AsyncMock(
            side_effect=_build_get_responder(
                pr_response={
                    # No trailing "Z"/offset -> naive datetimes.
                    "created_at": "2026-06-08T16:00:00",
                    "updated_at": "2026-06-08T16:00:00",
                },
                check_runs_response={
                    "check_runs": [
                        {
                            "name": "DCO",
                            "status": "in_progress",
                            "started_at": _iso(old),
                        }
                    ],
                },
                status_response={"statuses": []},
            )
        )

        is_stuck, name, age = await mgr._detect_stuck_required_check(pr)
        assert is_stuck is False
        assert name is None
        assert age == 0.0

    @pytest.mark.asyncio
    async def test_dco_slash_dco_name_variant_matches(self) -> None:
        """The ``dco/dco`` status-context naming is matched."""
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_CHECK_THRESHOLD_SECONDS + 30)
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

        is_stuck, name, _age = await mgr._detect_stuck_required_check(pr)
        assert is_stuck is True
        assert name == "dco/dco"


class TestDetectStuckDcoStatusContexts:
    """Detection via the older ``commits/{sha}/status`` API."""

    @pytest.mark.asyncio
    async def test_detects_stuck_dco_status_context(self) -> None:
        mgr, client = _make_manager()
        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=STUCK_CHECK_THRESHOLD_SECONDS + 30)
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

        is_stuck, name, age = await mgr._detect_stuck_required_check(pr)
        assert is_stuck is True
        assert name == "DCO"
        assert age >= STUCK_CHECK_THRESHOLD_SECONDS


class TestDetectStuckDcoRobustness:
    """Defensive behaviour when the GitHub API misbehaves."""

    @pytest.mark.asyncio
    async def test_pr_fetch_failure_returns_safe_default(self) -> None:
        mgr, client = _make_manager()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        pr = _make_pr_info()

        is_stuck, name, age = await mgr._detect_stuck_required_check(pr)
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

        is_stuck, name, age = await mgr._detect_stuck_required_check(pr)
        assert is_stuck is False
        assert name is None
        assert age == 0.0

    @pytest.mark.asyncio
    async def test_no_github_client_returns_safe_default(self) -> None:
        mgr, _client = _make_manager()
        # Simulate the manager being used outside the async context.
        mgr._github_client = None
        pr = _make_pr_info()

        is_stuck, name, age = await mgr._detect_stuck_required_check(pr)
        assert is_stuck is False
        assert name is None
        assert age == 0.0


def _printed(console_mock) -> str:
    """Join the positional text of every ``console.print`` call."""
    return " ".join(
        str(call.args[0]) for call in console_mock.print.call_args_list if call.args
    )


class TestReportMergeFailure:
    """Non-dependabot stuck-check reporting in ``_report_merge_failure``."""

    @pytest.mark.asyncio
    async def test_stuck_check_reports_and_arms_auto_merge(self) -> None:
        """A stuck check yields the ⚠️ line and arms auto-merge (non-dirty)."""
        mgr, _client = _make_manager()
        pr = _make_pr_info(author="someuser", mergeable_state="blocked")
        result = MergeResult(pr_info=pr, status=MergeStatus.PENDING)
        with (
            patch.object(
                mgr,
                "_detect_stuck_required_check",
                new_callable=AsyncMock,
                return_value=(True, "DCO", 120.0),
            ),
            patch.object(
                mgr,
                "_enable_auto_merge_for_pr",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_enable,
            patch.object(mgr, "_console") as mock_console,
        ):
            out = await mgr._report_merge_failure(pr, "o", "r", result, "blocked")

        assert out.status == MergeStatus.FAILED
        assert out.error == "stuck check: DCO"
        mock_enable.assert_awaited_once()
        printed = _printed(mock_console)
        assert "⚠️ Stuck check" in printed
        # The generic failure line is suppressed for stuck PRs.
        assert "❌ Failed" not in printed

    @pytest.mark.asyncio
    async def test_stuck_check_when_dirty_does_not_arm_auto_merge(self) -> None:
        """A dirty PR cannot take auto-merge, so it is not armed."""
        mgr, _client = _make_manager()
        pr = _make_pr_info(author="someuser", mergeable_state="dirty")
        result = MergeResult(pr_info=pr, status=MergeStatus.PENDING)
        with (
            patch.object(
                mgr,
                "_detect_stuck_required_check",
                new_callable=AsyncMock,
                return_value=(True, "DCO", 120.0),
            ),
            patch.object(
                mgr, "_enable_auto_merge_for_pr", new_callable=AsyncMock
            ) as mock_enable,
            patch.object(mgr, "_console"),
        ):
            out = await mgr._report_merge_failure(
                pr, "o", "r", result, "merge conflicts"
            )

        assert out.status == MergeStatus.FAILED
        mock_enable.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_stuck_check_emits_generic_failure(self) -> None:
        mgr, _client = _make_manager()
        pr = _make_pr_info(author="someuser")
        result = MergeResult(pr_info=pr, status=MergeStatus.PENDING)
        with (
            patch.object(
                mgr,
                "_detect_stuck_required_check",
                new_callable=AsyncMock,
                return_value=(False, None, 0.0),
            ),
            patch.object(mgr, "_console") as mock_console,
        ):
            out = await mgr._report_merge_failure(
                pr, "o", "r", result, "branch protection rules prevent merge"
            )

        assert out.status == MergeStatus.FAILED
        # The result error now surfaces the real reason so the
        # end-of-run summary is informative (was a generic message).
        assert out.error == "branch protection rules prevent merge"
        # The inline line is now terse (URL only); the reason is
        # reserved for the end-of-run summary to avoid duplication.
        printed = _printed(mock_console)
        assert "❌ Failed" in printed
        assert "branch protection" not in printed

    @pytest.mark.asyncio
    async def test_dependabot_skips_stuck_detection(self) -> None:
        """For dependabot the recreate path handles stuck checks, not here."""
        mgr, _client = _make_manager()
        pr = _make_pr_info(author="dependabot[bot]")
        result = MergeResult(pr_info=pr, status=MergeStatus.PENDING)
        with (
            patch.object(
                mgr, "_detect_stuck_required_check", new_callable=AsyncMock
            ) as mock_detect,
            patch.object(mgr, "_console"),
        ):
            out = await mgr._report_merge_failure(pr, "o", "r", result, "blocked")

        assert out.status == MergeStatus.FAILED
        mock_detect.assert_not_called()
