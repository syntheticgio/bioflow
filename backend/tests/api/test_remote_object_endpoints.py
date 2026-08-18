"""Byte-reading endpoints refuse an offloaded object; viewing it still works.

Routes are awaited directly rather than driven through TestClient, for the
reason test_infer_molecule_type_endpoint.py records: TestClient's blocking
portal runs on a different loop than the Motor connection `beanie_models`
holds, and mixing them fails with "attached to a different loop".
"""

import pytest
from beanie import PydanticObjectId

from app.api.v1.objects import (
    download_object,
    get_object,
    infer_molecule_type_endpoint,
    offload_object_endpoint,
    reingest_object,
)
from app.errors import ValidationError
from app.models import (
    DataObject,
    FormatInfo,
    FormatKind,
    Locality,
    ObjectStatus,
    RemoteSource,
)

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

OWNER = "local"


async def _offloaded() -> DataObject:
    obj = DataObject(
        project_id=PydanticObjectId(),
        owner=OWNER,
        name="ERR17407954_1.fastq.gz",
        size=98765,
        status=ObjectStatus.READY,
        blob_sha256=None,
        locality=Locality.REMOTE,
        remote_source=RemoteSource(accession="ERR17407954", size=98765),
        metadata={"sra_run": "ERR17407954"},
        format=FormatInfo(kind=FormatKind.FASTQ),
    )
    await obj.insert()
    return obj


async def test_download_refuses_and_names_the_fetch():
    obj = await _offloaded()
    with pytest.raises(ValidationError) as excinfo:
        await download_object(obj.id, OWNER)
    assert "ERR17407954" in str(excinfo.value)
    assert "no stored content" not in str(excinfo.value)


async def test_reingest_refuses():
    obj = await _offloaded()
    with pytest.raises(ValidationError) as excinfo:
        await reingest_object(obj.id, OWNER)
    assert "re-ingest" in str(excinfo.value)


async def test_molecule_type_inference_refuses():
    """Refuses on locality, not on format: this object *is* a FASTQ.

    The format guard runs first in this route, so a non-FASTQ would refuse
    for a different reason. Using a FASTQ proves the locality check is what
    fired.
    """
    obj = await _offloaded()
    with pytest.raises(ValidationError) as excinfo:
        await infer_molecule_type_endpoint(obj.id, OWNER)
    assert "sample" in str(excinfo.value)


async def test_viewing_an_offloaded_object_still_works():
    """The permissive direction, and the reason `check_local` is not inside
    `object_with_blob`: an offloaded file must stay visible in the explorer,
    or the user cannot find the thing they need to fetch back.
    """
    obj = await _offloaded()
    detail = await get_object(obj.id, OWNER)
    assert detail.name == "ERR17407954_1.fastq.gz"
    # The response schema serializes status to a plain string.
    assert detail.status == ObjectStatus.READY.value


async def test_the_offload_endpoint_flips_an_object_remote():
    """The round trip a user actually performs: offload, then see it listed."""
    from app.models import Blob, BlobState, BlobStorage

    digest = "d" * 64
    await Blob(
        id=digest,
        size=4096,
        state=BlobState.PRESENT,
        storage=BlobStorage.MANAGED,
        ref_count=1,
    ).save()
    obj = DataObject(
        project_id=PydanticObjectId(),
        owner=OWNER,
        name="DRR1066343_2.fastq",
        size=4096,
        status=ObjectStatus.READY,
        blob_sha256=digest,
        metadata={"sra_run": "DRR1066343"},
        format=FormatInfo(kind=FormatKind.FASTQ),
    )
    await obj.insert()

    out = await offload_object_endpoint(obj.id, OWNER)
    assert out.locality == Locality.REMOTE.value
    assert out.status == ObjectStatus.READY.value

    # And it is still fetchable through the detail route afterwards.
    detail = await get_object(obj.id, OWNER)
    assert detail.name == "DRR1066343_2.fastq"


async def test_the_offload_endpoint_refuses_an_unfetchable_object():
    obj = DataObject(
        project_id=PydanticObjectId(),
        owner=OWNER,
        name="local_assembly.fasta",
        size=10,
        status=ObjectStatus.READY,
        blob_sha256="e" * 64,
        metadata={},
    )
    await obj.insert()
    with pytest.raises(ValidationError):
        await offload_object_endpoint(obj.id, OWNER)


async def test_the_reference_picker_reports_locality():
    """The align dialog warns about a download before starting one.

    Without this field the picker still lists an offloaded reference (that is
    stage 3's guarantee) but the dialog cannot say that choosing it means
    waiting on gigabytes of transfer.
    """
    from app.api.v1.pipelines import list_references
    from app.models import FormatInfo, FormatKind, Project

    project = Project(name="picker-locality", slug="picker-locality", owner=OWNER)
    await project.insert()

    remote_ref = DataObject(
        project_id=project.id,
        owner=OWNER,
        name="GCF_000146045.2_genomic.fna",
        size=12_000_000,
        status=ObjectStatus.READY,
        blob_sha256=None,
        locality=Locality.REMOTE,
        remote_source=RemoteSource(accession="GCF_000146045.2", size=12_000_000),
        metadata={"sra_run": "GCF_000146045.2"},
        format=FormatInfo(kind=FormatKind.FASTA),
    )
    await remote_ref.insert()

    out = await list_references(project.id, OWNER)
    entry = next(r for r in out["references"] if r["object_id"] == str(remote_ref.id))
    assert entry["locality"] == "remote"
    assert entry["size"] == 12_000_000
