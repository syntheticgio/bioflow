"""Guard tests for GenBank sequence extraction (#348, GS-17..GS-20).

The guard reads the world rather than a stored flag: it asks whether a
REFERENCE object derived from this GenBank exists. These tests pin the two
consequences that fall out of that choice -- a deleted reference makes the
action available again, and a renamed one does not.
"""

import pytest
import pytest_asyncio
from app.models import DataObject, ObjectRole, ObjectStatus
from app.services import pipeline_service
from beanie import PydanticObjectId

from tests.services import helpers

# `beanie_models` is module-scoped and holds a Motor connection bound to that
# scope's loop, so the tests (and the fixtures that touch the database) have
# to run on the same one -- see tests/services/test_annotation_inputs.py for
# the same note.
pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(loop_scope="module")
async def project():
    return await helpers.make_project(f"gb-proj-{PydanticObjectId()}")


async def _object(project, *, name, role, derived_from=None):
    """Insert a minimal DataObject.

    No Blob: `existing_extracted_sequence` is a pure query over the objects
    collection and never resolves content, so blob plumbing would be noise.
    """
    obj = DataObject(
        project_id=project.id,
        owner=project.owner,
        name=name,
        size=100,
        status=ObjectStatus.READY,
        role=role,
        derived_from=derived_from or [],
    )
    await obj.insert()
    return obj


class TestExistingExtraction:
    async def test_none_when_nothing_derived(self, project):
        gb = await _object(project, name="x.gbff", role=ObjectRole.ANNOTATION)
        assert await pipeline_service.existing_extracted_sequence(gb.id) is None

    async def test_finds_a_derived_reference(self, project):
        gb = await _object(project, name="x.gbff", role=ObjectRole.ANNOTATION)
        ref = await _object(
            project, name="x.fna", role=ObjectRole.REFERENCE, derived_from=[gb.id]
        )
        found = await pipeline_service.existing_extracted_sequence(gb.id)
        assert found is not None and found.id == ref.id

    async def test_a_rename_does_not_hide_it(self, project):
        """GS-20: the query keys on derived_from and role, never on name."""
        gb = await _object(project, name="x.gbff", role=ObjectRole.ANNOTATION)
        ref = await _object(
            project,
            name="renamed-by-user.fna",
            role=ObjectRole.REFERENCE,
            derived_from=[gb.id],
        )
        found = await pipeline_service.existing_extracted_sequence(gb.id)
        assert found is not None and found.id == ref.id

    async def test_ignores_a_derived_object_of_another_role(self, project):
        """An exported annotation subset is also derived_from the GenBank."""
        gb = await _object(project, name="x.gbff", role=ObjectRole.ANNOTATION)
        await _object(
            project,
            name="subset.gff",
            role=ObjectRole.ANNOTATION,
            derived_from=[gb.id],
        )
        assert await pipeline_service.existing_extracted_sequence(gb.id) is None
