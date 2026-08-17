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
from collections.abc import Iterator
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


# Categories whose bytes are still on disk and could be freed. A missing blob's
# bytes are already gone, so counting them would promise space that does not
# exist.
_RECLAIMABLE = (DriftCategory.ORPHANED_FILE, DriftCategory.STALLED_INGEST)


# How many files one pass holds in memory and asks Mongo about at once.
#
# A whole-tree walk does not scale to the few hundred thousand files the design
# doc names as the target (#499): every Path is held at once against a job
# provisioned at mem_mb=128, and the matching `$in` of every digest blows past
# MongoDB's 16MB BSON command limit long before that -- an opaque
# BSONObjectTooLarge from a job nobody is watching.
#
# 2,000 64-char digests is roughly 150KB of BSON, two orders of magnitude under
# the limit, while keeping the query count low enough that a large tree is not
# dominated by round-trips.
WALK_BATCH_SIZE = 2000


# A cap high enough to mean "keep everything", for a detector called on its own
# rather than through sweep(). Only sweep() cares about the real cap.
_UNCAPPED = 2**62


class DriftAccumulator:
    """Exact counts and reclaimable bytes; capped examples.

    The detectors used to return a full list of every entry found, which
    sweep() then counted, summed, and discarded all but
    MAX_ENTRIES_PER_CATEGORY of. At the few hundred thousand files #499
    targets that discarded list is
    the peak allocation of the whole sweep -- measured at 212MB for 300k
    orphans, against a job provisioned at mem_mb=128 -- so batching the walk
    alone would have moved the ceiling without removing it.

    Counts and `reclaimable_bytes` stay exact above the cap, which is the
    invariant the report has always promised: the number is the actionable
    part, the examples are illustration.
    """

    def __init__(self, cap: int = MAX_ENTRIES_PER_CATEGORY):
        self.cap = cap
        self.counts: dict[str, int] = {}
        self.reclaimable_bytes = 0
        self.entries: list[DriftEntry] = []

    def add(self, entry: DriftEntry) -> None:
        category = entry.category.value
        seen = self.counts.get(category, 0)
        self.counts[category] = seen + 1
        if entry.category in _RECLAIMABLE:
            self.reclaimable_bytes += entry.size_bytes
        if seen < self.cap:
            self.entries.append(entry)


def _iter_object_files(batch_size: int | None = None) -> Iterator[list[Path]]:
    """Regular files under objects/ in fixed-size batches, across the sharding.

    A generator rather than a list so memory stays bounded by `batch_size`
    regardless of tree size. Synchronous: each batch is pulled through
    asyncio.to_thread so a large tree never blocks the event loop, matching
    reap_report_dirs.

    `batch_size` defaults to WALK_BATCH_SIZE read at call time, not bound as a
    default argument -- a module-level default would freeze the value at import
    and leave the constant unable to change it.
    """
    if batch_size is None:
        batch_size = WALK_BATCH_SIZE
    root = settings.objects_dir
    if not root.exists():
        return
    batch: list[Path] = []
    for shard in root.iterdir():
        if not shard.is_dir():
            continue
        for entry in shard.iterdir():
            if not entry.is_file():
                continue
            batch.append(entry)
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch


_WALK_DONE = object()


def _next_batch(batches: Iterator[list[Path]]):
    """One step of the walk generator, for asyncio.to_thread.

    `next()` cannot cross a thread boundary with its StopIteration intact, so
    exhaustion is signalled with a sentinel instead.
    """
    return next(batches, _WALK_DONE)


async def find_orphaned_files(into: DriftAccumulator | None = None) -> list[DriftEntry]:
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

    The tree is walked in batches, one bounded `$in` lookup per batch -- see
    WALK_BATCH_SIZE. Pass `into` to stream findings into a shared
    DriftAccumulator, which caps retained examples so peak memory does not grow
    with the number of orphans; the return value is then the capped list rather
    than every entry found. Called with no accumulator it returns everything,
    which is what the per-detector tests read.
    """
    acc = into if into is not None else DriftAccumulator(cap=_UNCAPPED)
    cutoff = datetime.now(UTC) - GC_GRACE
    before = len(acc.entries)

    batches = _iter_object_files()
    while True:
        files = await asyncio.to_thread(_next_batch, batches)
        if files is _WALK_DONE:
            break

        digests = [f.name for f in files]
        records = await Blob.find({"_id": {"$in": digests}}).to_list()
        by_digest = {b.id: b for b in records}

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

            acc.add(
                DriftEntry(
                    category=category,
                    path=f"{digest[:2]}/{digest}",
                    digest=digest,
                    size_bytes=size,
                )
            )

    return acc.entries[before:]


async def find_missing_blobs(into: DriftAccumulator | None = None) -> list[DriftEntry]:
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

    Streamed rather than `.to_list()`ed (#499): a library whose drive went
    missing has every managed blob in this state at once, and there is no
    reason to hold them all to walk them once.
    """
    acc = into if into is not None else DriftAccumulator(cap=_UNCAPPED)
    before = len(acc.entries)

    async for blob in Blob.find(
        Blob.state == BlobState.MISSING,
        Blob.storage == BlobStorage.MANAGED,
    ):
        acc.add(
            DriftEntry(
                category=DriftCategory.MISSING_BLOB,
                path=blob.rel_path or blob.id,
                digest=blob.id,
                size_bytes=blob.size,
            )
        )

    return acc.entries[before:]


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


async def find_missing_report_dirs(into: DriftAccumulator | None = None) -> list[DriftEntry]:
    """Objects whose facts claim a report whose directory is gone.

    The opposite direction from reap_report_dirs, which finds directories with
    no record. This finds records with no directory -- the one that fails
    late, when a user opens a Results tab the UI offered them.

    Report directories are addressed positionally as <root>/<object_id>/;
    nothing stores a path, so the claim is the predicate fact plus the id.

    Streamed through a projected cursor rather than `.to_list()` of whole
    documents (#499): only `_id` and the one fact being tested are needed, and
    a library with many QC'd objects would otherwise hold every full DataObject
    in memory at once for no gain.
    """
    acc = into if into is not None else DriftAccumulator(cap=_UNCAPPED)
    before = len(acc.entries)
    collection = DataObject.get_pymongo_collection()

    for predicate, root in REPORT_ROOTS.items():
        field = f"facts.{predicate}"
        cursor = collection.find(
            {field: {"$exists": True}},
            projection={"_id": 1, field: 1},
        )
        async for doc in cursor:
            facts = doc.get("facts") or {}
            if not object_claims_report(facts, predicate):
                continue
            object_id = doc["_id"]
            path = root / str(object_id)
            if await asyncio.to_thread(path.exists):
                continue
            acc.add(
                DriftEntry(
                    category=DriftCategory.MISSING_REPORT_DIR,
                    path=str(path),
                    object_id=str(object_id),
                )
            )

    return acc.entries[before:]


async def sweep() -> DriftReport:
    """Run every detector and store the result. Never deletes anything.

    The mount sentinel is checked first, for the same reason verify_files
    checks it: an unmounted external drive presents as an *empty* /data rather
    than an error, so a sweep that ran anyway would report the entire library
    as drift.
    """
    home = check_home()
    if not home.ok:
        report = await DriftReport.load()
        report.swept_at = datetime.now(UTC)
        report.skipped = True
        report.skip_reason = home.detail
        report.counts = {}
        report.entries = []
        report.reclaimable_bytes = 0
        await report.save()
        log.info("drift_sweep_skipped", reason=home.detail)
        return report

    # One accumulator across all three detectors, so nothing ever holds the
    # full set of findings: it caps retained examples per category -- not
    # globally, since a flood of one category must not hide every example of
    # another -- while keeping counts and reclaimable bytes exact.
    acc = DriftAccumulator()
    await find_orphaned_files(into=acc)
    await find_missing_blobs(into=acc)
    await find_missing_report_dirs(into=acc)

    counts = acc.counts
    reclaimable = acc.reclaimable_bytes
    kept = acc.entries

    # Loaded here, immediately before the writes, rather than at the top of
    # the function: the detectors above can run for minutes on a large tree,
    # and loading late narrows the window during which this in-memory copy
    # could go stale before save()'s full-document replace.
    report = await DriftReport.load()
    report.swept_at = datetime.now(UTC)
    report.skipped = False
    report.skip_reason = None
    report.counts = counts
    report.entries = kept
    report.reclaimable_bytes = reclaimable
    await report.save()

    log.info("drift_sweep_complete", counts=counts, reclaimable_bytes=reclaimable)
    return report
