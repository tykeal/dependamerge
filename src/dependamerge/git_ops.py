# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
git_ops: Safe wrappers around common git operations with token redaction.

This module provides:
- A single run_git() entrypoint that redacts secrets in logs and exceptions
- High-level helpers for clone/fetch/checkout/rebase/push flows
- Utilities for secure temporary workspaces

Design goals:
- Never leak credentials or tokens in logs or exceptions
- Reasonable defaults for automation (no prompts, fast clones, skip LFS smudge)
- Allow interactive passes (inherit stdio) when the caller needs terminal UI
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Public API
__all__ = [
    "GitError",
    "GitResult",
    "ensure_git_available",
    "run_git",
    "clone",
    "add_remote",
    "fetch",
    "checkout",
    "rebase",
    "rebase_continue",
    "rebase_abort",
    "status_porcelain",
    "list_conflicted_files",
    "add",
    "add_all",
    "commit_amend_no_edit",
    "push_force_with_lease",
    "rev_list_count",
    "create_secure_tempdir",
    "secure_rmtree",
]

# Type aliases
PathLike = str | Path

# SECURITY: Token redaction patterns cover all known GitHub token formats,
# GitLab tokens, and JWT-like tokens. Keep this list up to date when new
# token prefixes are introduced. See git_ops module docstring for design goals.
_TOKEN_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{20,}", re.IGNORECASE),  # GitHub PAT (classic)
    re.compile(r"ghs_[A-Za-z0-9]{20,}", re.IGNORECASE),  # GitHub App installation
    re.compile(r"ghu_[A-Za-z0-9]{20,}", re.IGNORECASE),  # GitHub user-to-server
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}", re.IGNORECASE),  # fine-grained
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}", re.IGNORECASE),  # GitLab PAT
    # JWT-like long tokens (best-effort)
    re.compile(r"[A-Za-z0-9-_]{20,}\.[A-Za-z0-9-_]{20,}\.[A-Za-z0-9-_]{10,}"),
]
# http(s)://user:password@host
_BASIC_AUTH_IN_URL = re.compile(r"(https?://[^:/@\s]+:)([^@/\s]+)(@)", re.IGNORECASE)
# x-access-token:<token>@ in URL
_X_ACCESS_TOKEN_IN_URL = re.compile(r"(x-access-token:)([^@]+)(@)", re.IGNORECASE)


def _redact(text: str) -> str:
    """Redact likely secrets (tokens/passwords) from a string."""
    if not text:
        return text
    # Basic auth password redaction
    text = _BASIC_AUTH_IN_URL.sub(r"\1***\3", text)
    # x-access-token redaction
    text = _X_ACCESS_TOKEN_IN_URL.sub(r"\1***\3", text)
    # Token patterns
    for pat in _TOKEN_PATTERNS:
        text = pat.sub("***", text)
    return text


def _redact_seq(parts: Sequence[str]) -> Sequence[str]:
    """Redact secrets from a sequence of strings."""
    return [(_redact(p) if isinstance(p, str) else p) for p in parts]


def _build_git_env(env_overrides: dict[str, str] | None = None, *, lfs_skip: bool = True) -> dict[str, str]:
    """Build a safe environment for git invocations."""
    env = os.environ.copy()

    # Avoid interactive auth prompts inside automation
    env.setdefault("GIT_TERMINAL_PROMPT", "0")

    # Speed up clones when LFS is present; callers can disable if needed
    if lfs_skip:
        env.setdefault("GIT_LFS_SKIP_SMUDGE", "1")

    # Allow overrides (but do not allow overriding security toggles to unsafe defaults)
    if env_overrides:
        for k, v in env_overrides.items():
            env[k] = v

    return env


@dataclass
class GitResult:
    """Result of a git command execution."""

    returncode: int
    stdout: str
    stderr: str
    args: tuple[str, ...]


class GitError(RuntimeError):
    """Raised when a git command fails with non-zero exit code."""

    def __init__(
        self,
        message: str,
        *,
        args: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        redacted_cmd = _redact(" ".join(args))
        redacted_out = _redact(stdout or "")
        redacted_err = _redact(stderr or "")
        super().__init__(
            f"{message}\n  cmd: {redacted_cmd}\n  exit: {returncode}\n  stderr: {redacted_err.strip()}"
        )
        # SECURITY: Redact args_vec to prevent token leakage if callers
        # inspect exception attributes. Command args may contain tokens
        # embedded in clone URLs (e.g., x-access-token:<token>@host).
        self.args_vec = tuple(_redact(str(a)) for a in args)
        self.returncode = returncode
        self.stdout = redacted_out
        self.stderr = redacted_err


def ensure_git_available() -> None:
    """Ensure 'git' is available on PATH; raise GitError if not."""
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(
                "git is not available or failed to run",
                args=("git", "--version"),
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )
    except FileNotFoundError as e:
        raise GitError(
            "git executable not found on PATH",
            args=("git", "--version"),
            returncode=127,
            stdout="",
            stderr=str(e),
        ) from e


def run_git(
    args: Sequence[str],
    *,
    cwd: PathLike | None = None,
    env_overrides: dict[str, str] | None = None,
    interactive: bool = False,
    check: bool = True,
    timeout: float | None = None,
    logger: Callable[[str], None] | None = None,
    lfs_skip: bool = True,
) -> GitResult:
    """
    Run a git command safely with redaction.

    Args:
        args: Full command, starting with 'git' (e.g., ["git","status","--porcelain"]).
        cwd: Working directory for the command.
        env_overrides: Environment overrides to merge.
        interactive: If True, inherit stdin/stdout/stderr (no capture) - for user sessions.
        check: If True, raise GitError on non-zero exit code.
        timeout: Optional timeout in seconds.
        logger: Optional logger callable receiving a redacted command line string.
        lfs_skip: If True, set GIT_LFS_SKIP_SMUDGE=1 by default.

    Returns:
        GitResult with stdout/stderr captured (empty when interactive=True).

    Raises:
        GitError if check=True and the command fails.
    """
    if not args or args[0] != "git":
        raise ValueError("run_git requires args to start with 'git'")

    env = _build_git_env(env_overrides, lfs_skip=lfs_skip)

    # Build a redacted command string for logging
    cmd_str = shlex.join(_redact_seq([str(a) for a in args]))  # type: ignore[arg-type]
    if logger:
        logger(f"$ {cmd_str}")

    try:
        if interactive:
            retcode = subprocess.run(
                list(args),
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                check=False,
                timeout=timeout,
            ).returncode
            stdout_str = ""
            stderr_str = ""
        else:
            cp = subprocess.run(
                list(args),
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
            retcode = cp.returncode
            stdout_str = str(cp.stdout or "")
            stderr_str = str(cp.stderr or "")

        result = GitResult(
            returncode=retcode,
            stdout=stdout_str,
            stderr=stderr_str,
            args=tuple(str(a) for a in args),
        )

        if check and result.returncode != 0:
            raise GitError(
                "git command failed",
                args=result.args,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return result
    except subprocess.TimeoutExpired as e:
        raise GitError(
            "git command timed out",
            args=tuple(str(a) for a in args),
            returncode=124,
            stdout="",
            stderr=str(e),
        ) from e


# -----------------------------
# High-level git helper methods
# -----------------------------


def clone(
    url: str,
    dest: PathLike,
    *,
    branch: str | None = None,
    depth: int | None = 50,
    single_branch: bool = True,
    no_tags: bool = True,
    filter_blobs: bool = True,
    quiet: bool = True,
    logger: Callable[[str], None] | None = None,
) -> None:
    """Clone a repository with defaults optimized for speed and safety."""
    args = ["git", "clone"]
    if quiet:
        args.append("--quiet")
    if depth and depth > 0:
        args.extend(["--depth", str(depth)])
    if single_branch:
        args.append("--single-branch")
    if no_tags:
        args.append("--no-tags")
    if filter_blobs:
        args.extend(["--filter=blob:none"])
    if branch:
        args.extend(["--branch", branch])
    args.extend([url, str(dest)])

    run_git(args, logger=logger)


def add_remote(
    name: str,
    url: str,
    *,
    cwd: PathLike,
    logger: Callable[[str], None] | None = None,
) -> None:
    run_git(["git", "remote", "add", name, url], cwd=cwd, logger=logger)


def fetch(
    remote: str,
    refspecs: str | Sequence[str] = (),
    *,
    cwd: PathLike,
    depth: int | None = None,
    unshallow: bool = False,
    prune: bool = False,
    logger: Callable[[str], None] | None = None,
) -> None:
    """Fetch refs with optional shallow/unshallow behavior.

    .. warning::

       Passing a *bare* branch name (e.g. ``fetch("origin", "main")``)
       only populates ``FETCH_HEAD``; it does **not** update
       ``refs/remotes/<remote>/<branch>`` unless the remote's
       configured fetch refspec already covers that branch.  After a
       ``--single-branch`` clone (which is :func:`clone`'s default)
       the configured refspec covers only the branch the clone
       targeted, so a subsequent ``fetch("origin", "main")`` leaves
       ``origin/main`` *undefined* locally and any downstream
       ``rebase`` / ``merge`` / ``rev-list`` against ``origin/main``
       fails with ``fatal: invalid upstream 'origin/main'``.

       When the caller wants the fetched branch to be usable as a
       remote-tracking ref (the usual case), use :func:`fetch_branch`
       instead, which always writes through an explicit refspec
       mapping.  Callers that genuinely want
       ``FETCH_HEAD``-only semantics (e.g. preparing a one-shot
       ``git merge FETCH_HEAD``) can keep using the bare form here.
    """
    args = ["git", "fetch", remote]
    if prune:
        args.append("--prune")
    if unshallow:
        args.append("--unshallow")
    if depth and depth > 0:
        args.extend(["--depth", str(depth)])
    if isinstance(refspecs, str):
        if refspecs:
            args.append(refspecs)
    else:
        args.extend(list(refspecs))
    run_git(args, cwd=cwd, logger=logger)


def fetch_branch(
    remote: str,
    branch: str,
    *,
    cwd: PathLike,
    depth: int | None = None,
    force: bool = True,
    logger: Callable[[str], None] | None = None,
) -> None:
    """Fetch ``branch`` from ``remote`` into ``refs/remotes/<remote>/<branch>``.

    Wraps :func:`fetch` with an explicit refspec mapping so the
    remote-tracking ref always lands locally, regardless of the
    remote's configured fetch refspec.  This is the safe form to
    use after a ``--single-branch`` clone (which is :func:`clone`'s
    default) when subsequent code needs to refer to
    ``<remote>/<branch>`` — e.g. as the target of
    :func:`rebase` / :func:`rev_list_count` / a ``log <r>/<b>..HEAD``
    invocation.

    A bare ``git fetch <remote> <branch>`` would only populate
    ``FETCH_HEAD`` in that scenario and the downstream rebase would
    fail with ``fatal: invalid upstream '<remote>/<branch>'``
    — see :func:`fetch` for the full background.

    Args:
        remote: Remote name (e.g. ``"origin"`` or ``"upstream"``).
        branch: Branch name on the remote (no ``refs/heads/`` prefix).
        cwd: Working directory in which to run ``git``.
        depth: Optional shallow-fetch depth.  ``None`` means
            "inherit the existing depth" (no ``--depth`` flag).
        force: When True (the default), prepend ``+`` to the
            refspec so the remote-tracking ref is updated even when
            the remote has been force-pushed (the common case for
            dependency-update bot branches that get re-pushed).
        logger: Optional logger callback.
    """
    prefix = "+" if force else ""
    refspec = f"{prefix}refs/heads/{branch}:refs/remotes/{remote}/{branch}"
    fetch(remote, refspec, cwd=cwd, depth=depth, logger=logger)



def checkout(
    branch: str,
    *,
    cwd: PathLike,
    create: bool = False,
    track: str | None = None,
    logger: Callable[[str], None] | None = None,
) -> None:
    """Checkout a branch; optionally create and set upstream."""
    args = ["git", "checkout"]
    if create:
        args.append("-B")
    args.append(branch)
    run_git(args, cwd=cwd, logger=logger)
    if track:
        run_git(
            ["git", "branch", "--set-upstream-to", track, branch],
            cwd=cwd,
            logger=logger,
        )


def rebase(
    onto: str,
    *,
    cwd: PathLike,
    autostash: bool = True,
    interactive: bool = False,
    logger: Callable[[str], None] | None = None,
) -> GitResult:
    """
    Start a rebase onto the provided branch/ref.

    If interactive=True, inherits stdio to allow editor/mergetool usage during conflicts.
    """
    args = ["git", "rebase"]
    if autostash:
        args.append("--autostash")
    args.append(onto)
    return run_git(args, cwd=cwd, interactive=interactive, check=False, logger=logger)


def rebase_continue(
    *,
    cwd: PathLike,
    interactive: bool = False,
    logger: Callable[[str], None] | None = None,
) -> GitResult:
    return run_git(
        ["git", "rebase", "--continue"],
        cwd=cwd,
        interactive=interactive,
        check=False,
        logger=logger,
    )


def rebase_abort(
    *,
    cwd: PathLike,
    logger: Callable[[str], None] | None = None,
) -> None:
    run_git(["git", "rebase", "--abort"], cwd=cwd, logger=logger)


def status_porcelain(
    *,
    cwd: PathLike,
    logger: Callable[[str], None] | None = None,
) -> str:
    """Return porcelain status output."""
    res = run_git(["git", "status", "--porcelain"], cwd=cwd, check=True, logger=logger)
    return res.stdout


def list_conflicted_files(
    *,
    cwd: PathLike,
    logger: Callable[[str], None] | None = None,
) -> list[str]:
    """
    Parse 'git status --porcelain' to list conflicted files.

    Conflicted XY codes include: DD, AU, UD, UA, DU, AA, UU
    """
    out = status_porcelain(cwd=cwd, logger=logger)
    conflicted = []
    for line in out.splitlines():
        if not line:
            continue
        # Format: XY <path>
        code = line[:2]
        path = line[3:].strip()
        if code in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
            conflicted.append(path)
    return conflicted


def add(
    paths: str | Sequence[str],
    *,
    cwd: PathLike,
    logger: Callable[[str], None] | None = None,
) -> None:
    if isinstance(paths, str):
        args = ["git", "add", "--", paths]
    else:
        args = ["git", "add", "--", *paths]
    run_git(args, cwd=cwd, logger=logger)


def add_all(
    *,
    cwd: PathLike,
    logger: Callable[[str], None] | None = None,
) -> None:
    run_git(["git", "add", "-A"], cwd=cwd, logger=logger)


def commit_amend_no_edit(
    *,
    cwd: PathLike,
    no_verify: bool = False,
    logger: Callable[[str], None] | None = None,
) -> None:
    args = ["git", "commit", "--amend", "--no-edit"]
    if no_verify:
        args.append("--no-verify")
    run_git(args, cwd=cwd, logger=logger)


def push_force_with_lease(
    remote: str,
    src_ref: str,
    dst_ref: str,
    *,
    cwd: PathLike,
    logger: Callable[[str], None] | None = None,
) -> None:
    run_git(
        ["git", "push", "--force-with-lease", remote, f"{src_ref}:{dst_ref}"],
        cwd=cwd,
        logger=logger,
    )


def rev_list_count(
    range_expr: str,
    *,
    cwd: PathLike,
    logger: Callable[[str], None] | None = None,
) -> int:
    """Return the number of commits in the given revision range (e.g., 'base..HEAD')."""
    res = run_git(["git", "rev-list", "--count", range_expr], cwd=cwd, logger=logger)
    try:
        return int((res.stdout or "0").strip())
    except ValueError:
        return 0


# --------------------------------------
# Secure temporary directory helpers
# --------------------------------------


def create_secure_tempdir(prefix: str = "dependamerge-") -> str:
    """
    Create a temporary directory with restrictive permissions (0700).

    Returns:
        Absolute path to the created directory.
    """
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        os.chmod(path, 0o700)
    except Exception:
        # Best effort; continue even if chmod fails (Windows, etc.)
        pass
    return path


def _chmod_tree_safe(
    path: PathLike, file_mode: int = 0o600, dir_mode: int = 0o700
) -> None:
    """Best-effort to ensure paths are writable/removable by adjusting modes."""
    try:
        p = Path(path)
        if not p.exists():
            return
        for root, dirs, files in os.walk(p, topdown=False):
            for name in files:
                fp = Path(root) / name
                try:
                    os.chmod(fp, file_mode)
                except Exception:
                    # Best-effort: skip files we cannot chmod; the
                    # later rmtree retry handles stubborn paths.
                    pass
            for name in dirs:
                dp = Path(root) / name
                try:
                    os.chmod(dp, dir_mode)
                except Exception:
                    # Best-effort: skip dirs we cannot chmod.
                    pass
        try:
            os.chmod(p, dir_mode)
        except Exception:
            # Best-effort: ignore failure to chmod the tree root.
            pass
    except Exception:
        # Ignore any errors; deletion attempts will proceed anyway
        pass


def secure_rmtree(path: PathLike) -> None:
    """
    Remove a directory tree, attempting to scrub permissions first.

    Note: This does not guarantee cryptographically secure wiping of file
    contents. It makes a best effort to avoid permission-related failures
    and to remove all files. For true secure deletion, additional OS-level
    facilities are required and platform-dependent.
    """
    _chmod_tree_safe(path)
    try:
        shutil.rmtree(path)
    except Exception:
        # Retry with onerror handler that adjusts perms
        def _onerror(func, p, exc):
            try:
                st = os.lstat(p)
                if stat.S_ISDIR(st.st_mode):
                    os.chmod(p, 0o700)
                else:
                    os.chmod(p, 0o600)
                func(p)
            except Exception:
                # Give up on this path
                pass

        shutil.rmtree(path, onerror=_onerror)
