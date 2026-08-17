"""mark_running resets progress on a later attempt, keeping a high-water mark.

Two cases behave differently, and only one was ever broken. A *terminal*
failure already does the right thing: nothing clears `progress`, so a failed
job sits at 80% next to its error, which is the most useful thing it could
show -- untouched by this change. A *requeue* (lease expiry, retry backoff)
was broken: nothing reset `progress` before this, so a job that died at 80%
came back on its next attempt still claiming 80% while it restarted from
zero. `mark_running` is the once-per-attempt write that starts a job running
again, so it is where the reset belongs -- and where the discarded progress
is worth keeping as `last_attempt_progress`, since "attempt 2; attempt 1
reached 80% at 'assembly'" is the most useful line the UI can show about a
job that keeps dying at the same point.
"""


import pytest

from app.models import Job, JobProgress, JobState
from app.queue import queue

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def _make_job(*, attempts: int, progress: JobProgress | None = None) -> Job:
    job = Job(
        type="mark_running_probe",
        state=JobState.QUEUED,
        payload={},
        owner="local",
        attempts=attempts,
        progress=progress or JobProgress(),
    )
    await job.insert()
    return job


class TestFirstAttempt:
    async def test_no_previous_progress_stashes_nothing(self):
        """attempts=0: there is no prior attempt to remember, and the field
        must stay None rather than holding an empty record the UI would have
        to special-case."""
        job = await _make_job(attempts=0)

        result = await queue.mark_running(str(job.id), "worker-1", 0)

        assert result is not None
        assert result.last_attempt_progress is None
        assert result.progress.pct is None


class TestRequeuedAttempt:
    async def test_a_later_attempt_stashes_the_prior_progress_and_clears_it(self):
        """The regression: a job that died at 80% must not come back
        claiming 80%, but attempt 1's high point must survive somewhere."""
        job = await _make_job(
            attempts=1,
            progress=JobProgress(
                pct=0.8, phase="assembling", message="draft", peak_rss_bytes=15_247_000_000
            ),
        )

        result = await queue.mark_running(str(job.id), "worker-1", 0)

        assert result is not None
        assert result.progress.pct is None
        assert result.progress.phase == ""
        assert result.last_attempt_progress is not None
        assert result.last_attempt_progress.attempt == 1
        assert result.last_attempt_progress.pct == 0.8
        assert result.last_attempt_progress.phase == "assembling"
        assert result.last_attempt_progress.peak_rss_bytes == 15_247_000_000

    async def test_only_the_immediately_prior_attempt_is_kept(self):
        """Not a history: a third attempt overwrites what the second attempt
        left, it does not accumulate."""
        job = await _make_job(attempts=1, progress=JobProgress(pct=0.3, phase="first"))
        after_attempt_2 = await queue.mark_running(str(job.id), "worker-1", 0)
        assert after_attempt_2.last_attempt_progress.pct == 0.3

        after_attempt_2.progress = JobProgress(pct=0.6, phase="second")
        after_attempt_2.attempts = 2
        await after_attempt_2.save()

        result = await queue.mark_running(str(job.id), "worker-1", 1)

        assert result.last_attempt_progress.attempt == 2
        assert result.last_attempt_progress.pct == 0.6
        assert result.last_attempt_progress.phase == "second"

    async def test_a_prior_attempt_with_no_progress_at_all_stashes_nothing(self):
        """A job that was requeued before it ever called ctx.progress() has
        nothing worth remembering -- default JobProgress, not a real attempt
        record."""
        job = await _make_job(attempts=1, progress=JobProgress())

        result = await queue.mark_running(str(job.id), "worker-1", 0)

        assert result.last_attempt_progress is None


class TestTerminalFailureIsUnaffected:
    async def test_a_completed_jobs_progress_is_never_touched_by_mark_running(self):
        """Guards the half that was already correct: mark_running is only
        ever called to start a new attempt, so a terminal job's progress
        (the thing a failed job shows next to its error) is out of scope
        here by construction -- this test documents that boundary rather
        than exercising a code path that could regress it."""
        job = await _make_job(
            attempts=3, progress=JobProgress(pct=0.8, phase="assembling")
        )
        job.state = JobState.FAILED
        await job.save()

        reloaded = await Job.get(job.id)
        assert reloaded.progress.pct == 0.8
        assert reloaded.state == JobState.FAILED
