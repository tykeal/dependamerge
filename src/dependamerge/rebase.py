# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Rebase strategies for behind pull requests.

This module centralises every code path dependamerge uses to bring
a PR up to date with its base branch before merging:

- :func:`should_use_local_rebase` decides between the local-git
  workflow (preserves verified commit signatures via the user's
  ``~/.gitconfig``) and the GitHub REST ``update-branch``
  endpoint (fast but produces unsigned commits).
- :func:`local_rebase_pr` runs the local clone + rebase +
  force-push-with-lease against a secure temp workspace.
- :func:`rest_rebase_and_poll` runs the REST ``update-branch``
  call followed by the post-rebase polling loop that waits for
  GitHub to recompute mergeability.
- :func:`perform_step5_rebase` is the top-level dispatcher used by
  :class:`AsyncMergeManager._merge_single_pr` Step 5.
- :func:`authed_clone_url` injects a token into an HTTPS clone URL
  for non-interactive ``git clone`` auth.

The dispatcher takes a :class:`RebaseContext` rather than a full
``AsyncMergeManager`` reference, which keeps the rebase logic
testable in isolation (no need to construct a manager + GitHub
client + progress tracker just to exercise a decision tree).

The local-rebase path is the headline reason this module exists:
``PUT /repos/{owner}/{repo}/pulls/{n}/update-branch`` creates a
server-side merge commit whose committer is the calling token's
GitHub user, which is *not* signed with the user's local SSH/GPG
key.  On repos whose branch protection requires verified
signatures, the resulting commit loses its ``Verified`` badge and
becomes un-mergeable.  ``pre-commit-ci[bot]`` PRs are particularly
affected because that bot has no comment macro for recreating a PR
with a re-signed commit (https://github.com/pre-commit-ci/issues/issues/41).
The local path solves this by shelling out to ``git`` so the
user's signing config is honoured.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console

from . import git_ops
from .git_ops import (
    GitError,
    add_remote,
    checkout,
    clone,
    ensure_git_available,
    fetch,
    push_force_with_lease,
    rebase,
    rebase_abort,
    secure_rmtree,
)
from .models import PullRequestInfo
from .output_utils import log_and_print

if TYPE_CHECKING:
    from .github_async import GitHubAsync


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class RebaseContext:
    """Bundle of dependencies the rebase orchestrator needs.

    Passed in lieu of a full ``AsyncMergeManager`` reference so the
    rebase logic stays decoupled from manager internals and can be
    tested without standing up the whole merge pipeline.
    """

    github_client: GitHubAsync | None
    token: str
    rebase_local: bool
    preview_mode: bool
    merge_recheck_interval: float
    merge_poll_max_attempts: int
    log: logging.Logger
    console: Console
    # Mutable set on the manager that records PR keys (``owner/repo#N``)
    # which have already been through Step 5.  Step 5.5 consults this
    # to avoid doubling the configured ``merge_timeout``.  We keep the
    # raw set reference rather than a callback so the existing
    # invariant (Step 5 always adds, Step 5.5 always reads) stays
    # obvious at the call site.
    rebased_prs: set[str]
    # Async callable equivalent to ``manager._enable_auto_merge_for_pr``.
    # Passed in to avoid a circular import.
    enable_auto_merge: Callable[[PullRequestInfo, str, str], Awaitable[bool]]


@dataclass
class Step5Outcome:
    """Result of :func:`perform_step5_rebase`.

    ``failed`` indicates the caller should mark the PR as ``FAILED``
    and bail out of the merge attempt.  ``error_message`` is the
    user-visible reason in that case.
    """

    failed: bool = False
    error_message: str | None = None


def authed_clone_url(clone_url: str, token: str) -> str:
    """Return an HTTPS clone URL with the token injected for auth.

    The token *is* passed to ``git`` as part of the URL command-
    line argument, which means it can be visible to process-
    listing tools (``ps``, ``/proc/<pid>/cmdline``) on the local
    machine for the duration of the git invocation. Log output is
    separately redacted by :func:`git_ops._redact`, but no
    equivalent protection exists for ``ps``-style introspection.
    Callers needing stronger guarantees should use a different
    auth mechanism (e.g. SSH keys or a credential helper) and
    pass an unmodified ``ssh://`` / ``git@`` URL through
    unchanged.

    Non-HTTPS URLs (SSH, ``git://``) are returned unchanged.
    """
    if clone_url.startswith("https://"):
        return clone_url.replace("https://", f"https://x-access-token:{token}@")
    return clone_url


async def should_use_local_rebase(
    *,
    github_client: GitHubAsync | None,
    pr_info: PullRequestInfo,
    owner: str,
    repo: str,
    base_branch: str,
    rebase_local: bool,
    log: logging.Logger,
) -> tuple[bool, str]:
    """Decide whether Step 5 should rebase locally instead of via REST.

    Returns ``(use_local, reason)``.  ``reason`` is a short
    human-readable string suitable for debug logging or a
    user-visible note when ``use_local`` is True.

    The gate activates when ``rebase_local`` is True AND either:

    - the PR is from ``pre-commit-ci[bot]`` (always — that bot has
      no comment macro for recreating a PR with a re-signed
      commit), OR
    - the base branch requires verified signatures AND the current
      PR head commit is itself verified (so REST update-branch
      *would* break verification).

    Strict ``is True`` comparison is used on the
    ``requires_commit_signatures`` return so ``AsyncMock`` defaults
    don't accidentally route real PRs into the local-rebase path
    in tests that haven't explicitly set ``return_value``.  If the
    requirement check raises, we fail safely to the REST path.
    """
    if not rebase_local:
        return False, "--no-rebase-local set"

    # Always use local rebase for pre-commit.ci PRs. The bot has
    # no comment macro to recover from a verification break, so
    # we treat it as opt-in regardless of branch protection.
    if pr_info.author == "pre-commit-ci[bot]":
        return True, "pre-commit-ci[bot] has no recreate/rebase macro"

    if github_client is None:
        return False, "no GitHub client"

    # Branch-protection signature requirement (classic + rulesets)
    try:
        requires_signatures = await github_client.requires_commit_signatures(
            owner, repo, base_branch
        )
    except Exception as exc:
        log.debug(
            "Could not determine signature requirement for %s/%s:%s: %s",
            owner,
            repo,
            base_branch,
            exc,
        )
        return False, "signature requirement check failed"

    # Strict ``is True`` rather than truthy check: ``AsyncMock``
    # default returns evaluate as truthy, and we explicitly do
    # not want to enter the network-touching local-rebase path
    # in test mocks that haven't been set up to handle it.
    if requires_signatures is not True:
        return False, "base branch does not require signatures"

    # Base does require verified signatures. Check whether the
    # current PR head is itself verified — if it isn't, REST
    # update-branch can't make things worse, so we don't need
    # the local-rebase machinery.
    try:
        all_verified, _unverified = await github_client.check_pr_commit_signatures(
            owner, repo, pr_info.number
        )
    except Exception as exc:
        log.debug(
            "Could not check PR commit signatures for %s/%s#%s: %s",
            owner,
            repo,
            pr_info.number,
            exc,
        )
        # Fail closed: if we can't confirm the PR head is
        # verified, route to the REST path. The opposite
        # (assuming verification and using the local path)
        # would mean transient API failures could trigger
        # network-touching local clones, and would conflict
        # with the documented gate ("base requires signatures
        # AND PR head is verified"). When verification isn't
        # established, REST update-branch can't make things
        # any worse than they already are.
        return False, "signature check failed"

    if all_verified:
        return True, "base requires signatures and PR head is verified"
    return False, "PR head is not currently verified"


async def local_rebase_pr(
    *,
    pr_info: PullRequestInfo,
    owner: str,
    repo: str,
    token: str,
    log: logging.Logger,
) -> bool:
    """Rebase a PR locally and force-push the result.

    Clones the head repo into a secure temp workspace, fetches the
    base branch (from upstream when the PR is from a fork), runs
    ``git rebase``, and force-pushes with lease back to the head
    repo.  All git invocations inherit the user's ``~/.gitconfig``,
    so signing config is respected.

    Returns True only if every step succeeds.  On any failure (no
    ``git`` on PATH, conflict during rebase, network error, push
    rejected) the workspace is cleaned up and False is returned;
    the caller should fall through to the auto-merge path so we
    never leave a half-applied state.
    """
    # Ensure ``git`` is on PATH before we start. ``GitError`` is
    # also raised when git is missing entirely.
    try:
        ensure_git_available()
    except Exception as exc:
        log.debug("Local rebase unavailable (no git on PATH?): %s", exc)
        return False

    # We need the head/base clone URLs. They are populated for PRs
    # surfaced by recent versions of the find-similar / merge flows;
    # if missing we synthesise them from the repository names.
    head_clone_url = pr_info.head_repo_clone_url
    base_clone_url = pr_info.base_repo_clone_url
    head_full = pr_info.head_repo_full_name
    base_full = pr_info.base_repo_full_name or f"{owner}/{repo}"
    if not head_clone_url:
        head_clone_url = f"https://github.com/{head_full or base_full}.git"
    if not base_clone_url:
        base_clone_url = f"https://github.com/{base_full}.git"
    head_full = head_full or base_full

    head_branch = pr_info.head_branch
    base_branch = pr_info.base_branch or "main"
    if not head_branch:
        log.debug(
            "Local rebase: PR %s/%s#%s missing head_branch",
            owner,
            repo,
            pr_info.number,
        )
        return False

    origin_url = authed_clone_url(head_clone_url, token)
    upstream_url = authed_clone_url(base_clone_url, token)

    # Use a per-PR workspace under a secure temp parent so
    # concurrent rebases (--concurrency=N) don't collide.
    workspace_parent = Path(
        git_ops.create_secure_tempdir(prefix="dependamerge-localrebase-")
    )
    workspace = (
        workspace_parent
        / f"{(head_full or base_full).replace('/', '__')}__pr_{pr_info.number}"
    )
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        # Clone the head repo at the PR's head branch. Shallow
        # clone keeps disk + network footprint low for what are
        # typically tiny dependency-update PRs.
        try:
            clone(
                origin_url,
                workspace,
                branch=head_branch,
                depth=50,
                single_branch=True,
                no_tags=True,
                filter_blobs=True,
                logger=log.debug,
            )
        except GitError as exc:
            log.debug("Local rebase: clone failed for %s: %s", pr_info.html_url, exc)
            return False

        # Fetch the base branch — from upstream when the PR is
        # from a fork, from origin otherwise. We need it
        # available locally before we can rebase onto it.
        try:
            if (head_full or base_full) != base_full:
                add_remote("upstream", upstream_url, cwd=workspace, logger=log.debug)
                fetch(
                    "upstream",
                    base_branch,
                    cwd=workspace,
                    depth=50,
                    logger=log.debug,
                )
                rebase_onto = f"upstream/{base_branch}"
            else:
                fetch(
                    "origin",
                    base_branch,
                    cwd=workspace,
                    depth=50,
                    logger=log.debug,
                )
                rebase_onto = f"origin/{base_branch}"
        except GitError as exc:
            log.debug("Local rebase: fetch failed for %s: %s", pr_info.html_url, exc)
            return False

        # Make sure we are on the head branch (defensive against
        # detached HEAD after clone --branch).
        try:
            checkout(head_branch, cwd=workspace, create=False, logger=log.debug)
        except GitError:
            # Already on the branch, or branch missing locally;
            # rebase will surface the real problem if any.
            pass

        # Rebase. ``git rebase`` runs with ``check=False`` (see
        # ``git_ops.rebase``), so a non-zero exit does *not* raise
        # ``GitError``; we have to inspect ``returncode``
        # ourselves. Conflicts are the most common cause of a
        # non-zero exit here, but other failures (corrupt index,
        # invalid base ref, etc.) hit the same path — surface
        # stderr/stdout in debug output so the cause is visible
        # to anyone investigating, then abort the rebase to leave
        # the workspace in a clean state before cleanup.
        rebase_result = rebase(
            rebase_onto,
            cwd=workspace,
            autostash=False,
            interactive=False,
            logger=log.debug,
        )

        if rebase_result.returncode != 0:
            log.debug(
                "Local rebase: rebase exited non-zero for %s "
                "(rc=%d, stderr=%r, stdout=%r); aborting.",
                pr_info.html_url,
                rebase_result.returncode,
                rebase_result.stderr,
                rebase_result.stdout,
            )
            try:
                rebase_abort(cwd=workspace, logger=log.debug)
            except Exception:
                pass
            return False

        # Force-push with lease to the head repo. We push back to
        # ``origin`` because the head ref always lives there (even
        # for forks, the head repo *is* the fork).
        try:
            push_force_with_lease(
                "origin",
                head_branch,
                head_branch,
                cwd=workspace,
                logger=log.debug,
            )
        except GitError as exc:
            log.debug(
                "Local rebase: force-push failed for %s: %s",
                pr_info.html_url,
                exc,
            )
            return False

        log.debug("Local rebase succeeded for %s", pr_info.html_url)
        return True

    finally:
        # Always clean up. The workspace contains a clone of the
        # user's repository, so we want it gone even on success.
        try:
            secure_rmtree(workspace_parent)
        except Exception as exc:
            log.debug(
                "Local rebase: failed to clean up workspace %s: %s",
                workspace_parent,
                exc,
            )


async def perform_step5_rebase(
    *,
    ctx: RebaseContext,
    pr_info: PullRequestInfo,
    owner: str,
    repo: str,
) -> Step5Outcome:
    """Run Step 5 of the merge flow: bring the PR up to date with its base.

    Dispatches between the local-git path (signature-preserving)
    and the legacy REST ``update-branch`` path based on
    :func:`should_use_local_rebase`.  When the local path is
    selected, REST ``update-branch`` is **never** called — even on
    local-rebase failure — so we never destroy a verified
    signature.  In the failure case we mark the PR as having been
    through Step 5 (so Step 5.5 doesn't double the configured
    ``merge_timeout``) and let auto-merge take over server-side.

    Returns a :class:`Step5Outcome`.  ``failed=True`` indicates the
    caller should set ``MergeStatus.FAILED`` and bail; the legacy
    REST path is the only path that can produce this outcome (a
    raised exception during ``update_branch`` or the polling loop).
    """
    if ctx.preview_mode:
        # NOTE: In preview mode, we should NOT print here as it
        # breaks single-line reporting.  The preview output
        # should only be a single line per PR in the evaluation
        # section.
        return Step5Outcome()

    log_and_print(
        ctx.log,
        ctx.console,
        f"🔄 Rebasing: {pr_info.html_url} [behind base branch]",
        level="debug",
    )

    use_local, local_reason = await should_use_local_rebase(
        github_client=ctx.github_client,
        pr_info=pr_info,
        owner=owner,
        repo=repo,
        base_branch=pr_info.base_branch or "main",
        rebase_local=ctx.rebase_local,
        log=ctx.log,
    )

    if use_local:
        await _run_local_path(
            ctx=ctx,
            pr_info=pr_info,
            owner=owner,
            repo=repo,
            local_reason=local_reason,
        )
        return Step5Outcome()

    return await _run_rest_path(
        ctx=ctx,
        pr_info=pr_info,
        owner=owner,
        repo=repo,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _run_local_path(
    *,
    ctx: RebaseContext,
    pr_info: PullRequestInfo,
    owner: str,
    repo: str,
    local_reason: str,
) -> None:
    """Local-rebase path.  Always succeeds from the caller's POV.

    Whether the underlying ``git`` workflow succeeds or fails, we
    record the PR in ``_rebased_prs`` so Step 5.5 doesn't double
    the merge_timeout, and we never fall back to REST
    ``update-branch`` (doing so would defeat the whole point of
    the local path).

    We also enable auto-merge here — not in Step 5.5 — because
    marking ``_rebased_prs`` makes Step 5.5 skip this PR, and
    without auto-merge enablement Step 6's skip gate wouldn't
    fire either (it only routes to ``AUTO_MERGE_PENDING`` when
    the PR is in ``_auto_merge_enabled``).  Without that, a still-
    pending PR would fall through to a manual merge attempt and
    405 against unfinished checks — the failure mode this whole
    feature exists to prevent.
    """
    log_and_print(
        ctx.log,
        ctx.console,
        f"🛡️  Local rebase: {pr_info.html_url} [{local_reason}]",
        level="debug",
    )
    try:
        local_rebase_ok = await local_rebase_pr(
            pr_info=pr_info,
            owner=owner,
            repo=repo,
            token=ctx.token,
            log=ctx.log,
        )
    except Exception as exc:
        ctx.log.debug(
            "Local rebase raised unexpectedly for %s: %s",
            pr_info.html_url,
            exc,
        )
        local_rebase_ok = False

    ctx.rebased_prs.add(f"{owner}/{repo}#{pr_info.number}")

    # Enable auto-merge regardless of local-rebase outcome.
    # On success: GitHub may need a few seconds to recompute
    # mergeability and start checks against the new head;
    # auto-merge will fire when checks pass.
    # On failure: the PR is unchanged on the GitHub side; auto-
    # merge will fire when the PR is brought up to date through
    # some other channel (a third-party rebase, a human update).
    # In both cases, having auto-merge enabled means Step 6's
    # skip gate routes the PR to ``AUTO_MERGE_PENDING`` rather
    # than attempting a 405-prone manual merge.
    try:
        await ctx.enable_auto_merge(pr_info, owner, repo)
    except Exception as exc:
        ctx.log.debug(
            "Could not enable auto-merge after local rebase for %s: %s",
            pr_info.html_url,
            exc,
        )

    if local_rebase_ok:
        log_and_print(
            ctx.log,
            ctx.console,
            f"✅ Rebased (local): {pr_info.html_url}",
            level="debug",
        )
    else:
        log_and_print(
            ctx.log,
            ctx.console,
            f"🛡️  Local rebase failed; deferring to auto-merge: {pr_info.html_url}",
            level="debug",
        )


async def _run_rest_path(
    *,
    ctx: RebaseContext,
    pr_info: PullRequestInfo,
    owner: str,
    repo: str,
) -> Step5Outcome:
    """Legacy REST ``update-branch`` path with post-rebase polling.

    Uses the GitHub REST API to bring the PR up to date, enables
    auto-merge so the PR merges even if we time out waiting for
    status checks, then polls until checks complete or
    ``merge_timeout`` elapses.  Updates ``pr_info`` in place with
    the post-rebase state.

    Returns a :class:`Step5Outcome` whose ``failed`` field is True
    when ``update_branch`` (or the polling apparatus) raises an
    exception — the caller should mark the merge as ``FAILED`` in
    that case.
    """
    if ctx.github_client is None:
        return Step5Outcome(failed=True, error_message="GitHub client not initialized")
    client = ctx.github_client

    try:
        await client.update_branch(owner, repo, pr_info.number)

        # Enable auto-merge so the PR merges even if we time out
        # waiting for status checks.
        auto_merge_ok = await ctx.enable_auto_merge(pr_info, owner, repo)
        if auto_merge_ok:
            ctx.log.debug(
                "Auto-merge enabled after rebase for %s/%s#%s",
                owner,
                repo,
                pr_info.number,
            )

        # Wait for GitHub to process the update and run checks
        ctx.console.print(f"⏳ Waiting: {pr_info.html_url}")
        await asyncio.sleep(ctx.merge_recheck_interval)

        updated_mergeable, updated_mergeable_state = await _poll_post_rebase(
            ctx=ctx,
            pr_info=pr_info,
            owner=owner,
            repo=repo,
            auto_merge_ok=auto_merge_ok,
        )

        # Update our PR info with the latest state.  Preserve the
        # previous non-None values when the refresh returns
        # ``null`` (GitHub is still computing).  The Step 6
        # auto-merge skip gate accepts both ``True`` and ``None``
        # (it excludes only the explicit ``False`` case), so a
        # transient null no longer blocks the auto-merge path on
        # its own.  We still preserve the prior known ``True`` so
        # downstream logging and any future tightening of that
        # predicate get an accurate state to work with.  The same
        # rationale applies to ``mergeable_state``: GitHub returns
        # ``null`` while still computing, and the post-rebase
        # reporting / Step 5.5 logic branches on this value (e.g.
        # "clean" vs "blocked" vs "behind"); a transient ``None``
        # would otherwise be classified as the catch-all "other
        # state" branch.
        if updated_mergeable is not None:
            pr_info.mergeable = updated_mergeable
        if updated_mergeable_state is not None:
            pr_info.mergeable_state = updated_mergeable_state

        # Mark this PR as having gone through the Step 5 rebase
        # + poll path.  Step 5.5 will consult ``_rebased_prs`` to
        # avoid doubling the merge_timeout when the rebase exits
        # in ``blocked`` or ``behind`` state.
        ctx.rebased_prs.add(f"{owner}/{repo}#{pr_info.number}")

        _log_post_rebase_status(ctx=ctx, pr_info=pr_info)
        return Step5Outcome()

    except Exception as exc:
        ctx.console.print(f"❌ Failed: {pr_info.html_url} [rebase error: {exc}]")
        return Step5Outcome(failed=True, error_message=f"Failed to rebase PR: {exc}")


async def _poll_post_rebase(
    *,
    ctx: RebaseContext,
    pr_info: PullRequestInfo,
    owner: str,
    repo: str,
    auto_merge_ok: bool,
) -> tuple[bool | None, str | None]:
    """Poll the PR after ``update_branch`` until it stabilises.

    Returns the latest ``(mergeable, mergeable_state)`` observed.
    Updates ``pr_info.head_sha`` in place when the refresh shows a
    new head commit (so any subsequent ``analyze_block_reason()``
    call queries the rebased commit, not the pre-rebase one).
    """
    if ctx.github_client is None:
        return pr_info.mergeable, pr_info.mergeable_state
    client = ctx.github_client

    updated_mergeable: bool | None = pr_info.mergeable
    updated_mergeable_state: str | None = pr_info.mergeable_state

    for check_attempt in range(ctx.merge_poll_max_attempts):
        updated_pr_data: Any = await client.get(
            f"/repos/{owner}/{repo}/pulls/{pr_info.number}"
        )

        if isinstance(updated_pr_data, dict):
            updated_mergeable = updated_pr_data.get("mergeable")
            updated_mergeable_state = updated_pr_data.get("mergeable_state")
            updated_head = (updated_pr_data.get("head") or {}).get("sha")
            if updated_head:
                pr_info.head_sha = updated_head
        else:
            updated_mergeable = None
            updated_mergeable_state = None

        if _poll_should_continue(
            ctx=ctx,
            pr_info=pr_info,
            attempt=check_attempt,
            mergeable_state=updated_mergeable_state,
            auto_merge_ok=auto_merge_ok,
        ):
            await asyncio.sleep(ctx.merge_recheck_interval)
            continue
        break

    return updated_mergeable, updated_mergeable_state


def _poll_should_continue(
    *,
    ctx: RebaseContext,
    pr_info: PullRequestInfo,
    attempt: int,
    mergeable_state: str | None,
    auto_merge_ok: bool,
) -> bool:
    """Return True when the post-rebase poll loop should keep waiting.

    Centralising the per-state decisions here keeps
    :func:`_poll_post_rebase` short and readable.
    """
    if mergeable_state == "clean":
        return False

    last_attempt = attempt >= ctx.merge_poll_max_attempts - 1

    if mergeable_state == "behind":
        if last_attempt:
            return False
        ctx.log.debug(
            "PR still processing rebase, waiting... (attempt %d/%d)",
            attempt + 1,
            ctx.merge_poll_max_attempts,
        )
        return True

    if mergeable_state == "blocked":
        if last_attempt:
            _log_blocked_timeout(ctx=ctx, pr_info=pr_info, auto_merge_ok=auto_merge_ok)
            return False
        ctx.log.debug(
            "PR status checks running after rebase, waiting... (attempt %d/%d)",
            attempt + 1,
            ctx.merge_poll_max_attempts,
        )
        return True

    if mergeable_state is None:
        # GitHub is still computing mergeability (typically right
        # after update_branch).  Treat as transient and keep
        # polling until the deadline or a concrete state arrives —
        # breaking here would otherwise exit prematurely and (if
        # auto-merge enablement failed) fall through to a manual
        # merge attempt against the still-resolving PR state.
        if last_attempt:
            return False
        ctx.log.debug(
            "PR mergeable_state still computing after rebase, "
            "waiting... (attempt %d/%d)",
            attempt + 1,
            ctx.merge_poll_max_attempts,
        )
        return True

    # Any other concrete state ("dirty", "draft", "unstable",
    # "unknown", ...) ends the poll loop immediately.
    return False


def _log_blocked_timeout(
    *,
    ctx: RebaseContext,
    pr_info: PullRequestInfo,
    auto_merge_ok: bool,
) -> None:
    """Emit the user-facing line when the post-rebase poll times out blocked."""
    if auto_merge_ok:
        log_and_print(
            ctx.log,
            ctx.console,
            f"⏳ Auto-merge will complete: {pr_info.html_url} "
            "[timeout waiting for checks]",
            level="warning",
        )
    else:
        log_and_print(
            ctx.log,
            ctx.console,
            f"⚠️ Proceeding without checks: {pr_info.html_url} "
            "[timeout waiting for checks]",
            level="warning",
        )


def _log_post_rebase_status(
    *,
    ctx: RebaseContext,
    pr_info: PullRequestInfo,
) -> None:
    """Emit the post-rebase status line based on the final mergeable_state."""
    state = pr_info.mergeable_state
    if state == "clean":
        log_and_print(
            ctx.log,
            ctx.console,
            f"✅ Rebased: {pr_info.html_url}",
            level="debug",
        )
    elif state == "behind":
        log_and_print(
            ctx.log,
            ctx.console,
            f"⚠️  Rebased: {pr_info.html_url} [still behind after rebase]",
            level="debug",
        )
    elif state == "blocked":
        log_and_print(
            ctx.log,
            ctx.console,
            f"⬆️ Rebased: {pr_info.html_url} [waiting for status checks]",
            level="debug",
        )
    else:
        log_and_print(
            ctx.log,
            ctx.console,
            f"ℹ️  Rebased: {pr_info.html_url}",
            level="debug",
        )
