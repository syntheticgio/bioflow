# Annotation Track Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Draw annotation features along a coordinate axis in the existing annotation Results view, reusing #257's SQLite feature index.

**Architecture:** Three layers, built bottom-up. A pure query layer in `annotation_db.py` (binned counts, and gene models assembled from parents plus children). A resolution layer in `pipeline_service.py` that decides which reference supplies the axis, or refuses. One route in `pipelines.py` that switches response shape by density. Then the React section, rendered as inline SVG.

**Tech Stack:** Python 3 / FastAPI / SQLite (stdlib `sqlite3`) / pytest; React + TypeScript, inline SVG, TanStack Query.

**Spec:** [`docs/superpowers/specs/2026-08-12-annotation-track-viewer-design.md`](../specs/2026-08-12-annotation-track-viewer-design.md)

---

## File Structure

**Backend**

| File | Responsibility |
|---|---|
| `backend/app/pipelines/annotation_db.py` (modify) | Add `count_in_window`, `bin_counts`, `features_in_window`. All SQL lives here — the split `variant_db` documents. |
| `backend/app/services/pipeline_service.py` (modify, `:2371`) | Fix `_reference_for_annotation` role preference; add `resolve_annotation_reference` (two tiers + refusal reason). |
| `backend/app/api/v1/pipelines.py` (modify) | One route: `GET /pipelines/annotationstats/window/{object_id}`. |

**Backend tests**

| File | Covers |
|---|---|
| `backend/tests/pipelines/test_annotation_window.py` (create) | Binning, gene models, orphans, row packing. |
| `backend/tests/services/test_annotation_reference.py` (create) | Both resolution tiers and the refusal. |
| `backend/tests/api/test_annotation_window_endpoint.py` (create) | Mode switching, clamping, 404s. |

**Frontend**

| File | Responsibility |
|---|---|
| `frontend/src/api/types.ts` (modify, near `:1948`) | `AnnotationWindow` response types. |
| `frontend/src/api/client.ts` (modify, near `:1114`) | `annotationWindow()` fetcher. |
| `frontend/src/components/AnnotationTrack.tsx` (create) | The track: SVG rendering, row packing, controls. |
| `frontend/src/components/AnnotationResults.tsx` (modify) | Mount the track between charts and table. |

**Why a new test file rather than extending `test_annotation_db.py`:** that file is organised around the *table* (paging, filters, children-on-expand). Window queries are a different concern with different fixtures, and mixing them makes both harder to read.

---

## Task 1: Row-packing helper

Pure function, no SQL, no I/O. Built first because it is the only non-obvious algorithm here and everything else can be tested without it.

**Files:**
- Create: `backend/app/pipelines/annotation_window.py`
- Test: `backend/tests/pipelines/test_annotation_window.py`

- [ ] **Step 1: Write the failing test**

```python
"""Window queries for the annotation track viewer.

Separate from test_annotation_db because the table's concerns (paging,
filters, children-on-expand) and the viewer's (bins, gene models, packing)
have different fixtures and different failure modes.
"""

from app.pipelines.annotation_window import pack_rows


class TestPackRows:
    def test_non_overlapping_features_share_one_row(self):
        items = [(100, 200), (300, 400), (500, 600)]
        assert pack_rows(items) == [0, 0, 0]

    def test_overlapping_features_get_distinct_rows(self):
        items = [(100, 500), (200, 600), (300, 700)]
        assert pack_rows(items) == [0, 1, 2]

    def test_row_is_reused_once_it_is_free(self):
        # Third feature starts after the first ends, so it reuses row 0.
        items = [(100, 200), (150, 250), (300, 400)]
        assert pack_rows(items) == [0, 1, 0]

    def test_touching_features_do_not_share_a_row(self):
        # End is inclusive: a feature ending at 200 and one starting at 200
        # overlap at that base and must not be drawn on one line.
        items = [(100, 200), (200, 300)]
        assert pack_rows(items) == [0, 1]

    def test_features_beyond_the_cap_report_none(self):
        items = [(100, 500)] * 15
        rows = pack_rows(items, max_rows=12)
        assert rows[:12] == list(range(12))
        assert rows[12:] == [None, None, None]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_annotation_window.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.annotation_window'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Geometry for the annotation track viewer.

Pure functions over coordinates: no database, no I/O. Kept out of
annotation_db.py because that module is where SQL lives, and packing is not
a query.
"""

# Rows drawn per strand before the track stops growing. A dense locus would
# otherwise push the feature table off the page; the viewer reports the
# overflow instead. See the spec's row-packing section.
MAX_ROWS_PER_STRAND = 12


def pack_rows(
    items: list[tuple[int, int]], *, max_rows: int = MAX_ROWS_PER_STRAND
) -> list[int | None]:
    """Assign each (start, end) a row index so no two on a row overlap.

    Greedy by input order, which the callers supply in coordinate order: the
    first row whose last feature ends before this one starts wins. Returns
    `None` for a feature that would need a row beyond `max_rows` -- the
    caller counts those and renders "+N more" rather than growing.

    Coordinates are treated as inclusive on both ends, matching the index:
    a feature ending at 200 and one starting at 200 share that base and
    therefore cannot share a row.
    """
    row_ends: list[int] = []
    out: list[int | None] = []

    for start, end in items:
        placed: int | None = None
        for i, row_end in enumerate(row_ends):
            if start > row_end:
                row_ends[i] = end
                placed = i
                break
        else:
            if len(row_ends) < max_rows:
                row_ends.append(end)
                placed = len(row_ends) - 1
        out.append(placed)

    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_annotation_window.py -q`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_window.py backend/tests/pipelines/test_annotation_window.py
git commit -m "feat(pipelines): pack overlapping annotation features into rows"
```

---

## Task 2: Binned counts

**Files:**
- Modify: `backend/app/pipelines/annotation_db.py` (append after `count_features`)
- Test: `backend/tests/pipelines/test_annotation_window.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_annotation_window.py`, and add the imports at the top of the file:

```python
import pytest

from app.pipelines.annotation_db import bin_counts, build_annotation_db, count_in_window
from app.pipelines.annotation_parse import Feature


def _f(contig, start, end, ftype="gene", feature_id="f", parent=None, strand="+"):
    return Feature(
        contig=contig, start=start, end=end, type=ftype, strand=strand,
        score=None, name=feature_id, feature_id=feature_id, parent=parent,
        biotype=None, attributes=f"ID={feature_id}",
    )


@pytest.fixture
def db(tmp_path):
    """Ten genes at 1000-base spacing on chr1, plus one on chr2."""
    rows = [
        _f("chr1", 1000 * i, 1000 * i + 500, feature_id=f"g{i}")
        for i in range(1, 11)
    ]
    rows.append(_f("chr2", 1000, 1500, feature_id="other"))
    path = tmp_path / "features.db"
    build_annotation_db(rows=iter(rows), db_path=path)
    return path


class TestCountInWindow:
    def test_counts_only_the_requested_contig(self, db):
        assert count_in_window(db_path=db, contig="chr2", start=0, end=100_000) == 1

    def test_counts_overlap_not_containment(self, db):
        # g1 spans 1000-1500. A window starting at 1200 still contains part
        # of it, so it counts.
        assert count_in_window(db_path=db, contig="chr1", start=1200, end=1300) == 1

    def test_excludes_children(self, tmp_path):
        rows = [
            _f("chr1", 100, 900, feature_id="g1"),
            _f("chr1", 100, 200, ftype="exon", feature_id="e1", parent="g1"),
        ]
        path = tmp_path / "c.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        assert count_in_window(db_path=path, contig="chr1", start=0, end=1000) == 1


class TestBinCounts:
    def test_returns_exactly_the_requested_number_of_bins(self, db):
        counts = bin_counts(
            db_path=db, contig="chr1", start=0, end=10_000, bins=10
        )
        assert len(counts) == 10
        # Bins are 1000 bases wide. Bin 0 covers 0-999 and holds no gene
        # (g1 starts at 1000); bin 1 covers 1000-1999 and holds g1.
        assert counts[0] == 0
        assert counts[1] == 1
        # g10 starts at exactly 10000, the window's last base, so it lands in
        # the final bin alongside g9.
        assert counts[9] == 2

    def test_empty_bins_are_zero_not_missing(self, tmp_path):
        rows = [_f("chr1", 100, 200, feature_id="only")]
        path = tmp_path / "sparse.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        counts = bin_counts(db_path=path, contig="chr1", start=0, end=1000, bins=10)
        # 100-base bins, so bp 100 is the first base of bin 1, not bin 0.
        assert counts == [0, 1, 0, 0, 0, 0, 0, 0, 0, 0]

    def test_feature_straddling_a_bin_edge_counts_once(self, tmp_path):
        # Counted by start coordinate, so a feature crossing an edge lands in
        # exactly one bin rather than being double-counted.
        rows = [_f("chr1", 450, 650, feature_id="straddle")]
        path = tmp_path / "edge.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        counts = bin_counts(db_path=path, contig="chr1", start=0, end=1000, bins=2)
        assert counts == [1, 0]

    def test_window_shorter_than_bins_floors_at_one_base(self, tmp_path):
        rows = [_f("chr1", 5, 5, feature_id="point")]
        path = tmp_path / "tiny.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        counts = bin_counts(db_path=path, contig="chr1", start=0, end=9, bins=600)
        # A 9-base span cannot make 600 bins; it makes 9 one-base bins, and
        # the returned length is authoritative rather than the request.
        assert len(counts) == 9
        assert counts[5] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_annotation_window.py -q`
Expected: FAIL — `ImportError: cannot import name 'bin_counts' from 'app.pipelines.annotation_db'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/pipelines/annotation_db.py`:

```python
def count_in_window(*, db_path: Path, contig: str, start: int, end: int) -> int:
    """How many top-level features overlap this window.

    Top-level only, because that is what the viewer draws and therefore what
    the density threshold has to be measured against -- counting every exon
    would push a modest gene view over the threshold and show a density band
    where individual genes would have fitted.
    """
    con = _connect(db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM features "
            "WHERE parent IS NULL AND contig = ? AND start <= ? AND end >= ?",
            (contig, end, start),
        ).fetchone()[0]
    finally:
        con.close()


def bin_counts(
    *, db_path: Path, contig: str, start: int, end: int, bins: int
) -> list[int]:
    """Feature counts per equal-width bin across the window.

    Binned in SQL rather than in Python: the GROUP BY rides
    ix_features_locus, so a full-contig query over 200k features returns in
    ~82ms, where fetching every row to count it in Python would allocate all
    of them.

    Features are binned by `start`, so one straddling a bin edge is counted
    once rather than in both. A bin with no features is 0 rather than absent
    -- for feature counts, unlike GC content, "no features here" genuinely is
    zero, and the caller draws a flat band rather than a gap.

    The span is half-open (`end - start`) and `bin_bases` is a *ceiling*, so
    the bin count never exceeds what was asked for and the last bin absorbs
    the remainder. Deriving the width by flooring instead yields one bin more
    than requested whenever the span does not divide evenly -- verified
    against SQLite, where a 0-10000 window at 10 bins produced 11.
    """
    span = max(1, end - start)
    requested = max(1, min(int(bins), span))
    bin_bases = -(-span // requested)
    # Re-derived from the width rather than reused, so width and count cannot
    # disagree about which bin a coordinate falls in.
    n_bins = -(-span // bin_bases)

    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT (start - ?) / ? AS bin, COUNT(*) FROM features "
            "WHERE parent IS NULL AND contig = ? AND start <= ? AND end >= ? "
            "GROUP BY bin",
            (start, bin_bases, contig, end, start),
        ).fetchall()
    finally:
        con.close()

    out = [0] * n_bins
    for bin_index, count in rows:
        # A feature starting before the window has a negative bin; it is
        # visible but its start is off-screen, so it belongs to the first bin.
        i = min(max(int(bin_index), 0), n_bins - 1)
        out[i] += count
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_annotation_window.py -q`
Expected: PASS, 12 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_db.py backend/tests/pipelines/test_annotation_window.py
git commit -m "feat(pipelines): count annotation features per coordinate bin"
```

---

## Task 3: Gene models in a window

**Files:**
- Modify: `backend/app/pipelines/annotation_db.py` (append after `bin_counts`)
- Test: `backend/tests/pipelines/test_annotation_window.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_annotation_window.py`, adding `features_in_window` to the `annotation_db` import at the top:

```python
class TestFeaturesInWindow:
    @pytest.fixture
    def gene_db(self, tmp_path):
        """One gene with two exons, and an exon whose parent is absent."""
        rows = [
            _f("chr1", 1000, 2000, feature_id="g1"),
            _f("chr1", 1000, 1200, ftype="exon", feature_id="e1", parent="g1"),
            _f("chr1", 1800, 2000, ftype="exon", feature_id="e2", parent="g1"),
            _f("chr1", 5000, 5100, ftype="exon", feature_id="orphan", parent="ghost"),
        ]
        path = tmp_path / "genes.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        return path

    def test_children_are_attached_to_their_parent(self, gene_db):
        got = features_in_window(
            db_path=gene_db, contig="chr1", start=0, end=3000
        )
        assert len(got) == 1
        assert got[0]["feature_id"] == "g1"
        assert [c["feature_id"] for c in got[0]["children"]] == ["e1", "e2"]

    def test_orphaned_child_is_returned_detached(self, gene_db):
        # Its parent does not exist. Dropping it would show empty sequence
        # where a feature is, so it is drawn on its own.
        got = features_in_window(
            db_path=gene_db, contig="chr1", start=4000, end=6000
        )
        assert [f["feature_id"] for f in got] == ["orphan"]
        assert got[0]["children"] == []

    def test_child_whose_parent_is_offscreen_is_returned(self, gene_db):
        # Window covers e2 but not g1's start. e2 is at this locus, so it
        # appears rather than vanishing.
        got = features_in_window(
            db_path=gene_db, contig="chr1", start=1900, end=1950
        )
        assert [f["feature_id"] for f in got] == ["g1"]

    def test_filters_by_type(self, gene_db):
        got = features_in_window(
            db_path=gene_db, contig="chr1", start=0, end=6000, feature_type="exon"
        )
        assert {f["feature_id"] for f in got} == {"e1", "e2", "orphan"}

    def test_filters_by_strand(self, tmp_path):
        rows = [
            _f("chr1", 100, 200, feature_id="plus", strand="+"),
            _f("chr1", 300, 400, feature_id="minus", strand="-"),
        ]
        path = tmp_path / "strand.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        got = features_in_window(
            db_path=path, contig="chr1", start=0, end=1000, strand="-"
        )
        assert [f["feature_id"] for f in got] == ["minus"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_annotation_window.py -q`
Expected: FAIL — `ImportError: cannot import name 'features_in_window'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/pipelines/annotation_db.py`:

```python
def features_in_window(
    *,
    db_path: Path,
    contig: str,
    start: int,
    end: int,
    feature_type: str | None = None,
    biotype: str | None = None,
    strand: str | None = None,
) -> list[dict]:
    """Drawable features overlapping the window, children attached.

    Two queries rather than a join: the parents, then every child of those
    parents via ix_features_parent. A join would repeat each parent's columns
    once per child, which for a gene with fifty exons is fifty copies of the
    same row to reassemble in Python anyway.

    Unpaged, deliberately -- the window is bounded by coordinates and the
    caller only reaches this below the density threshold, so the row count is
    already small by construction.

    A feature whose parent is not in the result (off-screen, or absent from a
    malformed file) is returned as a top-level row of its own rather than
    dropped: a viewer that claims to show a region must not silently omit
    features in it.
    """
    filters = FeatureFilters(
        contig=contig,
        start_min=start,
        start_max=end,
        feature_type=feature_type,
        biotype=biotype,
        strand=strand,
        # The window is the bound, not the hierarchy. An explicit type filter
        # (`exon`) must reach children, and a child whose parent is off-screen
        # still has to appear.
        top_level_only=False,
    )
    where, args = _where(filters)

    con = _connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in con.execute(
                f"SELECT {_COLUMNS} FROM features{where} ORDER BY start", args
            ).fetchall()
        ]
    finally:
        con.close()

    by_id = {r["feature_id"]: r for r in rows if r["feature_id"]}
    out: list[dict] = []
    for r in rows:
        r["children"] = []
        parent = by_id.get(r["parent"]) if r["parent"] else None
        if parent is None:
            out.append(r)
    for r in rows:
        parent = by_id.get(r["parent"]) if r["parent"] else None
        if parent is not None:
            parent["children"].append(r)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_annotation_window.py -q`
Expected: PASS, 17 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_db.py backend/tests/pipelines/test_annotation_window.py
git commit -m "feat(pipelines): assemble gene models for a coordinate window"
```

---

## Task 4: Fix the reference role preference

A bug fix to existing behaviour, committed separately from the feature. `_reference_for_annotation` has **no test coverage today**, so the tests come first and characterise the bug before it is fixed.

**Files:**
- Modify: `backend/app/services/pipeline_service.py:2371-2381`
- Test: `backend/tests/services/test_annotation_reference.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""Which reference supplies an annotation's coordinate axis.

Untested before the track viewer, which is why the role-preference bug in
_reference_for_annotation survived: a wrong reference cost #257 only a
slightly-off coverage percentage, but costs the viewer a coordinate ruler
that is silently wrong.
"""

import pytest

from app.models.object import FormatKind, ObjectRole
from app.services import pipeline_service


class _Obj:
    """A DataObject stand-in: only the fields resolution reads."""

    def __init__(self, oid, kind=FormatKind.FASTA, role=None, facts=None,
                 derived_from=None, project_id="p1", owner="o"):
        self.id = oid
        self.format = type("F", (), {"kind": kind})()
        self.role = role
        self.facts = facts or {}
        self.derived_from = derived_from or []
        self.project_id = project_id
        self.owner = owner
        self.name = str(oid)


@pytest.fixture
def objects(monkeypatch):
    """Register objects DataObject.get resolves by id."""
    store: dict = {}

    async def fake_get(oid):
        return store.get(oid)

    monkeypatch.setattr(pipeline_service.DataObject, "get", staticmethod(fake_get))
    return store


class TestProvenanceTier:
    async def test_prefers_the_reference_role_over_a_bare_fasta(self, objects):
        # The bug: a protein FASTA listed first was returned as the reference.
        protein = _Obj("prot", role=ObjectRole.PROTEIN)
        genome = _Obj("gen", role=ObjectRole.REFERENCE)
        objects.update({"prot": protein, "gen": genome})
        ann = _Obj("ann", kind=FormatKind.GFF, derived_from=["prot", "gen"])

        got = await pipeline_service._reference_for_annotation(ann)
        assert got is genome

    async def test_falls_back_to_bare_fasta_when_no_role_is_set(self, objects):
        plain = _Obj("plain", role=None)
        objects["plain"] = plain
        ann = _Obj("ann", kind=FormatKind.GFF, derived_from=["plain"])

        assert await pipeline_service._reference_for_annotation(ann) is plain

    async def test_ignores_non_fasta_parents(self, objects):
        bam = _Obj("bam", kind=FormatKind.BAM)
        objects["bam"] = bam
        ann = _Obj("ann", kind=FormatKind.GFF, derived_from=["bam"])

        assert await pipeline_service._reference_for_annotation(ann) is None

    async def test_returns_none_with_no_provenance(self, objects):
        ann = _Obj("ann", kind=FormatKind.GFF, derived_from=[])
        assert await pipeline_service._reference_for_annotation(ann) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_annotation_reference.py -q`
Expected: FAIL — `test_prefers_the_reference_role_over_a_bare_fasta` returns the protein FASTA, since the current code takes the first FASTA parent unconditionally.

- [ ] **Step 3: Write minimal implementation**

Replace `_reference_for_annotation` at `backend/app/services/pipeline_service.py:2371`:

```python
async def _reference_for_annotation(ann) -> DataObject | None:
    """The reference this annotation describes, from its provenance.

    Prefers an explicit REFERENCE role over bare FASTA format, matching
    `reference_for_bam` below: an annotation's parents may include a protein
    or CDS FASTA downloaded alongside the genome, and returning one of those
    gives the track viewer a coordinate axis for the wrong sequence.

    Best-effort: an annotation with no recorded reference still computes
    #257's stats, reporting per-contig counts without coverage fractions.
    """
    fallback: DataObject | None = None
    for parent_id in (ann.derived_from or []):
        parent = await DataObject.get(parent_id)
        if parent is None or parent.format.kind is not FormatKind.FASTA:
            continue
        if parent.role is ObjectRole.REFERENCE:
            return parent
        fallback = fallback or parent
    return fallback
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_annotation_reference.py -q`
Expected: PASS, 4 passed

Then confirm nothing that depended on the old behaviour broke:

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_annotation_stats_launch.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_annotation_reference.py
git commit -m "fix(pipelines): prefer the reference role when resolving an annotation's genome

_reference_for_annotation took the first FASTA in derived_from with no role
check, so an annotation downloaded alongside its protein.faa could resolve to
the protein set. Matches reference_for_bam's posture, which already prefers
ObjectRole.REFERENCE and falls back to bare FASTA."
```

---

## Task 5: Two-tier resolution with a refusal reason

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (append after `_reference_for_annotation`)
- Test: `backend/tests/services/test_annotation_reference.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_annotation_reference.py`:

```python
class TestAccessionTier:
    @pytest.fixture
    def project(self, monkeypatch, objects):
        """A project whose object list the accession tier scans."""
        listed: list = []

        async def fake_list(project_id, *, owner, limit=500, status=None):
            return listed

        monkeypatch.setattr(
            pipeline_service.object_service, "list_objects", fake_list
        )
        return listed

    async def test_matches_a_reference_with_the_same_accession(self, objects, project):
        genome = _Obj("gen", role=ObjectRole.REFERENCE,
                      facts={"ncbi_assembly_accession": "GCF_000001405.39"})
        project.append(genome)
        ann = _Obj("ann", kind=FormatKind.GFF,
                   facts={"ncbi_assembly_accession": "GCF_000001405.39"})

        got = await pipeline_service.resolve_annotation_reference(ann)
        assert got.reference is genome
        assert got.reason is None

    async def test_version_suffixes_do_not_match(self, objects, project):
        # .39 and .40 are different assemblies with different coordinates.
        project.append(_Obj("gen", role=ObjectRole.REFERENCE,
                            facts={"ncbi_assembly_accession": "GCF_000001405.40"}))
        ann = _Obj("ann", kind=FormatKind.GFF,
                   facts={"ncbi_assembly_accession": "GCF_000001405.39"})

        got = await pipeline_service.resolve_annotation_reference(ann)
        assert got.reference is None
        assert "no reference" in got.reason.lower()

    async def test_gca_and_gcf_counterparts_do_not_match(self, objects, project):
        project.append(_Obj("gen", role=ObjectRole.REFERENCE,
                            facts={"ncbi_assembly_accession": "GCA_000001405.39"}))
        ann = _Obj("ann", kind=FormatKind.GFF,
                   facts={"ncbi_assembly_accession": "GCF_000001405.39"})

        assert (await pipeline_service.resolve_annotation_reference(ann)).reference is None

    async def test_provenance_wins_over_accession(self, objects, project):
        provenance = _Obj("gen", role=ObjectRole.REFERENCE)
        objects["gen"] = provenance
        project.append(_Obj("other", role=ObjectRole.REFERENCE,
                            facts={"ncbi_assembly_accession": "GCF_9.1"}))
        ann = _Obj("ann", kind=FormatKind.GFF, derived_from=["gen"],
                   facts={"ncbi_assembly_accession": "GCF_9.1"})

        got = await pipeline_service.resolve_annotation_reference(ann)
        assert got.reference is provenance

    async def test_non_fasta_candidates_are_ignored(self, objects, project):
        # A VCF carrying the same accession is not a coordinate axis.
        project.append(_Obj("vcf", kind=FormatKind.VCF,
                            facts={"ncbi_assembly_accession": "GCF_9.1"}))
        ann = _Obj("ann", kind=FormatKind.GFF,
                   facts={"ncbi_assembly_accession": "GCF_9.1"})

        assert (await pipeline_service.resolve_annotation_reference(ann)).reference is None

    async def test_refuses_when_the_annotation_has_no_accession(self, objects, project):
        project.append(_Obj("gen", role=ObjectRole.REFERENCE,
                            facts={"ncbi_assembly_accession": "GCF_9.1"}))
        ann = _Obj("ann", kind=FormatKind.GFF, facts={})

        got = await pipeline_service.resolve_annotation_reference(ann)
        assert got.reference is None
        assert got.reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_annotation_reference.py -q`
Expected: FAIL — `AttributeError: module 'app.services.pipeline_service' has no attribute 'resolve_annotation_reference'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/pipeline_service.py`, after `_reference_for_annotation`:

```python
@dataclass(frozen=True)
class AnnotationReference:
    """Which reference supplies an annotation's axis, or why none does.

    A reason rather than a bare None: the viewer tells the user what would
    fix it, since "no reference" is actionable and a blank panel is not.
    """

    reference: DataObject | None = None
    reason: str | None = None


async def resolve_annotation_reference(ann) -> AnnotationReference:
    """The reference whose coordinates this annotation is drawn against.

    Two tiers, then refusal. The axis is a claim about what the coordinates
    mean, so a guess is worse than nothing: a wrong reference draws a ruler
    of the wrong length with features positioned against it, which looks
    authoritative and is false.

    Tier 1 is explicit provenance. Tier 2 matches `ncbi_assembly_accession`,
    the fact NCBI lookups write on both the genome and the annotation
    downloaded with it -- the same match `resolve_annotation_inputs` makes
    for the consequence-annotation card, and for the same reason.

    Accessions compare by exact string equality: GCF_000001405.39 and .40 are
    different assemblies, and a GCA counterpart is a different record.
    """
    from app.services import object_service

    reference = await _reference_for_annotation(ann)
    if reference is not None:
        return AnnotationReference(reference=reference)

    accession = (ann.facts or {}).get("ncbi_assembly_accession")
    if accession:
        candidates = await object_service.list_objects(
            ann.project_id, owner=ann.owner, limit=500, status=ObjectStatus.READY
        )
        for obj in candidates:
            if obj.format.kind is not FormatKind.FASTA:
                continue
            if obj.facts.get("ncbi_assembly_accession") == accession:
                return AnnotationReference(reference=obj)

    return AnnotationReference(
        reason=(
            f"No reference resolved for {ann.name}. Its provenance records no "
            "genome, and no reference in this project carries a matching NCBI "
            "assembly accession. The feature table and summary charts are "
            "still available."
        )
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_annotation_reference.py -q`
Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_annotation_reference.py
git commit -m "feat(pipelines): resolve an annotation's reference by provenance or accession"
```

---

## Task 6: The window route

**Files:**
- Modify: `backend/app/api/v1/pipelines.py` (after `get_annotation_children`, ~`:1030`)
- Test: `backend/tests/api/test_annotation_window_endpoint.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""The track viewer's window route.

Structured like test_annotation_stats_endpoints.py: the real route on a bare
FastAPI app, with object_service.get_object stubbed and a real features.db
built in a temp directory, so builder/route schema drift is caught here too.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers
from app.pipelines.annotation_db import build_annotation_db
from app.pipelines.annotation_parse import Feature
from tests.api.bare_app import override_owner, stub_get_object

OBJECT_ID = "507f1f77bcf86cd799439011"
MISSING_ID = "507f191e810c19729de860ea"


def _f(start, end, feature_id, ftype="gene", parent=None):
    return Feature(
        contig="chr1", start=start, end=end, type=ftype, strand="+", score=None,
        name=feature_id, feature_id=feature_id, parent=parent, biotype=None,
        attributes=f"ID={feature_id}",
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)
    rows = [_f(1000 * i, 1000 * i + 500, f"g{i}") for i in range(1, 6)]
    rows.append(_f(1000, 1200, "e1", ftype="exon", parent="g1"))
    build_annotation_db(
        rows=iter(rows),
        db_path=tmp_path / "annotation_stats" / OBJECT_ID / "features.db",
    )
    stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID, MISSING_ID})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


def get_window(client, object_id=OBJECT_ID, **params):
    params.setdefault("contig", "chr1")
    return client.get(
        f"/pipelines/annotationstats/window/{object_id}", params=params
    )


class TestModeSwitching:
    def test_sparse_window_returns_features(self, client):
        body = get_window(client, start=0, end=10_000).json()
        assert body["mode"] == "features"
        assert [f["feature_id"] for f in body["features"]] == [
            "g1", "g2", "g3", "g4", "g5"
        ]

    def test_children_ride_along(self, client):
        body = get_window(client, start=0, end=10_000).json()
        g1 = body["features"][0]
        assert [c["feature_id"] for c in g1["children"]] == ["e1"]

    def test_dense_window_returns_bins(self, client, monkeypatch):
        monkeypatch.setattr(pipelines_api, "ANNOTATION_DENSITY_THRESHOLD", 3)
        body = get_window(client, start=0, end=10_000, bins=10).json()
        assert body["mode"] == "binned"
        assert len(body["counts"]) == 10
        assert sum(body["counts"]) == 5

    def test_response_echoes_the_requested_window(self, client):
        body = get_window(client, start=100, end=9_000).json()
        assert (body["contig"], body["start"], body["end"]) == ("chr1", 100, 9_000)


class TestValidation:
    def test_bins_are_clamped_to_1000(self, client, monkeypatch):
        monkeypatch.setattr(pipelines_api, "ANNOTATION_DENSITY_THRESHOLD", 1)
        body = get_window(client, start=0, end=10_000_000, bins=999_999).json()
        assert len(body["counts"]) == 1000

    def test_end_before_start_is_rejected(self, client):
        assert get_window(client, start=500, end=100).status_code == 422

    def test_missing_database_404s(self, client):
        assert get_window(client, object_id=MISSING_ID, start=0, end=10).status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/api/test_annotation_window_endpoint.py -q`
Expected: FAIL — every test 404s; the route does not exist.

- [ ] **Step 3: Write minimal implementation**

Add to `backend/app/api/v1/pipelines.py` after `get_annotation_children`, and add `annotation_window` to the existing `app.pipelines` imports at the top:

```python
# Top-level features in a window above which the viewer draws a density band
# instead of individual features. A legibility judgement, not a performance
# limit: at a typical section width this is ~2px per feature, below which
# boxes stop being separable. Expected to be tuned against a real GFF3.
ANNOTATION_DENSITY_THRESHOLD = 500

# A client asking for a million bins must not make the server build a million
# rows; 10 is below any width worth drawing.
_MIN_BINS, _MAX_BINS = 10, 1000


@router.get("/annotationstats/window/{object_id}")
async def get_annotation_window(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    contig: str,
    start: int,
    end: int,
    bins: int = 600,
    feature_type: str | None = None,
    biotype: str | None = None,
    strand: str | None = None,
) -> dict:
    """Features overlapping a coordinate window, or their density.

    Two response shapes behind one route, chosen server-side by a count: a
    client cannot know how dense a region is before asking, and making it
    ask twice would double the round trips on every pan.

    `mode` distinguishes them rather than which key is present, so an empty
    region cannot be mistaken for a dense one. The window is echoed back
    because panning issues overlapping requests that can return out of
    order -- a response with no record of what it answers cannot be matched
    to the current viewport.

    Ownership is checked before the path is built, for the same reason the
    sibling routes do it: annotation_stats_dir is laid out by object id
    alone, so this lookup is the only thing separating one profile's
    annotations from another's.
    """
    await object_service.get_object(object_id, owner=owner)

    if end < start:
        raise ValidationError(
            "end must not be before start",
            details={"start": start, "end": end},
        )

    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    common = {"contig": contig, "start": start, "end": end}
    total = annotation_db.count_in_window(db_path=db_path, **common)

    if total >= ANNOTATION_DENSITY_THRESHOLD:
        counts = annotation_db.bin_counts(
            db_path=db_path,
            bins=max(_MIN_BINS, min(int(bins), _MAX_BINS)),
            **common,
        )
        # Derived from the returned array rather than recomputed, so the
        # width reported to the client cannot disagree with the binning that
        # actually happened.
        span = max(1, end - start)
        return {
            "mode": "binned",
            **common,
            "bin_bases": -(-span // max(1, len(counts))),
            "counts": counts,
            "total": total,
        }

    features = annotation_db.features_in_window(
        db_path=db_path,
        feature_type=feature_type,
        biotype=biotype,
        strand=strand,
        **common,
    )
    rows = annotation_window.pack_rows(
        [(f["start"], f["end"]) for f in features]
    )
    for feature, row in zip(features, rows):
        feature["row"] = row

    return {
        "mode": "features",
        **common,
        "features": [f for f in features if f["row"] is not None],
        "truncated_rows": sum(1 for r in rows if r is None),
        "total": total,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/api/test_annotation_window_endpoint.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/tests/api/test_annotation_window_endpoint.py
git commit -m "feat(api): serve annotation features or their density for a window"
```

---

## Task 7: Verify resolution against the real database

Not a code change. CLAUDE.md records that #257's suggestion rules passed a green suite while miscounting `protein.faa` as an alignable reference, because the fixtures were already shaped the way the rules expected. Task 4's bug is that exact shape, so it is checked against real objects before the UI is built on it.

**Files:** none — verification only.

- [ ] **Step 1: Find a project with an annotation and run resolution against it**

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models.object import DataObject, FormatKind
from app.services import pipeline_service as ps

async def main():
    await connect_to_mongo()
    anns = await DataObject.find(
        DataObject.format.kind == FormatKind.GFF
    ).limit(5).to_list()
    if not anns:
        print('No GFF objects in this database -- upload one to check.')
        return
    for a in anns:
        got = await ps.resolve_annotation_reference(a)
        name = got.reference.name if got.reference else None
        role = got.reference.role if got.reference else None
        print(f'{a.name}: reference={name} role={role} reason={got.reason}')

asyncio.run(main())
"
```

- [ ] **Step 2: Confirm the result is honest**

Expected: each annotation resolves to a **genomic** FASTA, or refuses with a reason. A resolution to a `protein.faa`, a `cds_from_genomic.fna`, or an unrelated assembly is a bug in Task 4 or 5 — fix it there and re-run before continuing.

Note this reads the main stack's database, which is the point: it is real data rather than fixtures. It is a read-only query and writes nothing.

- [ ] **Step 3: Commit**

Nothing to commit. Record the observed output in the PR description.

---

## Task 8: Frontend types and client

**Files:**
- Modify: `frontend/src/api/types.ts` (after `AnnotationFeaturePage`, ~`:1948`)
- Modify: `frontend/src/api/client.ts` (after `annotationChildren`, ~`:1116`)

- [ ] **Step 1: Add the types**

In `frontend/src/api/types.ts`, after `AnnotationFeaturePage`:

```typescript
/** A feature drawn in the track, with its children and packed row. */
export interface AnnotationWindowFeature extends AnnotationFeature {
  children: AnnotationFeature[];
  row: number;
}

/** Individual features, below the density threshold. */
export interface AnnotationWindowFeatures {
  mode: "features";
  contig: string;
  start: number;
  end: number;
  total: number;
  features: AnnotationWindowFeature[];
  /** Features dropped because the row cap was reached. */
  truncated_rows: number;
}

/** Per-bin counts, at or above the density threshold. */
export interface AnnotationWindowBinned {
  mode: "binned";
  contig: string;
  start: number;
  end: number;
  total: number;
  bin_bases: number;
  counts: number[];
}

/** Discriminated on `mode`, so an empty region cannot read as a dense one. */
export type AnnotationWindow =
  | AnnotationWindowFeatures
  | AnnotationWindowBinned;
```

- [ ] **Step 2: Add the client method**

In `frontend/src/api/client.ts`, after `annotationChildren`:

```typescript
  /** Features overlapping a window, or their density when too dense to draw.
   *  The response echoes the window back so an out-of-order reply can be
   *  matched to the current viewport. */
  annotationWindow: (
    objectId: string,
    q: {
      contig: string;
      start: number;
      end: number;
      bins?: number;
      feature_type?: string;
      strand?: string;
    },
  ) => {
    const p = new URLSearchParams({
      contig: q.contig,
      start: String(Math.max(0, Math.floor(q.start))),
      end: String(Math.floor(q.end)),
    });
    if (q.bins) p.set("bins", String(q.bins));
    if (q.feature_type) p.set("feature_type", q.feature_type);
    if (q.strand) p.set("strand", q.strand);
    return request<AnnotationWindow>(
      `/pipelines/annotationstats/window/${objectId}?${p.toString()}`,
    );
  },
```

Add `AnnotationWindow` to the existing type import block at the top of `client.ts`.

- [ ] **Step 3: Verify it compiles**

Run: `docker compose exec web npx tsc --noEmit -p tsconfig.json`
Expected: no errors. (If the `web` container is not up, run `./ops/worktree-up.sh` first.)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(frontend): add the annotation window client and its types"
```

---

## Task 9: The track component

**Files:**
- Create: `frontend/src/components/AnnotationTrack.tsx`

- [ ] **Step 1: Write the component**

```tsx
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import type {
  AnnotationContigStat,
  AnnotationWindowFeature,
  ObjectDetail as ObjectDetailData,
} from "../api/types";

/**
 * Features along a coordinate axis: what the summary charts and the feature
 * table cannot show -- clustering, overlap, strand, and exon structure in
 * genomic context.
 *
 * The axis comes from the reference's recorded contig lengths, never from the
 * annotation's own coordinates. A ruler synthesised from the last feature's
 * end would mean "as far as annotation reaches" while looking like the contig
 * length, which is the kind of quietly-wrong picture this view exists to
 * avoid.
 */

const TRACK_WIDTH = 1000;
const ROW_HEIGHT = 18;
const AXIS_HEIGHT = 28;

function fmt(n: number): string {
  return n.toLocaleString();
}

export function AnnotationTrack({
  obj,
  contigs,
  onPickFeature,
}: {
  obj: ObjectDetailData;
  /** Per-contig stats from #257. Only entries with a known length are usable
   *  as an axis; the caller filters, and reports what it dropped. */
  contigs: AnnotationContigStat[];
  onPickFeature?: (name: string) => void;
}) {
  const drawable = useMemo(
    () => contigs.filter((c) => typeof c.length === "number" && c.length > 0),
    [contigs],
  );
  const hidden = contigs.length - drawable.length;

  const [contig, setContig] = useState(drawable[0]?.name ?? "");
  const current = drawable.find((c) => c.name === contig) ?? drawable[0];
  const contigLength = current?.length ?? 0;

  const [view, setView] = useState({ start: 0, end: contigLength });
  const [typeFilter, setTypeFilter] = useState<string | undefined>();

  const win = useQuery({
    queryKey: ["annotationWindow", obj.id, contig, view.start, view.end, typeFilter],
    queryFn: () =>
      api.annotationWindow(obj.id, {
        contig,
        start: view.start,
        end: view.end,
        bins: 600,
        feature_type: typeFilter,
      }),
    enabled: Boolean(contig && contigLength > 0),
  });

  function pickContig(name: string) {
    const next = drawable.find((c) => c.name === name);
    setContig(name);
    setView({ start: 0, end: next?.length ?? 0 });
  }

  function zoom(factor: number) {
    const span = view.end - view.start;
    const mid = view.start + span / 2;
    const half = Math.max(50, (span * factor) / 2);
    setView({
      start: Math.max(0, Math.round(mid - half)),
      end: Math.min(contigLength, Math.round(mid + half)),
    });
  }

  function pan(fraction: number) {
    const span = view.end - view.start;
    const delta = Math.round(span * fraction);
    let start = view.start + delta;
    let end = view.end + delta;
    if (start < 0) { end -= start; start = 0; }
    if (end > contigLength) { start -= end - contigLength; end = contigLength; }
    setView({ start: Math.max(0, start), end });
  }

  if (drawable.length === 0) {
    return (
      <div className="section">
        <div className="section-title">Track</div>
        <div className="section-note">
          No contig has a recorded length, so no coordinate axis can be drawn.
          The feature table below is unaffected.
        </div>
      </div>
    );
  }

  const span = Math.max(1, view.end - view.start);
  const toX = (bp: number) =>
    ((Math.min(Math.max(bp, view.start), view.end) - view.start) / span) *
    TRACK_WIDTH;

  const data = win.data;
  const features: AnnotationWindowFeature[] =
    data?.mode === "features" ? data.features : [];
  const maxRow = features.reduce((m, f) => Math.max(m, f.row), 0);
  const bodyHeight = data?.mode === "binned" ? 80 : (maxRow + 1) * ROW_HEIGHT + 12;

  return (
    <div className="section">
      <div className="section-title">Track</div>

      <div className="track-controls">
        <select value={contig} onChange={(e) => pickContig(e.target.value)}>
          {drawable.map((c) => (
            <option key={c.name} value={c.name}>
              {c.name} · {fmt(c.length as number)} bp
            </option>
          ))}
        </select>
        <span className="track-locus">
          {contig}:{fmt(view.start)}-{fmt(view.end)}
        </span>
        <button type="button" className="btn" onClick={() => pan(-0.4)}>←</button>
        <button type="button" className="btn" onClick={() => zoom(0.5)}>+</button>
        <button type="button" className="btn" onClick={() => zoom(2)}>−</button>
        <button
          type="button"
          className="btn"
          onClick={() => setView({ start: 0, end: contigLength })}
        >
          Whole contig
        </button>
        {typeFilter && (
          <button type="button" className="btn" onClick={() => setTypeFilter(undefined)}>
            Clear “{typeFilter}”
          </button>
        )}
      </div>

      {hidden > 0 && (
        <div className="section-note">
          {hidden} contig{hidden === 1 ? "" : "s"} not shown — no recorded
          length, so no axis can be drawn for {hidden === 1 ? "it" : "them"}.
        </div>
      )}

      {win.isError && (
        <div className="section-note">Could not load this region.</div>
      )}

      <svg
        viewBox={`0 0 ${TRACK_WIDTH} ${AXIS_HEIGHT + bodyHeight}`}
        className="annotation-track"
        role="img"
        aria-label={`Annotation features on ${contig} from ${view.start} to ${view.end}`}
      >
        <line
          x1={0} y1={AXIS_HEIGHT - 8} x2={TRACK_WIDTH} y2={AXIS_HEIGHT - 8}
          className="track-axis"
        />
        <text x={0} y={AXIS_HEIGHT - 14} className="track-tick">{fmt(view.start)}</text>
        <text x={TRACK_WIDTH} y={AXIS_HEIGHT - 14} textAnchor="end" className="track-tick">
          {fmt(view.end)}
        </text>

        {data?.mode === "binned" &&
          data.counts.map((count, i) => {
            const max = Math.max(...data.counts, 1);
            const w = TRACK_WIDTH / data.counts.length;
            const h = (count / max) * 70;
            return (
              <rect
                key={i}
                x={i * w}
                y={AXIS_HEIGHT + (70 - h)}
                width={Math.max(1, w - 0.5)}
                height={h}
                className="track-bin"
              >
                <title>{count} features</title>
              </rect>
            );
          })}

        {data?.mode === "features" &&
          features.map((f) => {
            const y = AXIS_HEIGHT + f.row * ROW_HEIGHT;
            const x1 = toX(f.start);
            const x2 = toX(f.end);
            const cls = f.strand === "-" ? "track-feature minus" : "track-feature plus";
            return (
              <g
                key={`${f.feature_id ?? f.name}-${f.start}`}
                className={cls}
                onClick={() => f.name && onPickFeature?.(f.name)}
              >
                <title>
                  {`${f.name ?? "(unnamed)"} · ${f.type ?? "feature"} · ` +
                    `${contig}:${fmt(f.start)}-${fmt(f.end)} · ` +
                    `${f.strand ?? "?"} strand`}
                </title>
                <line x1={x1} y1={y + 7} x2={x2} y2={y + 7} className="track-spine" />
                {f.children.length === 0 ? (
                  <rect x={x1} y={y + 2} width={Math.max(1, x2 - x1)} height={10} rx={2} />
                ) : (
                  f.children.map((c, i) => (
                    <rect
                      key={i}
                      x={toX(c.start)}
                      y={y + 2}
                      width={Math.max(1, toX(c.end) - toX(c.start))}
                      height={10}
                      rx={2}
                    />
                  ))
                )}
              </g>
            );
          })}
      </svg>

      <div className="section-note">
        {win.isLoading && "Loading…"}
        {data?.mode === "binned" &&
          `${fmt(data.total)} features in view — too dense to draw individually. ` +
            `Each bar covers ${fmt(data.bin_bases)} bp. Zoom in to see features.`}
        {data?.mode === "features" &&
          `${features.length} feature${features.length === 1 ? "" : "s"} in view` +
            (data.truncated_rows > 0
              ? ` · +${data.truncated_rows} more not shown — zoom in`
              : "")}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Add the styles**

Append to the stylesheet that already carries the other results-view rules (`frontend/src/styles/*.css` — use the file that defines `.section-note`):

```css
.track-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
.track-locus { font-variant-numeric: tabular-nums; opacity: 0.8; }
.annotation-track { width: 100%; height: auto; }
.track-axis { stroke: currentColor; stroke-width: 1; opacity: 0.35; }
.track-tick { font-size: 10px; fill: currentColor; opacity: 0.6; }
.track-bin { fill: var(--accent, #6b8ac4); opacity: 0.75; }
.track-spine { stroke: currentColor; stroke-width: 1; opacity: 0.55; }
.track-feature { cursor: pointer; }
.track-feature.plus rect { fill: #6b8ac4; }
.track-feature.minus rect { fill: #c08a5a; }
.track-feature.plus .track-spine { stroke: #6b8ac4; }
.track-feature.minus .track-spine { stroke: #c08a5a; }
.track-feature:hover rect { opacity: 0.75; }
```

- [ ] **Step 3: Verify it compiles**

Run: `docker compose exec web npx tsc --noEmit -p tsconfig.json`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AnnotationTrack.tsx frontend/src/styles
git commit -m "feat(ui): draw annotation features along a coordinate axis"
```

---

## Task 10: Mount the track in the Results view

**Files:**
- Modify: `frontend/src/components/AnnotationResults.tsx`

- [ ] **Step 1: Mount it between the charts and the table**

Add the import at the top:

```tsx
import { AnnotationTrack } from "./AnnotationTrack";
```

Then, in the rendered output, place the track after the charts block and before `<AnnotationFeatureTable ... />`:

```tsx
      <AnnotationTrack
        obj={obj}
        contigs={f.annotation_per_contig ?? []}
        onPickFeature={setNameQuery}
      />
```

`setNameQuery` is the table's existing name-search state. If `AnnotationResults` does not already own it, lift it out of `AnnotationFeatureTable` into `AnnotationResults` and pass it down as a controlled prop — clicking a feature filtering the table is the payoff of keeping the two adjacent.

- [ ] **Step 2: Verify it compiles**

Run: `docker compose exec web npx tsc --noEmit -p tsconfig.json`
Expected: no errors.

- [ ] **Step 3: Verify in the browser**

```bash
./ops/worktree-up.sh
```

Open `http://localhost:5273`, find a project with a GFF/GTF/BED that has computed results, and check:

- The track renders below the charts, above the table.
- "Whole contig" on a dense annotation shows the density band with the "too dense to draw" note.
- Zooming in switches to individual features with exon blocks.
- Panning past either end clamps rather than scrolling into negative coordinates.
- Clicking a feature filters the table below it.
- An annotation whose contigs have no recorded length shows the no-axis note rather than an empty box.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AnnotationResults.tsx
git commit -m "feat(ui): show the annotation track above the feature table"
```

---

## Task 11: Full suite, then open the PR

- [ ] **Step 1: Run the whole backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the **count**, not the exit code — CLAUDE.md is explicit that "green" means the number, and that a suite dying with EXIT=137 is host memory rather than a test failure.

- [ ] **Step 2: Check the suggestion service**

Per CLAUDE.md, confirm no card's `unavailable` reason this work makes untrue:

```bash
grep -n "annotation" backend/app/services/suggestion_service.py
```

Expected: no card claiming annotation results cannot be viewed. If one exists, update it and its test in `backend/tests/services/test_suggestion_service.py`.

- [ ] **Step 3: Bring the test stack down**

```bash
./ops/worktree-up.sh --down
```

A stack left up wipes other test runs' data mid-run — the failure CLAUDE.md documents from 2026-08-12.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --title "feat(annotation): add a coordinate-based annotation track viewer" --body "Closes #295"
```

Write the body to say **why**: the feature index #257 built was already viewport-shaped, so this is a window route plus a track section rather than a reparse. Note the `fix(pipelines):` commit in Task 4 as a separable behaviour change, and record the Task 7 output.

Label the PR `type:feature`, `area:pipelines`, `area:frontend` — `.github/release.yml` categorises by label, not by the title's prefix.

- [ ] **Step 5: Watch CI until every check reports**

```bash
gh pr checks <N>
```

```bash
gh pr view <N> --json mergeable,mergeStateStatus
```

Poll until every check reports pass/fail — a "pending" read seconds after creation is the run not having started. Expect `ruff` to catch things the local run does not (CLAUDE.md records `I001` import-order failing in CI on a green local suite). Fix, push, re-poll. Report the URL only once checks are green and `mergeable` is clean.
