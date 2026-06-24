# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the shared bot-identity helpers."""

from __future__ import annotations

import pytest

from dependamerge.bot_identity import (
    canonical_bot_login,
    is_automation_author,
    is_copilot,
    is_dependabot,
    normalize_bot_login,
)


class TestCanonicalBotLogin:
    """``canonical_bot_login`` normalises GraphQL ``Bot`` logins to REST form."""

    def test_bot_typename_appends_suffix(self) -> None:
        assert canonical_bot_login("dependabot", "Bot") == "dependabot[bot]"

    def test_bot_typename_applies_to_any_app_actor(self) -> None:
        # Driven by __typename, not a dependabot-specific match: every bot
        # is canonicalised uniformly.
        assert canonical_bot_login("renovate", "Bot") == "renovate[bot]"
        assert canonical_bot_login("pre-commit-ci", "Bot") == "pre-commit-ci[bot]"
        assert canonical_bot_login("github-actions", "Bot") == "github-actions[bot]"

    def test_already_suffixed_is_idempotent(self) -> None:
        assert canonical_bot_login("dependabot[bot]", "Bot") == "dependabot[bot]"

    def test_user_typename_unchanged(self) -> None:
        # A human login that happens to resemble a bot is left untouched.
        assert canonical_bot_login("octocat", "User") == "octocat"

    def test_missing_typename_leaves_login_unchanged(self) -> None:
        # Without __typename (e.g. an older query) the bare login is
        # preserved; predicates below still match it.
        assert canonical_bot_login("dependabot", None) == "dependabot"

    def test_empty_login_returns_unknown(self) -> None:
        assert canonical_bot_login(None, "Bot") == "unknown"
        assert canonical_bot_login("", "Bot") == "unknown"


class TestNormalizeBotLogin:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("dependabot[bot]", "dependabot"),
            ("dependabot", "dependabot"),
            ("Dependabot[bot]", "dependabot"),
            ("RENOVATE[BOT]", "renovate"),
            (None, ""),
            ("", ""),
        ],
    )
    def test_normalize(self, value: str | None, expected: str) -> None:
        assert normalize_bot_login(value) == expected


class TestIsDependabot:
    def test_matches_both_forms(self) -> None:
        assert is_dependabot("dependabot[bot]")
        assert is_dependabot("dependabot")
        assert is_dependabot("Dependabot[bot]")

    def test_rejects_other_actors(self) -> None:
        assert not is_dependabot("renovate[bot]")
        assert not is_dependabot("octocat")
        assert not is_dependabot(None)
        assert not is_dependabot("")


class TestIsCopilot:
    @pytest.mark.parametrize(
        "login",
        [
            "Copilot",
            "copilot",
            "copilot[bot]",
            "github-copilot",
            "github-copilot[bot]",
            "copilot-pull-request-reviewer",
        ],
    )
    def test_matches_every_copilot_identity(self, login: str) -> None:
        assert is_copilot(login)

    def test_rejects_other_actors(self) -> None:
        assert not is_copilot("dependabot[bot]")
        assert not is_copilot("octocat")
        assert not is_copilot(None)
        assert not is_copilot("")


class TestIsAutomationAuthor:
    @pytest.mark.parametrize(
        "author",
        [
            # Known tools, REST (suffixed) form.
            "dependabot[bot]",
            "renovate[bot]",
            "pre-commit-ci[bot]",
            "github-actions[bot]",
            "allcontributors[bot]",
            # Known tools, GraphQL (bare) form.
            "dependabot",
            "renovate",
            "github-actions",
            # Copilot is automation too.
            "github-copilot[bot]",
            # Unknown bot caught by the [bot] fallthrough.
            "some-future-bot[bot]",
        ],
    )
    def test_classifies_automation(self, author: str) -> None:
        assert is_automation_author(author)

    @pytest.mark.parametrize(
        "author",
        ["john-doe", "jane-smith", "some-user", None, ""],
    )
    def test_rejects_humans(self, author: str | None) -> None:
        assert not is_automation_author(author)
