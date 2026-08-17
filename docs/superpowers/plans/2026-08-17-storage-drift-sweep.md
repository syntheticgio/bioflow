# Storage Drift Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only maintenance sweep that reports storage drift between Mongo records and the filesystem in five categories, surfaced on a new Settings > Storage page.

**Architecture:** A new `drift_service.py` holds three independently testable detector functions plus a `sweep()` that composes them. A thin scheduled handler wraps it. The result is stored as a Beanie singleton document (the `AppSettings.SINGLETON_ID` pattern) and read by one GET route, rendered by a new settings page. Category 2 (records with no files) reuses `verify_files`' existing `BlobState.MISSING` rather than re-deriving it.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (Mongo), pytest, React 18 + TypeScript, TanStack Query, react-router-dom.

**Spec:** [`docs/superpowers/specs/2026-08-17-storage-drift-sweep-design.md`](../specs/2026-08-17-storage-drift-sweep-design.md)

**Issue:** [#412](https://github.com/syntheticgio/bioflow/issues/412)

## Global Constraints

- **Report only. No deletion of any kind** (spec DS-16). This version never unlinks, never `rmtree`s, never writes to a blob or object record.
- **Never report an `EXTERNAL` blob** (spec DS-7). `BlobStorage.EXTERNAL` files are registered in place, live outside `BIOINFO_HOME`, and are never ours to reclaim.
- **Only `objects_dir` and the four report roots are walked** (spec DS-8). Never `staging_dir`, `tmp_dir`, `ncbi_dir`, `lineages_dir`, `agent_sessions_dir`.
- **`GC_GRACE`** is `timedelta(hours=1)`, imported from `app.services.blob_service`. Do not introduce a second grace constant.
- **Conventional Commits** subjects, imperative mood, lowercase after the colon, no trailing period, scope from the existing set (`api`, `frontend`, `services`). See `CLAUDE.md`.
- **Tests run from this worktree** with `./backend/run-worktree-tests.sh`, never `docker compose exec api python -m pytest` (which silently tests main's code).
- Blob records are inserted `PENDING` **before** bytes are placed (`blob_service.create_blob_record`). Record-before-file is the invariant every detector relies on.

---

## File Structure

**Create:**
- `backend/app/models/drift.py` — the stored sweep result document + entry/category types
- `backend/app/services/drift_service.py` — three detectors + `sweep()`
- `backend/app/api/v1/maintenance.py` — one GET route
- `backend/tests/services/test_drift_service.py` — detector unit tests
- `backend/tests/services/test_drift_registry.py` — the two registry exhaustiveness tests
- `backend/tests/api/test_maintenance.py` — route test
- `frontend/src/components/SettingsStorage.tsx` — the settings page

**Modify:**
- `backend/app/models/__init__.py` — export the new model
- `backend/app/queue/handlers.py` — add the `sweep_storage_drift` handler
- `backend/app/queue/scheduler.py` — add the schedule + resources entry
- `backend/app/db/init.py` (or wherever Beanie document models are registered) — register the new document
- `backend/app/api/v1/__init__.py` — include the new router
- `frontend/src/components/SettingsNav.tsx` — add the Storage nav item
- `frontend/src/App.tsx` (or the settings route file) — add the `/settings/storage` route

---

### Task 1: The stored result model

**Files:**
- Create: `backend/app/models/drift.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/services/test_drift_service.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `DriftCategory` (StrEnum with members `ORPHANED_FILE`, `STALLED_INGEST`, `MISSING_BLOB`, `MISSING_REPORT_DIR`), `DriftEntry` (BaseModel: `category: DriftCategory`, `path: str`, `object_id: str | None`, `digest: str | None`, `size_bytes: int`), `DriftReport` (Document singleton: `SINGLETON_ID`, `swept_at: datetime`, `skipped: bool`, `skip_reason: str | None`, `counts: dict[str, int]`, `entries: list[DriftEntry]`, `reclaimable_bytes: int`, plus `async classmethod load()`)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_drift_service.py`:

```python
import pytest

from app.models.drift import DriftCategory, DriftEntry, DriftReport

pytestmark = pytest.mark.asyncio


class TestDriftReportModel:
    async def test_load_creates_the_singleton_when_absent(self):
        report = await DriftReport.load()
        assert report.id == DriftReport.SINGLETON_ID
        assert report.counts == {}
        assert report.entries == []
        assert report.reclaimable_bytes == 0

    async def test_load_returns_the_same_document_twice(self):
        first = await DriftReport.load()
        first.reclaimable_bytes = 4096
        await first.save()

        second = await DriftReport.load()
        assert second.reclaimable_bytes == 4096

    async def test_entry_carries_its_category_and_size(self):
        entry = DriftEntry(
            category=DriftCategory.ORPHANED_FILE,
            path="ab/abc123",
            size_bytes=1024,
        )
        assert entry.category is DriftCategory.ORPHANED_FILE
        assert entry.object_id is None
        assert entry.digest is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.drift'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/models/drift.py`:

```python
"""The stored result of the storage drift sweep.

Exactly one document, like `AppSettings`: the sweep reports current state, and
#412 asks for a list to look at rather than a trend. History would cost a
growing collection to answer a question nobody asked.
"""

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from beanie import Document
from pydantic import BaseModel, Field

from app.models.base import utcnow

# Entry lists are capped so a pathological drift state cannot produce an
# unbounded document. Counts stay exact above the cap -- the number is the
# actionable part, and 500 examples is already more than anyone reads.
MAX_ENTRIES_PER_CATEGORY = 500


class DriftCategory(StrEnum):
    # A file under objects/ with no Blob record at all.
    ORPHANED_FILE = "orphaned_file"
    # A file whose Blob record is still PENDING past GC_GRACE: an ingest that
    # started and never finished. Distinct from ORPHANED_FILE because the
    # cause and the fix differ.
    STALLED_INGEST = "stalled_ingest"
    # A Blob record verify_files has confirmed absent. Not re-derived here.
    MISSING_BLOB = "missing_blob"
    # An object claiming a report whose directory is gone.
    MISSING_REPORT_DIR = "missing_report_dir"


class DriftEntry(BaseModel):
    category: DriftCategory
    path: str
    object_id: str | None = None
    digest: str | None = None
    size_bytes: int = 0


class DriftReport(Document):
    """The latest sweep. Upserted on first read, like `AppSettings.load`."""

    SINGLETON_ID: ClassVar[str] = "drift_report"

    id: str = Field(default=SINGLETON_ID)

    swept_at: datetime = Field(default_factory=utcnow)
    # True when the storage home was not mounted, in which case every blob
    # would look missing and the sweep refuses to draw conclusions.
    skipped: bool = False
    skip_reason: str | None = None

    counts: dict[str, int] = Field(default_factory=dict)
    entries: list[DriftEntry] = Field(default_factory=list)
    reclaimable_bytes: int = 0

    @classmethod
    async def load(cls) -> "DriftReport":
        found = await cls.get(cls.SINGLETON_ID)
        if found is not None:
            return found
        created = cls()
        await created.insert()
        return created

    class Settings:
        name = "drift_report"
```

- [ ] **Step 4: Register the model**

In `backend/app/models/__init__.py`, add to the imports and `__all__` alongside the existing model exports:

```python
from app.models.drift import DriftCategory, DriftEntry, DriftReport
```

Then add `DriftReport` to the `ALL_MODELS` list in the same file. That list is what `db/client.py:94` passes to `init_beanie` as `document_models`, and it is the single registration point — the module docstring says so explicitly. Without it, `DriftReport.load()` raises `CollectionWasNotInitialized`.

- [ ] **Step 5: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/drift.py backend/app/models/__init__.py backend/tests/services/test_drift_service.py
git commit -m "feat(api): add the stored drift report document"
```

---

### Task 2: Detector 1 — orphaned files and stalled ingests

**Files:**
- Create: `backend/app/services/drift_service.py`
- Test: `backend/tests/services/test_drift_service.py`

**Interfaces:**
- Consumes: `DriftCategory`, `DriftEntry` from Task 1
- Produces: `async def find_orphaned_files() -> list[DriftEntry]` — returns entries in categories `ORPHANED_FILE` and `STALLED_INGEST`

Implements spec **DS-1**, **DS-2**, **DS-3**, **DS-8**.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_drift_service.py`:

```python
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.models.blob import Blob, BlobState, BlobStorage
from app.services import drift_service

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def _place_blob_file(digest: str, content: bytes = b"hello") -> None:
    """Write a file where a managed blob of this digest would live."""
    path = settings.objects_dir / digest[:2] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class TestFindOrphanedFiles:
    async def test_file_with_no_blob_record_is_an_orphan(self):
        _place_blob_file(DIGEST_A)

        entries = await drift_service.find_orphaned_files()

        orphans = [e for e in entries if e.category is DriftCategory.ORPHANED_FILE]
        assert [e.digest for e in orphans] == [DIGEST_A]
        assert orphans[0].size_bytes == 5

    async def test_old_pending_record_is_a_stalled_ingest(self):
        _place_blob_file(DIGEST_B)
        stale = datetime.now(UTC) - timedelta(hours=3)
        await Blob(
            id=DIGEST_B,
            size=5,
            state=BlobState.PENDING,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_B[:2]}/{DIGEST_B}",
            created_at=stale,
            updated_at=stale,
        ).insert()

        entries = await drift_service.find_orphaned_files()

        stalled = [e for e in entries if e.category is DriftCategory.STALLED_INGEST]
        assert [e.digest for e in stalled] == [DIGEST_B]

    async def test_recent_pending_record_is_not_reported(self):
        """An ingest in flight right now. The whole false-positive guard."""
        _place_blob_file(DIGEST_C)
        await Blob(
            id=DIGEST_C,
            size=5,
            state=BlobState.PENDING,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_C[:2]}/{DIGEST_C}",
        ).insert()

        entries = await drift_service.find_orphaned_files()

        assert entries == []

    async def test_present_record_is_not_reported(self):
        _place_blob_file(DIGEST_A)
        await Blob(
            id=DIGEST_A,
            size=5,
            state=BlobState.PRESENT,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_A[:2]}/{DIGEST_A}",
        ).insert()

        entries = await drift_service.find_orphaned_files()

        assert entries == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py::TestFindOrphanedFiles -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.drift_service'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/drift_service.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py::TestFindOrphanedFiles -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/drift_service.py backend/tests/services/test_drift_service.py
git commit -m "feat(api): detect orphaned files and stalled ingests under objects/"
```

---

### Task 3: Detector 2 — missing blobs, reusing verify_files

**Files:**
- Modify: `backend/app/services/drift_service.py`
- Test: `backend/tests/services/test_drift_service.py`

**Interfaces:**
- Consumes: `DriftEntry`, `DriftCategory` from Task 1
- Produces: `async def find_missing_blobs() -> list[DriftEntry]` — entries in category `MISSING_BLOB`

Implements spec **DS-4**, **DS-7**.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_drift_service.py`:

```python
class TestFindMissingBlobs:
    async def test_missing_managed_blob_is_reported(self):
        await Blob(
            id=DIGEST_A,
            size=2048,
            state=BlobState.MISSING,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_A[:2]}/{DIGEST_A}",
        ).insert()

        entries = await drift_service.find_missing_blobs()

        assert [e.digest for e in entries] == [DIGEST_A]
        assert entries[0].category is DriftCategory.MISSING_BLOB
        assert entries[0].size_bytes == 2048

    async def test_missing_external_blob_is_never_reported(self):
        """Registered in place; outside BIOINFO_HOME, never ours to reclaim."""
        await Blob(
            id=DIGEST_B,
            size=2048,
            state=BlobState.MISSING,
            storage=BlobStorage.EXTERNAL,
            external_path="/somewhere/else/reads.fastq",
        ).insert()

        entries = await drift_service.find_missing_blobs()

        assert entries == []

    async def test_present_blob_is_not_reported(self):
        await Blob(
            id=DIGEST_C,
            size=2048,
            state=BlobState.PRESENT,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_C[:2]}/{DIGEST_C}",
        ).insert()

        entries = await drift_service.find_missing_blobs()

        assert entries == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py::TestFindMissingBlobs -v`
Expected: FAIL with `AttributeError: module 'app.services.drift_service' has no attribute 'find_missing_blobs'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/drift_service.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py::TestFindMissingBlobs -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/drift_service.py backend/tests/services/test_drift_service.py
git commit -m "feat(api): report blobs verify_files has confirmed missing"
```

---

### Task 4: The report-root registry and its exhaustiveness tests

**Files:**
- Modify: `backend/app/services/drift_service.py`
- Test: `backend/tests/services/test_drift_registry.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `REPORT_ROOTS: dict[str, Path]` (predicate fact name → report root), `REPORTS_WITHOUT_DIRS: frozenset[str]`, `ALL_REPORT_STATUS_FACTS: frozenset[str]`, `def object_claims_report(facts: dict, predicate: str) -> bool`

Implements spec **DS-9**, **DS-10**, **DS-11**, **DS-12**.

This task is the registry, separated from its consumer (Task 5) because the exhaustiveness tests are the point: per `CLAUDE.md`, a registry keyed by something an enum already enumerates silently skips members it has no entry for, and only a test catches it.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_drift_registry.py`:

```python
"""Exhaustiveness for the report-root registry.

The failure this guards against is silent: a new report type nobody adds to
REPORT_ROOTS is simply never checked for drift, with nothing raising. Same
shape as the STAR/_SIDECAR_ROLES failure described in CLAUDE.md.
"""

from app.config import settings
from app.services import drift_service


class TestReportRootRegistry:
    def test_every_status_fact_is_classified(self):
        classified = set(drift_service.REPORT_ROOTS) | drift_service.REPORTS_WITHOUT_DIRS
        assert drift_service.ALL_REPORT_STATUS_FACTS == classified

    def test_no_fact_is_both_mapped_and_excluded(self):
        overlap = set(drift_service.REPORT_ROOTS) & drift_service.REPORTS_WITHOUT_DIRS
        assert overlap == set()

    def test_every_mapped_root_is_a_real_report_root(self):
        known = {
            settings.qc_reports_dir,
            settings.bam_stats_dir,
            settings.vcf_stats_dir,
            settings.annotation_stats_dir,
        }
        assert set(drift_service.REPORT_ROOTS.values()) <= known

    def test_each_root_is_claimed_by_exactly_one_predicate(self):
        roots = list(drift_service.REPORT_ROOTS.values())
        assert len(roots) == len(set(roots))


class TestClaimsReport:
    def test_qc_predicate_requires_a_string(self):
        assert drift_service.object_claims_report({"qc_tool": "fastp"}, "qc_tool")
        assert not drift_service.object_claims_report({"qc_tool": None}, "qc_tool")
        assert not drift_service.object_claims_report({}, "qc_tool")

    def test_annotation_predicate_requires_status_ok(self):
        fact = "annotation_stats_status"
        assert drift_service.object_claims_report({fact: "ok"}, fact)
        assert not drift_service.object_claims_report({fact: "failed"}, fact)
        assert not drift_service.object_claims_report({}, fact)

    def test_summary_predicates_require_presence(self):
        fact = "bam_stats_summary"
        assert drift_service.object_claims_report({fact: {"mean_depth": 30}}, fact)
        assert not drift_service.object_claims_report({fact: None}, fact)
        assert not drift_service.object_claims_report({}, fact)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_registry.py -v`
Expected: FAIL with `AttributeError: module 'app.services.drift_service' has no attribute 'REPORT_ROOTS'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/drift_service.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_registry.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/drift_service.py backend/tests/services/test_drift_registry.py
git commit -m "feat(api): map each report predicate fact to its report root"
```

---

### Task 5: Detector 3 — missing report directories

**Files:**
- Modify: `backend/app/services/drift_service.py`
- Test: `backend/tests/services/test_drift_service.py`

**Interfaces:**
- Consumes: `REPORT_ROOTS`, `object_claims_report` from Task 4; `DriftEntry`, `DriftCategory` from Task 1
- Produces: `async def find_missing_report_dirs() -> list[DriftEntry]` — entries in category `MISSING_REPORT_DIR`

Implements spec **DS-5**.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_drift_service.py`:

```python
from app.models.object import DataObject


async def _make_object(facts: dict) -> DataObject:
    from beanie import PydanticObjectId

    obj = DataObject(
        project_id=PydanticObjectId(),
        name="sample.fastq.gz",
        facts=facts,
    )
    await obj.insert()
    return obj


class TestFindMissingReportDirs:
    async def test_object_claiming_qc_with_no_directory_is_reported(self):
        obj = await _make_object({"qc_tool": "fastp"})

        entries = await drift_service.find_missing_report_dirs()

        assert [e.object_id for e in entries] == [str(obj.id)]
        assert entries[0].category is DriftCategory.MISSING_REPORT_DIR

    async def test_object_with_its_directory_present_is_not_reported(self):
        obj = await _make_object({"qc_tool": "fastp"})
        (settings.qc_reports_dir / str(obj.id)).mkdir(parents=True, exist_ok=True)

        entries = await drift_service.find_missing_report_dirs()

        assert entries == []

    async def test_object_claiming_no_report_is_not_reported(self):
        await _make_object({"qc_total_reads": 1000})

        entries = await drift_service.find_missing_report_dirs()

        assert entries == []

    async def test_failed_annotation_status_is_not_a_claim(self):
        """`annotation_stats_status` gates on == "ok", matching the UI."""
        await _make_object({"annotation_stats_status": "failed"})

        entries = await drift_service.find_missing_report_dirs()

        assert entries == []

    async def test_transcript_qc_has_no_directory_and_is_never_reported(self):
        await _make_object({"transcript_qc_status": "ok"})

        entries = await drift_service.find_missing_report_dirs()

        assert entries == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py::TestFindMissingReportDirs -v`
Expected: FAIL with `AttributeError: module 'app.services.drift_service' has no attribute 'find_missing_report_dirs'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/drift_service.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py::TestFindMissingReportDirs -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/drift_service.py backend/tests/services/test_drift_service.py
git commit -m "feat(api): detect report directories missing from claiming objects"
```

---

### Task 6: Compose the sweep

**Files:**
- Modify: `backend/app/services/drift_service.py`
- Test: `backend/tests/services/test_drift_service.py`

**Interfaces:**
- Consumes: all three detectors from Tasks 2, 3, 5; `DriftReport`, `MAX_ENTRIES_PER_CATEGORY` from Task 1
- Produces: `async def sweep() -> DriftReport` — runs all detectors, stores and returns the singleton

Implements spec **DS-6**, **DS-13** (the service half), **DS-16**, and the `check_home` guard.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_drift_service.py`:

```python
from unittest.mock import patch

from app.storage.home import HomeStatus


class TestSweep:
    async def test_sweep_counts_each_category_and_sums_reclaimable(self):
        _place_blob_file(DIGEST_A, b"0123456789")  # orphan, 10 bytes
        await Blob(
            id=DIGEST_B,
            size=2048,
            state=BlobState.MISSING,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_B[:2]}/{DIGEST_B}",
        ).insert()

        report = await drift_service.sweep()

        assert report.counts["orphaned_file"] == 1
        assert report.counts["missing_blob"] == 1
        # Only categories 1 and 2 are reclaimable: a missing blob's bytes are
        # already gone, so counting them would promise space that does not exist.
        assert report.reclaimable_bytes == 10

    async def test_sweep_is_stored_and_readable(self):
        _place_blob_file(DIGEST_A)

        await drift_service.sweep()
        stored = await DriftReport.load()

        assert stored.counts["orphaned_file"] == 1
        assert stored.swept_at is not None

    async def test_sweep_skips_when_home_is_not_mounted(self):
        """Every blob looks missing when the drive is gone."""
        with patch(
            "app.services.drift_service.check_home",
            return_value=HomeStatus(False, "sentinel missing", "/data"),
        ):
            report = await drift_service.sweep()

        assert report.skipped is True
        assert report.skip_reason == "sentinel missing"
        assert report.counts == {}

    async def test_entries_are_capped_but_counts_stay_exact(self):
        for i in range(drift_service.MAX_ENTRIES_PER_CATEGORY + 5):
            digest = f"{i:064x}"
            _place_blob_file(digest)

        report = await drift_service.sweep()

        assert report.counts["orphaned_file"] == drift_service.MAX_ENTRIES_PER_CATEGORY + 5
        capped = [e for e in report.entries if e.category is DriftCategory.ORPHANED_FILE]
        assert len(capped) == drift_service.MAX_ENTRIES_PER_CATEGORY
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py::TestSweep -v`
Expected: FAIL with `AttributeError: module 'app.services.drift_service' has no attribute 'sweep'`

- [ ] **Step 3: Write minimal implementation**

Add the import at the top of `backend/app/services/drift_service.py`:

```python
from app.models.drift import (
    MAX_ENTRIES_PER_CATEGORY,
    DriftCategory,
    DriftEntry,
    DriftReport,
)
from app.storage.home import check_home
```

(Replace the existing `from app.models.drift import ...` line with the above.)

Then append:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py -v`
Expected: PASS (all classes)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/drift_service.py backend/tests/services/test_drift_service.py
git commit -m "feat(api): compose the drift detectors into one stored sweep"
```

---

### Task 7: The scheduled handler

**Files:**
- Modify: `backend/app/queue/handlers.py`
- Modify: `backend/app/queue/scheduler.py`
- Test: `backend/tests/services/test_drift_service.py`

**Interfaces:**
- Consumes: `drift_service.sweep` from Task 6
- Produces: job type `"sweep_storage_drift"`, handler `async def sweep_storage_drift(ctx: JobContext) -> dict`

Implements spec **DS-13**, **DS-15**.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_drift_service.py`:

```python
class TestSweepHandler:
    def _ctx(self):
        """Minimal JobContext stand-in: the handler only needs check_cancel."""
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.payload = {}
        ctx.check_cancel = MagicMock(return_value=None)
        return ctx

    async def test_handler_runs_the_sweep_and_returns_counts(self):
        from app.queue.handlers import sweep_storage_drift

        _place_blob_file(DIGEST_A)

        result = await sweep_storage_drift(self._ctx())

        assert result["counts"]["orphaned_file"] == 1
        assert "reclaimable_bytes" in result

    async def test_handler_is_registered_with_the_queue(self):
        from app.queue.registry import get_handler

        assert get_handler("sweep_storage_drift") is not None

    async def test_schedule_and_resources_are_seeded(self):
        from app.queue.scheduler import DEFAULT_SCHEDULES, RESOURCES

        ids = {s["_id"] for s in DEFAULT_SCHEDULES}
        assert "sweep_storage_drift" in ids
        assert "sweep_storage_drift" in RESOURCES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py::TestSweepHandler -v`
Expected: FAIL with `ImportError: cannot import name 'sweep_storage_drift'`

`get_handler(name) -> HandlerSpec | None` is defined at `backend/app/queue/registry.py:200`.

- [ ] **Step 3: Add the handler**

In `backend/app/queue/handlers.py`, add near `reap_report_dirs` (before the trailing pipeline-handler imports at the bottom of the file):

```python
@handler(
    "sweep_storage_drift",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.MAINTENANCE,
    resources=JobResources(cpu=1, mem_mb=128, io=IoClass.LIGHT),
    max_attempts=2,
)
async def sweep_storage_drift(ctx: JobContext) -> dict:
    """Report drift between object records and the filesystem. Never deletes.

    A thin wrapper: the detection lives in drift_service so it can be tested
    without going through the queue, matching how reap_report_dirs delegates
    its deletion to object_service.remove_report_dirs.
    """
    from app.services import drift_service

    ctx.check_cancel()
    report = await drift_service.sweep()
    return {
        "skipped": report.skipped,
        "counts": report.counts,
        "reclaimable_bytes": report.reclaimable_bytes,
    }
```

- [ ] **Step 4: Add the schedule**

In `backend/app/queue/scheduler.py`, append to `DEFAULT_SCHEDULES`:

```python
    {
        "_id": "sweep_storage_drift",
        # Every six hours. Drift is caused by crashes and interrupted writes,
        # not by ordinary use, so it accumulates slowly -- and the sweep walks
        # the whole objects/ tree, which is the most expensive maintenance
        # read in the system. Nothing here is urgent: the report exists so a
        # user can look deliberately, not so they are told immediately.
        "job_type": "sweep_storage_drift",
        "interval_seconds": 21600,
        "job_class": JobClass.MAINTENANCE,
        "payload": {},
    },
```

And to `RESOURCES`:

```python
    "sweep_storage_drift": JobResources(cpu=1, mem_mb=128, io=IoClass.LIGHT),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_drift_service.py::TestSweepHandler -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Check the provenance walker**

`backend/app/services/provenance_walker.py:182` lists maintenance job types that should not appear in an object's provenance narrative (it includes `reap_report_dirs`). Read that list and add `"sweep_storage_drift"` if the surrounding code's intent is "maintenance jobs are not provenance" — this sweep touches nothing, so it must never appear in a lineage.

- [ ] **Step 7: Commit**

```bash
git add backend/app/queue/handlers.py backend/app/queue/scheduler.py backend/app/services/provenance_walker.py backend/tests/services/test_drift_service.py
git commit -m "feat(api): schedule the storage drift sweep every six hours"
```

---

### Task 8: The read route

**Files:**
- Create: `backend/app/api/v1/maintenance.py`
- Modify: `backend/app/api/v1/__init__.py`
- Test: `backend/tests/api/test_maintenance.py`

**Interfaces:**
- Consumes: `DriftReport` from Task 1
- Produces: `GET /api/v1/maintenance/drift` returning `{swept_at, skipped, skip_reason, counts, entries, reclaimable_bytes}`

Implements spec **DS-14** (the API half).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_maintenance.py`:

```python
import pytest

from app.models.drift import DriftCategory, DriftEntry, DriftReport

pytestmark = pytest.mark.asyncio


class TestDriftRoute:
    async def test_returns_the_stored_report(self, client):
        report = await DriftReport.load()
        report.counts = {"orphaned_file": 2}
        report.reclaimable_bytes = 8192
        report.entries = [
            DriftEntry(
                category=DriftCategory.ORPHANED_FILE,
                path="ab/abc",
                digest="a" * 64,
                size_bytes=4096,
            )
        ]
        await report.save()

        res = await client.get("/api/v1/maintenance/drift")

        assert res.status_code == 200
        body = res.json()
        assert body["counts"]["orphaned_file"] == 2
        assert body["reclaimable_bytes"] == 8192
        assert body["entries"][0]["category"] == "orphaned_file"

    async def test_returns_an_empty_report_before_any_sweep(self, client):
        res = await client.get("/api/v1/maintenance/drift")

        assert res.status_code == 200
        assert res.json()["counts"] == {}
```

The async `client` fixture is defined at `backend/tests/api/conftest.py:21`; these tests use it as-is.

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/api/test_maintenance.py -v`
Expected: FAIL with 404

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/api/v1/maintenance.py`:

```python
"""Read-only maintenance reporting.

Not owner-scoped, matching `settings.py`: there is one machine and one
filesystem here, so a profile header cannot change the answer.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.models.drift import DriftEntry, DriftReport

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


class DriftReportOut(BaseModel):
    swept_at: str
    skipped: bool
    skip_reason: str | None
    counts: dict[str, int]
    entries: list[DriftEntry]
    reclaimable_bytes: int


@router.get("/drift", response_model=DriftReportOut)
async def get_drift_report() -> DriftReportOut:
    """The most recent sweep. Reading never triggers one.

    The sweep walks the whole objects/ tree, so running it inside a request
    would block that request for as long as the walk takes. It runs on its
    schedule; this returns whatever it last stored.
    """
    report = await DriftReport.load()
    return DriftReportOut(
        swept_at=report.swept_at.isoformat(),
        skipped=report.skipped,
        skip_reason=report.skip_reason,
        counts=report.counts,
        entries=report.entries,
        reclaimable_bytes=report.reclaimable_bytes,
    )
```

- [ ] **Step 4: Register the router**

In `backend/app/api/v1/__init__.py`, add the import alongside the others and include it near `settings.router`:

```python
api_router.include_router(maintenance.router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/api/test_maintenance.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/maintenance.py backend/app/api/v1/__init__.py backend/tests/api/test_maintenance.py
git commit -m "feat(api): serve the latest storage drift report"
```

---

### Task 9: The Settings > Storage page

**Files:**
- Create: `frontend/src/components/SettingsStorage.tsx`
- Modify: `frontend/src/components/SettingsNav.tsx`
- Modify: the settings route file (grep for `"/settings/resources"` to find where routes are declared)

**Interfaces:**
- Consumes: `GET /api/v1/maintenance/drift` from Task 8
- Produces: the `/settings/storage` route

Implements spec **DS-14**.

There is no headless component-testing setup in this repo (no jsdom, zero `.test.tsx` files) and none is expected — verification for this task is manual, in the browser, per `CLAUDE.md`.

- [ ] **Step 1: Write the component**

Create `frontend/src/components/SettingsStorage.tsx`:

```tsx
import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";

type DriftEntry = {
  category: string;
  path: string;
  object_id: string | null;
  digest: string | null;
  size_bytes: number;
};

type DriftReport = {
  swept_at: string;
  skipped: boolean;
  skip_reason: string | null;
  counts: Record<string, number>;
  entries: DriftEntry[];
  reclaimable_bytes: number;
};

/**
 * What each category means, in the user's terms rather than the schema's.
 * The label answers "what happened"; the hint answers "what should I do".
 */
const CATEGORIES: { key: string; label: string; hint: string }[] = [
  {
    key: "orphaned_file",
    label: "Files with no record",
    hint: "Disk space used by files nothing points at. Safe to ignore; not yet reclaimable here.",
  },
  {
    key: "stalled_ingest",
    label: "Unfinished ingests",
    hint: "An upload or import started and never completed. Re-importing the file is the fix.",
  },
  {
    key: "missing_blob",
    label: "Records with no file",
    hint: "These will fail when a pipeline tries to read them. Re-import the source file.",
  },
  {
    key: "missing_report_dir",
    label: "Missing results",
    hint: "The object offers a Results tab whose data is gone. Re-run the analysis to restore it.",
  },
];

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

export function SettingsStorage() {
  const report = useQuery({
    queryKey: ["maintenance", "drift"],
    queryFn: () => api.get<DriftReport>("/maintenance/drift"),
  });

  if (report.isLoading) return <p className="muted">Loading…</p>;
  if (report.error) return <p className="error">Could not load the storage report.</p>;

  const data = report.data;
  if (!data) return null;

  const total = Object.values(data.counts).reduce((a, b) => a + b, 0);

  return (
    <section className="settings-storage">
      <h2>Storage</h2>
      <p className="muted">
        A read-only check that the database and the files on disk still agree.
        Nothing here deletes anything.
      </p>

      {data.skipped ? (
        <p className="warning">
          The last check did not run: {data.skip_reason ?? "storage unavailable"}
        </p>
      ) : (
        <>
          <p className="muted">
            Last checked {new Date(data.swept_at).toLocaleString()}
          </p>

          {total === 0 ? (
            <p>No drift found. The database and the filesystem agree.</p>
          ) : (
            <>
              <p>
                {formatBytes(data.reclaimable_bytes)} in files that are no longer
                referenced.
              </p>
              <dl className="drift-categories">
                {CATEGORIES.map((c) => (
                  <div key={c.key}>
                    <dt>
                      {c.label}: {data.counts[c.key] ?? 0}
                    </dt>
                    <dd className="muted">{c.hint}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}
        </>
      )}
    </section>
  );
}
```

- [ ] **Step 2: Add the nav item**

In `frontend/src/components/SettingsNav.tsx`, add to the `items` array after Resources:

```tsx
    { to: "/settings/storage", label: "Storage" },
```

- [ ] **Step 3: Add the route**

Find where `/settings/resources` is routed (grep `frontend/src` for `settings/resources`) and add a sibling route rendering `<SettingsStorage />` at `/settings/storage`, matching the surrounding style exactly.

- [ ] **Step 4: Verify in the browser**

Bring up this worktree's stack and check the page renders:

```bash
./ops/worktree-up.sh
```

Open `http://localhost:5273/settings/storage`. Confirm: the page loads, the nav item is highlighted, and with no sweep yet run it shows "No drift found". Then create drift deliberately and re-check (Task 10).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/SettingsStorage.tsx frontend/src/components/SettingsNav.tsx
git commit -m "feat(frontend): add a Storage page reporting database and disk drift"
```

---

### Task 10: Verify against real data

**Files:** none — this is the acceptance gate from the spec.

Per `CLAUDE.md`: a rule that passes its unit tests can still be wrong on real objects, because the fixtures were built to look the way the rules expect. The suggestion-rules precedent is the warning. This task is not optional.

- [ ] **Step 1: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Read the count, not the exit code. Every test must pass.

- [ ] **Step 2: Trigger a sweep against the worktree stack's real data**

With the worktree stack up (`./ops/worktree-up.sh`, which seeds a copy of the main database):

```bash
docker compose -p bioflow-issue-356-f07efd exec api python -c "import asyncio; from app.services import drift_service; print(asyncio.run(drift_service.sweep()).counts)"
```

Use the actual project name `worktree-up.sh` reports if it differs.

- [ ] **Step 3: Confirm zero false positives**

This is the acceptance bar from #412: a report that cries wolf will be ignored. Inspect every reported entry against reality. Any `missing_report_dir` entry must correspond to an object whose Results tab genuinely fails to open; any `orphaned_file` must genuinely have no record. If a category reports entries that are actually fine, fix the detector before proceeding.

- [ ] **Step 4: Create each drift condition deliberately and confirm it is caught**

Against the worktree stack:

- Delete a blob file out from under a `PRESENT` record, wait for `verify_files` to mark it `MISSING` (two passes, ≥60s apart), re-sweep → `missing_blob`
- Drop a file into `objects/ab/` with a syntactically valid but unknown digest name → `orphaned_file`
- `rm -rf` a `qc_reports/<object_id>/` for an object whose Facts show `qc_tool` → `missing_report_dir`

Confirm each lands in the right category and that the Storage page displays it.

- [ ] **Step 5: Bring the stack down**

```bash
./ops/worktree-up.sh --down
```

A stack you brought up for testing is yours to bring back down.

- [ ] **Step 6: Commit any fixes**

```bash
git add -A
git commit -m "fix(api): correct drift detection found against real data"
```

(Skip if steps 3 and 4 needed no changes.)

---

### Task 11: Close out the issue

**Files:**
- Modify: `docs/TODO.md` and `docs/TODO-done.md` (only if an entry covers this)

- [ ] **Step 1: Check for a TODO entry**

The issue mentions "an open TODO entry about QC report directories vanishing from disk while the object's facts still point at them". Find it:

```bash
grep -n -i "report dir\|qc report\|vanish" docs/TODO.md
```

If an entry exists and this work resolves it, append ` — FIXED` to its heading, write a short note saying what shipped and where the code lives, note what the implementation did differently from the entry's own diagnosis, and move the whole entry to `docs/TODO-done.md`. If it is only partially resolved, leave it in `docs/TODO.md` — moving it would bury the still-open part.

- [ ] **Step 2: Rebase onto main**

```bash
git fetch origin main
git rebase origin/main
```

- [ ] **Step 3: Verify the work survived the rebase**

```bash
git diff origin/main...HEAD --stat
```

Confirm the file list matches what this plan touched, and skim for anything reverted.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

The PR description must carry the why, not just the what, and must include `Closes #412`. Label it `type:feature`, `area:backend`, `area:infrastructure` — `.github/release.yml` categorizes by label, and an unlabelled PR lands under "Other changes".

- [ ] **Step 5: Watch CI and merge once green**

Poll until every check reports pass:

```bash
gh pr checks <N>
```

```bash
gh pr view <N> --json mergeable,mergeStateStatus
```

Fix anything red (CI's `ruff check` catches import-order rules the local run does not). Once every check passes and `mergeable` is `MERGEABLE`:

```bash
gh pr merge <N> --rebase --delete-branch
```

`--rebase`, not `--squash`: `CHANGELOG.md` is generated from commit subjects *and bodies*, and a squash concatenates the bodies under one subject.

- [ ] **Step 6: Remove the worktree**

Bring down anything still up, then remove the worktree per `CLAUDE.md`.

---

## Self-Review

**Spec coverage:**

| Requirement | Task |
|---|---|
| DS-1 orphaned_file | 2 |
| DS-2 stalled_ingest | 2 |
| DS-3 young PENDING not reported | 2 |
| DS-4 missing_blob from verify_files | 3 |
| DS-5 missing_report_dir | 5 |
| DS-6 reclaimable_bytes | 6 |
| DS-7 EXTERNAL excluded | 3 (test + query filter) |
| DS-8 only objects_dir + report roots walked | 2, 5 |
| DS-9 UI-keyed mapping | 4 |
| DS-10 transcript_qc companion set | 4 |
| DS-11 exhaustiveness test | 4 |
| DS-12 UI/handler predicate agreement | 4 |
| DS-13 scheduled, not in-request | 7, 8 |
| DS-14 settings page | 8, 9 |
| DS-15 check_cancel | 7 |
| DS-16 no deletion | Global constraint; no task writes a delete |

**Note on DS-12:** Task 4's `TestClaimsReport` asserts each predicate's shape matches the UI gate. A stricter reading of DS-12 — a fixture object per report type where the handler ran, asserting the UI predicate and the `*_status` fact agree — is worth adding if the handlers' fact-writing is easy to invoke in a test. Task 4's tests satisfy the requirement's intent; extend them if the fuller version proves cheap.

**Placeholder scan:** No TBDs. Every symbol the plan tells the implementer to reach for has been verified to exist at the named location: `ALL_MODELS` in `models/__init__.py` (Task 1), `get_handler` at `registry.py:200` (Task 7), the `client` fixture at `tests/api/conftest.py:21` (Task 8), `GC_GRACE` at `blob_service.py:24`, `check_home` at `storage/home.py:145`. One step still directs a grep — the settings route file in Task 9 — because routing is declared in a file this plan did not need to open; the nav file it sits beside is named exactly.

**Type consistency:** `DriftEntry`, `DriftCategory`, `DriftReport`, `MAX_ENTRIES_PER_CATEGORY` are defined in Task 1 and used with those exact names in Tasks 2–8. `find_orphaned_files`, `find_missing_blobs`, `find_missing_report_dirs`, `sweep` keep consistent names across Tasks 2, 3, 5, 6, 7. `REPORT_ROOTS`, `REPORTS_WITHOUT_DIRS`, `ALL_REPORT_STATUS_FACTS`, `object_claims_report` are defined in Task 4 and consumed in Task 5.
