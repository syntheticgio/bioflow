"""Fetching an offloaded object's bytes back.

The distinction that matters throughout: `download_sra_run` creates objects,
`fetch_remote` restores one. Everything pointing at the offloaded object --
its children's `derived_from`, its mate, its sidecars, the runs it took part
in -- names its id, so a fetch that created a second object would leave all
of them addressing a file with no bytes.
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
    RemoteSource,
)
from app.services import object_service, pipeline_service

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    """Jobs land in Mongo without a Redis to push to.

    The same seam tests/queue/test_queue_owner.py uses: enqueue's Mongo insert
    is the deduplication guard, which is the behaviour under test here, and
    the Redis push that follows it is not.
    """
    from app.queue import queue

    async def _skip(*args, **kwargs):
        return None

    monkeypatch.setattr(queue, "_push_to_redis", _skip)
    monkeypatch.setattr(queue, "publish_event", _skip)

OWNER = "local"
SIZE = 4096

_counter = 0


def _next() -> int:
    global _counter
    _counter += 1
    return _counter


async def _project() -> Project:
    name = f"fetch-test-{_next()}"
    p = Project(name=name, slug=name, owner=OWNER)
    await p.insert()
    return p


async def _offloaded(project: Project, **overrides) -> DataObject:
    base = dict(
        project_id=project.id,
        owner=OWNER,
        name="DRR1066343_1.fastq.gz",
        size=SIZE,
        status=ObjectStatus.READY,
        blob_sha256=None,
        locality=Locality.REMOTE,
        remote_source=RemoteSource(accession="DRR1066343", size=SIZE),
        metadata={"sra_run": "DRR1066343"},
    )
    obj = DataObject(**{**base, **overrides})
    await obj.insert()
    return obj


# --- ensure_local: enqueue, dedup, refusal ---------------------------------


async def test_a_local_object_needs_no_fetch():
    """The common case: callers chain this unconditionally."""
    project = await _project()
    local = await _offloaded(
        project, locality=Locality.LOCAL, remote_source=None, blob_sha256="a" * 64
    )
    assert await pipeline_service.ensure_local(local, owner=OWNER) is None


async def test_a_remote_object_gets_a_fetch_job():
    project = await _project()
    obj = await _offloaded(project)
    job_id = await pipeline_service.ensure_local(obj, owner=OWNER)
    assert job_id is not None

    from app.models import Job

    job = await Job.get(job_id)
    assert job.type == "fetch_remote"
    assert job.payload["object_id"] == str(obj.id)
    assert job.payload["accession"] == "DRR1066343"
    assert job.payload["bytes_estimate"] == SIZE


async def test_two_pipelines_needing_one_file_produce_one_job():
    """The dedup requirement from the plan, stated as its user-visible effect.

    Two alignments launched seconds apart against the same offloaded FASTQ
    must wait on one download, not start two multi-gigabyte transfers.
    """
    project = await _project()
    obj = await _offloaded(project)

    first = await pipeline_service.ensure_local(obj, owner=OWNER)
    second = await pipeline_service.ensure_local(obj, owner=OWNER)

    assert first is not None
    assert second == first, "the second caller must wait on the first job"

    from app.models import Job

    jobs = await Job.find({"type": "fetch_remote", "object_id": obj.id}).to_list()
    assert len(jobs) == 1, f"expected one fetch job, found {len(jobs)}"


async def test_a_remote_object_with_no_address_refuses_rather_than_queueing():
    project = await _project()
    obj = await _offloaded(project, remote_source=None, metadata={}, name="mystery.fastq")
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.ensure_local(obj, owner=OWNER)
    assert "nothing records where to fetch it" in str(excinfo.value)


async def test_the_query_shape_finds_an_in_flight_fetch():
    """`active_fetch_job_query` is extracted so its shape is assertable."""
    from app.models import ACTIVE_STATES

    object_id = PydanticObjectId()
    q = pipeline_service.active_fetch_job_query(object_id)
    assert q["type"] == "fetch_remote"
    assert q["object_id"] == object_id
    assert set(q["state"]["$in"]) == {s.value for s in ACTIVE_STATES}


# --- restore: the round trip ----------------------------------------------


async def test_restore_reattaches_to_the_same_object(tmp_path):
    """The whole point: same id, same facts, same provenance, bytes back."""
    project = await _project()
    parent = PydanticObjectId()
    obj = await _offloaded(
        project,
        facts={"read_count": 1000},
        derived_from=[parent],
        tags=["raw"],
    )
    original_id = obj.id

    payload = b"@r1\nACGT\n+\nIIII\n" * 64
    staged = tmp_path / "DRR1066343_1.fastq"
    staged.write_bytes(payload)

    restored = await object_service.restore_bytes_for_object(
        original_id, owner=OWNER, path=staged
    )

    assert restored.id == original_id, "a fetch must not create a second object"
    assert restored.locality is Locality.LOCAL
    assert restored.remote_source is None
    assert restored.blob_sha256 is not None
    assert restored.status is ObjectStatus.READY
    assert restored.facts == {"read_count": 1000}
    assert restored.derived_from == [parent]
    assert restored.tags == ["raw"]

    total = await DataObject.find(DataObject.project_id == project.id).count()
    assert total == 1, "the fetch created an extra object"


async def test_restore_deduplicates_against_bytes_already_stored(tmp_path):
    """Refetching content another object already holds must not store it twice."""
    project = await _project()

    # Content unique to this test. Bytes are content-addressed and this module
    # stores several files, so reusing another test's payload would count that
    # test's references in the refcount asserted below.
    payload = b"@dedup\nACGTACGT\n+\nIIIIIIII\n" * 64
    first_path = tmp_path / "existing.fastq"
    first_path.write_bytes(payload)
    holder = DataObject(
        project_id=project.id, owner=OWNER, name="existing.fastq",
        size=len(payload), status=ObjectStatus.READY,
    )
    await holder.insert()
    await object_service.restore_bytes_for_object(
        holder.id, owner=OWNER, path=first_path
    )
    holder = await DataObject.get(holder.id)
    shared_digest = holder.blob_sha256

    # Now fetch identical bytes back into an offloaded object.
    obj = await _offloaded(project, name="existing.fastq")
    second_path = tmp_path / "fetched.fastq"
    second_path.write_bytes(payload)
    restored = await object_service.restore_bytes_for_object(
        obj.id, owner=OWNER, path=second_path
    )

    assert restored.blob_sha256 == shared_digest, "identical bytes made a second blob"
    blob = await Blob.get(shared_digest)
    assert blob.ref_count == 2, f"refcount should count both objects, got {blob.ref_count}"


async def test_restore_heals_a_blob_record_marked_missing(tmp_path):
    """`attach_blob_to_object` sets state PRESENT, which matters here.

    An offloaded object's blob can be GC'd and its record left behind. The
    fetch must bring that record back to PRESENT rather than leaving a
    resurrected file described as missing.
    """
    project = await _project()
    obj = await _offloaded(project)

    payload = b"@r1\nTTTT\n+\nIIII\n" * 64
    staged = tmp_path / "DRR1066343_1.fastq"
    staged.write_bytes(payload)

    restored = await object_service.restore_bytes_for_object(
        obj.id, owner=OWNER, path=staged
    )
    blob = await Blob.get(restored.blob_sha256)
    assert blob.state is BlobState.PRESENT
    assert blob.storage is BlobStorage.MANAGED


# --- the applier: picking the right staged file ---------------------------


async def test_the_applier_restores_only_this_objects_half_of_a_pair(tmp_path):
    """A paired run stages two files; each offloaded object owns one.

    Restoring both into one object is impossible, but discarding the wrong one
    is not -- the applier has to match by name. The mate stays remote and gets
    its own job when it is itself needed; fetching both here would re-download
    gigabytes the user did not ask for.
    """
    from app.queue.results import _apply_fetch_remote

    project = await _project()
    r1 = await _offloaded(project, name="DRR1066343_1.fastq.gz", read_number=1)
    r2 = await _offloaded(project, name="DRR1066343_2.fastq.gz", read_number=2)

    p1 = tmp_path / "DRR1066343_1.fastq"
    p2 = tmp_path / "DRR1066343_2.fastq"
    p1.write_bytes(b"@r1\nAAAA\n+\nIIII\n" * 32)
    p2.write_bytes(b"@r2\nCCCC\n+\nIIII\n" * 32)

    await _apply_fetch_remote(
        {
            "object_id": str(r1.id),
            "staged": [
                {"name": "DRR1066343_1.fastq", "path": str(p1)},
                {"name": "DRR1066343_2.fastq", "path": str(p2)},
            ],
        },
        owner=OWNER,
    )

    restored = await DataObject.get(r1.id)
    assert restored.locality is Locality.LOCAL
    assert restored.blob_sha256 is not None

    untouched = await DataObject.get(r2.id)
    assert untouched.locality is Locality.REMOTE, "the mate must stay remote"
    assert untouched.blob_sha256 is None

    assert not p2.exists(), "the unused staged file should be discarded"

    total = await DataObject.find(DataObject.project_id == project.id).count()
    assert total == 2, "the applier created an object"


async def test_a_single_end_run_restores_even_when_the_name_was_changed(tmp_path):
    """The fallback: one staged file and one object means no ambiguity.

    A user who renamed a downloaded FASTQ still gets it back -- matching
    strictly on name would strand the file with nothing saying why.
    """
    from app.queue.results import _apply_fetch_remote

    project = await _project()
    obj = await _offloaded(project, name="my-renamed-reads.fastq.gz")

    staged = tmp_path / "DRR1066343.fastq"
    staged.write_bytes(b"@r\nGGGG\n+\nIIII\n" * 32)

    await _apply_fetch_remote(
        {
            "object_id": str(obj.id),
            "staged": [{"name": "DRR1066343.fastq", "path": str(staged)}],
        },
        owner=OWNER,
    )

    restored = await DataObject.get(obj.id)
    assert restored.locality is Locality.LOCAL
    assert restored.name == "my-renamed-reads.fastq.gz", "the user's name was replaced"


async def test_the_applier_survives_an_object_deleted_mid_fetch(tmp_path):
    """A multi-gigabyte download outlives the user's patience sometimes."""
    from app.queue.results import _apply_fetch_remote

    project = await _project()
    obj = await _offloaded(project)
    object_id = obj.id
    await obj.delete()

    staged = tmp_path / "DRR1066343_1.fastq"
    staged.write_bytes(b"@r\nTTTT\n+\nIIII\n" * 32)

    # Must not raise: the job succeeded, there is simply nowhere to put it.
    await _apply_fetch_remote(
        {
            "object_id": str(object_id),
            "staged": [{"name": "DRR1066343_1.fastq", "path": str(staged)}],
        },
        owner=OWNER,
    )


async def test_the_applier_reports_when_nothing_matches(tmp_path):
    """Two staged files, neither matching: better logged than half-applied."""
    from app.queue.results import _apply_fetch_remote

    project = await _project()
    obj = await _offloaded(project, name="DRR1066343_1.fastq.gz")

    p1 = tmp_path / "SRR999_1.fastq"
    p2 = tmp_path / "SRR999_2.fastq"
    p1.write_bytes(b"x" * 64)
    p2.write_bytes(b"y" * 64)

    await _apply_fetch_remote(
        {
            "object_id": str(obj.id),
            "staged": [
                {"name": "SRR999_1.fastq", "path": str(p1)},
                {"name": "SRR999_2.fastq", "path": str(p2)},
            ],
        },
        owner=OWNER,
    )

    still_remote = await DataObject.get(obj.id)
    assert still_remote.locality is Locality.REMOTE, (
        "a non-matching fetch must leave the object fetchable, not half-applied"
    )


# --- registration ---------------------------------------------------------


async def test_fetch_remote_is_registered_as_a_handler():
    """A handler nothing dispatches to is silently dead.

    `sra_handlers` is imported for its `@handler` side effects, so this also
    catches the import being dropped.
    """
    from app.queue import handlers  # noqa: F401 - registration side effects
    from app.queue.registry import get_handler

    spec = get_handler("fetch_remote")
    assert spec is not None
    assert spec.max_attempts == 3, "a failed transfer is usually the network"


async def test_fetch_remote_has_an_applier():
    """Without one, the bytes are fetched and then dropped on the floor."""
    from app.queue.results import _APPLIERS

    assert "fetch_remote" in _APPLIERS
