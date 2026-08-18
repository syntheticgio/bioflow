"""Blob ledger operations: refcounting, verification bookkeeping, GC selection.

Refcount changes and the object writes that motivate them must agree, so the
mutating paths run inside a MongoDB transaction. That is the entire reason the
stack requires a replica set.
"""

from datetime import UTC, datetime, timedelta

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.db.client import get_client, get_db
from app.errors import NotFoundError, ValidationError
from app.logging import get_logger
from app.models import (
    Blob,
    BlobState,
    BlobStorage,
    DataObject,
    Locality,
    ObjectStatus,
)
from app.storage.paths import blob_rel_path

log = get_logger(__name__)

# A blob is only unlinked after its refcount has been zero for this long. The
# window closes a real race: object deleted -> refcount 0 -> GC unlinks, while
# an in-flight upload had just deduplicated against that very blob.
GC_GRACE = timedelta(hours=1)


async def attach_blob_to_object(
    *,
    object_id: PydanticObjectId,
    digest: str,
    size: int,
    storage: BlobStorage = BlobStorage.MANAGED,
    external_path: str | None = None,
    observed_mtime: float | None = None,
    status: ObjectStatus = ObjectStatus.INGESTING,
    content_sha256: str | None = None,
) -> Blob:
    """Increment the blob refcount and point the object at it, atomically.

    `content_sha256` is set only on first insert (`$setOnInsert`), never
    updated on an existing blob: the field describes what the stored bytes
    decompress to, which cannot change for a digest that already names one
    immutable set of bytes.
    """
    now = datetime.now(UTC)
    db = get_db()

    # `state` and `miss_count` belong to $set only: MongoDB rejects an update
    # that touches the same path in both $set and $setOnInsert, and $set is the
    # correct home for them anyway since it covers insert *and* update.
    set_on_insert = {
        "size": size,
        "storage": storage.value,
        "first_seen_at": now,
        "owner": "local",
        "created_at": now,
        "schema_version": 1,
        "verify_mode": "stat",
        "content_sha256": content_sha256,
    }
    if storage is BlobStorage.MANAGED:
        set_on_insert["rel_path"] = blob_rel_path(digest)
        set_on_insert["external_path"] = None
    else:
        set_on_insert["rel_path"] = None
        set_on_insert["external_path"] = external_path

    async with get_client().start_session() as session:
        async with await session.start_transaction():
            await db.blobs.update_one(
                {"_id": digest},
                {
                    "$setOnInsert": set_on_insert,
                    "$inc": {"ref_count": 1},
                    "$set": {
                        "updated_at": now,
                        "last_verified_at": now,
                        "observed_size": size,
                        "observed_mtime": observed_mtime,
                        # A blob being written to is by definition present, and
                        # this heals a record previously marked missing.
                        "state": BlobState.PRESENT.value,
                        "miss_count": 0,
                    },
                },
                upsert=True,
                session=session,
            )
            await db.objects.update_one(
                {"_id": object_id},
                {
                    "$set": {
                        "blob_sha256": digest,
                        "size": size,
                        "status": status.value,
                        "updated_at": now,
                    }
                },
                session=session,
            )

    blob = await Blob.get(digest)
    if blob is None:  # pragma: no cover - upsert guarantees existence
        raise RuntimeError(f"Blob {digest} vanished immediately after upsert")

    # A managed placement supersedes a prior external registration: the content
    # is identical by definition, and a managed copy is one we control.
    if storage is BlobStorage.MANAGED and blob.storage is BlobStorage.EXTERNAL:
        await db.blobs.update_one(
            {"_id": digest},
            {
                "$set": {
                    "storage": BlobStorage.MANAGED.value,
                    "rel_path": blob_rel_path(digest),
                    "updated_at": now,
                }
            },
        )
        log.info("blob_upgraded_to_managed", digest=digest)
        blob = await Blob.get(digest)

    return blob  # type: ignore[return-value]


async def attach_existing_blob_to_object(
    *,
    object_id: PydanticObjectId,
    digest: str,
    size: int,
    session=None,
) -> Blob:
    """Point an object at a blob that already exists, and take a reference.

    The share path's counterpart to `attach_blob_to_object`, and separate from
    it on purpose. That function is for callers that just *placed bytes*, so it
    writes `last_verified_at`, `observed_size`, `observed_mtime`, `state` and
    `miss_count` -- verification facts it earned by touching the file. A share
    touches no file and has earned none of them:

    - Writing `observed_mtime=None` destroys the drift baseline an EXTERNAL
      blob is checked against (`queue/handlers.py`), silently reducing drift
      detection to size-only.
    - Writing `last_verified_at=now` pushes the blob to the back of the
      verifier's oldest-first rotation without anything having been verified.
    - Writing `state=PRESENT` would heal a MISSING or QUARANTINED record on
      the strength of a caller that looked at nothing.

    So this touches `ref_count` and `updated_at` and nothing else on the blob.

    The blob must exist and be PRESENT; a share of content we cannot vouch for
    is refused rather than handed over. `session` is accepted so an acceptance
    cascade -- parent, sidecars, mate -- lands as one transaction.
    """
    blob = await Blob.get(digest)
    if blob is None or blob.state is not BlobState.PRESENT:
        raise NotFoundError(
            f"Content is no longer available for sharing (blob {digest[:12]}...)"
        )

    now = datetime.now(UTC)
    db = get_db()

    async def _apply(s):
        await db.blobs.update_one(
            {"_id": digest}, {"$inc": {"ref_count": 1}, "$set": {"updated_at": now}}, session=s
        )
        await db.objects.update_one(
            {"_id": object_id},
            {
                "$set": {
                    "blob_sha256": digest,
                    "size": size,
                    "status": ObjectStatus.READY.value,
                    "updated_at": now,
                }
            },
            session=s,
        )

    if session is not None:
        await _apply(session)
    else:
        async with get_client().start_session() as s:
            async with await s.start_transaction():
                await _apply(s)

    # Built from the blob already fetched above rather than re-read: inside a
    # caller-supplied transaction the write is not yet visible to a fresh read
    # anyway, and outside one this just saves a round-trip.
    blob.ref_count += 1
    blob.updated_at = now
    return blob


async def detach_blob_from_object(object_id: PydanticObjectId) -> None:
    """Delete an object and decrement its blob's refcount, atomically.

    The bytes are not touched here. The GC job unlinks them later, after the
    grace window -- see GC_GRACE.
    """
    obj = await DataObject.get(object_id)
    if obj is None:
        return

    now = datetime.now(UTC)
    db = get_db()
    async with get_client().start_session() as session:
        async with await session.start_transaction():
            await db.objects.delete_one({"_id": object_id}, session=session)
            if obj.blob_sha256:
                await db.blobs.update_one(
                    {"_id": obj.blob_sha256},
                    {"$inc": {"ref_count": -1}, "$set": {"updated_at": now}},
                    session=session,
                )
            if obj.project_id:
                await db.projects.update_one(
                    {"_id": obj.project_id},
                    {
                        "$inc": {
                            "counters.object_count": -1,
                            "counters.total_bytes": -obj.size,
                        },
                        "$set": {"updated_at": now},
                    },
                    session=session,
                )


async def release_bytes_for_object(
    object_id: PydanticObjectId, *, accession: str, component: str | None = None
) -> DataObject:
    """Release an object's bytes while keeping the object itself, atomically.

    The offload half of #523: the file stays listed, badged, and provenance-
    linked; only its content is dropped, to be fetched back on demand.

    Deliberately *not* built on `detach_blob_from_object`, whose docstring
    ("decrement its blob's refcount") describes only half of what it does --
    its first transaction statement is `delete_one`, and it also decrements
    `counters.object_count`. That is delete-the-object-and-release-its-blob,
    the opposite of this. Adding a flag to it was rejected: a boolean that
    flips whether a function deletes a row is a call site nobody can read.

    Everything here happens in one transaction, and the ordering inside it is
    load-bearing. `verify_blobs` marks objects MISSING by matching
    `blob_sha256 == blob.id`; if the refcount dropped in one write and the
    digest were cleared in another, a sweep landing between them would find an
    object still pointing at a blob whose bytes are on their way out and
    label it broken. Clearing the digest in the same transaction is what makes
    an offloaded object invisible to that query -- a mechanism, not luck.

    `status` is untouched, and stays READY. See `Locality` for why: the
    reference picker and the Actions rules filter on READY, the latter at the
    query layer, so an offloaded object with any other status would vanish
    from both with no error.

    The bytes themselves are not unlinked here. Once the refcount reaches zero
    the GC job collects them after `GC_GRACE`, exactly as it does for a
    deleted object.
    """
    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError("Object not found")
    if obj.locality is Locality.REMOTE:
        # Idempotent rather than an error: a double-click on the offload
        # button should not read as a failure, and there is nothing left to do.
        return obj
    if not obj.blob_sha256:
        raise ValidationError(
            f"{obj.name!r} has no stored content to release",
            details={"object_id": str(object_id)},
        )

    now = datetime.now(UTC)
    digest = obj.blob_sha256
    db = get_db()
    async with get_client().start_session() as session:
        async with await session.start_transaction():
            await db.objects.update_one(
                {"_id": object_id},
                {
                    "$set": {
                        "blob_sha256": None,
                        "locality": Locality.REMOTE.value,
                        "remote_source": {
                            "accession": accession,
                            "component": component,
                            "size": obj.size,
                        },
                        "updated_at": now,
                    }
                },
                session=session,
            )
            await db.blobs.update_one(
                {"_id": digest},
                {"$inc": {"ref_count": -1}, "$set": {"updated_at": now}},
                session=session,
            )
            if obj.project_id:
                # `object_count` is deliberately absent: the object is still
                # there. Only the bytes left, so only total_bytes moves.
                await db.projects.update_one(
                    {"_id": obj.project_id},
                    {
                        "$inc": {"counters.total_bytes": -obj.size},
                        "$set": {"updated_at": now},
                    },
                    session=session,
                )

    log.info(
        "object_bytes_released",
        object_id=str(object_id),
        digest=digest,
        accession=accession,
        size=obj.size,
    )
    refreshed = await DataObject.get(object_id)
    assert refreshed is not None
    return refreshed


async def find_present_blob(digest: str) -> Blob | None:
    """Look up a blob eligible for deduplication, by the hash of its stored bytes.

    A record in any state other than PRESENT is treated as a miss: quarantined
    or missing content must be re-placed rather than trusted.
    """
    blob = await Blob.get(digest)
    if blob is None or blob.state is not BlobState.PRESENT:
        return None
    return blob


async def find_present_blob_by_content(content_sha256: str) -> Blob | None:
    """Look up a blob eligible for dedup, by the hash of its *uncompressed* content.

    Separate from `find_present_blob` on purpose: that function looks up the
    primary key (the hash of the bytes actually on disk), which stays correct
    for an uncompressed blob without any change. This one exists because a
    compressible format's dedup key is the plaintext hash instead -- two
    ingests of the same FASTQ must converge on one blob even if one was
    written by bgzip and the other by the stdlib fallback, which produce
    different compressed bytes (and so different `id`s) from identical input.

    Ambiguous only in the window between a race's two placements; the
    PRESENT-state filter and `find_first` (oldest first, by insertion order)
    make that deterministic rather than a source of flaky dedup.
    """
    blob = await Blob.find_one(
        Blob.content_sha256 == content_sha256, Blob.state == BlobState.PRESENT
    )
    return blob


async def gc_candidates(limit: int = 100) -> list[Blob]:
    cutoff = datetime.now(UTC) - GC_GRACE
    return await Blob.find(
        Blob.ref_count <= 0,
        Blob.updated_at < cutoff,
        Blob.storage == BlobStorage.MANAGED,
    ).limit(limit).to_list()


async def create_blob_record(
    digest: str,
    size: int,
    *,
    storage: BlobStorage = BlobStorage.MANAGED,
    external_path: str | None = None,
) -> Blob:
    """Insert a pending blob record, tolerating a concurrent identical insert."""
    now = datetime.now(UTC)
    blob = Blob(
        id=digest,
        size=size,
        state=BlobState.PENDING,
        storage=storage,
        rel_path=blob_rel_path(digest) if storage is BlobStorage.MANAGED else None,
        external_path=external_path,
        first_seen_at=now,
    )
    try:
        await blob.insert()
    except DuplicateKeyError:
        existing = await Blob.get(digest)
        if existing is not None:
            return existing
        raise
    return blob
