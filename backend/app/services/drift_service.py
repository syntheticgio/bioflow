"""Read-only detection of drift between the object records and the filesystem.

Reports; never deletes. A sweep that deletes is a sweep that can delete the
wrong thing because of a bug in the sweep itself, and the value here is
visibility -- see #412 and the design doc.

Category `missing_blob` is deliberately *not* re-derived: `verify_files`
already detects it with a two-strike rule and a whole-batch circuit breaker
that tolerate transiently unmounted external drives. Re-checking here would be
a second, worse implementation of the same thing.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.logging import get_logger
from app.models.blob import Blob, BlobState, BlobStorage
from app.models.drift import DriftCategory, DriftEntry
from app.services.blob_service import GC_GRACE

log = get_logger(__name__)


def _walk_object_files() -> list[Path]:
    """Every regular file under objects/, across the two-level sharding.

    Synchronous: called through asyncio.to_thread so a large tree never blocks
    the event loop, matching reap_report_dirs.
    """
    root = settings.objects_dir
    if not root.exists():
        return []
    found: list[Path] = []
    for shard in root.iterdir():
        if not shard.is_dir():
            continue
        for entry in shard.iterdir():
            if entry.is_file():
                found.append(entry)
    return found


async def find_orphaned_files() -> list[DriftEntry]:
    """Files under objects/ with no usable Blob record.

    Two categories, not one. A file with no record at all is a different
    failure from a file whose record never left PENDING: the first is most
    likely a gc_blobs crash between unlinking the row and unlinking the file,
    the second an ingest that died partway. Same evidence on disk, different
    cause, different fix.

    Record-before-file is the invariant that makes this safe: blob records are
    inserted PENDING *before* bytes are placed, so a file with no record is a
    genuine anomaly rather than a race. A PENDING record younger than GC_GRACE
    is an ingest in flight and is never reported.
    """
    files = await asyncio.to_thread(_walk_object_files)
    if not files:
        return []

    digests = [f.name for f in files]
    records = await Blob.find({"_id": {"$in": digests}}).to_list()
    by_digest = {b.id: b for b in records}

    cutoff = datetime.now(UTC) - GC_GRACE
    entries: list[DriftEntry] = []

    for path in files:
        digest = path.name
        blob = by_digest.get(digest)

        if blob is not None and blob.state is not BlobState.PENDING:
            continue

        if blob is not None:
            updated = blob.updated_at
            if updated is not None and updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if updated is not None and updated > cutoff:
                # An ingest in flight. Not drift.
                continue
            category = DriftCategory.STALLED_INGEST
        else:
            category = DriftCategory.ORPHANED_FILE

        try:
            size = path.stat().st_size
        except OSError:
            # Vanished between the walk and the stat -- the sweep is
            # best-effort and a partial report beats no report.
            continue

        entries.append(
            DriftEntry(
                category=category,
                path=f"{digest[:2]}/{digest}",
                digest=digest,
                size_bytes=size,
            )
        )

    return entries


async def find_missing_blobs() -> list[DriftEntry]:
    """Records whose bytes verify_files has confirmed absent.

    A read of existing detection, not a second implementation of it.
    verify_files requires two consecutive misses at least 60s apart and trips a
    whole-batch circuit breaker when a large fraction of one batch misses, so
    BlobState.MISSING already means "absent, and not merely because a drive
    blinked". Re-statting here would be strictly worse: a single check with
    none of those guards.

    EXTERNAL blobs are excluded. Their bytes live outside BIOINFO_HOME under
    paths we registered but never owned, so a vanished external file is the
    user's business, not reclaimable drift.
    """
    records = await Blob.find(
        Blob.state == BlobState.MISSING,
        Blob.storage == BlobStorage.MANAGED,
    ).to_list()

    return [
        DriftEntry(
            category=DriftCategory.MISSING_BLOB,
            path=blob.rel_path or blob.id,
            digest=blob.id,
            size_bytes=blob.size,
        )
        for blob in records
    ]
