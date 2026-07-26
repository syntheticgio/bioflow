"""Content-addressed storage: hashing, placement, refcounting, and unlinking.

Placement is idempotent by construction -- the destination path is derived from
the content, so writing the same bytes twice converges. Deletion is the
dangerous direction: several objects may reference one blob, so nothing is
unlinked without a refcount at zero plus a grace period.

Blocking file I/O in this module is synchronous by design. Callers must run it
off the event loop (see queue/executor.py thread mode, or asyncio.to_thread).
"""

import hashlib
import os
import shutil
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.config import settings
from app.errors import JobCancelled
from app.logging import get_logger
from app.storage.paths import blob_path, blob_rel_path, validate_sha256

log = get_logger(__name__)

# 4 MiB balances syscall overhead against page-cache pressure. Larger buffers
# showed no measurable gain across a FUSE mount.
READ_BUFFER = 4 * 1024 * 1024
# Cancellation is checked on this boundary, bounding cancel latency to the time
# it takes to read ~8 MiB (tens of ms even on a slow mount).
CANCEL_CHECK_BYTES = 8 * 1024 * 1024


class PlacementResult(StrEnum):
    CREATED = "created"  # new content, file moved into the store
    DEDUP = "dedup"  # identical content already present; our copy discarded


@dataclass
class Placement:
    result: PlacementResult
    digest: str
    path: Path
    size: int


def hash_file(
    path: Path,
    *,
    cancel_event: threading.Event | None = None,
    progress_cb=None,
) -> tuple[str, int]:
    """Stream a file and return (sha256_hex, size).

    hashlib releases the GIL for buffers above ~2 KiB and file reads release it
    too, so running this in a thread genuinely overlaps with the event loop --
    a process pool would only add IPC cost.
    """
    h = hashlib.sha256()
    size = 0
    since_check = 0

    with open(path, "rb") as f:
        while chunk := f.read(READ_BUFFER):
            h.update(chunk)
            size += len(chunk)
            since_check += len(chunk)
            if since_check >= CANCEL_CHECK_BYTES:
                since_check = 0
                if cancel_event is not None and cancel_event.is_set():
                    raise JobCancelled("Cancelled during hashing")
                if progress_cb is not None:
                    progress_cb(size)

    if progress_cb is not None:
        progress_cb(size)
    return h.hexdigest(), size


def write_stream_to_temp(chunks, dest_dir: Path | None = None) -> tuple[Path, str, int]:
    """Write an iterable of byte chunks to a temp file, hashing in the same pass.

    One pass matters: re-reading a 100 GB file across VirtioFS to hash it would
    double an already slow operation for no benefit.
    """
    dest_dir = dest_dir or settings.tmp_dir
    dest_dir.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = _mkstemp(dest_dir)
    tmp_path = Path(tmp_name)
    h = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in chunks:
                if not chunk:
                    continue
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path, h.hexdigest(), size


def _mkstemp(directory: Path) -> tuple[int, str]:
    import tempfile

    return tempfile.mkstemp(dir=directory, prefix="ingest-", suffix=".part")


def place_blob(source: Path, digest: str, size: int) -> Placement:
    """Move an already-hashed file into the object store.

    The final rename is atomic within a filesystem, which is why startup asserts
    that tmp/, staging/ and objects/ share one -- see storage/home.py.
    """
    digest = validate_sha256(digest)
    final = blob_path(digest)

    if final.exists():
        existing_size = final.stat().st_size
        if existing_size == size:
            # Same content already stored. Discard our copy.
            source.unlink(missing_ok=True)
            return Placement(PlacementResult.DEDUP, digest, final, size)
        # A size mismatch under a content-addressed name means the stored file
        # is corrupt (or, vanishingly, a hash collision). Never overwrite it
        # silently -- move it aside so the evidence survives.
        _quarantine(final, reason="size_mismatch", expected=size, actual=existing_size)

    final.parent.mkdir(parents=True, exist_ok=True)
    # Read-only: cheap insurance against a future pipeline writing into an input.
    # Not a security boundary, especially over VirtioFS.
    try:
        os.chmod(source, 0o444)
    except OSError as e:
        log.warning("chmod_failed", path=str(source), error=str(e))

    os.rename(source, final)
    _fsync_dir(final.parent)
    log.info("blob_placed", digest=digest, size=size)
    return Placement(PlacementResult.CREATED, digest, final, size)


def _quarantine(path: Path, *, reason: str, **details) -> None:
    quarantine_dir = settings.meta_dir / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    target = quarantine_dir / f"{path.name}.{reason}"
    try:
        shutil.move(str(path), str(target))
        log.error("blob_quarantined", path=str(path), moved_to=str(target), **details)
    except OSError as e:
        log.error("blob_quarantine_failed", path=str(path), error=str(e))


def _fsync_dir(path: Path) -> None:
    """Persist the directory entry itself, not just the file contents."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        # Directory fsync is unreliable across FUSE. Acceptable for a
        # single-user local tool; the refcount ledger tolerates a lost tail.
        log.debug("dir_fsync_failed", path=str(path), error=str(e))


def unlink_blob(digest: str) -> bool:
    """Remove a managed blob's bytes. Callers must verify refcount first."""
    path = blob_path(digest)
    try:
        # chmod first: the file is 0444, and some filesystems refuse unlink of
        # a read-only file depending on the parent's permissions.
        os.chmod(path, 0o644)
    except OSError:
        pass
    try:
        path.unlink()
        log.info("blob_unlinked", digest=digest, rel_path=blob_rel_path(digest))
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.error("blob_unlink_failed", digest=digest, error=str(e))
        return False
