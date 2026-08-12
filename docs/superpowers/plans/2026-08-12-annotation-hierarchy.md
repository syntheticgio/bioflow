# Annotation Hierarchy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve GFF/GTF parent references against the file's own ID set so no record is invisible, then build a gene-first view on the resolved tree.

**Architecture:** `annotation_parse.py` stops collapsing multi-parent records; `annotation_db.py` gains a post-insert resolution pass that classifies every row's `parent_status` and assigns `depth` via a depth-capped walk, plus a `genes` summary table built from that same walk. The compute handler calls both after `build_annotation_db`. The API gains a `view` parameter and a genes route; the frontend gains a three-position toggle and bounded recursion.

**Tech Stack:** Python 3.12, SQLite (stdlib `sqlite3`), FastAPI, pytest, React + TypeScript, TanStack Query.

**Spec:** [`docs/superpowers/specs/2026-08-12-annotation-hierarchy-design.md`](../specs/2026-08-12-annotation-hierarchy-design.md)

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/app/pipelines/annotation_parse.py` | Pure line → `Feature`. Now emits `parents` tuple. | Modify |
| `backend/app/pipelines/annotation_hierarchy.py` | Resolution statuses, depth walk, gene table. All new SQL. | Create |
| `backend/app/pipelines/annotation_db.py` | Schema, insert, paged reads. Gains status columns + filter. | Modify |
| `backend/app/queue/annotation_handlers.py` | Calls resolution and gene build after insert; merges facts. | Modify |
| `backend/app/api/v1/pipelines.py` | `view` param, genes route, depth cap on children. | Modify |
| `frontend/src/components/AnnotationFeatureTable.tsx` | View toggle, bounded recursion, gene/unresolved rows. | Modify |
| `frontend/src/components/AnnotationResults.tsx` | Integrity line. | Modify |

`annotation_hierarchy.py` is a new file rather than more of `annotation_db.py` because `annotation_db.py` is already 245 lines of schema-and-reads, and resolution is a distinct responsibility (one-shot, write-side, runs once per job) from serving pages. They share only the table name.

**Note on running tests:** this plan is executed in a worktree, so use `./backend/run-worktree-tests.sh`, never `docker compose exec api pytest` — the latter silently tests `main`'s code. See `CLAUDE.md`.

---

## Task 1: `Feature.parents` replaces `Feature.parent`

Multi-parent GFF3 records currently lose every parent but the first. This task changes the dataclass and both GFF/GTF parsers; storage follows in Task 3.

**Files:**
- Modify: `backend/app/pipelines/annotation_parse.py:16-34` (dataclass), `:109-141` (GFF), `:144-199` (GTF), `:202-230` (BED)
- Test: `backend/tests/pipelines/test_annotation_parse.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/pipelines/test_annotation_parse.py`:

```python
def test_gff_multi_parent_keeps_every_parent():
    line = "chr1\t.\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1,t2"
    feature = parse_gff_line(line)
    assert feature.parents == ("t1", "t2")


def test_gff_single_parent_is_a_one_tuple():
    line = "chr1\t.\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1"
    assert parse_gff_line(line).parents == ("t1",)


def test_gff_no_parent_is_an_empty_tuple():
    line = "chr1\t.\tgene\t100\t200\t.\t+\t.\tID=g1"
    assert parse_gff_line(line).parents == ()


def test_gff_multi_parent_strips_whitespace():
    line = "chr1\t.\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1, t2"
    assert parse_gff_line(line).parents == ("t1", "t2")


def test_gtf_transcript_parent_is_its_gene():
    line = 'chr1\t.\ttranscript\t100\t200\t.\t+\t.\tgene_id "g1"; transcript_id "t1";'
    assert parse_gtf_line(line).parents == ("g1",)


def test_gtf_exon_falls_back_to_gene_without_transcript_id():
    line = 'chr1\t.\texon\t100\t200\t.\t+\t.\tgene_id "g1";'
    assert parse_gtf_line(line).parents == ("g1",)


def test_bed_has_no_parents():
    assert parse_bed_line("chr1\t99\t200\tpeak1").parents == ()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_parse.py -q
```

Expected: FAIL with `AttributeError: 'Feature' object has no attribute 'parents'`.

- [ ] **Step 3: Change the dataclass field**

In `backend/app/pipelines/annotation_parse.py`, replace the `parent` field and its docstring:

```python
@dataclass(frozen=True)
class Feature:
    """One row of the features table.

    `parents` is a tuple because GFF3 allows `Parent=a,b` for an exon shared
    between two transcripts. An empty tuple means the record declares no
    parent, which is what makes it a candidate root -- whether it *is* a root
    is decided by resolution, not here.
    """

    contig: str
    start: int
    end: int
    type: str | None
    strand: str | None
    score: float | None
    name: str | None
    feature_id: str | None
    parents: tuple[str, ...]
    biotype: str | None
    attributes: str | None
```

- [ ] **Step 4: Update the GFF parser**

Replace the parent-handling block and the `Feature(...)` construction in `parse_gff_line`:

```python
    # GFF3 allows Parent=a,b for an exon shared by two transcripts. Every
    # named parent is kept; storage writes one row per relationship.
    raw_parent = attrs.get("Parent", "")
    parents = tuple(p.strip() for p in raw_parent.split(",") if p.strip())

    return Feature(
        contig=fields[0],
        start=start,
        end=end,
        type=fields[2] or None,
        strand=_strand(fields[6]),
        score=_score(fields[5]),
        name=attrs.get("Name") or attrs.get("gene") or attrs.get("ID"),
        feature_id=attrs.get("ID"),
        parents=parents,
        biotype=attrs.get("gene_biotype") or attrs.get("biotype"),
        attributes=fields[8],
    )
```

- [ ] **Step 5: Update the GTF parser**

In `parse_gtf_line`, replace the `if ftype == "gene": ...` block and the `Feature(...)` construction:

```python
    if ftype == "gene":
        feature_id, parent = gene_id, None
    elif ftype == "transcript":
        feature_id, parent = transcript_id, gene_id
    else:
        # exon/CDS/UTR rows: GTF gives them no identifier of their own, so
        # feature_id stays None. parent is the transcript when one is named,
        # else the gene directly -- a CDS row missing transcript_id still
        # attaches to something rather than becoming a parentless row.
        parent = transcript_id or gene_id
        feature_id = None

    return Feature(
        contig=fields[0],
        start=start,
        end=end,
        type=ftype,
        strand=_strand(fields[6]),
        score=_score(fields[5]),
        name=attrs.get("gene_name") or attrs.get("gene_id"),
        feature_id=feature_id,
        parents=(parent,) if parent else (),
        biotype=attrs.get("gene_biotype") or attrs.get("gene_type"),
        attributes=fields[8],
    )
```

- [ ] **Step 6: Update the BED parser**

In `parse_bed_line`, change `parent=None,` to:

```python
        parents=(),
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_parse.py -q
```

Expected: PASS. Other annotation tests still fail — Task 3 fixes those.

- [ ] **Step 8: Commit**

```bash
git add backend/app/pipelines/annotation_parse.py backend/tests/pipelines/test_annotation_parse.py
git commit -m "fix(annotation): keep every parent of a multi-parent GFF3 record, not just the first"
```

---

## Task 2: Resolution statuses and the depth walk

The core of the feature. New module; no existing code changes yet.

**Files:**
- Create: `backend/app/pipelines/annotation_hierarchy.py`
- Test: `backend/tests/pipelines/test_annotation_hierarchy.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_annotation_hierarchy.py`:

```python
"""Parent resolution: which records have a real parent, and which do not.

The point of this module is that a record whose Parent names nothing is
*visible* rather than silently dropped, so most of these tests assert on
counts that must reconcile with the number of rows put in.
"""

import sqlite3

import pytest

from app.pipelines.annotation_hierarchy import DEPTH_CAP, resolve_hierarchy


def _build(tmp_path, rows):
    """A features table with just the columns resolution touches."""
    db_path = tmp_path / "features.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE features (
          contig TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
          type TEXT, strand TEXT, score REAL, name TEXT,
          feature_id TEXT, parent TEXT, biotype TEXT, attributes TEXT,
          parent_status TEXT NOT NULL DEFAULT 'root', depth INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    con.executemany(
        "INSERT INTO features (contig, start, end, type, feature_id, parent) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    con.execute("CREATE INDEX ix_features_feature_id ON features(feature_id)")
    con.execute("CREATE INDEX ix_features_parent ON features(parent)")
    con.commit()
    con.close()
    return db_path


def _statuses(db_path):
    con = sqlite3.connect(db_path)
    try:
        return dict(
            con.execute(
                "SELECT COALESCE(feature_id, parent), parent_status FROM features"
            ).fetchall()
        )
    finally:
        con.close()


def test_a_record_with_no_parent_is_root(tmp_path):
    db = _build(tmp_path, [("chr1", 1, 100, "gene", "g1", None)])
    resolve_hierarchy(db_path=db)
    assert _statuses(db)["g1"] == "root"


def test_a_record_whose_parent_exists_is_resolved(tmp_path):
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "g1", None),
        ("chr1", 1, 50, "exon", "e1", "g1"),
    ])
    resolve_hierarchy(db_path=db)
    assert _statuses(db)["e1"] == "resolved"


def test_a_record_whose_parent_does_not_exist_is_dangling(tmp_path):
    db = _build(tmp_path, [("chr1", 1, 50, "exon", "e1", "nosuchgene")])
    resolve_hierarchy(db_path=db)
    assert _statuses(db)["e1"] == "dangling"


def test_a_parent_matching_two_rows_is_ambiguous(tmp_path):
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "dup", None),
        ("chr2", 1, 100, "gene", "dup", None),
        ("chr1", 1, 50, "exon", "e1", "dup"),
    ])
    resolve_hierarchy(db_path=db)
    assert _statuses(db)["e1"] == "ambiguous"


def test_a_record_parented_to_itself_is_self(tmp_path):
    db = _build(tmp_path, [("chr1", 1, 100, "gene", "g1", "g1")])
    resolve_hierarchy(db_path=db)
    assert _statuses(db)["g1"] == "self"


def test_a_two_node_cycle_is_cyclic(tmp_path):
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "a", "b"),
        ("chr1", 1, 100, "gene", "b", "a"),
    ])
    resolve_hierarchy(db_path=db)
    assert _statuses(db) == {"a": "cyclic", "b": "cyclic"}


def test_depth_counts_from_zero_at_the_root(tmp_path):
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "g1", None),
        ("chr1", 1, 80, "mRNA", "t1", "g1"),
        ("chr1", 1, 50, "exon", "e1", "t1"),
    ])
    resolve_hierarchy(db_path=db)
    con = sqlite3.connect(db)
    depths = dict(con.execute("SELECT feature_id, depth FROM features").fetchall())
    con.close()
    assert depths == {"g1": 0, "t1": 1, "e1": 2}


def test_unresolvable_records_are_stored_at_the_cap(tmp_path):
    db = _build(tmp_path, [("chr1", 1, 50, "exon", "e1", "nosuchgene")])
    resolve_hierarchy(db_path=db)
    con = sqlite3.connect(db)
    depth = con.execute("SELECT depth FROM features").fetchone()[0]
    con.close()
    assert depth == DEPTH_CAP


def test_counts_reconcile_with_every_row_stored(tmp_path):
    """AH-10: nothing is dropped, whatever its status."""
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "g1", None),
        ("chr1", 1, 50, "exon", "e1", "g1"),
        ("chr1", 1, 50, "exon", "e2", "nosuchgene"),
        ("chr1", 1, 100, "gene", "s1", "s1"),
    ])
    counts = resolve_hierarchy(db_path=db)["counts"]
    assert sum(counts.values()) == 4
    assert counts == {"root": 1, "resolved": 1, "dangling": 1, "self": 1}


def test_max_depth_ignores_unresolved_sentinels(tmp_path):
    """AH-14: an unresolved row sits at the cap, which is not a tree depth."""
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "g1", None),
        ("chr1", 1, 80, "mRNA", "t1", "g1"),
        ("chr1", 1, 50, "exon", "e1", "nosuchtranscript"),
    ])
    assert resolve_hierarchy(db_path=db)["max_depth"] == 1


def test_the_gene_mode_is_recorded_at_build_time(tmp_path):
    """The route reads this back rather than recomputing it per page."""
    db = _three_level(tmp_path)
    resolve_hierarchy(db_path=db)
    build_gene_table(db_path=db)
    assert gene_mode(db_path=db) == "typed"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_hierarchy.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipelines.annotation_hierarchy'`.

- [ ] **Step 3: Write the module**

Create `backend/app/pipelines/annotation_hierarchy.py`:

```python
"""Resolving a feature's declared parent against the file's own ID set.

#257 wrote `parent` as a raw string and never checked it pointed at anything.
A record whose Parent named a nonexistent feature was then invisible in every
view: excluded from the parent page because its parent is not NULL, and under
no expanded row because nothing carries that feature_id. This module is what
makes such a record visible -- it is classified, counted, and reachable.

Resolution runs as indexed UPDATEs against the built table rather than as a
second pass over the source file. The rejected alternative holds every
distinct ID in a Python set, which on a human GFF3 is hundreds of MB of RSS
at the same moment the worker carries an insert batch. SQLite holds the ID
set on disk, so peak memory here does not scale with the annotation.
"""

import sqlite3
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# The ancestor walk stops here. A fixed cap rather than true cycle detection:
# the walk must not hang on a hostile or pathological file, and a hierarchy
# deeper than a handful of levels does not occur in real annotation data --
# gene -> transcript -> exon is three. A depth-100 chain is a broken file
# whether or not it technically closes into a cycle, and both cases want the
# same treatment. The frontend uses the same constant to bound its recursion.
DEPTH_CAP = 100

# Every status a row can carry. Ordered as resolution assigns them: each pass
# claims rows the previous ones did not, so the order is load-bearing.
STATUSES = ("root", "self", "ambiguous", "dangling", "resolved", "cyclic")

UNRESOLVED_STATUSES = ("dangling", "ambiguous", "self", "cyclic")


def resolve_hierarchy(*, db_path: Path) -> dict:
    """Classify every row's parent reference and assign its depth.

    Returns `{"counts": {status: n}, "max_depth": n}`. The caller stores both
    as facts, and the table's own Unresolved view filters on the same column
    -- one query behind both, so the summary and the table cannot disagree.
    """
    con = sqlite3.connect(db_path)
    try:
        # Order matters: each statement claims only rows still at the default.
        con.execute(
            "UPDATE features SET parent_status = 'root' WHERE parent IS NULL"
        )
        con.execute(
            "UPDATE features SET parent_status = 'self' "
            "WHERE parent IS NOT NULL AND parent = feature_id"
        )
        # Ambiguous before dangling: a parent naming two rows is a different
        # problem from one naming none, and the duplicate-ID subquery would
        # otherwise be masked by the NOT EXISTS check passing.
        con.execute(
            "UPDATE features SET parent_status = 'ambiguous' "
            "WHERE parent_status NOT IN ('root', 'self') AND parent IN ("
            "  SELECT feature_id FROM features WHERE feature_id IS NOT NULL"
            "  GROUP BY feature_id HAVING COUNT(*) > 1"
            ")"
        )
        con.execute(
            "UPDATE features SET parent_status = 'dangling' "
            "WHERE parent_status NOT IN ('root', 'self', 'ambiguous') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM features p WHERE p.feature_id = features.parent"
            ")"
        )
        con.execute(
            "UPDATE features SET parent_status = 'resolved' "
            "WHERE parent_status NOT IN ('root', 'self', 'ambiguous', 'dangling')"
        )
        con.commit()

        _assign_depths(con)
        con.commit()

        counts = dict(
            con.execute(
                "SELECT parent_status, COUNT(*) FROM features GROUP BY parent_status"
            ).fetchall()
        )
        # Only over rows whose depth is a tree position: an unresolved row
        # sits at the cap as a sentinel (AH-9), and reporting that as the
        # file's depth would say every annotation is 100 levels deep.
        max_depth = con.execute(
            "SELECT MAX(depth) FROM features "
            "WHERE parent_status IN ('root', 'resolved')"
        ).fetchone()[0]
    finally:
        con.close()

    log.info("annotation_hierarchy_resolved", max_depth=max_depth, **counts)
    return {"counts": counts, "max_depth": max_depth or 0}


def _assign_depths(con: sqlite3.Connection) -> None:
    """Walk down from the roots, one level per iteration.

    Level-order rather than per-row recursion: each iteration is a single
    indexed UPDATE over the rows whose parents were assigned last round, so
    the whole walk is DEPTH_CAP statements at worst rather than one per row.

    Anything still unassigned when the cap is reached is part of a cycle or
    hangs off one, and is marked `cyclic`. Rows that failed to resolve at all
    sit at the cap too -- see AH-9: for them the number is a sentinel, not a
    position in a tree.
    """
    con.execute(
        f"UPDATE features SET depth = {DEPTH_CAP} "
        "WHERE parent_status != 'root'"
    )
    con.execute("UPDATE features SET depth = 0 WHERE parent_status = 'root'")

    for level in range(1, DEPTH_CAP):
        changed = con.execute(
            "UPDATE features SET depth = ? "
            "WHERE parent_status = 'resolved' AND depth = ? AND parent IN ("
            "  SELECT feature_id FROM features WHERE depth = ?"
            ")",
            (level, DEPTH_CAP, level - 1),
        ).rowcount
        if not changed:
            break

    # Resolved rows never reached by the walk are in or below a cycle.
    con.execute(
        f"UPDATE features SET parent_status = 'cyclic' "
        f"WHERE parent_status = 'resolved' AND depth = {DEPTH_CAP}"
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_hierarchy.py -q
```

Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_hierarchy.py backend/tests/pipelines/test_annotation_hierarchy.py
git commit -m "feat(annotation): classify every parent reference as resolved, dangling, ambiguous, self, or cyclic"
```

---

## Task 3: Schema columns, one row per relationship, status filter

Wires Task 1's `parents` and Task 2's columns into the stored table.

**Files:**
- Modify: `backend/app/pipelines/annotation_db.py:27-30` (`_COLUMNS`), `:33-54` (`FeatureFilters`), `:57-127` (`build_annotation_db`), `:130-174` (`_where`)
- Test: `backend/tests/pipelines/test_annotation_db.py`

- [ ] **Step 1: Write the failing tests**

In `backend/tests/pipelines/test_annotation_db.py`, update the `_f` helper's signature so existing tests keep working, then add the new tests:

```python
def _f(contig, start, end, type, feature_id, parent=None, name=None, biotype=None):
    return Feature(
        contig=contig,
        start=start,
        end=end,
        type=type,
        strand="+",
        score=None,
        name=name or feature_id,
        feature_id=feature_id,
        parents=(parent,) if parent else (),
        biotype=biotype,
        attributes=f"ID={feature_id}",
    )


def _multi(contig, start, end, type, feature_id, parents):
    return Feature(
        contig=contig, start=start, end=end, type=type, strand="+", score=None,
        name=feature_id, feature_id=feature_id, parents=parents,
        biotype=None, attributes=f"ID={feature_id}",
    )


def test_a_multi_parent_feature_is_stored_once_per_relationship(tmp_path):
    """AH-11: expanding either transcript must show the shared exon."""
    db_path = tmp_path / "f.db"
    build_annotation_db(
        rows=iter([
            _f("chr1", 1, 500, "mRNA", "t1"),
            _f("chr1", 1, 500, "mRNA", "t2"),
            _multi("chr1", 10, 50, "exon", "e1", ("t1", "t2")),
        ]),
        db_path=db_path,
    )
    assert len(children_of(db_path=db_path, parent_id="t1")) == 1
    assert len(children_of(db_path=db_path, parent_id="t2")) == 1


def test_build_returns_source_feature_count_not_row_count(tmp_path):
    """AH-12: the summary total must not inflate with relationship count."""
    db_path = tmp_path / "f.db"
    total = build_annotation_db(
        rows=iter([
            _f("chr1", 1, 500, "mRNA", "t1"),
            _multi("chr1", 10, 50, "exon", "e1", ("t1", "t2")),
        ]),
        db_path=db_path,
    )
    assert total == 2


def test_a_feature_with_no_parents_stores_one_row(tmp_path):
    db_path = tmp_path / "f.db"
    build_annotation_db(rows=iter([_f("chr1", 1, 500, "gene", "g1")]), db_path=db_path)
    assert count_features(db_path=db_path, filters=FeatureFilters()) == 1


def test_parent_status_filter_selects_only_those_statuses(tmp_path):
    db_path = tmp_path / "f.db"
    build_annotation_db(
        rows=iter([
            _f("chr1", 1, 500, "gene", "g1"),
            _f("chr1", 10, 50, "exon", "e1", parent="nosuchgene"),
        ]),
        db_path=db_path,
    )
    resolve_hierarchy(db_path=db_path)
    filters = FeatureFilters(top_level_only=False, parent_status=("dangling",))
    rows = query_features(db_path=db_path, filters=filters, offset=0, limit=10)
    assert [r["feature_id"] for r in rows] == ["e1"]


def test_rows_carry_their_status_and_depth(tmp_path):
    db_path = tmp_path / "f.db"
    build_annotation_db(rows=iter([_f("chr1", 1, 500, "gene", "g1")]), db_path=db_path)
    resolve_hierarchy(db_path=db_path)
    row = query_features(
        db_path=db_path, filters=FeatureFilters(), offset=0, limit=1
    )[0]
    assert row["parent_status"] == "root"
    assert row["depth"] == 0
```

Add the import at the top of the file:

```python
from app.pipelines.annotation_hierarchy import resolve_hierarchy
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_db.py -q
```

Expected: FAIL — `TypeError: Feature.__init__() got an unexpected keyword argument 'parents'` is already fixed by the helper, so the failures are `FeatureFilters` having no `parent_status` and rows lacking `parent_status`.

- [ ] **Step 3: Add the columns to `_COLUMNS`**

In `backend/app/pipelines/annotation_db.py`, replace the `_COLUMNS` constant:

```python
_COLUMNS = (
    "contig, start, end, type, strand, score, name, feature_id, "
    "parent, biotype, attributes, parent_status, depth"
)
```

- [ ] **Step 4: Add `parent_status` to the filter**

Replace the `FeatureFilters` dataclass:

```python
@dataclass(frozen=True)
class FeatureFilters:
    """What the table is currently showing.

    One object rather than loose arguments so `query_features` and
    `count_features` cannot drift apart about what is being filtered -- the
    page and its total have to agree or pagination silently misreports.

    `top_level_only` defaults True because the table opens on parents. It is
    a field rather than a hardcoded clause because the type filter has to
    clear it: every exon has a parent, so filtering to `exon` with the flag
    set returns an empty table on a perfectly good GFF3.

    `parent_status` is how the Unresolved view expresses itself -- the rows
    whose parent reference resolved to nothing. It is a tuple rather than a
    single value because that view shows four statuses at once.
    """

    contig: str | None = None
    start_min: int | None = None
    start_max: int | None = None
    feature_type: str | None = None
    biotype: str | None = None
    name_query: str | None = None
    strand: str | None = None
    top_level_only: bool = True
    parent_status: tuple[str, ...] | None = None
```

- [ ] **Step 5: Write one row per relationship**

In `build_annotation_db`, replace the schema, the insert loop, and the index block. The whole function body from `con.execute("PRAGMA journal_mode=OFF")` to `return inserted`:

```python
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute(
            """
            CREATE TABLE features (
              contig     TEXT NOT NULL,
              start      INTEGER NOT NULL,
              end        INTEGER NOT NULL,
              type       TEXT,
              strand     TEXT,
              score      REAL,
              name       TEXT,
              feature_id TEXT,
              parent     TEXT,
              biotype    TEXT,
              attributes TEXT,
              parent_status TEXT NOT NULL DEFAULT 'root',
              depth      INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # Counts source features, not stored rows. A multi-parent GFF3 exon
        # writes one row per relationship so expanding either parent finds
        # it, but it is one feature -- returning the row count here would
        # inflate the summary's feature total (AH-12).
        features = 0
        batch: list[tuple] = []
        for f in rows:
            features += 1
            # An empty `parents` writes a single row with a NULL parent,
            # which is what makes the row a candidate root.
            for parent in f.parents or (None,):
                batch.append(
                    (
                        f.contig, f.start, f.end, f.type, f.strand, f.score,
                        f.name, f.feature_id, parent, f.biotype, f.attributes,
                    )
                )
            if len(batch) >= _INSERT_BATCH:
                con.executemany(
                    "INSERT INTO features (contig, start, end, type, strand, "
                    "score, name, feature_id, parent, biotype, attributes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
                )
                batch = []

        if batch:
            con.executemany(
                "INSERT INTO features (contig, start, end, type, strand, "
                "score, name, feature_id, parent, biotype, attributes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
            )

        con.execute("CREATE INDEX ix_features_locus ON features(contig, start)")
        # The index the whole paging design rests on: expanding a gene must
        # be a seek, not a scan of three million rows.
        con.execute("CREATE INDEX ix_features_parent ON features(parent)")
        # Resolution's every UPDATE looks parents up by this column; without
        # it each pass is a full scan.
        con.execute("CREATE INDEX ix_features_feature_id ON features(feature_id)")
        con.execute("CREATE INDEX ix_features_type ON features(type)")
        con.execute("CREATE INDEX ix_features_name ON features(name)")
        con.execute("CREATE INDEX ix_features_status ON features(parent_status)")
        con.commit()
    finally:
        con.close()

    return features
```

- [ ] **Step 6: Filter on status**

In `_where`, add this clause immediately after the `top_level_only` block:

```python
    if filters.parent_status:
        placeholders = ",".join("?" for _ in filters.parent_status)
        clauses.append(f"parent_status IN ({placeholders})")
        args.extend(filters.parent_status)
```

- [ ] **Step 7: Run the whole annotation suite**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_db.py tests/pipelines/test_annotation_parse.py tests/pipelines/test_annotation_hierarchy.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/pipelines/annotation_db.py backend/tests/pipelines/test_annotation_db.py
git commit -m "feat(annotation): store one row per parent relationship and filter by resolution status"
```

---

## Task 4: The genes table

**Files:**
- Modify: `backend/app/pipelines/annotation_hierarchy.py`
- Test: `backend/tests/pipelines/test_annotation_hierarchy.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_annotation_hierarchy.py`:

```python
from app.pipelines.annotation_hierarchy import (
    build_gene_table,
    gene_mode,
    query_genes,
)


def _genes(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return {r["feature_id"]: dict(r) for r in con.execute("SELECT * FROM genes")}
    finally:
        con.close()


def _three_level(tmp_path):
    """One gene, two transcripts, three exons -- one exon shared."""
    return _build(tmp_path, [
        ("chr1", 100, 900, "gene", "g1", None),
        ("chr1", 100, 500, "mRNA", "t1", "g1"),
        ("chr1", 400, 900, "mRNA", "t2", "g1"),
        ("chr1", 100, 200, "exon", "e1", "t1"),
        ("chr1", 400, 500, "exon", "shared", "t1"),
        ("chr1", 400, 500, "exon", "shared", "t2"),
        ("chr1", 800, 900, "exon", "e3", "t2"),
    ])


def test_typed_mode_stores_one_row_per_gene_typed_feature(tmp_path):
    db = _three_level(tmp_path)
    resolve_hierarchy(db_path=db)
    result = build_gene_table(db_path=db)
    assert result["mode"] == "typed"
    assert list(_genes(db)) == ["g1"]


def test_fallback_mode_stores_one_row_per_root(tmp_path):
    """AH-19: a flat Bakta-shaped file has no gene rows to page over."""
    db = _build(tmp_path, [
        ("chr1", 1, 100, "CDS", "c1", None),
        ("chr1", 200, 300, "CDS", "c2", None),
    ])
    resolve_hierarchy(db_path=db)
    result = build_gene_table(db_path=db)
    assert result["mode"] == "fallback"
    assert sorted(_genes(db)) == ["c1", "c2"]


def test_a_gene_carries_its_direct_child_count(tmp_path):
    db = _three_level(tmp_path)
    resolve_hierarchy(db_path=db)
    build_gene_table(db_path=db)
    assert _genes(db)["g1"]["child_count"] == 2


def test_a_shared_descendant_counts_once(tmp_path):
    """AH-36: two paths to one exon is one exon."""
    db = _three_level(tmp_path)
    resolve_hierarchy(db_path=db)
    build_gene_table(db_path=db)
    # t1, t2, e1, shared, e3 -- the shared exon reached twice, counted once.
    assert _genes(db)["g1"]["descendant_count"] == 5


def test_a_gene_span_covers_its_descendants(tmp_path):
    db = _three_level(tmp_path)
    resolve_hierarchy(db_path=db)
    build_gene_table(db_path=db)
    gene = _genes(db)["g1"]
    assert (gene["span_start"], gene["span_end"]) == (100, 900)


def test_query_genes_pages_in_position_order(tmp_path):
    db = _build(tmp_path, [
        ("chr1", 500, 600, "gene", "g2", None),
        ("chr1", 100, 200, "gene", "g1", None),
    ])
    resolve_hierarchy(db_path=db)
    build_gene_table(db_path=db)
    rows = query_genes(db_path=db, offset=0, limit=10)
    assert [r["feature_id"] for r in rows] == ["g1", "g2"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_hierarchy.py -q
```

Expected: FAIL with `ImportError: cannot import name 'build_gene_table'`.

- [ ] **Step 3: Add the gene table builder**

Append to `backend/app/pipelines/annotation_hierarchy.py`:

```python
# What counts as a gene when the file says so. Kept small and explicit: a
# type not in here is not silently treated as a gene, because a view labelled
# Genes that lists something else is the failure this feature exists to
# prevent. Extend deliberately, not by pattern-matching on substrings.
GENE_TYPES = ("gene", "pseudogene", "ncRNA_gene")


def build_gene_table(*, db_path: Path) -> dict:
    """One row per gene, with the counts the Genes view shows.

    Stored rather than computed on expand: per-row subtree counts mean
    walking two levels down for every visible row on every page turn, which
    degrades exactly on the large files where the view earns its place. The
    table is O(genes), not O(features).

    Returns the mode and the row count. `mode` is `typed` when the file has
    gene-typed features and `fallback` when it has none -- the frontend says
    which, so a file whose roots are NCBI `region` records does not present
    a list of contigs under a heading that says Genes.
    """
    con = sqlite3.connect(db_path)
    try:
        con.execute("DROP TABLE IF EXISTS genes")
        con.execute(
            """
            CREATE TABLE genes (
              feature_id TEXT,
              contig     TEXT NOT NULL,
              start      INTEGER NOT NULL,
              end        INTEGER NOT NULL,
              type       TEXT,
              strand     TEXT,
              name       TEXT,
              biotype    TEXT,
              child_count      INTEGER NOT NULL DEFAULT 0,
              descendant_count INTEGER NOT NULL DEFAULT 0,
              span_start INTEGER NOT NULL,
              span_end   INTEGER NOT NULL
            )
            """
        )

        placeholders = ",".join("?" for _ in GENE_TYPES)
        typed = con.execute(
            f"SELECT COUNT(*) FROM features WHERE type IN ({placeholders})",
            GENE_TYPES,
        ).fetchone()[0]

        if typed:
            mode = "typed"
            where, args = f"type IN ({placeholders})", list(GENE_TYPES)
        else:
            mode = "fallback"
            where, args = "parent_status = 'root'", []

        con.execute(
            f"""
            INSERT INTO genes (feature_id, contig, start, end, type, strand,
                               name, biotype, span_start, span_end)
            SELECT feature_id, contig, start, end, type, strand, name, biotype,
                   start, end
            FROM features WHERE {where}
            """,
            args,
        )
        con.execute("CREATE INDEX ix_genes_locus ON genes(contig, start)")
        # The mode is stored, not recomputed. The route needs it on every
        # page request, and re-running the type count there would scan
        # `features` to answer a question already settled at build time.
        con.execute(
            "CREATE TABLE gene_meta (mode TEXT NOT NULL, gene_count INTEGER NOT NULL)"
        )
        con.commit()

        count = con.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        con.execute("INSERT INTO gene_meta VALUES (?, ?)", (mode, count))
        _fill_gene_counts(con)
        con.commit()
    finally:
        con.close()

    log.info("annotation_gene_table_built", mode=mode, genes=count)
    return {"mode": mode, "count": count}


def gene_mode(*, db_path: Path) -> str:
    """Which rule built the genes table, as recorded at build time.

    Falls back to `typed` when the meta table is missing, which is the shape
    of a database built before this feature -- the route stays serving
    rather than 500ing on a stale artifact the user has not recomputed.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT mode FROM gene_meta").fetchone()
        return row[0] if row else "typed"
    except sqlite3.OperationalError:
        return "typed"
    finally:
        con.close()


def _fill_gene_counts(con: sqlite3.Connection) -> None:
    """Descendant counts and spans, one gene at a time.

    The walk keeps a `seen` set per gene, which is what makes a descendant
    reached by two paths count once (AH-36) -- a shared exon under two
    transcripts of the same gene is one exon. Bounded by DEPTH_CAP so a cycle
    that survived resolution cannot spin here.

    Per-gene rather than one sweeping query because the de-duplication is not
    expressible as a GROUP BY over a join: the same row reached twice has to
    be recognised as the same row, not counted twice.
    """
    genes = con.execute(
        "SELECT rowid, feature_id, start, end FROM genes WHERE feature_id IS NOT NULL"
    ).fetchall()

    for rowid, feature_id, start, end in genes:
        seen: set[str] = set()
        span_start, span_end = start, end
        frontier = [feature_id]
        child_count = 0

        for level in range(DEPTH_CAP):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            children = con.execute(
                f"SELECT feature_id, start, end FROM features "
                f"WHERE parent IN ({placeholders})",
                frontier,
            ).fetchall()
            if level == 0:
                child_count = len(children)

            next_frontier: list[str] = []
            for child_id, c_start, c_end in children:
                span_start = min(span_start, c_start)
                span_end = max(span_end, c_end)
                # A child with no ID of its own (GTF exons) is a leaf: it
                # counts, but nothing can hang off it. Keyed by identity so
                # two such rows are two descendants.
                key = child_id or f"\x00leaf:{c_start}:{c_end}"
                if key in seen:
                    continue
                seen.add(key)
                if child_id:
                    next_frontier.append(child_id)
            frontier = next_frontier

        con.execute(
            "UPDATE genes SET child_count = ?, descendant_count = ?, "
            "span_start = ?, span_end = ? WHERE rowid = ?",
            (child_count, len(seen), span_start, span_end, rowid),
        )


def query_genes(*, db_path: Path, offset: int, limit: int) -> list[dict]:
    """One page of the Genes view, in position order.

    Ordered explicitly, unlike `query_features`: the genes table is built by
    a SELECT whose order is not guaranteed to be file order, and it is small
    enough (O(genes)) that the sort is cheap.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT * FROM genes ORDER BY contig, start LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def count_genes(*, db_path: Path) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
    finally:
        con.close()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_hierarchy.py -q
```

Expected: PASS, 15 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_hierarchy.py backend/tests/pipelines/test_annotation_hierarchy.py
git commit -m "feat(annotation): build a per-gene summary table with de-duplicated descendant counts"
```

---

## Task 5: Call resolution from the compute handler

**Files:**
- Modify: `backend/app/queue/annotation_handlers.py:20` (import), `:131-150` (build and facts)
- Test: `backend/tests/pipelines/test_annotation_stats_launch.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_annotation_stats_launch.py`:

```python
def test_handler_stores_hierarchy_facts(tmp_path, monkeypatch):
    """The integrity counts the Results view reads come from the job."""
    from app.queue import annotation_handlers

    source = tmp_path / "a.gff"
    source.write_text(
        "##gff-version 3\n"
        "chr1\t.\tgene\t100\t900\t.\t+\t.\tID=g1;Name=BRCA1\n"
        "chr1\t.\tmRNA\t100\t500\t.\t+\t.\tID=t1;Parent=g1\n"
        "chr1\t.\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1\n"
        "chr1\t.\texon\t300\t400\t.\t+\t.\tID=e2;Parent=nosuchtranscript\n"
    )

    ctx = _ctx(
        payload={
            "object_id": "507f1f77bcf86cd799439011",
            "format_kind": "gff",
            "annotation_path": str(source),
            "contig_lengths": [["chr1", 1000]],
        },
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    facts = annotation_handlers.run_annotation_stats(ctx)["facts"]

    assert facts["annotation_unresolved_count"] == 1
    assert facts["annotation_parent_status_counts"]["dangling"] == 1
    assert facts["annotation_gene_mode"] == "typed"
    assert facts["annotation_gene_count"] == 1
    assert facts["annotation_feature_count"] == 4
    # gene -> mRNA -> exon is two hops; the dangling exon's sentinel depth
    # must not be what gets reported here.
    assert facts["annotation_max_depth"] == 2
```

If `_ctx` does not already exist in that file, reuse whichever fixture the
existing tests in it use to build a `JobContext` — read the file's existing
tests first and follow their construction exactly rather than inventing one.

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_stats_launch.py -q
```

Expected: FAIL with `KeyError: 'annotation_unresolved_count'`.

- [ ] **Step 3: Import the module**

In `backend/app/queue/annotation_handlers.py`, change the import on line 20:

```python
from app.pipelines import (
    annotation_db,
    annotation_hierarchy,
    annotation_parse,
    annotation_stats,
)
```

- [ ] **Step 4: Resolve before the rename, then merge the facts**

Replace the block from `tmp_db = report_dir / "features.db.tmp"` through the `return`:

```python
    tmp_db = report_dir / "features.db.tmp"
    total = annotation_db.build_annotation_db(rows=_rows(), db_path=tmp_db)

    # Resolution and the gene table run against the temporary path, before
    # the rename: a database is published only once it is fully classified,
    # so the table never queries rows whose parent_status is still the
    # insert-time default.
    ctx.progress(phase="resolve", pct=0.7, message="resolving hierarchy")
    resolution = annotation_hierarchy.resolve_hierarchy(db_path=tmp_db)
    status_counts = resolution["counts"]
    gene_result = annotation_hierarchy.build_gene_table(db_path=tmp_db)

    tmp_db.replace(report_dir / "features.db")

    ctx.progress(phase="summarize", pct=0.9, message="summarizing")

    unresolved = sum(
        status_counts.get(s, 0)
        for s in annotation_hierarchy.UNRESOLVED_STATUSES
    )

    facts = {
        "annotation_stats_status": "ok",
        **acc.finish(),
        **annotation_stats.parse_header_directives(header),
        "annotation_parent_status_counts": status_counts,
        "annotation_unresolved_count": unresolved,
        "annotation_max_depth": resolution["max_depth"],
        "annotation_gene_mode": gene_result["mode"],
        "annotation_gene_count": gene_result["count"],
    }

    log.info(
        "annotation_stats_built",
        object_id=str(object_id),
        features=total,
        unresolved=unresolved,
    )
    return {"object_id": str(object_id), "facts": facts}
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_stats_launch.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/annotation_handlers.py backend/tests/pipelines/test_annotation_stats_launch.py
git commit -m "feat(annotation): resolve hierarchy and build the gene table during the compute job"
```

---

## Task 6: API — view parameter, genes route, depth cap

**Files:**
- Modify: `backend/app/api/v1/pipelines.py:940-1016`
- Test: `backend/tests/api/test_annotation_stats_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/api/test_annotation_stats_endpoints.py`, following that file's existing client/auth fixtures:

```python
async def test_unresolved_view_returns_only_broken_records(client, owner, tmp_annotation_db):
    """The bug this feature fixes: a dangling record is reachable."""
    resp = await client.get(
        f"/api/v1/pipelines/annotationstats/features/{tmp_annotation_db}"
        "?view=unresolved"
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert rows
    assert all(
        r["parent_status"] in ("dangling", "ambiguous", "self", "cyclic")
        for r in rows
    )


async def test_all_view_applies_no_status_filter(client, owner, tmp_annotation_db):
    resp = await client.get(
        f"/api/v1/pipelines/annotationstats/features/{tmp_annotation_db}?view=all"
    )
    assert resp.status_code == 200
    statuses = {r["parent_status"] for r in resp.json()["rows"]}
    assert "root" in statuses


async def test_genes_route_returns_gene_rows(client, owner, tmp_annotation_db):
    resp = await client.get(
        f"/api/v1/pipelines/annotationstats/genes/{tmp_annotation_db}"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] in ("typed", "fallback")
    assert body["rows"][0]["descendant_count"] >= 0


async def test_genes_route_404s_without_computed_results(client, owner, bare_object_id):
    resp = await client.get(
        f"/api/v1/pipelines/annotationstats/genes/{bare_object_id}"
    )
    assert resp.status_code == 404
```

The `tmp_annotation_db` fixture must build a database containing at least one
root, one resolved child, and one dangling record, then call
`resolve_hierarchy` and `build_gene_table` on it, and place it at
`settings.annotation_stats_dir / str(object_id) / "features.db"`. Model it on
whatever fixture the existing tests in this file already use to stage a
computed database, extending that fixture rather than adding a parallel one.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/api/test_annotation_stats_endpoints.py -q
```

Expected: FAIL — 404 on the genes route, and `view` ignored.

- [ ] **Step 3: Add the view parameter**

In `backend/app/api/v1/pipelines.py`, add `view` to `get_annotation_features`'s
signature after `strand`:

```python
    view: Literal["all", "unresolved"] = "all",
```

Add the import at the top of the file if `Literal` is not already imported:

```python
from typing import Literal
```

- [ ] **Step 4: Map the view onto the filters**

In the same function, replace the `filters = annotation_db.FeatureFilters(...)`
construction:

```python
    # The Unresolved view is the one place a record whose Parent named
    # nothing is reachable -- it is excluded from the default page by
    # definition, since its parent is not NULL.
    unresolved = view == "unresolved"
    filters = annotation_db.FeatureFilters(
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

Add `annotation_hierarchy` to the module's existing `from app.pipelines import ...` line.

- [ ] **Step 5: Add the genes route**

Insert immediately after `get_annotation_features` and before
`get_annotation_children`:

```python
@router.get("/annotationstats/genes/{object_id}")
async def get_annotation_genes(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    offset: int = 0,
    limit: int = 100,
    skip_count: bool = False,
) -> dict:
    """A page of the Genes view.

    Its own route rather than a third `view` value: genes page over a
    different table with a different row shape (child and descendant counts,
    a span), so folding it into the features route would mean one endpoint
    returning two row types.

    `mode` tells the client whether these are gene-typed features or the
    root fallback, which the UI states rather than leaving implied.
    """
    await object_service.get_object(object_id, owner=owner)

    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    rows = annotation_hierarchy.query_genes(
        db_path=db_path, offset=offset, limit=limit
    )
    total = (
        None if skip_count else annotation_hierarchy.count_genes(db_path=db_path)
    )
    return {
        "total": total,
        "rows": rows,
        "mode": annotation_hierarchy.gene_mode(db_path=db_path),
    }
```

`gene_mode` reads the value recorded at build time (Task 4) rather than
recomputing it — the route is called on every page turn, and the answer was
settled once when the table was built.

- [ ] **Step 6: Cap the children route**

Replace the body of `get_annotation_children`'s return statement:

```python
    return {
        "rows": annotation_db.children_of(db_path=db_path, parent_id=parent_id),
        "depth_cap": annotation_hierarchy.DEPTH_CAP,
    }
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/api/test_annotation_stats_endpoints.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/tests/api/test_annotation_stats_endpoints.py
git commit -m "feat(annotation): serve the genes view and reach unresolved records over the API"
```

---

## Task 7: Frontend — view toggle and bounded recursion

No component test setup exists in this repo (`CLAUDE.md`: zero `.test.tsx`
files, none expected). Verification is manual, in Step 5.

**Files:**
- Modify: `frontend/src/components/AnnotationFeatureTable.tsx:61-71` (state), `:91` (reset effect), `:305` (row props), `:356-425` (`FeatureRow`)

- [ ] **Step 1: Add the view state and the depth cap**

Near the top of `AnnotationFeatureTable.tsx`, below the imports:

```tsx
/** Matches DEPTH_CAP in annotation_hierarchy.py. A cyclic Parent chain used
 *  to recurse here without a guard, which hung the browser. */
const DEPTH_CAP = 100;

type View = "genes" | "all" | "unresolved";
```

Add to the component's state block, beside the existing `useState` calls:

```tsx
  const [view, setView] = useState<View>("genes");
```

- [ ] **Step 2: Reset paging and expansion when the view changes**

Find the existing effect that clears expansion state on filter change
(around line 91) and add `view` to its dependency array, alongside the
filters it already watches. Add `setPage(0)` to its body if it is not
already there. The comment above it should read:

```tsx
  // A child row expanded under the old filters must not survive into a
  // different result set -- nor into a different view, whose rows are a
  // different slice of the same table entirely.
```

- [ ] **Step 3: Route the Genes view to its own endpoint**

The Genes view pages over a different table with a different row shape, so
it is a second query rather than a parameter on the existing one. Add it
beside the existing features query:

```tsx
  const genesQuery = useQuery({
    queryKey: ["annotationstats", "genes", objectId, page],
    queryFn: () =>
      api
        .get(`/pipelines/annotationstats/genes/${objectId}`, {
          params: { offset: page * PAGE_SIZE, limit: PAGE_SIZE },
        })
        .then((r) => r.data),
    enabled: view === "genes",
  });
```

Then add `view` to the existing features query's `queryKey`, pass it in that
query's `params`, and set its `enabled` to `view !== "genes"` so the two
never fetch at once.

The rows the table renders become whichever query owns the current view:

```tsx
  const rows = view === "genes" ? (genesQuery.data?.rows ?? []) : (data?.rows ?? []);
  const total = view === "genes" ? genesQuery.data?.total : data?.total;
```

Use `PAGE_SIZE` as the name the component already uses for its page length —
read the existing query and match it rather than introducing a second
constant.

- [ ] **Step 4: State the fallback when the file has no genes**

AH-21. Directly above the table body, so it is read before the rows:

```tsx
      {view === "genes" && genesQuery.data?.mode === "fallback" && (
        <p className="text-xs text-slate-400">
          No gene records in this file; showing top-level features.
        </p>
      )}
```

Without this line the view lists whatever the file's roots are — on an NCBI
GFF3 that is one `region` per contig — under a heading that says Genes.

- [ ] **Step 5: Render the toggle**

Immediately above the existing filter row's JSX:

```tsx
      <div className="flex gap-1" role="tablist" aria-label="Feature view">
        {(["genes", "all", "unresolved"] as View[]).map((v) => (
          <button
            key={v}
            role="tab"
            aria-selected={view === v}
            onClick={() => setView(v)}
            className={
              view === v
                ? "px-3 py-1 text-sm rounded bg-slate-700 text-white"
                : "px-3 py-1 text-sm rounded text-slate-400 hover:text-slate-200"
            }
          >
            {v === "genes" ? "Genes" : v === "all" ? "All records" : "Unresolved"}
            {v === "unresolved" && unresolvedCount > 0 && (
              <span className="ml-1.5 text-xs text-amber-400">
                {unresolvedCount}
              </span>
            )}
          </button>
        ))}
      </div>
```

Accept `unresolvedCount` as a prop on this component, defaulting to 0, and
pass it from `AnnotationResults.tsx` in Task 8.

- [ ] **Step 6: Show why an unresolved row is unresolved**

AH-29. In `FeatureRow`'s cell block, after the name cell:

```tsx
      {row.parent_status && row.parent_status !== "root" &&
        row.parent_status !== "resolved" && (
          <span className="text-xs text-amber-400" title={`parent: ${row.parent ?? "none"}`}>
            {row.parent_status}
            {row.parent ? ` → ${row.parent}` : ""}
          </span>
        )}
```

A row saying only "unresolved" tells the user nothing actionable; the
dangling reference is the part that identifies which record in their file is
wrong. Add `parent_status` and `parent` to the `FeatureRowData` type.

- [ ] **Step 7: Bound the recursion**

In `FeatureRow`, add `depth` to its props type and its destructuring:

```tsx
  depth?: number;
```

Then guard the recursive render. Replace the `{expanded && ...}` block:

```tsx
      {expanded &&
        (depth ?? 0) < DEPTH_CAP &&
        (children?.rows ?? []).map((child: FeatureRowData, i: number) => (
          <FeatureRow
            key={`${child.feature_id ?? "leaf"}-${i}`}
            row={child}
            objectId={objectId}
            expandedIds={expandedIds}
            onToggle={onToggle}
            depth={(depth ?? 0) + 1}
          />
        ))}
```

- [ ] **Step 8: Verify manually**

```bash
./ops/worktree-up.sh
```

Open http://localhost:5273, find an annotation object with computed results,
and confirm: the three tabs switch, the Genes tab shows child and descendant
counts, expanding a gene shows children, switching views collapses
everything and returns to page 1, and the Unresolved tab lists records with
a status. Then:

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 9: Commit**

```bash
git add frontend/src/components/AnnotationFeatureTable.tsx
git commit -m "feat(annotation): add a genes/all/unresolved view toggle to the feature table"
```

---

## Task 8: Frontend — the integrity line

**Files:**
- Modify: `frontend/src/components/AnnotationResults.tsx`

- [ ] **Step 1: Read the unresolved count from facts**

In `AnnotationResults.tsx`, where the other facts are read:

```tsx
  const unresolvedCount = (facts.annotation_unresolved_count as number) ?? 0;
  const statusCounts =
    (facts.annotation_parent_status_counts as Record<string, number>) ?? {};
```

- [ ] **Step 2: Render the block, absent when clean**

Above the feature table:

```tsx
      {unresolvedCount > 0 && (
        <div className="rounded border border-amber-700/40 bg-amber-950/20 p-3 text-sm">
          <button
            onClick={() => setForcedView("unresolved")}
            className="text-amber-300 underline underline-offset-2 hover:text-amber-200"
          >
            {unresolvedCount.toLocaleString()} record
            {unresolvedCount === 1 ? "" : "s"} with unresolved parentage
          </button>
          <span className="ml-2 text-slate-400">
            {Object.entries(statusCounts)
              .filter(([s]) => s !== "root" && s !== "resolved")
              .map(([s, n]) => `${n.toLocaleString()} ${s}`)
              .join(", ")}
          </span>
        </div>
      )}
```

Rendered only when non-zero, following the pattern
`annotation_stats.finish()` uses for its optional fact keys — absent rather
than an empty block on a clean file (AH-17).

- [ ] **Step 3: Make the count reach the view (AH-16)**

The count is the entry point to the Unresolved view, so the parent owns
enough state to drive it. Add to `AnnotationResults.tsx`:

```tsx
  const [forcedView, setForcedView] = useState<string | null>(null);
```

Pass it down alongside the count:

```tsx
        unresolvedCount={unresolvedCount}
        forcedView={forcedView}
        onViewChange={() => setForcedView(null)}
```

In `AnnotationFeatureTable.tsx`, accept both props and let a forced view win
once:

```tsx
  useEffect(() => {
    if (forcedView === "unresolved") setView("unresolved");
  }, [forcedView]);
```

Call `onViewChange?.()` inside the toggle's `onClick` so a later manual
switch clears the force and the button stays live for a second click.

- [ ] **Step 4: Verify manually**

Bring the worktree stack up as in Task 7 Step 8 and confirm three things: the
block appears on a file with dangling records, is absent on a clean one, and
clicking the count switches the table to the Unresolved view.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AnnotationResults.tsx
git commit -m "feat(annotation): report unresolved parentage on the results summary"
```

---

## Task 9: Verify against a real annotation

`CLAUDE.md` requires checking a rule against the real database, not only its
unit tests — the fixture files in Tasks 2 and 4 are built to contain the
cases the code expects, which is exactly how #257's suggestion rules passed
green while being wrong.

- [ ] **Step 1: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count, not the exit code.

- [ ] **Step 2: Resolve a real NCBI GFF3**

With the worktree stack up, compute results for a real NCBI GFF3 (one whose
first records are `region` per contig), then:

```bash
docker compose -p bioflow-worktree exec api python -c "
from pathlib import Path
from app.pipelines import annotation_hierarchy as ah
import sqlite3, sys
db = Path(sys.argv[1])
con = sqlite3.connect(db)
print('statuses:', dict(con.execute('SELECT parent_status, COUNT(*) FROM features GROUP BY parent_status')))
print('gene mode rows:', con.execute('SELECT COUNT(*) FROM genes').fetchone())
print('max depth:', con.execute('SELECT MAX(depth) FROM features WHERE parent_status IN (\"root\",\"resolved\")').fetchone())
" /path/to/features.db
```

Confirm three things, each of which a fixture cannot show:
- Status counts sum to the stored row count.
- `region` records do not appear in `genes` (the file has real `gene` rows,
  so `mode` must be `typed`).
- Max resolved depth is small — 2 or 3 — not near the cap.

- [ ] **Step 3: Record what the real file showed**

If any real-file check contradicts a fixture-driven assumption, fix the code
and add a fixture reproducing it before moving on.

- [ ] **Step 4: Tear the stack down**

```bash
./ops/worktree-up.sh --down
```

---

## Task 10: Push and open the PR

- [ ] **Step 1: Confirm the suite is green**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

- [ ] **Step 2: Push**

```bash
git push -u origin HEAD
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create --base main --fill --title "feat(annotation): reconstruct gene and transcript feature hierarchies"
```

The description must carry the why — that a dangling-parent record was
invisible in every view before this — and `Closes #296`.

- [ ] **Step 4: Label it**

```bash
gh pr edit --add-label "type:feature" --add-label "area:pipelines" --add-label "area:frontend"
```

- [ ] **Step 5: Watch CI to a terminal state**

```bash
gh pr checks --watch
```

Fix what it finds — `ruff check` runs rules the local suite does not,
`I001` import-order in particular. Re-poll after pushing a fix. Report the
URL only once checks are green and `mergeable` is clean.
