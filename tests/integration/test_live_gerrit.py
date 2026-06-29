# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Live Gerrit integration test for the ``dependamerge`` CLI.

Mirrors the GitHub dry-run integration test for the Gerrit code path: it
dynamically discovers an open change on a live Gerrit server and runs
``merge --dry-run`` against it, asserting that nothing was reviewed or
submitted.

The test fails safe.  It skips (never fails) when Gerrit is not configured
(see :func:`tests.integration.conftest.gerrit_config`) or when the server
currently has no open changes, so it is safe in CI and on contributor
machines without Gerrit access.
"""

from __future__ import annotations

import pytest

from dependamerge.cli import app

from .conftest import combined_output

pytestmark = pytest.mark.integration


def _discover_open_change_url(settings: dict[str, str]) -> str | None:
    """Return the web URL of one open change, or ``None`` if none exist."""
    from dependamerge.gerrit.service import create_gerrit_service

    service = create_gerrit_service(
        host=settings["host"],
        base_path=settings.get("base_path") or None,
        username=settings["username"],
        password=settings["password"],
    )
    # A small limit keeps the probe cheap; we only need one usable change.
    changes = service.get_all_open_changes(limit=25)
    for change in changes:
        if change.url:
            return change.url
    return None


class TestGerritDryRunLive:
    def test_merge_dry_run_previews_without_submitting(self, runner, gerrit_settings):
        """`merge --dry-run` previews a real Gerrit change without submitting.

        Discovers a live open change and confirms the dry-run path reports
        a preview and never reviews or submits anything.  Skips when the
        server has no open changes so the suite never fails on an empty
        target.
        """
        change_url = _discover_open_change_url(gerrit_settings)
        if not change_url:
            pytest.skip(
                f"No open changes found on '{gerrit_settings['host']}'; "
                "skipping Gerrit dry-run integration test"
            )

        result = runner.invoke(
            app,
            ["merge", change_url, "--dry-run", "--no-progress"],
            env={
                "GERRIT_USERNAME": gerrit_settings["username"],
                "GERRIT_PASSWORD": gerrit_settings["password"],
            },
        )
        assert result.exit_code == 0, combined_output(result)
        assert "Dry run: no changes were reviewed or submitted" in combined_output(
            result
        )
