"""search_objects and list_jobs: the only two tools the project Q&A loop can
call. Scoping to one project and owner lives entirely here -- the JSON
schemas exposed to the model have no project_id/owner property at all, and
the execute_* functions take them as explicit keyword arguments injected by
the caller, never read from the model's parsed arguments dict.
"""

import pytest
from beanie import PydanticObjectId

from app.models import DataObject, Job, JobState, Project
from app.services import project_service
from app.services.ai import qa_tools

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


async def _project(owner: str) -> Project:
    return await project_service.create_project(name="p", owner=owner, parent_id=None)


class TestToolSchemas:
    def test_search_objects_schema_has_no_project_id_or_owner_property(self):
        props = qa_tools.SEARCH_OBJECTS_SPEC.parameters["properties"]
        assert "project_id" not in props
        assert "owner" not in props

    def test_list_jobs_schema_has_no_project_id_or_owner_property(self):
        props = qa_tools.LIST_JOBS_SPEC.parameters["properties"]
        assert "project_id" not in props
        assert "owner" not in props


class TestExecuteSearchObjects:
    async def test_is_scoped_to_the_given_project_and_owner(self):
        owner_a = "qa-tools-owner-a"
        project_a = await _project(owner_a)
        await DataObject(
            project_id=project_a.id, name="a.fastq", owner=owner_a,
            format={"kind": "fastq"},
        ).insert()

        result = await qa_tools.execute_search_objects(
            {}, project_id=project_a.id, owner=owner_a
        )

        names = {o["name"] for o in result["objects"]}
        assert names == {"a.fastq"}

    async def test_a_second_owners_object_in_the_same_project_id_is_not_returned(self):
        """Two different owners can each have a project with the same-shaped
        id space; asserting both directions per CLAUDE.md's scoping warning."""
        owner_a, owner_b = "qa-tools-owner-b1", "qa-tools-owner-b2"
        project_a = await _project(owner_a)
        await DataObject(project_id=project_a.id, name="mine.fastq", owner=owner_a).insert()
        await DataObject(project_id=project_a.id, name="theirs.fastq", owner=owner_b).insert()

        result = await qa_tools.execute_search_objects(
            {}, project_id=project_a.id, owner=owner_a
        )

        names = {o["name"] for o in result["objects"]}
        assert names == {"mine.fastq"}
        assert "theirs.fastq" not in names

    async def test_a_model_supplied_project_id_or_owner_is_ignored(self):
        """Even if a confused model includes these keys in its arguments,
        there is no code path that reads them -- project_id/owner always come
        from the caller's explicit keyword arguments."""
        owner_a = "qa-tools-owner-c"
        project_a = await _project(owner_a)
        await DataObject(project_id=project_a.id, name="real.fastq", owner=owner_a).insert()

        result = await qa_tools.execute_search_objects(
            {"project_id": "000000000000000000000000", "owner": "somebody-else"},
            project_id=project_a.id,
            owner=owner_a,
        )

        assert {o["name"] for o in result["objects"]} == {"real.fastq"}

    async def test_returns_a_trimmed_projection(self):
        owner_a = "qa-tools-owner-d"
        project_a = await _project(owner_a)
        await DataObject(
            project_id=project_a.id, name="x.bam", owner=owner_a, size=100,
            facts={"mean_coverage": 30},
        ).insert()

        result = await qa_tools.execute_search_objects(
            {}, project_id=project_a.id, owner=owner_a
        )

        obj = result["objects"][0]
        assert set(obj.keys()) <= {"id", "name", "kind", "status", "size", "facts"}

    async def test_kinds_filter_is_applied(self):
        owner_a = "qa-tools-owner-e"
        project_a = await _project(owner_a)
        await DataObject(
            project_id=project_a.id, name="a.fastq", owner=owner_a, format={"kind": "fastq"}
        ).insert()
        await DataObject(
            project_id=project_a.id, name="b.bam", owner=owner_a, format={"kind": "bam"}
        ).insert()

        result = await qa_tools.execute_search_objects(
            {"kinds": ["bam"]}, project_id=project_a.id, owner=owner_a
        )

        assert {o["name"] for o in result["objects"]} == {"b.bam"}

    async def test_limit_is_capped_regardless_of_requested_value(self):
        owner_a = "qa-tools-owner-f"
        project_a = await _project(owner_a)
        for i in range(5):
            await DataObject(project_id=project_a.id, name=f"f{i}.fastq", owner=owner_a).insert()

        result = await qa_tools.execute_search_objects(
            {"limit": 10000}, project_id=project_a.id, owner=owner_a
        )

        assert len(result["objects"]) <= qa_tools.MAX_TOOL_RESULT_ROWS


class TestExecuteListJobs:
    async def test_is_scoped_to_the_given_project_and_owner(self):
        owner_a = "qa-tools-jobs-a"
        project_id = PydanticObjectId()
        await Job(type="trim_reads", owner=owner_a, project_id=project_id).insert()

        result = await qa_tools.execute_list_jobs(
            {}, project_id=project_id, owner=owner_a
        )

        assert {j["type"] for j in result["jobs"]} == {"trim_reads"}

    async def test_a_second_owners_job_in_the_same_project_is_not_returned(self):
        owner_a, owner_b = "qa-tools-jobs-b1", "qa-tools-jobs-b2"
        project_id = PydanticObjectId()
        await Job(type="mine_job", owner=owner_a, project_id=project_id).insert()
        await Job(type="theirs_job", owner=owner_b, project_id=project_id).insert()

        result = await qa_tools.execute_list_jobs(
            {}, project_id=project_id, owner=owner_a
        )

        types = {j["type"] for j in result["jobs"]}
        assert types == {"mine_job"}
        assert "theirs_job" not in types

    async def test_job_type_filter_is_applied(self):
        owner_a = "qa-tools-jobs-c"
        project_id = PydanticObjectId()
        await Job(type="trim_reads", owner=owner_a, project_id=project_id).insert()
        await Job(type="align_reads", owner=owner_a, project_id=project_id).insert()

        result = await qa_tools.execute_list_jobs(
            {"job_type": "align_reads"}, project_id=project_id, owner=owner_a
        )

        assert {j["type"] for j in result["jobs"]} == {"align_reads"}

    async def test_state_filter_is_applied(self):
        owner_a = "qa-tools-jobs-d"
        project_id = PydanticObjectId()
        await Job(
            type="a", owner=owner_a, project_id=project_id, state=JobState.RUNNING
        ).insert()
        await Job(
            type="b", owner=owner_a, project_id=project_id, state=JobState.SUCCEEDED
        ).insert()

        result = await qa_tools.execute_list_jobs(
            {"state": "running"}, project_id=project_id, owner=owner_a
        )

        assert {j["type"] for j in result["jobs"]} == {"a"}

    async def test_system_owned_jobs_are_visible_alongside_the_callers_own(self):
        """Matches the union pattern GET /jobs already uses -- maintenance
        jobs belong to no profile but should still be answerable about."""
        from app.queue import keys

        owner_a = "qa-tools-jobs-e"
        project_id = PydanticObjectId()
        await Job(type="verify_files", owner=keys.SYSTEM_OWNER, project_id=project_id).insert()

        result = await qa_tools.execute_list_jobs(
            {}, project_id=project_id, owner=owner_a
        )

        assert {j["type"] for j in result["jobs"]} == {"verify_files"}

    async def test_limit_is_capped(self):
        owner_a = "qa-tools-jobs-f"
        project_id = PydanticObjectId()
        for i in range(5):
            await Job(type=f"job{i}", owner=owner_a, project_id=project_id).insert()

        result = await qa_tools.execute_list_jobs(
            {"limit": 10000}, project_id=project_id, owner=owner_a
        )

        assert len(result["jobs"]) <= qa_tools.MAX_TOOL_RESULT_ROWS

    async def test_returns_a_trimmed_projection(self):
        owner_a = "qa-tools-jobs-g"
        project_id = PydanticObjectId()
        await Job(type="trim_reads", owner=owner_a, project_id=project_id).insert()

        result = await qa_tools.execute_list_jobs({}, project_id=project_id, owner=owner_a)

        job = result["jobs"][0]
        assert set(job.keys()) <= {"type", "state", "progress", "timing", "error"}
