"""Deriving a run's status from its member jobs.

Status is computed on read rather than stored, so this function *is* the
status: there is no second source of truth to fall back on. The cases worth
covering are the ones that are awkward to reproduce against a live queue -- a
failed index build while the alignment is still blocked, a run whose jobs have
been pruned, a run that produced its BAM but failed to parse it.
"""

import pytest
from app.models import JobState, RunJobRole, RunStatus
from app.services.run_service import derive_status

ALIGN = RunJobRole.ALIGN
INDEX = RunJobRole.INDEX
INGEST = RunJobRole.INGEST
INDEX_BAM = RunJobRole.INDEX_BAM


class TestSucceeded:
    def test_all_members_succeeded(self):
        assert derive_status([
            (INDEX, JobState.SUCCEEDED),
            (ALIGN, JobState.SUCCEEDED),
            (INDEX_BAM, JobState.SUCCEEDED),
            (INGEST, JobState.SUCCEEDED),
        ]) is RunStatus.SUCCEEDED

    def test_a_run_with_no_members(self):
        """Every job deduplicated away, or a run created and never linked.
        Nothing failed, so nothing is wrong."""
        assert derive_status([]) is RunStatus.SUCCEEDED


class TestPrunedJobs:
    def test_a_pruned_member_counts_as_succeeded(self):
        """Jobs are TTL-pruned after 30 days while the run record outlives
        them. Treating a missing job as a failure would make an old run
        spontaneously report failure -- a lie about something that worked."""
        assert derive_status([(ALIGN, None), (INGEST, None)]) is RunStatus.SUCCEEDED

    def test_an_old_run_does_not_become_failed(self):
        assert derive_status([
            (INDEX, None), (ALIGN, None), (INDEX_BAM, None),
        ]) is RunStatus.SUCCEEDED

    def test_a_pruned_member_alongside_a_live_failure_still_fails(self):
        assert derive_status([
            (INDEX, None), (ALIGN, JobState.FAILED),
        ]) is RunStatus.FAILED


class TestFailure:
    @pytest.mark.parametrize(
        "state", [JobState.FAILED, JobState.DEAD, JobState.CANCELLED]
    )
    def test_any_unsuccessful_terminal_state_fails_the_run(self, state):
        """Not just FAILED. A cancelled or dead member means the run did not
        do what was asked, whatever the reason."""
        assert derive_status([(ALIGN, state)]) is RunStatus.FAILED

    def test_failure_wins_over_a_still_running_sibling(self):
        """The run is already doomed and saying so beats waiting for a sibling
        whose work is now wasted -- the same reasoning the dependency gate uses
        to fail a dependent as soon as any dependency fails."""
        assert derive_status([
            (INDEX, JobState.FAILED), (ALIGN, JobState.RUNNING),
        ]) is RunStatus.FAILED

    def test_failure_wins_over_a_blocked_sibling(self):
        """The shape a real failed alignment takes: the index build died and
        the alignment behind it never started."""
        assert derive_status([
            (INDEX, JobState.FAILED), (ALIGN, JobState.BLOCKED),
        ]) is RunStatus.FAILED

    def test_a_failed_optional_member_does_not_fail_the_run(self):
        assert derive_status([
            (ALIGN, JobState.SUCCEEDED), (INGEST, JobState.FAILED),
        ]) is not RunStatus.FAILED


class TestPartial:
    def test_a_failed_ingest_yields_partial(self):
        """The BAM was produced and exists; only its header parse failed, which
        is recoverable by re-ingesting. FAILED would overstate it and SUCCEEDED
        would hide it."""
        assert derive_status([
            (ALIGN, JobState.SUCCEEDED),
            (INDEX_BAM, JobState.SUCCEEDED),
            (INGEST, JobState.FAILED),
        ]) is RunStatus.PARTIAL

    def test_partial_requires_everything_else_to_be_finished(self):
        """While a required member is still going, the run is RUNNING -- the
        optional failure is not the headline yet."""
        assert derive_status([
            (ALIGN, JobState.RUNNING), (INGEST, JobState.FAILED),
        ]) is RunStatus.RUNNING

    def test_a_cancelled_ingest_also_yields_partial(self):
        assert derive_status([
            (ALIGN, JobState.SUCCEEDED), (INGEST, JobState.CANCELLED),
        ]) is RunStatus.PARTIAL


class TestInFlight:
    def test_a_running_member_makes_the_run_running(self):
        assert derive_status([
            (INDEX, JobState.SUCCEEDED), (ALIGN, JobState.RUNNING),
        ]) is RunStatus.RUNNING

    def test_running_wins_over_a_queued_sibling(self):
        assert derive_status([
            (ALIGN, JobState.RUNNING), (INGEST, JobState.QUEUED),
        ]) is RunStatus.RUNNING

    @pytest.mark.parametrize(
        "state",
        [JobState.PENDING, JobState.QUEUED, JobState.DELAYED, JobState.BLOCKED],
    )
    def test_nothing_started_is_waiting(self, state):
        assert derive_status([(ALIGN, state)]) is RunStatus.WAITING

    def test_a_blocked_alignment_behind_a_running_build_is_running(self):
        """The common shape right after launching against an unindexed
        reference: the run *is* doing something, just not the alignment yet."""
        assert derive_status([
            (INDEX, JobState.RUNNING), (ALIGN, JobState.BLOCKED),
        ]) is RunStatus.RUNNING

    def test_a_freshly_launched_run_is_waiting(self):
        assert derive_status([
            (INDEX, JobState.QUEUED), (ALIGN, JobState.BLOCKED),
        ]) is RunStatus.WAITING

    def test_a_finished_alignment_with_a_queued_ingest_is_waiting(self):
        """Not SUCCEEDED: the run has outstanding work, even if the
        interesting part is done."""
        assert derive_status([
            (ALIGN, JobState.SUCCEEDED), (INGEST, JobState.QUEUED),
        ]) is RunStatus.WAITING


class TestOptionalRoles:
    def test_only_ingest_is_optional_by_default(self):
        """A failed index build or BAM index is a real failure: the first means
        no alignment happened, the second means the BAM cannot be used by a
        genome browser or variant caller."""
        for role in (INDEX, ALIGN, INDEX_BAM):
            assert derive_status([(role, JobState.FAILED)]) is RunStatus.FAILED

    def test_the_optional_set_is_configurable(self):
        """Kept a parameter so a future run kind can declare its own optional
        steps without editing the precedence rules."""
        assert derive_status(
            [(INDEX_BAM, JobState.FAILED), (ALIGN, JobState.SUCCEEDED)],
            optional_roles=frozenset({INDEX_BAM}),
        ) is RunStatus.PARTIAL
