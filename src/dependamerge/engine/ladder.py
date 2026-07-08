# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Recovery ladder: one decision table for every "not mergeable yet" PR.

The legacy orchestration scatters recovery routing across
``_merge_single_pr`` (Steps 0.5/5/5.5/6), ``_handle_merge_conflict``,
``_report_merge_failure`` and the rebase module, each with its own
entry conditions and waits.  The ladder centralises the *decision*:
given a pure :class:`LadderInput` describing a PR's observed state, it
returns the single next :class:`Action` to take.  Executing the action
(API calls, parking) is the pipeline's job — the ladder itself does no
I/O, which makes every rung unit-testable in isolation.

Rungs, in priority order:

1.  ``dirty``      → dependabot: request ``@dependabot rebase`` and
    wait; anyone else: terminal failure (no macro can regenerate a
    conflicted lockfile for a human PR).
2.  ``behind``     → rebase (dependabot macro, else update-branch /
    local rebase) when fixing is enabled; otherwise arm auto-merge
    and wait.
3.  ``blocked`` with a **failing required check** while the branch is
    **behind base** → rebase.  The check verdict is stale: it ran
    against pre-rebase content, and only a rebase can re-trigger it
    against current base.  (This is the rung the legacy code lacks —
    it classifies a completed failing check as unrecoverable and
    reports failure without attempting the rebase that would fix it.)
4.  ``blocked`` with a **stuck** required check (queued/pending far
    beyond normal startup) → dependabot: recreate the PR; a stale
    pre-commit.ci check instead gets a ``pre-commit.ci run``
    re-trigger.  Stuck outranks the pending-wait rung below: waiting
    on a check that will never report cannot succeed.
5.  ``blocked`` with **pending** required checks → arm auto-merge and
    wait for them to land.
6.  ``blocked`` for a reason that cannot self-resolve (missing
    approvals we cannot supply, genuinely failing checks on an
    up-to-date branch) → terminal failure with that reason.
7.  ``unstable`` → merge while the merge button is live
    (``mergeable`` is True — e.g. only a non-required check is red);
    otherwise wait once for mergeability to settle, then fail.
8.  ``clean`` / unknown / "" → attempt the merge and let the merge
    call itself be the arbiter.

Forward progress: every recovery rung is attempted at most once per
item per run (tracked via ``attempted``), so a recovery that does not
change the observed state degrades to the next rung instead of
looping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ActionKind(Enum):
    """What the pipeline should do next for a PR."""

    MERGE = "merge"  # attempt the merge now
    WAIT_CHECKS = "wait_checks"  # arm auto-merge, park until checks land
    REBASE_DEPENDABOT = "rebase_dependabot"  # post @dependabot rebase, park
    REBASE_BRANCH = "rebase_branch"  # update-branch / local rebase, park
    RECREATE = "recreate"  # post @dependabot recreate, park
    RETRIGGER_PRECOMMIT = "retrigger_precommit"  # post pre-commit.ci run, park
    FAIL = "fail"  # terminal: no recovery applies


@dataclass
class Action:
    kind: ActionKind
    reason: str

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Action({self.kind.value}, {self.reason!r})"


# ``attempted`` markers, shared with WorkItem.attempts.
ATTEMPT_REBASE = "rebase"
ATTEMPT_RECREATE = "recreate"
ATTEMPT_PRECOMMIT = "precommit-retrigger"
ATTEMPT_WAIT = "wait-checks"


@dataclass
class LadderInput:
    """Pure, observed facts about one PR.  No I/O objects.

    ``behind_by`` is the commit count the PR branch is behind its base
    (``compare.behind_by``); ``None`` means unknown.  ``block_reason``
    is the phrase from ``analyze_block_reason`` when the PR is
    ``blocked``.  The three check classifications describe the PR's
    *required* checks only and may co-occur (a stuck check is also a
    pending one); ``decide`` resolves overlaps by rung priority.
    """

    mergeable_state: str  # clean/dirty/blocked/behind/unstable/unknown/""
    mergeable: bool | None
    is_dependabot: bool
    behind_by: int | None = None
    block_reason: str | None = None
    has_failing_required_check: bool = False
    has_pending_required_check: bool = False
    stuck_required_check: str | None = None  # check name when stuck
    stale_precommit_check: bool = False
    fix_out_of_date: bool = True
    attempted: set[str] = field(default_factory=set)


def decide(facts: LadderInput) -> Action:
    """Return the next action for a PR in the given observed state."""
    state = facts.mergeable_state

    if state == "dirty":
        return _decide_dirty(facts)

    if state == "behind":
        return _decide_behind(facts)

    if state == "blocked":
        return _decide_blocked(facts)

    if state == "unstable":
        return _decide_unstable(facts)

    # clean, unknown, "" — attempt the merge and let the merge call
    # itself be the arbiter.  This mirrors the legacy Step 6 routing.
    return Action(ActionKind.MERGE, f"state {state or 'unknown'!r}: attempt merge")


def _decide_unstable(facts: LadderInput) -> Action:
    # ``unstable`` merges directly only while the merge button is
    # live (mergeable is True — e.g. a non-required check is red).
    # When mergeability is still computing or reads False, wait once
    # for it to settle rather than firing a premature merge call.
    if facts.mergeable is True:
        return Action(
            ActionKind.MERGE,
            "unstable but mergeable: attempt merge",
        )
    if ATTEMPT_WAIT not in facts.attempted:
        return Action(
            ActionKind.WAIT_CHECKS,
            "unstable and not mergeable: wait for mergeability to settle",
        )
    return Action(ActionKind.FAIL, "unstable and never became mergeable")


def _decide_dirty(facts: LadderInput) -> Action:
    if facts.is_dependabot and ATTEMPT_REBASE not in facts.attempted:
        return Action(
            ActionKind.REBASE_DEPENDABOT,
            "merge conflict: dependabot rebase regenerates the branch",
        )
    return Action(ActionKind.FAIL, "merge conflicts")


def _decide_behind(facts: LadderInput) -> Action:
    if facts.fix_out_of_date and ATTEMPT_REBASE not in facts.attempted:
        if facts.is_dependabot:
            return Action(
                ActionKind.REBASE_DEPENDABOT,
                "behind base: dependabot rebase",
            )
        return Action(ActionKind.REBASE_BRANCH, "behind base: update branch")
    if ATTEMPT_WAIT not in facts.attempted:
        # Not fixing (or already fixed once): arm auto-merge so GitHub
        # completes the merge when the branch catches up.
        return Action(
            ActionKind.WAIT_CHECKS,
            "behind base: defer to auto-merge",
        )
    return Action(ActionKind.FAIL, "behind base and rebase did not converge")


def _decide_blocked(facts: LadderInput) -> Action:
    # Rung 3: stale failing verdict.  A required check that *failed*
    # on a branch that is *behind base* was judged against pre-rebase
    # content (e.g. an org-required workflow audit that the base
    # branch has since fixed).  Only a rebase re-runs it against
    # current base.  Guard: branch demonstrably behind, one attempt.
    if (
        facts.has_failing_required_check
        and (facts.behind_by or 0) > 0
        and ATTEMPT_REBASE not in facts.attempted
    ):
        if facts.is_dependabot:
            return Action(
                ActionKind.REBASE_DEPENDABOT,
                "failing required check on stale branch: rebase to "
                "re-run checks against current base",
            )
        return Action(
            ActionKind.REBASE_BRANCH,
            "failing required check on stale branch: update branch to "
            "re-run checks against current base",
        )

    # Rung 4a: a required check stuck in queued/pending far beyond
    # normal startup.  Recreating is the only reliable recovery for a
    # dependabot PR whose required check will never report.
    if facts.stuck_required_check and ATTEMPT_RECREATE not in facts.attempted:
        if facts.is_dependabot:
            return Action(
                ActionKind.RECREATE,
                f"stuck required check: {facts.stuck_required_check}",
            )
        # Non-dependabot PRs have no recreate macro; fall through to
        # the pending/terminal rungs below.

    # Rung 4b: a hung pre-commit.ci run gets its own re-trigger.
    if facts.stale_precommit_check and ATTEMPT_PRECOMMIT not in facts.attempted:
        return Action(
            ActionKind.RETRIGGER_PRECOMMIT,
            "stale pre-commit.ci check: re-trigger",
        )

    # Rung 5: checks are genuinely running — arm auto-merge and wait.
    if facts.has_pending_required_check and ATTEMPT_WAIT not in facts.attempted:
        return Action(
            ActionKind.WAIT_CHECKS,
            "pending required checks: defer to auto-merge",
        )

    # Rung 6: blocked for a reason no recovery addresses.
    reason = facts.block_reason or "blocked and cannot resolve on its own"
    return Action(ActionKind.FAIL, reason)


__all__ = [
    "Action",
    "ActionKind",
    "LadderInput",
    "decide",
    "ATTEMPT_REBASE",
    "ATTEMPT_RECREATE",
    "ATTEMPT_PRECOMMIT",
    "ATTEMPT_WAIT",
]
