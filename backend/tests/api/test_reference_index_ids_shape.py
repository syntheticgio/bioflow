"""`index_ids` is a sibling of `indexes`, not a key inside it.

`reference_index_status` returns the download-link map alongside the built
booleans, in the same dict. `list_references` used to nest that whole dict
under "indexes", which put `index_ids` one level deeper than the frontend
(and `ReferenceOption` in alignment.ts) reads it. Two things broke at once:
`entry.index_ids` was undefined, so a reference with any built index blanked
the metadata tab on `entry.index_ids[name]`; and `Object.keys(entry.indexes)`
yielded "index_ids" as though it were an aligner, rendering a junk row.

Routes are awaited directly rather than driven through TestClient -- see
test_remote_object_endpoints.py for why.
"""

import pytest
import pytest_asyncio

from app.api.v1.pipelines import list_references
from app.models import FormatKind, ObjectRole, SidecarRole
from app.pipelines.aligners import INDEX_ROLE, Aligner
from app.services import project_service
from tests.services.helpers import TEST_OWNER, make_object

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def reference_with_bwa_index():
    """A reference carrying one built aligner index and its .fai.

    Both are needed: the crash only reaches `entry.index_ids[name]` when a
    row is actually built, so a bare reference would pass either shape.

    Module-scoped and shared: the project name is unique per owner, so
    building this per test collided once both ran against one database.
    """
    project = await project_service.create_project(
        name="index-ids-shape", owner=TEST_OWNER
    )
    reference = await make_object(project, "genome.fna")
    reference.role = ObjectRole.REFERENCE
    reference.format.kind = FormatKind.FASTA
    await reference.save()

    index = await make_object(
        project,
        "genome.fna.bwt.2bit.64",
        sidecar_of=reference.id,
        sidecar_role=INDEX_ROLE[Aligner.BWA_MEM2],
    )
    fai = await make_object(
        project,
        "genome.fna.fai",
        sidecar_of=reference.id,
        sidecar_role=SidecarRole.FAI,
    )
    return project, reference, index, fai


async def test_index_ids_is_a_sibling_of_indexes(reference_with_bwa_index):
    project, reference, index, fai = reference_with_bwa_index

    payload = await list_references(project.id, TEST_OWNER)
    entry = next(
        r for r in payload["references"] if r["object_id"] == str(reference.id)
    )

    assert entry["index_ids"] == {
        Aligner.BWA_MEM2.value: str(index.id),
        "fai": str(fai.id),
    }


async def test_indexes_holds_only_booleans(reference_with_bwa_index):
    """`Object.keys(entry.indexes)` is how the panel enumerates aligners, so a
    non-boolean key there becomes a rendered row."""
    project, reference, _index, _fai = reference_with_bwa_index

    payload = await list_references(project.id, TEST_OWNER)
    entry = next(
        r for r in payload["references"] if r["object_id"] == str(reference.id)
    )

    assert "index_ids" not in entry["indexes"]
    assert all(isinstance(v, bool) for v in entry["indexes"].values())
    assert entry["indexes"][Aligner.BWA_MEM2.value] is True
    assert entry["indexes"]["fai"] is True
