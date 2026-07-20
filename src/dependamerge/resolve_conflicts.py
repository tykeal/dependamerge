# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
resolve_conflicts: Orchestrate interactive rebase flows to fix simple PR merge conflicts.

This module provides:
- Data models for selecting PRs to fix and controlling behavior
- A FixOrchestrator that:
  * Fetches PR details (head/base repo/branches, fork status, permissions)
  * Prepares secure temporary workspaces and clones/fetches repos
  * Runs an interactive rebase flow (manual resolution via user's editor/mergetool)
  * Amends commit when appropriate and force-pushes the updated branch
  * Cleans up temp workspaces by default (unless keep_temp is requested)
- An InteractiveResolver that guides a user through conflict resolution loops

The orchestrator design allows swapping the resolver for a future automated variant
that can run in parallel. The current interactive flow runs one PR at a time to keep
terminal interaction clean.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .git_ops import (
    GitError,
    add,
    add_all,
    add_remote,
    checkout,
    clone,
    commit_amend_no_edit,
    create_secure_tempdir,
    fetch_branch,
    list_conflicted_files,
    push_force_with_lease,
    rebase,
    rebase_abort,
    rebase_continue,
    rev_list_count,
    run_git,
    secure_rmtree,
)
from .github_async import GitHubAsync

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class PRSelection:
    """Minimal PR selector: repository 'owner/repo' and PR number."""

    repository: str
    pr_number: int


@dataclass
class FixOptions:
    """
    Options controlling the fix workflow.

    Attributes:
        workdir: Base directory for workspaces. If None, a secure temp directory is created.
        keep_temp: If True, workspaces are not removed on completion (default False).
        prefetch: Number of concurrent workspace preparations (clone/fetch).
        editor: Override command to edit conflicted files. If None, use $VISUAL or $EDITOR.
        mergetool: If True, try 'git mergetool' for conflicts; otherwise open in editor.
        interactive: If True, attach git commands to TTY where useful for user feedback.
        logger: Optional logger callable for informational messages (redacted).
    """

    workdir: str | None = None
    keep_temp: bool = False
    prefetch: int = 6
    editor: str | None = None
    mergetool: bool = False
    interactive: bool = True
    logger: Callable[[str], None] | None = None


@dataclass
class FixResult:
    """Outcome of attempting to fix a single PR."""

    selection: PRSelection
    success: bool
    message: str
    workspace: str | None = None


@dataclass
class PRContext:
    """Detailed PR information required for cloning/rebasing/pushing."""

    owner: str
    repo: str
    pr_number: int
    base_branch: str
    head_branch: str
    base_repo_full_name: str
    base_repo_clone_url: str
    head_repo_full_name: str
    head_repo_clone_url: str
    is_fork: bool
    maintainer_can_modify: bool

    @property
    def selection(self) -> PRSelection:
        return PRSelection(
            repository=f"{self.owner}/{self.repo}", pr_number=self.pr_number
        )


class FixOrchestrator:
    """
    Coordinates fetching PR details, preparing workspaces, and running the interactive
    conflict resolution flow. Interactive resolution is executed serially; workspace
    preparation (clone/fetch) is parallelized for responsiveness.
    """

    def __init__(
        self,
        token: str,
        *,
        progress_tracker: object | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        if not token:
            raise ValueError("A GitHub token is required for fix operations.")
        self._token = token
        self._progress = progress_tracker
        self._logger = logger or (lambda m: None)

    async def fetch_pr_details(
        self, selections: Sequence[PRSelection]
    ) -> list[PRContext]:
        """
        Fetch PR details via REST (single GitHubAsync session) for all selections.

        Returns:
            A list of PRContext containing the necessary repo/branch/permission info.
        """
        contexts: list[PRContext] = []

        async with GitHubAsync(token=self._token) as api:
            tasks = []
            for sel in selections:
                try:
                    owner, repo = sel.repository.split("/", 1)
                except ValueError:
                    self._log(
                        f"Skipping invalid repository full name: {sel.repository}"
                    )
                    continue

                tasks.append(self._fetch_one_pr(api, owner, repo, sel.pr_number))

            for coro in asyncio.as_completed(tasks):
                try:
                    ctx = await coro
                    if ctx:
                        contexts.append(ctx)
                except Exception as e:
                    self._log(f"Error fetching PR details: {e}")

        return contexts

    async def _fetch_one_pr(
        self, api: GitHubAsync, owner: str, repo: str, number: int
    ) -> PRContext | None:
        data = await api.get(f"/repos/{owner}/{repo}/pulls/{number}")
        if not isinstance(data, dict):
            return None

        base = data.get("base") or {}
        head = data.get("head") or {}
        base_repo = base.get("repo") or {}
        head_repo = head.get("repo") or {}

        base_branch = base.get("ref") or ""
        head_branch = head.get("ref") or ""
        base_full = base_repo.get("full_name") or f"{owner}/{repo}"
        head_full = head_repo.get("full_name") or base_full
        base_clone = base_repo.get("clone_url") or f"https://github.com/{base_full}.git"
        head_clone = head_repo.get("clone_url") or base_clone
        is_fork = bool(head_repo.get("fork")) if head_repo else False
        maint_mod = bool(data.get("maintainer_can_modify"))

        return PRContext(
            owner=owner,
            repo=repo,
            pr_number=number,
            base_branch=base_branch,
            head_branch=head_branch,
            base_repo_full_name=base_full,
            base_repo_clone_url=base_clone,
            head_repo_full_name=head_full,
            head_repo_clone_url=head_clone,
            is_fork=is_fork,
            maintainer_can_modify=maint_mod,
        )

    def run(
        self, selections: Sequence[PRSelection], options: FixOptions
    ) -> list[FixResult]:
        """
        Perform the full fix process:
          - create or use secure base workdir
          - fetch PR details
          - prefetch (clone/fetch) workspaces in parallel
          - resolve each PR interactively in serial
          - push updates and cleanup
        """
        # Create secure base workdir if not provided
        temp_created = False
        if options.workdir:
            base_dir = Path(options.workdir).absolute()
            base_dir.mkdir(parents=True, exist_ok=True)
        else:
            base_dir = Path(create_secure_tempdir(prefix="dependamerge-")).absolute()
            temp_created = True
            self._log(f"Created secure temp workspace at {base_dir}")

        # Wrap in try/finally for cleanup
        try:
            # Fetch detailed PR contexts
            if self._progress:
                op = getattr(self._progress, "update_operation", None)
                if callable(op):
                    try:
                        op("Fetching PR details for fix candidates...")
                    except Exception:
                        # Progress display is best-effort; ignore UI errors.
                        pass

            contexts = asyncio.run(self.fetch_pr_details(selections))

            # Filter out PRs we cannot push to (forks without maintainer_can_modify)
            prepared: list[tuple[PRContext, Path | None, str | None]] = []
            to_prepare: list[PRContext] = []
            for ctx in contexts:
                if ctx.is_fork and not ctx.maintainer_can_modify:
                    msg = "Skipping fork without maintainer-can-modify permission"
                    self._log(f"{ctx.base_repo_full_name}#{ctx.pr_number}: {msg}")
                    prepared.append((ctx, None, msg))
                else:
                    to_prepare.append(ctx)

            # Prefetch workspaces (clone & fetch) in parallel
            if to_prepare:
                if self._progress:
                    op = getattr(self._progress, "update_operation", None)
                    if callable(op):
                        try:
                            op("Preparing workspaces (clone/fetch repos)...")
                        except Exception:
                            # Progress display is best-effort; ignore UI errors.
                            pass
                prepared += self._prepare_workspaces_parallel(
                    to_prepare, base_dir, options
                )
            else:
                self._log("No PRs eligible for workspace preparation.")

            # Interactive resolution in serial to keep terminal clear
            resolver = InteractiveResolver(
                token=self._token, logger=options.logger or self._logger
            )

            results: list[FixResult] = []
            for ctx, workspace, prep_err in prepared:
                sel = ctx.selection
                if workspace is None:
                    results.append(
                        FixResult(
                            selection=sel,
                            success=False,
                            message=prep_err or "Preparation failed",
                        )
                    )
                    continue

                if self._progress:
                    suspend_fn = getattr(self._progress, "suspend", None)
                    if callable(suspend_fn):
                        try:
                            suspend_fn()
                        except Exception:
                            # Progress display is best-effort; ignore UI errors.
                            pass
                self._log(
                    f"Starting interactive rebase for {ctx.base_repo_full_name}#{ctx.pr_number} in {workspace}"
                )

                try:
                    ok, msg = resolver.resolve(ctx, workspace, options)
                    results.append(
                        FixResult(
                            selection=sel,
                            success=ok,
                            message=msg,
                            workspace=str(workspace),
                        )
                    )
                    self._log(f"{ctx.base_repo_full_name}#{ctx.pr_number}: {msg}")
                except KeyboardInterrupt:
                    # Attempt to abort any in-progress rebase and record failure
                    try:
                        rebase_abort(cwd=workspace)
                    except Exception as abort_err:
                        # Cleanup abort is best-effort; the failure is
                        # already recorded below regardless.  Surface it
                        # through the orchestrator logger so an unexpected
                        # cleanup failure remains discoverable.
                        self._log(
                            f"{ctx.base_repo_full_name}#{ctx.pr_number}: "
                            f"rebase --abort cleanup failed: {abort_err}"
                        )
                    results.append(
                        FixResult(
                            selection=sel,
                            success=False,
                            message="Aborted by user",
                            workspace=str(workspace),
                        )
                    )
                    self._log(
                        f"{ctx.base_repo_full_name}#{ctx.pr_number}: Aborted by user"
                    )
                except Exception as e:
                    results.append(
                        FixResult(
                            selection=sel,
                            success=False,
                            message=f"Error: {e}",
                            workspace=str(workspace),
                        )
                    )
                    self._log(f"{ctx.base_repo_full_name}#{ctx.pr_number}: Error: {e}")
                finally:
                    if self._progress:
                        resume_fn = getattr(self._progress, "resume", None)
                        if callable(resume_fn):
                            try:
                                resume_fn()
                            except Exception:
                                # Progress display is best-effort; ignore UI errors.
                                pass

            return results
        finally:
            # Cleanup base temp directory if we created it and keep_temp is False
            if temp_created and not options.keep_temp:
                try:
                    secure_rmtree(str(base_dir))
                    self._log(f"Removed temp workspace at {base_dir}")
                except Exception as e:
                    self._log(
                        f"Warning: Failed to remove temp workspace {base_dir}: {e}"
                    )

    def _prepare_workspaces_parallel(
        self,
        contexts: Sequence[PRContext],
        base_dir: Path,
        options: FixOptions,
    ) -> list[tuple[PRContext, Path | None, str | None]]:
        """
        Clone/fetch repositories for contexts in parallel.

        Returns:
            List of tuples (context, workspace_path or None, error_message or None).
        """
        results: list[tuple[PRContext, Path | None, str | None]] = []

        def worker(ctx: PRContext) -> tuple[PRContext, Path | None, str | None]:
            try:
                ws = self._prepare_single_workspace(ctx, base_dir, options)
                return (ctx, ws, None)
            except Exception as e:
                return (ctx, None, str(e))

        max_workers = max(1, int(options.prefetch or 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(worker, c) for c in contexts]
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())

        return results

    def _prepare_single_workspace(
        self,
        ctx: PRContext,
        base_dir: Path,
        options: FixOptions,
    ) -> Path:
        """
        Create a workspace directory and clone/fetch the necessary branches/remotes.

        Strategy:
        - Clone head repo (push target) at head_branch for PR.
        - If base repo differs, add 'upstream' remote and fetch base_branch.
        - If same repo, ensure base_branch is fetched from origin as well.
        """
        workspace_name = (
            f"{ctx.head_repo_full_name.replace('/', '__')}__pr_{ctx.pr_number}"
        )
        workspace = base_dir / workspace_name
        workspace.mkdir(parents=True, exist_ok=True)

        # Clone/fetch with clean (credential-free) URLs; the token is
        # supplied per-operation via GIT_ASKPASS so it never lands in
        # argv or the workspace's .git/config.
        origin_url = ctx.head_repo_clone_url
        upstream_url = ctx.base_repo_clone_url

        # Clone head repo
        self._log(f"Cloning {ctx.head_repo_full_name}@{ctx.head_branch} -> {workspace}")
        clone(
            origin_url,
            workspace,
            branch=ctx.head_branch,
            depth=50,
            single_branch=True,
            no_tags=True,
            filter_blobs=True,
            logger=self._log,
            token=self._token,
        )

        # Ensure we have base branch available for rebase
        if ctx.head_repo_full_name != ctx.base_repo_full_name:
            add_remote("upstream", upstream_url, cwd=workspace, logger=self._log)
            # Use ``fetch_branch`` so ``upstream/<base_branch>``
            # lands as a remote-tracking ref — the ``--single-branch``
            # clone above restricts the origin's configured refspec
            # to the PR head branch, so a bare
            # ``git fetch upstream <base>`` would only populate
            # ``FETCH_HEAD`` and the downstream
            # ``git rebase upstream/<base>`` in
            # :meth:`InteractiveResolver.resolve` would fail with
            # ``fatal: invalid upstream 'upstream/<base>'``.
            fetch_branch(
                "upstream",
                ctx.base_branch,
                cwd=workspace,
                depth=50,
                logger=self._log,
                token=self._token,
            )
        else:
            # Same repo; fetch the base branch from origin into the
            # remote-tracking ref (see comment in the fork branch
            # above for why ``fetch_branch`` is required rather than
            # a bare ``fetch``).
            fetch_branch(
                "origin",
                ctx.base_branch,
                cwd=workspace,
                depth=50,
                logger=self._log,
                token=self._token,
            )

        # Ensure we are on the head branch explicitly (detached HEAD safety)
        checkout(ctx.head_branch, cwd=workspace, create=False, logger=self._log)

        return workspace

    def _log(self, msg: str) -> None:
        try:
            self._logger(msg)
        except Exception:
            # The injected logger failed; record the cause with context
            # via the module logger, then still emit the message on stdout
            # so interactive output is not lost.
            _LOG.warning("Injected logger failed; using stdout fallback", exc_info=True)
            print(msg)


class InteractiveResolver:
    """
    Drives a manual conflict resolution process for a PR:
      - Start rebase onto base branch
      - On conflicts, for each conflicted file open user's editor or mergetool
      - Stage and continue rebase until clean
      - Amend commit when the PR is a single-commit change
      - Force push with lease to update the PR branch
    """

    def __init__(
        self,
        token: str,
        *,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._token = token
        self._log = logger or (lambda m: None)

    def resolve(
        self, ctx: PRContext, workspace: Path, options: FixOptions
    ) -> tuple[bool, str]:
        """
        Resolve conflicts interactively in the given workspace.

        Returns:
            (success, message)
        """
        base_remote = (
            "upstream"
            if ctx.head_repo_full_name != ctx.base_repo_full_name
            else "origin"
        )
        base_ref = f"{base_remote}/{ctx.base_branch}"

        # Initial rebase attempt
        self._log(f"Rebasing onto {base_ref}")
        rb = rebase(
            base_ref,
            cwd=workspace,
            autostash=True,
            interactive=options.interactive,
            logger=self._log,
        )
        if rb.returncode == 0:
            # Clean rebase; proceed to post steps
            self._log("Rebase completed without conflicts.")
        else:
            self._log("Conflicts detected. Entering manual resolution loop.")
            # Loop until rebase completes or user aborts
            while True:
                conflicts = list_conflicted_files(cwd=workspace, logger=self._log)
                if not conflicts:
                    # Sometimes rebase stops without conflicts (e.g., needs staging)
                    # Try to continue directly.
                    cont = rebase_continue(
                        cwd=workspace, interactive=options.interactive, logger=self._log
                    )
                    if cont.returncode == 0:
                        break
                    # If still not continuing, give the user a chance to edit anything
                    self._open_editor_for_paths(workspace, [], options)
                    add_all(cwd=workspace, logger=self._log)
                    cont = rebase_continue(
                        cwd=workspace, interactive=options.interactive, logger=self._log
                    )
                    if cont.returncode == 0:
                        break
                    # If it still fails, abort with an error message
                    return False, "Rebase could not continue and no conflicts listed"

                # Present and resolve each conflicted file
                self._log(f"Conflicted files: {', '.join(conflicts)}")
                if options.mergetool:
                    # Prefer mergetool if requested/configured
                    for path in conflicts:
                        self._run_mergetool(workspace, path, options)
                        add(path, cwd=workspace, logger=self._log)
                else:
                    # Open editor for each file
                    self._open_editor_for_paths(workspace, conflicts, options)
                    add(conflicts, cwd=workspace, logger=self._log)

                # Attempt to continue
                cont = rebase_continue(
                    cwd=workspace, interactive=options.interactive, logger=self._log
                )
                if cont.returncode == 0:
                    break
                # If still conflicts, loop again

        # Post-rebase: decide on amend rule
        try:
            count_expr = f"{base_ref}..HEAD"
            commit_count = rev_list_count(count_expr, cwd=workspace, logger=self._log)
        except GitError:
            commit_count = 0

        if commit_count == 1:
            # Single-commit PR: amend to preserve no extra top commit (no message change)
            self._log(
                "Single-commit change detected; amending commit without editing message."
            )
            try:
                commit_amend_no_edit(cwd=workspace, logger=self._log)
            except GitError as e:
                # Non-fatal; continue to push anyway
                self._log(f"Warning: amend failed: {e}")

        # Force push to update PR head branch
        self._log(
            f"Pushing updated branch with --force-with-lease to origin {ctx.head_branch}"
        )
        try:
            push_force_with_lease(
                "origin",
                "HEAD",
                f"refs/heads/{ctx.head_branch}",
                cwd=workspace,
                logger=self._log,
                token=self._token,
            )
        except GitError as e:
            return False, f"Push failed: {e}"

        return (
            True,
            "Rebased, amended (if applicable), and force-pushed to trigger checks",
        )

    def _open_editor_for_paths(
        self, cwd: Path, paths: Sequence[str], options: FixOptions
    ) -> None:
        """
        Open the user's editor for the given file paths. If no paths provided,
        open the editor at the repository root to allow manual edits.
        """
        editor_cmd = self._pick_editor(options)
        if not editor_cmd:
            # As a last resort, print instructions
            self._log(
                "No editor found. Please resolve conflicts manually in the workspace and then continue."
            )
            return

        # If the editor is VS Code, ensure we wait for the window to close
        # (-w) so the rebase does not continue before the user has saved
        # their conflict resolutions.
        #
        # Match on the launcher's program name (basename without a Windows
        # extension) against the known VS Code commands rather than a naive
        # substring test: a plain ``"code" in cmd_parts[0]`` would also fire
        # on unrelated binaries such as ``encode``, ``xcode`` or ``mycode``,
        # while still missing path-qualified launchers like ``/usr/bin/code``.
        cmd_parts = shlex.split(editor_cmd)
        prog = (
            Path(cmd_parts[0]).name.lower().removesuffix(".cmd").removesuffix(".exe")
            if cmd_parts
            else ""
        )
        if prog in ("code", "code-insiders") and "-w" not in cmd_parts:
            cmd_parts.append("-w")

        if paths:
            for p in paths:
                self._run_editor(cmd_parts, cwd, p)
        else:
            # Open editor at repo root
            self._run_editor(cmd_parts, cwd, None)

    def _run_editor(
        self, cmd_parts: list[str], cwd: Path, rel_path: str | None
    ) -> None:
        args = list(cmd_parts)
        if rel_path:
            args.append(rel_path)
        self._log(f"Opening editor: {' '.join(args)}")
        subprocess.run(args, cwd=str(cwd), check=False)

    def _run_mergetool(self, cwd: Path, rel_path: str, options: FixOptions) -> None:
        # Prefer --no-prompt to block until the tool finishes for this file
        args = ["git", "mergetool", "--no-prompt", "--", rel_path]
        run_git(
            args,
            cwd=cwd,
            interactive=options.interactive,
            check=False,
            logger=self._log,
        )

    def _pick_editor(self, options: FixOptions) -> str | None:
        if options.editor:
            return options.editor
        # Environment-driven choice
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if editor:
            return editor
        # Platform defaults
        if sys.platform.startswith("win"):
            return "notepad"
        # POSIX default
        return "vi"
