# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Live GitHub integration tests for the ``dependamerge`` CLI.

These tests run the real CLI (in-process, via Typer's ``CliRunner``)
against live GitHub, exercising the read-only report commands and the
dry-run write commands.  They never mutate anything: the report commands
are read-only by nature and the ``merge`` / ``close`` invocations all use
``--dry-run``, which previews without approving, merging, rebasing or
closing and skips the write-permission pre-flight so a read-only token
suffices.

The suite is the regression guard for the v0.7.0 defect where the
owner-wide report commands assumed an *organization* and failed against a
*personal* account with::

    Could not resolve to an Organization with the login of '<user>'

so every report-command assertion explicitly checks that this error did
not resurface.

All tests fail safe: they skip (never fail) when ``GITHUB_TOKEN`` is
absent or when the target space currently has no open automation PRs.
"""

from __future__ import annotations

import pytest

from dependamerge.cli import app

from .conftest import combined_output

pytestmark = pytest.mark.integration

# The exact GitHub GraphQL error fragment emitted by the v0.7.0 bug when a
# personal account was treated as an organization.  Any report command
# that resolves the owner correctly must never surface this.
_ORG_RESOLUTION_ERROR = "Could not resolve to an Organization"


# Owner-argument forms the CLI must accept, as ``str.format`` templates
# with an ``{owner}`` placeholder.  Defined at module scope so the
# parametrization below iterates the templates directly — there is no
# hand-maintained index range that can drift out of sync with the list
# and silently drop a form (the exact ``/orgs/<owner>/repositories`` form
# that motivated the parsing fix).  Covers the bare login, a bare login
# with a trailing slash, the bare and trailing-slash URLs, the
# scheme-less host, and the canonical ``/orgs`` forms.
OWNER_URL_TEMPLATES = [
    "{owner}",
    "{owner}/",
    "https://github.com/{owner}",
    "https://github.com/{owner}/",
    "github.com/{owner}",
    "https://github.com/orgs/{owner}",
    "https://github.com/orgs/{owner}/repositories",
]


def _assert_owner_resolved(result, owner: str) -> None:
    """Assert a report command resolved ``owner`` without the org bug."""
    output = combined_output(result)
    assert _ORG_RESOLUTION_ERROR not in output, (
        f"owner '{owner}' was mis-resolved as an organization:\n{output}"
    )
    assert result.exit_code == 0, (
        f"status/blocked exited {result.exit_code} for owner '{owner}':\n{output}"
    )


class TestStatusCommandLive:
    """`status` must work for both org and user accounts, every URL form."""

    @pytest.mark.parametrize("url_template", OWNER_URL_TEMPLATES)
    def test_status_user_account_all_url_forms(
        self, runner, github_token, integration_user, url_template
    ):
        """`status` resolves a *personal* account across every URL form.

        This is the direct regression for the reported v0.7.0 failure.
        The personal account is intentionally the bounded target so all
        URL forms can be exercised cheaply.
        """
        url = url_template.format(owner=integration_user)
        result = runner.invoke(
            app,
            ["status", url, "--no-progress", "--token", github_token],
        )
        _assert_owner_resolved(result, integration_user)

    def test_status_organization_account(self, runner, github_token, integration_org):
        """`status` resolves a genuine *organization* account.

        Run once against the canonical bare login: the org path is the
        slower target, and URL-form parsing is target-independent (already
        covered exhaustively against the user account above).
        """
        result = runner.invoke(
            app,
            ["status", integration_org, "--no-progress", "--token", github_token],
        )
        _assert_owner_resolved(result, integration_org)


class TestBlockedCommandLive:
    """`blocked` shares the owner-resolution path; verify both account types."""

    def test_blocked_user_account(self, runner, github_token, integration_user):
        result = runner.invoke(
            app,
            ["blocked", integration_user, "--no-progress", "--token", github_token],
        )
        _assert_owner_resolved(result, integration_user)

    def test_blocked_organization_account(self, runner, github_token, integration_org):
        result = runner.invoke(
            app,
            ["blocked", integration_org, "--no-progress", "--token", github_token],
        )
        _assert_owner_resolved(result, integration_org)


class TestDryRunWriteCommandsLive:
    """`merge`/`close` dry-run against a dynamically discovered live PR."""

    def test_merge_dry_run_previews_without_merging(
        self, runner, github_token, automation_pr_url
    ):
        """`merge --dry-run` previews a real PR under a read-only token.

        Asserts the permission pre-flight was skipped (the change that
        unblocked read-only testing) and that the command completed
        successfully without attempting a merge.
        """
        result = runner.invoke(
            app,
            [
                "merge",
                automation_pr_url,
                "--dry-run",
                "--no-progress",
                "--token",
                github_token,
            ],
        )
        assert result.exit_code == 0, combined_output(result)
        assert "Dry run: skipping token permission check" in combined_output(result)

    def test_close_dry_run_previews_without_closing(
        self, runner, github_token, automation_pr_url
    ):
        """`close --dry-run` previews matching PRs without closing them."""
        result = runner.invoke(
            app,
            [
                "close",
                automation_pr_url,
                "--dry-run",
                "--no-progress",
                "--token",
                github_token,
            ],
        )
        assert result.exit_code == 0, combined_output(result)
        assert "Dry run: would close" in combined_output(result)
