"""Chunked, resumable uploads.

The design assumption is that a transfer can fail at 90% of a 30 GB file. Every
piece of state is therefore recoverable: chunks land atomically, the session
records exactly which ones arrived, and a client that reconnects asks the server
what is still missing rather than starting over.
"""

import asyncio
import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from beanie import PydanticObjectId

from app.config import settings
from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging import get_logger
from app.models import (
    DataObject,
    IoClass,
    JobClass,
    JobResources,
    ObjectStatus,
    Project,
    SourceInfo,
    SourceMode,
    UploadSession,
    UploadState,
)
from app.storage.home import require_home
from app.storage.paths import staging_dir_for, validate_sha256

log = get_logger(__name__)

DEFAULT_CHUNK_SIZE = 16 * 1024 * 1024  # 16 MiB
# Chunk indices are tracked in an array on the session document. Capping the
# count and scaling chunk size instead keeps that array small even for a 1 TB
# file (which would otherwise need 65,000 entries at 16 MiB).
MAX_CHUNKS = 10_000
SESSION_TTL_HOURS = 24


def choose_chunk_size(total_size: int) -> int:
    if total_size <= 0:
        return DEFAULT_CHUNK_SIZE
    if total_size / DEFAULT_CHUNK_SIZE <= MAX_CHUNKS:
        return DEFAULT_CHUNK_SIZE
    target = total_size // (MAX_CHUNKS - 200)
    size = DEFAULT_CHUNK_SIZE
    while size < target:
        size *= 2
    return size


def chunk_path(staging: Path, index: int) -> Path:
    # Zero-padded so a directory listing sorts in assembly order.
    return staging / "chunks" / f"{index:06d}.part"


async def create_session(
    *,
    project_id: PydanticObjectId,
    owner: str,
    filename: str,
    total_size: int,
    client_sha256: str | None = None,
) -> tuple[UploadSession | None, DataObject | None]:
    """Open an upload session, or short-circuit if we already hold the content.

    Returns (session, None) for a normal upload, or (None, object) when the
    client's digest matched a blob already in the store -- in which case zero
    bytes cross the wire.

    `owner` is stamped on the session (and on any object this creates) so the
    owner-scoped cascade in project_service.delete_project_tree can see them.
    Without it every session took the "local" default from
    TimestampedDocument, and an upload into a non-"local" project left a
    session -- and a staging directory -- that deleting the project could not
    reach.
    """
    require_home()

    # Resolved through the owner-scoped lookup rather than Project.get, matching
    # object_service's register_* paths: a wrong-owner project reads as missing
    # rather than as someone else's.
    project = await Project.get(project_id)
    if project is None or project.owner != owner:
        raise NotFoundError(f"Project not found: {project_id}")

    name = Path(filename).name.strip()
    if not name or name in (".", ".."):
        raise ValidationError(f"Unsafe filename: {filename!r}")
    if total_size < 0:
        raise ValidationError("total_size must be non-negative")

    if client_sha256:
        client_sha256 = validate_sha256(client_sha256)
        obj = await _try_dedup(project_id, owner, name, client_sha256, total_size)
        if obj is not None:
            log.info("upload_dedup_preflight", digest=client_sha256, name=name)
            return None, obj

    chunk_size = choose_chunk_size(total_size)
    total_chunks = max(1, (total_size + chunk_size - 1) // chunk_size)

    session = UploadSession(
        project_id=project_id,
        owner=owner,
        filename=name,
        total_size=total_size,
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        client_sha256=client_sha256,
        staging_dir="",  # filled in below, once the id exists
        expires_at=datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS),
    )
    await session.insert()

    staging = staging_dir_for(str(session.id))
    (staging / "chunks").mkdir(parents=True, exist_ok=True)
    session.staging_dir = str(staging)
    await session.save()

    # Mirror the session to disk. If Mongo is lost, the staging directory still
    # says what it was for -- otherwise an orphaned 30 GB of chunks is a mystery.
    _write_manifest(staging, session)

    return session, None


async def _try_dedup(
    project_id: PydanticObjectId, owner: str, name: str, digest: str, size: int
) -> DataObject | None:
    from app.services import blob_service, object_service, project_service
    from app.storage.paths import blob_path

    blob = await blob_service.find_present_blob(digest)
    if blob is None:
        return None
    # Trust the ledger only as far as the filesystem agrees with it.
    if not blob_path(digest).exists():
        log.warning("dedup_blob_missing_on_disk", digest=digest)
        return None

    obj = DataObject(
        project_id=project_id,
        owner=owner,
        name=name,
        status=ObjectStatus.HASHING,
        size=size or blob.size,
        source=SourceInfo(mode=SourceMode.UPLOAD, original_name=name),
    )
    await obj.insert()
    await blob_service.attach_blob_to_object(
        object_id=obj.id, digest=digest, size=blob.size
    )
    await project_service.bump_counters(project_id, objects=1, total_bytes=blob.size)
    # The object's own owner, which is now its project's rather than the
    # TimestampedDocument default: this is the one path where a dedup hit
    # creates an object without going through object_service's register_*.
    await object_service.enqueue_ingest(obj, owner=obj.owner, digest=digest)
    return await DataObject.get(obj.id)


def _write_manifest(staging: Path, session: UploadSession) -> None:
    try:
        (staging / "manifest.json").write_text(
            json.dumps(
                {
                    "session_id": str(session.id),
                    "project_id": str(session.project_id),
                    "filename": session.filename,
                    "total_size": session.total_size,
                    "chunk_size": session.chunk_size,
                    "total_chunks": session.total_chunks,
                    "created_at": session.created_at.isoformat(),
                },
                indent=2,
            )
        )
    except OSError as e:  # noqa: BLE001 - the manifest is a convenience, not state
        log.warning("manifest_write_failed", error=str(e))


async def get_session(session_id: PydanticObjectId, *, owner: str) -> UploadSession:
    """Fetch a session, treating another owner's session as missing.

    `owner` is required rather than optional because every caller below reaches
    a session through this one function -- resume, chunk write, complete and
    abort all start here. Making it a keyword with no default means a new call
    site cannot silently inherit an unpartitioned lookup: it fails to run until
    it says whose session it means.
    """
    session = await UploadSession.get(session_id)
    if session is None or session.owner != owner:
        raise NotFoundError(f"Upload session not found: {session_id}")
    return session


def missing_chunks(session: UploadSession) -> list[int]:
    received = set(session.received_chunks)
    return [i for i in range(session.total_chunks) if i not in received]


async def write_chunk(
    session_id: PydanticObjectId,
    index: int,
    data: bytes,
    *,
    owner: str,
    expected_sha256: str | None = None,
) -> UploadSession:
    """Store one chunk. Idempotent -- re-sending a chunk is always safe."""
    session = await get_session(session_id, owner=owner)

    if session.state is not UploadState.OPEN:
        raise ConflictError(
            f"Upload session is {session.state.value}; chunks are only accepted "
            "while it is open",
            details={"state": session.state.value},
        )
    if not 0 <= index < session.total_chunks:
        raise ValidationError(
            f"Chunk index {index} out of range (0..{session.total_chunks - 1})"
        )

    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256:
        if digest != expected_sha256.lower():
            raise ValidationError(
                "Chunk digest mismatch; the chunk was corrupted in transit",
                details={"index": index, "expected": expected_sha256, "actual": digest},
            )
    else:
        log.warning(
            "Chunk received without client digest: session=%s index=%d",
            session_id,
            index,
        )

    staging = Path(session.staging_dir)
    target = chunk_path(staging, index)
    await asyncio.to_thread(_write_chunk_atomic, target, data)

    # $addToSet keeps this idempotent: a retried chunk does not inflate the
    # count. received_bytes is recomputed rather than incremented for the same
    # reason -- a resend would otherwise double-count.
    already = index in set(session.received_chunks)
    from app.db.client import get_db

    await get_db().upload_sessions.update_one(
        {"_id": session_id},
        {
            "$addToSet": {"received_chunks": index},
            "$set": {
                f"chunk_digests.{index}": digest,
                "updated_at": datetime.now(UTC),
            },
            **({"$inc": {"received_bytes": len(data)}} if not already else {}),
        },
    )
    return await get_session(session_id, owner=owner)


def _write_chunk_atomic(target: Path, data: bytes) -> None:
    """Write to .tmp, fsync, then rename.

    A file named .part is therefore always complete. Without this, a transfer
    interrupted mid-write would leave a truncated chunk that looks valid and
    silently corrupts the assembled file.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    import os

    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


async def complete_session(
    session_id: PydanticObjectId, *, owner: str
) -> tuple[UploadSession, DataObject, str]:
    """Close the session and enqueue assembly.

    Returns (session, object, job_id).
    """
    from app.db.client import get_db
    from app.queue import queue

    session = await get_session(session_id, owner=owner)

    if session.state is UploadState.COMPLETED and session.resulting_object_id:
        obj = await DataObject.get(session.resulting_object_id)
        if obj is not None:
            return session, obj, ""

    missing = missing_chunks(session)
    if missing:
        raise ConflictError(
            f"{len(missing)} chunk(s) still missing",
            details={"missing_chunks": missing[:100], "missing_count": len(missing)},
        )

    # Compare-and-swap the state so two concurrent completes cannot both enqueue
    # assembly for the same session. Allow retrying a FAILED session.
    res = await get_db().upload_sessions.find_one_and_update(
        {
            "_id": session_id,
            "$or": [
                {"state": UploadState.OPEN.value},
                {"state": UploadState.FAILED.value},
            ],
        },
        {"$set": {"state": UploadState.ASSEMBLING.value, "updated_at": datetime.now(UTC)}},
    )
    if res is None:
        raise ConflictError(
            "Upload session is already being completed",
            details={"state": (await get_session(session_id, owner=owner)).state.value},
        )

    obj = DataObject(
        project_id=session.project_id,
        # From the session rather than a parameter: the session already carries
        # the owner create_session resolved, and completing an upload must not
        # be a chance to re-attribute it.
        owner=session.owner,
        name=session.filename,
        status=ObjectStatus.UPLOADING,
        size=session.total_size,
        source=SourceInfo(mode=SourceMode.UPLOAD, original_name=session.filename),
    )
    await obj.insert()

    await get_db().upload_sessions.update_one(
        {"_id": session_id}, {"$set": {"resulting_object_id": obj.id}}
    )

    job = await queue.enqueue(
        "assemble_upload",
        # The object's own owner, inherited from the session just above.
        owner=obj.owner,
        payload={"session_id": str(session_id), "object_id": str(obj.id)},
        # The user just clicked "upload" and is watching a progress bar.
        job_class=JobClass.USER_INTERACTIVE,
        resources=JobResources(cpu=1, mem_mb=256, io=IoClass.HEAVY),
        dedup_key=f"assemble_upload:{session_id}",
        project_id=session.project_id,
        object_id=obj.id,
    )

    return await get_session(session_id, owner=owner), obj, str(job.id) if job else ""


async def abort_session(session_id: PydanticObjectId, *, owner: str) -> None:
    session = await get_session(session_id, owner=owner)
    await session.set(
        {UploadSession.state: UploadState.ABORTED, UploadSession.updated_at: datetime.now(UTC)}
    )
    await asyncio.to_thread(cleanup_staging, session.staging_dir)


def cleanup_staging(staging_dir: str) -> None:
    if not staging_dir:
        return
    path = Path(staging_dir)
    # Refuse to recurse outside the staging tree, however the value got here.
    try:
        path.resolve().relative_to(settings.staging_dir.resolve())
    except ValueError:
        log.error("refusing_cleanup_outside_staging", path=str(path))
        return
    shutil.rmtree(path, ignore_errors=True)


async def list_active_sessions(
    project_id: PydanticObjectId | None = None, *, owner: str, limit: int = 50
) -> list[UploadSession]:
    query: dict = {
        "owner": owner,
        "state": {"$in": [UploadState.OPEN.value, UploadState.ASSEMBLING.value]},
    }
    if project_id:
        query["project_id"] = project_id
    return await UploadSession.find(query).sort("-created_at").limit(limit).to_list()
