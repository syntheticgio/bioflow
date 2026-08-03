"""JobExecutor.run() writes exactly one JobRunTiming record per run, for
every terminal outcome -- not just success.

This is the first test to drive `timing_service.record()` through the real
executor rather than constructing `JobRunTiming` directly (flagged as a gap
in Task 8's review). It exercises the sampler lifecycle (start in `run()`,
cancel and await in `finally`) and the outcome-tagging added in Task 9, and
guards against the double-recording bug: `_record_timing` used to be called
once on the success path inside `try` *and* had to move to a single call in
`finally` for every outcome, so a leftover duplicate would write two records
per successful job.
"""

import pytest

from app.errors import PermanentError
from app.models import Job, JobClass, JobResources, JobState
from app.models.timing import JobRunTiming, RunOutcome
from app.queue import queue
from app.queue.executor import JobExecutor
from app.queue.registry import HandlerMode, HandlerSpec

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """`queue.complete` releases the lease and publishes an event over Redis,
    neither of which this process has. Both are stubbed the same way
    `test_queue_owner.py` stubs `enqueue`'s Redis half -- the Mongo write
    that `_record_timing` actually depends on (`job.timing.started_at` etc.)
    stays real.
    """

    async def _skip(*args, **kwargs):
        return True

    monkeypatch.setattr(queue, "release", _skip)
    monkeypatch.setattr(queue, "publish_event", _skip)


def _spec(fn) -> HandlerSpec:
    return HandlerSpec(
        name="test_handler",
        fn=fn,
        mode=HandlerMode.ASYNC,
        default_class=JobClass.USER_BACKGROUND,
        default_resources=JobResources(),
        max_attempts=5,
    )


async def _make_job(*, job_type: str = "test_handler", size: int = 1_000_000) -> Job:
    from datetime import UTC, datetime

    job = Job(
        type=job_type,
        state=JobState.RUNNING,
        payload={"size": size},
        owner="local",
    )
    job.timing.enqueued_at = datetime.now(UTC)
    job.timing.started_at = datetime.now(UTC)
    await job.insert()
    return job


class TestSuccessPath:
    async def test_a_successful_run_writes_one_succeeded_record(self):
        job = await _make_job(job_type="exec_timing_success")

        async def ok(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(ok), epoch=0)

        records = await JobRunTiming.find(
            JobRunTiming.job_id == str(job.id)
        ).to_list()
        assert len(records) == 1
        record = records[0]
        assert record.outcome == RunOutcome.SUCCEEDED
        assert record.job_type == "exec_timing_success"

    async def test_a_fast_job_leaves_resource_fields_null(self):
        """RESOURCE_FLOOR_MS is 60s; nothing here sleeps that long, so the
        resources block must stay empty rather than reporting a peak drawn
        from a couple of samples."""
        job = await _make_job(job_type="exec_timing_fast")

        async def ok(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(ok), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.resources.peak_rss_bytes is None
        assert record.resources.peak_cpu_percent is None
        assert record.resources.sample_count == 0

    async def test_exactly_one_record_is_written_per_run(self):
        """Guards against the double-recording bug: an old success-path call
        to `_record_timing` left in place alongside the new `finally` call
        would write two rows for one job."""
        job = await _make_job(job_type="exec_timing_once")

        async def ok(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(ok), epoch=0)

        count = await JobRunTiming.find(
            JobRunTiming.job_id == str(job.id)
        ).count()
        assert count == 1


class TestFailurePath:
    async def test_a_permanent_error_writes_a_failed_record(self):
        job = await _make_job(job_type="exec_timing_failed")

        async def boom(ctx):
            raise PermanentError("nope", code="test_error")

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(boom), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.outcome == RunOutcome.FAILED

    async def test_a_permanent_failure_also_writes_exactly_one_record(self):
        job = await _make_job(job_type="exec_timing_failed_once")

        async def boom(ctx):
            raise PermanentError("nope", code="test_error")

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(boom), epoch=0)

        count = await JobRunTiming.find(
            JobRunTiming.job_id == str(job.id)
        ).count()
        assert count == 1
