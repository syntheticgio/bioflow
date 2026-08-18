"""A run remembers which parameter set configured it (#414)."""

import pytest
from beanie import PydanticObjectId

from app.models.run import AppliedParameterSet, PipelineRun, RunKind

pytestmark = pytest.mark.usefixtures("beanie_models")


class TestAppliedParameterSet:
    def test_runs_default_to_no_set(self):
        run = PipelineRun(
            kind=RunKind.ALIGNMENT, project_id=PydanticObjectId(), label="x"
        )
        assert run.from_parameter_set is None

    def test_snapshot_carries_name_and_revision(self):
        applied = AppliedParameterSet(
            set_id=PydanticObjectId(), name="Nanopore fast",
            revision=3, edited_after_apply=True,
        )
        run = PipelineRun(
            kind=RunKind.ALIGNMENT, project_id=PydanticObjectId(),
            label="x", from_parameter_set=applied,
        )
        assert run.from_parameter_set.name == "Nanopore fast"
        assert run.from_parameter_set.revision == 3
        assert run.from_parameter_set.edited_after_apply is True


class TestCreateRunThreadsProvenance:
    pytestmark = pytest.mark.asyncio(loop_scope="module")

    async def test_create_run_stores_the_snapshot(self):
        from app.services.run_service import create_run

        applied = AppliedParameterSet(
            set_id=PydanticObjectId(), name="Nanopore fast",
            revision=2, edited_after_apply=False,
        )
        run = await create_run(
            kind=RunKind.ALIGNMENT, project_id=PydanticObjectId(), label="x",
            inputs=[], params={"threads": 8}, owner="owner-prov",
            from_parameter_set=applied,
        )
        reloaded = await PipelineRun.get(run.id)
        assert reloaded.from_parameter_set.name == "Nanopore fast"
        assert reloaded.from_parameter_set.revision == 2

    async def test_absent_by_default(self):
        from app.services.run_service import create_run

        run = await create_run(
            kind=RunKind.ALIGNMENT, project_id=PydanticObjectId(), label="x",
            inputs=[], params={}, owner="owner-prov",
        )
        assert (await PipelineRun.get(run.id)).from_parameter_set is None
