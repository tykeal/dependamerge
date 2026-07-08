# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the recovery ladder decision table."""

from __future__ import annotations

from dependamerge.engine.ladder import (
    ATTEMPT_PRECOMMIT,
    ATTEMPT_REBASE,
    ATTEMPT_RECREATE,
    ATTEMPT_WAIT,
    ActionKind,
    LadderInput,
    decide,
)


def _facts(**overrides) -> LadderInput:
    base: dict = {
        "mergeable_state": "clean",
        "mergeable": True,
        "is_dependabot": True,
    }
    base.update(overrides)
    return LadderInput(**base)


class TestMergeableStates:
    def test_clean_merges(self):
        action = decide(_facts(mergeable_state="clean"))
        assert action.kind is ActionKind.MERGE

    def test_unknown_state_attempts_merge(self):
        action = decide(_facts(mergeable_state="unknown", mergeable=None))
        assert action.kind is ActionKind.MERGE

    def test_empty_state_attempts_merge(self):
        action = decide(_facts(mergeable_state="", mergeable=None))
        assert action.kind is ActionKind.MERGE

    def test_unstable_mergeable_attempts_merge(self):
        # ``unstable`` = only a non-required check is red; the merge
        # button is live and the merge call is the arbiter.
        action = decide(_facts(mergeable_state="unstable", mergeable=True))
        assert action.kind is ActionKind.MERGE

    def test_unstable_not_mergeable_waits_once(self):
        # Mergeability still computing (None) or reading False: wait
        # for it to settle instead of firing a premature merge call.
        for mergeable in (None, False):
            action = decide(_facts(mergeable_state="unstable", mergeable=mergeable))
            assert action.kind is ActionKind.WAIT_CHECKS

    def test_unstable_not_mergeable_after_wait_fails(self):
        action = decide(
            _facts(
                mergeable_state="unstable",
                mergeable=False,
                attempted={ATTEMPT_WAIT},
            )
        )
        assert action.kind is ActionKind.FAIL


class TestDirty:
    def test_dependabot_conflict_rebases(self):
        action = decide(_facts(mergeable_state="dirty"))
        assert action.kind is ActionKind.REBASE_DEPENDABOT

    def test_human_conflict_fails(self):
        action = decide(_facts(mergeable_state="dirty", is_dependabot=False))
        assert action.kind is ActionKind.FAIL
        assert action.reason == "merge conflicts"

    def test_dependabot_conflict_after_rebase_attempt_fails(self):
        action = decide(_facts(mergeable_state="dirty", attempted={ATTEMPT_REBASE}))
        assert action.kind is ActionKind.FAIL


class TestBehind:
    def test_dependabot_behind_uses_macro(self):
        action = decide(_facts(mergeable_state="behind"))
        assert action.kind is ActionKind.REBASE_DEPENDABOT

    def test_non_dependabot_behind_updates_branch(self):
        action = decide(_facts(mergeable_state="behind", is_dependabot=False))
        assert action.kind is ActionKind.REBASE_BRANCH

    def test_behind_without_fix_waits(self):
        action = decide(_facts(mergeable_state="behind", fix_out_of_date=False))
        assert action.kind is ActionKind.WAIT_CHECKS

    def test_behind_after_rebase_attempt_waits(self):
        action = decide(_facts(mergeable_state="behind", attempted={ATTEMPT_REBASE}))
        assert action.kind is ActionKind.WAIT_CHECKS

    def test_behind_after_rebase_and_wait_fails(self):
        action = decide(
            _facts(
                mergeable_state="behind",
                attempted={ATTEMPT_REBASE, ATTEMPT_WAIT},
            )
        )
        assert action.kind is ActionKind.FAIL


class TestBlockedStaleFailingCheck:
    """Rung 3: the rung the legacy orchestration lacks.

    A required check that completed with a failure on a branch that is
    behind base was judged against stale content; a rebase re-runs it
    against current base.  This is exactly the org-required workflow
    audit scenario: a batch of automation PRs branched before the
    audit fix merged all fail the audit until rebased.
    """

    def test_dependabot_failing_check_behind_rebases(self):
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                has_failing_required_check=True,
                behind_by=6,
            )
        )
        assert action.kind is ActionKind.REBASE_DEPENDABOT
        assert "stale" in action.reason

    def test_non_dependabot_failing_check_behind_updates_branch(self):
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                is_dependabot=False,
                has_failing_required_check=True,
                behind_by=1,
            )
        )
        assert action.kind is ActionKind.REBASE_BRANCH

    def test_failing_check_up_to_date_fails(self):
        # Branch is current: the failure is genuine; a rebase cannot
        # change the verdict, so no rebase is attempted.
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                has_failing_required_check=True,
                behind_by=0,
                block_reason="Blocked by failing check: Audit",
            )
        )
        assert action.kind is ActionKind.FAIL
        assert action.reason == "Blocked by failing check: Audit"

    def test_failing_check_unknown_behind_fails(self):
        # Unknown distance from base: fail closed rather than rebase
        # a possibly-current branch.
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                has_failing_required_check=True,
                behind_by=None,
            )
        )
        assert action.kind is ActionKind.FAIL

    def test_failing_check_behind_after_rebase_fails(self):
        # The rebase already happened and the check still fails: the
        # failure is real on current base.
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                has_failing_required_check=True,
                behind_by=2,
                attempted={ATTEMPT_REBASE},
                block_reason="Blocked by failing check: Audit",
            )
        )
        assert action.kind is ActionKind.FAIL


class TestBlockedOtherRungs:
    def test_pending_checks_wait(self):
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                has_pending_required_check=True,
            )
        )
        assert action.kind is ActionKind.WAIT_CHECKS

    def test_pending_checks_after_wait_fails(self):
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                has_pending_required_check=True,
                attempted={ATTEMPT_WAIT},
                block_reason="Blocked by pending required check: build",
            )
        )
        assert action.kind is ActionKind.FAIL

    def test_stuck_check_dependabot_recreates(self):
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                stuck_required_check="DCO",
            )
        )
        assert action.kind is ActionKind.RECREATE
        assert "DCO" in action.reason

    def test_stuck_check_non_dependabot_falls_through(self):
        # No recreate macro for human PRs; with nothing else pending
        # the item fails with the block reason.
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                is_dependabot=False,
                stuck_required_check="DCO",
                block_reason="Blocked by pending required check: DCO",
            )
        )
        assert action.kind is ActionKind.FAIL

    def test_stuck_check_after_recreate_falls_through(self):
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                stuck_required_check="DCO",
                attempted={ATTEMPT_RECREATE},
            )
        )
        assert action.kind is ActionKind.FAIL

    def test_stale_precommit_retriggers(self):
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                stale_precommit_check=True,
            )
        )
        assert action.kind is ActionKind.RETRIGGER_PRECOMMIT

    def test_stale_precommit_after_retrigger_fails(self):
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                stale_precommit_check=True,
                attempted={ATTEMPT_PRECOMMIT},
            )
        )
        assert action.kind is ActionKind.FAIL

    def test_blocked_no_recovery_fails_with_reason(self):
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                block_reason="Blocked by missing required reviews",
            )
        )
        assert action.kind is ActionKind.FAIL
        assert action.reason == "Blocked by missing required reviews"

    def test_blocked_no_reason_fails_with_default(self):
        action = decide(_facts(mergeable_state="blocked", mergeable=False))
        assert action.kind is ActionKind.FAIL
        assert "cannot resolve" in action.reason


class TestRungPriority:
    def test_stale_failing_check_outranks_stuck_and_precommit(self):
        # A behind branch with a failing check is rebased even when a
        # stuck check / stale pre-commit.ci is also present: the
        # rebase re-triggers everything at once.
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                has_failing_required_check=True,
                behind_by=3,
                stuck_required_check="DCO",
                stale_precommit_check=True,
            )
        )
        assert action.kind is ActionKind.REBASE_DEPENDABOT

    def test_stuck_check_outranks_pending_wait(self):
        action = decide(
            _facts(
                mergeable_state="blocked",
                mergeable=False,
                stuck_required_check="DCO",
                has_pending_required_check=True,
            )
        )
        assert action.kind is ActionKind.RECREATE
