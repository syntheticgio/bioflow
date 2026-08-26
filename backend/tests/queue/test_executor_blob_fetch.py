"""The compute node's input-blob fetch must not run on the event loop.

`executor.run` fetches a job's input blobs from the primary before dispatching
the handler. That fetch is a synchronous urllib download of arbitrarily large
files (blob_transfer.py), and it ran directly on the loop. A multi-GB fetch
would therefore freeze heartbeats, the cancel watcher, and the lease renewals
that feed the reaper for the whole transfer -- so every *other* running job's
lease expired and those jobs were reaped and re-run while still executing.
This is the double-run the worker's watchdog exists to prevent, and the
watchdog was frozen too.

The fix is to run the fetch in a worker thread. These tests pin that: the
resolve call must happen off the loop thread on a compute node, and not at
all on the primary. The thread-id check is the whole point -- a test that
only asserts the job succeeds passes both before and after the fix.
"""

import threading

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


class TestBlobFetchOffTheEventLoop:
    async def test_a_compute_node_resolves_blobs_in_a_worker_thread(
        self, monkeypatch
    ):
        """The whole bug: the fetch used to run on the loop. The thread id
        must differ from the loop thread's, which is only true of a
        to_thread/pool execution."""
        from app.config import settings
        from app.queue import executor as executor_module

        monkeypatch.setattr(
            type(settings), "is_compute_node", property(lambda self: True)
        )

        seen = {"ran": False, "thread": None}

        def _resolve(payload):
            seen["ran"] = True
            seen["thread"] = threading.get_ident()

        monkeypatch.setattr(executor_module, "_resolve_input_blobs", _resolve)

        job = await _make_job(job_type="exec_blob_fetch_thread")

        async def handler(ctx):
            return {}

        loop_thread = threading.get_ident()
        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(handler), epoch=0)

        assert seen["ran"], "compute node must resolve input blobs before running"
        assert (
            seen["thread"] != loop_thread
        ), "input-blob fetch ran on the event loop thread -- it would freeze the loop"

    async def test_the_primary_does_not_fetch_blobs(self, monkeypatch):
        """No-op on the primary: nothing to fetch, nothing that must be
        threaded."""
        from app.config import settings
        from app.queue import executor as executor_module

        monkeypatch.setattr(
            type(settings), "is_compute_node", property(lambda self: False)
        )

        calls: list[dict] = []
        monkeypatch.setattr(
            executor_module,
            "_resolve_input_blobs",
            lambda payload: calls.append(payload),
        )

        job = await _make_job(job_type="exec_blob_fetch_primary")

        async def handler(ctx):
            return {}

        executor = JobExecutor("test-worker")
        await executor.run(job, _spec(handler), epoch=0)

        assert calls == []
