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

import asyncio

import pytest

from app.errors import JobCancelled, PermanentError, RetryableError
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


def _spec(fn, *, max_attempts: int = 5) -> HandlerSpec:
    return HandlerSpec(
        name="test_handler",
        fn=fn,
        mode=HandlerMode.ASYNC,
        default_class=JobClass.USER_BACKGROUND,
        default_resources=JobResources(),
        max_attempts=max_attempts,
    )


async def _make_job(
    *,
    job_type: str = "test_handler",
    size: int = 1_000_000,
    attempts: int = 0,
    max_attempts: int = 5,
    payload: dict | None = None,
) -> Job:
    from datetime import UTC, datetime

    full_payload = {"size": size, **(payload or {})}
    job = Job(
        type=job_type,
        state=JobState.RUNNING,
        payload=full_payload,
        owner="local",
        attempts=attempts,
        max_attempts=max_attempts,
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


class TestRetryPath:
    """RetryableError takes one of two branches depending on whether this
    run exhausts `job.max_attempts`: DEAD when it does, FAILED (with a retry
    still scheduled) when it doesn't."""

    async def test_retryable_error_with_attempts_remaining_writes_a_failed_record(
        self, monkeypatch
    ):
        job = await _make_job(
            job_type="exec_timing_retry_failed", attempts=0, max_attempts=5
        )

        async def boom(ctx):
            raise RetryableError("transient", code="test_retryable")

        async def _skip_retry(*args, **kwargs):
            return None

        monkeypatch.setattr(queue, "retry_later", _skip_retry)

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(boom), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.outcome == RunOutcome.FAILED

    async def test_retryable_error_that_exhausts_attempts_writes_a_dead_record(
        self, monkeypatch
    ):
        job = await _make_job(
            job_type="exec_timing_retry_dead", attempts=4, max_attempts=5
        )

        async def boom(ctx):
            raise RetryableError("transient", code="test_retryable")

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(boom), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.outcome == RunOutcome.DEAD


class TestCancellationPaths:
    async def test_job_cancelled_writes_a_cancelled_record(self):
        job = await _make_job(job_type="exec_timing_job_cancelled")

        async def boom(ctx):
            raise JobCancelled("cancelled by user")

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(boom), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.outcome == RunOutcome.CANCELLED

    async def test_asyncio_cancellederror_writes_a_cancelled_record_not_succeeded(self):
        """asyncio.CancelledError is a BaseException, not caught by `except
        JobCancelled` or `except (RetryableError, Exception)`. Before the
        fix, `outcome` stayed at its initial SUCCEEDED value all the way to
        `finally`, so a job killed mid-run (e.g. worker shutdown) would be
        recorded as a fast success -- corrupting the duration/memory models
        with a bad training point. The handler here raises CancelledError
        directly, standing in for cancellation occurring during dispatch;
        the executor must still re-raise it after tagging the outcome, so
        `run()` itself is expected to raise back out to the caller.
        """
        job = await _make_job(job_type="exec_timing_asyncio_cancelled")

        async def boom(ctx):
            raise asyncio.CancelledError()

        executor = JobExecutor("test-worker")
        with pytest.raises(asyncio.CancelledError):
            await executor.run(job, _spec(boom), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.outcome == RunOutcome.CANCELLED


class TestToolAndThreadsCapture:
    """`tool`, `tool_version` and `threads` used to be blank on every row:
    `tool`/`tool_version` were never passed to `timing_service.record()` at
    all, and `threads` read `payload["threads"]`, a key no real launcher
    sets -- every launcher nests it under `payload["params"]["threads"]`.
    Verified against real job documents in the running app before writing
    these.
    """

    async def test_threads_is_read_from_nested_params(self):
        job = await _make_job(
            job_type="exec_timing_threads_nested",
            payload={"params": {"threads": 8}},
        )

        async def ok(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(ok), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.threads == 8

    async def test_threads_falls_back_to_a_flat_key(self):
        job = await _make_job(
            job_type="exec_timing_threads_flat", payload={"threads": 4}
        )

        async def ok(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(ok), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.threads == 4

    async def test_a_null_params_value_does_not_raise(self):
        """A payload carrying `"params": null` is not something to discover
        in the executor's `finally` block."""
        job = await _make_job(
            job_type="exec_timing_threads_null_params", payload={"params": None}
        )

        async def ok(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(ok), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.threads is None

    async def test_tool_key_is_read_from_the_tool_field(self):
        job = await _make_job(
            job_type="exec_timing_tool_field", payload={"tool": "fastp"}
        )

        async def ok(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(ok), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.tool == "fastp"

    async def test_tool_key_falls_back_to_aligner(self):
        job = await _make_job(
            job_type="exec_timing_tool_aligner", payload={"aligner": "star"}
        )

        async def ok(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(ok), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.tool == "star"

    async def test_a_job_naming_no_tool_records_none(self):
        job = await _make_job(job_type="exec_timing_tool_none", payload={})

        async def ok(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(ok), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.tool is None
        assert record.tool_version is None

    async def test_tool_version_comes_from_the_cache_without_probing(
        self, monkeypatch
    ):
        from app.pipelines import tools

        monkeypatch.setattr(
            tools,
            "_seeded",
            {
                "fastp": (
                    "fingerprint",
                    tools.Tool(name="fastp", path="/bin/fastp", version="0.24.0"),
                )
            },
        )

        def _boom_probe(*args, **kwargs):
            raise AssertionError("must not probe from the executor's finally")

        monkeypatch.setattr(tools, "_probe", _boom_probe)

        job = await _make_job(
            job_type="exec_timing_tool_version", payload={"tool": "fastp"}
        )

        async def ok(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(ok), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.tool_version == "0.24.0"

    async def test_tool_version_is_none_on_a_cache_miss(self, monkeypatch):
        from app.pipelines import tools

        monkeypatch.setattr(tools, "_seeded", {})

        def _boom_probe(*args, **kwargs):
            raise AssertionError("must not probe from the executor's finally")

        monkeypatch.setattr(tools, "_probe", _boom_probe)

        job = await _make_job(
            job_type="exec_timing_tool_version_miss", payload={"tool": "fastp"}
        )

        async def ok(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(ok), epoch=0)

        record = await JobRunTiming.find_one(JobRunTiming.job_id == str(job.id))
        assert record is not None
        assert record.tool_version is None
