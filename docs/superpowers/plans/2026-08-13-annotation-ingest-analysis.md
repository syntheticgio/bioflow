# Automatic annotation analysis at ingest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every ingested annotation file analyze itself automatically, so the first visit to its Results tab shows results rather than a "Compute results" button.

**Architecture:** Two trigger sites added to the tail of `_apply_ingest_headers`, both calling #257's existing `launch_annotation_stats` unchanged — one when an annotation becomes READY, one when a REFERENCE FASTA becomes READY (which backfills annotations that were analyzed before their reference existed). One new fact, `annotation_contig_lengths_known`, makes the backfill query a flat match and lets the UI explain missing coverage. No new handler, parser, job type, or endpoint.

**Tech Stack:** Python 3.14, FastAPI, Beanie/Motor (MongoDB), pytest (async), React + TypeScript.

**Spec:** [`docs/superpowers/specs/2026-08-13-annotation-ingest-analysis-design.md`](../specs/2026-08-13-annotation-ingest-analysis-design.md)

---

## Background you need

**Where things live.** `_apply_ingest_headers` (`backend/app/queue/results.py:99`) runs when a file finishes ingesting; it writes status/format/facts, assigns the REFERENCE role, calls `_link_mate`, and logs `ingest_applied`. That log call is the tail of the function and the anchor for everything here.

`launch_annotation_stats` (`backend/app/services/pipeline_service.py:2488`) already resolves the file path and the reference contig lengths, then enqueues with `dedup_key=f"annotation_stats:{ann.id}"`. **You are not changing what it does** — only adding one fact to the payload it builds.

**Why two triggers.** `resolve_annotation_reference` only considers candidate references with `status=READY, role=REFERENCE`. An NCBI download stages `genomic.fna` and `genomic.gff` concurrently, and the FASTA's REFERENCE role lands *after* a network lookup. So an annotation that finishes ingesting first resolves no reference and gets analyzed with no contig lengths — coverage null everywhere, no track axis, and nothing saying why. Trigger 2 repairs that.

**The sidecar guard is the highest-risk part of this change.** On the author's real database, 8 of 13 objects with an annotation format are `.fai` and STAR `.ann` sidecars misdetected as BED. All carry `sidecar_of != None`. Without the guard, every reference ingest queues 8 junk jobs. Task 2's second test is the one that catches a broken guard — a test asserting real annotations *do* trigger passes whether or not the guard works.

**Running tests.** You are in a worktree. Use the worktree runner, never `docker compose exec api pytest` (that silently tests main's code):

```bash
./backend/run-worktree-tests.sh tests/path -q
```

Paths below are relative to the repo root unless they start with `tests/`, in which case they are relative to `backend/`.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `backend/app/services/pipeline_service.py` | The R5 eligibility predicate + the payload flag | Modify |
| `backend/app/queue/annotation_handlers.py` | Return `annotation_contig_lengths_known` in facts | Modify |
| `backend/app/queue/results.py` | Both triggers, as one helper called from `_apply_ingest_headers` | Modify |
| `backend/tests/services/test_annotation_autoanalysis_eligibility.py` | Predicate tests | Create |
| `backend/tests/pipelines/test_annotation_contig_lengths_fact.py` | Fact round-trip tests | Create |
| `backend/tests/queue/test_annotation_ingest_triggers.py` | Both triggers, incl. the sidecar guard | Create |
| `frontend/src/api/types.ts` | Declare the new fact | Modify |
| `frontend/src/components/AnnotationResults.tsx` | The R9 "no reference" note | Modify |

Task order is dependency order: the predicate and the fact are independent of each other, but the triggers depend on both.

---

### Task 1: The eligibility predicate (R5)

The rule deciding whether an object should be auto-analyzed. It lives in `pipeline_service.py` beside the other annotation predicates, and is pure and synchronous so it can be tested without a queue or a database.

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (after `_check_annotation_stats_callable`, ~line 2486)
- Test: `backend/tests/services/test_annotation_autoanalysis_eligibility.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_annotation_autoanalysis_eligibility.py`:

```python
"""Which objects auto-analyze at ingest.

The sidecar case is the one that matters. On a real database, every object
whose format.kind is BED was a .fai or a STAR .ann index misdetected as BED
-- 8 of them, all carrying sidecar_of. Without that exclusion every
reference ingest queues 8 jobs that each write a database of garbage
intervals, and no test that only checks real annotations would notice.
"""

from types import SimpleNamespace

import pytest

from app.models.object import FormatKind
from app.services.pipeline_service import should_auto_analyze_annotation


def _obj(kind=FormatKind.GFF, sidecar_of=None, facts=None):
    return SimpleNamespace(kind=kind, sidecar_of=sidecar_of, facts=facts or {})


class TestFormat:
    @pytest.mark.parametrize(
        "kind",
        [FormatKind.GFF, FormatKind.GTF, FormatKind.BED, FormatKind.GENBANK],
    )
    def test_every_annotation_format_qualifies(self, kind):
        o = _obj(kind=kind)
        assert should_auto_analyze_annotation(
            kind=o.kind, sidecar_of=o.sidecar_of, facts=o.facts
        ) is True

    @pytest.mark.parametrize(
        "kind", [FormatKind.BAM, FormatKind.VCF, FormatKind.FASTA, FormatKind.FASTQ]
    )
    def test_non_annotation_formats_do_not(self, kind):
        assert should_auto_analyze_annotation(
            kind=kind, sidecar_of=None, facts={}
        ) is False


class TestSidecarGuard:
    def test_a_sidecar_never_qualifies(self):
        # A .fai misdetected as BED. This is the direction that fails when
        # the guard breaks.
        assert should_auto_analyze_annotation(
            kind=FormatKind.BED, sidecar_of="507f1f77bcf86cd799439011", facts={}
        ) is False

    @pytest.mark.parametrize(
        "kind",
        [FormatKind.GFF, FormatKind.GTF, FormatKind.BED, FormatKind.GENBANK],
    )
    def test_the_guard_applies_to_every_format(self, kind):
        assert should_auto_analyze_annotation(
            kind=kind, sidecar_of="507f1f77bcf86cd799439011", facts={}
        ) is False


class TestAlreadyAnalyzed:
    def test_a_never_analyzed_annotation_qualifies(self):
        assert should_auto_analyze_annotation(
            kind=FormatKind.GFF, sidecar_of=None, facts={}
        ) is True

    def test_one_analyzed_without_a_reference_qualifies_for_repair(self):
        assert should_auto_analyze_annotation(
            kind=FormatKind.GFF,
            sidecar_of=None,
            facts={
                "annotation_stats_status": "ok",
                "annotation_contig_lengths_known": False,
            },
        ) is True

    def test_a_fully_analyzed_annotation_does_not(self):
        assert should_auto_analyze_annotation(
            kind=FormatKind.GFF,
            sidecar_of=None,
            facts={
                "annotation_stats_status": "ok",
                "annotation_contig_lengths_known": True,
            },
        ) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_annotation_autoanalysis_eligibility.py -q
```

Expected: collection error — `ImportError: cannot import name 'should_auto_analyze_annotation'`.

- [ ] **Step 3: Write the predicate**

In `backend/app/services/pipeline_service.py`, immediately after `_check_annotation_stats_callable` ends (before `async def launch_annotation_stats`):

```python
def should_auto_analyze_annotation(
    *, kind: FormatKind, sidecar_of, facts: dict
) -> bool:
    """Whether ingest should analyze this object without being asked.

    Public and parameter-wise rather than object-wise so both trigger sites
    and their tests can call it without constructing a DataObject.

    The sidecar exclusion is load-bearing, not defensive. Every object on
    this machine's real database whose format.kind is BED is a `.fai` or a
    STAR `.ann` index that the detector called BED -- 8 of them. A `.fai` is
    not an annotation however it parses, and analyzing one writes a database
    of garbage intervals under a name nobody will recognize.

    A file already analyzed *with* a reference is skipped; one analyzed
    without a reference is not, because that is exactly the result trigger 2
    exists to repair.
    """
    if kind not in _ANNOTATION_STATS_FORMATS:
        return False
    if sidecar_of is not None:
        return False
    if not facts.get("annotation_stats_status"):
        return True
    return facts.get("annotation_contig_lengths_known") is not True
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_annotation_autoanalysis_eligibility.py -q
```

Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_annotation_autoanalysis_eligibility.py
git commit -m "feat(pipelines): add the auto-analysis eligibility rule for annotations"
```

---

### Task 2: Record whether a reference was resolved (R7, R8)

`launch_annotation_stats` already computes `lengths`. This records whether it found any, so the backfill query is a flat match and the UI can explain missing coverage.

**Files:**
- Modify: `backend/app/services/pipeline_service.py:2519-2545` (the `lengths` block and the payload)
- Modify: `backend/app/queue/annotation_handlers.py` (the `facts` dict)
- Test: `backend/tests/pipelines/test_annotation_contig_lengths_fact.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_annotation_contig_lengths_fact.py`:

```python
"""annotation_contig_lengths_known round-trips launcher -> payload -> facts.

The fact exists so the ingest backfill can find annotations analyzed before
their reference was READY with a flat equality match, rather than an
$elemMatch over annotation_per_contig -- an array holding one entry per
contig, thousands of them on a scaffolded assembly. It also distinguishes
"no reference at all" from "reference resolved but missing this contig",
which are different situations that should not be repaired the same way.
"""

import pytest

from app.models.object import FormatKind, ObjectRole, ObjectStatus
from app.queue import queue as queue_module
from app.services import pipeline_service


class _Obj:
    def __init__(self, oid, kind=FormatKind.GFF, role=None, facts=None,
                 derived_from=None, project_id="p1", owner="o",
                 status=ObjectStatus.READY):
        self.id = oid
        self.format = type("F", (), {"kind": kind})()
        self.role = role
        self.facts = facts or {}
        self.derived_from = derived_from or []
        self.project_id = project_id
        self.owner = owner
        self.status = status
        self.name = str(oid)


@pytest.fixture
def wired(monkeypatch):
    """Same stub set as test_annotation_stats_reference_wiring.py."""
    objects: dict = {}
    listed: list = []
    enqueued: dict = {}

    async def fake_get_object(object_id, *, owner):
        return objects[object_id]

    async def fake_get(oid):
        return objects.get(oid)

    async def fake_list_objects(project_id, *, owner, limit=500, status=None):
        return listed

    async def fake_resolve_readable(obj):
        return None, "/tmp/ann.gff3"

    async def fake_enqueue(name, **kwargs):
        enqueued.update(kwargs)
        return type("Job", (), {"id": "job1"})()

    monkeypatch.setattr(pipeline_service.object_service, "get_object", fake_get_object)
    monkeypatch.setattr(pipeline_service.object_service, "list_objects", fake_list_objects)
    monkeypatch.setattr(pipeline_service.DataObject, "get", staticmethod(fake_get))
    monkeypatch.setattr(pipeline_service, "_resolve_readable", fake_resolve_readable)
    monkeypatch.setattr(pipeline_service, "_check_annotation_stats_callable", lambda ann: None)
    monkeypatch.setattr(queue_module, "enqueue", fake_enqueue)

    return objects, listed, enqueued


class TestLauncherRecordsIt:
    async def test_true_when_a_reference_resolves(self, wired):
        objects, listed, enqueued = wired
        genome = _Obj("gen", kind=FormatKind.FASTA, role=ObjectRole.REFERENCE,
                      facts={"ncbi_assembly_accession": "GCF_9.1",
                             "sequence_lengths": {"chr1": 1000}})
        listed.append(genome)
        objects["ann"] = _Obj("ann", facts={"ncbi_assembly_accession": "GCF_9.1"})

        await pipeline_service.launch_annotation_stats(object_id="ann", owner="o")

        assert enqueued["payload"]["contig_lengths_known"] is True

    async def test_false_when_nothing_resolves(self, wired):
        # The race this whole feature exists to survive: the annotation is
        # ready before its genome is.
        objects, listed, enqueued = wired
        objects["ann"] = _Obj("ann", facts={})

        await pipeline_service.launch_annotation_stats(object_id="ann", owner="o")

        assert enqueued["payload"]["contig_lengths_known"] is False


class TestHandlerReturnsIt:
    def test_facts_carry_the_flag(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.queue import annotation_handlers
        from app.queue.registry import JobContext

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        source = tmp_path / "a.gff"
        source.write_text(
            "##gff-version 3\n"
            "chr1\t.\tgene\t100\t900\t.\t+\t.\tID=g1;Name=BRCA1\n"
        )
        ctx = JobContext(
            job_id="j1",
            payload={
                "object_id": "507f1f77bcf86cd799439011",
                "format_kind": "gff",
                "annotation_path": str(source),
                "contig_lengths": [["chr1", 1000]],
                "contig_lengths_known": True,
            },
            epoch=1,
            attempts=1,
            owner="local",
        )

        result = annotation_handlers.run_annotation_stats(ctx)

        assert result["facts"]["annotation_contig_lengths_known"] is True

    def test_flag_is_false_when_the_payload_says_so(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.queue import annotation_handlers
        from app.queue.registry import JobContext

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        source = tmp_path / "a.gff"
        source.write_text(
            "##gff-version 3\n"
            "chr1\t.\tgene\t100\t900\t.\t+\t.\tID=g1;Name=BRCA1\n"
        )
        ctx = JobContext(
            job_id="j2",
            payload={
                "object_id": "507f1f77bcf86cd799439012",
                "format_kind": "gff",
                "annotation_path": str(source),
                "contig_lengths": [],
                "contig_lengths_known": False,
            },
            epoch=1,
            attempts=1,
            owner="local",
        )

        result = annotation_handlers.run_annotation_stats(ctx)

        assert result["facts"]["annotation_contig_lengths_known"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_contig_lengths_fact.py -q
```

Expected: 4 failures — `KeyError: 'contig_lengths_known'` on the launcher tests, and `KeyError: 'annotation_contig_lengths_known'` on the handler tests.

- [ ] **Step 3: Record it on the payload**

In `backend/app/services/pipeline_service.py`, the payload dict built in `launch_annotation_stats` (~line 2526) currently reads:

```python
    payload: dict = {
        "object_id": str(ann.id),
        "project_id": str(ann.project_id),
        "format_kind": str(ann.format.kind.value),
        "contig_lengths": [[name, length] for name, length in lengths.items()],
        "annotation_path": path,
    }
```

Add one entry:

```python
    payload: dict = {
        "object_id": str(ann.id),
        "project_id": str(ann.project_id),
        "format_kind": str(ann.format.kind.value),
        "contig_lengths": [[name, length] for name, length in lengths.items()],
        # Recorded rather than re-derived from contig_lengths downstream:
        # ingest's backfill queries this as a flat field, and an $elemMatch
        # over annotation_per_contig could not tell "no reference resolved"
        # apart from "reference resolved but missing this contig".
        "contig_lengths_known": bool(lengths),
        "annotation_path": path,
    }
```

- [ ] **Step 4: Return it from the handler**

In `backend/app/queue/annotation_handlers.py`, the `facts` dict near the end of `run_annotation_stats` currently starts:

```python
    facts = {
        "annotation_stats_status": "ok",
        **acc.finish(),
```

Insert the flag after `annotation_stats_status`:

```python
    facts = {
        "annotation_stats_status": "ok",
        # From the payload, not from `contig_lengths` being non-empty: a
        # GenBank file states its own lengths on each LOCUS line and needs no
        # reference, so emptiness of the payload's list is not the same
        # question. The launcher is the only place that knows whether a
        # reference was looked for and found.
        "annotation_contig_lengths_known": bool(
            ctx.payload.get("contig_lengths_known")
        ),
        **acc.finish(),
```

**Note for GenBank:** a GenBank file sets its own contig lengths from LOCUS lines after the DB is built, so it can have real coverage while `contig_lengths_known` is False. That would make the backfill re-analyze GenBank files needlessly. Fix it by overriding the flag in the GenBank branch — find the existing block:

```python
    if fmt == "genbank":
        parsed_lengths = extra_facts.pop("_contig_lengths", None)
        assert parsed_lengths is not None, (
            "_genbank_rows must be fully consumed before this point"
        )
        if parsed_lengths:
            acc.set_contig_lengths(parsed_lengths)
```

and add one line inside the `if parsed_lengths:` body:

```python
        if parsed_lengths:
            acc.set_contig_lengths(parsed_lengths)
            # A GenBank file carries its own lengths, so it is not waiting on
            # a reference and must not be re-analyzed by ingest's backfill.
            extra_facts["annotation_contig_lengths_known"] = True
```

`extra_facts` is spread into `facts` after the literal keys, so this override wins.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_contig_lengths_fact.py -q
```

Expected: PASS, 4 tests.

- [ ] **Step 6: Run the existing annotation suite for regressions**

```bash
./backend/run-worktree-tests.sh tests/pipelines/ tests/services/test_annotation_stats_reference_wiring.py -q
```

Expected: all pass. The payload gained a key; nothing asserts on the payload's exact key set.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/queue/annotation_handlers.py backend/tests/pipelines/test_annotation_contig_lengths_fact.py
git commit -m "feat(pipelines): record whether an annotation analysis resolved a reference"
```

---

### Task 3: Trigger 1 — analyze an annotation at its own ingest (R1, R2, R3, R10, R12)

**Files:**
- Modify: `backend/app/queue/results.py` (new helper + a call at the end of `_apply_ingest_headers`, after the `ingest_applied` log at ~line 226)
- Test: `backend/tests/queue/test_annotation_ingest_triggers.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/queue/test_annotation_ingest_triggers.py`:

```python
"""Ingest launches annotation analysis without being asked.

Two triggers, because one loses a race. An NCBI download stages a genome
and its GFF concurrently and the FASTA's REFERENCE role is assigned after a
network lookup, so an annotation finishing first resolves no reference and
is analyzed with no contig lengths -- null coverage, no track axis, and
nothing saying why. Trigger 2 repairs that when the reference lands.
"""

import pytest
from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models.object import (
    DataObject,
    FormatInfo,
    FormatKind,
    ObjectRole,
    ObjectStatus,
)
from app.queue import results as results_mod

PROJECT = "507f1f77bcf86cd799439011"


@pytest.fixture
async def _db():
    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=[DataObject])
    await DataObject.delete_all()
    yield db
    await DataObject.delete_all()
    await client.close()


@pytest.fixture
def launched(monkeypatch):
    """Capture every object id handed to launch_annotation_stats."""
    calls: list = []

    async def fake_launch(*, object_id, owner):
        calls.append(str(object_id))
        return type("Job", (), {"id": "job1"})()

    from app.services import pipeline_service

    monkeypatch.setattr(pipeline_service, "launch_annotation_stats", fake_launch)
    return calls


async def _obj(
    name,
    kind,
    *,
    status=ObjectStatus.READY,
    role=None,
    sidecar_of=None,
    facts=None,
) -> DataObject:
    o = DataObject(
        project_id=PROJECT,
        name=name,
        size=1024,
        status=status,
        role=role,
        sidecar_of=sidecar_of,
        format=FormatInfo(kind=kind),
        facts=facts or {},
    )
    await o.insert()
    return o


class TestTriggerOne:
    @pytest.mark.parametrize(
        "kind",
        [FormatKind.GFF, FormatKind.GTF, FormatKind.BED, FormatKind.GENBANK],
    )
    async def test_an_ingested_annotation_is_analyzed(self, _db, launched, kind):
        ann = await _obj(f"a.{kind.value}", kind)

        await results_mod._auto_analyze_after_ingest(ann, owner="local")

        assert launched == [str(ann.id)]

    async def test_a_sidecar_is_not_analyzed(self, _db, launched):
        # A .fai misdetected as BED -- 8 of these sit on the author's real
        # database. This is the direction that fails when the guard breaks.
        ref = await _obj("g.fna", FormatKind.FASTA)
        fai = await _obj("g.fna.fai", FormatKind.BED, sidecar_of=ref.id)

        await results_mod._auto_analyze_after_ingest(fai, owner="local")

        assert launched == []

    async def test_a_non_annotation_is_not_analyzed(self, _db, launched):
        bam = await _obj("s.bam", FormatKind.BAM)

        await results_mod._auto_analyze_after_ingest(bam, owner="local")

        assert launched == []


class TestFailureIsolation:
    async def test_a_failing_launch_leaves_the_object_ready(self, _db, monkeypatch):
        from app.errors import ValidationError
        from app.services import pipeline_service

        async def boom(*, object_id, owner):
            raise ValidationError("no")

        monkeypatch.setattr(pipeline_service, "launch_annotation_stats", boom)
        ann = await _obj("a.gff", FormatKind.GFF)

        # Must not raise: a malformed annotation cannot turn a successfully
        # stored source file into an ingest failure.
        await results_mod._auto_analyze_after_ingest(ann, owner="local")

        again = await DataObject.get(ann.id)
        assert again.status is ObjectStatus.READY
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_ingest_triggers.py -q
```

Expected: failures — `AttributeError: module 'app.queue.results' has no attribute '_auto_analyze_after_ingest'`.

- [ ] **Step 3: Write the helper**

In `backend/app/queue/results.py`, add immediately before `async def _link_mate` (~line 237):

```python
async def _auto_analyze_after_ingest(obj: DataObject, *, owner: str) -> None:
    """Analyze a freshly ingested annotation without being asked.

    #257 made annotation results an on-demand computation, which left every
    newly ingested annotation opening to a button. This closes that, at a
    measured 0.83s for a 12.5MB GFF -- queued, so the object is READY long
    before the job runs.

    Never raises. The object is already READY by the time this is called and
    must stay that way: a malformed annotation is a failed recoverable
    computation, not a failed ingest. The Results tab falls back to its
    "Compute results" button and the failure shows in the Computations
    panel like any other job.
    """
    from app.errors import AppError
    from app.services import pipeline_service

    if not pipeline_service.should_auto_analyze_annotation(
        kind=obj.format.kind, sidecar_of=obj.sidecar_of, facts=obj.facts
    ):
        return

    try:
        await pipeline_service.launch_annotation_stats(object_id=obj.id, owner=owner)
    except AppError as e:
        log.warning(
            "annotation_autoanalysis_failed", object_id=str(obj.id), error=str(e)
        )
```

- [ ] **Step 4: Call it from the applier**

In `_apply_ingest_headers`, after the `log.info("ingest_applied", ...)` call that ends the function (~line 226-234), add:

```python
    # After the log, so an annotation that cannot be queued still records a
    # completed ingest. `obj` carries the pre-update snapshot, so re-read the
    # fields the eligibility rule needs from what was actually written.
    obj.format = update.get(DataObject.format, obj.format)
    obj.facts = update.get(DataObject.facts, obj.facts)
    await _auto_analyze_after_ingest(obj, owner=owner)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_ingest_triggers.py -q
```

Expected: PASS, 6 tests.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/results.py backend/tests/queue/test_annotation_ingest_triggers.py
git commit -m "feat(pipelines): analyze an annotation when it finishes ingesting"
```

**On R13 and R15** (the object reaches READY without waiting for its analysis; the applier does not grow with the number of annotations): both hold by construction, because `launch_annotation_stats` enqueues rather than runs. Keep it that way. If you ever find yourself awaiting the analysis itself here, or calling the handler directly to "make the test simpler", you have reintroduced exactly what #257 rejected — a full-file parse inline in ingest, where a pathological file stalls the upload instead of failing a retryable job.

---

### Task 4: Trigger 2 — backfill when a reference lands (R4, R5, R11, R14)

**Files:**
- Modify: `backend/app/queue/results.py` (extend `_auto_analyze_after_ingest`)
- Test: `backend/tests/queue/test_annotation_ingest_triggers.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/queue/test_annotation_ingest_triggers.py`:

```python
class TestTriggerTwo:
    async def test_a_reference_backfills_a_never_analyzed_annotation(
        self, _db, launched
    ):
        ann = await _obj("a.gff", FormatKind.GFF)
        ref = await _obj("g.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert launched == [str(ann.id)]

    async def test_a_reference_repairs_a_referenceless_analysis(self, _db, launched):
        # The race: this annotation was analyzed before its genome existed,
        # so its coverage is null everywhere.
        ann = await _obj(
            "a.gff",
            FormatKind.GFF,
            facts={
                "annotation_stats_status": "ok",
                "annotation_contig_lengths_known": False,
            },
        )
        ref = await _obj("g.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert launched == [str(ann.id)]

    async def test_a_fully_analyzed_annotation_is_left_alone(self, _db, launched):
        await _obj(
            "a.gff",
            FormatKind.GFF,
            facts={
                "annotation_stats_status": "ok",
                "annotation_contig_lengths_known": True,
            },
        )
        ref = await _obj("g.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert launched == []

    async def test_backfill_skips_sidecars(self, _db, launched):
        # The 8 misdetected .fai/.ann files must not be swept up by the
        # backfill either -- the guard has to hold on both trigger paths.
        parent = await _obj("g.fna", FormatKind.FASTA)
        await _obj("g.fna.fai", FormatKind.BED, sidecar_of=parent.id)
        ref = await _obj("ref.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert launched == []

    async def test_a_fasta_without_the_reference_role_backfills_nothing(
        self, _db, launched
    ):
        await _obj("a.gff", FormatKind.GFF)
        plain = await _obj("protein.faa", FormatKind.FASTA)

        await results_mod._auto_analyze_after_ingest(plain, owner="local")

        assert launched == []

    async def test_backfill_is_scoped_to_the_reference_s_project(self, _db, launched):
        other = DataObject(
            project_id="507f1f77bcf86cd799439099",
            name="elsewhere.gff",
            size=10,
            status=ObjectStatus.READY,
            format=FormatInfo(kind=FormatKind.GFF),
        )
        await other.insert()
        ref = await _obj("g.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert launched == []

    async def test_one_failing_backfill_does_not_stop_the_others(
        self, _db, monkeypatch
    ):
        from app.errors import ValidationError
        from app.services import pipeline_service

        seen: list = []

        async def flaky(*, object_id, owner):
            seen.append(str(object_id))
            if len(seen) == 1:
                raise ValidationError("no")
            return type("Job", (), {"id": "j"})()

        monkeypatch.setattr(pipeline_service, "launch_annotation_stats", flaky)
        await _obj("a1.gff", FormatKind.GFF)
        await _obj("a2.gff", FormatKind.GFF)
        ref = await _obj("g.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert len(seen) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_ingest_triggers.py -q -k TriggerTwo
```

Expected: 6 of 7 fail with `assert [] == ['<id>']` (nothing backfills yet). `test_a_fully_analyzed_annotation_is_left_alone`, `test_backfill_skips_sidecars`, `test_a_fasta_without_the_reference_role_backfills_nothing`, and `test_backfill_is_scoped_to_the_reference_s_project` may pass vacuously — they assert an empty list. That is expected; they become meaningful once the backfill exists.

- [ ] **Step 3: Extend the helper**

Replace the body of `_auto_analyze_after_ingest` in `backend/app/queue/results.py` with:

```python
async def _auto_analyze_after_ingest(obj: DataObject, *, owner: str) -> None:
    """Analyze annotations without being asked, from either direction.

    Two triggers, because one loses a race. Analyzing an annotation when it
    finishes ingesting is right whenever its reference is already READY --
    but `resolve_annotation_reference` filters candidates to
    `status=READY, role=REFERENCE`, and an NCBI download stages a genome and
    its GFF concurrently with the FASTA's role assigned only after a network
    lookup. An annotation winning that race is analyzed with no contig
    lengths: null coverage on every contig, no track axis, and nothing
    saying why. So a reference landing also sweeps the project for
    annotations that wanted one.

    That sweep is deliberately not restricted to referenceless analyses --
    it also picks up never-analyzed files, which backfills annotations
    ingested before this feature existed. The dedup key makes the redundancy
    free.

    Never raises, and one failure never stops the rest. The object is
    already READY by the time this runs and must stay that way: a malformed
    annotation is a failed recoverable computation, not a failed ingest.
    """
    from app.errors import AppError
    from app.services import pipeline_service

    async def _launch(target: DataObject) -> None:
        try:
            await pipeline_service.launch_annotation_stats(
                object_id=target.id, owner=owner
            )
        except AppError as e:
            log.warning(
                "annotation_autoanalysis_failed",
                object_id=str(target.id),
                error=str(e),
            )

    if pipeline_service.should_auto_analyze_annotation(
        kind=obj.format.kind, sidecar_of=obj.sidecar_of, facts=obj.facts
    ):
        await _launch(obj)
        return

    # Trigger 2. Only a reference gives an annotation the coordinate axis it
    # was missing, so a plain FASTA (protein.faa, cds_from_genomic.fna) is
    # not a reason to re-analyze anything.
    if obj.format.kind is not FormatKind.FASTA or obj.role is not ObjectRole.REFERENCE:
        return

    # One project-scoped query regardless of how many annotations it holds;
    # the eligibility rule then filters in Python so both triggers share
    # exactly one definition of who qualifies.
    candidates = await DataObject.find(
        DataObject.project_id == obj.project_id,
        DataObject.status == ObjectStatus.READY,
        {"format.kind": {"$in": [k.value for k in _AUTO_ANALYZE_FORMATS]}},
    ).to_list()

    for ann in candidates:
        if pipeline_service.should_auto_analyze_annotation(
            kind=ann.format.kind, sidecar_of=ann.sidecar_of, facts=ann.facts
        ):
            await _launch(ann)
```

Add the format tuple near the top of `results.py`, after the imports:

```python
# The formats ingest may auto-analyze. Mirrors
# pipeline_service._ANNOTATION_STATS_FORMATS, imported at call time there to
# avoid a circular import at module load; kept here as values for the Mongo
# query, which cannot take enum members.
_AUTO_ANALYZE_FORMATS = (
    FormatKind.GFF,
    FormatKind.GTF,
    FormatKind.BED,
    FormatKind.GENBANK,
)
```

Confirm `FormatKind`, `ObjectRole`, and `ObjectStatus` are already imported in `results.py`; add any that are missing to the existing `from app.models...` import block.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_ingest_triggers.py -q
```

Expected: PASS, 13 tests.

- [ ] **Step 5: Guard against the two registries drifting**

`_AUTO_ANALYZE_FORMATS` duplicates `_ANNOTATION_STATS_FORMATS`. CLAUDE.md's "hand-maintained registries keyed by an enum" note is exactly this shape — add the exhaustiveness test. Append to `backend/tests/queue/test_annotation_ingest_triggers.py`:

```python
def test_the_two_format_tuples_do_not_drift():
    """results._AUTO_ANALYZE_FORMATS mirrors pipeline_service's tuple.

    Two hand-maintained tuples of the same enum members: a format added to
    one and not the other is silently never auto-analyzed, with nothing
    failing to say so.
    """
    from app.services.pipeline_service import _ANNOTATION_STATS_FORMATS

    assert set(results_mod._AUTO_ANALYZE_FORMATS) == set(_ANNOTATION_STATS_FORMATS)
```

- [ ] **Step 6: Pin the shared dedup key (R6)**

Both triggers and the button call the same launcher, so a manual click during an automatic run collapses into one job. That is inherited, not written — which is exactly why it should be pinned, since a future refactor giving the automatic path its own key would double-queue silently. Append:

```python
async def test_the_automatic_path_shares_the_button_s_dedup_key(_db, monkeypatch):
    """A manual "Compute results" click during an automatic run must not
    create a second job. Both paths call launch_annotation_stats, whose key
    is annotation_stats:{id} -- capture it rather than trusting that."""
    from app.queue import queue as queue_module
    from app.services import pipeline_service

    keys: list = []

    async def fake_enqueue(name, **kwargs):
        keys.append(kwargs.get("dedup_key"))
        return type("Job", (), {"id": "j"})()

    async def fake_get_object(object_id, *, owner):
        return await DataObject.get(object_id)

    async def fake_resolve_readable(o):
        return None, "/tmp/a.gff"

    monkeypatch.setattr(queue_module, "enqueue", fake_enqueue)
    monkeypatch.setattr(pipeline_service.object_service, "get_object", fake_get_object)
    monkeypatch.setattr(pipeline_service, "_resolve_readable", fake_resolve_readable)

    ann = await _obj("a.gff", FormatKind.GFF)

    # The automatic path.
    await results_mod._auto_analyze_after_ingest(ann, owner="local")
    # The button.
    await pipeline_service.launch_annotation_stats(object_id=ann.id, owner="local")

    assert keys == [f"annotation_stats:{ann.id}"] * 2
```

- [ ] **Step 7: Run it**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_ingest_triggers.py -q
```

Expected: PASS, 15 tests.

- [ ] **Step 8: Commit**

```bash
git add backend/app/queue/results.py backend/tests/queue/test_annotation_ingest_triggers.py
git commit -m "feat(pipelines): backfill annotation analysis when a reference lands"
```

---

### Task 5: Say why coverage is missing (R9)

Today a referenceless analysis renders blank coverage bars with no explanation.

**Files:**
- Modify: `frontend/src/api/types.ts:2066-2086`
- Modify: `frontend/src/components/AnnotationResults.tsx:146-154`

- [ ] **Step 1: Declare the fact**

In `frontend/src/api/types.ts`, inside `AnnotationStatsFacts`, after `annotation_malformed_lines`:

```typescript
  annotation_malformed_lines?: number;
  /** False when no reference resolved, so per-contig coverage is unavailable. */
  annotation_contig_lengths_known?: boolean;
```

- [ ] **Step 2: Render the refusal**

In `frontend/src/components/AnnotationResults.tsx`, the coverage section currently reads:

```tsx
          <div className="section">
            <div className="section-title">Annotated coverage</div>
            <div className="section-note">
              Fraction of each sequence covered by at least one feature.
              Overlapping features are counted once.
            </div>
            <AnnotationCoverageChart contigs={contigs} />
          </div>
```

Replace with:

```tsx
          <div className="section">
            <div className="section-title">Annotated coverage</div>
            {f.annotation_contig_lengths_known === false ? (
              // Blank bars with no explanation read as a broken chart. The
              // sequence lengths coverage divides by come from the reference,
              // and none was resolved for this annotation.
              <div className="section-note">
                Coverage needs the sequence lengths from this annotation's
                reference, and no matching reference was found in this
                project. Add the reference genome and the coverage will be
                computed automatically.
              </div>
            ) : (
              <>
                <div className="section-note">
                  Fraction of each sequence covered by at least one feature.
                  Overlapping features are counted once.
                </div>
                <AnnotationCoverageChart contigs={contigs} />
              </>
            )}
          </div>
```

The `=== false` comparison is deliberate: an annotation analyzed before this field existed has it `undefined` and must keep rendering its chart, not the refusal.

- [ ] **Step 3: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/components/AnnotationResults.tsx
git commit -m "feat(ui): say why annotation coverage is missing instead of drawing blank bars"
```

---

### Task 6: Verify against the real database and the real app

CLAUDE.md is explicit that a green suite is not enough here — the Actions-tab suggestion rules passed a full suite while miscounting `protein.faa` as an alignable reference, because every fixture fed the rules objects that already looked the way the rules expected. This task is that check.

- [ ] **Step 1: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. Read the count, not the exit code. If the run dies with EXIT=137 that is host memory, not a test failure — check for other worktree stacks with `./ops/worktree-up.sh --list` and prune orphans.

- [ ] **Step 2: Check the eligibility rule against real objects**

From the **main checkout** (this reads the main stack's database, and the predicate is importable there once merged; before merge, run it against this worktree's stack instead — see Step 3):

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models.object import DataObject
from app.services.pipeline_service import should_auto_analyze_annotation

async def main():
    await connect_to_mongo()
    objs = await DataObject.find({'format.kind': {'\$in': ['gff','gtf','bed','genbank']}}).to_list()
    yes = no = 0
    for o in objs:
        ok = should_auto_analyze_annotation(kind=o.format.kind, sidecar_of=o.sidecar_of, facts=o.facts)
        yes, no = (yes+1, no) if ok else (yes, no+1)
        print(f'{str(ok):5} {o.format.kind.value:8} sidecar={o.sidecar_of is not None} {o.name[:45]}')
    print(f'\nqualify={yes} skip={no}')
asyncio.run(main())
"
```

Expected on the author's machine: **5 qualify** (the real GFFs), **8 skip** (every `.fai` and STAR `.ann`). If any sidecar qualifies, the guard is broken — stop and fix it before going further.

- [ ] **Step 3: Bring up this worktree's stack**

```bash
./ops/worktree-up.sh
```

UI on 5273, API on 8100, with a snapshot copy of the main database.

- [ ] **Step 4: Verify the end-to-end behaviour in the browser**

At `localhost:5273`:

1. Upload a GFF into a project that already holds its reference genome. Its Results tab should show results without you pressing anything.
2. Open one of the 5 pre-existing GFFs. It should still show the "Compute results" button — ingest already ran for these, so only a reference landing backfills them.
3. Upload a GFF into a project with **no** reference. Confirm it analyzes and the coverage section shows the R9 note rather than blank bars.
4. Then upload the matching reference genome into that project. The annotation should re-analyze on its own and the coverage chart should appear.

Step 4 is the whole point of trigger 2 — verify it, don't assume it.

- [ ] **Step 5: Confirm no junk jobs were queued**

In the Computations panel, confirm no `run_annotation_stats` job exists for any `.fai` or STAR index file. That is the sidecar guard holding in the real system.

- [ ] **Step 6: Tear the stack down**

```bash
./ops/worktree-up.sh --down
```

CLAUDE.md: a stack you brought up for testing is yours to bring back down. Leaving it running corrupts other test runs, since `conftest.py` drops every collection in `biopipe_test` at session start.

- [ ] **Step 7: Commit any fixes**

If the real-database check or browser verification turned up a fix, commit it separately from the feature commits:

```bash
git add -A
git commit -m "fix(pipelines): <what the real-data check found>"
```

---

### Task 7: Open the PR

- [ ] **Step 1: Confirm the suite is green**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Read the pass count. Green is the precondition for pushing.

- [ ] **Step 2: Push**

```bash
git push -u origin HEAD
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create --base main --title "feat(annotation): analyze annotations automatically at ingest" --body "$(cat <<'EOF'
Makes every ingested annotation analyze itself, so the first visit to its
Results tab shows results rather than a "Compute results" button.

## Why two triggers

`resolve_annotation_reference` only considers references that are already
READY with the REFERENCE role. An NCBI download stages a genome and its GFF
concurrently, and the FASTA's role is assigned after a network lookup — so
an annotation that finishes ingesting first resolves no reference and is
analyzed with no contig lengths: null coverage on every contig, no track
axis, and nothing saying why. A second trigger, when a REFERENCE FASTA
becomes READY, sweeps the project and repairs those. It also covers the
standalone case (a GFF uploaded before its genome) and backfills
annotations that predate this feature.

## Why universal, with no size threshold

Measured on the largest real GFF available (12.5 MB, 51,793 features):
0.83s total — parse 0.25s, SQLite build 0.40s, hierarchy 0.43s. The only
real cost is the 26 MB database, and a size threshold responds to that
badly: it would decline exactly the large-file results a user is most
likely to wait for. Eviction of `annotation_stats_dir` is the honest fix if
disk pressure appears, and is deferred.

## The sidecar guard

8 of the 13 objects with an annotation format on a real database are `.fai`
and STAR `.ann` sidecars misdetected as BED. Without excluding
`sidecar_of`, every reference ingest would queue 8 junk jobs each writing a
database of garbage intervals. The manual path never hit this because
nobody clicks "Compute results" on a `.fai`. Tested in the failing
direction — a test that only checks real annotations do trigger would pass
whether or not the guard works.

## Failure isolation

Both triggers run after the object is already READY and are wrapped so an
AppError logs a warning rather than propagating, following the existing
`then_bam_stats` chain. A failed analysis leaves the object READY with the
"Compute results" button as fallback. One failing backfill does not stop
the rest.

Closes #298

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Label it**

`.github/release.yml` categorizes release notes by label, not by the title's prefix — an unlabelled PR lands under "Other changes".

```bash
gh pr edit --add-label "type:feature" --add-label "area:pipelines"
```

- [ ] **Step 5: Watch CI and fix what it finds**

```bash
gh pr checks
```

Poll until every check reports pass or fail, not just until the command returns — a "pending" read seconds after creation means the run has not started. Also check `gh pr view --json mergeable,mergeStateStatus`.

CI runs `ruff check`, which catches things the local suite never invokes — `I001` (import order) has already broken a PR in this repo whose local run was green. Read the job log, apply the minimal fix ruff suggests, push, re-poll. If `mergeStateStatus` is a real conflict, rebase on `origin/main` and push.

Report the PR URL only once checks are green and `mergeable` is clean.

- [ ] **Step 6: Update the issue**

Per CLAUDE.md, record implementation completion on [#298](https://github.com/syntheticgio/bioflow/issues/298) and relabel from `status: implementation plan` to `status:ready` — or note the PR is open and awaiting review.

---

## Out of scope

Both are noted in the spec and on the issue; do not do them here.

**Eviction of `annotation_stats_dir`.** Universal analysis makes 2× disk amplification real and nothing cleans it up. Wants its own issue.

**The BED misdetection.** 8 sidecars classified as BED is a format-detector bug. This plan routes around it with the sidecar guard, which is correct regardless of what the detector reports. Fixing the detector is separate.
