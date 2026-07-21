# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""Tests for GIT_ASKPASS-based credential passing in git_ops.

Covers:
- git_askpass_env() — helper script creation, env overrides, cleanup
- run_git(token=...) — token supplied via environment, never argv
- clone(token=...) — credential-free URL with askpass auth
- redact_text() — public redaction helper
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from dependamerge import git_ops
from dependamerge.git_ops import (
    clone,
    git_askpass_env,
    redact_text,
    run_git,
)

TOKEN = "ghp_" + "a" * 36


class TestGitAskpassEnv:
    """The askpass helper must keep secrets out of argv and off disk."""

    def test_yields_expected_env_overrides(self) -> None:
        with git_askpass_env(TOKEN) as env:
            assert env["GIT_ASKPASS"]
            assert env["DM_GIT_ASKPASS_TOKEN"] == TOKEN
            assert env["DM_GIT_ASKPASS_USERNAME"] == "x-access-token"
            assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_custom_username(self) -> None:
        with git_askpass_env(TOKEN, username="oauth2") as env:
            assert env["DM_GIT_ASKPASS_USERNAME"] == "oauth2"

    def test_script_contains_no_secret(self) -> None:
        with git_askpass_env(TOKEN) as env:
            content = Path(env["GIT_ASKPASS"]).read_text(encoding="utf-8")
            assert TOKEN not in content

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX file-mode bits: chmod/S_IMODE do not reflect Windows ACLs",
    )
    def test_script_is_owner_only_executable(self) -> None:
        with git_askpass_env(TOKEN) as env:
            mode = stat.S_IMODE(os.stat(env["GIT_ASKPASS"]).st_mode)
            assert mode == 0o700

    def test_script_removed_on_exit(self) -> None:
        with git_askpass_env(TOKEN) as env:
            script = Path(env["GIT_ASKPASS"])
            assert script.exists()
        assert not script.exists()
        assert not script.parent.exists()

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX shebang execution: subprocess does not honor #!/bin/sh on Windows",
    )
    def test_script_answers_username_and_password_prompts(self) -> None:
        """Functional check: run the helper the way git would."""
        with git_askpass_env(TOKEN) as env:
            run_env = {**os.environ, **env}
            username = subprocess.run(
                [env["GIT_ASKPASS"], "Username for 'https://github.com':"],
                capture_output=True,
                text=True,
                env=run_env,
                check=True,
            ).stdout.strip()
            password = subprocess.run(
                [env["GIT_ASKPASS"], "Password for 'https://github.com':"],
                capture_output=True,
                text=True,
                env=run_env,
                check=True,
            ).stdout.strip()
        assert username == "x-access-token"
        assert password == TOKEN


class TestRunGitWithToken:
    """run_git(token=...) must never place the token in argv."""

    def test_token_absent_from_argv_and_present_in_env(self) -> None:
        captured_args: list[str] = []
        captured_env: dict[str, str] = {}

        def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
            captured_args.extend(str(a) for a in args)
            captured_env.update(kwargs.get("env") or {})

            class _CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return _CP()

        with patch.object(git_ops.subprocess, "run", side_effect=fake_run):
            run_git(["git", "version"], token=TOKEN)

        assert all(TOKEN not in a for a in captured_args)
        assert captured_env["DM_GIT_ASKPASS_TOKEN"] == TOKEN
        assert captured_env["GIT_ASKPASS"]
        assert captured_env["GIT_TERMINAL_PROMPT"] == "0"

    def test_no_token_leaves_env_untouched(self) -> None:
        captured_env: dict[str, str] = {}

        def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
            captured_env.update(kwargs.get("env") or {})

            class _CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return _CP()

        with patch.object(git_ops.subprocess, "run", side_effect=fake_run):
            run_git(["git", "version"])

        assert "DM_GIT_ASKPASS_TOKEN" not in captured_env

    def test_stale_askpass_token_scrubbed_when_no_token(self) -> None:
        """A stale token in the parent env must not reach git subprocesses."""
        captured_env: dict[str, str] = {}

        def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
            captured_env.update(kwargs.get("env") or {})

            class _CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return _CP()

        stale_env = {
            "DM_GIT_ASKPASS_TOKEN": TOKEN,
            "DM_GIT_ASKPASS_USERNAME": "leaked-user",
        }
        with (
            patch.dict(os.environ, stale_env, clear=False),
            patch.object(git_ops.subprocess, "run", side_effect=fake_run),
        ):
            run_git(["git", "version"])

        assert "DM_GIT_ASKPASS_TOKEN" not in captured_env
        assert "DM_GIT_ASKPASS_USERNAME" not in captured_env

    def test_token_askpass_wins_over_env_overrides(self) -> None:
        captured_env: dict[str, str] = {}

        def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
            captured_env.update(kwargs.get("env") or {})

            class _CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return _CP()

        with patch.object(git_ops.subprocess, "run", side_effect=fake_run):
            run_git(
                ["git", "version"],
                token=TOKEN,
                env_overrides={"GIT_ASKPASS": "/stale/askpass"},
            )

        assert captured_env["GIT_ASKPASS"] != "/stale/askpass"


class TestCloneWithToken:
    """clone(token=...) keeps the URL free of credentials."""

    def test_clone_forwards_token_and_clean_url(self) -> None:
        with patch.object(git_ops, "run_git") as mock_run_git:
            clone(
                "https://github.com/owner/repo.git",
                "/tmp/dest",
                token=TOKEN,
            )
        args, kwargs = mock_run_git.call_args
        cmd = args[0]
        assert "https://github.com/owner/repo.git" in cmd
        assert all(TOKEN not in str(part) for part in cmd)
        assert kwargs["token"] == TOKEN


class TestRedactText:
    """Public redaction helper masks known secret shapes."""

    def test_redacts_x_access_token_url(self) -> None:
        url = f"https://x-access-token:{TOKEN}@github.com/o/r.git"
        redacted = redact_text(url)
        assert TOKEN not in redacted
        assert "x-access-token:***@" in redacted

    def test_redacts_basic_auth_url(self) -> None:
        redacted = redact_text("https://user:hunter2@example.com/repo.git")
        assert "hunter2" not in redacted

    def test_plain_text_unchanged(self) -> None:
        assert redact_text("https://github.com/o/r.git") == (
            "https://github.com/o/r.git"
        )
