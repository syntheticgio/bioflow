"""Object lifecycle: ingest, metadata edits, deletion."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from beanie import PydanticObjectId

from app.config import settings
from app.errors import ConflictError, NotFoundError, PayloadTooLargeError, ValidationError
from app.logging import get_logger
from app.models import (
    Blob,
    BlobStorage,
    DataObject,
    IoClass,
    JobClass,
    JobResources,
    ObjectRole,
    ObjectStatus,
    Project,
    RunJobRole,
    SidecarRole,
    SourceInfo,
    SourceMode,
)
from app.services import blob_service, project_service
from app.storage import cas, detect
from app.storage.home import require_home

log = get_logger(__name__)


async def get_object(object_id: PydanticObjectId) -> DataObject:
    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")
    return obj


async def list_objects(
    project_id: PydanticObjectId,
    *,
    limit: int = 200,
    status: ObjectStatus | None = None,
    include_sidecars: bool = False,
) -> list[DataObject]:
    """Objects in a project, newest first.

    Sidecars are excluded by default. Filtering here rather than in the client
    is deliberate: a bwa-mem2 index is five files, so a handful of references
    would otherwise crowd out the files a user actually works with *and* eat
    the result limit before the interesting ones are reached.

    They remain real objects with real verification and GC -- they are hidden
    from this listing, not from the system. `sidecar_of` is how the explorer
    surfaces them on their parent instead.
    """
    query: dict = {"project_id": project_id}
    if status is not None:
        query["status"] = status.value
    if not include_sidecars:
        # Matches both a missing field and an explicit null, so objects that
        # predate sidecars are listed exactly as before.
        query["sidecar_of"] = None
    return await DataObject.find(query).sort("-created_at").limit(limit).to_list()


async def list_sidecars(parent_id: PydanticObjectId) -> list[DataObject]:
    """The scaffolding attached to one object."""
    return await DataObject.find(DataObject.sidecar_of == parent_id).to_list()


async def ingest_stream(
    *,
    project_id: PydanticObjectId,
    filename: str,
    stream,
    max_bytes: int | None = None,
) -> DataObject:
    """Phase 0 upload: stream a request body into the store, hashing as we go.

    Deliberately capped -- resumable chunked upload is Phase 2. A failure here
    loses the whole transfer, which is acceptable for small files and not for
    the multi-GB ones this tool exists to handle.

    The file write runs in a worker thread: writing to a VirtioFS mount can
    block for tens of milliseconds, which on the event loop would stall every
    other request and, in the worker, the heartbeat.
    """
    require_home()
    max_bytes = max_bytes or settings.max_simple_upload_bytes

    project = await Project.get(project_id)
    if project is None:
        raise NotFoundError(f"Project not found: {project_id}")

    name = Path(filename).name.strip()
    if not name or name in (".", ".."):
        raise ValidationError(f"Unsafe filename: {filename!r}")

    obj = DataObject(
        project_id=project_id,
        name=name,
        status=ObjectStatus.UPLOADING,
        source=SourceInfo(mode=SourceMode.UPLOAD, original_name=name),
    )
    await obj.insert()

    try:
        tmp_path, digest, size = await asyncio.to_thread(
            _drain_to_temp, stream, max_bytes, name
        )
    except BaseException:
        await obj.delete()
        raise

    try:
        await obj.set({DataObject.status: ObjectStatus.HASHING, DataObject.size: size})

        existing = await blob_service.find_present_blob(digest)
        if existing is not None and _blob_bytes_present(existing):
            # Identical content already stored; discard the copy we just made.
            tmp_path.unlink(missing_ok=True)
            log.info("upload_deduplicated", digest=digest, name=name)
        else:
            await asyncio.to_thread(cas.place_blob, tmp_path, digest, size)

        await blob_service.attach_blob_to_object(
            object_id=obj.id, digest=digest, size=size, storage=BlobStorage.MANAGED
        )
        await project_service.bump_counters(project_id, objects=1, total_bytes=size)
        await enqueue_ingest(obj, digest=digest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        await obj.set(
            {
                DataObject.status: ObjectStatus.ERROR,
                DataObject.updated_at: datetime.now(UTC),
            }
        )
        raise

    return await get_object(obj.id)


async def ingest_local_file(
    *,
    project_id: PydanticObjectId,
    path: Path,
    name: str,
    role: ObjectRole | None = None,
    derived_from: list[PydanticObjectId] | None = None,
    produced_by_job: PydanticObjectId | None = None,
    facts: dict | None = None,
    metadata: dict | None = None,
    sidecar_of: PydanticObjectId | None = None,
    sidecar_role: SidecarRole | None = None,
) -> DataObject:
    """Take ownership of a file this application produced.

    The third ingest path, and the one a pipeline uses. `ingest_stream` is
    capped at `max_simple_upload_bytes` because a failed transfer loses
    everything; that does not apply to a file already sitting on our own
    filesystem, and trimmed reads routinely exceed the cap. `register_in_place`
    is closer but marks the blob EXTERNAL, which means garbage collection will
    never reclaim it -- wrong for bytes we generated and own.

    `path` is consumed: on success it is renamed into the object store, and on
    dedup it is unlinked. Callers pass a file under `tmp_dir`, which shares a
    filesystem with `objects/` so the placement is an atomic rename rather than
    a copy.
    """
    require_home()

    project = await Project.get(project_id)
    if project is None:
        raise NotFoundError(f"Project not found: {project_id}")

    safe_name = Path(name).name.strip()
    if not safe_name or safe_name in (".", ".."):
        raise ValidationError(f"Unsafe filename: {name!r}")

    if not await asyncio.to_thread(path.exists):
        raise NotFoundError(f"Produced file is missing: {path}")

    obj = DataObject(
        project_id=project_id,
        name=safe_name,
        status=ObjectStatus.HASHING,
        role=role,
        derived_from=derived_from or [],
        produced_by_job=produced_by_job,
        facts=facts or {},
        metadata=metadata or {},
        sidecar_of=sidecar_of,
        sidecar_role=sidecar_role,
        # The bytes originate here, so there is no upload and no external path
        # to record. Mode stays UPLOAD: the object store owns the content, and
        # provenance lives in derived_from rather than in source.
        source=SourceInfo(mode=SourceMode.UPLOAD, original_name=safe_name),
    )
    await obj.insert()

    try:
        digest, size = await asyncio.to_thread(cas.hash_file, path)
        await obj.set({DataObject.size: size})

        existing = await blob_service.find_present_blob(digest)
        if existing is not None and _blob_bytes_present(existing):
            # A trim run that produced byte-identical output to something
            # already stored -- re-running the same job on the same input, most
            # likely. Keep the record, drop the duplicate bytes.
            await _discard(path)
            log.info("produced_file_deduplicated", digest=digest, name=safe_name)
        else:
            await asyncio.to_thread(cas.place_blob, path, digest, size)

        await blob_service.attach_blob_to_object(
            object_id=obj.id, digest=digest, size=size, storage=BlobStorage.MANAGED
        )
        await project_service.bump_counters(project_id, objects=1, total_bytes=size)
        ingest_job_id = await enqueue_ingest(obj, digest=digest)

        # Join the run that produced this file, so its header parse groups with
        # the alignment or trim that caused it rather than appearing loose.
        # `produced_by_job` identifies the causing job; an ordinary upload has
        # none and stays ungrouped, which is correct -- it was not part of a
        # pipeline run.
        if produced_by_job and ingest_job_id:
            from app.services import run_service

            await run_service.link_job_to_run_of(
                cause_job_id=str(produced_by_job),
                job_id=PydanticObjectId(ingest_job_id),
                role=RunJobRole.INGEST,
            )
    except BaseException:
        await _discard(path)
        await obj.set(
            {
                DataObject.status: ObjectStatus.ERROR,
                DataObject.updated_at: datetime.now(UTC),
            }
        )
        raise

    return await get_object(obj.id)


async def _discard(path: Path) -> None:
    """Remove scratch output, off the loop. Never raises: this runs on cleanup
    paths where the original failure is what matters."""
    try:
        await asyncio.to_thread(path.unlink, True)
    except OSError as e:
        log.warning("discard_failed", path=str(path), error=str(e))


def _drain_to_temp(stream, max_bytes: int, name: str) -> tuple[Path, str, int]:
    """Consume a sync iterable of chunks, enforcing the size ceiling as we go.

    The limit is checked during the transfer rather than from Content-Length,
    which a client controls and may omit entirely.
    """
    total = 0

    def limited():
        nonlocal total
        for chunk in stream:
            total += len(chunk)
            if total > max_bytes:
                raise PayloadTooLargeError(
                    f"Upload exceeds the {max_bytes // (1024 * 1024)} MB limit for simple "
                    "uploads. Resumable chunked upload arrives in Phase 2.",
                    details={"filename": name, "max_bytes": max_bytes},
                )
            yield chunk

    return cas.write_stream_to_temp(limited())


def _blob_bytes_present(blob: Blob) -> bool:
    if blob.storage is BlobStorage.EXTERNAL:
        return bool(blob.external_path and Path(blob.external_path).exists())
    from app.storage.paths import blob_path

    return blob_path(blob.id).exists()


async def enqueue_ingest(
    obj: DataObject,
    *,
    digest: str | None = None,
    path: Path | None = None,
    job_class: JobClass = JobClass.USER_BACKGROUND,
) -> str:
    """Queue header parsing for an object.

    Detection and parsing move off the request path: opening a CRAM header or a
    heavily-scaffolded BAM is slow enough to hurt a response, and a malformed
    file must not take the upload down with it.
    """
    from app.queue import queue

    # Current metadata rides along so the handler can honour a manually-entered
    # SRA accession and avoid overwriting anything the user has set.
    payload: dict = {
        "object_id": str(obj.id),
        "name": obj.name,
        "metadata": obj.metadata,
    }
    if path is not None:
        payload["path"] = str(path)
    elif digest is not None:
        payload["sha256"] = digest
    else:
        raise ValidationError("enqueue_ingest requires a digest or a path")

    await obj.set({DataObject.status: ObjectStatus.INGESTING})

    job = await queue.enqueue(
        "ingest_headers",
        payload=payload,
        job_class=job_class,
        resources=JobResources(cpu=1, mem_mb=256, io=IoClass.LIGHT),
        dedup_key=f"ingest_headers:{obj.id}",
        project_id=obj.project_id,
        object_id=obj.id,
    )
    return str(job.id) if job else ""


async def _apply_detection_external(obj: DataObject, path: Path) -> None:
    """Detect format for a file at an arbitrary path (register-in-place)."""
    try:
        result = await asyncio.to_thread(detect.detect, path, obj.name)
    except Exception as e:  # noqa: BLE001 - detection must never fail ingest
        log.warning("detection_failed", object_id=str(obj.id), error=str(e))
        await obj.set({DataObject.status: ObjectStatus.READY})
        return

    await obj.set(
        {
            DataObject.format: result.to_format_info(),
            DataObject.status: ObjectStatus.READY,
            DataObject.updated_at: datetime.now(UTC),
        }
    )


async def _apply_detection(obj: DataObject, digest: str) -> None:
    """Detect the format inline.

    Phase 3 moves this to a queued job; at Phase 0 sizes, reading 64 KiB is
    cheaper than the round trip through the queue.
    """
    from app.storage.paths import blob_path

    path = blob_path(digest)
    try:
        result = await asyncio.to_thread(detect.detect, path, obj.name)
    except Exception as e:  # noqa: BLE001 - detection must never fail an upload
        log.warning("detection_failed", object_id=str(obj.id), error=str(e))
        await obj.set({DataObject.status: ObjectStatus.READY})
        return

    await obj.set(
        {
            DataObject.format: result.to_format_info(),
            DataObject.status: ObjectStatus.READY,
            DataObject.updated_at: datetime.now(UTC),
        }
    )


async def register_in_place(
    *,
    project_id: PydanticObjectId,
    path_str: str,
    name: str | None = None,
) -> tuple[DataObject, str]:
    """Register a file that already exists on disk, without copying it.

    Zero bytes are moved: the object records a pointer to the file where it
    already lives. This is the realistic path for the multi-GB files that are
    already on the drive -- copying them would double the storage for nothing.

    The tradeoff is ownership. We do not control an external file: it can be
    moved, edited, or deleted behind our back. So the blob is marked EXTERNAL,
    GC never unlinks it, and verification watches size and mtime for drift.
    """
    from app.queue import queue
    from app.storage.paths import resolve_registerable

    require_home()

    project = await Project.get(project_id)
    if project is None:
        raise NotFoundError(f"Project not found: {project_id}")

    # Resolves symlinks *before* checking containment, so a link pointing
    # outside the allowlist is rejected rather than followed.
    resolved = resolve_registerable(path_str)

    if not resolved.exists():
        raise ValidationError(f"File does not exist: {resolved}")
    if not resolved.is_file():
        raise ValidationError(f"Not a regular file: {resolved}")

    stat = resolved.stat()

    existing = await Blob.find_one(Blob.external_path == str(resolved))
    if existing is not None:
        raise ConflictError(
            "That file is already registered",
            details={"path": str(resolved), "sha256": existing.id},
        )

    obj = DataObject(
        project_id=project_id,
        name=name or resolved.name,
        status=ObjectStatus.HASHING,
        size=stat.st_size,
        source=SourceInfo(
            mode=SourceMode.REGISTER_IN_PLACE,
            original_path=str(resolved),
            original_name=resolved.name,
        ),
    )
    await obj.insert()

    # Hashing a 100 GB file cannot happen in a request, so it goes to the queue.
    job = await queue.enqueue(
        "register_hash",
        payload={
            "object_id": str(obj.id),
            "path": str(resolved),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        },
        job_class=JobClass.USER_BACKGROUND,
        resources=JobResources(cpu=1, mem_mb=128, io=IoClass.HEAVY),
        dedup_key=f"register_hash:{obj.id}",
        project_id=project_id,
        object_id=obj.id,
    )

    await project_service.bump_counters(project_id, objects=1, total_bytes=stat.st_size)
    log.info("registered_in_place", path=str(resolved), size=stat.st_size)
    return obj, str(job.id) if job else ""


def apply_role_update(obj: DataObject, updates: dict) -> None:
    """Apply a role change, distinguishing an explicit null from an omission.

    Every other field in update_object uses `.get(k) is not None`, which treats
    null and absent alike. Role cannot: clearing it is how a reference is
    converted back to reads, so the *presence of the key* is what matters.

    The same distinction is recorded durably in `user_touched`. Within one
    request the key's presence says the user had an opinion; afterwards only
    that list remembers, and re-ingest needs it to avoid re-asserting a role
    the user removed.
    """
    if "role" not in updates:
        return
    raw = updates["role"]
    obj.role = ObjectRole(raw) if raw is not None else None
    if "role" not in obj.user_touched:
        obj.user_touched = [*obj.user_touched, "role"]


def _is_reads(obj: DataObject) -> bool:
    """Whether a file is something that can have a paired-end mate.

    Deliberately not a format check. The feature exists for files whose
    conventional signals are missing, so restricting to FASTQ would recreate
    the gap it closes -- and trimmed reads pair exactly like raw ones.
    """
    return obj.role is not ObjectRole.REFERENCE and obj.sidecar_of is None


async def set_pair(
    object_id: PydanticObjectId,
    mate_object_id: PydanticObjectId,
    read_number: int,
) -> DataObject:
    """Pair two reads files by hand, symmetrically.

    The mate always receives the opposite read number, so a request cannot
    produce two R1s. Both sides record `"mate"` in user_touched, which is what
    stops filename inference from overriding the choice on a later re-ingest.

    Strict about preconditions: both sides must currently be unpaired. The
    dropdown already filters paired candidates out, so a rejection here means a
    stale tab or a script -- and displacing a third file's pairing silently
    would be worse than an error.
    """
    if object_id == mate_object_id:
        raise ValidationError("A file cannot be paired with itself")

    obj = await get_object(object_id)
    mate = await get_object(mate_object_id)

    if obj.project_id != mate.project_id:
        raise ValidationError("Both files must be in the same project")
    if obj.mate_object_id is not None:
        raise ValidationError(f"{obj.name} is already paired; unpair it first")
    if mate.mate_object_id is not None:
        raise ValidationError(f"{mate.name} is already paired; unpair it first")
    if not _is_reads(obj) or not _is_reads(mate):
        raise ValidationError("Only reads files can be paired")

    # Conditional on the mate still being unpaired, and checked before the
    # subject is touched -- so losing this race leaves nothing half-written.
    # Same shape as _link_mate's double write.
    linked = await DataObject.find_one(
        DataObject.id == mate.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).update(
        {
            "$set": {
                DataObject.mate_object_id: obj.id,
                DataObject.read_number: 3 - read_number,
                DataObject.updated_at: datetime.now(UTC),
            },
            "$addToSet": {"user_touched": "mate"},
        }
    )
    if not getattr(linked, "modified_count", 0):
        raise ValidationError(f"{mate.name} was paired by something else; try again")

    await DataObject.find_one(
        DataObject.id == obj.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).update(
        {
            "$set": {
                DataObject.mate_object_id: mate.id,
                DataObject.read_number: read_number,
                DataObject.updated_at: datetime.now(UTC),
            },
            "$addToSet": {"user_touched": "mate"},
        }
    )

    log.info(
        "pair_set_manually",
        object_id=str(obj.id),
        mate_id=str(mate.id),
        read_number=read_number,
    )
    return await get_object(object_id)


async def update_object(object_id: PydanticObjectId, updates: dict) -> DataObject:
    obj = await get_object(object_id)

    if updates.get("name") is not None:
        new_name = Path(updates["name"]).name.strip()
        if not new_name:
            raise ValidationError("Object name cannot be empty")
        obj.name = new_name
    if updates.get("tags") is not None:
        obj.tags = [t.strip() for t in updates["tags"] if t and t.strip()]
    apply_role_update(obj, updates)
    if updates.get("metadata") is not None:
        from app.metadata import schemas

        # Coerced against the schema for this file's format, so numbers sort as
        # numbers and dates compare correctly. Unknown keys pass through
        # untouched -- the schema suggests, it does not restrict. Role rides
        # along because role is applied above, before metadata: a single PATCH
        # carrying both must validate against the incoming role, not the
        # outgoing one.
        validated = schemas.coerce_and_validate(
            updates["metadata"], obj.format.kind, role=obj.role
        )
        merged = {**obj.metadata, **validated.values}
        # A null means "clear this field", which is how the UI removes a value.
        obj.metadata = {k: v for k, v in merged.items() if v is not None}
        if validated.warnings:
            log.info(
                "metadata_warnings",
                object_id=str(obj.id),
                warnings=validated.warnings,
            )

    obj.touch()
    await obj.save()
    return obj


async def delete_object(object_id: PydanticObjectId) -> None:
    """Delete an object, and any sidecars that exist only to accompany it.

    The cascade is required rather than tidy. Blob GC is refcount-driven, and a
    sidecar's only reason to exist is its parent: nothing else will ever
    reference an orphaned index, so it would sit at refcount 1 forever and
    never be collected.

    Safe precisely because sidecars are scaffolding -- an index is rebuildable
    from the reference, so nothing is lost that cannot be recreated. Derived
    files (`derived_from`) deliberately do *not* cascade: a trimmed FASTQ
    outlives its source, and deleting reads must not silently destroy the
    alignments made from them.
    """
    await get_object(object_id)

    sidecars = await DataObject.find(DataObject.sidecar_of == object_id).to_list()
    for sidecar in sidecars:
        # Sidecars of sidecars are not a shape this produces today (a .bai
        # attaches to a BAM, not to another sidecar), but recursing costs
        # nothing and means a future two-level artifact cannot strand a blob.
        await delete_object(sidecar.id)
        log.info(
            "sidecar_deleted_with_parent",
            object_id=str(sidecar.id),
            parent_id=str(object_id),
            role=sidecar.sidecar_role.value if sidecar.sidecar_role else None,
        )

    await blob_service.detach_blob_from_object(object_id)


async def object_with_blob(object_id: PydanticObjectId) -> tuple[DataObject, Blob | None]:
    obj = await get_object(object_id)
    blob = await Blob.get(obj.blob_sha256) if obj.blob_sha256 else None
    return obj, blob
