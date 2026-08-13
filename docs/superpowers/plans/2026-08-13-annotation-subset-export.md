# Annotation Subset Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export the annotation feature table's current filter as a new project object, re-emitting original source lines so the output is byte-identical to the input.

**Architecture:** A line number is recorded on every parsed feature and stored in the SQLite index. A new `annotation_export` module computes the closure of a filter (matched features plus ancestors and descendants) as a set of line numbers, then re-scans the source file once and copies those lines verbatim, verifying each against the index as it goes. A queued handler wraps the two and registers the result as an object with `derived_from` set.

**Tech Stack:** Python 3.14, SQLite (recursive CTEs), FastAPI, Beanie/Motor, pytest, React + TypeScript.

**Spec:** [`docs/superpowers/specs/2026-08-13-annotation-subset-export-design.md`](../specs/2026-08-13-annotation-subset-export-design.md) — requirements AE-1 through AE-25.

**Issue:** [#358](https://github.com/syntheticgio/bioflow/issues/358)

---

## Background an implementer needs

**The constraint this whole feature rests on:** never rebuild a feature line from the `Feature` dataclass. `parse_gff_line` does not keep the GFF `source` column (field 2) or `phase` (field 8), and `parse_bed_line` converts BED's zero-based coordinates to one-based. A line reconstructed from `Feature` would silently lose reading frame. The export copies original bytes; it never serializes.

**Why line numbers and not byte offsets:** offsets bind the index to exact file bytes, and if the file shifts they point at wrong-but-valid lines, producing a plausible and wrong export. A line number can be re-parsed and checked against what the index recorded. That check is required, not optional — it is the entire reason this approach was chosen.

**Formats:** GFF3, GTF, and BED only. GenBank features span multiple lines and its segment children are synthetic rows matching no single line, so `Feature.line` is `None` for GenBank and export refuses it.

**Where things live:**
- `backend/app/pipelines/annotation_parse.py` — the `Feature` dataclass and pure per-line parsers
- `backend/app/pipelines/annotation_db.py` — SQLite schema, `FeatureFilters`, `_where()`, queries
- `backend/app/pipelines/annotation_hierarchy.py` — `DEPTH_CAP`, parent resolution
- `backend/app/queue/annotation_handlers.py` — the `run_annotation_stats` job
- `backend/app/queue/results.py` — job-result appliers and the `_APPLIERS` registry
- `backend/app/services/pipeline_service.py` — job launchers
- `backend/app/api/v1/pipelines.py` — routes

**Running tests.** This plan is executed from a worktree, so use the worktree runner, not `docker compose exec api`:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_export.py -v
```

Running `docker compose exec api python -m pytest` from a worktree silently tests **main's** code, not yours.

## File structure

| File | Responsibility |
|---|---|
| `backend/app/pipelines/annotation_parse.py` | *Modify.* `Feature` gains `line: int \| None`. Parsers stay pure and do not set it. |
| `backend/app/pipelines/annotation_db.py` | *Modify.* `features` table gains a `line` column; both INSERTs carry it. |
| `backend/app/pipelines/annotation_export.py` | *Create.* `closure_lines()` and `write_subset()`. The whole export algorithm, no I/O beyond the two files it is handed. |
| `backend/app/queue/annotation_handlers.py` | *Modify.* Sets `line` in `_line_rows`; adds the `export_annotation_subset` handler. |
| `backend/app/queue/results.py` | *Modify.* Adds `_apply_export_annotation_subset` and registers it in `_APPLIERS`. |
| `backend/app/services/pipeline_service.py` | *Modify.* Adds `launch_annotation_export`. |
| `backend/app/api/v1/pipelines.py` | *Modify.* Adds the export POST route and a closure-count GET route. |
| `frontend/src/components/AnnotationFeatureTable.tsx` | *Modify.* Adds the export control. |
| `backend/tests/pipelines/test_annotation_export.py` | *Create.* Closure, verification, and fidelity tests. |
| `backend/tests/fixtures/annotation/ncbi_sample.gff3` | *Create.* A real NCBI-format GFF3 for the fidelity test. |

---

## Task 1: Record a line number on every parsed feature

`Feature` gains the field; the handler's loop sets it. The parsers stay pure functions of one string, which is what makes the format edge cases testable as plain calls.

**Files:**
- Modify: `backend/app/pipelines/annotation_parse.py:16-36`
- Modify: `backend/app/queue/annotation_handlers.py:51-96`
- Test: `backend/tests/pipelines/test_annotation_parse.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_annotation_parse.py`:

```python
def test_feature_defaults_to_no_line_number():
    """The parsers are pure functions of one string and cannot know the line.

    The handler's loop sets it. AE-1/AE-2.
    """
    line = "chr1\t.\tgene\t100\t200\t.\t+\t.\tID=g1"
    feature = annotation_parse.parse_gff_line(line)
    assert feature.line is None


def test_feature_line_is_settable_via_replace():
    """The handler sets the line with dataclasses.replace on a frozen Feature."""
    import dataclasses

    line = "chr1\t.\tgene\t100\t200\t.\t+\t.\tID=g1"
    feature = annotation_parse.parse_gff_line(line)
    numbered = dataclasses.replace(feature, line=7)
    assert numbered.line == 7
    assert numbered.feature_id == "g1"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_parse.py -k "line" -v
```

Expected: FAIL with `TypeError: Feature.__init__() got an unexpected keyword argument 'line'` or `AttributeError: 'Feature' object has no attribute 'line'`.

- [ ] **Step 3: Add the field to `Feature`**

In `backend/app/pipelines/annotation_parse.py`, add to the `Feature` dataclass after `attributes`:

```python
    attributes: str | None
    # The 1-based source line this feature was parsed from, or None when the
    # feature does not correspond to a single line. Set by the handler's read
    # loop, not by the parse functions -- they take one string and cannot know
    # where it came from.
    #
    # None for GenBank: a GenBank feature spans a location line plus
    # continuation-wrapped qualifiers, and its segment children are synthetic
    # rows matching no line at all. Subset export refuses GenBank for exactly
    # this reason (AE-2, AE-25).
    line: int | None = None
```

The default is what keeps every existing construction site and test fixture compiling.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_parse.py -v
```

Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Set the line number in the handler's read loop**

In `backend/app/queue/annotation_handlers.py`, add the import at the top:

```python
import dataclasses
import gzip
from pathlib import Path
```

Then in `_line_rows`, replace the `acc.add(feature)` line and what follows it. The current code is:

```python
            feature = parse_line(stripped)
            if feature is None:
                acc.add_malformed()
                continue
            acc.add(feature)
```

Change it to:

```python
            feature = parse_line(stripped)
            if feature is None:
                acc.add_malformed()
                continue
            # enumerate() is 0-based; source lines are 1-based, and the export
            # re-scan counts the same way (AE-1).
            feature = dataclasses.replace(feature, line=i + 1)
            acc.add(feature)
```

`_genbank_rows` is left alone: its features keep `line=None` (AE-2).

- [ ] **Step 6: Run the annotation handler tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/ tests/queue/ -q
```

Expected: PASS. No count should drop from before this task.

- [ ] **Step 7: Commit**

```bash
git add backend/app/pipelines/annotation_parse.py backend/app/queue/annotation_handlers.py backend/tests/pipelines/test_annotation_parse.py
git commit -m "feat(pipelines): record the source line number on every parsed feature

Set by the handler's read loop rather than the parse functions, which take
one string and cannot know where it came from. None for GenBank, whose
features span multiple lines and whose segment children are synthetic.

Groundwork for subset export, which re-emits original source lines rather
than reconstructing them from Feature -- a reconstruction would lose the
GFF source column and phase, which Feature does not keep."
```

---

## Task 2: Store the line number in the index

**Files:**
- Modify: `backend/app/pipelines/annotation_db.py:84-133`
- Test: `backend/tests/pipelines/test_annotation_db.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_annotation_db.py`:

```python
def test_build_stores_the_line_number(tmp_path):
    """AE-3: the index records each feature's source line."""
    db_path = tmp_path / "features.db"
    rows = [
        annotation_parse.Feature(
            contig="chr1", start=100, end=200, type="gene", strand="+",
            score=None, name="g1", feature_id="g1", parents=(), biotype=None,
            attributes="ID=g1", line=4,
        ),
    ]
    annotation_db.build_annotation_db(rows=rows, db_path=db_path)

    con = sqlite3.connect(db_path)
    try:
        stored = con.execute("SELECT line FROM features").fetchall()
    finally:
        con.close()
    assert stored == [(4,)]


def test_multi_parent_rows_share_one_line_number(tmp_path):
    """AE-4: one source line stored under two parents keeps one line number."""
    db_path = tmp_path / "features.db"
    rows = [
        annotation_parse.Feature(
            contig="chr1", start=100, end=200, type="exon", strand="+",
            score=None, name="e1", feature_id="e1", parents=("t1", "t2"),
            biotype=None, attributes="ID=e1;Parent=t1,t2", line=9,
        ),
    ]
    annotation_db.build_annotation_db(rows=rows, db_path=db_path)

    con = sqlite3.connect(db_path)
    try:
        stored = con.execute("SELECT parent, line FROM features ORDER BY parent").fetchall()
    finally:
        con.close()
    assert stored == [("t1", 9), ("t2", 9)]
```

If `sqlite3` and `annotation_parse` are not already imported in that test file, add:

```python
import sqlite3

from app.pipelines import annotation_db, annotation_parse
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_db.py -k "line_number" -v
```

Expected: FAIL with `sqlite3.OperationalError: no such column: line`.

- [ ] **Step 3: Add the column and carry it in both INSERTs**

In `backend/app/pipelines/annotation_db.py`, add the column to the `CREATE TABLE` after `attributes`:

```python
              attributes TEXT,
              -- The 1-based source line, for subset export's verbatim
              -- re-emission. Nullable: GenBank features span several lines
              -- and record none.
              line       INTEGER,
              parent_status TEXT NOT NULL DEFAULT 'root',
              depth      INTEGER NOT NULL DEFAULT 0
```

No index on `line`: the export selects by filter and reads `line` out, it never looks a feature up by line.

Then update the row tuple built in the loop:

```python
            for parent in f.parents or (None,):
                batch.append(
                    (
                        f.contig, f.start, f.end, f.type, f.strand, f.score,
                        f.name, f.feature_id, parent, f.biotype, f.attributes,
                        f.line,
                    )
                )
```

And **both** `executemany` calls — the in-loop one and the trailing one — become:

```python
                con.executemany(
                    "INSERT INTO features (contig, start, end, type, strand, "
                    "score, name, feature_id, parent, biotype, attributes, "
                    "line) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", batch
                )
```

Note the twelfth `?`. Missing it on either call is the failure mode here.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_db.py -v
```

Expected: PASS, all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_db.py backend/tests/pipelines/test_annotation_db.py
git commit -m "feat(pipelines): store each feature's source line in the annotation index

No index on the column: subset export selects by filter and reads the line
out, it never looks a feature up by line. Rows sharing one source line
under multiple parents all record that line."
```

---

## Task 3: Compute the closure of a filter

The closure is every matched feature plus every ancestor and every descendant, expressed as source line numbers.

**Files:**
- Create: `backend/app/pipelines/annotation_export.py`
- Test: `backend/tests/pipelines/test_annotation_export.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_annotation_export.py`:

```python
"""Subset export: closure, verified re-emission, and round-trip fidelity."""

import pytest

from app.pipelines import annotation_db, annotation_export, annotation_hierarchy, annotation_parse


def _feature(**kw):
    """A Feature with sensible defaults, so each test states only what it means."""
    base = dict(
        contig="chr1", start=100, end=200, type="gene", strand="+", score=None,
        name=None, feature_id=None, parents=(), biotype=None, attributes=None,
        line=None,
    )
    base.update(kw)
    return annotation_parse.Feature(**base)


def _build(tmp_path, rows):
    """Build an index and resolve its hierarchy, as the real job does."""
    db_path = tmp_path / "features.db"
    annotation_db.build_annotation_db(rows=rows, db_path=db_path)
    annotation_hierarchy.resolve_hierarchy(db_path=db_path)
    return db_path


# A three-level tree: one gene, one transcript, two exons.
def _gene_tree():
    return [
        _feature(type="gene", feature_id="g1", name="g1", start=100, end=900, line=1),
        _feature(type="transcript", feature_id="t1", parents=("g1",), start=100, end=900, line=2),
        _feature(type="exon", feature_id="e1", parents=("t1",), start=100, end=200, line=3),
        _feature(type="exon", feature_id="e2", parents=("t1",), start=800, end=900, line=4),
    ]


def test_closure_reaches_descendants(tmp_path):
    """AE-6: filtering to a gene exports its transcript and exons too."""
    db_path = _build(tmp_path, _gene_tree())
    filters = annotation_db.FeatureFilters(feature_type="gene", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2, 3, 4}


def test_closure_reaches_ancestors(tmp_path):
    """AE-7: filtering to exons exports the transcript and gene above them."""
    db_path = _build(tmp_path, _gene_tree())
    filters = annotation_db.FeatureFilters(feature_type="exon", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2, 3, 4}


def test_closure_reaches_both_directions_from_mid_tree(tmp_path):
    """A transcript match pulls the gene above and the exons below."""
    db_path = _build(tmp_path, _gene_tree())
    filters = annotation_db.FeatureFilters(feature_type="transcript", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2, 3, 4}


def test_closure_excludes_unmatched_trees(tmp_path):
    """A second gene on another contig is not dragged in."""
    rows = _gene_tree() + [
        _feature(type="gene", feature_id="g2", contig="chr2", start=10, end=20, line=5),
    ]
    db_path = _build(tmp_path, rows)
    filters = annotation_db.FeatureFilters(contig="chr1", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2, 3, 4}


def test_multi_parent_feature_contributes_one_line(tmp_path):
    """AE-8: an exon shared by two transcripts is two rows but one line."""
    rows = [
        _feature(type="gene", feature_id="g1", start=100, end=900, line=1),
        _feature(type="transcript", feature_id="t1", parents=("g1",), line=2),
        _feature(type="transcript", feature_id="t2", parents=("g1",), line=3),
        _feature(type="exon", feature_id="e1", parents=("t1", "t2"), line=4),
    ]
    db_path = _build(tmp_path, rows)
    filters = annotation_db.FeatureFilters(feature_type="gene", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2, 3, 4}


def test_closure_terminates_on_a_cycle(tmp_path):
    """AE-9: a hierarchy that points at itself must not hang the walk."""
    rows = [
        _feature(type="gene", feature_id="a", parents=("b",), line=1),
        _feature(type="gene", feature_id="b", parents=("a",), line=2),
    ]
    db_path = _build(tmp_path, rows)
    filters = annotation_db.FeatureFilters(feature_type="gene", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2}


def test_closure_ignores_top_level_only(tmp_path):
    """AE-10: top_level_only is a paging device, not a statement about content."""
    db_path = _build(tmp_path, _gene_tree())

    with_flag = annotation_export.closure_lines(
        db_path=db_path,
        filters=annotation_db.FeatureFilters(feature_type="exon", top_level_only=True),
    )
    without_flag = annotation_export.closure_lines(
        db_path=db_path,
        filters=annotation_db.FeatureFilters(feature_type="exon", top_level_only=False),
    )

    assert with_flag == without_flag == {1, 2, 3, 4}


def test_unresolved_filter_returns_matches_without_ancestors(tmp_path):
    """Correct but worth pinning: an unresolved row's parent by definition
    does not resolve, so the upward walk finds nothing to add."""
    rows = [
        _feature(type="gene", feature_id="g1", line=1),
        _feature(type="exon", feature_id="e1", parents=("nonexistent",), line=2),
    ]
    db_path = _build(tmp_path, rows)
    filters = annotation_db.FeatureFilters(
        top_level_only=False,
        parent_status=annotation_hierarchy.UNRESOLVED_STATUSES,
    )

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {2}


def test_empty_match_returns_no_lines(tmp_path):
    """AE-16 is enforced by the handler; the query itself returns an empty set."""
    db_path = _build(tmp_path, _gene_tree())
    filters = annotation_db.FeatureFilters(contig="chrZ", top_level_only=False)

    assert annotation_export.closure_lines(db_path=db_path, filters=filters) == set()


def test_features_without_a_line_are_skipped(tmp_path):
    """GenBank rows carry no line and cannot be exported (AE-2)."""
    rows = [_feature(type="gene", feature_id="g1", line=None)]
    db_path = _build(tmp_path, rows)
    filters = annotation_db.FeatureFilters(feature_type="gene", top_level_only=False)

    assert annotation_export.closure_lines(db_path=db_path, filters=filters) == set()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_export.py -v
```

Expected: FAIL with `ImportError: cannot import name 'annotation_export'`.

- [ ] **Step 3: Write `closure_lines`**

Create `backend/app/pipelines/annotation_export.py`:

```python
"""Exporting a filtered subset of an annotation as a new file.

The rule that governs this module: **never rebuild a feature line from
`Feature`**. `parse_gff_line` keeps neither the GFF `source` column nor
`phase`, and `parse_bed_line` converts BED to one-based coordinates, so a
reconstructed line would silently lose reading frame. Export copies original
bytes.

That is why the unit of work here is a *line number* rather than a feature.
`closure_lines` decides which lines belong in the output; `write_subset`
copies them out of the source verbatim.
"""

import sqlite3
from pathlib import Path

from app.logging import get_logger
from app.pipelines import annotation_db
from app.pipelines.annotation_hierarchy import DEPTH_CAP

log = get_logger(__name__)


def closure_lines(
    *, db_path: Path, filters: annotation_db.FeatureFilters
) -> set[int]:
    """Source line numbers for every feature the export must contain.

    That is the filter's matches plus every ancestor and every descendant of
    a match (AE-5 through AE-7). A filter matches *rows*, but a valid
    annotation needs whole trees: exporting a gene without its exons leaves
    `Parent=` references dangling, and exporting exons without their gene
    leaves orphans.

    `top_level_only` is forced off (AE-10). It is how the table pages, not a
    statement about content, and honouring it would exclude exactly the child
    rows the closure exists to re-add.

    Both walks are bounded by DEPTH_CAP for the reason `_assign_depths`
    documents: a file whose parent references form a cycle is a real thing,
    and the walk must terminate on one (AE-9).

    Rows with no line number contribute nothing -- that is GenBank, whose
    features span several lines (AE-2).
    """
    # The export never pages, so the table's paging flag must not narrow it.
    filters = annotation_db.FeatureFilters(
        contig=filters.contig,
        start_min=filters.start_min,
        start_max=filters.start_max,
        feature_type=filters.feature_type,
        biotype=filters.biotype,
        name_query=filters.name_query,
        strand=filters.strand,
        top_level_only=False,
        parent_status=filters.parent_status,
    )
    where, args = annotation_db._where(filters)

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # The seed: rowids of every matched row. rowid rather than feature_id
        # because a row may have neither a feature_id nor a parent (BED), and
        # it still has to reach the output.
        seed = [r[0] for r in con.execute(f"SELECT rowid FROM features{where}", args)]
        if not seed:
            return set()

        lines: set[int] = set()
        placeholders = ",".join("?" for _ in seed)

        # Matched rows themselves (AE-5).
        for (line,) in con.execute(
            f"SELECT line FROM features WHERE rowid IN ({placeholders}) "
            "AND line IS NOT NULL",
            seed,
        ):
            lines.add(line)

        # Descendants (AE-6). Walks feature_id -> parent, level by level, so
        # the cap counts tree depth rather than rows visited.
        frontier = {
            r[0]
            for r in con.execute(
                f"SELECT feature_id FROM features WHERE rowid IN ({placeholders}) "
                "AND feature_id IS NOT NULL",
                seed,
            )
        }
        seen_down = set(frontier)
        for _ in range(DEPTH_CAP):
            if not frontier:
                break
            ph = ",".join("?" for _ in frontier)
            rows = con.execute(
                f"SELECT feature_id, line FROM features WHERE parent IN ({ph})",
                list(frontier),
            ).fetchall()
            nxt = set()
            for fid, line in rows:
                if line is not None:
                    lines.add(line)
                if fid is not None and fid not in seen_down:
                    seen_down.add(fid)
                    nxt.add(fid)
            frontier = nxt

        # Ancestors (AE-7). Walks parent -> feature_id, the other direction.
        frontier = {
            r[0]
            for r in con.execute(
                f"SELECT parent FROM features WHERE rowid IN ({placeholders}) "
                "AND parent IS NOT NULL",
                seed,
            )
        }
        seen_up = set(frontier)
        for _ in range(DEPTH_CAP):
            if not frontier:
                break
            ph = ",".join("?" for _ in frontier)
            rows = con.execute(
                f"SELECT parent, line FROM features WHERE feature_id IN ({ph})",
                list(frontier),
            ).fetchall()
            nxt = set()
            for parent, line in rows:
                if line is not None:
                    lines.add(line)
                if parent is not None and parent not in seen_up:
                    seen_up.add(parent)
                    nxt.add(parent)
            frontier = nxt

        return lines
    finally:
        con.close()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_export.py -v
```

Expected: PASS, all eleven tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_export.py backend/tests/pipelines/test_annotation_export.py
git commit -m "feat(pipelines): compute the closure of an annotation filter

A filter matches rows, but a valid annotation needs whole trees: a gene
without its exons leaves Parent= references dangling, and exons without
their gene leave orphans. The closure is every match plus its ancestors and
descendants, returned as source line numbers.

Both walks are bounded by DEPTH_CAP, so a file whose parent references form
a cycle terminates rather than hanging. top_level_only is forced off: it is
how the table pages, not a statement about content."
```

---

## Task 4: Write the subset, verifying every line

**Files:**
- Modify: `backend/app/pipelines/annotation_export.py`
- Test: `backend/tests/pipelines/test_annotation_export.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_annotation_export.py`:

```python
_GFF_SOURCE = """\
##gff-version 3
##sequence-region chr1 1 1000
chr1\t.\tgene\t100\t900\t.\t+\t.\tID=g1
chr1\t.\texon\t100\t200\t.\t+\t0\tID=e1;Parent=g1
chr2\t.\tgene\t10\t20\t.\t-\t.\tID=g2
"""


def _write_source(tmp_path, text=_GFF_SOURCE):
    source = tmp_path / "in.gff3"
    source.write_text(text)
    return source


def _index_for_source(tmp_path, source):
    """Build an index whose line numbers match the file on disk."""
    rows = []
    for i, raw in enumerate(source.read_text().splitlines(), start=1):
        if raw.startswith("#"):
            continue
        feature = annotation_parse.parse_gff_line(raw)
        if feature is not None:
            rows.append(dataclasses.replace(feature, line=i))
    return _build(tmp_path, rows)


def test_written_lines_are_byte_identical(tmp_path):
    """AE-11: output lines are copied, never reconstructed."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={3, 4},
        header=["##gff-version 3"], fmt="gff",
    )

    written = dest.read_text().splitlines()
    assert "chr1\t.\texon\t100\t200\t.\t+\t0\tID=e1;Parent=g1" in written


def test_phase_survives_the_round_trip(tmp_path):
    """The reason this design copies bytes: Feature does not keep phase, so a
    reconstructed CDS line would lose its reading frame."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={4},
        header=["##gff-version 3"], fmt="gff",
    )

    exon = [ln for ln in dest.read_text().splitlines() if not ln.startswith("#")][0]
    assert exon.split("\t")[7] == "0"


def test_output_preserves_source_order(tmp_path):
    """AE-12: satisfied structurally by walking the source, not the line set."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={5, 3},
        header=["##gff-version 3"], fmt="gff",
    )

    features = [ln for ln in dest.read_text().splitlines() if not ln.startswith("#")]
    assert features[0].startswith("chr1")
    assert features[1].startswith("chr2")


def test_gff_version_pragma_is_written(tmp_path):
    """AE-13: a GFF3 export starts with the version pragma."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={3},
        header=["##gff-version 3"], fmt="gff",
    )

    assert dest.read_text().splitlines()[0] == "##gff-version 3"


def test_gff_version_pragma_is_synthesized_when_absent(tmp_path):
    """AE-13: the output must be valid GFF3 even when the input was sloppy."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={3},
        header=[], fmt="gff",
    )

    assert dest.read_text().splitlines()[0] == "##gff-version 3"


def test_sequence_region_pragmas_are_dropped(tmp_path):
    """AE-17: they describe the whole source and are wrong on a subset."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={3},
        header=["##gff-version 3", "##sequence-region chr1 1 1000"], fmt="gff",
    )

    assert "sequence-region" not in dest.read_text()


def test_bed_gets_no_synthesized_header(tmp_path):
    """AE-13a: BED has no mandatory header, so none is invented."""
    source = tmp_path / "in.bed"
    source.write_text("chr1\t99\t200\tpeak1\n")
    rows = [dataclasses.replace(annotation_parse.parse_bed_line("chr1\t99\t200\tpeak1"), line=1)]
    db_path = _build(tmp_path, rows)
    dest = tmp_path / "out.bed"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={1}, header=[], fmt="bed",
    )

    assert dest.read_text() == "chr1\t99\t200\tpeak1\n"


def test_a_shifted_source_file_fails_the_export(tmp_path):
    """AE-14/AE-15: the guardrail that makes line numbers safe.

    Delete a line from the source after indexing, so every later line number
    now points one row off. The export must refuse rather than emit a
    plausible, wrong file.
    """
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)

    kept = [ln for ln in source.read_text().splitlines() if "g1" not in ln]
    source.write_text("\n".join(kept) + "\n")

    with pytest.raises(annotation_export.StaleIndexError):
        annotation_export.write_subset(
            source=source, dest=tmp_path / "out.gff3", db_path=db_path,
            lines={3, 4}, header=["##gff-version 3"], fmt="gff",
        )


def test_write_subset_returns_the_line_count(tmp_path):
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)

    written = annotation_export.write_subset(
        source=source, dest=tmp_path / "out.gff3", db_path=db_path,
        lines={3, 4}, header=["##gff-version 3"], fmt="gff",
    )

    assert written == 2
```

Add `import dataclasses` to the top of the test file.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_export.py -k "write or pragma or phase or shifted or order or bed" -v
```

Expected: FAIL with `AttributeError: module 'app.pipelines.annotation_export' has no attribute 'write_subset'`.

- [ ] **Step 3: Write `write_subset` and its error type**

Append to `backend/app/pipelines/annotation_export.py`:

```python
class StaleIndexError(Exception):
    """The source file no longer matches the index that describes it.

    Raised when a line about to be emitted does not parse to the feature the
    index recorded for that line number. The caller turns this into a
    PermanentError: re-running cannot help, because the index must be
    recomputed first.
    """


# Pragmas that describe the whole source file and would be wrong on a subset.
_DROPPED_PRAGMA_PREFIXES = ("##sequence-region",)

_PARSERS = {
    "gff": "parse_gff_line",
    "gtf": "parse_gtf_line",
    "bed": "parse_bed_line",
}


def _header_for(header: list[str], fmt: str) -> list[str]:
    """The comment lines the output should carry.

    A GFF3 file without `##gff-version` is not valid GFF3, so one is
    synthesized when the source did not have it (AE-13). GTF and BED have no
    mandatory header and get nothing invented (AE-13a).
    """
    kept = [
        line for line in header
        if not line.startswith(_DROPPED_PRAGMA_PREFIXES)
    ]
    if fmt == "gff" and not any(l.startswith("##gff-version") for l in kept):
        kept.insert(0, "##gff-version 3")
    return kept


def write_subset(
    *,
    source: Path,
    dest: Path,
    db_path: Path,
    lines: set[int],
    header: list[str],
    fmt: str,
) -> int:
    """Copy `lines` out of `source` into `dest`, verbatim and verified.

    Iterates the *source* and emits lines whose number is in the set, rather
    than iterating the set and seeking. That is what makes source order a
    structural property rather than something a later change could regress
    (AE-12), and it means one sequential pass over a file rather than a seek
    per feature.

    Every emitted line is re-parsed and checked against what the index
    recorded for that line number. A mismatch means the file changed under
    the index, and the export must fail rather than emit a plausible, wrong
    file -- see StaleIndexError (AE-14).

    Returns the number of feature lines written.
    """
    from app.pipelines import annotation_parse
    from app.queue.annotation_handlers import _open_text

    parse_line = getattr(annotation_parse, _PARSERS[fmt])

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        expected = {
            line: (contig, start, end)
            for line, contig, start, end in con.execute(
                "SELECT DISTINCT line, contig, start, end FROM features "
                "WHERE line IS NOT NULL"
            )
        }
    finally:
        con.close()

    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0

    with open(dest, "w") as out:
        for pragma in _header_for(header, fmt):
            out.write(pragma + "\n")

        with _open_text(source) as fh:
            for i, raw in enumerate(fh, start=1):
                if i not in lines:
                    continue
                stripped = raw.rstrip("\n")
                feature = parse_line(stripped)
                if feature is None:
                    raise StaleIndexError(
                        f"line {i} of {source.name} no longer parses as a "
                        f"{fmt} feature; the annotation index is out of date"
                    )
                want = expected.get(i)
                if want is not None and (
                    feature.contig, feature.start, feature.end
                ) != want:
                    raise StaleIndexError(
                        f"line {i} of {source.name} is "
                        f"{feature.contig}:{feature.start}-{feature.end}, but "
                        f"the index recorded {want[0]}:{want[1]}-{want[2]}; "
                        "the annotation index is out of date"
                    )
                out.write(stripped + "\n")
                written += 1

    return written
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_export.py -v
```

Expected: PASS, all twenty tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_export.py backend/tests/pipelines/test_annotation_export.py
git commit -m "feat(pipelines): write a verified annotation subset

Copies original source lines rather than serializing Feature, which keeps
neither the GFF source column nor phase -- a reconstructed CDS line would
silently lose its reading frame.

Iterates the source and emits lines in the set, rather than iterating the
set and seeking, so source order is structural. Every emitted line is
re-parsed and checked against the index; a mismatch raises StaleIndexError
rather than producing a plausible, wrong file. That check is the reason line
numbers were chosen over byte offsets."
```

---

## Task 5: Prove fidelity against a real NCBI file

Hand-built fixtures are the shape that let the suggestion-rules and STAR registry failures pass while being wrong. This test uses a real file.

**Files:**
- Create: `backend/tests/fixtures/annotation/ncbi_sample.gff3`
- Test: `backend/tests/pipelines/test_annotation_export.py`

- [ ] **Step 1: Create the fixture**

Create `backend/tests/fixtures/annotation/ncbi_sample.gff3` with real NCBI-format content — note the populated `source` column, the `phase` values on CDS rows, the multi-level hierarchy, and the URL-escaped attribute:

```
##gff-version 3
#!gff-spec-version 1.21
#!processor NCBI annotwriter
##sequence-region NC_000913.3 1 4641652
##species https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=511145
NC_000913.3	RefSeq	region	1	4641652	.	+	.	ID=NC_000913.3:1..4641652;Dbxref=taxon:511145;genome=chromosome
NC_000913.3	RefSeq	gene	190	255	.	+	.	ID=gene-b0001;Name=thrL;gbkey=Gene;gene=thrL;locus_tag=b0001
NC_000913.3	RefSeq	CDS	190	255	.	+	0	ID=cds-NP_414542.1;Parent=gene-b0001;Name=NP_414542.1;product=thr operon leader peptide;protein_id=NP_414542.1
NC_000913.3	RefSeq	gene	337	2799	.	+	.	ID=gene-b0002;Name=thrA;gbkey=Gene;gene=thrA;locus_tag=b0002
NC_000913.3	RefSeq	CDS	337	2799	.	+	0	ID=cds-NP_414543.1;Parent=gene-b0002;Name=NP_414543.1;product=fused aspartokinase I%2Fhomoserine dehydrogenase I;protein_id=NP_414543.1
NC_000913.3	RefSeq	gene	2801	3733	.	+	.	ID=gene-b0003;Name=thrB;gbkey=Gene;gene=thrB;locus_tag=b0003
NC_000913.3	RefSeq	CDS	2801	3733	.	+	0	ID=cds-NP_414544.1;Parent=gene-b0003;Name=NP_414544.1;product=homoserine kinase;protein_id=NP_414544.1
```

- [ ] **Step 2: Write the failing test**

Append to `backend/tests/pipelines/test_annotation_export.py`:

```python
_FIXTURE = Path(__file__).parent.parent / "fixtures" / "annotation" / "ncbi_sample.gff3"


def test_full_export_is_byte_identical_to_the_source(tmp_path):
    """AE-11, against a real NCBI file rather than hand-built lines.

    Exporting everything must reproduce every feature line exactly. This is
    the test that fails loudly if a later refactor starts rebuilding lines
    from Feature -- which would drop the source column and phase.
    """
    source = _FIXTURE
    rows = []
    for i, raw in enumerate(source.read_text().splitlines(), start=1):
        if raw.startswith("#"):
            continue
        feature = annotation_parse.parse_gff_line(raw)
        if feature is not None:
            rows.append(dataclasses.replace(feature, line=i))
    db_path = _build(tmp_path, rows)

    filters = annotation_db.FeatureFilters(top_level_only=False)
    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    dest = tmp_path / "out.gff3"
    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines=lines,
        header=["##gff-version 3"], fmt="gff",
    )

    original = [l for l in source.read_text().splitlines() if not l.startswith("#")]
    exported = [l for l in dest.read_text().splitlines() if not l.startswith("#")]
    assert exported == original


def test_ncbi_source_column_and_phase_survive(tmp_path):
    """The two fields Feature discards. Named separately from the fidelity
    test so a failure says which property broke."""
    source = _FIXTURE
    rows = []
    for i, raw in enumerate(source.read_text().splitlines(), start=1):
        if raw.startswith("#"):
            continue
        feature = annotation_parse.parse_gff_line(raw)
        if feature is not None:
            rows.append(dataclasses.replace(feature, line=i))
    db_path = _build(tmp_path, rows)

    filters = annotation_db.FeatureFilters(feature_type="CDS", top_level_only=False)
    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    dest = tmp_path / "out.gff3"
    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines=lines,
        header=["##gff-version 3"], fmt="gff",
    )

    cds = [
        l.split("\t") for l in dest.read_text().splitlines()
        if not l.startswith("#") and l.split("\t")[2] == "CDS"
    ]
    assert cds, "expected CDS rows in the export"
    for row in cds:
        assert row[1] == "RefSeq"   # source column, dropped by Feature
        assert row[7] == "0"        # phase, dropped by Feature


def test_escaped_attributes_are_not_re_encoded(tmp_path):
    """parse_gff_attributes unquotes %2F; copying bytes means the output still
    carries the escape rather than a literal slash."""
    source = _FIXTURE
    rows = []
    for i, raw in enumerate(source.read_text().splitlines(), start=1):
        if raw.startswith("#"):
            continue
        feature = annotation_parse.parse_gff_line(raw)
        if feature is not None:
            rows.append(dataclasses.replace(feature, line=i))
    db_path = _build(tmp_path, rows)

    filters = annotation_db.FeatureFilters(top_level_only=False)
    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    dest = tmp_path / "out.gff3"
    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines=lines,
        header=["##gff-version 3"], fmt="gff",
    )

    assert "%2F" in dest.read_text()
```

Add `from pathlib import Path` to the test file's imports.

- [ ] **Step 3: Run the tests to verify they fail, then pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_export.py -k "ncbi or byte_identical or escaped" -v
```

Expected: PASS immediately — Tasks 3 and 4 already implement this. If any fail, the bug is real and in `closure_lines` or `write_subset`, not in the test.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/fixtures/annotation/ncbi_sample.gff3 backend/tests/pipelines/test_annotation_export.py
git commit -m "test(pipelines): prove export fidelity against a real NCBI GFF3

Hand-built fixtures are the shape that let the suggestion-rules and STAR
registry failures pass while being wrong: they feed the code objects that
already look the way it expects. This exports a real NCBI file in full and
asserts the feature lines are byte-identical, with named tests for the two
fields Feature discards -- the source column and phase."
```

---

## Task 6: The export handler

**Files:**
- Modify: `backend/app/queue/annotation_handlers.py`
- Test: `backend/tests/queue/test_annotation_export_handler.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_annotation_export_handler.py`:

```python
"""The export_annotation_subset handler."""

import dataclasses

import pytest

from app.errors import PermanentError
from app.pipelines import annotation_db, annotation_hierarchy, annotation_parse
from app.queue.annotation_handlers import export_annotation_subset


class _Ctx:
    """The parts of JobContext this handler touches.

    `job_id` is here because _prepare_workdir derives the scratch directory
    from it.
    """

    def __init__(self, payload, job_id="test-export-job"):
        self.payload = payload
        self.job_id = job_id

    def check_cancel(self):
        return None

    def progress(self, **kw):
        return None


_SOURCE = """\
##gff-version 3
chr1\t.\tgene\t100\t900\t.\t+\t.\tID=g1
chr1\t.\texon\t100\t200\t.\t+\t0\tID=e1;Parent=g1
chr2\t.\tgene\t10\t20\t.\t-\t.\tID=g2
"""


def _setup(tmp_path):
    source = tmp_path / "in.gff3"
    source.write_text(_SOURCE)

    rows = []
    for i, raw in enumerate(source.read_text().splitlines(), start=1):
        if raw.startswith("#"):
            continue
        feature = annotation_parse.parse_gff_line(raw)
        if feature is not None:
            rows.append(dataclasses.replace(feature, line=i))

    db_path = tmp_path / "features.db"
    annotation_db.build_annotation_db(rows=rows, db_path=db_path)
    annotation_hierarchy.resolve_hierarchy(db_path=db_path)
    return source, db_path


def test_export_writes_the_closure_and_reports_counts(tmp_path):
    source, db_path = _setup(tmp_path)
    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "gff",
        "filters": {"contig": "chr1"},
        "output_name": "subset.gff3",
    })

    result = export_annotation_subset(ctx)

    assert result["feature_count"] == 2      # the gene and its exon
    assert result["output"]["name"] == "subset.gff3"
    written = open(result["output"]["tmp_path"]).read()
    assert "ID=g1" in written and "ID=e1" in written
    assert "ID=g2" not in written


def test_export_refuses_genbank(tmp_path):
    """AE-25: GenBank features span several lines and record none."""
    source, db_path = _setup(tmp_path)
    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "genbank",
        "filters": {},
        "output_name": "subset.gff3",
    })

    with pytest.raises(PermanentError, match="genbank"):
        export_annotation_subset(ctx)


def test_export_refuses_an_empty_match(tmp_path):
    """AE-16: an annotation with a header and no features is a file that
    silently disappoints, so refuse rather than write it."""
    source, db_path = _setup(tmp_path)
    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "gff",
        "filters": {"contig": "chrZ"},
        "output_name": "subset.gff3",
    })

    with pytest.raises(PermanentError, match="no features"):
        export_annotation_subset(ctx)


def test_a_stale_index_fails_permanently(tmp_path):
    """AE-15: retrying cannot help -- the index must be recomputed first."""
    source, db_path = _setup(tmp_path)
    kept = [l for l in source.read_text().splitlines() if "g1" not in l]
    source.write_text("\n".join(kept) + "\n")

    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "gff",
        "filters": {"contig": "chr1"},
        "output_name": "subset.gff3",
    })

    with pytest.raises(PermanentError, match="out of date"):
        export_annotation_subset(ctx)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_export_handler.py -v
```

Expected: FAIL with `ImportError: cannot import name 'export_annotation_subset'`.

- [ ] **Step 3: Write the handler**

In `backend/app/queue/annotation_handlers.py`, add `annotation_export` to the pipelines import block and import the workdir helper:

```python
from app.queue.pipeline_handlers import _prepare_workdir
```

If that import creates a cycle (`pipeline_handlers` importing back from this module), move `_prepare_workdir` to a shared module rather than duplicating it — a second copy of the scratch-directory convention is how two handlers end up disagreeing about where outputs live.

Then append the handler at the end of the file:

```python
@handler(
    "export_annotation_subset",
    # THREAD for the same reason run_annotation_stats is: the work is a
    # SQLite read and a file copy in this process, with no binary to spawn.
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
)
def export_annotation_subset(ctx: JobContext) -> dict:
    """Write the filtered subset of an annotation to a new file.

    The filter arrives as it was applied in the table and is passed straight
    through to `closure_lines`. This handler never re-derives it: that is
    what keeps the exported subset and the displayed table from drifting.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("export_annotation_subset requires an 'object_id'")

    fmt = ctx.payload.get("format_kind")
    if fmt == "genbank":
        raise PermanentError(
            "cannot export a subset of a genbank annotation: its features span "
            "several lines and its segment rows correspond to no single line"
        )
    if fmt not in ("gff", "gtf", "bed"):
        raise PermanentError(f"export_annotation_subset cannot read format {fmt!r}")

    source = Path(ctx.payload["annotation_path"])
    if not source.exists():
        raise PermanentError(f"annotation file is missing: {source}")

    db_path = Path(ctx.payload["db_path"])
    if not db_path.exists():
        raise PermanentError(
            "this annotation has no computed results; compute them before exporting"
        )

    raw_filters = ctx.payload.get("filters") or {}
    status = raw_filters.get("parent_status")
    filters = annotation_db.FeatureFilters(
        contig=raw_filters.get("contig"),
        start_min=raw_filters.get("start_min"),
        start_max=raw_filters.get("start_max"),
        feature_type=raw_filters.get("feature_type"),
        biotype=raw_filters.get("biotype"),
        name_query=raw_filters.get("name_query"),
        strand=raw_filters.get("strand"),
        top_level_only=False,
        parent_status=tuple(status) if status else None,
    )

    ctx.progress(phase="closure", pct=0.2, message="selecting features")
    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)
    if not lines:
        raise PermanentError(
            "no features matched the requested filters, so there is nothing to export"
        )

    ctx.progress(phase="write", pct=0.5, message="writing subset")
    header: list[str] = []
    with _open_text(source) as fh:
        for i, raw in enumerate(fh):
            if i >= _HEADER_SCAN_LINES:
                break
            stripped = raw.rstrip("\n")
            if stripped.startswith("#"):
                header.append(stripped)

    # _prepare_workdir, not a bare tmp path: it puts the output under
    # settings.tmp_dir, which shares a filesystem with objects/, so ingesting
    # the finished file is an atomic rename rather than a copy. It also wipes
    # the directory on entry, so a retry does not inherit a half-written file.
    work = _prepare_workdir(ctx, "annotation_export")
    dest = work / ctx.payload["output_name"]
    try:
        written = annotation_export.write_subset(
            source=source, dest=dest, db_path=db_path, lines=lines,
            header=header, fmt=fmt,
        )
    except annotation_export.StaleIndexError as e:
        # Not retryable: the same job would read the same mismatched file.
        # The index has to be recomputed first, which is a user action.
        raise PermanentError(
            f"{e} -- recompute this annotation's results and export again"
        ) from e

    log.info(
        "annotation_subset_exported",
        object_id=str(object_id),
        features=written,
    )
    return {
        "object_id": str(object_id),
        "feature_count": written,
        "output": {"tmp_path": str(dest), "name": ctx.payload["output_name"]},
    }
```

`_prepare_workdir` writes under `settings.tmp_dir`, so the tests need that pointed at `tmp_path` rather than the real store. Add this fixture to the test file:

```python
@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    """Keep _prepare_workdir out of the real tmp/ tree."""
    from app.config import settings

    monkeypatch.setattr(settings, "tmp_dir", tmp_path / "tmp")
    return settings
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_export_handler.py -v
```

Expected: PASS, all four tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/annotation_handlers.py backend/tests/queue/test_annotation_export_handler.py
git commit -m "feat(queue): add the annotation subset export handler

Receives the filter as it was applied in the table and passes it straight to
closure_lines -- it never re-derives the filter, which is what keeps the
exported subset and the displayed table from drifting.

A stale index becomes a PermanentError rather than a retry: the same job
would read the same mismatched file, and the fix is recomputing the index."
```

---

## Task 7: Register the result as a derived object

`_APPLIERS` is a hand-maintained dict keyed by job type, and a missing entry is **silently skipped** — the job succeeds and no object is ever created. This is the registry shape CLAUDE.md warns about; the test asserts the wiring, not just the applier.

**Files:**
- Modify: `backend/app/queue/results.py`
- Test: `backend/tests/queue/test_annotation_export_handler.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/queue/test_annotation_export_handler.py`:

```python
def test_export_applier_is_registered():
    """_APPLIERS is hand-maintained and silently skips unknown job types: a
    missing entry means the job succeeds and no object is ever created."""
    from app.queue.results import _APPLIERS

    assert "export_annotation_subset" in _APPLIERS
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_annotation_export_handler.py -k registered -v
```

Expected: FAIL with `AssertionError`.

- [ ] **Step 3: Write the applier and register it**

In `backend/app/queue/results.py`, add the applier near the other annotation appliers:

```python
async def _apply_export_annotation_subset(result: dict, *, owner: str) -> None:
    """Register an exported annotation subset as a new object.

    `derived_from` the source annotation, so the subset's lineage is
    inspectable and the explorer can relate the two (AE-19).

    Deliberately carries no annotation results facts (AE-20): the new object
    opens to a "Compute results" button like any other annotation. Whether
    ingestion should trigger analysis automatically is #298's question, and
    answering it here by side effect would pre-empt it.
    """
    object_id = result.get("object_id")
    output = result.get("output")
    if not output or not object_id:
        return

    source = await DataObject.get(PydanticObjectId(object_id))
    if source is None:
        log.warning("annotation_export_parent_missing", object_id=object_id)
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
            facts={"annotation_subset_feature_count": result.get("feature_count")},
            # The subset describes the same biology as its source.
            metadata=dict(source.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error(
            "annotation_export_ingest_failed", object_id=object_id, error=str(e)
        )
        return

    log.info(
        "annotation_export_applied",
        object_id=object_id,
        subset_id=str(subset.id),
    )
```

Then add the entry to `_APPLIERS`:

```python
    "run_annotation_stats": _apply_run_annotation_stats,
    "export_annotation_subset": _apply_export_annotation_subset,
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/ -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/results.py backend/tests/queue/test_annotation_export_handler.py
git commit -m "feat(queue): register an exported annotation subset as a derived object

derived_from the source annotation, so the subset's lineage is inspectable.
Carries no annotation results facts: the subset opens to a Compute results
button like any other annotation, leaving whether ingestion should compute
automatically to #298 rather than answering it here by side effect.

The registration test asserts the _APPLIERS entry exists, because that dict
silently skips unknown job types -- a missing entry would mean the job
succeeds and no object is ever created."
```

---

## Task 8: Launcher and API routes

**Files:**
- Modify: `backend/app/services/pipeline_service.py`
- Modify: `backend/app/api/v1/pipelines.py`
- Test: `backend/tests/api/test_annotation_export_endpoint.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_annotation_export_endpoint.py`, following the shape of `backend/tests/api/test_annotation_stats_endpoints.py` (read that file first and match its fixtures and auth setup):

```python
"""The annotation subset export routes."""

import pytest


@pytest.mark.asyncio
async def test_export_count_404s_without_an_index(client, auth_headers):
    """AE-24: export is unavailable before results are computed."""
    resp = await client.get(
        "/api/v1/pipelines/annotationstats/export-count/000000000000000000000000",
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_export_404s_without_an_index(client, auth_headers):
    resp = await client.post(
        "/api/v1/pipelines/annotationstats/export/000000000000000000000000",
        headers=auth_headers,
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/api/test_annotation_export_endpoint.py -v
```

Expected: FAIL with 404 from the router itself (route not found) — which passes the assertion for the wrong reason. Confirm by checking the response body says the path is unknown rather than the index is missing. This is why Step 4 re-runs them after the routes exist.

- [ ] **Step 3: Add the launcher**

In `backend/app/services/pipeline_service.py`, after `launch_annotation_stats`:

```python
async def launch_annotation_export(
    *,
    object_id: PydanticObjectId,
    owner: str,
    filters: dict,
    output_name: str,
):
    """Queue a subset export for a GFF/GTF/BED annotation.

    Unlike launch_annotation_stats this *does* derive an object, so the job's
    result goes through _apply_export_annotation_subset.
    """
    from app.queue import queue
    from app.services import object_service

    ann = await object_service.get_object(object_id, owner=owner)
    _check_annotation_stats_callable(ann)

    digest, path = await _resolve_readable(ann)
    # Same reasoning as launch_annotation_stats: the THREAD handler reads
    # ctx.payload["annotation_path"] directly and does no blob resolution.
    path = path or str(blob_path(digest))

    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise ValueError(
            "this annotation has no computed results; compute them before exporting"
        )

    return await queue.enqueue(
        "export_annotation_subset",
        payload={
            "object_id": str(object_id),
            "annotation_path": path,
            "db_path": str(db_path),
            "format_kind": _annotation_format_kind(ann),
            "filters": filters,
            "output_name": output_name,
        },
        owner=owner,
    )
```

If `_annotation_format_kind` does not already exist, reuse whatever `launch_annotation_stats` uses to derive `format_kind` for its own payload — read that function and match it exactly rather than inventing a second derivation.

- [ ] **Step 4: Add the routes**

In `backend/app/api/v1/pipelines.py`, after the existing annotation routes:

```python
@router.get("/annotationstats/export-count/{object_id}")
async def get_annotation_export_count(
    object_id: PydanticObjectId,
    contig: str | None = None,
    feature_type: str | None = None,
    biotype: str | None = None,
    name_query: str | None = None,
    strand: str | None = None,
    unresolved: bool = False,
    owner: str = Depends(current_owner),
):
    """How many features a subset export would contain (AE-23).

    Separate from the export itself so the UI can show matched-vs-exported
    before anything is queued -- the closure is routinely larger than the
    matched count, and an unexplained difference reads as a bug.
    """
    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="no annotation results")

    filters = annotation_db.FeatureFilters(
        contig=contig,
        feature_type=feature_type,
        biotype=biotype,
        name_query=name_query,
        strand=strand,
        top_level_only=False,
        parent_status=(
            annotation_hierarchy.UNRESOLVED_STATUSES if unresolved else None
        ),
    )
    return {
        "matched": annotation_db.count_features(db_path=db_path, filters=filters),
        "exported": len(
            annotation_export.closure_lines(db_path=db_path, filters=filters)
        ),
    }


@router.post(
    "/annotationstats/export/{object_id}",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
)
async def launch_annotation_export(
    object_id: PydanticObjectId,
    contig: str | None = None,
    feature_type: str | None = None,
    biotype: str | None = None,
    name_query: str | None = None,
    strand: str | None = None,
    unresolved: bool = False,
    output_name: str | None = None,
    owner: str = Depends(current_owner),
):
    """Queue a subset export using the filters the table is displaying."""
    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="no annotation results")

    filters = {
        "contig": contig,
        "feature_type": feature_type,
        "biotype": biotype,
        "name_query": name_query,
        "strand": strand,
    }
    if unresolved:
        filters["parent_status"] = list(annotation_hierarchy.UNRESOLVED_STATUSES)

    try:
        job = await pipeline_service.launch_annotation_export(
            object_id=object_id,
            owner=owner,
            filters=filters,
            output_name=output_name or f"{object_id}.subset.gff3",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return job
```

Add `annotation_export` to the `from app.pipelines import (...)` block at the top of the file.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/api/ -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/api/v1/pipelines.py backend/tests/api/test_annotation_export_endpoint.py
git commit -m "feat(api): add annotation subset export routes

The count route is separate from the export so the UI can show matched
against exported before anything is queued: the closure is routinely larger
than the matched count, and an unexplained difference reads as a bug."
```

---

## Task 9: The export control in the feature table

No headless component testing exists in this repo, so this task is verified manually.

**Files:**
- Modify: `frontend/src/components/AnnotationFeatureTable.tsx`

- [ ] **Step 1: Add the export control**

In `frontend/src/components/AnnotationFeatureTable.tsx`, add state and a fetch for the counts beside the existing filter state:

```tsx
const [exportCounts, setExportCounts] = useState<{ matched: number; exported: number } | null>(null);
const [exporting, setExporting] = useState(false);

// Refreshed whenever the filters change, so the button can state what it
// will produce before it is pressed. The closure is routinely larger than
// the matched count -- showing only one of the two reads as a bug.
useEffect(() => {
  if (!hasResults || formatKind === "genbank") return;
  const params = new URLSearchParams();
  if (contig) params.set("contig", contig);
  if (featureType) params.set("feature_type", featureType);
  if (biotype) params.set("biotype", biotype);
  if (nameQuery) params.set("name_query", nameQuery);
  if (strand) params.set("strand", strand);
  if (unresolvedOnly) params.set("unresolved", "true");

  let cancelled = false;
  api
    .get(`/pipelines/annotationstats/export-count/${objectId}?${params}`)
    .then((r) => {
      if (!cancelled) setExportCounts(r.data);
    })
    .catch(() => {
      if (!cancelled) setExportCounts(null);
    });
  return () => {
    cancelled = true;
  };
}, [objectId, contig, featureType, biotype, nameQuery, strand, unresolvedOnly, hasResults, formatKind]);
```

Then the control itself, beside the filter row:

```tsx
{formatKind !== "genbank" && (
  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
    <button
      disabled={!hasResults || exporting || exportCounts?.exported === 0}
      onClick={async () => {
        setExporting(true);
        try {
          const params = new URLSearchParams();
          if (contig) params.set("contig", contig);
          if (featureType) params.set("feature_type", featureType);
          if (biotype) params.set("biotype", biotype);
          if (nameQuery) params.set("name_query", nameQuery);
          if (strand) params.set("strand", strand);
          if (unresolvedOnly) params.set("unresolved", "true");
          await api.post(`/pipelines/annotationstats/export/${objectId}?${params}`);
        } finally {
          setExporting(false);
        }
      }}
    >
      {exporting ? "Exporting…" : "Export filtered"}
    </button>
    {exportCounts && (
      <span style={{ color: "var(--text-faint)", fontSize: 12 }}>
        {exportCounts.matched.toLocaleString()} matched →{" "}
        {exportCounts.exported.toLocaleString()} exported, including parents and children
      </span>
    )}
  </div>
)}
```

Match the component's existing prop names for `objectId`, `formatKind`, `hasResults`, and the filter state — read the top of the file and use what is already there rather than the names above where they differ.

Both the count fetch and the export POST build their query string from the **same filter state the table renders from** (AE-22). Do not add separate export-only filter inputs: the guarantee that you export what you were looking at holds because there is one source of filter state, and a second set of controls would break it silently.

- [ ] **Step 2: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

- [ ] **Step 3: Verify manually at localhost:5273**

Open an annotation object with computed results and confirm:

- The control appears beside the filters, and shows two counts.
- Changing any filter updates both counts without a page reload (AE-22).
- Filtering to one contig raises the exported count above the matched count.
- The exported file contains exactly the contig you filtered to, and no other (AE-22 end to end).
- Pressing it queues a job, and the resulting object appears in the project with the source listed as its parent.
- Opening the new object shows a "Compute results" button rather than computed results (AE-20).
- A GenBank annotation shows no export control (AE-25).
- An annotation with no computed results shows the control disabled (AE-24).

- [ ] **Step 4: Tear the stack down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AnnotationFeatureTable.tsx
git commit -m "feat(ui): export the annotation table's current filter

Shows matched and exported counts together: the closure adds parents and
children, so the exported number is routinely larger, and showing only one
of the two reads as a bug.

Hidden for GenBank, whose features span several lines and cannot be
re-emitted by line number."
```

---

## Task 10: Full suite, lint, and PR

- [ ] **Step 1: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count — "green" means the number, not the exit code of whatever ran last.

- [ ] **Step 2: Run ruff**

```bash
docker compose -p biopipe exec api ruff check backend/app/
```

Expected: no findings. CI runs an import-order rule (`I001`) that a local run can miss, so if CI later flags one, split the combined import it names.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

- [ ] **Step 4: Label the PR**

```bash
gh pr edit --add-label "type:feature,area:pipelines,area:frontend"
```

`.github/release.yml` categorizes notes by label, not by the commit prefix; an unlabelled PR lands under "Other changes".

- [ ] **Step 5: Watch CI until every check reports**

```bash
gh pr checks
```

Poll until each check reports pass or fail, not merely until the command returns. A "pending" read seconds after creation is the run not having started. Fix what CI finds, push, and re-poll.

```bash
gh pr view --json mergeable,mergeStateStatus
```

`UNSTABLE` means checks are still running; keep waiting. A real conflict means rebase on `origin/main` and push again.

- [ ] **Step 6: Add `Closes #358` if `--fill` did not carry it**

```bash
gh pr edit --body "$(gh pr view --json body -q .body)

Closes #358"
```

---

## Self-review notes

**Spec coverage.** AE-1/AE-2 Task 1; AE-3/AE-4 Task 2; AE-5–AE-10 Task 3; AE-11–AE-17 Tasks 4 and 5; AE-18–AE-20 Task 7; AE-21–AE-25 Tasks 8 and 9. AE-16 is enforced in the handler (Task 6) rather than the query, which returns an empty set — noted in both places.

**Deliberate deviation from the spec's Components section.** The spec describes `closure_lines` using recursive CTEs. Task 3 implements level-by-level iteration instead: it bounds the walk by `DEPTH_CAP` in the same units `_assign_depths` uses (tree depth, not rows visited), which a bare `WITH RECURSIVE` does not do without extra depth bookkeeping. Same result, same bound, and it keeps the cycle guarantee explicit. Worth flagging in review rather than hiding.

**`write_subset` signature.** The spec wrote `write_subset(*, source, dest, lines, header, fmt)`. The implementation adds `db_path`, because verification (AE-14) needs the index to compare against. The spec's Components section is a sketch; this is the honest signature.

**Corrected during self-review.** The handler first used `ctx.scratch_dir`, which does not exist on `JobContext` — the real convention is `_prepare_workdir(ctx, kind)` from `pipeline_handlers.py:792`, which matters beyond naming: it places output under `settings.tmp_dir`, which shares a filesystem with `objects/`, making ingestion an atomic rename rather than a multi-gigabyte copy. The handler test gained a fixture pointing `settings.tmp_dir` at `tmp_path` as a result.

**Verified against the tree while writing:** `_open_text` and `_HEADER_SCAN_LINES` (`annotation_handlers.py:38,35`), `_where` and `count_features` (`annotation_db.py:151,260`), `DEPTH_CAP` and `UNRESOLVED_STATUSES` (`annotation_hierarchy.py:29,35`), `_apply_run_annotation_stats` and its `_APPLIERS` entry (`results.py:1626,2634`), and `ingest_local_file`'s keyword set (`results.py:1301`). `_annotation_format_kind` is the one symbol Task 8 references without confirming it exists — the step says to read `launch_annotation_stats` and match whatever it uses rather than inventing a second derivation.
