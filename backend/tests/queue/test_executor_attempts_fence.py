"""The retry-path `attempts` write must be epoch-fenced like every other queue
write.

This is the one write on the retry path that the ticket left unfenced:
`retry_later` and `complete` are both guarded by `lease.epoch`, but the
`attempts` counter that sits between them was written with `_id` alone. A
worker whose lease already expired and was re-claimed (a new epoch was granted)
would still land its stale `snapshot + 1` on the live document, overwriting
the current attempt count and throwing off when the job finally goes dead.
"""

import pytest

from app.errors import RetryableError
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
    """Stub the Redis half of the retry path; the Mongo `attempts` write under
    test stays real."""

    async def _skip(*args, **kwargs):
        return None

    monkeypatch.setattr(queue, "release", _skip)
    monkeypatch.setattr(queue, "publish_event", _skip)
    monkeypatch.setattr(queue, "retry_later", _skip)


def _spec(fn) -> HandlerSpec:
    return HandlerSpec(
        name="test_handler",
        fn=fn,
        mode=HandlerMode.ASYNC,
        default_class=JobClass.USER_BACKGROUND,
        default_resources=JobResources(),
        max_attempts=5,
    )


async def _make_job(attempts: int = 0, max_attempts: int = 5) -> Job:
    from datetime import UTC, datetime

    job = Job(
        type="attempts_fence",
        state=JobState.RUNNING,
        payload={"size": 1_000_000},
        owner="local",
        attempts=attempts,
        max_attempts=max_attempts,
    )
    job.timing.enqueued_at = datetime.now(UTC)
    job.timing.started_at = datetime.now(UTC)
    await job.insert()
    return job


def _lease(epoch: int) -> JobLease:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return JobLease(
        worker_id="w-live", expires_at=now, heartbeat_at=now, epoch=epoch
    )


async def _fresh(job_id) -> Job:
    return await Job.get(job_id)


class TestAttemptsFence:
    async def test_live_epoch_writes_the_incremented_count(self):
        """The happy path: a live worker (lease epoch matches) increments the
        counter. Regression guard that the fence does not block the worker that
        still owns the lease."""
        job = await _make_job(attempts=0)
        await job.set({Job.lease: _lease(5)})

        async def boom(ctx):
            raise RetryableError("transient", code="test_retryable")

        await JobExecutor("test-worker").run(job, _spec(boom), epoch=5)

        fresh = await _fresh(job.id)
        assert fresh.attempts == 1

    async def test_stale_epoch_does_not_overwrite_the_live_count(self):
        """The ticket: a worker holding an old epoch must not clobber the live
        attempt count. The zombie's in-memory snapshot is `attempts=0`; since
        it claimed, the lease expired, the job was re-claimed at a new epoch
        and retried to 3. Without the fence the zombie writes `0 + 1 = 1`,
        resetting the counter and letting the job retry past `max_attempts`."""
        job = await _make_job(attempts=0)  # zombie's stale in-memory snapshot
        # The live worker has since re-claimed at epoch 5 and recorded 3 tries.
        await job.set({Job.lease: _lease(5), "attempts": 3})

        async def boom(ctx):
            raise RetryableError("transient", code="test_retryable")

        # Zombie runs with its stale epoch 4; its snapshot is attempts=0.
        await JobExecutor("zombie-worker").run(job, _spec(boom), epoch=4)

        fresh = await _fresh(job.id)
        # The live count survived; the zombie's stale write (0+1=1) was rejected.
        assert fresh.attempts == 3
        assert fresh.lease.epoch == 5

    async def test_dead_branch_is_unaffected_by_the_fence(self):
        """When this run exhausts max_attempts the job goes DEAD via
        `queue.complete`, which fences itself; the unfenced `attempts` write is
        not on that path at all. Guard against the fence being applied in a way
        that breaks the terminal branch."""
        job = await _make_job(attempts=4, max_attempts=5)
        await job.set({Job.lease: _lease(5)})

        async def boom(ctx):
            raise RetryableError("transient", code="test_retryable")

        await JobExecutor("test-worker").run(job, _spec(boom), epoch=5)

        fresh = await _fresh(job.id)
        assert fresh.state is JobState.DEAD
