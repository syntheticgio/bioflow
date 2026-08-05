"""Worker._start_job resolves run membership once, at claim time.

The real production wiring: `_start_job` calls `run_service.runs_for_job`
exactly once and caches the result on `JobContext.run_ids`, which the
throttled progress writer (twice a second per running job) then only ever
reads back, never re-queries. A job shared by two runs is the case that
catches a scalar shortcut -- see models/run.py's `RunJob` docstring for why
`Job` itself has no `run_id` field to fall back on.
"""

import asyncio

import pytest

from app.models import (
    Job,
    JobClass,
    JobLease,
    JobResources,
    JobState,
    RunJobRole,
    RunKind,
)
from app.queue import queue
from app.queue.queue import ClaimedJob
from app.queue.registry import HandlerMode, handler
from app.queue.worker import Worker
from app.services import run_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

_seen_run_ids: list[list[str]] = []


@handler(
    "worker_run_ids_probe",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.USER_BACKGROUND,
)
async def _probe(ctx):
    _seen_run_ids.append(sorted(ctx.run_ids))
    return {}


async def _make_run(label: str):
    from beanie import PydanticObjectId

    return await run_service.create_run(
        kind=RunKind.ALIGNMENT,
        project_id=PydanticObjectId(),
        label=label,
        inputs=[],
        params={},
        owner="local",
    )


async def _make_job(job_type: str) -> Job:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    job = Job(
        type=job_type,
        state=JobState.QUEUED,
        payload={},
        owner="local",
        lease=JobLease(
            worker_id="w", expires_at=now + timedelta(minutes=5), heartbeat_at=now, epoch=0
        ),
    )
    await job.insert()
    return job


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    async def _skip(*args, **kwargs):
        return True

    monkeypatch.setattr(queue, "release", _skip)
    monkeypatch.setattr(queue, "publish_event", _skip)
    _seen_run_ids.clear()


class TestStartJobResolvesRunIds:
    async def test_a_job_shared_by_two_runs_carries_both_on_the_context(self):
        job = await _make_job("worker_run_ids_probe")
        run_a = await _make_run("first alignment")
        run_b = await _make_run("second alignment, reusing the index")
        await run_service.link_job(run_a.id, job.id, RunJobRole.INDEX)
        await run_service.link_job(run_b.id, job.id, RunJobRole.INDEX, shared=True)

        worker = Worker(worker_id="test-worker")
        claimed = ClaimedJob(
            job_id=str(job.id), job_class="user_background", cpu=1, mem_mb=64, io="none", epoch=0
        )

        await worker._start_job(claimed)
        task, ctx, _ = worker._running[str(job.id)]
        await task

        assert set(ctx.run_ids) == {str(run_a.id), str(run_b.id)}
        assert _seen_run_ids == [sorted([str(run_a.id), str(run_b.id)])]

    async def test_a_job_in_no_run_gets_an_empty_list(self):
        job = await _make_job("worker_run_ids_probe")

        worker = Worker(worker_id="test-worker")
        claimed = ClaimedJob(
            job_id=str(job.id), job_class="user_background", cpu=1, mem_mb=64, io="none", epoch=0
        )

        await worker._start_job(claimed)
        task, ctx, _ = worker._running[str(job.id)]
        await task

        assert ctx.run_ids == []
        assert _seen_run_ids == [[]]
