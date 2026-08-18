"""from_parameter_set, proven over the actual HTTP read path.

Two existing tests bracket this boundary without crossing it:
`tests/api/test_parameter_set_provenance_wiring.py` proves the launch routes
pass `from_parameter_set` into `run_service.create_run`, and
`tests/models/test_parameter_set_provenance.py` proves `PipelineRun` round-trips
the field through Mongo. Neither one ever asks what `GET /api/v1/runs/{id}`
actually puts in its JSON body -- which is exactly where `RunOut` silently
dropped the field (it declared no `from_parameter_set` field and `of()` never
passed it through), even though every layer around it was correct and tested.
"""

import pytest
from beanie import PydanticObjectId

from app.models import RunKind
from app.models.run import AppliedParameterSet
from app.services import run_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


class TestRunDetailCarriesFromParameterSet:
    async def test_get_run_returns_the_applied_parameter_set(self, client, two_profiles):
        applied = AppliedParameterSet(
            set_id=PydanticObjectId(),
            name="Nanopore fast",
            revision=3,
            edited_after_apply=True,
        )
        run = await run_service.create_run(
            kind=RunKind.ALIGNMENT,
            project_id=PydanticObjectId(),
            label="a-preset-run",
            inputs=[],
            params={},
            owner=two_profiles["a"].owner_id(),
            from_parameter_set=applied,
        )

        resp = await client.get(
            f"/api/v1/runs/{run.id}", headers=two_profiles["a_headers"]
        )

        assert resp.status_code == 200
        body = resp.json()["from_parameter_set"]
        assert body is not None
        assert body["set_id"] == str(applied.set_id)
        assert body["name"] == applied.name
        assert body["revision"] == applied.revision
        assert body["edited_after_apply"] == applied.edited_after_apply

    async def test_get_run_returns_null_when_no_set_was_applied(
        self, client, two_profiles
    ):
        run = await run_service.create_run(
            kind=RunKind.ALIGNMENT,
            project_id=PydanticObjectId(),
            label="a-hand-configured-run",
            inputs=[],
            params={},
            owner=two_profiles["a"].owner_id(),
        )

        resp = await client.get(
            f"/api/v1/runs/{run.id}", headers=two_profiles["a_headers"]
        )

        assert resp.status_code == 200
        assert "from_parameter_set" in resp.json()
        assert resp.json()["from_parameter_set"] is None
