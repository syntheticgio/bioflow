"""The job dependency gate.

A dependent job is held out of Redis entirely until everything it needs has
succeeded, so these cover the decision that releases it. The interesting cases
are all failure-shaped: the design's requirement is that a failed index build
*fails* the alignment behind it rather than leaving it queued forever, which is
the difference between an error the user sees and a job that never runs.
"""

import pytest
from beanie import PydanticObjectId

from app.models import JobState
from app.queue.queue import classify_dependencies


class FakeJob:
    """Enough of a Job for the classification under test.

    A real Job is a Beanie Document and cannot be constructed without an
    initialized collection; `classify_dependencies` only ever reads `.state`,
    so a stand-in keeps these tests free of a database.
    """

    def __init__(self, state: JobState, *, job_type: str = "build_index"):
        self.id = PydanticObjectId()
        self.type = job_type
        self.state = state


def make_job(state: JobState, *, job_type: str = "build_index") -> FakeJob:
    return FakeJob(state, job_type=job_type)


class TestClassifyDependencies:
    def test_a_succeeded_dependency_neither_blocks_nor_fails(self):
        unfinished, failed = classify_dependencies([make_job(JobState.SUCCEEDED)])
        assert unfinished == []
        assert failed == []

    @pytest.mark.parametrize(
        "state",
        [JobState.PENDING, JobState.QUEUED, JobState.DELAYED, JobState.BLOCKED,
         JobState.RUNNING],
    )
    def test_an_active_dependency_blocks(self, state):
        unfinished, failed = classify_dependencies([make_job(state)])
        assert len(unfinished) == 1
        assert failed == []

    @pytest.mark.parametrize(
        "state", [JobState.FAILED, JobState.CANCELLED, JobState.DEAD]
    )
    def test_any_unsuccessful_terminal_state_fails_the_dependent(self, state):
        """Not just FAILED. A cancelled or dead index build is equally never
        going to produce the file the alignment needs, and a dependent left
        queued behind one would wait forever."""
        unfinished, failed = classify_dependencies([make_job(state)])
        assert unfinished == []
        assert len(failed) == 1

    def test_blocked_counts_as_active(self):
        """A chain three deep -- index, align, index_bam -- means a dependency
        can itself be blocked. Treating BLOCKED as terminal would dispatch the
        third step while the first had not started."""
        unfinished, failed = classify_dependencies([make_job(JobState.BLOCKED)])
        assert len(unfinished) == 1
        assert failed == []

    def test_a_missing_dependency_does_not_block(self):
        """Ids with no job behind them are pruned records, not pending work.
        Blocking on one would strand the dependent past the 30-day TTL."""
        unfinished, failed = classify_dependencies([])
        assert unfinished == []
        assert failed == []

    def test_waits_for_the_slowest_of_several(self):
        """Releasing on the first dependency to finish is the bug this guards:
        an alignment needing both an index and a .fai must wait for both."""
        unfinished, failed = classify_dependencies(
            [make_job(JobState.SUCCEEDED), make_job(JobState.RUNNING)]
        )
        assert len(unfinished) == 1
        assert failed == []

    def test_one_failure_among_successes_still_fails(self):
        unfinished, failed = classify_dependencies(
            [make_job(JobState.SUCCEEDED), make_job(JobState.FAILED)]
        )
        assert failed
        assert unfinished == []

    def test_failure_is_reported_even_while_a_sibling_runs(self):
        """The dependent is doomed the moment any dependency fails; making it
        wait for the healthy sibling to finish first would only delay the
        error, and the sibling's work is wasted either way."""
        unfinished, failed = classify_dependencies(
            [make_job(JobState.RUNNING), make_job(JobState.FAILED)]
        )
        assert len(failed) == 1
        assert len(unfinished) == 1


class TestTolerantDependencies:
    """`continue_on_failure` nodes: a dependency whose failure must not
    cascade.

    The workflow case this exists for is a QC node feeding a downstream step.
    QC failing means we lack a report, not that the assembly behind it is
    unusable -- the same judgement `OPTIONAL_ROLES` already encodes for runs.
    """

    def test_a_tolerated_failure_does_not_fail_the_dependent(self):
        dep = make_job(JobState.FAILED)
        unfinished, failed = classify_dependencies(
            [dep], tolerate_failure_of={dep.id}
        )
        assert unfinished == []
        assert failed == []

    def test_an_untolerated_failure_still_fails_the_dependent(self):
        """Tolerance is per-id, not a global switch."""
        tolerated = make_job(JobState.FAILED)
        fatal = make_job(JobState.FAILED)
        unfinished, failed = classify_dependencies(
            [tolerated, fatal], tolerate_failure_of={tolerated.id}
        )
        assert [j.id for j in failed] == [fatal.id]

    def test_a_tolerated_dependency_still_blocks_while_active(self):
        """Tolerating failure is not the same as not waiting. A running QC
        node still has to finish before its dependent starts, or the dependent
        races the file QC is reading."""
        dep = make_job(JobState.RUNNING)
        unfinished, failed = classify_dependencies(
            [dep], tolerate_failure_of={dep.id}
        )
        assert len(unfinished) == 1
        assert failed == []

    def test_default_is_unchanged(self):
        """Omitting the argument must behave exactly as before -- every
        existing caller relies on it."""
        unfinished, failed = classify_dependencies([make_job(JobState.FAILED)])
        assert len(failed) == 1
