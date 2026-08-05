"""run_service.runs_for_job: every run a job belongs to, not just the first.

`models/run.py`'s `RunJob` docstring records that a scalar `run_id` field on
`Job` was rejected -- `build_index` is deduplicated by content, so a second
alignment against the same reference reuses the first one's build, and that
one job genuinely belongs to two runs. `run_for_job` (singular, pre-existing)
uses `find_one` and is correct for its own callers, but a progress event that
carries run membership for #18's aggregation must not repeat that shape: a
scalar there would silently drop the second run's membership.
"""

import pytest
from beanie import PydanticObjectId

from app.models import Job, JobClass, JobResources, JobState, RunJobRole, RunKind
from app.services import run_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def _make_run(label: str):
    return await run_service.create_run(
        kind=RunKind.ALIGNMENT,
        project_id=PydanticObjectId(),
        label=label,
        inputs=[],
        params={},
        owner="local",
    )


async def _make_job() -> Job:
    job = Job(
        type="build_index",
        payload={},
        state=JobState.RUNNING,
        job_class=JobClass.COMPUTE,
        resources=JobResources(),
    )
    await job.insert()
    return job


class TestRunsForJob:
    async def test_a_job_shared_by_two_runs_returns_both(self):
        """The case a scalar field gets wrong. Assert the set of ids, not the
        length -- a naive implementation that returns [run_a.id] alone would
        satisfy a bare length check."""
        job = await _make_job()
        run_a = await _make_run("first alignment")
        run_b = await _make_run("second alignment, reusing the index")

        await run_service.link_job(run_a.id, job.id, RunJobRole.INDEX)
        await run_service.link_job(run_b.id, job.id, RunJobRole.INDEX, shared=True)

        result = await run_service.runs_for_job(job.id)

        assert set(result) == {run_a.id, run_b.id}

    async def test_a_job_in_one_run_returns_a_single_element_list(self):
        job = await _make_job()
        run = await _make_run("only alignment")
        await run_service.link_job(run.id, job.id, RunJobRole.INDEX)

        result = await run_service.runs_for_job(job.id)

        assert result == [run.id]

    async def test_a_job_in_no_run_returns_an_empty_list(self):
        job = await _make_job()

        result = await run_service.runs_for_job(job.id)

        assert result == []
