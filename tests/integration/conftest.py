# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Shared fixtures and helpers for the live integration test suite.

Every test in :mod:`tests.integration` is marked ``integration`` and is
designed to *fail safe*: it runs the real ``dependamerge`` CLI against
live GitHub / Gerrit servers in **dry-run** mode, and skips cleanly when
the credentials or target conditions it needs are absent.  This makes the
suite safe to wire into CI on pull requests with read-only tokens without
ever mutating a repository.

Configuration is entirely environment-driven so CI and local runs can
point the tests at different targets without code changes:

GitHub:
    GITHUB_TOKEN            Required.  A read-only token is sufficient;
                            the dry-run paths never need write scopes.
    DEPENDAMERGE_IT_ORG     Organization login to exercise the
                            "organization account" path against.
                            Default: ``lfreleng-actions``.
    DEPENDAMERGE_IT_USER    User login to exercise the "personal
                            account" path against (the case that
                            regressed in v0.7.0).
                            Default: ``ModeSevenIndustrialSolutions``.

Gerrit (all optional; the Gerrit tests skip unless host + creds exist):
    DEPENDAMERGE_IT_GERRIT_HOST   Gerrit hostname (e.g. ``gerrit.onap.org``).
    DEPENDAMERGE_IT_GERRIT_BASE_PATH   Optional URL base path when the
                            server is not mounted at the root
                            (e.g. ``r`` for ``https://gerrit.onap.org/r/``).
    GERRIT_USERNAME / GERRIT_PASSWORD   HTTP credentials.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

# Default targets.  ``ModeSevenIndustrialSolutions`` is intentionally a
# *personal* account (not an organization): it is the exact login whose
# owner-wide report commands regressed in v0.7.0, so keeping it as the
# default user target guards against that specific class of bug.
DEFAULT_ORG = "lfreleng-actions"
DEFAULT_USER = "ModeSevenIndustrialSolutions"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@pytest.fixture(scope="session")
def github_token() -> str:
    """Return a GitHub token or skip the whole GitHub integration suite."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        pytest.skip("GITHUB_TOKEN not set; skipping live GitHub integration test")
    return token


@pytest.fixture(scope="session")
def integration_org() -> str:
    """Login of the organization account to exercise."""
    return _env("DEPENDAMERGE_IT_ORG", DEFAULT_ORG)


@pytest.fixture(scope="session")
def integration_user() -> str:
    """Login of the personal (non-organization) account to exercise."""
    return _env("DEPENDAMERGE_IT_USER", DEFAULT_USER)


@pytest.fixture
def runner() -> CliRunner:
    """A fresh Typer ``CliRunner`` per test.

    Click 8.2 removed the ``mix_stderr`` option, so ``result.output``
    holds only stdout and stderr is captured separately on
    ``result.stderr``.  The CLI's report and dry-run output is written
    through a Rich console to stdout, but tests should still consult both
    streams (see :func:`combined_output`) so an unexpected message routed
    to stderr is never silently missed.
    """
    return CliRunner()


def combined_output(result) -> str:
    """Return a result's stdout and stderr joined into one string.

    Guards assertions against Click 8.2+ splitting the streams: relevant
    output normally lands on stdout, but combining both makes the checks
    robust regardless of which stream a given line uses.
    """
    parts = [result.output or ""]
    try:
        stderr = result.stderr
    except (ValueError, AttributeError):
        # stderr may be unavailable (e.g. not separately captured).
        stderr = ""
    if stderr:
        parts.append(stderr)
    return "".join(parts)


def discover_automation_pr(owner: str, token: str) -> str | None:
    """Find one open automation PR under ``owner``, or ``None``.

    Automation PRs (dependabot, pre-commit.ci, ...) come and go in the
    target spaces, so the integration tests must *discover* a live one
    rather than hard-code a URL that will rot.  This reuses the very same
    owner-wide enumeration the ``merge`` command relies on, so it also
    transitively exercises the owner-resolution fix for both org and user
    accounts.

    Args:
        owner: Organization or user login to scan.
        token: GitHub token.

    Returns:
        The ``html_url`` of an open automation PR, or ``None`` if the
        owner currently has no open automation PRs (a normal, transient
        condition the caller should treat as "skip, do not fail").
    """
    from dependamerge.github_service import GitHubService

    async def _find() -> str | None:
        service = GitHubService(token=token)
        try:
            prs, _errors = await service.fetch_owner_open_prs(
                owner, only_automation=True
            )
        finally:
            await service.close()
        for pr in prs:
            if pr.html_url:
                return pr.html_url
        return None

    return asyncio.run(_find())


@pytest.fixture(scope="session")
def automation_pr_url(github_token: str, integration_user: str, integration_org: str):
    """Discover a live open automation PR URL, or skip the test.

    Searches the personal account first (its automation PRs are the
    motivating case for this work) and falls back to the organization.
    Skips with a clear message when neither space currently has an open
    automation PR so the suite never fails on an empty target.
    """
    for owner in (integration_user, integration_org):
        url = discover_automation_pr(owner, github_token)
        if url:
            return url
    pytest.skip(
        "No open automation PRs found in "
        f"'{integration_user}' or '{integration_org}'; "
        "skipping dynamic PR integration test"
    )


def gerrit_config() -> dict[str, str] | None:
    """Return Gerrit connection settings from the environment, or ``None``.

    Requires a host plus HTTP credentials; returns ``None`` (caller
    should skip) when any piece is missing so the Gerrit integration
    tests stay dormant in environments without Gerrit access.
    """
    host = os.environ.get("DEPENDAMERGE_IT_GERRIT_HOST", "").strip()
    base_path = os.environ.get("DEPENDAMERGE_IT_GERRIT_BASE_PATH", "").strip()
    username = (
        os.environ.get("GERRIT_USERNAME", "").strip()
        or os.environ.get("GERRIT_HTTP_USER", "").strip()
    )
    password = (
        os.environ.get("GERRIT_PASSWORD", "").strip()
        or os.environ.get("GERRIT_HTTP_PASSWORD", "").strip()
    )
    if not (host and username and password):
        return None
    return {
        "host": host,
        "base_path": base_path,
        "username": username,
        "password": password,
    }


@pytest.fixture
def gerrit_settings() -> Iterator[dict[str, str]]:
    """Yield Gerrit settings or skip when Gerrit access is not configured."""
    config = gerrit_config()
    if config is None:
        pytest.skip(
            "Gerrit not configured (need DEPENDAMERGE_IT_GERRIT_HOST + "
            "GERRIT_USERNAME/GERRIT_PASSWORD); skipping Gerrit integration test"
        )
    # ``pytest.skip`` raises, so ``config`` is non-None here; assert it so
    # the type checker narrows the Optional before the yield.
    assert config is not None
    yield config
