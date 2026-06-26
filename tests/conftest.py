# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Shared pytest fixtures for dependamerge tests.

Typed Mock Client Pattern
=========================

Problem
-------
``AsyncMergeManager`` (and ``AsyncCloseManager``) declare their internal HTTP
client as an optional type::

    self._github_client: GitHubAsync | None = None

The client is only populated inside ``__aenter__()`` (the async context
manager).  In tests we routinely bypass the context manager and inject an
``AsyncMock`` directly::

    mgr = AsyncMergeManager(token="t")
    mgr._github_client = AsyncMock()
    mgr._github_client.get = AsyncMock(return_value=...)  # ← warning!

Because the *declared* type is ``GitHubAsync | None``, basedpyright cannot
prove the value is non-``None`` after assignment and flags every subsequent
attribute access as ``reportOptionalMemberAccess``.

Solution
--------
The ``make_merge_manager`` helper (and any similar helpers in individual test
modules) returns a **tuple** ``(manager, client)`` where ``client`` is typed
as ``AsyncMock`` — a concrete, non-optional reference to the same object
stored in ``manager._github_client``.  All subsequent mock configuration
should go through the ``client`` variable::

    mgr, client = make_merge_manager(token="t")
    client.get = AsyncMock(return_value=...)          # ✓ no warning
    client.post_issue_comment = AsyncMock()            # ✓ no warning
    client.post_issue_comment.assert_called_once()     # ✓ no warning

This eliminates basedpyright ``reportOptionalMemberAccess`` warnings without
changing any production code or adding ``assert ... is not None`` boilerplate
to every test.

Guidelines for New Tests
------------------------
1. **Always** use the ``make_merge_manager`` helper (or a module-local
   wrapper around it) when you need an ``AsyncMergeManager`` with a mocked
   GitHub client outside of ``async with``.

2. Hold on to the returned ``client`` variable and use it — *not*
   ``mgr._github_client`` — for all mock setup and assertions.

3. If a test intentionally sets ``_github_client = None`` to exercise the
   "no client" code path, do that *after* unpacking the tuple::

       mgr, _client = make_merge_manager()
       mgr._github_client = None   # intentional for this test

4. If you use ``async with AsyncMergeManager(...) as mgr:`` (which calls
   ``__aenter__`` and sets the real client), you can safely replace the
   client inside the block because basedpyright already narrowed the type.
   You do **not** need this helper in that case.

See Also
--------
- ``tests/test_dependabot_recreate.py`` — ``_make_manager()`` wraps this
  helper with module-specific defaults.
- ``tests/test_precommit_ci_trigger.py`` — same pattern.
- ``tests/test_github2gerrit_detector.py`` — same pattern for
  ``_make_mgr_with_no_gitreview``.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock

import pytest

from dependamerge.merge_manager import AsyncMergeManager

_RUN_INTEGRATION_ENV = "DEPENDAMERGE_RUN_INTEGRATION"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--run-integration`` opt-in flag.

    Live integration tests (see ``tests/integration``) hit real GitHub /
    Gerrit servers.  They already self-skip when credentials are absent,
    but they must not run as part of the ordinary unit-test suite even
    when a token happens to be present in the environment, because they
    are slow and network-dependent.  They run only when explicitly
    requested via ``--run-integration`` or the
    ``DEPENDAMERGE_RUN_INTEGRATION`` environment variable.
    """
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run live GitHub/Gerrit integration tests (marked 'integration').",
    )


def _integration_enabled(config: pytest.Config) -> bool:
    if config.getoption("--run-integration"):
        return True
    return os.environ.get(_RUN_INTEGRATION_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip ``integration`` tests unless explicitly opted in."""
    if _integration_enabled(config):
        return
    skip_integration = pytest.mark.skip(
        reason="integration tests disabled (pass --run-integration or set "
        f"{_RUN_INTEGRATION_ENV}=1)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


def make_merge_manager(**overrides: Any) -> tuple[AsyncMergeManager, AsyncMock]:
    """Build an ``AsyncMergeManager`` with a pre-injected ``AsyncMock`` client.

    Returns a ``(manager, client)`` tuple.  The ``client`` reference is typed
    as ``AsyncMock`` (never ``None``), so attribute access on it will not
    trigger basedpyright ``reportOptionalMemberAccess`` warnings.

    All keyword arguments are forwarded to ``AsyncMergeManager.__init__``.
    A ``token`` default of ``"test-token"`` is provided if not supplied.

    Usage::

        mgr, client = make_merge_manager(preview_mode=True)
        client.get = AsyncMock(return_value={...})
        result = await mgr._some_method(pr)
        client.get.assert_called_once()

    Parameters
    ----------
    **overrides:
        Keyword arguments forwarded to ``AsyncMergeManager()``.

    Returns
    -------
    tuple[AsyncMergeManager, AsyncMock]
        The manager instance and its mock GitHub client.
    """
    defaults: dict[str, Any] = {"token": "test-token"}
    defaults.update(overrides)
    mgr = AsyncMergeManager(**defaults)
    client = AsyncMock()
    mgr._github_client = client
    return mgr, client
