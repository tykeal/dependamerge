# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Shared helpers for reconciling GitHub bot/App actor identities.

GitHub reports the *same* App actor under two different login forms
depending on the API surface:

- The REST API returns the suffixed form, e.g. ``dependabot[bot]``.
- The GraphQL API returns the bare form for a ``Bot`` actor, e.g.
  ``dependabot`` (the ``[bot]`` suffix is dropped).

This split has historically been worked around ad-hoc in several
places (the PR comparator, the Gerrit comparator, repo-merge automation
detection).  Centralising it here gives every consumer one canonical
form and one set of predicates, so a downstream equality check can never
again silently misclassify a bot just because it arrived via GraphQL.

Two layers are provided:

- :func:`canonical_bot_login` normalises an actor login *to the REST
  form* at the data boundary (where a GraphQL node is converted to an
  internal model).  It is driven by the GraphQL ``__typename`` so it
  applies uniformly to *every* App actor (dependabot, renovate,
  pre-commit-ci, github-actions, copilot, …), not just one identity.
- :func:`normalize_bot_login` and :func:`is_dependabot` compare logins
  irrespective of the ``[bot]`` suffix, so a stray non-canonical value
  cannot disable identity-specific handling.
"""

from __future__ import annotations

BOT_SUFFIX = "[bot]"

# GraphQL ``__typename`` value for an App/bot actor.
_BOT_TYPENAME = "Bot"

# Known automation-tool base logins (``[bot]``-stripped, lower-cased).
# Membership is checked against the *normalised* login so both the REST
# (``dependabot[bot]``) and GraphQL (``dependabot``) forms match.  Any
# other App actor is still caught by the ``[bot]`` fallthrough in
# :func:`is_automation_author`, so this set exists to recognise the
# *bare* (GraphQL) forms of known tools, not to be exhaustive.
_AUTOMATION_BOT_NAMES = frozenset(
    {
        "dependabot",
        "renovate",
        "pre-commit-ci",
        "pre-commit",
        "github-actions",
        "allcontributors",
        "copilot",
        "github-copilot",
    }
)

# Known GitHub Copilot actor logins (normalised).  Copilot reports under
# several identities depending on the surface: the App author
# (``Copilot`` / ``copilot[bot]`` / ``github-copilot[bot]``) and the
# review actor (``copilot-pull-request-reviewer``).
_COPILOT_NAMES = frozenset(
    {
        "copilot",
        "github-copilot",
        "copilot-pull-request-reviewer",
    }
)


def canonical_bot_login(login: str | None, typename: str | None = None) -> str:
    """Return ``login`` in the canonical REST form.

    When ``typename`` identifies a GraphQL ``Bot`` actor whose login lacks
    the ``[bot]`` suffix (the bare form GraphQL returns), append it so the
    value matches the REST API form used throughout the codebase.  Every
    other input is returned unchanged.

    Args:
        login: The actor login as returned by the API (may be ``None``).
        typename: The GraphQL ``__typename`` for the actor, when known.
            Only ``"Bot"`` triggers suffixing; this keeps the
            normalisation actor-type driven rather than name-matching a
            specific bot.

    Returns:
        The canonical login, or ``"unknown"`` when ``login`` is empty.
    """
    if not login:
        return "unknown"
    if typename == _BOT_TYPENAME and not login.endswith(BOT_SUFFIX):
        return f"{login}{BOT_SUFFIX}"
    return login


def normalize_bot_login(login: str | None) -> str:
    """Return a lower-cased login with any ``[bot]`` suffix removed.

    Use this for comparisons that must treat ``dependabot`` and
    ``dependabot[bot]`` (and any other suffixed/bare bot pair) as equal,
    regardless of which API surface produced the value.
    """
    if not login:
        return ""
    normalized = login.lower()
    if normalized.endswith(BOT_SUFFIX):
        normalized = normalized[: -len(BOT_SUFFIX)]
    return normalized


def is_dependabot(author: str | None) -> bool:
    """Return True when ``author`` is dependabot, in either login form.

    Robust to the REST (``dependabot[bot]``) and GraphQL (``dependabot``)
    forms so dependabot-specific recovery (rebase / recreate macros) is
    never skipped because of a non-canonical login.
    """
    return normalize_bot_login(author) == "dependabot"


def is_copilot(author: str | None) -> bool:
    """Return True when ``author`` is a GitHub Copilot actor.

    Matches every Copilot identity (App author and review actor) in both
    the REST and GraphQL login forms, so Copilot-specific handling (review
    dismissal, block-reason analysis) cannot be bypassed by a
    non-canonical login.
    """
    return normalize_bot_login(author) in _COPILOT_NAMES


def is_automation_author(author: str | None) -> bool:
    """Return True when ``author`` is an automation/bot actor.

    Recognises the known automation tools by their normalised base login
    (so both ``dependabot`` and ``dependabot[bot]`` match) and, as a
    fallthrough, treats *any* actor whose login carries the ``[bot]``
    marker as automation.  The fallthrough preserves the historical
    "anything ending in ``[bot]``" breadth so an unknown future bot is
    still classified as automation.
    """
    if not author:
        return False
    if normalize_bot_login(author) in _AUTOMATION_BOT_NAMES:
        return True
    return author.lower().endswith(BOT_SUFFIX)
