"""Object lifecycle: ingest, metadata edits, deletion."""

import asyncio
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from beanie import PydanticObjectId

from app.config import settings
from app.errors import ConflictError, NotFoundError, PayloadTooLargeError, ValidationError
from app.logging import get_logger
from app.metadata import sra as sra_metadata
from app.models import (
    Blob,
    BlobStorage,
    DataObject,
    IoClass,
    JobClass,
    JobResources,
    Locality,
    ObjectRole,
    ObjectStatus,
    RunJobRole,
    SidecarRole,
    SourceInfo,
    SourceMode,
)
from app.services import blob_service, project_service
from app.storage import cas, compress, detect
from app.storage.home import require_home

log = get_logger(__name__)

# Report roots shared by remove, copy, and reap. Every function that iterates
# per-object report directories must use this tuple so a new root cannot be
# added to some call sites and silently skipped by others.
_REPORT_ROOTS = (
    settings.qc_reports_dir,
    settings.bam_stats_dir,
    settings.vcf_stats_dir,
    settings.annotation_stats_dir,
)


async def get_object(object_id: PydanticObjectId, *, owner: str) -> DataObject:
    """Fetch an object, scoped to its owner.

    A wrong-owner lookup raises the same NotFoundError as a missing one, on
    purpose -- same reasoning as project_service.get_project: it keeps every
    existing caller's error handling working unchanged, and it does not confirm
    to one profile that another profile's id exists.

    This is also the choke point the mutating helpers below resolve through, so
    scoping it here scopes update, delete and pairing at the same time.
    """
    obj = await DataObject.get(object_id)
    if obj is None or obj.owner != owner:
        raise NotFoundError(f"Object not found: {object_id}")
    return obj


async def list_objects(
    project_id: PydanticObjectId,
    *,
    owner: str,
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
    query: dict = {"owner": owner, "project_id": project_id}
    if status is not None:
        query["status"] = status.value
    if not include_sidecars:
        # Matches both a missing field and an explicit null, so objects that
        # predate sidecars are listed exactly as before.
        query["sidecar_of"] = None
    return await DataObject.find(query).sort("-created_at").limit(limit).to_list()


async def get_objects_by_ids(
    project_id: PydanticObjectId,
    object_ids: list[PydanticObjectId],
    *,
    owner: str,
) -> dict[str, DataObject]:
    """Fetch specific objects within a project, keyed by str(id).

    Scoped by project_id and owner so the caller inherits the same ownership
    boundary list_objects enforces. Ids that do not exist, are not in this
    project, or are not owned by `owner` are simply absent from the result --
    the caller decides whether that is an error.
    """
    objects = await DataObject.find(
        {
            "_id": {"$in": object_ids},
            "project_id": project_id,
            "owner": owner,
        }
    ).to_list()
    return {str(o.id): o for o in objects}


async def list_sidecars(parent_id: PydanticObjectId, *, owner: str) -> list[DataObject]:
    """The scaffolding attached to one object."""
    return await DataObject.find(
        DataObject.sidecar_of == parent_id, DataObject.owner == owner
    ).to_list()


async def ingest_stream(
    *,
    owner: str,
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

    # Resolved through the owner-scoped lookup rather than Project.get, which
    # would let one profile ingest into another's project. Raises NotFoundError
    # for a wrong owner exactly as it does for a missing project, so the
    # explicit None branch this replaces is no longer needed.
    await project_service.get_project(project_id, owner=owner)

    name = Path(filename).name.strip()
    if not name or name in (".", ".."):
        raise ValidationError(f"Unsafe filename: {filename!r}")

    obj = DataObject(
        project_id=project_id,
        owner=owner,
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

        # Compression happens here, before dedup and placement, so both the
        # digest checked below and the bytes placed are already final -- see
        # docs/superpowers/specs/2026-08-05-object-compression-design.md.
        # `digest`/`size` shadow the plaintext values above: everything from
        # here on refers to what is actually going into the store.
        staged = await _stage_for_placement(tmp_path, name)
        tmp_path, digest, size = staged.path, staged.digest, staged.size

        dedup_hit = (
            await blob_service.find_present_blob_by_content(staged.content_sha256)
            if staged.content_sha256
            else None
        )
        existing = dedup_hit or await blob_service.find_present_blob(digest)
        if existing is not None and _blob_bytes_present(existing):
            # Identical content already stored; discard the copy we just made.
            tmp_path.unlink(missing_ok=True)
            digest, size = existing.id, existing.size
            log.info("upload_deduplicated", digest=digest, name=name)
        else:
            await asyncio.to_thread(cas.place_blob, tmp_path, digest, size)

        if staged.name != name:
            await obj.set({DataObject.name: staged.name})
        await blob_service.attach_blob_to_object(
            object_id=obj.id,
            digest=digest,
            size=size,
            storage=BlobStorage.MANAGED,
            content_sha256=staged.content_sha256,
        )
        await project_service.bump_counters(project_id, objects=1, total_bytes=size)
        await enqueue_ingest(obj, owner=owner, digest=digest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        await obj.set(
            {
                DataObject.status: ObjectStatus.ERROR,
                DataObject.updated_at: datetime.now(UTC),
            }
        )
        raise

    return await get_object(obj.id, owner=owner)


async def ingest_local_file(
    *,
    owner: str,
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
    content_sha256: str | None = None,
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

    `content_sha256` is for a caller that already compressed `path` itself --
    `download_sra_run` does, since it has a JobContext to report progress
    through and this function never does. Passing it here skips a redundant
    compression attempt (`path` is already `.gz`) while still letting dedup
    find a blob compressed by a different run or a different compressor.
    """
    require_home()

    # Owner-scoped, so a pipeline cannot deposit its output into a project
    # belonging to another profile. See the note in ingest_stream.
    await project_service.get_project(project_id, owner=owner)

    safe_name = Path(name).name.strip()
    if not safe_name or safe_name in (".", ".."):
        raise ValidationError(f"Unsafe filename: {name!r}")

    if not await asyncio.to_thread(path.exists):
        raise NotFoundError(f"Produced file is missing: {path}")

    obj = DataObject(
        project_id=project_id,
        owner=owner,
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
        # Compression happens here, before dedup and placement -- see
        # docs/superpowers/specs/2026-08-05-object-compression-design.md.
        # `path` is reassigned to the compressed copy when compression ran;
        # the plaintext `path` handed in by the caller no longer exists past
        # this point in that case (_stage_for_placement discards it).
        staged = await _stage_for_placement(
            path, safe_name, precomputed_content_sha256=content_sha256
        )
        path, digest, size = staged.path, staged.digest, staged.size
        await obj.set({DataObject.size: size})

        dedup_hit = (
            await blob_service.find_present_blob_by_content(staged.content_sha256)
            if staged.content_sha256
            else None
        )
        existing = dedup_hit or await blob_service.find_present_blob(digest)
        if existing is not None and _blob_bytes_present(existing):
            # A trim run that produced byte-identical output to something
            # already stored -- re-running the same job on the same input, most
            # likely. Keep the record, drop the duplicate bytes.
            await _discard(path)
            digest, size = existing.id, existing.size
            log.info("produced_file_deduplicated", digest=digest, name=safe_name)
        else:
            await asyncio.to_thread(cas.place_blob, path, digest, size)

        if staged.name != safe_name:
            safe_name = staged.name
            await obj.set({DataObject.name: safe_name})
        await blob_service.attach_blob_to_object(
            object_id=obj.id,
            digest=digest,
            size=size,
            storage=BlobStorage.MANAGED,
            content_sha256=staged.content_sha256,
        )
        await project_service.bump_counters(project_id, objects=1, total_bytes=size)
        ingest_job_id = await enqueue_ingest(obj, owner=owner, digest=digest)

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

    return await get_object(obj.id, owner=owner)


@dataclass
class _StagedContent:
    """What to place, after the compression decision has been made.

    `digest`/`size` describe the bytes `path` actually holds -- the CAS key
    they will be placed under. `content_sha256` is set only when compression
    ran, and is what dedup looks up by instead (see
    blob_service.find_present_blob_by_content) so two ingests of the same
    plaintext converge on one blob even if a different compressor wrote each.
    """

    path: Path
    digest: str
    size: int
    name: str
    content_sha256: str | None


async def _stage_for_placement(
    path: Path, name: str, *, precomputed_content_sha256: str | None = None
) -> _StagedContent:
    """Detect the format, compress if the design's allowlist says so, and
    return what to place -- fused into one pass per compress.compress_and_hash
    so this costs no extra read of a large file.

    `path` is consumed on the compress branch: the plaintext temp file is
    removed once the compressed copy exists, since only one of the two is
    going into the store. The uncompressed branch leaves `path` exactly as
    handed in, so a caller's existing cleanup-on-error path keeps working
    unchanged for every format that is not compressed.

    `precomputed_content_sha256` is for a caller that already compressed
    `path` itself before handing it here -- `download_sra_run` compresses in
    its own handler, where it has a JobContext to report progress and honour
    cancellation against, something this async service function never has.
    Detection there already established the content is compressed, so this
    skips both the redundant detect() call and a second compression, but
    still carries the plaintext hash through so dedup-by-content applies
    exactly as it does for every other ingest path.
    """
    if precomputed_content_sha256 is not None:
        digest, size = await asyncio.to_thread(cas.hash_file, path)
        return _StagedContent(
            path=path,
            digest=digest,
            size=size,
            name=name,
            content_sha256=precomputed_content_sha256,
        )

    detection = await asyncio.to_thread(detect.detect, path, name)

    if not compress.should_compress(detection.kind, detection.compression):
        digest, size = await asyncio.to_thread(cas.hash_file, path)
        return _StagedContent(path=path, digest=digest, size=size, name=name, content_sha256=None)

    result = await asyncio.to_thread(compress.compress_and_hash, path)
    await _discard(path)
    # should_compress already required Compression.NONE from the sniffed
    # bytes, so a name still carrying a compression suffix here means the
    # extension disagreed with the content -- a mislabeled or corrupt `.gz`
    # whose bytes were not actually gzip. Stripping it before appending avoids
    # a cosmetic `name.gz.gz` in that case rather than trusting the name.
    bare_name = detect.strip_compression_suffix(name)
    return _StagedContent(
        path=result.path,
        digest=result.compressed_sha256,
        size=result.compressed_size,
        name=f"{bare_name}.gz",
        content_sha256=result.content_sha256,
    )


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
    owner: str,
    digest: str | None = None,
    path: Path | None = None,
    job_class: JobClass = JobClass.USER_BACKGROUND,
) -> str:
    """Queue header parsing for an object.

    Detection and parsing move off the request path: opening a CRAM header or a
    heavily-scaffolded BAM is slow enough to hurt a response, and a malformed
    file must not take the upload down with it.

    `owner` is forwarded to `queue.enqueue`, so the queued job is attributed to
    the profile whose file it is -- not to whichever profile happened to boot
    first, which is what the inherited "local" default gave before.
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
        owner=owner,
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
    owner: str,
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

    # Owner-scoped, so registering a path cannot attach it to another
    # profile's project. See the note in ingest_stream.
    await project_service.get_project(project_id, owner=owner)

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
        owner=owner,
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
        owner=owner,
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
    *,
    owner: str,
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

    # Both sides resolve through the owner-scoped lookup, so pairing cannot
    # reach across the partition from either direction.
    obj = await get_object(object_id, owner=owner)
    mate = await get_object(mate_object_id, owner=owner)

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
        DataObject.owner == owner,
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
        DataObject.owner == owner,
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
    return await get_object(object_id, owner=owner)


async def clear_pair(object_id: PydanticObjectId, *, owner: str) -> DataObject:
    """Undo a pairing, from either side.

    Clears the pointer *and* the read number on both files, but leaves "mate"
    in user_touched: the cleared state is itself the user's decision, and that
    entry is what stops filename inference from re-asserting the pairing on the
    next re-ingest.

    A no-op on an unpaired object, so the button is idempotent.
    """
    obj = await get_object(object_id, owner=owner)

    if obj.mate_object_id is None:
        return obj

    cleared = {
        "$set": {
            DataObject.mate_object_id: None,
            DataObject.read_number: None,
            DataObject.updated_at: datetime.now(UTC),
        },
        "$addToSet": {"user_touched": "mate"},
    }

    # The mate is cleared by id rather than by fetching it first: the row may
    # be gone (deleted out from under a stale tab), and that must not block
    # unpairing the file the user is actually looking at. Still owner-scoped --
    # a mate always shares its parent's owner, so the clause never costs a
    # legitimate clear, and a cross-owner mate pointer arriving some other way
    # must not become a write into another profile's row.
    await DataObject.find_one(
        DataObject.id == obj.mate_object_id, DataObject.owner == owner
    ).update(cleared)
    await DataObject.find_one(
        DataObject.id == obj.id, DataObject.owner == owner
    ).update(cleared)

    log.info("pair_cleared_manually", object_id=str(obj.id), mate_id=str(obj.mate_object_id))
    return await get_object(object_id, owner=owner)


async def update_object(
    object_id: PydanticObjectId, updates: dict, *, owner: str
) -> DataObject:
    obj = await get_object(object_id, owner=owner)

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


async def delete_object(object_id: PydanticObjectId, *, owner: str) -> None:
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

    Report directories are removed here too. They sit outside objects/ and so
    are not content-addressed, which means blob GC never sees them: nothing but
    this call will ever free them.
    """
    # The ownership check for the whole cascade: a wrong-owner call raises here,
    # before any sidecar is touched or any blob decremented.
    await get_object(object_id, owner=owner)

    sidecars = await list_sidecars(object_id, owner=owner)
    for sidecar in sidecars:
        # Sidecars of sidecars are not a shape this produces today (a .bai
        # attaches to a BAM, not to another sidecar), but recursing costs
        # nothing and means a future two-level artifact cannot strand a blob.
        # The owner goes down with it: a sidecar always shares its parent's
        # owner, so passing anything else would fail its own get_object and
        # leave the index behind at refcount 1.
        await delete_object(sidecar.id, owner=owner)
        log.info(
            "sidecar_deleted_with_parent",
            object_id=str(sidecar.id),
            parent_id=str(object_id),
            role=sidecar.sidecar_role.value if sidecar.sidecar_role else None,
        )

    await asyncio.to_thread(
        remove_report_dirs, object_id, caller="delete_object", reason="object_deleted"
    )
    await blob_service.detach_blob_from_object(object_id)


def remove_report_dirs(object_id: PydanticObjectId, *, caller: str, reason: str) -> None:
    """Remove the per-object Results directories written outside objects/.

    Best-effort by design. The overwhelmingly common case is that none of these
    exist -- most objects never have Results computed -- so absence is normal
    and must never fail the delete. A directory that cannot be removed is worth
    a log line and nothing more: the object is going away regardless, and
    refusing to delete it would trade a recoverable disk leak for a file the
    user cannot get rid of.

    `caller` and `reason` are required, not defaulted, so every deletion's log
    line says who triggered it and why -- the two facts issue #10 found
    missing when a report directory disappeared with no way to tell whether
    `delete_object` or the reaper was responsible.
    """
    for parent in _REPORT_ROOTS:
        path = parent / str(object_id)
        # The id is a validated ObjectId, so it cannot traverse -- checked
        # anyway because the cost is nothing and the blast radius of an rmtree
        # that escapes its parent is the whole storage home.
        try:
            path.resolve().relative_to(parent.resolve())
        except ValueError:
            log.error(
                "refusing_report_cleanup_outside_parent",
                path=str(path),
                caller=caller,
                reason=reason,
            )
            continue
        if not path.exists():
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            log.warning(
                "report_dir_cleanup_failed",
                object_id=str(object_id),
                path=str(path),
                error=str(exc),
                caller=caller,
                reason=reason,
            )
        else:
            log.info(
                "report_dir_removed",
                object_id=str(object_id),
                path=str(path),
                caller=caller,
                reason=reason,
            )


def copy_report_dirs(src_object_id: PydanticObjectId, dst_object_id: PydanticObjectId) -> None:
    """Copy the per-object Results directories to a new object id, for sharing.

    Report directories are keyed by object id, not by blob digest, so a shared
    copy has a different id than its source and would otherwise arrive with no
    QC report. A read-time fallback through the source id is the wrong fix --
    it breaks the moment the source is deleted, since `remove_report_dirs`
    removes exactly these directories. Copying at share time makes the
    recipient's report independent bytes under the recipient's own id.

    Best-effort, mirroring `remove_report_dirs`: a missing source directory is
    the common case (most objects have no Results computed) and is skipped
    silently; an already-present destination is left alone rather than merged;
    a copy failure logs and moves to the next root. Nothing here raises -- a
    missing report is recomputable, and failing the share over one would trade
    a working file for none.
    """
    for parent in _REPORT_ROOTS:
        src = parent / str(src_object_id)
        dst = parent / str(dst_object_id)
        try:
            src.resolve().relative_to(parent.resolve())
            dst.resolve().relative_to(parent.resolve())
        except ValueError:
            log.error("refusing_report_copy_outside_parent", src=str(src), dst=str(dst))
            continue
        if not src.exists() or dst.exists():
            continue
        try:
            shutil.copytree(src, dst)
        except OSError as exc:
            log.warning(
                "report_dir_copy_failed",
                src_object_id=str(src_object_id),
                dst_object_id=str(dst_object_id),
                path=str(src),
                error=str(exc),
            )
        else:
            log.info(
                "report_dir_copied",
                src_object_id=str(src_object_id),
                dst_object_id=str(dst_object_id),
                path=str(dst),
            )


async def object_with_blob(
    object_id: PydanticObjectId, *, owner: str
) -> tuple[DataObject, Blob | None]:
    obj = await get_object(object_id, owner=owner)
    blob = await Blob.get(obj.blob_sha256) if obj.blob_sha256 else None
    return obj, blob


async def offload_object(object_id: PydanticObjectId, *, owner: str) -> DataObject:
    """Drop an object's bytes, keeping the file in the project tree.

    The precondition is the whole feature: bytes may only be released when
    they can be got back. `metadata.sra_run` is the address the SRA download
    path has always written; `parse_accession` on the filename is the fallback
    for objects that predate it, and is deliberately checked second so a
    stored accession always beats one guessed from a name.

    v1 releases SRA runs only. An assembly is one of the things this refuses:
    what accession an assembly object stores, and whether a single component
    can be re-fetched, has not been established, and guessing would produce an
    object that can never be restored -- silent data loss dressed as a
    space saving.

    Sidecars are not cascaded, unlike `delete_object`. An index is scaffolding
    and rebuildable, but it is also small, and dropping it would mean a fetch
    has to rebuild the index too. The user asked to reclaim a multi-gigabyte
    FASTQ, not to make the next alignment slower.
    """
    obj = await get_object(object_id, owner=owner)

    if obj.locality is Locality.REMOTE:
        return obj  # already offloaded; idempotent, see release_bytes_for_object

    accession = obj.metadata.get("sra_run") or sra_metadata.parse_accession(obj.name)
    if not accession:
        raise ValidationError(
            f"{obj.name!r} cannot be offloaded: nothing records where to fetch it back from",
            details={"object_id": str(object_id), "name": obj.name},
        )

    return await blob_service.release_bytes_for_object(
        object_id, accession=str(accession)
    )


def check_local(obj: DataObject, *, verb: str) -> None:
    """Refuse an offloaded object before a caller reaches for its bytes.

    A separate helper rather than a check inside `object_with_blob`, because
    that function is also how the plain detail endpoint loads an object -- and
    a remote object must still be *viewable*, listed, and suggested. Only the
    paths that actually read bytes refuse.

    Without this, every such path falls through to its own "no stored content"
    message, which is written for an upload still in flight and reads as a bug
    on a file the user offloaded on purpose. `verb` names what was being
    attempted so the message is about their action, not our storage.
    """
    if obj.locality is not Locality.REMOTE:
        return
    # `remote_source` first, `metadata.sra_run` as the fallback -- see
    # pipeline_service._refetch_accession for why the fallback matters.
    accession = (
        obj.remote_source.accession
        if obj.remote_source is not None
        else (str(obj.metadata["sra_run"]) if obj.metadata.get("sra_run") else None)
    )
    where = f" from {accession}" if accession else ""
    raise ValidationError(
        f"{obj.name!r} is stored remotely -- fetch it{where} before you can {verb} it",
        details={
            "object_id": str(obj.id),
            "locality": obj.locality.value,
            "accession": accession,
        },
    )
