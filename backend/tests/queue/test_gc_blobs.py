"""`gc_blobs` must never destroy bytes or a record it did not truly claim.

Nothing exercised this handler's body before: the existing `gc_blobs` tests all
assert on scheduler and API plumbing around the *name*, never on the deletion
path, which is how a guard that can never fire survived in it.

The assertions here all land on the direction that fails when the claim is not
real -- a blob re-referenced between the GC query and the delete must keep both
its bytes and its record. Asserting the happy path (an unreferenced blob is
collected) would pass against a GC with no guard at all.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Blob, BlobState, BlobStorage
from app.queue.registry import JobContext

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


def _ctx(**payload) -> JobContext:
    return JobContext(job_id="gc-1", payload=payload, epoch=1, attempts=1, owner="local")


@pytest.fixture(autouse=True)
def _storage_is_healthy(monkeypatch):
    """`gc_blobs` refuses to delete anything while the drive looks questionable.

    Patched on `app.storage.home`, not on the handlers module: `gc_blobs`
    imports `check_home` inside the function body, so the name it resolves is
    the source module's at call time.
    """
    from app.storage import home

    monkeypatch.setattr(
        home, "check_home", lambda: home.HomeStatus(True, "", "/tmp")
    )
    yield


async def _stale_blob(digest: str, *, storage=BlobStorage.MANAGED, ref_count=0) -> Blob:
    """A collectable blob: refcount at zero, older than the GC grace window."""
    old = datetime.now(UTC) - timedelta(hours=4)
    blob = Blob(
        id=digest,
        size=1024,
        state=BlobState.PRESENT,
        storage=storage,
        rel_path=f"{digest[:2]}/{digest}" if storage is BlobStorage.MANAGED else None,
        external_path=None if storage is BlobStorage.MANAGED else f"/tmp/{digest}",
        first_seen_at=old,
        ref_count=ref_count,
    )
    await blob.insert()
    # `updated_at` is what gc_candidates filters on; force it past the grace.
    await Blob.find_one(Blob.id == digest).update({"$set": {Blob.updated_at: old}})
    return await Blob.get(digest)


async def test_a_blob_rereferenced_before_the_claim_keeps_its_bytes(monkeypatch):
    """The core Q-2 failure: the claim check could never fire.

    `find_one(...).update(...)` returns an UpdateResult, never None, so the
    `if claimed is None: continue` guard was dead and a blob re-referenced
    between the candidate query and the claim was unlinked anyway.
    """
    from app.queue import handlers
    from app.services import blob_service

    digest = "a" * 64
    await _stale_blob(digest)

    unlinked: list[str] = []
    monkeypatch.setattr(
        handlers.cas, "unlink_blob", lambda d: unlinked.append(d) or True
    )

    # The race: the blob is handed to the GC as a candidate, then re-referenced
    # (a dedup upload of previously-deleted content) before the claim runs.
    async def _candidates(limit=100):
        got = await Blob.find(Blob.id == digest).to_list()
        await Blob.find_one(Blob.id == digest).update({"$set": {Blob.ref_count: 1}})
        return got

    monkeypatch.setattr(blob_service, "gc_candidates", _candidates)

    await handlers.gc_blobs(_ctx())

    assert digest not in unlinked, "a live blob's bytes were unlinked"
    survivor = await Blob.get(digest)
    assert survivor is not None, "a live blob's record was deleted"
    assert survivor.ref_count == 1


async def test_a_failed_unlink_keeps_the_record(monkeypatch, tmp_path):
    """Bytes that could not be removed must keep the record that points at them.

    `unlink_blob` returns False for a real OSError (EACCES, EIO) as well as for
    an already-absent file. Deleting the record on the former orphans the bytes
    permanently: nothing else knows they exist, so nothing can ever reclaim them.

    The file is created for real and `blob_path` pointed at it, so this asserts
    the "bytes survive" branch deliberately rather than depending on whatever
    happens to exist under the container's objects dir.
    """
    from app.queue import handlers

    digest = "b" * 64
    await _stale_blob(digest)

    present = tmp_path / digest
    present.write_bytes(b"still here")
    monkeypatch.setattr(handlers, "blob_path", lambda d: present)

    # The disk refuses: bytes are still there afterwards.
    monkeypatch.setattr(handlers.cas, "unlink_blob", lambda d: False)

    await handlers.gc_blobs(_ctx())

    survivor = await Blob.get(digest)
    assert survivor is not None, "record dropped while bytes remain"
    # Left collectable so the next sweep retries, not stuck at the tombstone.
    assert survivor.ref_count == 0


async def test_an_already_absent_file_still_drops_the_record(monkeypatch, tmp_path):
    """The other half of a False return: nothing to orphan, so the record goes.

    Without this, keeping the record on every False would leak a row for every
    blob whose bytes were already gone -- and those rows are exactly what the
    GC exists to clear.
    """
    from app.queue import handlers

    digest = "0" * 64
    await _stale_blob(digest)

    monkeypatch.setattr(handlers, "blob_path", lambda d: tmp_path / "definitely-absent")
    monkeypatch.setattr(handlers.cas, "unlink_blob", lambda d: False)

    await handlers.gc_blobs(_ctx())

    assert await Blob.get(digest) is None


async def test_a_genuinely_collectable_blob_is_still_collected(monkeypatch):
    """The guard must not be so strict that it stops collecting anything.

    Asserted on this blob rather than on the run's total: the tests in this
    module share one database, so a global count also sees whatever the others
    left collectable.
    """
    from app.queue import handlers

    digest = "c" * 64
    await _stale_blob(digest)

    unlinked: list[str] = []
    monkeypatch.setattr(
        handlers.cas, "unlink_blob", lambda d: unlinked.append(d) or True
    )

    await handlers.gc_blobs(_ctx())

    assert digest in unlinked
    assert await Blob.get(digest) is None


async def test_external_records_respect_the_grace_window(monkeypatch):
    """A freshly-dereferenced external record is not swept the same second.

    `gc_candidates` filters on `updated_at < cutoff`, but the batch delete of
    external records applied no age filter at all -- so an external blob whose
    refcount dipped to zero moments ago (a detach that is about to be undone,
    or a re-reference mid-flight) lost its record with no grace at all, while
    an identical managed blob got a full hour.
    """
    from app.queue import handlers

    fresh = "d" * 64
    blob = Blob(
        id=fresh,
        size=2048,
        state=BlobState.PRESENT,
        storage=BlobStorage.EXTERNAL,
        external_path=f"/tmp/{fresh}",
        first_seen_at=datetime.now(UTC),
        ref_count=0,
    )
    await blob.insert()

    monkeypatch.setattr(handlers.cas, "unlink_blob", lambda d: True)

    await handlers.gc_blobs(_ctx())

    assert await Blob.get(fresh) is not None, "external record swept inside the grace window"


async def test_a_stale_external_record_is_pruned(monkeypatch):
    """The complement: past the grace window, the external record does go."""
    from app.queue import handlers

    digest = "e" * 64
    await _stale_blob(digest, storage=BlobStorage.EXTERNAL)

    monkeypatch.setattr(handlers.cas, "unlink_blob", lambda d: True)

    await handlers.gc_blobs(_ctx())

    assert await Blob.get(digest) is None


async def test_a_referenced_external_record_is_never_pruned(monkeypatch):
    """An external blob someone still points at keeps its record regardless of age."""
    from app.queue import handlers

    digest = "f" * 64
    await _stale_blob(digest, storage=BlobStorage.EXTERNAL, ref_count=2)

    monkeypatch.setattr(handlers.cas, "unlink_blob", lambda d: True)

    await handlers.gc_blobs(_ctx())

    assert await Blob.get(digest) is not None
