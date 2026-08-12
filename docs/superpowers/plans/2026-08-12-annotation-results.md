# Annotation File Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give GFF/GTF/BED files a Results tab showing what the annotation contains — feature counts, coverage, and a searchable, hierarchy-aware feature table.

**Architecture:** One on-demand compute job makes a single streaming pass over the annotation file, writing bounded aggregates to object facts and every feature row to a per-object SQLite database. Two endpoints serve the table: a filtered/paged parent query and an unpaged children query. This mirrors `run_vcf_stats` end to end — that path already stores SQLite under `vcf_stats_dir` and serves it paged, so this is the second instance of a proven shape rather than a new one.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor, SQLite (stdlib `sqlite3`), pytest; React 18 + TypeScript, TanStack Query, Recharts.

**Spec:** [`docs/superpowers/specs/2026-08-12-annotation-results-design.md`](../specs/2026-08-12-annotation-results-design.md) — read it before starting. Issue [#257](https://github.com/syntheticgio/bioflow/issues/257).

---

## Orientation for someone new to this codebase

Five things that are non-obvious and will cost you an hour each if you discover them the hard way:

1. **Run tests from a worktree with `./backend/run-worktree-tests.sh tests/ -q`**, never `docker compose exec api python -m pytest`. The `api` container bind-mounts the *main* checkout, so the latter silently tests main's code and reports on the wrong tree with no error.

2. **`registry.load_handlers()` imports only `app.queue.handlers`.** A new handler module must be added to the import block at the bottom of `backend/app/queue/handlers.py:958` or its `@handler` decorator never runs and the job name does not exist at runtime. Nothing fails at import time — the job just fails to dispatch.

3. **Handlers run in a worker process with no DB access.** Anything the handler needs from Mongo (contig lengths, file paths) must be put on the payload by the launcher in `pipeline_service.py`. See `launch_vcf_stats` at `backend/app/services/pipeline_service.py:2266` for the worked example.

4. **`worker` does not hot-reload.** After changing any handler, run `docker compose restart worker` or the job silently executes the old in-memory code.

5. **The reference implementation is `backend/app/pipelines/variant_db.py`.** Read it first. This plan's `annotation_db.py` is deliberately its sibling: same function shapes, same PRAGMA choices, same read-only connection helper, same "indexes after bulk insert" ordering. Where this plan seems to make an arbitrary choice, that file is usually the reason.

## File structure

**Create:**

| File | Responsibility |
|---|---|
| `backend/app/pipelines/annotation_parse.py` | Pure parsing: a line of GFF3/GTF/BED → a normalized `Feature`. No I/O, no SQL. |
| `backend/app/pipelines/annotation_stats.py` | Aggregation: accumulators consuming `Feature`s and emitting the facts blob. No I/O, no SQL. |
| `backend/app/pipelines/annotation_db.py` | SQLite: build, filter, page, count, children. The only place SQL lives. |
| `backend/app/queue/annotation_handlers.py` | The `run_annotation_stats` job: I/O, streaming, orchestration. |
| `backend/tests/pipelines/test_annotation_parse.py` | Parser tests, per format. |
| `backend/tests/pipelines/test_annotation_stats.py` | Aggregation tests, incl. merged-interval coverage. |
| `backend/tests/pipelines/test_annotation_db.py` | Query-layer tests, incl. the `top_level_only` interaction. |
| `backend/tests/pipelines/test_annotation_stats_launch.py` | Launcher guard tests. |
| `backend/tests/api/test_annotation_stats_endpoints.py` | Route tests, incl. ownership. |
| `frontend/src/components/AnnotationResults.tsx` | The Results tab: empty state, summary, charts. |
| `frontend/src/components/AnnotationFeatureTable.tsx` | The table: filters, paging, expansion. |
| `frontend/src/components/AnnotationCharts.tsx` | The three/five charts. |

**Modify:**

| File | Change |
|---|---|
| `backend/app/config.py` | Add `annotation_stats_dir` property. |
| `backend/app/queue/handlers.py:895` | Add the dir to the reaper tuple. |
| `backend/app/queue/handlers.py:958` | Import `annotation_handlers`. |
| `backend/app/queue/results.py` | Add `_apply_run_annotation_stats` + dispatch entry. |
| `backend/app/services/pipeline_service.py` | Add `launch_annotation_stats`. |
| `backend/app/api/v1/pipelines.py` | Add three routes (launch, features, children). |
| `frontend/src/api/types.ts` | Add fact and page types. |
| `frontend/src/api/client.ts` | Add three client methods. |
| `frontend/src/components/DetailPanel.tsx:352` | Extend `hasResults`; render `AnnotationResults`. |

The parse/aggregate/SQL split is deliberate: parsing has the most edge cases and the most tests, and keeping it free of I/O means those tests are plain function calls with no fixtures.

---

## Task 1: Normalized feature parsing

**Files:**
- Create: `backend/app/pipelines/annotation_parse.py`
- Test: `backend/tests/pipelines/test_annotation_parse.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_annotation_parse.py`:

```python
"""Parsing GFF3, GTF, and BED lines into one normalized Feature.

Kept free of I/O so every case is a plain function call: this is where the
format edge cases live, and they are the part most likely to be wrong.
"""

import pytest

from app.pipelines.annotation_parse import (
    Feature,
    parse_bed_line,
    parse_gff_attributes,
    parse_gff_line,
    parse_gtf_attributes,
    parse_gtf_line,
)


class TestGffAttributes:
    def test_parses_key_value_pairs(self):
        attrs = parse_gff_attributes("ID=gene1;Name=BRCA1;gene_biotype=protein_coding")
        assert attrs == {
            "ID": "gene1",
            "Name": "BRCA1",
            "gene_biotype": "protein_coding",
        }

    def test_url_decodes_values(self):
        # GFF3 percent-encodes reserved characters; a product name with a
        # comma or equals sign is common in NCBI annotations.
        attrs = parse_gff_attributes("product=alpha%2Cbeta%3Dgamma")
        assert attrs["product"] == "alpha,beta=gamma"

    def test_empty_attributes_column(self):
        assert parse_gff_attributes(".") == {}
        assert parse_gff_attributes("") == {}

    def test_ignores_malformed_pairs(self):
        # A bare token with no '=' is skipped rather than raising.
        attrs = parse_gff_attributes("ID=gene1;junk;Name=X")
        assert attrs == {"ID": "gene1", "Name": "X"}


class TestGtfAttributes:
    def test_parses_quoted_pairs(self):
        attrs = parse_gtf_attributes('gene_id "ENSG01"; transcript_id "ENST01";')
        assert attrs == {"gene_id": "ENSG01", "transcript_id": "ENST01"}

    def test_tolerates_missing_trailing_semicolon(self):
        attrs = parse_gtf_attributes('gene_id "ENSG01"')
        assert attrs == {"gene_id": "ENSG01"}

    def test_unquoted_values(self):
        attrs = parse_gtf_attributes("exon_number 3;")
        assert attrs == {"exon_number": "3"}


class TestGffLine:
    def test_parses_a_gene_row(self):
        line = (
            "chr1\tHAVANA\tgene\t1000\t2000\t.\t+\t.\t"
            "ID=gene1;Name=BRCA1;gene_biotype=protein_coding"
        )
        f = parse_gff_line(line)
        assert f == Feature(
            contig="chr1",
            start=1000,
            end=2000,
            type="gene",
            strand="+",
            score=None,
            name="BRCA1",
            feature_id="gene1",
            parent=None,
            biotype="protein_coding",
            attributes="ID=gene1;Name=BRCA1;gene_biotype=protein_coding",
        )

    def test_child_records_its_parent(self):
        line = "chr1\tHAVANA\texon\t1000\t1100\t.\t+\t.\tID=exon1;Parent=gene1"
        f = parse_gff_line(line)
        assert f.parent == "gene1"
        assert f.type == "exon"

    def test_multiple_parents_keeps_the_first(self):
        # GFF3 permits Parent=a,b for a feature shared by two transcripts.
        # The table's tree is single-parent, so the first wins; the raw
        # attribute column still carries both.
        line = "chr1\t.\texon\t10\t20\t.\t+\t.\tID=e1;Parent=t1,t2"
        assert parse_gff_line(line).parent == "t1"

    def test_score_parsed_when_numeric(self):
        line = "chr1\t.\tgene\t1\t9\t42.5\t+\t.\tID=g1"
        assert parse_gff_line(line).score == 42.5

    def test_missing_score_is_none(self):
        # None rather than 0.0: an absent score is not a score of zero.
        line = "chr1\t.\tgene\t1\t9\t.\t+\t.\tID=g1"
        assert parse_gff_line(line).score is None

    def test_falls_back_to_id_for_name(self):
        line = "chr1\t.\tgene\t1\t9\t.\t+\t.\tID=gene1"
        assert parse_gff_line(line).name == "gene1"

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "#comment",
            "##gff-version 3",
            "chr1\t.\tgene\t1",  # too few columns
            "chr1\t.\tgene\tNOTANUMBER\t9\t.\t+\t.\tID=g1",
        ],
    )
    def test_unparseable_returns_none(self, line):
        assert parse_gff_line(line) is None


class TestGtfLine:
    def test_transcript_parent_is_its_gene(self):
        line = (
            'chr1\tENSEMBL\ttranscript\t1000\t2000\t.\t+\t.\t'
            'gene_id "ENSG01"; transcript_id "ENST01";'
        )
        f = parse_gtf_line(line)
        assert f.feature_id == "ENST01"
        assert f.parent == "ENSG01"

    def test_exon_parent_is_its_transcript(self):
        line = (
            'chr1\tENSEMBL\texon\t1000\t1100\t.\t+\t.\t'
            'gene_id "ENSG01"; transcript_id "ENST01"; exon_number "1";'
        )
        f = parse_gtf_line(line)
        assert f.parent == "ENST01"

    def test_gene_row_is_top_level(self):
        line = 'chr1\tENSEMBL\tgene\t1000\t2000\t.\t+\t.\tgene_id "ENSG01";'
        f = parse_gtf_line(line)
        assert f.feature_id == "ENSG01"
        assert f.parent is None

    def test_cds_without_transcript_id_falls_back_to_gene(self):
        # Some GTFs omit transcript_id on CDS rows. Attaching to the gene
        # keeps the row in the tree rather than orphaning it at top level,
        # where it would inflate the parent count.
        line = 'chr1\tX\tCDS\t1000\t1100\t.\t+\t0\tgene_id "ENSG01";'
        assert parse_gtf_line(line).parent == "ENSG01"

    def test_biotype_from_gene_biotype(self):
        line = (
            'chr1\tX\tgene\t1\t9\t.\t+\t.\t'
            'gene_id "G1"; gene_biotype "lncRNA";'
        )
        assert parse_gtf_line(line).biotype == "lncRNA"


class TestBedLine:
    def test_three_column_bed(self):
        f = parse_bed_line("chr1\t0\t100")
        assert f.contig == "chr1"
        assert f.type is None
        assert f.parent is None
        assert f.name is None

    def test_converts_to_one_based_inclusive(self):
        # BED is half-open and zero-based: [0,100) is bases 1..100 in GFF's
        # 1-based inclusive terms. Getting this wrong is an off-by-one that
        # stays invisible until someone compares against a genome browser.
        f = parse_bed_line("chr1\t0\t100")
        assert (f.start, f.end) == (1, 100)

    def test_named_bed_uses_column_four(self):
        f = parse_bed_line("chr1\t0\t100\tpeak1\t960\t+")
        assert f.name == "peak1"
        assert f.score == 960.0
        assert f.strand == "+"

    def test_bed_score_dot_is_none(self):
        f = parse_bed_line("chr1\t0\t100\tpeak1\t.\t+")
        assert f.score is None

    @pytest.mark.parametrize(
        "line",
        ["", "#comment", "track name=x", "browser position chr1", "chr1\t0"],
    )
    def test_unparseable_returns_none(self, line):
        assert parse_bed_line(line) is None


class TestCoordinateAgreement:
    def test_bed_and_gff_describe_the_same_interval_identically(self):
        """The invariant the whole normalization exists for.

        The same 100-base interval written in either format must produce the
        same start/end, or the locus jump means different things depending on
        which file you opened.
        """
        bed = parse_bed_line("chr1\t999\t2000")
        gff = parse_gff_line("chr1\t.\tgene\t1000\t2000\t.\t+\t.\tID=g1")
        assert (bed.start, bed.end) == (gff.start, gff.end) == (1000, 2000)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_parse.py -q
```

Expected: collection error, `ModuleNotFoundError: No module named 'app.pipelines.annotation_parse'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/annotation_parse.py`:

```python
"""One normalized feature row from a GFF3, GTF, or BED line.

Pure functions with no I/O so the format edge cases -- which is most of what
this file is -- are testable as plain calls.

The normalization that matters is coordinates. BED is zero-based half-open;
GFF and GTF are one-based inclusive. Everything downstream (the locus jump,
the coverage accumulator, the table) assumes one-based inclusive, so BED is
converted here and nowhere else.
"""

from dataclasses import dataclass
from urllib.parse import unquote


@dataclass(frozen=True)
class Feature:
    """One row of the features table.

    `parent` being None is what makes a feature top-level, which is the
    property the table pages over -- see the spec's paging decision.
    """

    contig: str
    start: int
    end: int
    type: str | None
    strand: str | None
    score: float | None
    name: str | None
    feature_id: str | None
    parent: str | None
    biotype: str | None
    attributes: str | None


# Lines that are structural rather than data, in any of the three formats.
_SKIP_PREFIXES = ("#", "track", "browser")


def _score(value: str) -> float | None:
    """A score column, which is '.' when absent in every format here.

    None rather than 0.0, the same reasoning `variant_db._num` documents: an
    absent score is not a score of zero, and storing it as one would sort the
    row to the bottom of a score-ordered view rather than out of it.
    """
    if value in (".", ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _strand(value: str) -> str | None:
    return value if value in ("+", "-") else None


def parse_gff_attributes(column: str) -> dict[str, str]:
    """GFF3's 9th column: `key=value;key=value`, percent-encoded.

    Malformed pairs are skipped rather than raising -- a single bad attribute
    must not cost the whole feature, which is the posture `bakta_runner.
    parse_gff3` documents for the same data.
    """
    if not column or column == ".":
        return {}
    out: dict[str, str] = {}
    for pair in column.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        out[key.strip()] = unquote(value.strip())
    return out


def parse_gtf_attributes(column: str) -> dict[str, str]:
    """GTF's 9th column: `key "value"; key "value";`.

    Unquoted values are accepted too; some writers emit bare numbers for
    `exon_number`.
    """
    if not column or column == ".":
        return {}
    out: dict[str, str] = {}
    for pair in column.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        key, _, value = pair.partition(" ")
        if not value:
            continue
        out[key.strip()] = value.strip().strip('"')
    return out


def _tabular_fields(line: str, minimum: int) -> list[str] | None:
    """Split a data line, or None if it is structural or too short."""
    if not line or line.startswith(_SKIP_PREFIXES):
        return None
    fields = line.rstrip("\n").split("\t")
    if len(fields) < minimum:
        return None
    return fields


def parse_gff_line(line: str) -> Feature | None:
    """One GFF3 data line. None for comments, blanks, and malformed rows."""
    fields = _tabular_fields(line, 9)
    if fields is None:
        return None
    try:
        start = int(fields[3])
        end = int(fields[4])
    except ValueError:
        return None

    attrs = parse_gff_attributes(fields[8])

    # GFF3 allows Parent=a,b for an exon shared by two transcripts. The
    # table's tree is single-parent, so the first wins; the raw attribute
    # column preserves both for the expanded detail row.
    parent = attrs.get("Parent")
    if parent:
        parent = parent.split(",")[0]

    return Feature(
        contig=fields[0],
        start=start,
        end=end,
        type=fields[2] or None,
        strand=_strand(fields[6]),
        score=_score(fields[5]),
        name=attrs.get("Name") or attrs.get("gene") or attrs.get("ID"),
        feature_id=attrs.get("ID"),
        parent=parent or None,
        biotype=attrs.get("gene_biotype") or attrs.get("biotype"),
        attributes=fields[8],
    )


def parse_gtf_line(line: str) -> Feature | None:
    """One GTF data line.

    GTF has no ID/Parent. Hierarchy is inferred from the identifier columns:
    a transcript belongs to its gene_id, and anything below a transcript
    (exon, CDS, UTR) belongs to its transcript_id. A row carrying neither is
    top-level.

    A sub-transcript row missing transcript_id falls back to gene_id rather
    than becoming top-level -- orphaning it would inflate the parent count
    the table pages over.
    """
    fields = _tabular_fields(line, 9)
    if fields is None:
        return None
    try:
        start = int(fields[3])
        end = int(fields[4])
    except ValueError:
        return None

    attrs = parse_gtf_attributes(fields[8])
    ftype = fields[2] or None
    gene_id = attrs.get("gene_id")
    transcript_id = attrs.get("transcript_id")

    if ftype == "gene":
        feature_id, parent = gene_id, None
    elif ftype == "transcript":
        feature_id, parent = transcript_id, gene_id
    else:
        feature_id = transcript_id or gene_id
        parent = transcript_id or gene_id
        # A row whose only identifier is the one it would parent itself by.
        if feature_id == parent:
            parent = gene_id if transcript_id else None

    return Feature(
        contig=fields[0],
        start=start,
        end=end,
        type=ftype,
        strand=_strand(fields[6]),
        score=_score(fields[5]),
        name=attrs.get("gene_name") or attrs.get("gene_id"),
        feature_id=feature_id,
        parent=parent,
        biotype=attrs.get("gene_biotype") or attrs.get("gene_type"),
        attributes=fields[8],
    )


def parse_bed_line(line: str) -> Feature | None:
    """One BED data line, converted to one-based inclusive coordinates.

    BED's [start, end) with a zero-based start is the same interval as GFF's
    [start+1, end] one-based inclusive. See the module docstring: this is the
    only place the conversion happens.
    """
    fields = _tabular_fields(line, 3)
    if fields is None:
        return None
    try:
        start = int(fields[1])
        end = int(fields[2])
    except ValueError:
        return None

    return Feature(
        contig=fields[0],
        start=start + 1,
        end=end,
        type=None,
        strand=_strand(fields[5]) if len(fields) > 5 else None,
        score=_score(fields[4]) if len(fields) > 4 else None,
        name=fields[3] if len(fields) > 3 and fields[3] != "." else None,
        feature_id=None,
        parent=None,
        biotype=None,
        attributes=None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_parse.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_parse.py backend/tests/pipelines/test_annotation_parse.py
git commit -m "feat(pipelines): normalize GFF3, GTF, and BED lines to one feature shape"
```

---

## Task 2: Aggregation accumulators

**Files:**
- Create: `backend/app/pipelines/annotation_stats.py`
- Test: `backend/tests/pipelines/test_annotation_stats.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_annotation_stats.py`:

```python
"""Bounded aggregates over a stream of features.

The coverage accumulator is the one with a real invariant: overlapping
features must not double-count bases, or a dense annotation reports coverage
above 100%.
"""

from app.pipelines.annotation_parse import Feature
from app.pipelines.annotation_stats import AnnotationAccumulator, parse_header_directives


def _feature(contig="chr1", start=1, end=100, type="gene", parent=None, biotype=None):
    return Feature(
        contig=contig,
        start=start,
        end=end,
        type=type,
        strand="+",
        score=None,
        name="x",
        feature_id="x1",
        parent=parent,
        biotype=biotype,
        attributes="ID=x1",
    )


class TestCounts:
    def test_counts_features_and_types(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(type="gene"))
        acc.add(_feature(type="exon", parent="x1"))
        acc.add(_feature(type="exon", parent="x1"))
        facts = acc.finish()
        assert facts["annotation_feature_count"] == 3
        assert facts["annotation_type_counts"] == {"gene": 1, "exon": 2}

    def test_counts_top_level_separately(self):
        """The table pages over parents, so its total must be countable."""
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(type="gene"))
        acc.add(_feature(type="exon", parent="x1"))
        facts = acc.finish()
        assert facts["annotation_top_level_count"] == 1

    def test_counts_biotypes_when_present(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(biotype="protein_coding"))
        acc.add(_feature(biotype="protein_coding"))
        acc.add(_feature(biotype="lncRNA"))
        facts = acc.finish()
        assert facts["annotation_biotype_counts"] == {
            "protein_coding": 2,
            "lncRNA": 1,
        }

    def test_biotype_counts_absent_when_no_biotypes(self):
        """Absent rather than an empty dict: the UI renders the block only
        when there is something in it, and {} would render an empty chart."""
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature())
        assert "annotation_biotype_counts" not in acc.finish()

    def test_per_contig_counts(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000, "chr2": 500})
        acc.add(_feature(contig="chr1"))
        acc.add(_feature(contig="chr1"))
        acc.add(_feature(contig="chr2"))
        per = acc.finish()["annotation_per_contig"]
        by_name = {c["name"]: c for c in per}
        assert by_name["chr1"]["count"] == 2
        assert by_name["chr2"]["count"] == 1

    def test_records_malformed_line_count(self):
        acc = AnnotationAccumulator(contig_lengths={})
        acc.add_malformed()
        acc.add_malformed()
        assert acc.finish()["annotation_malformed_lines"] == 2

    def test_malformed_absent_when_zero(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 10})
        acc.add(_feature())
        assert "annotation_malformed_lines" not in acc.finish()


class TestCoverage:
    def test_simple_coverage_fraction(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(start=1, end=100))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["chr1"]["covered_bases"] == 100
        assert per["chr1"]["covered_fraction"] == 0.1

    def test_overlapping_features_do_not_double_count(self):
        """The invariant this accumulator exists for. Two exons overlapping
        by 50 bases cover 150 bases, not 200."""
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(start=1, end=100))
        acc.add(_feature(start=51, end=150))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["chr1"]["covered_bases"] == 150

    def test_fully_contained_feature_adds_nothing(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(start=1, end=100))
        acc.add(_feature(start=20, end=30))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["chr1"]["covered_bases"] == 100

    def test_coverage_never_exceeds_contig_length(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 100})
        for start in range(1, 100, 5):
            acc.add(_feature(start=start, end=start + 40))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["chr1"]["covered_bases"] <= 100
        assert per["chr1"]["covered_fraction"] <= 1.0

    def test_unordered_input_still_merges(self):
        """Features arrive in file order, which is not guaranteed sorted."""
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(start=51, end=150))
        acc.add(_feature(start=1, end=100))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["chr1"]["covered_bases"] == 150

    def test_contig_with_no_length_reports_null_fraction(self):
        """A contig absent from the reference's lengths gets a count but no
        fraction -- null, not 0.0: unknown and zero are different claims."""
        acc = AnnotationAccumulator(contig_lengths={})
        acc.add(_feature(contig="scaffold_99"))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["scaffold_99"]["count"] == 1
        assert per["scaffold_99"]["covered_fraction"] is None


class TestLengthHistogram:
    def test_bins_feature_lengths(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 10_000})
        acc.add(_feature(start=1, end=10))
        acc.add(_feature(start=1, end=10))
        acc.add(_feature(start=1, end=5000))
        hist = acc.finish()["annotation_length_histogram"]
        assert sum(b["count"] for b in hist) == 3
        assert all("min" in b and "max" in b for b in hist)


class TestAttributeKeys:
    def test_counts_attribute_keys(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add_attribute_keys(["ID", "Name", "Parent"])
        acc.add_attribute_keys(["ID", "Name"])
        assert acc.finish()["annotation_attribute_keys"] == {
            "ID": 2,
            "Name": 2,
            "Parent": 1,
        }


class TestHeaderDirectives:
    def test_reads_gff_version_and_source(self):
        header = [
            "##gff-version 3",
            "#!genome-build GRCh38.p13",
            "#!annotation-source NCBI RefSeq GCF_000001405.39",
        ]
        meta = parse_header_directives(header)
        assert meta["gff_version"] == "3"
        assert meta["genome_build"] == "GRCh38.p13"
        assert meta["annotation_source"] == "NCBI RefSeq GCF_000001405.39"

    def test_empty_for_no_directives(self):
        assert parse_header_directives(["chr1\t.\tgene\t1\t9\t.\t+\t.\tID=g1"]) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_stats.py -q
```

Expected: `ModuleNotFoundError: No module named 'app.pipelines.annotation_stats'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/annotation_stats.py`:

```python
"""Bounded aggregates over a stream of features.

Everything here is O(contigs) or O(distinct types) in memory, never O(features)
-- a human GFF3 has millions of rows and the accumulator runs inside the same
single pass that builds the database.

The one exception is per-contig interval merging, which holds a list of merged
intervals per contig. That is bounded by the annotation's structure rather than
its row count (merged intervals collapse as they overlap), and on a dense
annotation it converges to roughly one interval per gene-dense region.
"""

from app.pipelines.annotation_parse import Feature

# Histogram edges for feature length, in bases. Chosen for annotation shapes:
# exons are 10^2, genes 10^3-10^4, and a whole-chromosome feature is an
# outlier worth seeing rather than clipping.
_LENGTH_BINS: tuple[int, ...] = (
    0, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 50_000, 100_000,
)


def parse_header_directives(lines: list[str]) -> dict:
    """Provenance from the `##`/`#!` header block.

    Only the directives worth showing in the provenance line. An unknown
    directive is ignored rather than stored: this feeds a one-line UI string,
    not a general metadata store.
    """
    meta: dict = {}
    for line in lines:
        line = line.strip()
        if line.startswith("##gff-version"):
            parts = line.split()
            if len(parts) > 1:
                meta["gff_version"] = parts[1]
        elif line.startswith("#!genome-build "):
            meta["genome_build"] = line[len("#!genome-build "):].strip()
        elif line.startswith("#!annotation-source "):
            meta["annotation_source"] = line[len("#!annotation-source "):].strip()
        elif line.startswith("##source-version "):
            meta["source_version"] = line[len("##source-version "):].strip()
    return meta


class _ContigCoverage:
    """Merged intervals for one contig.

    Kept sorted and merged on insert rather than sorted at the end, because
    the end is where memory is tightest and a full sort of every interval on a
    3M-feature file is exactly what this accumulator exists to avoid.
    """

    def __init__(self) -> None:
        self.intervals: list[list[int]] = []

    def add(self, start: int, end: int) -> None:
        # Fast path: features usually arrive in coordinate order, so the new
        # interval either extends the last one or starts after it.
        if self.intervals:
            last = self.intervals[-1]
            if start > last[1] + 1:
                self.intervals.append([start, end])
                return
            if start >= last[0]:
                last[1] = max(last[1], end)
                return

        # Slow path: out-of-order input. Insert and re-merge.
        self.intervals.append([start, end])
        self.intervals.sort()
        merged: list[list[int]] = []
        for iv in self.intervals:
            if merged and iv[0] <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], iv[1])
            else:
                merged.append(iv)
        self.intervals = merged

    def covered_bases(self) -> int:
        return sum(end - start + 1 for start, end in self.intervals)


class AnnotationAccumulator:
    """Every bounded number the Results view shows, from one pass.

    `contig_lengths` comes from the reference's facts via the job payload --
    the handler runs in a worker process and cannot query for it.
    """

    def __init__(self, *, contig_lengths: dict[str, int]) -> None:
        self._contig_lengths = contig_lengths
        self._total = 0
        self._top_level = 0
        self._malformed = 0
        self._types: dict[str, int] = {}
        self._biotypes: dict[str, int] = {}
        self._attr_keys: dict[str, int] = {}
        self._per_contig_count: dict[str, int] = {}
        self._coverage: dict[str, _ContigCoverage] = {}
        self._length_counts = [0] * (len(_LENGTH_BINS) + 1)

    def add(self, f: Feature) -> None:
        self._total += 1
        if f.parent is None:
            self._top_level += 1
        if f.type:
            self._types[f.type] = self._types.get(f.type, 0) + 1
        if f.biotype:
            self._biotypes[f.biotype] = self._biotypes.get(f.biotype, 0) + 1

        self._per_contig_count[f.contig] = self._per_contig_count.get(f.contig, 0) + 1
        cov = self._coverage.get(f.contig)
        if cov is None:
            cov = self._coverage[f.contig] = _ContigCoverage()
        cov.add(f.start, f.end)

        length = f.end - f.start + 1
        for i, edge in enumerate(_LENGTH_BINS):
            if length <= edge:
                self._length_counts[i] += 1
                break
        else:
            self._length_counts[-1] += 1

    def add_malformed(self) -> None:
        self._malformed += 1

    def add_attribute_keys(self, keys) -> None:
        for key in keys:
            self._attr_keys[key] = self._attr_keys.get(key, 0) + 1

    def finish(self) -> dict:
        per_contig = []
        for name, count in sorted(
            self._per_contig_count.items(), key=lambda kv: -kv[1]
        ):
            length = self._contig_lengths.get(name)
            covered = self._coverage[name].covered_bases()
            # Clamped: a feature running past a contig's recorded length
            # (a mismatched annotation/reference pair) must not report
            # coverage above 1.0, which reads as a bug in the chart.
            if length:
                covered = min(covered, length)
            per_contig.append(
                {
                    "name": name,
                    "length": length,
                    "count": count,
                    "covered_bases": covered,
                    # None, not 0.0: an unknown contig length is not zero
                    # coverage, and a chart must not draw it as an empty bar.
                    "covered_fraction": (
                        round(covered / length, 6) if length else None
                    ),
                    "per_mb": (
                        round(count / (length / 1_000_000), 3) if length else None
                    ),
                }
            )

        histogram = []
        for i, edge in enumerate(_LENGTH_BINS):
            low = 0 if i == 0 else _LENGTH_BINS[i - 1] + 1
            histogram.append({"min": low, "max": edge, "count": self._length_counts[i]})
        histogram.append(
            {
                "min": _LENGTH_BINS[-1] + 1,
                "max": None,
                "count": self._length_counts[-1],
            }
        )

        facts: dict = {
            "annotation_feature_count": self._total,
            "annotation_top_level_count": self._top_level,
            "annotation_contig_count": len(self._per_contig_count),
            "annotation_per_contig": per_contig,
            "annotation_length_histogram": histogram,
        }
        # Each of these is omitted rather than emitted empty: the UI renders a
        # block only when its key is present, and an empty dict would draw an
        # empty chart instead of nothing.
        if self._types:
            facts["annotation_type_counts"] = self._types
        if self._biotypes:
            facts["annotation_biotype_counts"] = self._biotypes
        if self._attr_keys:
            facts["annotation_attribute_keys"] = self._attr_keys
        if self._malformed:
            facts["annotation_malformed_lines"] = self._malformed
        return facts
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_stats.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_stats.py backend/tests/pipelines/test_annotation_stats.py
git commit -m "feat(pipelines): accumulate annotation aggregates in one bounded pass"
```

---

## Task 3: The SQLite feature database

**Files:**
- Create: `backend/app/pipelines/annotation_db.py`
- Test: `backend/tests/pipelines/test_annotation_db.py`

Read `backend/app/pipelines/variant_db.py` before starting. This module is its sibling and should look like it.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_annotation_db.py`:

```python
"""The SQLite database backing the feature table.

Separate from annotation_stats because this is the only stateful part of the
feature and the only place SQL lives -- the same split variant_db documents.
"""

import pytest

from app.pipelines.annotation_parse import Feature
from app.pipelines.annotation_db import (
    FeatureFilters,
    build_annotation_db,
    children_of,
    count_features,
    query_features,
)


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
        parent=parent,
        biotype=biotype,
        attributes=f"ID={feature_id}",
    )


def _features():
    """Two genes on chr1 with exons, one gene on chr2, one bare BED-ish row."""
    return iter(
        [
            _f("chr1", 1000, 2000, "gene", "g1", name="BRCA1", biotype="protein_coding"),
            _f("chr1", 1000, 1200, "exon", "e1", parent="g1"),
            _f("chr1", 1800, 2000, "exon", "e2", parent="g1"),
            _f("chr1", 5000, 6000, "gene", "g2", name="KINASE1", biotype="lncRNA"),
            _f("chr1", 5000, 5100, "exon", "e3", parent="g2"),
            _f("chr2", 100, 900, "gene", "g3", name="TP53", biotype="protein_coding"),
            _f("chr2", 2000, 2500, None, "b1", name=None),
        ]
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "features.db"
    build_annotation_db(rows=_features(), db_path=path)
    return path


class TestBuild:
    def test_inserts_every_row(self, tmp_path):
        path = tmp_path / "features.db"
        assert build_annotation_db(rows=_features(), db_path=path) == 7

    def test_rebuild_replaces_rather_than_appends(self, tmp_path):
        path = tmp_path / "features.db"
        build_annotation_db(rows=_features(), db_path=path)
        assert build_annotation_db(rows=_features(), db_path=path) == 7
        assert count_features(db_path=path, filters=FeatureFilters(top_level_only=False)) == 7


class TestTopLevelPaging:
    def test_defaults_to_top_level_only(self, db):
        """The table opens on parents, not on three million exons."""
        rows = query_features(db_path=db, filters=FeatureFilters(), offset=0, limit=50)
        assert [r["feature_id"] for r in rows] == ["g1", "g2", "g3", "b1"]

    def test_count_matches_the_page(self, db):
        """The page and its total must agree or pagination misreports."""
        filters = FeatureFilters()
        assert count_features(db_path=db, filters=filters) == 4

    def test_offset_and_limit(self, db):
        rows = query_features(db_path=db, filters=FeatureFilters(), offset=1, limit=2)
        assert [r["feature_id"] for r in rows] == ["g2", "g3"]

    def test_rows_carry_a_has_children_flag(self, db):
        """The chevron must not render on a row with nothing under it."""
        rows = query_features(db_path=db, filters=FeatureFilters(), offset=0, limit=50)
        by_id = {r["feature_id"]: r for r in rows}
        assert by_id["g1"]["has_children"] is True
        assert by_id["g3"]["has_children"] is False


class TestChildren:
    def test_returns_children_in_position_order(self, db):
        rows = children_of(db_path=db, parent_id="g1")
        assert [r["feature_id"] for r in rows] == ["e1", "e2"]

    def test_empty_for_a_leaf(self, db):
        assert children_of(db_path=db, parent_id="g3") == []

    def test_empty_for_an_unknown_parent(self, db):
        assert children_of(db_path=db, parent_id="nope") == []


class TestFilters:
    def test_filter_by_contig(self, db):
        filters = FeatureFilters(contig="chr2")
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert {r["feature_id"] for r in rows} == {"g3", "b1"}
        assert count_features(db_path=db, filters=filters) == 2

    def test_type_filter_searches_children_too(self, db):
        """The interaction that silently breaks the table.

        Filtering to `exon` must clear top_level_only -- every exon has a
        parent, so leaving it set returns an empty table on a valid GFF3.
        """
        filters = FeatureFilters(feature_type="exon")
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert {r["feature_id"] for r in rows} == {"e1", "e2", "e3"}
        assert count_features(db_path=db, filters=filters) == 3

    def test_biotype_filter(self, db):
        filters = FeatureFilters(biotype="protein_coding")
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert {r["feature_id"] for r in rows} == {"g1", "g3"}

    def test_name_search_is_substring_and_case_insensitive(self, db):
        filters = FeatureFilters(name_query="kinase")
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert [r["feature_id"] for r in rows] == ["g2"]

    def test_name_search_escapes_like_wildcards(self, db):
        """A literal % must not match everything."""
        filters = FeatureFilters(name_query="%")
        assert count_features(db_path=db, filters=filters) == 0

    def test_strand_filter(self, db):
        filters = FeatureFilters(strand="-")
        assert count_features(db_path=db, filters=filters) == 0

    def test_filters_compose(self, db):
        filters = FeatureFilters(contig="chr1", biotype="protein_coding")
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert [r["feature_id"] for r in rows] == ["g1"]


class TestLocusJump:
    def test_finds_features_overlapping_a_window(self, db):
        """Overlap, not containment: a gene straddling the window's edge is
        in the window as far as anyone looking at that locus is concerned."""
        filters = FeatureFilters(contig="chr1", start_min=1900, start_max=5200)
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert {r["feature_id"] for r in rows} == {"g1", "g2"}

    def test_excludes_features_outside_the_window(self, db):
        filters = FeatureFilters(contig="chr1", start_min=3000, start_max=4000)
        assert count_features(db_path=db, filters=filters) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_db.py -q
```

Expected: `ModuleNotFoundError: No module named 'app.pipelines.annotation_db'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/annotation_db.py`:

```python
"""The SQLite database backing the annotation feature table.

The sibling of `variant_db`, for the same reason and with the same shape: a
human GFF3 holds millions of rows, where reading the whole file to slice a
page in Python costs hundreds of MB of RSS per in-flight request, and the
same data in SQLite answers a filtered page in well under a millisecond.

What differs from variant_db is hierarchy. Every row is stored, but the table
pages over *top-level* features (those with no parent) and fetches children
per-parent on expand -- see the spec's paging decision. That keeps LIMIT and
OFFSET meaning the same thing they mean in the variant table.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger
from app.pipelines.annotation_parse import Feature

log = get_logger(__name__)

# Batched inserts, same reasoning as variant_db._INSERT_BATCH.
_INSERT_BATCH = 10_000

# The columns every read returns. Named once so the page query and the
# children query cannot drift apart about the row shape the client parses.
_COLUMNS = (
    "contig, start, end, type, strand, score, name, feature_id, "
    "parent, biotype, attributes"
)


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
    """

    contig: str | None = None
    start_min: int | None = None
    start_max: int | None = None
    feature_type: str | None = None
    biotype: str | None = None
    name_query: str | None = None
    strand: str | None = None
    top_level_only: bool = True


def build_annotation_db(*, rows, db_path: Path) -> int:
    """Stream features into an indexed SQLite database.

    `rows` is consumed once and never materialized: at 3M features a list
    would exhaust the container before a row was written.

    Indexes are built *after* the bulk insert -- creating them first makes
    every insert maintain four B-trees. Journaling and synchronous writes are
    off because this file is a derived artifact rebuilt from the annotation on
    demand, so durability buys nothing.

    Returns the number of rows inserted.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    con = sqlite3.connect(db_path)
    try:
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
              attributes TEXT
            )
            """
        )

        inserted = 0
        batch: list[tuple] = []
        for f in rows:
            batch.append(
                (
                    f.contig, f.start, f.end, f.type, f.strand, f.score,
                    f.name, f.feature_id, f.parent, f.biotype, f.attributes,
                )
            )
            if len(batch) >= _INSERT_BATCH:
                con.executemany(
                    "INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
                )
                inserted += len(batch)
                batch = []

        if batch:
            con.executemany(
                "INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
            )
            inserted += len(batch)

        con.execute("CREATE INDEX ix_features_locus ON features(contig, start)")
        # The index the whole paging design rests on: expanding a gene must
        # be a seek, not a scan of three million rows.
        con.execute("CREATE INDEX ix_features_parent ON features(parent)")
        con.execute("CREATE INDEX ix_features_type ON features(type)")
        con.execute("CREATE INDEX ix_features_name ON features(name)")
        con.commit()
    finally:
        con.close()

    return inserted


def _where(filters: FeatureFilters) -> tuple[str, list]:
    """The WHERE clause and its bound parameters.

    Every value is bound, never interpolated: these come from query string
    arguments and reach a SQL statement directly.
    """
    clauses: list[str] = []
    args: list = []

    if filters.top_level_only:
        clauses.append("parent IS NULL")
    if filters.contig:
        clauses.append("contig = ?")
        args.append(filters.contig)
    # Overlap rather than containment: a feature straddling the window's edge
    # is at that locus as far as anyone looking there is concerned.
    if filters.start_max is not None:
        clauses.append("start <= ?")
        args.append(filters.start_max)
    if filters.start_min is not None:
        clauses.append("end >= ?")
        args.append(filters.start_min)
    if filters.feature_type:
        clauses.append("type = ?")
        args.append(filters.feature_type)
    if filters.biotype:
        clauses.append("biotype = ?")
        args.append(filters.biotype)
    if filters.strand:
        clauses.append("strand = ?")
        args.append(filters.strand)
    if filters.name_query:
        # LIKE wildcards in user input are escaped: a search for "%" must
        # find features named "%", not every feature in the file.
        escaped = (
            filters.name_query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        clauses.append("name LIKE ? ESCAPE '\\'")
        args.append(f"%{escaped}%")

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), args


def _connect(db_path: Path) -> sqlite3.Connection:
    """Read-only. Nothing but the compute job ever writes, and SQLite handles
    concurrent readers without coordination."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def query_features(
    *, db_path: Path, filters: FeatureFilters, offset: int, limit: int
) -> list[dict]:
    """One page of the table, in file order.

    Ordered by rowid rather than (contig, start): annotation files are written
    in coordinate order, so insertion order already is position order, and an
    explicit ORDER BY would cost a sort on every page. This is the same trade
    `query_variants` documents.

    `has_children` rides along so the client knows whether to draw an expand
    chevron. Computed with an EXISTS subquery against ix_features_parent,
    which is a seek per row rather than a scan.
    """
    where, args = _where(filters)
    con = _connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            f"SELECT {_COLUMNS}, "
            f"EXISTS(SELECT 1 FROM features c WHERE c.parent = features.feature_id) "
            f"AS has_children "
            f"FROM features{where} LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        con.close()

    for r in rows:
        r["has_children"] = bool(r["has_children"])
    return rows


def children_of(*, db_path: Path, parent_id: str) -> list[dict]:
    """Every child of one feature, in position order.

    Unpaged deliberately: a transcript has tens of exons, not thousands, and
    paging inside an expanded row is complexity with no payoff.
    """
    con = _connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            f"SELECT {_COLUMNS} FROM features WHERE parent = ? ORDER BY start",
            (parent_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def count_features(*, db_path: Path, filters: FeatureFilters) -> int:
    """How many rows match. See the route: this is not recomputed on every
    page turn, because a combined predicate cannot use a single index."""
    where, args = _where(filters)
    con = _connect(db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM features{where}", args).fetchone()[0]
    finally:
        con.close()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_db.py -q
```

Expected: all tests pass.

**Note on `test_type_filter_searches_children_too`:** as written above, that test constructs `FeatureFilters(feature_type="exon")`, which leaves `top_level_only` at its `True` default and returns nothing — the test fails against a correct implementation.

The rule that a type filter searches children lives in the **route layer** (Task 6, Step 7), not in `_where`. Fix the test by constructing the filter the way the route will:

```python
    def test_type_filter_searches_children_too(self, db):
        """The interaction that silently breaks the table.

        The route clears top_level_only whenever a type filter is set -- every
        exon has a parent, so leaving it set returns an empty table on a valid
        GFF3. This layer honors the flag it is given; Task 6 is what sets it.
        """
        filters = FeatureFilters(feature_type="exon", top_level_only=False)
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert {r["feature_id"] for r in rows} == {"e1", "e2", "e3"}
        assert count_features(db_path=db, filters=filters) == 3

    def test_type_filter_alone_finds_no_children(self, db):
        """The failure the route exists to prevent, pinned at this layer."""
        filters = FeatureFilters(feature_type="exon")
        assert count_features(db_path=db, filters=filters) == 0
```

Use those two in place of the single test shown above.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/annotation_db.py backend/tests/pipelines/test_annotation_db.py
git commit -m "feat(pipelines): store annotation features in a queryable SQLite index"
```

---

## Task 4: Storage directory and reaping

**Files:**
- Modify: `backend/app/config.py` (after the `vcf_stats_dir` property, ~line 386)
- Modify: `backend/app/queue/handlers.py:895`

- [ ] **Step 1: Add the settings property**

In `backend/app/config.py`, directly after the `vcf_stats_dir` property:

```python
    @property
    def annotation_stats_dir(self) -> Path:
        """Generated Annotation Results artifacts (the SQLite feature table),
        keyed by object id.

        Outside objects/ deliberately, same rationale as vcf_stats_dir: this
        is derivative and regenerable from the annotation file itself, so
        content-addressing it would buy deduplication of something never
        shared and cost a blob record per run.
        """
        return self.bioinfo_home / "annotation_stats"
```

- [ ] **Step 2: Add the directory to the reaper**

In `backend/app/queue/handlers.py:895`, extend the tuple:

```python
    for root in (
        settings.qc_reports_dir,
        settings.bam_stats_dir,
        settings.vcf_stats_dir,
        settings.annotation_stats_dir,
    ):
```

The sweep reaps directories named by object id and never inspects their contents, so a SQLite file needs no special handling.

- [ ] **Step 3: Verify nothing broke**

```bash
./backend/run-worktree-tests.sh tests/queue/ -q
```

Expected: all pass, same count as before the change.

- [ ] **Step 4: Commit**

```bash
git add backend/app/config.py backend/app/queue/handlers.py
git commit -m "feat(ops): keep annotation results artifacts in a reaped directory"
```

---

## Task 5: The compute handler and its applier

**Files:**
- Create: `backend/app/queue/annotation_handlers.py`
- Modify: `backend/app/queue/handlers.py:958` (the import block)
- Modify: `backend/app/queue/results.py` (applier + dispatch entry)

Read `backend/app/queue/variant_handlers.py:388-500` first — `run_vcf_stats` is the template.

- [ ] **Step 1: Write the handler**

Create `backend/app/queue/annotation_handlers.py`:

```python
"""Annotation Results: the feature table and its summary.

Read-only, like run_vcf_stats and run_bam_stats: derives no objects except
the regenerable SQLite database. The bounded summary returns as facts for
`_apply_run_annotation_stats` to merge; the per-feature detail goes to
settings.annotation_stats_dir and is queried by the table's routes.

One pass. The file is read once and every line reaches both the aggregate
accumulator and the database builder, because a 3M-line GFF3 read twice is
two minutes of I/O for numbers that could have been counted the first time.
"""

import gzip
from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import annotation_db, annotation_parse, annotation_stats
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# How many leading comment lines are kept for provenance. The directives
# worth reading are always in the first few; a file whose header runs longer
# than this is listing sequence-regions, which is not what we display.
_HEADER_SCAN_LINES = 50

_PARSERS = {
    "gff": annotation_parse.parse_gff_line,
    "gtf": annotation_parse.parse_gtf_line,
    "bed": annotation_parse.parse_bed_line,
}


def _open_text(path: Path):
    """Gzip-aware line reader.

    Sniffed by magic bytes rather than extension: an annotation downloaded
    from NCBI is gzipped whether or not whoever renamed it kept the suffix.
    """
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


@handler(
    "run_annotation_stats",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
)
def run_annotation_stats(ctx: JobContext) -> dict:
    """Summarize an annotation file and build its feature table."""
    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("run_annotation_stats requires an 'object_id'")

    fmt = ctx.payload.get("format_kind")
    parse_line = _PARSERS.get(fmt)
    if parse_line is None:
        raise PermanentError(f"run_annotation_stats cannot read format {fmt!r}")

    source = Path(ctx.payload["annotation_path"])
    if not source.exists():
        raise PermanentError(f"annotation file is missing: {source}")

    # Contig lengths come from the payload -- the ingest parser already read
    # them from the reference, and the handler cannot query for them.
    contig_lengths = {
        name: int(length)
        for name, length in (ctx.payload.get("contig_lengths") or [])
    }

    acc = annotation_stats.AnnotationAccumulator(contig_lengths=contig_lengths)
    header: list[str] = []

    ctx.progress(phase="parse", pct=0.1, message="reading features")

    def _rows():
        """One pass: every line reaches the accumulator and the database.

        Malformed lines are counted and skipped rather than raised -- a file
        that is half garbage should say so in its provenance line, not fail a
        job that could have summarized the good half.
        """
        with _open_text(source) as fh:
            for i, line in enumerate(fh):
                if i % 100_000 == 0:
                    ctx.check_cancel()
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    if len(header) < _HEADER_SCAN_LINES:
                        header.append(stripped)
                    continue
                feature = parse_line(stripped)
                if feature is None:
                    acc.add_malformed()
                    continue
                acc.add(feature)
                if feature.attributes:
                    if fmt == "gff":
                        keys = annotation_parse.parse_gff_attributes(
                            feature.attributes
                        ).keys()
                    else:
                        keys = annotation_parse.parse_gtf_attributes(
                            feature.attributes
                        ).keys()
                    acc.add_attribute_keys(keys)
                yield feature

    report_dir = settings.annotation_stats_dir / str(object_id)
    report_dir.mkdir(parents=True, exist_ok=True)

    # Built at a temporary path and renamed into place, so a failed recompute
    # leaves the previous working database rather than a half-built one the
    # table would query.
    tmp_db = report_dir / "features.db.tmp"
    total = annotation_db.build_annotation_db(rows=_rows(), db_path=tmp_db)
    tmp_db.replace(report_dir / "features.db")

    ctx.progress(phase="summarize", pct=0.9, message="summarizing")

    facts = {
        "annotation_stats_status": "ok",
        **acc.finish(),
        **annotation_stats.parse_header_directives(header),
    }

    log.info("annotation_stats_built", object_id=str(object_id), features=total)
    return {"object_id": str(object_id), "facts": facts}
```

- [ ] **Step 2: Register the module for import**

In `backend/app/queue/handlers.py:958`, add `annotation_handlers` to the import block, keeping it alphabetical:

```python
from app.queue import (  # noqa: E402, F401
    align_handlers,
    annotation_handlers,
    assembly_handlers,
    ...
)
```

Without this the `@handler` decorator never runs and `run_annotation_stats` does not exist at dispatch time. Nothing fails at import; the job just never resolves.

- [ ] **Step 3: Write the applier**

In `backend/app/queue/results.py`, directly after `_apply_run_vcf_stats`:

```python
async def _apply_run_annotation_stats(result: dict, *, owner: str) -> None:
    """Record an Annotation Results computation on the file it described.

    Read-only like QC, BAM stats, and VCF stats: no files to ingest, just
    facts merged onto the object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("annotation_stats_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "annotation_stats_applied",
        object_id=object_id,
        features=facts.get("annotation_feature_count"),
    )
```

- [ ] **Step 4: Register the applier**

In the dispatch dict at `backend/app/queue/results.py:2629`, beside `run_vcf_stats`:

```python
    "run_annotation_stats": _apply_run_annotation_stats,
```

- [ ] **Step 5: Verify the handler registers**

```bash
./backend/run-worktree-tests.sh tests/queue/ -q
```

Expected: all pass. If a registry exhaustiveness test exists it now covers the new name.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/annotation_handlers.py backend/app/queue/handlers.py backend/app/queue/results.py
git commit -m "feat(pipelines): compute annotation summaries and the feature index"
```

---

## Task 6: Launcher and routes

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (after `launch_vcf_stats`, ~line 2293)
- Modify: `backend/app/api/v1/pipelines.py`
- Test: `backend/tests/pipelines/test_annotation_stats_launch.py`
- Test: `backend/tests/api/test_annotation_stats_endpoints.py`

- [ ] **Step 1: Write the failing launcher tests**

Create `backend/tests/pipelines/test_annotation_stats_launch.py`:

```python
"""Guards on launching an annotation Results computation."""

import pytest

from app.errors import PermanentError
from app.models.object import FormatKind
from app.services.pipeline_service import _check_annotation_stats_callable


class _Obj:
    def __init__(self, kind, status="ready"):
        self.format = type("F", (), {"kind": kind})()
        self.status = status
        self.name = "ann.gff3"


class TestCallableGuard:
    @pytest.mark.parametrize("kind", [FormatKind.GFF, FormatKind.GTF, FormatKind.BED])
    def test_accepts_annotation_formats(self, kind):
        _check_annotation_stats_callable(_Obj(kind))

    @pytest.mark.parametrize("kind", [FormatKind.BAM, FormatKind.VCF, FormatKind.FASTA])
    def test_rejects_other_formats(self, kind):
        with pytest.raises(PermanentError, match="annotation"):
            _check_annotation_stats_callable(_Obj(kind))

    def test_rejects_an_object_still_ingesting(self):
        with pytest.raises(PermanentError, match="ready"):
            _check_annotation_stats_callable(_Obj(FormatKind.GFF, status="ingesting"))
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_stats_launch.py -q
```

Expected: `ImportError: cannot import name '_check_annotation_stats_callable'`.

- [ ] **Step 3: Write the launcher**

In `backend/app/services/pipeline_service.py`, after `launch_vcf_stats`:

```python
_ANNOTATION_STATS_FORMATS = (FormatKind.GFF, FormatKind.GTF, FormatKind.BED)


def _check_annotation_stats_callable(obj) -> None:
    """Refuse early and with a reason a person can act on."""
    if obj.format.kind not in _ANNOTATION_STATS_FORMATS:
        raise PermanentError(
            f"{obj.name} is not an annotation file (GFF, GTF, or BED)"
        )
    if obj.status != ObjectStatus.READY:
        raise PermanentError(f"{obj.name} is not ready yet")


async def launch_annotation_stats(*, object_id: PydanticObjectId, owner: str):
    """Queue the Results computation for a GFF/GTF/BED.

    Read-only, like launch_vcf_stats: no derived objects, just facts merged
    onto the object plus a SQLite database on disk.

    Requires no external tool -- the parse is pure Python -- so unlike the
    variant path there is no `tools.require` here.
    """
    from app.queue import queue
    from app.services import object_service

    ann = await object_service.get_object(object_id, owner=owner)
    _check_annotation_stats_callable(ann)

    digest, path = await _resolve_readable(ann)

    # Contig lengths for the coverage denominators. Taken from the
    # annotation's own facts when ingest recorded them, else from the
    # reference it is attached to. Absent is fine: coverage is reported as
    # null rather than zero for a contig of unknown length.
    lengths = ann.facts.get("reference_lengths") or {}
    if not lengths:
        reference = await _reference_for_annotation(ann)
        if reference is not None:
            lengths = reference.facts.get("sequence_lengths") or {}

    payload: dict = {
        "object_id": str(ann.id),
        "project_id": str(ann.project_id),
        "format_kind": str(ann.format.kind),
        "contig_lengths": [[name, length] for name, length in lengths.items()],
    }
    if digest:
        payload["annotation_sha256"] = digest
    if path:
        payload["annotation_path"] = path

    return await queue.enqueue(
        "run_annotation_stats",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"annotation_stats:{ann.id}",
        project_id=ann.project_id,
        object_id=ann.id,
    )


async def _reference_for_annotation(ann) -> DataObject | None:
    """The reference this annotation describes, from its provenance.

    Best-effort: an annotation with no recorded reference still computes,
    reporting per-contig counts without coverage fractions.
    """
    for parent_id in (ann.derived_from or []):
        parent = await DataObject.get(parent_id)
        if parent is not None and parent.format.kind == FormatKind.FASTA:
            return parent
    return None
```

Check the imports at the top of `pipeline_service.py` — `FormatKind`, `ObjectStatus`, and `DataObject` are likely already imported; add any that are not.

- [ ] **Step 4: Run the launcher tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_stats_launch.py -q
```

Expected: all pass.

- [ ] **Step 5: Write the failing route tests**

Create `backend/tests/api/test_annotation_stats_endpoints.py`, following the shape of `backend/tests/api/test_de_variant_summary_endpoints.py` for client and auth fixtures:

```python
"""The annotation feature table's routes.

The ownership check is the point of the first test: annotation_stats_dir is
laid out by object id alone, so that lookup is the only thing standing
between one profile and another profile's data.
"""

import pytest

from app.pipelines.annotation_db import build_annotation_db
from app.pipelines.annotation_parse import Feature


def _feature(feature_id, parent=None, start=100):
    return Feature(
        contig="chr1", start=start, end=start + 50, type="gene", strand="+",
        score=None, name=feature_id, feature_id=feature_id, parent=parent,
        biotype="protein_coding", attributes=f"ID={feature_id}",
    )


@pytest.fixture
def features_db(tmp_path, monkeypatch):
    """A built database at the path the routes resolve for one object id."""
    from app.config import settings

    monkeypatch.setattr(
        type(settings), "annotation_stats_dir",
        property(lambda self: tmp_path / "annotation_stats"),
    )

    def _build(object_id):
        path = tmp_path / "annotation_stats" / str(object_id) / "features.db"
        build_annotation_db(
            rows=iter([_feature("g1"), _feature("e1", parent="g1", start=110)]),
            db_path=path,
        )
        return path

    return _build


class TestFeaturesRoute:
    async def test_returns_a_page_with_a_total(self, client, ready_annotation, features_db):
        features_db(ready_annotation.id)
        r = await client.get(f"/api/v1/pipelines/annotationstats/features/{ready_annotation.id}")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1  # top-level only
        assert [row["feature_id"] for row in body["rows"]] == ["g1"]

    async def test_skip_count_omits_the_total(self, client, ready_annotation, features_db):
        features_db(ready_annotation.id)
        r = await client.get(
            f"/api/v1/pipelines/annotationstats/features/{ready_annotation.id}"
            "?skip_count=true"
        )
        assert r.json()["total"] is None

    async def test_type_filter_reaches_children(self, client, ready_annotation, features_db):
        """The route clears top_level_only when a type filter is set."""
        features_db(ready_annotation.id)
        r = await client.get(
            f"/api/v1/pipelines/annotationstats/features/{ready_annotation.id}"
            "?feature_type=gene"
        )
        assert {row["feature_id"] for row in r.json()["rows"]} == {"g1", "e1"}

    async def test_404_when_not_computed(self, client, ready_annotation):
        r = await client.get(
            f"/api/v1/pipelines/annotationstats/features/{ready_annotation.id}"
        )
        assert r.status_code == 404
        assert "Compute results first" in r.json()["detail"]

    async def test_another_profile_cannot_read_it(
        self, client, other_owner_annotation, features_db
    ):
        features_db(other_owner_annotation.id)
        r = await client.get(
            f"/api/v1/pipelines/annotationstats/features/{other_owner_annotation.id}"
        )
        assert r.status_code in (403, 404)


class TestChildrenRoute:
    async def test_returns_children(self, client, ready_annotation, features_db):
        features_db(ready_annotation.id)
        r = await client.get(
            f"/api/v1/pipelines/annotationstats/children/{ready_annotation.id}?parent_id=g1"
        )
        assert [row["feature_id"] for row in r.json()["rows"]] == ["e1"]

    async def test_empty_for_unknown_parent(self, client, ready_annotation, features_db):
        features_db(ready_annotation.id)
        r = await client.get(
            f"/api/v1/pipelines/annotationstats/children/{ready_annotation.id}?parent_id=nope"
        )
        assert r.json()["rows"] == []
```

Add `ready_annotation` and `other_owner_annotation` fixtures to `backend/tests/api/conftest.py` if no equivalent exists — model them on whatever fixture the VCF endpoint tests use for a ready object, with `format.kind` set to `FormatKind.GFF`.

- [ ] **Step 6: Run to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/api/test_annotation_stats_endpoints.py -q
```

Expected: 404s on every route (they do not exist yet).

- [ ] **Step 7: Write the routes**

In `backend/app/api/v1/pipelines.py`, after the vcfstats routes:

```python
class AnnotationStatsRequest(BaseModel):
    object_id: PydanticObjectId


@router.post(
    "/annotationstats", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_annotation_stats(
    body: AnnotationStatsRequest, owner: OwnerDep
) -> JobOut:
    """Queue the Results computation for a GFF/GTF/BED: feature summary and
    the searchable feature table. Read-only."""
    job = await pipeline_service.launch_annotation_stats(
        object_id=body.object_id, owner=owner
    )
    return JobOut.of(job)


@router.get("/annotationstats/features/{object_id}")
async def get_annotation_features(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    offset: int = 0,
    limit: int = 100,
    contig: str | None = None,
    start_min: int | None = None,
    start_max: int | None = None,
    feature_type: str | None = None,
    biotype: str | None = None,
    name_query: str | None = None,
    strand: str | None = None,
    skip_count: bool = False,
) -> dict:
    """A page of the feature table, filtered.

    Rows are top-level features by default -- a GFF3's genes rather than its
    three million exons -- and children are fetched per-parent by the sibling
    route when a row is expanded.

    `total` is the count *after* filtering, so pagination stays correct. It is
    omitted when `skip_count` is set, the same trade the variant route
    documents: a combined predicate cannot use a single index, so the client
    sends this when only the page number changed.

    The ownership check runs before the path is built, for the same reason the
    variant routes do it: `annotation_stats_dir` is laid out by object id
    alone, so the only thing standing between one profile and another
    profile's annotations is this lookup.
    """
    await object_service.get_object(object_id, owner=owner)

    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    # A type filter must search the whole file: every exon has a parent, so
    # leaving top_level_only set would return an empty table on a valid GFF3.
    filters = annotation_db.FeatureFilters(
        contig=contig,
        start_min=start_min,
        start_max=start_max,
        feature_type=feature_type,
        biotype=biotype,
        name_query=name_query,
        strand=strand,
        top_level_only=feature_type is None,
    )

    rows = annotation_db.query_features(
        db_path=db_path, filters=filters, offset=offset, limit=limit
    )
    total = (
        None
        if skip_count
        else annotation_db.count_features(db_path=db_path, filters=filters)
    )
    return {"total": total, "rows": rows}


@router.get("/annotationstats/children/{object_id}")
async def get_annotation_children(
    object_id: PydanticObjectId, parent_id: str, owner: OwnerDep
) -> dict:
    """Every child of one feature. Unpaged: a transcript has tens of exons."""
    await object_service.get_object(object_id, owner=owner)

    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    return {"rows": annotation_db.children_of(db_path=db_path, parent_id=parent_id)}
```

Add `annotation_db` to the `from app.pipelines import ...` line at the top of the file.

- [ ] **Step 8: Run the route tests**

```bash
./backend/run-worktree-tests.sh tests/api/test_annotation_stats_endpoints.py -q
```

Expected: all pass.

- [ ] **Step 9: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. Read the count, not the exit code.

- [ ] **Step 10: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/api/v1/pipelines.py backend/tests/pipelines/test_annotation_stats_launch.py backend/tests/api/test_annotation_stats_endpoints.py
git commit -m "feat(api): serve the annotation feature table, filtered and paged"
```

---

## Task 7: Frontend types and client

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add the types**

In `frontend/src/api/types.ts`, beside the other facts interfaces:

```typescript
/** One row of the annotation feature table. */
export interface AnnotationFeature {
  contig: string;
  start: number;
  end: number;
  type: string | null;
  strand: string | null;
  score: number | null;
  name: string | null;
  feature_id: string | null;
  parent: string | null;
  biotype: string | null;
  attributes: string | null;
  has_children: boolean;
}

/** A page of the feature table. `total` is null when skip_count was set. */
export interface AnnotationFeaturePage {
  total: number | null;
  rows: AnnotationFeature[];
}

export interface AnnotationContigStat {
  name: string;
  length: number | null;
  count: number;
  covered_bases: number;
  /** Null when the contig's length is unknown -- not zero coverage. */
  covered_fraction: number | null;
  per_mb: number | null;
}

export interface AnnotationLengthBin {
  min: number;
  max: number | null;
  count: number;
}

/** Facts written by run_annotation_stats. Every optional field is absent
 *  rather than empty when it has nothing to say, so a block renders only
 *  when there is something in it. */
export interface AnnotationStatsFacts {
  annotation_stats_status?: "ok";
  annotation_feature_count?: number;
  annotation_top_level_count?: number;
  annotation_contig_count?: number;
  annotation_per_contig?: AnnotationContigStat[];
  annotation_length_histogram?: AnnotationLengthBin[];
  annotation_type_counts?: Record<string, number>;
  annotation_biotype_counts?: Record<string, number>;
  annotation_attribute_keys?: Record<string, number>;
  annotation_malformed_lines?: number;
  gff_version?: string;
  genome_build?: string;
  annotation_source?: string;
  source_version?: string;
}

export interface FeatureQuery {
  offset: number;
  limit: number;
  contig?: string;
  startMin?: number;
  startMax?: number;
  featureType?: string;
  biotype?: string;
  nameQuery?: string;
  strand?: string;
  skipCount?: boolean;
}
```

- [ ] **Step 2: Add the client methods**

In `frontend/src/api/client.ts`, beside `launchVcfStats`:

```typescript
  /** Queue the Results computation for a GFF/GTF/BED. Read-only: produces
   * facts and a SQLite feature index, no derived objects. */
  launchAnnotationStats: (objectId: string, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/annotationstats${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify({ object_id: objectId }),
      }),

  /** A page of the feature table. Rows are top-level features unless a type
   *  filter is set, in which case the server searches children too. */
  annotationFeatures: (objectId: string, q: FeatureQuery) => {
    const p = new URLSearchParams({
      offset: String(q.offset),
      limit: String(q.limit),
    });
    if (q.contig) p.set("contig", q.contig);
    if (q.startMin != null) p.set("start_min", String(q.startMin));
    if (q.startMax != null) p.set("start_max", String(q.startMax));
    if (q.featureType) p.set("feature_type", q.featureType);
    if (q.biotype) p.set("biotype", q.biotype);
    if (q.nameQuery) p.set("name_query", q.nameQuery);
    if (q.strand) p.set("strand", q.strand);
    if (q.skipCount) p.set("skip_count", "true");
    return request<AnnotationFeaturePage>(
      `/pipelines/annotationstats/features/${objectId}?${p.toString()}`,
    );
  },

  /** Every child of one feature, for an expanded row. */
  annotationChildren: (objectId: string, parentId: string) =>
    request<{ rows: AnnotationFeature[] }>(
      `/pipelines/annotationstats/children/${objectId}?parent_id=${encodeURIComponent(parentId)}`,
    ),
```

Add the new type names to the existing `import type { ... }` block at the top of `client.ts`.

- [ ] **Step 3: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(frontend): add the annotation results client and its types"
```

---

## Task 8: Charts

**Files:**
- Create: `frontend/src/components/AnnotationCharts.tsx`

Read `frontend/src/components/VariantCharts.tsx` first and match its chart idiom, container sizing, and theme tokens.

- [ ] **Step 1: Write the component**

Create `frontend/src/components/AnnotationCharts.tsx` with four exported charts. Each takes already-computed facts — no data fetching, no derivation beyond reshaping for Recharts:

```typescript
import {
  Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import type { AnnotationContigStat, AnnotationLengthBin } from "../api/types";

/** Counts by feature type. GFF/GTF only -- a BED has no types, and the
 *  caller renders nothing rather than an empty chart. */
export function FeatureTypeChart({ counts }: { counts: Record<string, number> }) {
  const data = Object.entries(counts)
    .map(([type, count]) => ({ type, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 20);

  return (
    <ResponsiveContainer width="100%" height={Math.max(160, data.length * 24)}>
      <BarChart data={data} layout="vertical" margin={{ left: 80, right: 16 }}>
        <CartesianGrid strokeDasharray="3 3" className="chart-grid" />
        <XAxis type="number" />
        <YAxis type="category" dataKey="type" width={80} />
        <Tooltip />
        <Bar dataKey="count" fill="var(--chart-1)" />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Counts by biotype. Rendered only when the annotation carried biotypes. */
export function BiotypeChart({ counts }: { counts: Record<string, number> }) {
  const data = Object.entries(counts)
    .map(([biotype, count]) => ({ biotype, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 15);

  return (
    <ResponsiveContainer width="100%" height={Math.max(160, data.length * 24)}>
      <BarChart data={data} layout="vertical" margin={{ left: 110, right: 16 }}>
        <CartesianGrid strokeDasharray="3 3" className="chart-grid" />
        <XAxis type="number" />
        <YAxis type="category" dataKey="biotype" width={110} />
        <Tooltip />
        <Bar dataKey="count" fill="var(--chart-2)" />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Features per megabase, per contig. Contigs of unknown length are
 *  omitted rather than drawn at zero -- see covered_fraction's null. */
export function FeatureDensityChart({ contigs }: { contigs: AnnotationContigStat[] }) {
  const data = contigs
    .filter((c) => c.per_mb != null)
    .slice(0, 40)
    .map((c) => ({ name: c.name, per_mb: c.per_mb }));

  if (data.length === 0) return null;

  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={data} margin={{ bottom: 60 }}>
        <CartesianGrid strokeDasharray="3 3" className="chart-grid" />
        <XAxis dataKey="name" angle={-45} textAnchor="end" interval={0} height={70} />
        <YAxis label={{ value: "features / Mb", angle: -90, position: "insideLeft" }} />
        <Tooltip />
        <Bar dataKey="per_mb" fill="var(--chart-3)" />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Fraction of each contig covered by at least one feature. */
export function CoverageChart({ contigs }: { contigs: AnnotationContigStat[] }) {
  const data = contigs
    .filter((c) => c.covered_fraction != null)
    .slice(0, 40)
    .map((c) => ({ name: c.name, pct: (c.covered_fraction as number) * 100 }));

  if (data.length === 0) return null;

  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={data} margin={{ bottom: 60 }}>
        <CartesianGrid strokeDasharray="3 3" className="chart-grid" />
        <XAxis dataKey="name" angle={-45} textAnchor="end" interval={0} height={70} />
        <YAxis domain={[0, 100]} label={{ value: "% covered", angle: -90, position: "insideLeft" }} />
        <Tooltip formatter={(v: number) => `${v.toFixed(1)}%`} />
        <Bar dataKey="pct" fill="var(--chart-4)" />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Feature length distribution. */
export function LengthHistogram({ bins }: { bins: AnnotationLengthBin[] }) {
  const data = bins.map((b) => ({
    label: b.max == null ? `>${b.min.toLocaleString()}` : `${b.max.toLocaleString()}`,
    count: b.count,
  }));

  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} margin={{ bottom: 40 }}>
        <CartesianGrid strokeDasharray="3 3" className="chart-grid" />
        <XAxis dataKey="label" angle={-45} textAnchor="end" height={50} />
        <YAxis />
        <Tooltip />
        <Bar dataKey="count" fill="var(--chart-5)">
          {data.map((_, i) => (
            <Cell key={i} fill="var(--chart-5)" />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
```

Check `VariantCharts.tsx` for the actual CSS variable names in use (`--chart-1` etc. may differ) and match them.

- [ ] **Step 2: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AnnotationCharts.tsx
git commit -m "feat(ui): chart annotation feature types, density, and coverage"
```

---

## Task 9: The feature table

**Files:**
- Create: `frontend/src/components/AnnotationFeatureTable.tsx`

Read `frontend/src/components/VariantTable.tsx` first for the filter-bar markup, paging controls, and class names.

- [ ] **Step 1: Write the component**

Create `frontend/src/components/AnnotationFeatureTable.tsx`:

```typescript
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { AnnotationFeature, AnnotationStatsFacts } from "../api/types";

const PAGE_SIZE = 100;

/** `chr1:1,000,000-1,050,000` or `chr1:1000000`. Commas and spaces are
 *  tolerated because people paste these from genome browsers. */
function parseLocus(input: string): { contig: string; min?: number; max?: number } | null {
  const text = input.trim().replace(/,/g, "").replace(/\s/g, "");
  if (!text) return null;
  const m = text.match(/^([^:]+)(?::(\d+)(?:-(\d+))?)?$/);
  if (!m) return null;
  return {
    contig: m[1],
    min: m[2] ? Number(m[2]) : undefined,
    max: m[3] ? Number(m[3]) : m[2] ? Number(m[2]) : undefined,
  };
}

export function AnnotationFeatureTable({
  objectId,
  facts,
}: {
  objectId: string;
  facts: AnnotationStatsFacts;
}) {
  const [page, setPage] = useState(0);
  const [contig, setContig] = useState("");
  const [featureType, setFeatureType] = useState("");
  const [biotype, setBiotype] = useState("");
  const [strand, setStrand] = useState("");
  const [nameInput, setNameInput] = useState("");
  const [nameQuery, setNameQuery] = useState("");
  const [locusInput, setLocusInput] = useState("");
  const [locus, setLocus] = useState<ReturnType<typeof parseLocus>>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // Debounced so a typed gene name is one request rather than nine.
  useEffect(() => {
    const t = setTimeout(() => setNameQuery(nameInput), 300);
    return () => clearTimeout(t);
  }, [nameInput]);

  // Any filter change invalidates the current page and any expansion: a
  // stale parent's children must not leak into a new result set.
  useEffect(() => {
    setPage(0);
    setExpanded(new Set());
  }, [contig, featureType, biotype, strand, nameQuery, locus]);

  const contigs = useMemo(
    () => (facts.annotation_per_contig ?? []).map((c) => c.name),
    [facts.annotation_per_contig],
  );
  const types = useMemo(
    () => Object.keys(facts.annotation_type_counts ?? {}).sort(),
    [facts.annotation_type_counts],
  );
  const biotypes = useMemo(
    () => Object.keys(facts.annotation_biotype_counts ?? {}).sort(),
    [facts.annotation_biotype_counts],
  );

  // The total is cached across page turns: COUNT(*) over a composed
  // predicate cannot use a single index, so the server lets us skip it when
  // only the offset changed. Reset whenever a filter changes.
  const cachedTotal = useRef<number | null>(null);
  const filterKey = JSON.stringify({ contig, featureType, biotype, strand, nameQuery, locus });
  const lastFilterKey = useRef(filterKey);
  if (lastFilterKey.current !== filterKey) {
    lastFilterKey.current = filterKey;
    cachedTotal.current = null;
  }

  const query = useQuery({
    queryKey: ["annotation-features", objectId, filterKey, page],
    queryFn: () =>
      api.annotationFeatures(objectId, {
        offset: page * PAGE_SIZE,
        limit: PAGE_SIZE,
        contig: locus?.contig || contig || undefined,
        startMin: locus?.min,
        startMax: locus?.max,
        featureType: featureType || undefined,
        biotype: biotype || undefined,
        nameQuery: nameQuery || undefined,
        strand: strand || undefined,
        skipCount: cachedTotal.current != null,
      }),
  });

  if (query.data?.total != null) cachedTotal.current = query.data.total;
  const total = cachedTotal.current;

  const rows = query.data?.rows ?? [];
  const pageCount = total != null ? Math.ceil(total / PAGE_SIZE) : null;

  return (
    <div className="section">
      <div className="section-title">Features</div>

      <div className="filter-bar">
        <select value={contig} onChange={(e) => setContig(e.target.value)} disabled={!!locus}>
          <option value="">All contigs</option>
          {contigs.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>

        {types.length > 0 && (
          <select value={featureType} onChange={(e) => setFeatureType(e.target.value)}>
            <option value="">Top-level features</option>
            {types.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        )}

        {biotypes.length > 0 && (
          <select value={biotype} onChange={(e) => setBiotype(e.target.value)}>
            <option value="">All biotypes</option>
            {biotypes.map((b) => (
              <option key={b} value={b}>{b}</option>
            ))}
          </select>
        )}

        <select value={strand} onChange={(e) => setStrand(e.target.value)}>
          <option value="">Both strands</option>
          <option value="+">Forward (+)</option>
          <option value="-">Reverse (−)</option>
        </select>

        <input
          type="search"
          placeholder="Search name…"
          value={nameInput}
          onChange={(e) => setNameInput(e.target.value)}
        />

        <input
          type="text"
          placeholder="chr1:1,000,000-1,050,000"
          value={locusInput}
          onChange={(e) => setLocusInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") setLocus(parseLocus(locusInput));
          }}
          onBlur={() => setLocus(parseLocus(locusInput))}
        />
        {locus && (
          <button type="button" className="btn" onClick={() => { setLocusInput(""); setLocus(null); }}>
            Clear locus
          </button>
        )}
      </div>

      {featureType && (
        <div className="section-note">
          Showing every <code>{featureType}</code> feature, including those nested
          under a parent.
        </div>
      )}

      <table className="data-table">
        <thead>
          <tr>
            <th />
            <th>Name</th>
            <th>Type</th>
            <th>Contig</th>
            <th>Start</th>
            <th>End</th>
            <th>Length</th>
            <th>Strand</th>
            <th>Biotype</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((f, i) => (
            <FeatureRow
              key={`${f.feature_id ?? i}-${f.start}`}
              objectId={objectId}
              feature={f}
              expanded={!!f.feature_id && expanded.has(f.feature_id)}
              onToggle={() => {
                if (!f.feature_id) return;
                setExpanded((prev) => {
                  const next = new Set(prev);
                  next.has(f.feature_id!) ? next.delete(f.feature_id!) : next.add(f.feature_id!);
                  return next;
                });
              }}
            />
          ))}
        </tbody>
      </table>

      {query.isPending && <div className="section-note">Loading…</div>}
      {!query.isPending && rows.length === 0 && (
        <div className="section-note">No features match these filters.</div>
      )}

      <div className="pager">
        <button type="button" className="btn" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
          Previous
        </button>
        <span>
          Page {page + 1}
          {pageCount != null ? ` of ${pageCount.toLocaleString()}` : ""}
          {total != null ? ` · ${total.toLocaleString()} features` : ""}
        </span>
        <button
          type="button"
          className="btn"
          disabled={pageCount != null ? page + 1 >= pageCount : rows.length < PAGE_SIZE}
          onClick={() => setPage((p) => p + 1)}
        >
          Next
        </button>
      </div>
    </div>
  );
}

function FeatureRow({
  objectId,
  feature,
  expanded,
  onToggle,
}: {
  objectId: string;
  feature: AnnotationFeature;
  expanded: boolean;
  onToggle: () => void;
}) {
  // Cached per parent by React Query, so re-expanding a row is free.
  const children = useQuery({
    queryKey: ["annotation-children", objectId, feature.feature_id],
    queryFn: () => api.annotationChildren(objectId, feature.feature_id!),
    enabled: expanded && !!feature.feature_id,
  });

  return (
    <>
      <tr>
        <td>
          {feature.has_children && (
            <button
              type="button"
              className="btn-icon"
              aria-label={expanded ? "Collapse" : "Expand"}
              onClick={onToggle}
            >
              {expanded ? "▾" : "▸"}
            </button>
          )}
        </td>
        <td>{feature.name ?? feature.feature_id ?? "—"}</td>
        <td>{feature.type ?? "—"}</td>
        <td>{feature.contig}</td>
        <td>{feature.start.toLocaleString()}</td>
        <td>{feature.end.toLocaleString()}</td>
        <td>{(feature.end - feature.start + 1).toLocaleString()}</td>
        <td>{feature.strand ?? "—"}</td>
        <td>{feature.biotype ?? "—"}</td>
      </tr>
      {expanded &&
        (children.data?.rows ?? []).map((c, i) => (
          <tr key={`${c.feature_id ?? i}-${c.start}`} className="child-row">
            <td />
            <td>{c.name ?? c.feature_id ?? "—"}</td>
            <td>{c.type ?? "—"}</td>
            <td>{c.contig}</td>
            <td>{c.start.toLocaleString()}</td>
            <td>{c.end.toLocaleString()}</td>
            <td>{(c.end - c.start + 1).toLocaleString()}</td>
            <td>{c.strand ?? "—"}</td>
            <td>{c.biotype ?? "—"}</td>
          </tr>
        ))}
      {expanded && children.isPending && (
        <tr className="child-row">
          <td />
          <td colSpan={8}>Loading…</td>
        </tr>
      )}
    </>
  );
}
```

Check `VariantTable.tsx` for the real class names (`filter-bar`, `data-table`, `pager`, `btn-icon`) and match them; add a `.child-row` rule with a left indent to the relevant CSS file if none exists.

- [ ] **Step 2: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AnnotationFeatureTable.tsx
git commit -m "feat(ui): browse annotation features with filters and a locus jump"
```

---

## Task 10: The Results tab

**Files:**
- Create: `frontend/src/components/AnnotationResults.tsx`
- Modify: `frontend/src/components/DetailPanel.tsx:352` and `:806`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/AnnotationResults.tsx`, modeled on `VariantResults.tsx`:

```typescript
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { ObjectDetail as ObjectDetailData, AnnotationStatsFacts } from "../api/types";
import {
  BiotypeChart, CoverageChart, FeatureDensityChart, FeatureTypeChart, LengthHistogram,
} from "./AnnotationCharts";
import { AnnotationFeatureTable } from "./AnnotationFeatureTable";
import { NodeSelector } from "./NodeSelector";

/**
 * What an annotation file contains: how many features of what kinds, where
 * they sit across the reference, how much of it they cover, and the full
 * searchable feature table.
 *
 * Two layers. The interval core -- density, coverage, length distribution --
 * renders for every supported format. The feature-type and biotype blocks
 * render only when the file carried them, which is what lets one view serve
 * both a published GFF3 and a peak-call BED.
 */
export function AnnotationResults({ obj }: { obj: ObjectDetailData }) {
  const qc = useQueryClient();
  const f = obj.facts as AnnotationStatsFacts;
  const [targetNode, setTargetNode] = useState("");

  const compute = useMutation({
    mutationFn: () => api.launchAnnotationStats(obj.id, targetNode || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Computing results");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  if (f.annotation_stats_status !== "ok") {
    return (
      <div className="section">
        <NodeSelector value={targetNode} onChange={setTargetNode} />
        <div className="section-title">Annotation summary</div>
        <div className="section-note">
          Feature counts by type, coverage across the reference, and the full
          searchable feature table — computed on demand from the annotation.
        </div>
        <button
          type="button"
          className="btn primary"
          onClick={() => compute.mutate()}
          disabled={compute.isPending}
        >
          {compute.isPending ? "Computing…" : "Compute results"}
        </button>
      </div>
    );
  }

  const contigs = f.annotation_per_contig ?? [];

  return (
    <>
      <div className="qc-provenance">
        {[
          f.annotation_source ?? null,
          f.genome_build ? `build ${f.genome_build}` : null,
          f.annotation_feature_count != null
            ? `${f.annotation_feature_count.toLocaleString()} features`
            : null,
          f.annotation_contig_count != null
            ? `${f.annotation_contig_count.toLocaleString()} sequences`
            : null,
          // Only when nonzero: a clean file should not display a zero.
          f.annotation_malformed_lines
            ? `${f.annotation_malformed_lines.toLocaleString()} unreadable lines`
            : null,
        ]
          .filter(Boolean)
          .join(" · ")}{" "}
        <button
          type="button"
          className="btn-link"
          onClick={() => compute.mutate()}
          disabled={compute.isPending}
        >
          {compute.isPending ? "Recomputing…" : "Recompute"}
        </button>
      </div>

      {f.annotation_type_counts && (
        <div className="section">
          <div className="section-title">Features by type</div>
          <FeatureTypeChart counts={f.annotation_type_counts} />
        </div>
      )}

      {f.annotation_biotype_counts && (
        <div className="section">
          <div className="section-title">Features by biotype</div>
          <BiotypeChart counts={f.annotation_biotype_counts} />
        </div>
      )}

      {contigs.length > 0 && (
        <>
          <div className="section">
            <div className="section-title">Feature density</div>
            <FeatureDensityChart contigs={contigs} />
          </div>
          <div className="section">
            <div className="section-title">Annotated coverage</div>
            <div className="section-note">
              Fraction of each sequence covered by at least one feature.
              Overlapping features are counted once.
            </div>
            <CoverageChart contigs={contigs} />
          </div>
        </>
      )}

      {f.annotation_length_histogram && (
        <div className="section">
          <div className="section-title">Feature lengths</div>
          <LengthHistogram bins={f.annotation_length_histogram} />
        </div>
      )}

      <AnnotationFeatureTable objectId={obj.id} facts={f} />
    </>
  );
}
```

- [ ] **Step 2: Wire the Results tab**

In `frontend/src/components/DetailPanel.tsx`, extend the `hasResults` gate at line 352:

```typescript
  const hasResults =
    obj.format.kind === "bam" ||
    obj.format.kind === "vcf" ||
    obj.format.kind === "bcf" ||
    obj.format.kind === "gff" ||
    obj.format.kind === "gtf" ||
    obj.format.kind === "bed" ||
```

(keep the existing counts condition that follows).

Then in the render branch near line 806, add the annotation case before the VCF fallback:

```typescript
            ) : obj.format.kind === "gff" ||
              obj.format.kind === "gtf" ||
              obj.format.kind === "bed" ? (
              <AnnotationResults obj={obj} />
```

and add the import beside the others at line 52:

```typescript
import { AnnotationResults } from "./AnnotationResults";
```

- [ ] **Step 3: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AnnotationResults.tsx frontend/src/components/DetailPanel.tsx
git commit -m "feat(ui): give annotation files a results view"
```

---

## Task 11: Verify against real data

Unit tests feed hand-built objects that already look the way the code expects. This repo's CLAUDE.md is explicit that a rule can pass a green suite and still be wrong on a real file — the Actions-tab suggestion rules did exactly that. This task is the check that catches it.

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

UI on 5273, API on 8100. The main instance on 5173 keeps serving main.

- [ ] **Step 2: Restart the worker so the new handler loads**

```bash
COMPOSE_PROJECT_NAME=biopipe-wt docker compose restart worker
```

`worker` does not hot-reload. Check the project name against what `worktree-up.sh` printed, and confirm `run_annotation_stats` appears in the worker's `handlers_loaded` log line.

- [ ] **Step 3: Compute results on a real GFF3**

Open a project with an NCBI annotation at http://localhost:5273, go to the annotation file's Results tab, and press Compute results. Verify:

- feature type counts include `gene`, `mRNA`, `exon`, `CDS`
- the table opens on genes, not exons
- expanding a gene shows its mRNA/exons
- coverage is between 0 and 100% on every contig
- a locus jump finds the genes at that position
- searching a known gene name finds it

- [ ] **Step 4: Verify against the database directly**

Per CLAUDE.md, check the numbers against real objects rather than only fixtures:

```bash
COMPOSE_PROJECT_NAME=biopipe-wt docker compose exec api python -c "
import sqlite3, sys
from app.config import settings
oid = sys.argv[1]
db = settings.annotation_stats_dir / oid / 'features.db'
con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
print('rows      ', con.execute('SELECT COUNT(*) FROM features').fetchone()[0])
print('top-level ', con.execute('SELECT COUNT(*) FROM features WHERE parent IS NULL').fetchone()[0])
print('types     ', con.execute('SELECT type, COUNT(*) FROM features GROUP BY type ORDER BY 2 DESC LIMIT 8').fetchall())
" <object_id>
```

Cross-check `rows` against `grep -vc '^#'` on the source file. They should match exactly minus the malformed count shown in the provenance line.

- [ ] **Step 5: Repeat on a BED**

Confirm the two-layer degradation: density, coverage, and length charts render; feature-type and biotype blocks are absent, not empty. The table shows intervals with no expand chevrons.

- [ ] **Step 6: Check the suggestion service**

```bash
grep -n "annotation" backend/app/services/suggestion_service.py
```

If any card's `unavailable` reason has just stopped being true, update it and add the case to `backend/tests/services/test_suggestion_service.py`.

- [ ] **Step 7: Run the full suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. Read the count.

- [ ] **Step 8: Tear down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 9: Commit any fixes**

```bash
git add -A
git commit -m "fix(pipelines): correct annotation results against real files"
```

(Skip if Step 3–6 found nothing.)

---

## Task 12: Open the PR

- [ ] **Step 1: Confirm the suite is green**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Green is the precondition for pushing, and it means reading the count rather than the exit code.

- [ ] **Step 2: Lint as CI will**

```bash
COMPOSE_PROJECT_NAME=biopipe-wt docker compose exec api ruff check app/
```

CI runs `ruff check` with rules the local suite never invokes — `I001` (import order) caught a real bug on #217/#314 this way. Fix anything it reports before pushing.

- [ ] **Step 3: Push**

```bash
git push -u origin HEAD
```

- [ ] **Step 4: Open the PR**

```bash
gh pr create --base main --fill --label "type:feature,area:pipelines,area:frontend"
```

The title lands in the release notes verbatim, and `.github/release.yml` categorizes by label, not by the `feat:` prefix. Suggested title:

`feat(pipelines): give annotation files a results view`

The body must carry the *why* and `Closes #257`.

- [ ] **Step 5: Watch CI**

```bash
gh pr checks <N> --watch
```

Poll until every check reports pass or fail, not just until the command returns — a "pending" read seconds after creation is the run not having started. Also check for conflicts:

```bash
gh pr view <N> --json mergeable,mergeStateStatus
```

`UNSTABLE` means checks are still running; keep waiting. A real conflict means rebase on `origin/main` and push again. Only report the PR URL once checks are green and `mergeable` is clean.

- [ ] **Step 6: Update the issue**

Comment on [#257](https://github.com/syntheticgio/bioflow/issues/257) with the PR link, and set the label to `status: ready` (or whatever the repo's post-implementation state is).

---

## Notes for the implementer

**Where this is most likely to go wrong**, in order:

1. **Forgetting the import in `handlers.py:958`.** The job silently does not exist. Symptom: the button fires, a job is created, and it fails with an unknown-handler error.

2. **The type filter / `top_level_only` interaction.** The rule lives in the *route* (Task 6, Step 7), not in `_where`. If filtering to `exon` returns nothing, that is the bug.

3. **BED coordinate conversion.** `parse_bed_line` adds 1 to start and leaves end alone. Every other layer assumes one-based inclusive.

4. **Worker not restarted.** The fix appears not to work because the old code is still in memory.

5. **Testing from the worktree with the wrong command.** `docker compose exec api python -m pytest` tests main's code, not yours.

**Two things are computed but deliberately not displayed:**

- **`annotation_attribute_keys`** is accumulated (Task 2) and stored, but no
  chart renders it. That is intentional: it costs nothing to collect during a
  pass we are already making, and it is the input the deferred per-key
  value-distribution follow-up will need. Do not add a chart for it in this
  work — a bare key-frequency table is noise next to the type and biotype
  breakdowns that answer the same question better.
- **Header provenance is GFF/GTF-only.** `parse_header_directives` reads
  `##`/`#!` directives, which BED does not have. A BED's provenance line
  therefore shows counts without a source, which is correct rather than a gap.

**GTF hierarchy is the weakest inference in this plan.** GTF has no formal parent field, so `parse_gtf_line` reconstructs one from `gene_id`/`transcript_id`. It handles Ensembl and GENCODE shapes; a GTF from an unusual writer may nest differently. If a real GTF's table looks wrong, that function is where to look, and the fix is a test case in `TestGtfLine` before a code change.
