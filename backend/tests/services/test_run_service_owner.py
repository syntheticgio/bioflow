"""Owner scoping in run_service.

Per docs/superpowers/specs/2026-07-31-profiles-design.md, "Testing" -- a test
that asserts a profile can see its own run passes whether or not the filter was
ever applied, so every assertion here is about what the OTHER profile cannot
see, cannot delete, and cannot write to.

Runs are created through `run_service.create_run` rather than by constructing
`PipelineRun` directly: the writer half is the part that was missing, and a
test that stamped `owner` by hand would go green against a service that never
stamps it at all.

Owner ids are unique per test because the test database is module-scoped and
shared -- a generic "owner-a" would collide with a neighbouring test's rows and
turn an isolation assertion into an ordering one.
"""

import pytest
from beanie import PydanticObjectId

from app.models import (
    Job,
    JobClass,
    JobResources,
    JobState,
    PipelineRun,
    RunJob,
    RunJobRole,
    RunKind,
    RunStatus,
)
from app.services import run_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def _make_run(owner: str, label: str) -> PipelineRun:
    return await run_service.create_run(
        kind=RunKind.ALIGNMENT,
        project_id=PydanticObjectId(),
        label=label,
        inputs=[],
        params={},
        owner=owner,
    )


async def _make_job(state: JobState = JobState.RUNNING) -> Job:
    """A queue row for a run to point at.

    Inserted directly rather than through `queue.enqueue`, which needs a live
    Redis connection this process never opens. The owner is left at
    TimestampedDocument's "local" default on purpose: that is exactly what
    `queue.enqueue` writes today, so a status assertion built on it exercises
    the pre-Task-8 world the service actually runs in.
    """
    job = Job(
        type="align_reads",
        payload={},
        state=state,
        job_class=JobClass.COMPUTE,
        resources=JobResources(),
    )
    await job.insert()
    return job


class TestRunServiceOwnerScoping:
    async def test_create_run_stamps_the_given_owner(self):
        """The writer half. Without it every run inherits the "local" default
        and every filter below matches the wrong set."""
        run = await _make_run("run-stamp-a", "stamp")

        assert run.owner == "run-stamp-a"
        stored = await PipelineRun.get(run.id)
        assert stored is not None
        assert stored.owner == "run-stamp-a"

    async def test_status_for_many_excludes_another_owners_run(self):
        """The multi-run query behind the activity view.

        Both runs are given a job in the same state, so a result that reports
        status for the other owner's run cannot be explained away as the run
        simply having nothing to report.
        """
        mine = await _make_run("run-many-a", "mine")
        theirs = await _make_run("run-many-b", "theirs")
        for run in (mine, theirs):
            job = await _make_job()
            await run_service.link_job(run.id, job.id, RunJobRole.ALIGN)

        statuses = await run_service.status_for_many(
            [mine.id, theirs.id], owner="run-many-a"
        )

        assert theirs.id not in statuses
        assert statuses[mine.id] is RunStatus.RUNNING

    async def test_status_for_returns_nothing_for_another_owners_run(self):
        run = await _make_run("run-one-a", "one")
        job = await _make_job()
        await run_service.link_job(run.id, job.id, RunJobRole.ALIGN)

        status, detail = await run_service.status_for(run.id, owner="run-one-b")

        assert detail == []
        assert status is RunStatus.SUCCEEDED

    async def test_discard_run_refuses_and_destroys_nothing(self):
        """The dangerous one.

        discard_run used to delete the membership rows *before* it fetched the
        run, so a wrong-owner call destroyed another profile's link rows and
        only then discovered it should not have. Asserting the refusal is not
        enough -- the RunJob rows have to still be there afterwards.
        """
        run = await _make_run("run-discard-a", "discard")
        job = await _make_job()
        await run_service.link_job(run.id, job.id, RunJobRole.ALIGN)

        await run_service.discard_run(run.id, owner="run-discard-b")

        assert await PipelineRun.get(run.id) is not None
        assert await RunJob.find(RunJob.run_id == run.id).count() == 1

    async def test_record_outputs_does_not_write_to_another_owners_run(self):
        run = await _make_run("run-out-a", "outputs")
        produced = PydanticObjectId()

        await run_service.record_outputs(run.id, [produced], owner="run-out-b")

        stored = await PipelineRun.get(run.id)
        assert stored is not None
        assert stored.outputs == []
