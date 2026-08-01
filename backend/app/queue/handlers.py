"""Job handlers.

Every handler must be idempotent: delivery is at-least-once, so any handler can
be invoked twice for the same job (a lease can expire while the work is
genuinely still running).
"""

import time
from pathlib import Path

from beanie import PydanticObjectId

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import DataObject, IoClass, JobClass, JobResources, ObjectStatus
from app.queue.registry import HandlerMode, JobContext, handler
from app.storage import cas
from app.storage.paths import blob_path

log = get_logger(__name__)


@handler(
    "hash_blob",
    mode=HandlerMode.THREAD,
    job_class=JobClass.USER_BACKGROUND,
    resources=JobResources(cpu=1, mem_mb=128, io=IoClass.HEAVY),
)
def hash_blob(ctx: JobContext) -> dict:
    """Compute the SHA-256 of a file already on disk.

    Idempotent: hashing the same bytes always yields the same digest, so a
    repeat invocation simply recomputes the same answer.
    """
    path_str = ctx.payload.get("path")
    if not path_str:
        raise PermanentError("hash_blob requires a 'path' payload field")

    path = Path(path_str)
    if not path.exists():
        raise PermanentError(f"File does not exist: {path}")

    total = path.stat().st_size
    ctx.progress(phase="hashing", bytes_total=total, pct=0.0)

    def on_progress(done: int) -> None:
        ctx.progress(
            phase="hashing",
            bytes_done=done,
            bytes_total=total,
            pct=round(done / total, 4) if total else 1.0,
        )

    digest, size = cas.hash_file(
        path, cancel_event=ctx.cancel_event, progress_cb=on_progress
    )
    ctx.progress(phase="done", pct=1.0, bytes_done=size, bytes_total=size)
    return {"sha256": digest, "size": size}


@handler(
    "verify_blob",
    mode=HandlerMode.THREAD,
    job_class=JobClass.MAINTENANCE,
    resources=JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
)
def verify_blob(ctx: JobContext) -> dict:
    """Confirm a single blob's bytes are still present.

    The batched, mount-aware version is a Phase 4 periodic job; this handles a
    user-initiated check of one file.
    """
    digest = ctx.payload.get("sha256")
    if not digest:
        raise PermanentError("verify_blob requires a 'sha256' payload field")

    external = ctx.payload.get("external_path")
    path = Path(external) if external else blob_path(digest)
    exists = path.exists()
    size = path.stat().st_size if exists else None
    return {"sha256": digest, "exists": exists, "size": size, "path": str(path)}


@handler(
    "noop",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=0, mem_mb=0),
)
async def noop(ctx: JobContext) -> dict:
    """Does nothing. Used to exercise dispatch in tests and smoke checks."""
    return {"ok": True, "payload": ctx.payload}


@handler(
    "sleep_test",
    mode=HandlerMode.THREAD,
    job_class=JobClass.USER_BACKGROUND,
    resources=JobResources(cpu=1, mem_mb=32),
)
def sleep_test(ctx: JobContext) -> dict:
    """Sleep cooperatively, for exercising cancellation and lease renewal.

    Polls in short slices rather than one long sleep, which is what makes
    cancellation observable within ~100 ms.
    """
    seconds = float(ctx.payload.get("seconds", 5))
    step = 0.1
    waited = 0.0
    while waited < seconds:
        ctx.check_cancel()
        time.sleep(step)
        waited += step
        ctx.progress(phase="sleeping", pct=round(waited / seconds, 3))
    return {"slept": waited}


@handler(
    "assemble_upload",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=1, mem_mb=256, io=IoClass.HEAVY),
    max_attempts=3,
)
async def assemble_upload(ctx: JobContext) -> dict:
    """Concatenate an upload's chunks, hash them, and place the blob.

    Idempotent: assembly truncates its target, placement is content-addressed,
    and a session already completed short-circuits. A retry after a crash
    therefore converges rather than duplicating work.
    """
    import asyncio

    from app.models import BlobStorage, UploadSession, UploadState
    from app.services import blob_service, object_service, project_service, upload_service
    from app.storage import assembly

    session_id = ctx.payload.get("session_id")
    object_id = ctx.payload.get("object_id")
    if not session_id or not object_id:
        raise PermanentError("assemble_upload requires 'session_id' and 'object_id'")

    session = await UploadSession.get(PydanticObjectId(session_id))
    if session is None:
        raise PermanentError(f"Upload session not found: {session_id}")
    if session.state is UploadState.COMPLETED and session.resulting_sha256:
        return {"sha256": session.resulting_sha256, "already_completed": True}

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        raise PermanentError(f"Object not found: {object_id}")

    staging = Path(session.staging_dir)
    chunk_paths = [
        upload_service.chunk_path(staging, i) for i in range(session.total_chunks)
    ]
    assembled = staging / "assembled.bin"

    await obj.set({DataObject.status: ObjectStatus.HASHING})
    ctx.progress(phase="assembling", bytes_total=session.total_size, pct=0.0)

    def on_progress(done: int) -> None:
        ctx.progress(
            phase="assembling",
            bytes_done=done,
            bytes_total=session.total_size,
            pct=round(done / session.total_size, 4) if session.total_size else 1.0,
        )

    digest, size = await asyncio.to_thread(
        assembly.assemble_and_hash,
        chunk_paths,
        assembled,
        cancel_event=ctx.cancel_event,
        progress_cb=on_progress,
    )

    # The client's own digest is checked here rather than trusted: a mismatch
    # means the file we assembled is not the file they meant to send.
    if session.client_sha256 and digest != session.client_sha256:
        assembled.unlink(missing_ok=True)
        raise PermanentError(
            f"Assembled digest {digest} does not match the client-supplied "
            f"{session.client_sha256}; the upload is corrupt"
        )

    ctx.progress(phase="placing", pct=1.0)

    existing = await blob_service.find_present_blob(digest)
    if existing is not None and _blob_present_on_disk(existing):
        assembled.unlink(missing_ok=True)
        placement = "dedup"
    else:
        await asyncio.to_thread(cas.place_blob, assembled, digest, size)
        placement = "created"

    await blob_service.attach_blob_to_object(
        object_id=obj.id, digest=digest, size=size, storage=BlobStorage.MANAGED
    )
    await project_service.bump_counters(obj.project_id, objects=1, total_bytes=size)
    await object_service.enqueue_ingest(obj, digest=digest)

    await session.set(
        {
            UploadSession.state: UploadState.COMPLETED,
            UploadSession.resulting_sha256: digest,
            UploadSession.assembled_path: None,
        }
    )
    await asyncio.to_thread(upload_service.cleanup_staging, session.staging_dir)

    log.info("upload_assembled", digest=digest, size=size, placement=placement)
    return {"sha256": digest, "size": size, "placement": placement}


def _blob_present_on_disk(blob) -> bool:
    from app.models import BlobStorage

    if blob.storage is BlobStorage.EXTERNAL:
        return bool(blob.external_path and Path(blob.external_path).exists())
    return blob_path(blob.id).exists()


def _stat_or_none(path: Path):
    """stat() that reports absence as None rather than raising."""
    try:
        return path.stat()
    except OSError:
        return None


@handler(
    "ingest_headers",
    mode=HandlerMode.THREAD,
    job_class=JobClass.USER_BACKGROUND,
    resources=JobResources(cpu=1, mem_mb=256, io=IoClass.LIGHT),
    max_attempts=3,
)
def ingest_headers(ctx: JobContext) -> dict:
    """Detect a file's format and extract its header facts.

    Runs as a queue job rather than inline because opening a CRAM or a
    heavily-scaffolded BAM header is slow enough to hurt a request, and because
    a malformed file must not take an upload down with it.

    Idempotent: parsing is a pure read, so a repeat run recomputes the same
    facts.
    """
    from app.storage import detect as detect_mod
    from app.storage import parsers

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("ingest_headers requires an 'object_id'")

    path_str = ctx.payload.get("path")
    digest = ctx.payload.get("sha256")
    if path_str:
        path = Path(path_str)
    elif digest:
        path = blob_path(digest)
    else:
        raise PermanentError("ingest_headers requires 'path' or 'sha256'")

    if not path.exists():
        raise PermanentError(f"File not found for ingest: {path}")

    ctx.progress(phase="detecting", pct=0.1)
    name = ctx.payload.get("name") or path.name
    detection = detect_mod.detect(path, name)

    ctx.progress(phase="parsing", pct=0.4)
    facts = parsers.parse(
        path,
        detection.kind,
        detection.compression,
        cancel_event=ctx.cancel_event,
        # Managed blobs live under their hash, so the only place the user's
        # filename survives is the payload -- and conventions like _R1/_R2 are
        # encoded there.
        display_name=name,
    )

    # SRA enrichment: a file named SRR11768093_1.fastq can be looked up at NCBI
    # for organism, platform, library strategy and sample attributes. The
    # object's current metadata comes in on the payload so an accession the
    # user typed by hand takes precedence over the filename -- and so nothing
    # they entered is ever overwritten.
    ctx.progress(phase="enriching", pct=0.8)
    enrichment = None
    if settings.sra_enrichment_enabled:
        from app.metadata import enrich

        enrichment = enrich.enrich_from_sra(
            filename=name,
            existing_metadata=ctx.payload.get("metadata") or {},
            format_kind=detection.kind,
        ).to_dict()

    # Assembly enrichment: a reference genome from NCBI carries its accession in
    # the filename (GCF_000002445.2_ASM244v1_genomic.fna), which yields the
    # organism, assembly level and sequence stats -- and marks the file as a
    # reference.
    assembly_enrichment = None
    if settings.assembly_enrichment_enabled:
        from app.metadata import enrich

        assembly_enrichment = enrich.enrich_from_assembly(
            filename=name,
            existing_metadata=ctx.payload.get("metadata") or {},
            format_kind=detection.kind,
        ).to_dict()

    ctx.progress(phase="done", pct=1.0)
    return {
        "object_id": object_id,
        "enrichment": enrichment,
        "assembly_enrichment": assembly_enrichment,
        "format": {
            "kind": detection.kind.value,
            "compression": detection.compression.value,
            "confidence": detection.confidence.value,
            "extension_says": (
                detection.extension_says.value if detection.extension_says else None
            ),
            "magic_says": detection.magic_says.value if detection.magic_says else None,
        },
        "facts": facts,
    }


@handler(
    "register_hash",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.USER_BACKGROUND,
    resources=JobResources(cpu=1, mem_mb=128, io=IoClass.HEAVY),
    max_attempts=3,
)
async def register_hash(ctx: JobContext) -> dict:
    """Hash a registered-in-place file and attach it as an external blob.

    The file is never moved or copied. Its size and mtime are recorded so the
    verifier can later detect that it changed underneath us.
    """
    import asyncio

    from app.models import BlobStorage
    from app.services import blob_service, object_service

    object_id = ctx.payload.get("object_id")
    path_str = ctx.payload.get("path")
    if not object_id or not path_str:
        raise PermanentError("register_hash requires 'object_id' and 'path'")

    path = Path(path_str)
    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        raise PermanentError(f"Object not found: {object_id}")

    # stat() on a VirtioFS mount can block for tens of milliseconds; on the
    # event loop that stalls every heartbeat in this process.
    stat = await asyncio.to_thread(_stat_or_none, path)
    if stat is None:
        raise PermanentError(f"Registered file no longer exists: {path}")

    total = stat.st_size
    ctx.progress(phase="hashing", bytes_total=total, pct=0.0)

    def on_progress(done: int) -> None:
        ctx.progress(
            phase="hashing",
            bytes_done=done,
            bytes_total=total,
            pct=round(done / total, 4) if total else 1.0,
        )

    digest, size = await asyncio.to_thread(
        cas.hash_file, path, cancel_event=ctx.cancel_event, progress_cb=on_progress
    )

    # If we already hold this content as a managed blob, that copy wins: it is
    # one we control, and the bytes are identical by definition.
    existing = await blob_service.find_present_blob(digest)
    storage = BlobStorage.MANAGED if (
        existing is not None
        and existing.storage is BlobStorage.MANAGED
        and blob_path(digest).exists()
    ) else BlobStorage.EXTERNAL

    await blob_service.attach_blob_to_object(
        object_id=obj.id,
        digest=digest,
        size=size,
        storage=storage,
        external_path=str(path) if storage is BlobStorage.EXTERNAL else None,
        observed_mtime=stat.st_mtime,
    )
    await object_service.enqueue_ingest(obj, path=path)

    log.info("register_hashed", path=str(path), digest=digest, storage=storage.value)
    return {"sha256": digest, "size": size, "storage": storage.value}


@handler(
    "verify_files",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.MAINTENANCE,
    resources=JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
    max_attempts=2,
)
async def verify_files(ctx: JobContext) -> dict:
    """Confirm registered files still exist, in oldest-checked-first batches.

    The requirement is "check every minute that each file still exists". Taken
    literally, that means 100k stat() calls per minute across a FUSE mount --
    which would itself become the load problem this system exists to avoid. So
    each tick verifies a batch ordered by `last_verified_at` ascending, which
    covers the whole library on a rotation (500/min ≈ 3.5 hours for 100k files)
    at negligible cost.

    Two guards make this safe, and they matter more than the checking does:

      1. The mount sentinel is checked first. An unmounted external drive
         presents as an *empty* /data, not an error -- so without this, one
         pass would mark the entire library missing. If the sentinel is gone
         the whole batch aborts and nothing is touched.

      2. A single miss never marks a file missing. External drives disappear
         transiently; two consecutive misses at least 60s apart are required.
    """
    import asyncio
    from datetime import UTC, datetime, timedelta

    from app.models import Blob, BlobState, BlobStorage, DataObject, ObjectStatus
    from app.queue import queue as queue_mod
    from app.storage.home import check_home

    # --- Guard 1: is the drive actually there? ---
    home = check_home()
    if not home.ok:
        log.error("verify_aborted_storage_unavailable", detail=home.detail)
        await queue_mod.publish_event(
            "storage.unavailable", {"detail": home.detail, "path": home.path}
        )
        return {"skipped": True, "reason": home.detail, "checked": 0}

    batch_size = int(ctx.payload.get("batch_size", 500))
    now = datetime.now(UTC)

    blobs = (
        await Blob.find(Blob.state != BlobState.MISSING)
        .sort("+last_verified_at")
        .limit(batch_size)
        .to_list()
    )

    checked = present = newly_missing = confirmed_missing = drifted = 0

    for blob in blobs:
        ctx.check_cancel()
        checked += 1

        path = (
            Path(blob.external_path)
            if blob.storage is BlobStorage.EXTERNAL and blob.external_path
            else blob_path(blob.id)
        )
        stat = await asyncio.to_thread(_stat_or_none, path)

        if stat is not None:
            present += 1
            updates: dict = {
                Blob.last_verified_at: now,
                Blob.miss_count: 0,
                Blob.updated_at: now,
            }
            # Heal a record that had recorded a miss but is fine now.
            if blob.state is not BlobState.PRESENT:
                updates[Blob.state] = BlobState.PRESENT

            # An external file is not ours; it can change behind our back, and
            # silently accepting that would mean the recorded hash is a lie.
            if blob.storage is BlobStorage.EXTERNAL and blob.observed_size is not None:
                if stat.st_size != blob.observed_size or (
                    blob.observed_mtime is not None
                    and abs(stat.st_mtime - blob.observed_mtime) > 1.0
                ):
                    drifted += 1
                    updates[Blob.state] = BlobState.QUARANTINED
                    log.warning(
                        "external_blob_drifted",
                        digest=blob.id,
                        path=str(path),
                        recorded_size=blob.observed_size,
                        actual_size=stat.st_size,
                    )
                    await queue_mod.publish_event(
                        "blob.drifted", {"sha256": blob.id, "path": str(path)}
                    )
            await blob.set(updates)
            continue

        # --- Guard 2: two strikes, spaced apart ---
        first_miss = blob.last_miss_at is None
        long_enough = (
            not first_miss and (now - blob.last_miss_at) >= timedelta(seconds=60)
        )

        if first_miss or not long_enough:
            newly_missing += 1
            await blob.set(
                {
                    Blob.miss_count: blob.miss_count + 1,
                    Blob.last_miss_at: now,
                    Blob.last_verified_at: now,
                    Blob.updated_at: now,
                }
            )
            log.info("blob_missed_once", digest=blob.id, miss_count=blob.miss_count + 1)
            continue

        confirmed_missing += 1
        await blob.set(
            {
                Blob.state: BlobState.MISSING,
                Blob.miss_count: blob.miss_count + 1,
                Blob.last_miss_at: now,
                Blob.last_verified_at: now,
                Blob.updated_at: now,
            }
        )
        # Surface it on every object that references the vanished content.
        await DataObject.find(DataObject.blob_sha256 == blob.id).update(
            {"$set": {"status": ObjectStatus.MISSING.value, "updated_at": now}}
        )
        log.error("blob_confirmed_missing", digest=blob.id, path=str(path))
        await queue_mod.publish_event("blob.missing", {"sha256": blob.id})

    result = {
        "checked": checked,
        "present": present,
        "first_miss": newly_missing,
        "confirmed_missing": confirmed_missing,
        "drifted": drifted,
    }
    if confirmed_missing or drifted:
        log.warning("verify_found_problems", **result)
    return result


@handler(
    "gc_blobs",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.MAINTENANCE,
    resources=JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
)
async def gc_blobs(ctx: JobContext) -> dict:
    """Unlink managed blobs whose refcount has been zero past the grace window.

    External blobs are never unlinked -- we do not own files the user registered
    in place, so only the database record is removed.
    """
    import asyncio

    from app.models import Blob, BlobStorage
    from app.services import blob_service
    from app.storage.home import check_home

    # Never delete anything while the drive is questionable.
    home = check_home()
    if not home.ok:
        log.warning("gc_skipped_storage_unavailable", detail=home.detail)
        return {"skipped": True, "reason": home.detail}

    limit = int(ctx.payload.get("limit", 100))
    candidates = await blob_service.gc_candidates(limit=limit)

    unlinked = 0
    for blob in candidates:
        ctx.check_cancel()
        if blob.storage is BlobStorage.EXTERNAL:
            await blob.delete()
            continue
        if await asyncio.to_thread(cas.unlink_blob, blob.id):
            unlinked += 1
        await blob.delete()

    # External records with no references carry no bytes to reclaim.
    external_pruned = 0
    async for blob in Blob.find(
        Blob.ref_count <= 0, Blob.storage == BlobStorage.EXTERNAL
    ):
        await blob.delete()
        external_pruned += 1

    if unlinked or external_pruned:
        log.info("gc_completed", unlinked=unlinked, external_pruned=external_pruned)
    return {
        "candidates": len(candidates),
        "unlinked": unlinked,
        "external_pruned": external_pruned,
    }


@handler(
    "reap_uploads",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.MAINTENANCE,
    resources=JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
)
async def reap_uploads(ctx: JobContext) -> dict:
    """Remove abandoned staging directories.

    The session TTL index deletes the *document* but cannot touch the files, so
    an interrupted 30 GB upload would otherwise occupy the drive forever. This
    scans the staging tree itself rather than trusting the collection.
    """
    import asyncio
    from datetime import UTC, datetime, timedelta

    from app.models import UploadSession, UploadState
    from app.services import upload_service
    from app.storage.home import check_home

    home = check_home()
    if not home.ok:
        return {"skipped": True, "reason": home.detail}

    max_age_hours = float(ctx.payload.get("max_age_hours", 24))
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)

    active_ids = {
        str(s.id)
        for s in await UploadSession.find(
            {"state": {"$in": [UploadState.OPEN.value, UploadState.ASSEMBLING.value]}}
        ).to_list()
    }

    removed = 0
    staging_root = settings.staging_dir
    if not staging_root.exists():
        return {"removed": 0}

    for entry in await asyncio.to_thread(lambda: list(staging_root.iterdir())):
        ctx.check_cancel()
        if not entry.is_dir() or entry.name in active_ids:
            continue
        mtime = datetime.fromtimestamp(entry.stat().st_mtime, UTC)
        if mtime > cutoff:
            continue  # young orphan; a session may still be opening
        await asyncio.to_thread(upload_service.cleanup_staging, str(entry))
        removed += 1

    if removed:
        log.info("staging_reaped", removed=removed)
    return {"removed": removed}


@handler(
    "reap_report_dirs",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.MAINTENANCE,
    resources=JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
)
async def reap_report_dirs(ctx: JobContext) -> dict:
    """Remove Results directories whose object no longer exists.

    `object_service.delete_object` removes these as part of the delete, so this
    exists for the ones already stranded before it did -- and as a backstop, in
    the same spirit as reap_uploads scanning the tree rather than trusting the
    collection. A single VCF's variants.db can run to hundreds of megabytes, so
    a stranded directory is worth real disk.

    Matches on the directory name being an object id absent from Mongo. Entries
    that are not object ids are left alone: this sweeps its own leavings, and
    something else's file under these roots is not its business to delete.
    """
    import asyncio
    from datetime import UTC, datetime, timedelta

    from app.services import object_service
    from app.storage.home import check_home

    home = check_home()
    if not home.ok:
        return {"skipped": True, "reason": home.detail}

    # A directory is created before the compute job finishes writing into it,
    # and the object row can lag a moment behind at ingest. The grace window
    # keeps the sweep from racing either one.
    max_age_hours = float(ctx.payload.get("max_age_hours", 1))
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)

    removed = 0
    reclaimed = 0
    for root in (settings.qc_reports_dir, settings.bam_stats_dir, settings.vcf_stats_dir):
        if not root.exists():
            continue
        for entry in await asyncio.to_thread(lambda r=root: list(r.iterdir())):
            ctx.check_cancel()
            if not entry.is_dir():
                continue
            try:
                object_id = PydanticObjectId(entry.name)
            except Exception:
                continue
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, UTC)
            if mtime > cutoff:
                continue
            if await DataObject.get(object_id) is not None:
                continue
            size = await asyncio.to_thread(
                lambda e=entry: sum(f.stat().st_size for f in e.rglob("*") if f.is_file())
            )
            await asyncio.to_thread(object_service.remove_report_dirs, object_id)
            if not entry.exists():
                removed += 1
                reclaimed += size

    if removed:
        log.info("report_dirs_reaped", removed=removed, bytes_reclaimed=reclaimed)
    return {"removed": removed, "bytes_reclaimed": reclaimed}


# Pipeline handlers live in their own modules -- they shell out to external
# tools and carry a different failure model -- but must be imported here, since
# registry.load_handlers() imports only this one.
from app.queue import (  # noqa: E402, F401
    align_handlers,
    assembly_handlers,
    pipeline_handlers,
    sra_handlers,
    summary_handlers,
    uniprot_handlers,
    variant_handlers,
)
