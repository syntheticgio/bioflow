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
from app.models.drift import (
    MAX_ENTRIES_PER_CATEGORY,
    DriftCategory,
    DriftEntry,
    DriftReport,
)
from app.models.object import DataObject
from app.services.blob_service import GC_GRACE
from app.storage.home import check_home

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


# Which fact means "this object has a report", and where that report lives.
#
# Keyed on the fact the *UI* gates each tab on, not on the handler's
# `*_status` fact, and the two are not always the same. That choice is what
# makes this detector match the failure a user actually hits: a visible
# Results tab that fails when opened. Keying on `*_status` would report
# objects the UI never offers a tab for, and miss objects showing a broken
# one.
#
# The predicates are not uniform because the UI's gates are not uniform:
#   qc_tool                  -> typeof facts.qc_tool === "string"  (DetailPanel.tsx:619)
#   bam_stats_summary        -> presence                           (BamResults.tsx:98)
#   vcf_stats_summary        -> presence                           (VariantResults.tsx:42)
#   annotation_stats_status  -> === "ok"                           (AnnotationResults.tsx:40)
REPORT_ROOTS: dict[str, Path] = {
    "qc_tool": settings.qc_reports_dir,
    "bam_stats_summary": settings.bam_stats_dir,
    "vcf_stats_summary": settings.vcf_stats_dir,
    "annotation_stats_status": settings.annotation_stats_dir,
}

# Report status facts that intentionally have no directory. transcript_qc
# stores its results entirely in facts, so there is nothing on disk to drift.
# This is the companion frozenset from CLAUDE.md's "genuinely derivable"
# registry pattern: every status fact is either mapped above or listed here,
# and test_every_status_fact_is_classified fails if a new one is neither.
REPORTS_WITHOUT_DIRS: frozenset[str] = frozenset({"transcript_qc_status"})

# Every fact a handler writes to say a report was computed. Update this when a
# handler grows a new `*_status` fact -- the exhaustiveness test then forces a
# decision about whether it has a directory.
ALL_REPORT_STATUS_FACTS: frozenset[str] = frozenset(
    {
        "qc_tool",
        "bam_stats_summary",
        "vcf_stats_summary",
        "annotation_stats_status",
        "transcript_qc_status",
    }
)


def object_claims_report(facts: dict, predicate: str) -> bool:
    """Whether these facts assert the report behind `predicate` exists.

    Mirrors the frontend's gate for each tab exactly; see REPORT_ROOTS.
    """
    value = facts.get(predicate)
    if predicate == "qc_tool":
        return isinstance(value, str)
    if predicate == "annotation_stats_status":
        return value == "ok"
    return value is not None


async def find_missing_report_dirs() -> list[DriftEntry]:
    """Objects whose facts claim a report whose directory is gone.

    The opposite direction from reap_report_dirs, which finds directories with
    no record. This finds records with no directory -- the one that fails
    late, when a user opens a Results tab the UI offered them.

    Report directories are addressed positionally as <root>/<object_id>/;
    nothing stores a path, so the claim is the predicate fact plus the id.
    """
    entries: list[DriftEntry] = []

    for predicate, root in REPORT_ROOTS.items():
        candidates = await DataObject.find({f"facts.{predicate}": {"$exists": True}}).to_list()
        for obj in candidates:
            if not object_claims_report(obj.facts, predicate):
                continue
            path = root / str(obj.id)
            if await asyncio.to_thread(path.exists):
                continue
            entries.append(
                DriftEntry(
                    category=DriftCategory.MISSING_REPORT_DIR,
                    path=str(path),
                    object_id=str(obj.id),
                )
            )

    return entries


# Categories whose bytes are still on disk and could be freed. A missing blob's
# bytes are already gone, so counting them would promise space that does not
# exist.
_RECLAIMABLE = (DriftCategory.ORPHANED_FILE, DriftCategory.STALLED_INGEST)


async def sweep() -> DriftReport:
    """Run every detector and store the result. Never deletes anything.

    The mount sentinel is checked first, for the same reason verify_files
    checks it: an unmounted external drive presents as an *empty* /data rather
    than an error, so a sweep that ran anyway would report the entire library
    as drift.
    """
    report = await DriftReport.load()
    report.swept_at = datetime.now(UTC)

    home = check_home()
    if not home.ok:
        report.skipped = True
        report.skip_reason = home.detail
        report.counts = {}
        report.entries = []
        report.reclaimable_bytes = 0
        await report.save()
        log.info("drift_sweep_skipped", reason=home.detail)
        return report

    found: list[DriftEntry] = []
    found.extend(await find_orphaned_files())
    found.extend(await find_missing_blobs())
    found.extend(await find_missing_report_dirs())

    counts: dict[str, int] = {}
    for entry in found:
        counts[entry.category.value] = counts.get(entry.category.value, 0) + 1

    reclaimable = sum(e.size_bytes for e in found if e.category in _RECLAIMABLE)

    # Cap per category, not globally: a flood of one category must not hide
    # every example of another. Counts above stay exact.
    kept: list[DriftEntry] = []
    per_category: dict[str, int] = {}
    for entry in found:
        seen = per_category.get(entry.category.value, 0)
        if seen >= MAX_ENTRIES_PER_CATEGORY:
            continue
        per_category[entry.category.value] = seen + 1
        kept.append(entry)

    report.skipped = False
    report.skip_reason = None
    report.counts = counts
    report.entries = kept
    report.reclaimable_bytes = reclaimable
    await report.save()

    log.info("drift_sweep_complete", counts=counts, reclaimable_bytes=reclaimable)
    return report
