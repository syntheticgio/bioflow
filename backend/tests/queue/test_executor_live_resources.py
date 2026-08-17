"""Live resource observations on the running job document.

`ResourceSampler` already polls the job's process subtree once a second and
already tracks running peaks (queue/resource_sampler.py). Before this, every
reading was discarded except the final peaks, written to `job_timings` only
on completion and only for runs over the 60s floor -- nothing reached a
user watching a job in progress.

The naive fix is to merge the sampler's readings into whatever progress tick
a handler happens to produce. That silently does nothing for a phase-only
job: a six-minute Flye run calls `ctx.progress()` a handful of times, so
there is almost nothing to merge into. The sampler loop has to drive a tick
itself. These tests exercise that against the real executor and a real job
document, with a handler that reports no progress of its own -- the case a
merge-only implementation gets wrong.
"""

import asyncio

import pytest

from app.models import Job, JobClass, JobLease, JobResources, JobState
from app.queue import queue
from app.queue.executor import JobExecutor
from app.queue.registry import HandlerMode, HandlerSpec

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    async def _skip(*args, **kwargs):
        return True

    monkeypatch.setattr(queue, "release", _skip)
    monkeypatch.setattr(queue, "publish_event", _skip)


def _spec(fn, *, mode=HandlerMode.ASYNC) -> HandlerSpec:
    return HandlerSpec(
        name="test_handler",
        fn=fn,
        mode=mode,
        default_class=JobClass.USER_BACKGROUND,
        default_resources=JobResources(),
        max_attempts=5,
    )


async def _make_job(job_type: str) -> Job:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    job = Job(
        type=job_type,
        state=JobState.RUNNING,
        payload={"size": 1_000_000},
        owner="local",
        # _write_progress's update is conditional on lease.epoch matching --
        # a job with no lease has no field for that filter to match, and the
        # write silently touches zero documents.
        lease=JobLease(
            worker_id="test-worker",
            expires_at=now + timedelta(minutes=5),
            heartbeat_at=now,
            epoch=0,
        ),
    )
    job.timing.enqueued_at = now
    job.timing.started_at = now
    await job.insert()
    return job


class TestResourcesReachTheJobWithoutHandlerProgressCalls:
    async def test_a_phase_only_job_still_gets_a_live_reading(self, monkeypatch):
        """The regression a merge-only implementation passes and the app
        fails: a handler that never calls ctx.progress() must still end up
        with rss_bytes on the job document, because the sampler tick itself
        is what writes it."""
        from app.queue import executor as executor_module

        monkeypatch.setattr(executor_module, "SAMPLE_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(executor_module, "PROGRESS_INTERVAL_SECONDS", 0.01)

        job = await _make_job(job_type="exec_live_resources_phase_only")

        async def silent(ctx):
            await asyncio.sleep(0.2)
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(silent), epoch=0)
        # _write_progress is scheduled fire-and-forget (loop.create_task) and
        # not awaited by run(); in a long-lived worker process the loop keeps
        # spinning and eventually flushes it, but a test needs to yield once
        # for that queued task to actually execute before reading it back.
        await asyncio.sleep(0.05)

        reloaded = await Job.get(job.id)
        assert reloaded is not None
        assert reloaded.progress.rss_bytes is not None
        assert reloaded.progress.cpu_percent is not None

    async def test_peak_is_retained_alongside_the_current_reading(self, monkeypatch):
        from app.queue import executor as executor_module

        monkeypatch.setattr(executor_module, "SAMPLE_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(executor_module, "PROGRESS_INTERVAL_SECONDS", 0.01)

        job = await _make_job(job_type="exec_live_resources_peak")

        async def silent(ctx):
            await asyncio.sleep(0.2)
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(silent), epoch=0)
        await asyncio.sleep(0.05)

        reloaded = await Job.get(job.id)
        assert reloaded is not None
        assert reloaded.progress.peak_rss_bytes is not None
        assert reloaded.progress.peak_rss_bytes >= reloaded.progress.rss_bytes

    async def test_a_fast_job_below_one_sample_interval_may_have_no_reading(
        self, monkeypatch
    ):
        """Not a floor -- there is simply nothing to report if the job
        finishes before the sampler's first tick. Unlike job_timings'
        RESOURCE_FLOOR_MS, nothing here suppresses a reading that *did*
        happen; a fast job just may not have one."""
        from app.queue import executor as executor_module

        monkeypatch.setattr(executor_module, "SAMPLE_INTERVAL_SECONDS", 60.0)

        job = await _make_job(job_type="exec_live_resources_instant")

        async def instant(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(instant), epoch=0)

        reloaded = await Job.get(job.id)
        assert reloaded is not None
        # No assertion that rss_bytes is None -- only that nothing raised and
        # the job still completed. A slow CI box could still land one sample.
