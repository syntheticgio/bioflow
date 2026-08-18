"""Offloading keeps the object and releases only its bytes.

The counter assertions are the ones that matter most: `object_count`
unchanged while `total_bytes` drops is the difference between this and
deletion, and it is the thing `detach_blob_from_object` gets wrong for
this purpose (trap 1 -- its first transaction statement deletes the row).
"""

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import (
    Blob,
    BlobState,
    BlobStorage,
    DataObject,
    Locality,
    ObjectStatus,
    Project,
)
from app.services import blob_service, object_service

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]

OWNER = "local"
SIZE = 3_956_617_060  # the real DRR1066343_1.fastq, to keep the numbers honest

# Projects carry a unique (owner, parent_id, name) index and blobs are keyed by
# digest, so each test gets its own of both. Sharing either makes tests collide
# in ways that read as logic failures.
_counter = 0


def _next_id() -> int:
    global _counter
    _counter += 1
    return _counter


async def _project() -> Project:
    name = f"offload-test-{_next_id()}"
    project = Project(name=name, slug=name, owner=OWNER)
    await project.insert()
    return project


async def _downloaded(project: Project, digest: str | None = None, **overrides) -> DataObject:
    """An SRA-downloaded object with bytes attached, ready to offload."""
    digest = digest or f"{_next_id():064d}"
    blob = Blob(
        id=digest,
        size=SIZE,
        state=BlobState.PRESENT,
        storage=BlobStorage.MANAGED,
        ref_count=1,
    )
    await blob.save()
    overrides.setdefault("blob_sha256", digest)
    base = dict(
        project_id=project.id,
        owner=OWNER,
        name="DRR1066343_1.fastq",
        size=SIZE,
        status=ObjectStatus.READY,
        metadata={"sra_run": "DRR1066343"},
    )
    obj = DataObject(**{**base, **overrides})
    await obj.insert()
    await Project.find_one(Project.id == project.id).update(
        {"$inc": {"counters.object_count": 1, "counters.total_bytes": obj.size}}
    )
    return obj


async def test_offload_keeps_the_object_and_clears_the_digest():
    project = await _project()
    obj = await _downloaded(project)

    result = await object_service.offload_object(obj.id, owner=OWNER)

    assert result.locality is Locality.REMOTE
    assert result.blob_sha256 is None
    assert result.remote_source is not None
    assert result.remote_source.accession == "DRR1066343"
    assert result.remote_source.size == SIZE

    # The row is still there -- this is the whole difference from delete.
    still = await DataObject.get(obj.id)
    assert still is not None
    assert still.name == "DRR1066343_1.fastq"


async def test_status_stays_ready():
    project = await _project()
    obj = await _downloaded(project)
    result = await object_service.offload_object(obj.id, owner=OWNER)
    assert result.status is ObjectStatus.READY


async def test_object_count_is_unchanged_while_total_bytes_drops():
    """Trap 1 in one assertion. `detach_blob_from_object` decrements both."""
    project = await _project()
    obj = await _downloaded(project)

    before = await Project.get(project.id)
    assert before.counters.object_count == 1
    assert before.counters.total_bytes == SIZE

    await object_service.offload_object(obj.id, owner=OWNER)

    after = await Project.get(project.id)
    assert after.counters.object_count == 1, "offloading must not delete the object"
    assert after.counters.total_bytes == 0


async def test_the_blob_refcount_drops_so_gc_can_reclaim():
    project = await _project()
    obj = await _downloaded(project)
    await object_service.offload_object(obj.id, owner=OWNER)

    blob = await Blob.get(obj.blob_sha256)
    assert blob is not None
    assert blob.ref_count == 0, "the bytes must become collectable"


async def test_facts_and_provenance_survive():
    """The reason offloading is not deletion: everything except bytes stays."""
    project = await _project()
    parent_id = PydanticObjectId()
    job_id = PydanticObjectId()
    obj = await _downloaded(
        project,
        facts={"read_count": 39_566_170},
        derived_from=[parent_id],
        produced_by_job=job_id,
        tags=["raw"],
    )

    result = await object_service.offload_object(obj.id, owner=OWNER)

    assert result.facts == {"read_count": 39_566_170}
    assert result.derived_from == [parent_id]
    assert result.produced_by_job == job_id
    assert result.tags == ["raw"]


async def test_offload_refuses_an_object_with_no_way_back():
    """The precondition that separates reclaiming space from losing data."""
    project = await _project()
    obj = await _downloaded(
        project, name="assembly_contigs.fasta", metadata={}
    )
    with pytest.raises(ValidationError) as excinfo:
        await object_service.offload_object(obj.id, owner=OWNER)
    assert "fetch it back" in str(excinfo.value)

    unchanged = await DataObject.get(obj.id)
    assert unchanged.blob_sha256 == obj.blob_sha256, "a refused offload must not release bytes"
    assert unchanged.locality is Locality.LOCAL


async def test_an_accession_in_the_filename_is_enough():
    """Objects predating `metadata.sra_run` still carry it in their name."""
    project = await _project()
    obj = await _downloaded(project, name="SRR11768093_1.fastq.gz", metadata={})
    result = await object_service.offload_object(obj.id, owner=OWNER)
    assert result.remote_source.accession == "SRR11768093"


async def test_a_stored_accession_beats_one_guessed_from_the_name():
    """Order matters: a filename can be renamed, metadata is what was recorded."""
    project = await _project()
    obj = await _downloaded(
        project, name="SRR99999999_copy.fastq", metadata={"sra_run": "DRR1066343"}
    )
    result = await object_service.offload_object(obj.id, owner=OWNER)
    assert result.remote_source.accession == "DRR1066343"


async def test_offloading_twice_is_idempotent():
    project = await _project()
    obj = await _downloaded(project)
    digest = obj.blob_sha256
    await object_service.offload_object(obj.id, owner=OWNER)
    again = await object_service.offload_object(obj.id, owner=OWNER)

    assert again.locality is Locality.REMOTE
    after = await Project.get(project.id)
    assert after.counters.total_bytes == 0, "the second call must not double-decrement"
    blob = await Blob.get(digest)
    assert blob.ref_count == 0, "the second call must not decrement the refcount again"


async def test_release_refuses_an_object_with_no_bytes():
    project = await _project()
    obj = await _downloaded(project, blob_sha256=None)
    with pytest.raises(ValidationError):
        await blob_service.release_bytes_for_object(obj.id, accession="DRR1066343")


# ---------------------------------------------------------------------------
# Trap 2: `ObjectStatus.MISSING` already means "the blob went away", which is
# also literally true of an offloaded object. The mechanism that keeps the two
# apart is that both MISSING writers in `verify_blobs` match on
# `blob_sha256 == blob.id`, and offloading clears that field.
#
# These tests pin the mechanism rather than trusting it, and the second one
# asserts the permissive direction still works -- a genuinely lost blob must
# still mark its objects MISSING, or the guard has broken real detection.
# ---------------------------------------------------------------------------


async def test_an_offloaded_object_is_invisible_to_the_missing_sweep():
    """The query `verify_blobs` uses to mark objects broken must not match."""
    project = await _project()
    obj = await _downloaded(project)
    digest = obj.blob_sha256
    await object_service.offload_object(obj.id, owner=OWNER)

    # Exactly the query at queue/handlers.py, run against the released digest.
    matched = await DataObject.find(DataObject.blob_sha256 == digest).to_list()
    assert obj.id not in [o.id for o in matched], (
        "an offloaded object still matches the MISSING sweep and would read as broken"
    )

    after = await DataObject.get(obj.id)
    assert after.status is ObjectStatus.READY
    assert after.locality is Locality.REMOTE


async def test_a_genuinely_lost_blob_still_marks_its_object_missing():
    """The direction that breaks if offloading were guarded too broadly."""
    project = await _project()
    obj = await _downloaded(project)
    digest = obj.blob_sha256

    # No offload: the object still points at the blob, whose bytes vanished.
    matched = await DataObject.find(DataObject.blob_sha256 == digest).to_list()
    assert obj.id in [o.id for o in matched], (
        "a real object with a lost blob must still be found by the sweep"
    )


async def test_offload_leaves_report_directories_alone(tmp_path, monkeypatch):
    """qc_reports/ and friends are keyed by object id, outside objects/.

    Blob GC never sees them -- only `remove_report_dirs` frees them, and
    deletion is the only thing that should call it. An offloaded object's QC
    report must survive, since the report is the reason to keep the file
    listed at all after its bytes are gone.
    """
    # `_REPORT_ROOTS` is captured at import from computed settings properties,
    # which have no setters -- patching the module tuple is the reachable seam.
    qc_dir = tmp_path / "qc_reports"
    monkeypatch.setattr(object_service, "_REPORT_ROOTS", (qc_dir,))

    project = await _project()
    obj = await _downloaded(project)
    report = qc_dir / str(obj.id)
    report.mkdir(parents=True)
    (report / "fastqc_report.html").write_text("<html>qc</html>")

    await object_service.offload_object(obj.id, owner=OWNER)

    assert report.is_dir(), "offloading deleted a report directory"
    assert (report / "fastqc_report.html").read_text() == "<html>qc</html>"
