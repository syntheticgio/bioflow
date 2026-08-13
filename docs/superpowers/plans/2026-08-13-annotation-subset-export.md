# Annotation Subset Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export the annotation feature table's current filter as a new `ANNOTATION` object, by re-emitting original source lines selected by recorded line number.

**Architecture:** A `line_no` column on the features database makes each row addressable back to its source line. Export walks the parent/child closure of the filtered set, verifies the source file has not changed, re-reads and re-parses each selected line, and writes a file that `object_service.ingest_local_file` turns into a derived object. Nothing is ever serialized from a `Feature` — the parser does not store the GFF `source` column or `phase`, so reconstruction would silently lose reading frame.

**Tech Stack:** Python 3, SQLite (stdlib `sqlite3`), FastAPI, Beanie/Motor, pytest, React + TypeScript.

**Spec:** [`docs/superpowers/specs/2026-08-13-annotation-subset-export-design.md`](../specs/2026-08-13-annotation-subset-export-design.md) (AE-1 … AE-33).

---

## Before you start

**Run the test suite from this worktree, not with plain `docker compose exec`.** Inside a worktree, `docker compose exec api python -m pytest` silently tests *main's* code — the `api` container bind-mounts the main checkout. Every command in this plan uses:

```bash
./backend/run-worktree-tests.sh tests/path -q
```

**Three registries in this repo silently skip unknown keys.** This plan adds an entry to each, and each has its own task with its own assertion:

- `_APPLIERS` (`backend/app/queue/results.py:2692`) — a missing entry means the job succeeds and no object is created.
- `EXCLUDED_LAUNCHES` / `NODE_TYPES` (`backend/app/pipelines/node_types.py`) — an existing test will *fail* the moment you add a `launch_*` function. That is expected; Task 9 resolves it.
- `_SIDECAR_ROLES` is not touched — the export creates a `derived_from` object, not a sidecar.

## File Structure

**Create:**
- `backend/app/pipelines/annotation_export.py` — closure walk, verified re-emission, name derivation. Pure functions over a db path and a source path, no I/O beyond those two files, so every format edge case is testable as a plain call. This is the only new module; it mirrors `annotation_hierarchy.py`'s shape (SQL + pure logic, no queue or Mongo imports).
- `backend/tests/pipelines/test_annotation_export.py`
- `backend/tests/fixtures/annotation/ncbi_slice.gff3` — a real NCBI GFF3 slice, following the `fixtures/genbank/ecoli_slice.gbff` precedent.
- `backend/tests/queue/test_annotation_export_handler.py`
- `backend/tests/api/test_annotation_export_endpoint.py`

**Modify:**
- `backend/app/pipelines/annotation_parse.py` — `line_no` on `Feature`, optional parameter on the three parsers.
- `backend/app/pipelines/annotation_db.py` — `line_no` column, `_COLUMNS`, insert tuple.
- `backend/app/queue/annotation_handlers.py` — pass line numbers; record the source hash; the new export handler.
- `backend/app/api/v1/pipelines.py` — shared filter builder; export route.
- `backend/app/services/pipeline_service.py` — `launch_annotation_subset_export`.
- `backend/app/queue/results.py` — `_apply_annotation_subset_export` + `_APPLIERS` entry.
- `backend/app/pipelines/node_types.py` — `EXCLUDED_LAUNCHES` entry.
- `frontend/src/api/client.ts` — `annotationSubsetExport`.
- `frontend/src/components/AnnotationFeatureTable.tsx` — the export control.

---

### Task 1: Record a line number on every parsed feature

Covers AE-1, AE-2, AE-2a, AE-2b, AE-5.

**Files:**
- Modify: `backend/app/pipelines/annotation_parse.py:16-36` (the dataclass), `:111`, `:144`, `:197` (the three parsers)
- Test: `backend/tests/pipelines/test_annotation_parse.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_annotation_parse.py`:

```python
class TestLineNumbers:
    """The line number is what makes a feature addressable back to its
    source line, which is the whole basis of subset export: the parser
    stores neither the GFF `source` column nor `phase`, so a reconstructed
    line would lose reading frame."""

    def test_gff_records_the_line_it_came_from(self):
        line = "chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1"
        assert parse_gff_line(line, line_no=42).line_no == 42

    def test_gtf_records_the_line_it_came_from(self):
        line = 'chr1\tX\tgene\t1\t100\t.\t+\t.\tgene_id "g1";'
        assert parse_gtf_line(line, line_no=7).line_no == 7

    def test_bed_records_the_line_it_came_from(self):
        assert parse_bed_line("chr1\t0\t100", line_no=3).line_no == 3

    def test_line_number_is_optional(self):
        """The three parsers are dispatched through one dict in _line_rows
        and called with a single positional argument, so their signatures
        must stay interchangeable."""
        assert parse_gff_line("chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1").line_no is None
        assert parse_bed_line("chr1\t0\t100").line_no is None

    def test_multi_parent_rows_share_one_line_number(self):
        """Parent=a,b writes one row per relationship; all came from the
        same source line."""
        line = "chr1\tX\texon\t1\t100\t.\t+\t.\tID=e1;Parent=t1,t2"
        f = parse_gff_line(line, line_no=9)
        assert len(f.parents) == 2
        assert f.line_no == 9
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_parse.py::TestLineNumbers -q
```

Expected: FAIL — `TypeError: parse_gff_line() got an unexpected keyword argument 'line_no'`.

- [ ] **Step 3: Add the field**

In `backend/app/pipelines/annotation_parse.py`, add to `Feature` (after `attributes`):

```python
    # The 1-based line of the source file this feature was parsed from, or
    # None when it has no single line (GenBank's multi-line and synthetic
    # records). This is what subset export selects on: it re-emits original
    # source lines rather than reconstructing them, because `source` and
    # `phase` are not stored above and a rebuilt CDS line would carry a `.`
    # where a reading frame belongs.
    line_no: int | None = None
```

It must be last and defaulted — `Feature` is frozen and constructed positionally in places, and existing test helpers omit it.

- [ ] **Step 4: Thread it through the three parsers**

Change each signature and add the argument to each `Feature(...)` call:

```python
def parse_gff_line(line: str, line_no: int | None = None) -> Feature | None:
```
```python
def parse_gtf_line(line: str, line_no: int | None = None) -> Feature | None:
```
```python
def parse_bed_line(line: str, line_no: int | None = None) -> Feature | None:
```

In each of the three `return Feature(...)` expressions add `line_no=line_no,` as the final keyword argument.

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_parse.py -q
```

Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/annotation_parse.py backend/tests/pipelines/test_annotation_parse.py
git commit -m "feat(pipelines): record the source line each annotation feature came from"
```

---

### Task 2: Store the line number in the features database

Covers AE-3, AE-4.

**Files:**
- Modify: `backend/app/pipelines/annotation_db.py:27-30` (`_COLUMNS`), `:84-99` (schema), `:112-132` (inserts)
- Test: `backend/tests/pipelines/test_annotation_db.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_annotation_db.py`:

```python
class TestLineNumberColumn:
    def test_line_number_round_trips(self, tmp_path):
        db = tmp_path / "f.db"
        rows = [
            Feature(
                contig="chr1", start=1, end=100, type="gene", strand="+",
                score=None, name="g1", feature_id="g1", parents=(),
                biotype=None, attributes="ID=g1", line_no=5,
            )
        ]
        build_annotation_db(rows=rows, db_path=db)
        page = query_features(
            db_path=db, filters=FeatureFilters(), offset=0, limit=10
        )
        assert page[0]["line_no"] == 5

    def test_line_number_may_be_absent(self, tmp_path):
        """GenBank features span multiple lines and its segment children are
        synthetic, so they have no single source line. Null must mean 'not
        addressable by line', never 'not recorded yet'."""
        db = tmp_path / "f.db"
        rows = [
            Feature(
                contig="chr1", start=1, end=100, type="gene", strand="+",
                score=None, name="g1", feature_id="g1", parents=(),
                biotype=None, attributes="ID=g1",
            )
        ]
        build_annotation_db(rows=rows, db_path=db)
        page = query_features(
            db_path=db, filters=FeatureFilters(), offset=0, limit=10
        )
        assert page[0]["line_no"] is None
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_db.py::TestLineNumberColumn -q
```

Expected: FAIL — `KeyError: 'line_no'`.

- [ ] **Step 3: Add the column**

In `backend/app/pipelines/annotation_db.py`, extend `_COLUMNS`:

```python
_COLUMNS = (
    "contig, start, end, type, strand, score, name, feature_id, "
    "parent, biotype, attributes, parent_status, depth, line_no"
)
```

In the `CREATE TABLE features` statement, add before the closing paren (after `depth`):

```sql
              line_no    INTEGER
```

- [ ] **Step 4: Write it on insert**

In `build_annotation_db`, the batch append becomes:

```python
                batch.append(
                    (
                        f.contig, f.start, f.end, f.type, f.strand, f.score,
                        f.name, f.feature_id, parent, f.biotype, f.attributes,
                        f.line_no,
                    )
                )
```

Both `executemany` calls (the in-loop flush and the trailing one) become:

```python
                con.executemany(
                    "INSERT INTO features (contig, start, end, type, strand, "
                    "score, name, feature_id, parent, biotype, attributes, "
                    "line_no) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", batch
                )
```

Note the twelfth `?`. Both call sites must change — they are identical strings today and must stay identical.

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_db.py -q
```

Expected: PASS, all tests in the file.

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/annotation_db.py backend/tests/pipelines/test_annotation_db.py
git commit -m "feat(pipelines): store each feature's source line in the feature index"
```

---

### Task 3: Feed real line numbers from the handler

Covers AE-2b end to end.

**Files:**
- Modify: `backend/app/queue/annotation_handlers.py:51-96` (`_line_rows`)
- Test: `backend/tests/queue/test_annotation_export_handler.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_annotation_export_handler.py`:

```python
"""The export handler and the line numbers it depends on."""

from pathlib import Path

from app.pipelines.annotation_db import FeatureFilters, query_features
from app.queue.annotation_handlers import _line_rows
from app.pipelines import annotation_parse


class _Ctx:
    def check_cancel(self):
        return None


class _Acc:
    def __init__(self):
        self.rows = []

    def add(self, f):
        self.rows.append(f)

    def add_malformed(self):
        return None

    def add_attribute_keys(self, keys):
        return None


class TestLineNumbersFromFile:
    def test_line_numbers_count_comments_and_blanks(self, tmp_path):
        """The number addresses the file, not the features in it -- so a
        comment header does not shift every feature's recorded line."""
        src = tmp_path / "a.gff3"
        src.write_text(
            "##gff-version 3\n"
            "# a comment\n"
            "\n"
            "chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n"
            "chr1\tX\tgene\t200\t300\t.\t+\t.\tID=g2\n"
        )
        acc = _Acc()
        rows = list(
            _line_rows(
                annotation_parse.parse_gff_line, src, _Ctx(), acc, [], "gff"
            )
        )
        assert [r.line_no for r in rows] == [4, 5]
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_export_handler.py -q
```

Expected: FAIL — `assert [None, None] == [4, 5]`.

- [ ] **Step 3: Pass the line number**

In `backend/app/queue/annotation_handlers.py`, inside `_line_rows`, change:

```python
            feature = parse_line(stripped)
```

to:

```python
            # i is 0-based and counts every line including comments and
            # blanks, which is what makes this address the file rather than
            # the features in it -- export re-reads exactly this line.
            feature = parse_line(stripped, i + 1)
```

Positional, not keyword: the three parsers are dispatched through the dict at line 189 and must stay interchangeable.

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_export_handler.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/annotation_handlers.py backend/tests/queue/test_annotation_export_handler.py
git commit -m "feat(pipelines): number annotation features by their line in the file"
```

---

### Task 4: Record the source hash when the index is built

Covers AE-16's write side.

The launcher already computes the digest and puts it in the payload as
`annotation_sha256` (`backend/app/services/pipeline_service.py:2566`). The
handler simply does not keep it. This task stores it in facts so export can
compare.

**Files:**
- Modify: `backend/app/queue/annotation_handlers.py:239-257` (the `facts` dict)
- Test: `backend/tests/queue/test_annotation_export_handler.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/queue/test_annotation_export_handler.py`:

```python
class TestSourceHashRecorded:
    def test_facts_carry_the_source_hash(self, tmp_path, monkeypatch):
        """Export compares this against the object's current blob_sha256.
        Without it, a rebuilt index over a replaced file exports a subset
        that silently mixes two versions."""
        from app.config import settings
        from app.queue import annotation_handlers

        src = tmp_path / "a.gff3"
        src.write_text("chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n")
        monkeypatch.setattr(
            settings.__class__, "annotation_stats_dir",
            property(lambda self: tmp_path / "stats"),
        )

        class Ctx:
            payload = {
                "object_id": "507f1f77bcf86cd799439011",
                "format_kind": "gff",
                "annotation_path": str(src),
                "annotation_sha256": "abc123",
                "contig_lengths": [],
            }

            def check_cancel(self):
                return None

            def progress(self, **kw):
                return None

        out = annotation_handlers.run_annotation_stats(Ctx())
        assert out["facts"]["annotation_source_sha256"] == "abc123"
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_export_handler.py::TestSourceHashRecorded -q
```

Expected: FAIL — `KeyError: 'annotation_source_sha256'`.

- [ ] **Step 3: Record it**

In `run_annotation_stats`, add to the `facts` dict (after
`annotation_contig_lengths_known`):

```python
        # What export verifies the source against. Null for a file the
        # launcher could not digest (register-in-place, or hashing still
        # queued); export then proceeds on per-line verification alone and
        # says so in the exported object's facts.
        "annotation_source_sha256": ctx.payload.get("annotation_sha256"),
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_export_handler.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/annotation_handlers.py backend/tests/queue/test_annotation_export_handler.py
git commit -m "feat(pipelines): record which file version an annotation index was built from"
```

---

### Task 5: The closure walk

Covers AE-10 (`test_includes_matched_features`), AE-11
(`test_includes_ancestors`), AE-12 (`test_includes_descendants`), AE-14
(`test_terminates_on_a_cycle`), and AE-15 (the `DEPTH_CAP` bound in `_walk`).
AE-13 is asserted against real data in Task 7.

**Files:**
- Create: `backend/app/pipelines/annotation_export.py`
- Test: `backend/tests/pipelines/test_annotation_export.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_annotation_export.py`:

```python
"""Subset export: which lines are selected, and whether they can be trusted.

Separate from annotation_db because this is the only place the parent/child
closure lives, and from annotation_hierarchy because that resolves status
for the whole file while this walks one filtered set.
"""

import pytest

from app.pipelines.annotation_db import (
    FeatureFilters,
    build_annotation_db,
)
from app.pipelines.annotation_export import closure_lines
from app.pipelines.annotation_hierarchy import resolve_hierarchy
from app.pipelines.annotation_parse import Feature


def _f(feature_id, parent, line_no, type="gene", start=1, end=100):
    return Feature(
        contig="chr1", start=start, end=end, type=type, strand="+",
        score=None, name=feature_id, feature_id=feature_id,
        parents=(parent,) if parent else (), biotype=None,
        attributes=f"ID={feature_id}", line_no=line_no,
    )


@pytest.fixture
def gene_tree(tmp_path):
    """gene g1 -> mRNA t1 -> exons e1,e2; plus an unrelated gene g2."""
    db = tmp_path / "f.db"
    build_annotation_db(
        rows=[
            _f("g1", None, 1),
            _f("t1", "g1", 2, type="mRNA"),
            _f("e1", "t1", 3, type="exon"),
            _f("e2", "t1", 4, type="exon"),
            _f("g2", None, 5),
        ],
        db_path=db,
    )
    resolve_hierarchy(db_path=db)
    return db


class TestClosure:
    def test_includes_matched_features(self, gene_tree):
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(feature_type="exon", top_level_only=False),
        )
        assert {3, 4} <= lines

    def test_includes_ancestors(self, gene_tree):
        """A Parent= naming a feature absent from the file makes the export
        fail in downstream tools."""
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(feature_type="exon", top_level_only=False),
        )
        assert {1, 2} <= lines

    def test_includes_descendants(self, gene_tree):
        """A gene exported without its transcripts is valid and useless."""
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(name_query="g1", top_level_only=False),
        )
        assert lines == {1, 2, 3, 4}

    def test_excludes_unrelated_features(self, gene_tree):
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(name_query="g1", top_level_only=False),
        )
        assert 5 not in lines

    def test_terminates_on_a_cycle(self, tmp_path):
        """A malformed file whose parents form a loop must not hang."""
        db = tmp_path / "cycle.db"
        build_annotation_db(
            rows=[_f("a", "b", 1), _f("b", "a", 2)], db_path=db
        )
        resolve_hierarchy(db_path=db)
        lines = closure_lines(
            db_path=db,
            filters=FeatureFilters(name_query="a", top_level_only=False),
        )
        assert lines == {1, 2}

    def test_features_without_a_line_are_skipped(self, tmp_path):
        """GenBank rows carry no line number and cannot be re-emitted."""
        db = tmp_path / "nl.db"
        build_annotation_db(rows=[_f("g1", None, None)], db_path=db)
        resolve_hierarchy(db_path=db)
        lines = closure_lines(
            db_path=db, filters=FeatureFilters(top_level_only=False)
        )
        assert lines == set()
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_export.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.annotation_export'`.

- [ ] **Step 3: Write the module**

Create `backend/app/pipelines/annotation_export.py`:

```python
"""Exporting a filtered slice of an annotation as a new file.

The constraint everything here follows from: original source lines are
re-emitted, never reconstructed. `annotation_parse.Feature` stores neither
the GFF `source` column nor `phase`, and converts BED to one-based, so a
rebuilt CDS line would carry a `.` where a reading frame belongs -- valid
syntax, wrong biology, and silent. So the unit of export is a line number,
not a feature.
"""

import sqlite3
from pathlib import Path

from app.pipelines.annotation_db import FeatureFilters, _connect, _where
from app.pipelines.annotation_hierarchy import DEPTH_CAP


def closure_lines(*, db_path: Path, filters: FeatureFilters) -> set[int]:
    """Source lines of every matched feature, its ancestors, and its
    descendants.

    Ancestors are not optional: a `Parent=` reference to a feature absent
    from the output makes the file fail in downstream tools. Descendants
    are not either -- a gene without its transcripts is valid and useless.

    Walked level by level rather than with a recursive CTE, matching
    `_assign_depths`: the DEPTH_CAP bound then counts tree depth in the
    same units the rest of the module does, and a cycle terminates at the
    cap instead of recursing.

    Features with no line number (GenBank's multi-line and synthetic rows)
    are skipped -- they are not addressable and cannot be re-emitted.
    """
    where, args = _where(filters)
    con = _connect(db_path)
    try:
        matched_ids = {
            row[0]
            for row in con.execute(
                f"SELECT feature_id FROM features{where}", args
            )
            if row[0]
        }
        lines = {
            row[0]
            for row in con.execute(
                f"SELECT line_no FROM features{where}", args
            )
            if row[0] is not None
        }

        lines |= _walk(con, matched_ids, "up")
        lines |= _walk(con, matched_ids, "down")
    finally:
        con.close()
    return lines


def _walk(con: sqlite3.Connection, seed_ids: set[str], direction: str) -> set[int]:
    """Line numbers reached from `seed_ids` by following parent links.

    `up` follows each row's `parent` to its parent's row; `down` finds rows
    whose `parent` is one of the frontier. Bounded by DEPTH_CAP levels, and
    by `seen` so a cycle cannot revisit a node.
    """
    lines: set[int] = set()
    seen: set[str] = set(seed_ids)
    frontier = set(seed_ids)

    for _ in range(DEPTH_CAP):
        if not frontier:
            break
        placeholders = ",".join("?" for _ in frontier)
        if direction == "up":
            sql = (
                f"SELECT parent.feature_id, parent.line_no FROM features child "
                f"JOIN features parent ON parent.feature_id = child.parent "
                f"WHERE child.feature_id IN ({placeholders})"
            )
        else:
            sql = (
                f"SELECT feature_id, line_no FROM features "
                f"WHERE parent IN ({placeholders})"
            )
        rows = con.execute(sql, list(frontier)).fetchall()

        next_frontier: set[str] = set()
        for feature_id, line_no in rows:
            if line_no is not None:
                lines.add(line_no)
            if feature_id and feature_id not in seen:
                seen.add(feature_id)
                next_frontier.add(feature_id)
        frontier = next_frontier

    return lines
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_export.py -q
```

Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_export.py backend/tests/pipelines/test_annotation_export.py
git commit -m "feat(pipelines): walk the parent/child closure of a filtered feature set"
```

---

### Task 6: Verified re-emission, header copying, and the name

Covers AE-20, AE-21, AE-26, AE-27, AE-28, AE-28a, AE-29.

**Files:**
- Modify: `backend/app/pipelines/annotation_export.py`
- Test: `backend/tests/pipelines/test_annotation_export.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_annotation_export.py`:

```python
from app.pipelines.annotation_export import (
    ExportMismatch,
    subset_name,
    write_subset,
)


class TestWriteSubset:
    def test_emits_selected_lines_verbatim(self, tmp_path):
        src = tmp_path / "a.gff3"
        src.write_text(
            "##gff-version 3\n"
            "chr1\tHAVANA\tgene\t1\t100\t.\t+\t.\tID=g1\n"
            "chr1\tHAVANA\tCDS\t1\t100\t.\t+\t2\tID=c1;Parent=g1\n"
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines={3}, verify=None)
        text = out.read_text()
        assert "chr1\tHAVANA\tCDS\t1\t100\t.\t+\t2\tID=c1;Parent=g1" in text

    def test_preserves_the_phase_column(self, tmp_path):
        """The reason for re-emitting rather than reconstructing: `phase`
        is not stored on Feature at all."""
        src = tmp_path / "a.gff3"
        src.write_text("chr1\tX\tCDS\t1\t100\t.\t+\t2\tID=c1\n")
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines={1}, verify=None)
        assert out.read_text().split("\t")[7] == "2"

    def test_copies_the_header(self, tmp_path):
        """A GFF3 without ##gff-version 3 is malformed, and headers are not
        features so the closure never selects them."""
        src = tmp_path / "a.gff3"
        src.write_text(
            "##gff-version 3\n"
            "##sequence-region chr1 1 1000\n"
            "chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n"
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines={3}, verify=None)
        lines = out.read_text().splitlines()
        assert lines[0] == "##gff-version 3"
        assert lines[1] == "##sequence-region chr1 1 1000"

    def test_header_is_not_truncated_at_the_display_cap(self, tmp_path):
        """_HEADER_SCAN_LINES bounds what is *displayed*; reusing it here
        would silently drop a long ##sequence-region header."""
        src = tmp_path / "a.gff3"
        header = "".join(f"##sequence-region c{i} 1 10\n" for i in range(80))
        src.write_text(header + "chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n")
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines={81}, verify=None)
        assert out.read_text().count("##sequence-region") == 80

    def test_emits_in_file_order(self, tmp_path):
        src = tmp_path / "a.gff3"
        src.write_text(
            "chr1\tX\tgene\t1\t10\t.\t+\t.\tID=a\n"
            "chr1\tX\tgene\t2\t20\t.\t+\t.\tID=b\n"
            "chr1\tX\tgene\t3\t30\t.\t+\t.\tID=c\n"
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines={3, 1}, verify=None)
        ids = [l.split("ID=")[1] for l in out.read_text().splitlines()]
        assert ids == ["a", "c"]

    def test_rejects_a_line_that_no_longer_matches(self, tmp_path):
        """A wrong-but-plausible annotation file is worse than no file."""
        src = tmp_path / "a.gff3"
        src.write_text("chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n")
        out = tmp_path / "out.gff3"
        with pytest.raises(ExportMismatch):
            write_subset(
                source=src, dest=out, lines={1},
                verify={1: {"contig": "chr1", "start": 999, "end": 100}},
            )

    def test_accepts_a_line_that_matches(self, tmp_path):
        src = tmp_path / "a.gff3"
        src.write_text("chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n")
        out = tmp_path / "out.gff3"
        write_subset(
            source=src, dest=out, lines={1},
            verify={1: {"contig": "chr1", "start": 1, "end": 100}},
        )
        assert "ID=g1" in out.read_text()


class TestSubsetName:
    def test_uses_a_single_filter(self):
        assert subset_name("GRCh38.gff3", {"contig": "chr21"}) == "GRCh38.chr21.gff3"

    def test_uses_two_filters(self):
        name = subset_name("GRCh38.gff3", {"contig": "chr21", "feature_type": "exon"})
        assert name == "GRCh38.chr21.exon.gff3"

    def test_falls_back_past_two(self):
        """Four active filters make an unreadable name; facts carry the
        complete filter regardless."""
        name = subset_name(
            "GRCh38.gff3",
            {"contig": "chr21", "feature_type": "exon",
             "strand": "+", "biotype": "protein_coding"},
        )
        assert name == "GRCh38.subset.gff3"

    def test_handles_a_compound_extension(self):
        assert subset_name("GRCh38.gff3.gz", {"contig": "chr21"}) == (
            "GRCh38.chr21.gff3.gz"
        )
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_export.py -q
```

Expected: FAIL — `ImportError: cannot import name 'ExportMismatch'`.

- [ ] **Step 3: Implement**

Append to `backend/app/pipelines/annotation_export.py`:

```python
class ExportMismatch(Exception):
    """A source line no longer parses to the feature the index recorded.

    Raised rather than skipped: a subset that quietly drops or substitutes
    features is a wrong-but-plausible annotation file, which is worse than
    no file at all.
    """


# The filters that make a readable name, in the order they are tried. Kept
# to the ones a person would actually say out loud about a subset.
_NAME_KEYS = ("contig", "feature_type", "biotype", "strand")

# Every suffix that is part of an annotation's name rather than a filter
# slot, so `a.gff3.gz` keeps both.
_COMPOUND_SUFFIXES = (".gz", ".bgz")


def subset_name(source_name: str, active: dict) -> str:
    """The exported file's name: the source's, with up to two filters.

    Past two the name stops being readable, so it falls back to `subset`.
    The complete filter is recorded in the object's facts either way, so
    nothing is lost -- this only decides what is legible in a file list.
    """
    stem = source_name
    suffixes = ""
    for compound in _COMPOUND_SUFFIXES:
        if stem.endswith(compound):
            suffixes = compound + suffixes
            stem = stem[: -len(compound)]
            break
    if "." in stem:
        stem, _, ext = stem.rpartition(".")
        suffixes = f".{ext}" + suffixes

    parts = [str(active[k]) for k in _NAME_KEYS if active.get(k)]
    label = ".".join(parts) if 0 < len(parts) <= 2 else "subset"
    return f"{stem}.{label}{suffixes}"


def write_subset(
    *,
    source: Path,
    dest: Path,
    lines: set[int],
    verify: dict[int, dict] | None,
) -> int:
    """Copy `lines` from `source` to `dest`, header first, in file order.

    One sequential pass rather than seeking per line: the lines are spread
    through the file and a 3M-line GFF3 read once is cheaper than tens of
    thousands of seeks.

    `verify` maps a line number to the contig/start/end the index recorded
    for it. Each selected line is re-parsed and compared; a disagreement
    raises. Pass None only in tests that are exercising emission itself.

    Headers are read from the file directly rather than through
    `run_annotation_stats`'s `_HEADER_SCAN_LINES`, which bounds what is
    *displayed* and would silently truncate a long ##sequence-region block.

    Returns the number of feature lines written.
    """
    from app.pipelines import annotation_parse

    written = 0
    with open(source, errors="replace") as fh, open(dest, "w") as out:
        in_header = True
        for i, line in enumerate(fh, start=1):
            stripped = line.rstrip("\n")
            if in_header and stripped.startswith("#"):
                out.write(line if line.endswith("\n") else line + "\n")
                continue
            if stripped:
                in_header = False
            if i not in lines:
                continue
            if verify is not None:
                expected = verify.get(i)
                parsed = annotation_parse.parse_gff_line(stripped, i)
                if expected is not None and (
                    parsed is None
                    or parsed.contig != expected["contig"]
                    or parsed.start != expected["start"]
                    or parsed.end != expected["end"]
                ):
                    raise ExportMismatch(
                        f"line {i} of {source.name} no longer matches the "
                        f"computed index; recompute results and try again"
                    )
            out.write(line if line.endswith("\n") else line + "\n")
            written += 1
    return written
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_export.py -q
```

Expected: PASS, 17 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_export.py backend/tests/pipelines/test_annotation_export.py
git commit -m "feat(pipelines): re-emit verified annotation source lines with their header"
```

---

### Task 7: Fidelity against a real NCBI GFF3

Covers the spec's testing section. This is the task that catches what
hand-built fixtures cannot.

**Files:**
- Create: `backend/tests/fixtures/annotation/ncbi_slice.gff3`
- Test: `backend/tests/pipelines/test_annotation_export.py`

- [ ] **Step 1: Create the fixture**

A real NCBI GFF3 slice, following `fixtures/genbank/ecoli_slice.gbff`. It must
contain: the standard `##gff-version 3` and `#!` pragma header, at least one
complete `gene → mRNA → exon/CDS` tree, CDS rows with a **non-zero `phase`**
column, and a populated `source` column (`RefSeq`/`Gnomon`).

```bash
mkdir -p backend/tests/fixtures/annotation
```

Write `backend/tests/fixtures/annotation/ncbi_slice.gff3`:

```
##gff-version 3
#!gff-spec-version 1.21
#!processor NCBI annotwriter
##sequence-region NC_000021.9 1 46709983
NC_000021.9	RefSeq	region	1	46709983	.	+	.	ID=NC_000021.9:1..46709983;Dbxref=taxon:9606;chromosome=21
NC_000021.9	BestRefSeq	gene	5011799	5017145	.	+	.	ID=gene-LOC102723996;Name=LOC102723996;gene_biotype=protein_coding
NC_000021.9	BestRefSeq	mRNA	5011799	5017145	.	+	.	ID=rna-XM_011529554.1;Parent=gene-LOC102723996;Name=XM_011529554.1
NC_000021.9	BestRefSeq	exon	5011799	5011874	.	+	.	ID=exon-XM_011529554.1-1;Parent=rna-XM_011529554.1
NC_000021.9	BestRefSeq	exon	5015000	5015200	.	+	.	ID=exon-XM_011529554.1-2;Parent=rna-XM_011529554.1
NC_000021.9	BestRefSeq	CDS	5011820	5011874	.	+	0	ID=cds-XP_011527856.1;Parent=rna-XM_011529554.1;Name=XP_011527856.1
NC_000021.9	BestRefSeq	CDS	5015000	5015200	.	+	2	ID=cds-XP_011527856.1;Parent=rna-XM_011529554.1;Name=XP_011527856.1
NC_000021.9	Gnomon	gene	5022493	5040666	.	-	.	ID=gene-LOC100forward;Name=LOC100forward;gene_biotype=lncRNA
NC_000021.9	Gnomon	lnc_RNA	5022493	5040666	.	-	.	ID=rna-XR_001754960.1;Parent=gene-LOC100forward
NC_000021.9	Gnomon	exon	5040500	5040666	.	-	.	ID=exon-XR_001754960.1-1;Parent=rna-XR_001754960.1
```

Note line 12 (`CDS ... 2`): the non-zero phase is the point of the fixture.

- [ ] **Step 2: Write the failing test**

Append to `backend/tests/pipelines/test_annotation_export.py`:

```python
FIXTURES = Path(__file__).parent.parent / "fixtures" / "annotation"


class TestRealNcbiFidelity:
    """Against a real NCBI GFF3, not hand-built Feature objects.

    Hand-built fixtures feed the code objects that already look the way it
    expects -- how the suggestion-rules and STAR failures passed a green
    suite while being wrong. The columns asserted here (`source`, `phase`)
    are exactly the ones Feature does not store, so a reconstruction-based
    implementation passes every other test in this file and fails these.
    """

    def _index(self, tmp_path):
        from app.pipelines import annotation_parse

        src = FIXTURES / "ncbi_slice.gff3"
        rows = []
        with open(src) as fh:
            for i, line in enumerate(fh, start=1):
                stripped = line.rstrip("\n")
                if not stripped or stripped.startswith("#"):
                    continue
                f = annotation_parse.parse_gff_line(stripped, i)
                if f is not None:
                    rows.append(f)
        db = tmp_path / "real.db"
        build_annotation_db(rows=rows, db_path=db)
        resolve_hierarchy(db_path=db)
        return src, db

    def test_exported_lines_are_byte_identical(self, tmp_path):
        src, db = self._index(tmp_path)
        lines = closure_lines(
            db_path=db,
            filters=FeatureFilters(feature_type="CDS", top_level_only=False),
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines=lines, verify=None)

        source_lines = src.read_text().splitlines()
        for emitted in out.read_text().splitlines():
            if emitted.startswith("#"):
                continue
            assert emitted in source_lines

    def test_phase_survives(self, tmp_path):
        """The column Feature has no field for."""
        src, db = self._index(tmp_path)
        lines = closure_lines(
            db_path=db,
            filters=FeatureFilters(feature_type="CDS", top_level_only=False),
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines=lines, verify=None)
        cds = [
            l.split("\t") for l in out.read_text().splitlines()
            if not l.startswith("#") and l.split("\t")[2] == "CDS"
        ]
        assert {row[7] for row in cds} == {"0", "2"}

    def test_source_column_survives(self, tmp_path):
        """Column 2, also absent from Feature."""
        src, db = self._index(tmp_path)
        lines = closure_lines(
            db_path=db,
            filters=FeatureFilters(feature_type="CDS", top_level_only=False),
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines=lines, verify=None)
        for line in out.read_text().splitlines():
            if line.startswith("#"):
                continue
            assert line.split("\t")[1] in ("RefSeq", "BestRefSeq", "Gnomon")

    def test_no_dangling_parent_references(self, tmp_path):
        """AE-13, against a real hierarchy."""
        src, db = self._index(tmp_path)
        lines = closure_lines(
            db_path=db,
            filters=FeatureFilters(feature_type="CDS", top_level_only=False),
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines=lines, verify=None)

        from app.pipelines.annotation_parse import parse_gff_attributes

        present, referenced = set(), set()
        for line in out.read_text().splitlines():
            if line.startswith("#"):
                continue
            attrs = parse_gff_attributes(line.split("\t")[8])
            if attrs.get("ID"):
                present.add(attrs["ID"])
            for parent in attrs.get("Parent", "").split(","):
                if parent:
                    referenced.add(parent)
        assert referenced <= present
```

Add `from pathlib import Path` to the file's imports.

- [ ] **Step 3: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_export.py::TestRealNcbiFidelity -q
```

Expected: PASS, 4 tests. If `test_phase_survives` fails, the implementation is
reconstructing lines rather than copying them — go back to Task 6.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/fixtures/annotation/ncbi_slice.gff3 backend/tests/pipelines/test_annotation_export.py
git commit -m "test(pipelines): verify subset export against a real NCBI GFF3"
```

---

### Task 8: The export handler

Covers AE-16, AE-17, AE-18, AE-19, AE-25, AE-33.

**Files:**
- Modify: `backend/app/queue/annotation_handlers.py`
- Test: `backend/tests/queue/test_annotation_export_handler.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/queue/test_annotation_export_handler.py`:

```python
import pytest

from app.errors import PermanentError


def _payload(tmp_path, src, db, **over):
    base = {
        "object_id": "507f1f77bcf86cd799439011",
        "project_id": "507f1f77bcf86cd799439012",
        "annotation_path": str(src),
        "db_path": str(db),
        "source_sha256": "abc123",
        "recorded_sha256": "abc123",
        "source_name": "a.gff3",
        "filters": {"feature_type": "gene"},
        "out_dir": str(tmp_path / "out"),
    }
    base.update(over)
    return base


class _ExportCtx:
    def __init__(self, payload):
        self.payload = payload

    def check_cancel(self):
        return None

    def progress(self, **kw):
        return None


@pytest.fixture
def indexed(tmp_path):
    from app.pipelines.annotation_db import build_annotation_db
    from app.pipelines.annotation_hierarchy import resolve_hierarchy
    from app.pipelines import annotation_parse

    src = tmp_path / "a.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n"
        "chr1\tX\texon\t1\t50\t.\t+\t.\tID=e1;Parent=g1\n"
    )
    rows = []
    with open(src) as fh:
        for i, line in enumerate(fh, start=1):
            s = line.rstrip("\n")
            if s and not s.startswith("#"):
                rows.append(annotation_parse.parse_gff_line(s, i))
    db = tmp_path / "f.db"
    build_annotation_db(rows=rows, db_path=db)
    resolve_hierarchy(db_path=db)
    return src, db


class TestExportHandler:
    def test_writes_a_file_and_reports_counts(self, tmp_path, indexed):
        from app.queue.annotation_handlers import run_annotation_subset_export

        src, db = indexed
        out = run_annotation_subset_export(
            _ExportCtx(_payload(tmp_path, src, db))
        )
        assert out["counts"]["matched"] == 1
        # The gene matched; its exon came in through the closure.
        assert out["counts"]["exported"] == 2
        assert Path(out["output"]["tmp_path"]).exists()

    def test_rejects_a_changed_source(self, tmp_path, indexed):
        """The stale-index case per-line verification cannot catch."""
        src, db = indexed
        from app.queue.annotation_handlers import run_annotation_subset_export

        with pytest.raises(PermanentError) as e:
            run_annotation_subset_export(
                _ExportCtx(
                    _payload(tmp_path, src, db, source_sha256="different")
                )
            )
        assert "recompute" in str(e.value).lower()

    def test_proceeds_without_a_recorded_hash(self, tmp_path, indexed):
        src, db = indexed
        from app.queue.annotation_handlers import run_annotation_subset_export

        out = run_annotation_subset_export(
            _ExportCtx(_payload(tmp_path, src, db, recorded_sha256=None))
        )
        assert out["facts"]["annotation_subset_source_verified"] is False

    def test_records_verified_when_hashes_agree(self, tmp_path, indexed):
        src, db = indexed
        from app.queue.annotation_handlers import run_annotation_subset_export

        out = run_annotation_subset_export(
            _ExportCtx(_payload(tmp_path, src, db))
        )
        assert out["facts"]["annotation_subset_source_verified"] is True

    def test_rejects_genbank(self, tmp_path, indexed):
        src, db = indexed
        from app.queue.annotation_handlers import run_annotation_subset_export

        with pytest.raises(PermanentError):
            run_annotation_subset_export(
                _ExportCtx(
                    _payload(tmp_path, src, db, format_kind="genbank")
                )
            )
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_export_handler.py::TestExportHandler -q
```

Expected: FAIL — `ImportError: cannot import name 'run_annotation_subset_export'`.

- [ ] **Step 3: Write the handler**

Append to `backend/app/queue/annotation_handlers.py`. Add
`annotation_export` to the existing `from app.pipelines import (...)` block
first.

```python
@handler(
    "annotation_subset_export",
    # THREAD for the same reason as run_annotation_stats: this is Python
    # file I/O and SQLite, with no binary to spawn or kill.
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
)
def run_annotation_subset_export(ctx: JobContext) -> dict:
    """Write the filtered subset of an annotation to a new file."""
    payload = ctx.payload
    if payload.get("format_kind") == "genbank":
        raise PermanentError(
            "GenBank features span multiple lines and its segment children "
            "are synthetic, so a subset cannot be re-emitted from it"
        )

    source = Path(payload["annotation_path"])
    if not source.exists():
        raise PermanentError(f"annotation file is missing: {source}")

    db_path = Path(payload["db_path"])
    if not db_path.exists():
        raise PermanentError(
            "No computed results for this annotation. Compute results first."
        )

    # The whole-file check, before any line is read. Per-line verification
    # alone passes on a stale index whose file was replaced with one that is
    # mostly unchanged -- the exported lines each verify while the subset
    # silently mixes two versions.
    recorded = payload.get("recorded_sha256")
    current = payload.get("source_sha256")
    verified = bool(recorded) and bool(current)
    if verified and recorded != current:
        raise PermanentError(
            f"{payload.get('source_name') or source.name} has changed since "
            f"its results were computed; recompute results and try again"
        )

    # parent_status is declared a tuple but arrives from the JSON payload as
    # a list -- the queue serializes the launcher's dataclasses.asdict(). The
    # IN clause iterates either, so nothing breaks today; restoring the tuple
    # keeps the frozen dataclass's declared type honest for the next reader.
    raw_filters = dict(payload.get("filters") or {})
    if raw_filters.get("parent_status") is not None:
        raw_filters["parent_status"] = tuple(raw_filters["parent_status"])
    filters = annotation_db.FeatureFilters(**raw_filters)

    ctx.progress(phase="select", pct=0.2, message="selecting features")
    matched = annotation_db.count_features(db_path=db_path, filters=filters)
    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    ctx.progress(phase="write", pct=0.6, message="writing subset")
    out_dir = Path(payload["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    name = annotation_export.subset_name(
        payload.get("source_name") or source.name, payload.get("filters") or {}
    )
    dest = out_dir / name

    verify = annotation_export.verification_map(db_path=db_path, lines=lines)
    try:
        exported = annotation_export.write_subset(
            source=source, dest=dest, lines=lines, verify=verify
        )
    except annotation_export.ExportMismatch as e:
        dest.unlink(missing_ok=True)
        raise PermanentError(str(e)) from e

    log.info(
        "annotation_subset_exported",
        object_id=str(payload.get("object_id")),
        matched=matched,
        exported=exported,
    )
    return {
        "object_id": str(payload.get("object_id")),
        "output": {"tmp_path": str(dest), "name": name},
        "counts": {"matched": matched, "exported": exported},
        "facts": {
            "annotation_subset_filters": payload.get("filters") or {},
            "annotation_subset_matched": matched,
            "annotation_subset_exported": exported,
            # False when the launcher had no digest to compare (a
            # register-in-place file, or hashing still queued). The export
            # still ran on per-line verification; this makes the weaker
            # guarantee auditable rather than hidden.
            "annotation_subset_source_verified": verified,
        },
    }
```

- [ ] **Step 4: Add the verification map**

Append to `backend/app/pipelines/annotation_export.py`:

```python
def verification_map(*, db_path: Path, lines: set[int]) -> dict[int, dict]:
    """What the index recorded for each line, for `write_subset` to check.

    Read in one query rather than per line: the set is already bounded by
    the closure, and a query per line would be one round trip per feature.
    """
    if not lines:
        return {}
    con = _connect(db_path)
    try:
        placeholders = ",".join("?" for _ in lines)
        rows = con.execute(
            f"SELECT line_no, contig, start, end FROM features "
            f"WHERE line_no IN ({placeholders})",
            list(lines),
        ).fetchall()
    finally:
        con.close()
    return {
        line_no: {"contig": contig, "start": start, "end": end}
        for line_no, contig, start, end in rows
    }
```

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_export_handler.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/annotation_handlers.py backend/app/pipelines/annotation_export.py backend/tests/queue/test_annotation_export_handler.py
git commit -m "feat(pipelines): export a verified annotation subset in a queued job"
```

---

### Task 9: The launcher and its registry classification

Covers the launch path. **`test_every_launch_function_is_classified` will fail
the moment Step 1 lands** — that is the guardrail working, and Step 3 resolves it.

**Files:**
- Modify: `backend/app/services/pipeline_service.py`, `backend/app/pipelines/node_types.py:833`
- Test: `backend/tests/pipelines/test_node_types.py` (existing, no edit)

- [ ] **Step 1: Write the launcher**

Append to `backend/app/services/pipeline_service.py`:

```python
async def launch_annotation_subset_export(
    *,
    object_id: PydanticObjectId,
    filters: dict,
    owner: str,
):
    """Queue the export of an annotation's filtered subset as a new object.

    Unlike launch_annotation_stats this *does* create an object, so the
    applier in results.py is load-bearing -- see the _APPLIERS assertion in
    tests/queue/test_annotation_export_applier.py.
    """
    from app.config import settings
    from app.queue import queue
    from app.services import object_service

    ann = await object_service.get_object(object_id, owner=owner)
    _check_annotation_stats_callable(ann)

    if str(ann.format.kind.value) == "genbank":
        raise ValidationError(
            "A GenBank annotation cannot be exported as a subset"
        )

    digest, path = await _resolve_readable(ann)
    path = path or str(blob_path(digest))

    return await queue.enqueue(
        "annotation_subset_export",
        owner=owner,
        payload={
            "object_id": str(ann.id),
            "project_id": str(ann.project_id),
            "format_kind": str(ann.format.kind.value),
            "annotation_path": path,
            "db_path": str(
                settings.annotation_stats_dir / str(ann.id) / "features.db"
            ),
            "source_name": ann.name,
            "source_sha256": digest,
            # What the index was built from, recorded by run_annotation_stats.
            "recorded_sha256": ann.facts.get("annotation_source_sha256"),
            "filters": filters,
            "out_dir": str(settings.tmp_dir / "annotation_subset"),
        },
    )
```

No new imports are needed: `ValidationError` (line 17), `blob_path` (line 65),
`_resolve_readable` (line 156), and `_check_annotation_stats_callable`
(line 2469) all already exist in this file.

- [ ] **Step 2: Run the registry test to see it fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -q
```

Expected: FAIL — `test_every_launch_function_is_classified`, reporting
`pipeline_service.launch_annotation_subset_export` unclassified.

- [ ] **Step 3: Classify it**

In `backend/app/pipelines/node_types.py`, add inside the `EXCLUDED_LAUNCHES`
frozenset:

```python
        # Exports a subset of one annotation the user is looking at, driven
        # by the feature table's current filter. A canvas node would have no
        # way to express that filter, and the output is a file to download or
        # feed elsewhere rather than a pipeline step.
        "pipeline_service.launch_annotation_subset_export",
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/pipelines/node_types.py
git commit -m "feat(pipelines): launch an annotation subset export"
```

---

### Task 10: The applier, with its own assertion

Covers AE-22, AE-23, AE-24.

`_APPLIERS.get(job_type)` returns None for an unregistered type and
`_apply_result` then does nothing — the job reports **succeeded** while no
object is created. `results.py:53` records eleven appliers that sat in that
state, found only by running a real job.

**Files:**
- Modify: `backend/app/queue/results.py` (new applier + `:2692` registry)
- Test: `backend/tests/queue/test_annotation_export_applier.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/queue/test_annotation_export_applier.py`:

```python
"""The applier that turns an export job into an object.

The registry assertion is the point of this file. _APPLIERS silently skips
unknown job types, so a missing entry means the job succeeds and nothing is
ever created -- no error, no log, no object.
"""

from app.queue.results import _APPLIERS


class TestApplierIsRegistered:
    def test_export_job_type_has_an_applier(self):
        assert "annotation_subset_export" in _APPLIERS

    def test_the_applier_is_callable(self):
        assert callable(_APPLIERS["annotation_subset_export"])
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_export_applier.py -q
```

Expected: FAIL — `assert 'annotation_subset_export' in {...}`.

- [ ] **Step 3: Write the applier**

Add to `backend/app/queue/results.py`, near the other annotation appliers:

```python
async def _apply_annotation_subset_export(result: dict, *, owner: str) -> None:
    """Turn an exported subset into a derived ANNOTATION object.

    `derived_from` the source annotation rather than a sidecar: a subset is
    a file in its own right that the user downloads or feeds to another
    pipeline, not an index hanging off its parent.
    """
    from app.services import object_service

    output = result.get("output")
    object_id = result.get("object_id")
    if not output or not object_id:
        return

    source = await DataObject.get(PydanticObjectId(object_id))
    if source is None:
        log.warning("annotation_subset_parent_missing", object_id=object_id)
        return

    job_id = result.get("job_id")
    try:
        subset = await object_service.ingest_local_file(
            owner=source.owner,
            project_id=source.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.ANNOTATION,
            derived_from=[source.id],
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=dict(result.get("facts") or {}),
            # The subset describes the same biology as its source.
            metadata=dict(source.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error(
            "annotation_subset_ingest_failed", object_id=object_id, error=str(e)
        )
        return

    log.info(
        "annotation_subset_applied",
        object_id=object_id,
        subset_id=str(subset.id),
    )

    from app.services import run_service

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [subset.id], owner=subset.owner)
```

- [ ] **Step 4: Register it**

In the `_APPLIERS` dict at `backend/app/queue/results.py:2692`, after
`"run_annotation_stats": _apply_run_annotation_stats,`:

```python
    "annotation_subset_export": _apply_annotation_subset_export,
```

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_export_applier.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/results.py backend/tests/queue/test_annotation_export_applier.py
git commit -m "feat(pipelines): register an exported annotation subset as an object"
```

---

### Task 11: The shared filter builder and the export route

Covers AE-6, AE-7, AE-8, AE-9, AE-33.

**Files:**
- Modify: `backend/app/api/v1/pipelines.py:932-1001`
- Test: `backend/tests/api/test_annotation_export_endpoint.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/api/test_annotation_export_endpoint.py`:

```python
"""The export route, and the filter builder it shares with the page route."""

from app.api.v1.pipelines import build_feature_filters


class TestSharedFilterBuilder:
    """One definition of what a filter means. The page count and the export
    must agree, or the matched-versus-exported counts are meaningless."""

    def test_type_filter_clears_top_level_only(self):
        """Every exon has a parent, so leaving the flag set returns an empty
        table on a perfectly good GFF3."""
        f = build_feature_filters(feature_type="exon", view="all")
        assert f.top_level_only is False

    def test_no_type_filter_keeps_top_level_only(self):
        f = build_feature_filters(view="all")
        assert f.top_level_only is True

    def test_unresolved_view_clears_top_level_only(self):
        f = build_feature_filters(view="unresolved")
        assert f.top_level_only is False

    def test_unresolved_view_sets_parent_status(self):
        from app.pipelines import annotation_hierarchy

        f = build_feature_filters(view="unresolved")
        assert f.parent_status == annotation_hierarchy.UNRESOLVED_STATUSES

    def test_passes_through_the_plain_filters(self):
        f = build_feature_filters(
            contig="chr1", biotype="protein_coding", strand="+",
            name_query="BRCA", start_min=10, start_max=20, view="all",
        )
        assert f.contig == "chr1"
        assert f.biotype == "protein_coding"
        assert f.strand == "+"
        assert f.name_query == "BRCA"
        assert f.start_min == 10
        assert f.start_max == 20
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/api/test_annotation_export_endpoint.py -q
```

Expected: FAIL — `ImportError: cannot import name 'build_feature_filters'`.

- [ ] **Step 3: Extract the builder**

In `backend/app/api/v1/pipelines.py`, add above `get_annotation_features`:

```python
def build_feature_filters(
    *,
    contig: str | None = None,
    start_min: int | None = None,
    start_max: int | None = None,
    feature_type: str | None = None,
    biotype: str | None = None,
    name_query: str | None = None,
    strand: str | None = None,
    view: str = "all",
) -> annotation_db.FeatureFilters:
    """The one definition of what the table's filter arguments mean.

    Shared by the page route and the export route: the two must agree about
    `top_level_only`, because it decides whether "matched" counts genes or
    exons, and the export dialog reports that count next to the exported
    one.

    `features_in_window` deliberately does not come through here -- it sets
    top_level_only=False unconditionally for the track viewer, which is a
    different rule about a coordinate window rather than a filter.
    """
    unresolved = view == "unresolved"
    return annotation_db.FeatureFilters(
        contig=contig,
        start_min=start_min,
        start_max=start_max,
        feature_type=feature_type,
        biotype=biotype,
        name_query=name_query,
        strand=strand,
        # A type filter must search the whole file: every exon has a parent,
        # so leaving top_level_only set would return an empty table on a
        # valid GFF3. The Unresolved view clears it for the same reason.
        top_level_only=feature_type is None and not unresolved,
        parent_status=(
            annotation_hierarchy.UNRESOLVED_STATUSES if unresolved else None
        ),
    )
```

Then in `get_annotation_features`, replace the whole
`unresolved = view == "unresolved"` / `filters = annotation_db.FeatureFilters(...)`
block (currently lines 973-991) with:

```python
    filters = build_feature_filters(
        contig=contig,
        start_min=start_min,
        start_max=start_max,
        feature_type=feature_type,
        biotype=biotype,
        name_query=name_query,
        strand=strand,
        view=view,
    )
```

- [ ] **Step 4: Add the export route**

Append to `backend/app/api/v1/pipelines.py`:

```python
class AnnotationSubsetExportRequest(BaseModel):
    object_id: PydanticObjectId
    contig: str | None = None
    start_min: int | None = None
    start_max: int | None = None
    feature_type: str | None = None
    biotype: str | None = None
    name_query: str | None = None
    strand: str | None = None
    view: Literal["all", "unresolved"] = "all"


@router.post("/annotationstats/export")
async def export_annotation_subset(
    body: AnnotationSubsetExportRequest, owner: OwnerDep
) -> JobOut:
    """Queue the export of the feature table's current filter as a new
    ANNOTATION object, derived from the source."""
    filters = build_feature_filters(
        contig=body.contig,
        start_min=body.start_min,
        start_max=body.start_max,
        feature_type=body.feature_type,
        biotype=body.biotype,
        name_query=body.name_query,
        strand=body.strand,
        view=body.view,
    )
    job = await pipeline_service.launch_annotation_subset_export(
        object_id=body.object_id,
        filters=dataclasses.asdict(filters),
        owner=owner,
    )
    return JobOut.of(job)
```

Add `import dataclasses` to the top of the file — it is not currently
imported. `Literal` (line 5) and `BaseModel` (line 10) already are.

Note the ruff `I001` import-order rule that CI enforces and the local suite
does not: put `import dataclasses` with the other stdlib imports, not
appended at the end of the block.

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/api/test_annotation_export_endpoint.py tests/api/test_annotation_stats_endpoints.py -q
```

Expected: PASS — the existing endpoint tests confirm the extraction changed no
behaviour.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/tests/api/test_annotation_export_endpoint.py
git commit -m "feat(api): export an annotation's filtered subset through a shared filter builder"
```

---

### Task 12: The table's export control

Covers AE-30, AE-31, AE-32.

There is no headless component-testing setup in this repo (no jsdom, zero
`.test.tsx`), so this task is verified in the browser per `CLAUDE.md`.

**Files:**
- Modify: `frontend/src/api/client.ts:1156`, `frontend/src/components/AnnotationFeatureTable.tsx`

- [ ] **Step 1: Add the client call**

In `frontend/src/api/client.ts`, after `annotationFeatures`:

```ts
  /** Export the feature table's current filter as a new ANNOTATION object,
   *  derived from the source. The closure means the exported count exceeds
   *  the matched count whenever the filter hits a child. */
  annotationSubsetExport: (objectId: string, q: FeatureQuery) =>
    request<JobSummary>(`/pipelines/annotationstats/export`, {
      method: "POST",
      body: JSON.stringify({
        object_id: objectId,
        contig: q.contig || null,
        start_min: q.startMin ?? null,
        start_max: q.startMax ?? null,
        feature_type: q.featureType || null,
        biotype: q.biotype || null,
        name_query: q.nameQuery || null,
        strand: q.strand || null,
        view: q.view ?? "all",
      }),
    }),
```

- [ ] **Step 2: Add the control**

In `frontend/src/components/AnnotationFeatureTable.tsx`, derive whether a
filter is active from the existing state (`contig`, `featureType`, `biotype`,
`strand`, `locus`, and the debounced name query), and render an "Export
subset" button only when it is true and the source is not GenBank. The button
opens a confirm dialog stating both counts before it calls
`api.annotationSubsetExport`.

The matched count is the `total` the table already holds in `lastTotal`. The
exported count is not known until the job runs, so the dialog says so rather
than inventing a number:

```tsx
{filterActive && !isGenBank && (
  <button
    className="btn-secondary"
    onClick={() => setExportOpen(true)}
  >
    Export subset
  </button>
)}
```

Dialog body text:

> Export {lastTotal} matching features as a new annotation object.
>
> Parent and child features are included so the exported file has no
> dangling references, so the final count will be higher than
> {lastTotal}. Both counts are recorded on the new object.

- [ ] **Step 3: Verify in the browser**

```bash
./ops/worktree-up.sh
```

Open `localhost:5273`, go to a GFF3 object's Results → feature table, set a
filter, and confirm: the button appears only with a filter set; the dialog
shows the matched count; the export produces a new object in the project; the
new object's facts show both counts and the filter; the button is absent for
a GenBank annotation.

- [ ] **Step 4: Tear the stack down**

```bash
./ops/worktree-up.sh --down
```

A stack left up wipes other test runs' data — `conftest.py` drops every
collection in `biopipe_test` at session start.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/components/AnnotationFeatureTable.tsx
git commit -m "feat(ui): export the annotation table's current filter as an object"
```

---

### Task 13: Full suite, PR, and CI

- [ ] **Step 1: Run the whole backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count, not the exit code. If DB-touching tests fail
in a rotating pattern, check for orphaned stacks with
`./ops/worktree-up.sh --list`.

- [ ] **Step 2: Lint**

```bash
docker compose exec -T api ruff check app/
```

Run from the **main** checkout root. CI runs `ruff` with rules the local suite
never invokes — `I001` (import order) has broken a green branch here before.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill --title "feat(annotation): export a filtered annotation subset as a new object"
```

Add `Closes #358` to the body, and label the PR `type:feature`,
`area:pipelines`, `area:frontend` — `.github/release.yml` categorizes by
label, so an unlabelled PR lands under "Other changes".

- [ ] **Step 4: Watch CI to completion**

```bash
gh pr checks --watch
```

`gh pr create` returns before any check starts; a "pending" read seconds later
is the run not having started, not a reason to stop watching. Fix what CI
finds, push, and re-poll until every check reports pass.

- [ ] **Step 5: Update the issue**

Comment on #358 with the PR link and note that #297 keeps the editing half,
with the line number added here as the first half of per-feature identity.

---

## Notes for the implementer

**Do not restart the worker to test a handler change** — `worker` does not
hot-reload, but `run-worktree-tests.sh` starts its own throwaway container, so
tests always see current source. The restart only matters if you exercise a
job through the running stack in Task 12:

```bash
docker compose restart worker
```

**`docs/TODO.md` needs no entry closed by this work** — #358 is tracked as a
GitHub issue, not a TODO entry. Check with `grep -i "annotation.*export"
docs/TODO.md` before assuming; if an entry does exist, move it to
`docs/TODO-done.md` in full per `CLAUDE.md`.
