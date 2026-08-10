"""Manual molecule-type inference: sampling a FASTQ's own bases on request.

Same event-loop constraint as test_object_download.py -- the route is awaited
directly rather than driven through TestClient, because TestClient's blocking
portal runs on a different loop than the Motor connection `beanie_models`
holds, and mixing the two fails with "attached to a different loop".
"""

import pytest
import pytest_asyncio
from beanie import PydanticObjectId

from app.api.v1.objects import infer_molecule_type_endpoint
from app.config import settings
from app.errors import NotFoundError
from app.models import Blob, BlobState, BlobStorage, DataObject
from app.storage.paths import blob_rel_path

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

PROJECT_ID = PydanticObjectId("507f191e810c19729de860ea")
OWNER = "local"

DNA_BYTES = b"@read1\nACGTACGTACGT\n+\nIIIIIIIIIIII\n"
RNA_BYTES = b"@read1\nACGUACGUACGU\n+\nIIIIIIIIIIII\n"
DNA_SHA = "2f3c1c8e5c1d1f2b4a3d0e9f8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c"
RNA_SHA = "3f3c1c8e5c1d1f2b4a3d0e9f8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c"


@pytest_asyncio.fixture(loop_scope="module")
async def objects(tmp_path_factory, monkeypatch):
    """A DNA-like managed object, an RNA-like one, and a pending upload."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(settings, "bioinfo_home", home)

    dna_file = home / "objects" / blob_rel_path(DNA_SHA)
    dna_file.parent.mkdir(parents=True, exist_ok=True)
    dna_file.write_bytes(DNA_BYTES)

    rna_file = home / "objects" / blob_rel_path(RNA_SHA)
    rna_file.parent.mkdir(parents=True, exist_ok=True)
    rna_file.write_bytes(RNA_BYTES)

    await Blob(
        id=DNA_SHA,
        size=len(DNA_BYTES),
        state=BlobState.PRESENT,
        storage=BlobStorage.MANAGED,
        rel_path=blob_rel_path(DNA_SHA),
        ref_count=1,
    ).insert()
    await Blob(
        id=RNA_SHA,
        size=len(RNA_BYTES),
        state=BlobState.PRESENT,
        storage=BlobStorage.MANAGED,
        rel_path=blob_rel_path(RNA_SHA),
        ref_count=1,
    ).insert()

    dna_obj = await DataObject(
        project_id=PROJECT_ID, name="dna_reads.fastq", blob_sha256=DNA_SHA
    ).insert()
    rna_obj = await DataObject(
        project_id=PROJECT_ID, name="rna_reads.fastq", blob_sha256=RNA_SHA
    ).insert()
    pending = await DataObject(
        project_id=PROJECT_ID, name="still_uploading.fastq"
    ).insert()

    yield {"dna": dna_obj.id, "rna": rna_obj.id, "pending": pending.id}

    for doc in (dna_obj, rna_obj, pending):
        await doc.delete()
    for digest in (DNA_SHA, RNA_SHA):
        blob = await Blob.get(digest)
        if blob:
            await blob.delete()


class TestInference:
    async def test_infers_dna_from_a_t_only_fastq(self, objects):
        result = await infer_molecule_type_endpoint(objects["dna"], OWNER)
        assert result.molecule_type == "DNA"
        assert "no U found" in result.basis

    async def test_infers_rna_from_a_u_containing_fastq(self, objects):
        result = await infer_molecule_type_endpoint(objects["rna"], OWNER)
        assert result.molecule_type == "RNA"
        assert "U present" in result.basis

    async def test_does_not_write_to_the_object(self, objects):
        """This endpoint only samples and reports -- persisting the value is
        the caller's job through the normal metadata PATCH."""
        before = (await DataObject.get(objects["dna"])).metadata.copy()
        await infer_molecule_type_endpoint(objects["dna"], OWNER)
        after = await DataObject.get(objects["dna"])
        assert after.metadata == before


class TestUnavailableContent:
    async def test_an_object_with_no_blob_yet_is_a_404(self, objects):
        with pytest.raises(NotFoundError):
            await infer_molecule_type_endpoint(objects["pending"], OWNER)

    async def test_an_unknown_object_is_a_404(self, objects):
        with pytest.raises(NotFoundError):
            await infer_molecule_type_endpoint(
                PydanticObjectId("507f1f77bcf86cd799439011"), OWNER
            )

    async def test_another_profile_cannot_sample_the_bytes(self, objects):
        with pytest.raises(NotFoundError):
            await infer_molecule_type_endpoint(objects["dna"], "someone-else")
