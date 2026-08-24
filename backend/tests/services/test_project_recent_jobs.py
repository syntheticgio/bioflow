"""`recent_jobs` against real Job documents.

This function feeds the agent drawer's spawn-time project context. Its only
existing coverage patched it out (tests/api/test_agent.py), so a field name
that no Job has -- `job_type`, where the model says `type` -- rode to
production and turned every /ask on a project with finished jobs into a 500.
The agent could then never spawn, which is what the drawer reported as being
unable to connect (issue #814).
"""

import pytest

from app.models import Job, Project
from app.models.job import JobProgress, JobState
from app.services import project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def _project(owner: str = "recent-jobs-owner") -> Project:
    return await project_service.create_project(name="recent-jobs", owner=owner)


async def _job(project: Project, *, type_: str, state: JobState) -> Job:
    job = Job(type=type_, project_id=project.id, state=state)
    await job.insert()
    return job


class TestRecentJobs:
    async def test_summarises_finished_jobs_without_touching_absent_fields(self):
        """The regression: this raised AttributeError for every finished job."""
        project = await _project("recent-jobs-summary")
        await _job(project, type_="fastqc", state=JobState.SUCCEEDED)

        recent = await project_service.recent_jobs(project.id)

        assert len(recent) == 1
        assert recent[0]["type"] == "fastqc"
        assert recent[0]["state"] == JobState.SUCCEEDED
        # A percentage, not the whole JobProgress model -- see below.
        assert not isinstance(recent[0]["progress"], JobProgress)

    async def test_includes_failed_jobs_and_excludes_unfinished_ones(self):
        project = await _project("recent-jobs-states")
        await _job(project, type_="done", state=JobState.SUCCEEDED)
        await _job(project, type_="broke", state=JobState.FAILED)
        await _job(project, type_="waiting", state=JobState.PENDING)
        await _job(project, type_="going", state=JobState.RUNNING)

        types = {j["type"] for j in await project_service.recent_jobs(project.id)}

        assert types == {"done", "broke"}

    async def test_excludes_other_projects_jobs(self):
        mine = await _project("recent-jobs-mine")
        theirs = await _project("recent-jobs-theirs")
        await _job(mine, type_="mine", state=JobState.SUCCEEDED)
        await _job(theirs, type_="theirs", state=JobState.SUCCEEDED)

        types = {j["type"] for j in await project_service.recent_jobs(mine.id)}

        assert types == {"mine"}

    async def test_honours_the_limit(self):
        project = await _project("recent-jobs-limit")
        for i in range(4):
            await _job(project, type_=f"job-{i}", state=JobState.SUCCEEDED)

        assert len(await project_service.recent_jobs(project.id, limit=2)) == 2

    async def test_the_summary_is_json_serialisable(self):
        """It is injected into the agent's system prompt as text; a nested
        pydantic model there would blow up at spawn, not here."""
        import json

        project = await _project("recent-jobs-json")
        await _job(project, type_="fastqc", state=JobState.SUCCEEDED)

        json.dumps(await project_service.recent_jobs(project.id))
