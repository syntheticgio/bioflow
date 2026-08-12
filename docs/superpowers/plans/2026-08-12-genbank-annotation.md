# GenBank Annotation Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GenBank flat files (`.gb`, `.gbk`, `.gbff`) a first-class annotation format that flows into the existing annotation Results view.

**Architecture:** Two new pure-Python modules (`genbank_parse.py` for the grammar, `genbank_reader.py` for streaming records) produce the same `Feature` rows that `annotation_db` and `AnnotationAccumulator` already consume. The handler's `_PARSERS` dict generalizes from "format → line function" to "format → row iterator", with GFF/GTF/BED keeping their line loop behind an adapter. Nothing downstream of the parser changes.

**Tech Stack:** Python 3.12, pytest, SQLite (stdlib `sqlite3`), FastAPI. No new dependencies — the parser is hand-written deliberately (see spec).

**Spec:** [`docs/superpowers/specs/2026-08-12-genbank-annotation-design.md`](../specs/2026-08-12-genbank-annotation-design.md)

---

## Background an engineer needs before starting

**What a GenBank flat file looks like.** A file is a sequence of records, each terminated by a line containing only `//`. Every record starts with `LOCUS`, which states the sequence length. Keywords sit in column 1; continuation lines are indented. The `FEATURES` block holds the annotations, and `ORIGIN` (if present) holds the nucleotides.

```
LOCUS       NC_000913               4641652 bp    DNA     circular CON 09-MAR-2022
ACCESSION   NC_000913
VERSION     NC_000913.3
SOURCE      Escherichia coli str. K-12 substr. MG1655
FEATURES             Location/Qualifiers
     source          1..4641652
                     /organism="Escherichia coli str. K-12 substr. MG1655"
                     /mol_type="genomic DNA"
     gene            337..2799
                     /gene="thrA"
                     /locus_tag="b0002"
     CDS             join(complement(337..600),700..2799)
                     /gene="thrA"
                     /locus_tag="b0002"
                     /note="bifunctional: aspartokinase I (N-terminal);
                     homoserine dehydrogenase I (C-terminal)"
                     /pseudo
ORIGIN
        1 agcttttcat tctgactgca acgggcaata tgtctctgtg tggattaaaa aaagagtgtc
//
```

Feature-table geometry, which the parser depends on: the feature **key** (`gene`, `CDS`) begins at column 6, and the **location** at column 22. A qualifier line begins with `/` at column 22. Any line at column 22 that does not begin with `/` is a continuation of whatever came before it — either a wrapped location or a wrapped qualifier value.

**Coordinates.** GenBank is 1-based inclusive, the same convention `Feature` already uses. No conversion is needed (unlike BED — see `annotation_parse.py`'s module docstring).

**Running tests.** This plan's work happens in a worktree, so use the worktree runner, never `docker compose exec api`:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_parse.py -v
```

`docker compose exec api python -m pytest` silently tests **main's** code from a worktree — see CLAUDE.md.

---

## File Structure

**Create:**

| File | Responsibility |
|---|---|
| `backend/app/pipelines/genbank_parse.py` | Pure grammar functions: locations, qualifiers, feature rows. No I/O. |
| `backend/app/pipelines/genbank_reader.py` | Streaming record reader. Yields one record's header + feature lines at a time. |
| `backend/tests/pipelines/test_genbank_parse.py` | Grammar edge cases as plain calls. |
| `backend/tests/pipelines/test_genbank_reader.py` | Record splitting, `ORIGIN` skipping, truncation. |
| `backend/tests/pipelines/test_genbank_stats.py` | Coverage correctness — the intron test. |
| `backend/tests/fixtures/genbank/two_records.gbff` | Hand-built multi-record fixture. |
| `backend/tests/fixtures/genbank/two_records.gbff.gz` | Same file, gzipped. |
| `backend/tests/fixtures/genbank/ecoli_slice.gbff` | Real NCBI excerpt. |

**Modify:**

| File | Change |
|---|---|
| `backend/app/models/object.py` | `FormatKind.GENBANK` |
| `backend/app/storage/detect.py` | Extensions + `LOCUS` sniff |
| `backend/app/storage/compress.py` | `COMPRESSIBLE_KINDS` |
| `backend/app/metadata/schemas.py` | `FORMAT_FIELDS` |
| `backend/app/services/pipeline_service.py` | `_ANNOTATION_STATS_FORMATS`, `_is_annotation` |
| `backend/app/queue/annotation_handlers.py` | `_PARSERS` → row iterators |
| `frontend/src/lib/format.ts` | Format label |
| `frontend/src/icons/getFileIcon.ts` | Icon mapping |

**Task order rationale:** the enum lands first because every other task imports it. The parser (Tasks 2–4) is built and fully tested before anything wires it up, so the grammar is proven before the integration can obscure a bug in it.

---

### Task 1: Add the GENBANK format kind

**Files:**
- Modify: `backend/app/models/object.py:50` (before `TEXT`)
- Modify: `backend/app/metadata/schemas.py:376` (`FORMAT_FIELDS`)
- Test: `backend/tests/storage/test_metadata_schemas.py`

The enum member alone breaks the exhaustiveness tests in `test_metadata_schemas.py`, which assert `set(FormatKind) == set(FORMAT_FIELDS) | FORMAT_COMMON_ONLY`. That failure is the checklist working as designed (CLAUDE.md, "Hand-maintained registries keyed by an enum") — this task adds the member and immediately places it.

- [ ] **Step 1: Add the enum member**

In `backend/app/models/object.py`, insert before `TEXT = "text"`:

```python
    # GenBank flat file. Its own kind rather than TEXT because it is an
    # annotation with a feature table -- it reaches the same Results view as
    # GFF3 -- and because its LOCUS line states contig lengths, which no
    # other annotation format carries.
    GENBANK = "genbank"
```

- [ ] **Step 2: Run the exhaustiveness test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/storage/test_metadata_schemas.py -v
```

Expected: FAIL — a test asserting every `FormatKind` is covered reports `genbank` missing.

- [ ] **Step 3: Place GENBANK in FORMAT_FIELDS**

In `backend/app/metadata/schemas.py`, add to the `FORMAT_FIELDS` dict after the `GTF` line:

```python
    FormatKind.GENBANK: INTERVAL_FIELDS,
```

GenBank goes in `FORMAT_FIELDS`, not `FORMAT_COMMON_ONLY`: it is an interval format and gets the same questions as GFF/GTF.

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/storage/test_metadata_schemas.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/object.py backend/app/metadata/schemas.py
git commit -m "feat(formats): add GENBANK format kind"
```

---

### Task 2: Parse GenBank locations

**Files:**
- Create: `backend/app/pipelines/genbank_parse.py`
- Test: `backend/tests/pipelines/test_genbank_parse.py`

This is the task the issue's core constraint rests on: a `join` must not flatten into a false single interval.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_genbank_parse.py`:

```python
"""Parsing GenBank locations and qualifiers.

Kept free of I/O so every case is a plain function call: this is where the
format edge cases live, and they are the part most likely to be wrong.
"""

from app.pipelines.genbank_parse import Location, parse_location


class TestSimpleLocations:
    def test_plain_range(self):
        loc = parse_location("100..200")
        assert loc == Location(segments=[(100, 200)], strand="+", fuzzy=False)

    def test_single_position(self):
        # A feature at one base: start and end are the same.
        loc = parse_location("467")
        assert loc.segments == [(467, 467)]

    def test_between_positions(self):
        # `102^103` marks a site *between* two bases, used for insertion
        # points. Stored as the left base so it lands somewhere real on the
        # locus chart rather than being dropped.
        loc = parse_location("102^103")
        assert loc.segments == [(102, 102)]

    def test_complement_flips_strand(self):
        loc = parse_location("complement(100..200)")
        assert loc.segments == [(100, 200)]
        assert loc.strand == "-"


class TestFuzzyBounds:
    def test_fuzzy_start(self):
        # `<1` means "starts at or before 1" -- a partial feature running off
        # the contig edge. The bound is used as given and the flag records
        # that it is approximate.
        loc = parse_location("<1..200")
        assert loc.segments == [(1, 200)]
        assert loc.fuzzy is True

    def test_fuzzy_end(self):
        loc = parse_location("100..>200")
        assert loc.segments == [(100, 200)]
        assert loc.fuzzy is True


class TestJoinLocations:
    def test_join_keeps_segments_separate(self):
        # The constraint from #294: this must NOT become (100, 500).
        loc = parse_location("join(100..200,400..500)")
        assert loc.segments == [(100, 200), (400, 500)]
        assert loc.strand == "+"

    def test_join_inside_complement(self):
        # complement(join(...)) -- the whole feature is on the minus strand.
        loc = parse_location("complement(join(100..200,400..500))")
        assert loc.segments == [(100, 200), (400, 500)]
        assert loc.strand == "-"

    def test_complement_inside_join(self):
        # join(complement(...),...) -- mixed strands within one feature.
        # A single strand column cannot express that, so the feature takes
        # the strand of its first segment and every segment is preserved.
        loc = parse_location("join(complement(100..200),400..500)")
        assert loc.segments == [(100, 200), (400, 500)]
        assert loc.strand == "-"

    def test_order_behaves_like_join(self):
        # `order` means the segments are not known to be contiguous. For a
        # feature table that distinction has no representation, and treating
        # it as `join` keeps every segment rather than dropping the feature.
        loc = parse_location("order(100..200,400..500)")
        assert loc.segments == [(100, 200), (400, 500)]


class TestMalformedLocations:
    def test_empty_returns_none(self):
        assert parse_location("") is None

    def test_garbage_returns_none(self):
        # Never raises: an unrecognized grammar must not abort a whole file.
        assert parse_location("not-a-location") is None

    def test_remote_reference_returns_none(self):
        # `J00194.1:100..200` points into another record entirely. There is
        # no contig in this file to attach it to, so it is skipped.
        assert parse_location("J00194.1:100..200") is None

    def test_reversed_bounds_returns_none(self):
        assert parse_location("500..100") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_parse.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.genbank_parse'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/genbank_parse.py`:

```python
"""GenBank feature-table grammar: locations, qualifiers, and feature rows.

Pure functions with no I/O, the sibling of `annotation_parse.py` and for the
same reason -- the format edge cases are most of what this file is, and they
are testable as plain calls only if nothing here touches a file.

GenBank is 1-based inclusive, which is what `Feature` already uses, so unlike
BED there is no coordinate conversion here.
"""

import re
from dataclasses import dataclass

# A single position (`467`), a between-position (`102^103`), or a range
# (`100..200`), any bound optionally fuzzy (`<1`, `>200`).
_RANGE_RE = re.compile(
    r"^<?(\d+)(?:\s*(?:\.\.|\^)\s*>?(\d+))?$"
)


@dataclass(frozen=True)
class Location:
    """Where a feature sits, with its segments kept separate.

    `segments` is never collapsed to outer bounds: a `join` describes a
    feature that does not occupy the gaps between its parts, and flattening
    it would claim coverage over introns it does not cover (#294).
    """

    segments: list[tuple[int, int]]
    strand: str
    fuzzy: bool


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside parentheses.

    `join(complement(1..10),20..30)` splits into two parts, not three: a
    naive `text.split(",")` would cut `complement(1..10` in half.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def parse_location(text: str) -> Location | None:
    """A GenBank location string, or None if it cannot be read.

    None rather than an exception: an unrecognized grammar costs one feature,
    not the whole file. The handler counts these as malformed.
    """
    text = (text or "").strip()
    if not text:
        return None

    # A remote reference names another record. There is no contig in this
    # file it could attach to, so it is skipped rather than misplaced.
    if ":" in text:
        return None

    if text.startswith("complement(") and text.endswith(")"):
        inner = parse_location(text[len("complement(") : -1])
        if inner is None:
            return None
        return Location(segments=inner.segments, strand="-", fuzzy=inner.fuzzy)

    for keyword in ("join(", "order("):
        if text.startswith(keyword) and text.endswith(")"):
            segments: list[tuple[int, int]] = []
            fuzzy = False
            # The feature's strand is its first segment's. A mixed-strand
            # join cannot be expressed in one column; every segment is still
            # preserved, which is what the constraint actually requires.
            strand = "+"
            for i, part in enumerate(_split_top_level(text[len(keyword) : -1])):
                sub = parse_location(part.strip())
                if sub is None:
                    return None
                segments.extend(sub.segments)
                fuzzy = fuzzy or sub.fuzzy
                if i == 0:
                    strand = sub.strand
            if not segments:
                return None
            return Location(segments=segments, strand=strand, fuzzy=fuzzy)

    m = _RANGE_RE.match(text)
    if not m:
        return None
    start = int(m.group(1))
    # A bare position is a one-base feature; `102^103` stores the left base
    # so the site lands somewhere real rather than being dropped.
    end = int(m.group(2)) if m.group(2) else start
    if end < start:
        return None
    return Location(
        segments=[(start, end)],
        strand="+",
        fuzzy="<" in text or ">" in text,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_parse.py -v
```

Expected: PASS, 14 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/genbank_parse.py backend/tests/pipelines/test_genbank_parse.py
git commit -m "feat(pipelines): parse GenBank locations without flattening joins"
```

---

### Task 3: Parse GenBank qualifiers

**Files:**
- Modify: `backend/app/pipelines/genbank_parse.py`
- Modify: `backend/tests/pipelines/test_genbank_parse.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_genbank_parse.py` (and add `parse_qualifiers` to the import at the top of the file):

```python
class TestQualifiers:
    def test_simple_key_value(self):
        lines = ['/gene="thrA"', '/locus_tag="b0002"']
        assert parse_qualifiers(lines) == {"gene": "thrA", "locus_tag": "b0002"}

    def test_valueless_qualifier(self):
        # `/pseudo` has no value. Stored as an empty string so the key is
        # still present -- its presence is the information.
        assert parse_qualifiers(["/pseudo"]) == {"pseudo": ""}

    def test_unquoted_value(self):
        # Numeric qualifiers are conventionally unquoted.
        assert parse_qualifiers(["/codon_start=1"]) == {"codon_start": "1"}

    def test_wrapped_value_joins_with_space(self):
        # A /note wrapping across lines is one value. GenBank wraps on word
        # boundaries, so the parts join with a space.
        lines = [
            '/note="bifunctional: aspartokinase I (N-terminal);',
            'homoserine dehydrogenase I (C-terminal)"',
        ]
        assert parse_qualifiers(lines) == {
            "note": "bifunctional: aspartokinase I (N-terminal); "
                    "homoserine dehydrogenase I (C-terminal)"
        }

    def test_wrapped_translation_joins_without_space(self):
        # /translation is a protein sequence -- joining its parts with a
        # space would corrupt it. This is the one qualifier that wraps
        # mid-token rather than on word boundaries.
        lines = ['/translation="MRVLKFGGTSVAN', 'AERFLRVADILESNAR"']
        assert parse_qualifiers(lines) == {
            "translation": "MRVLKFGGTSVANAERFLRVADILESNAR"
        }

    def test_repeated_key_keeps_first(self):
        # /db_xref repeats legitimately. The dict keeps the first; the raw
        # block is preserved separately by the caller, so nothing is lost.
        lines = ['/db_xref="GeneID:1"', '/db_xref="ASAP:2"']
        assert parse_qualifiers(lines) == {"db_xref": "GeneID:1"}

    def test_ignores_malformed_line(self):
        # A line that is not a qualifier is skipped, not raised -- the same
        # posture parse_gff_attributes documents.
        assert parse_qualifiers(["junk", '/gene="thrA"']) == {"gene": "thrA"}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_parse.py -k Qualifiers -v
```

Expected: FAIL — `ImportError: cannot import name 'parse_qualifiers'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/app/pipelines/genbank_parse.py`:

```python
def parse_qualifiers(lines: list[str]) -> dict[str, str]:
    """The `/key="value"` block beneath a feature's location.

    Values wrap across lines; a continuation is any line not starting with
    `/`. Malformed lines are skipped rather than raised, the posture
    `parse_gff_attributes` documents for the same kind of data.

    A repeated key keeps the first occurrence. `/db_xref` legitimately
    repeats, but the caller preserves the raw block separately, so the
    dropped values remain visible in the feature's attributes column.
    """
    out: dict[str, str] = {}
    key: str | None = None
    parts: list[str] = []

    def flush() -> None:
        if key is None:
            return
        value = "".join(parts) if key == "translation" else " ".join(parts)
        out.setdefault(key, value.strip().strip('"'))

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("/"):
            flush()
            body = line[1:]
            if "=" in body:
                k, _, v = body.partition("=")
                key, parts = k.strip(), [v.strip()]
            else:
                # A valueless qualifier such as /pseudo. Its presence is the
                # information, so the key is stored with an empty value.
                key, parts = body.strip(), [""]
        elif key is not None:
            parts.append(line)
        # A continuation with no open key is malformed; skipped silently.

    flush()
    return out
```

**Note on `translation`:** every other wrapped qualifier breaks on word boundaries and rejoins with a space. A protein sequence breaks mid-token, so joining with a space would corrupt it. This is a real special case in the format, not an optimization.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_parse.py -v
```

Expected: PASS, 21 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/genbank_parse.py backend/tests/pipelines/test_genbank_parse.py
git commit -m "feat(pipelines): parse GenBank qualifier blocks with wrapped values"
```

---

### Task 4: Turn features into Feature rows

**Files:**
- Modify: `backend/app/pipelines/genbank_parse.py`
- Modify: `backend/tests/pipelines/test_genbank_parse.py`

This produces the parent + segment-children row shape from the spec, and assigns synthetic IDs.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_genbank_parse.py` (add `iter_features` to the imports):

```python
FEATURE_LINES = [
    "     gene            337..2799",
    '                     /gene="thrA"',
    '                     /locus_tag="b0002"',
    "     CDS             join(complement(337..600),700..2799)",
    '                     /gene="thrA"',
    '                     /product="aspartokinase"',
]


class TestIterFeatures:
    def test_simple_feature_is_one_row(self):
        rows = list(iter_features(FEATURE_LINES[:3], accession="NC_1"))
        assert len(rows) == 1
        row = rows[0]
        assert row.contig == "NC_1"
        assert (row.start, row.end) == (337, 2799)
        assert row.type == "gene"
        assert row.name == "thrA"
        assert row.parent is None
        assert row.feature_id == "gb:NC_1:0"

    def test_join_emits_parent_then_segments(self):
        rows = list(iter_features(FEATURE_LINES[3:], accession="NC_1"))
        assert len(rows) == 3

        parent, seg1, seg2 = rows
        # The parent spans the outer bounds, honestly -- the children below
        # state the real extent, the same way a GFF3 gene spans its introns.
        assert (parent.start, parent.end) == (337, 2799)
        assert parent.type == "CDS"
        assert parent.parent is None
        assert parent.feature_id == "gb:NC_1:0"

        assert (seg1.start, seg1.end) == (337, 600)
        assert (seg2.start, seg2.end) == (700, 2799)
        assert seg1.type == "CDS_segment"
        assert seg1.parent == "gb:NC_1:0"
        assert seg2.parent == "gb:NC_1:0"
        assert seg1.feature_id == "gb:NC_1:0:seg1"
        assert seg2.feature_id == "gb:NC_1:0:seg2"

    def test_strand_from_complement(self):
        rows = list(iter_features(FEATURE_LINES[3:], accession="NC_1"))
        assert all(r.strand == "-" for r in rows)

    def test_ids_are_unique_across_features(self):
        rows = list(iter_features(FEATURE_LINES, accession="NC_1"))
        ids = [r.feature_id for r in rows]
        assert len(ids) == len(set(ids))

    def test_name_falls_back_through_qualifiers(self):
        # /gene wins; then /locus_tag; then /product.
        lines = ["     CDS             1..9", '                     /locus_tag="b1"']
        assert list(iter_features(lines, accession="X"))[0].name == "b1"

        lines = ["     CDS             1..9", '                     /product="widget"']
        assert list(iter_features(lines, accession="X"))[0].name == "widget"

    def test_attributes_preserve_every_qualifier(self):
        # The issue's constraint: a qualifier nothing promotes to a column
        # must still survive. /product is not a column, so it has to be here.
        row = list(iter_features(FEATURE_LINES[3:], accession="NC_1"))[0]
        assert "product=aspartokinase" in row.attributes
        assert "gene=thrA" in row.attributes

    def test_score_is_always_none(self):
        # GenBank has no score column. None, not 0.0 -- the reasoning
        # annotation_parse._score documents.
        row = list(iter_features(FEATURE_LINES[:3], accession="NC_1"))[0]
        assert row.score is None

    def test_malformed_location_is_skipped(self):
        lines = ["     CDS             not-a-location", '                     /gene="x"']
        assert list(iter_features(lines, accession="X")) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_parse.py -k IterFeatures -v
```

Expected: FAIL — `ImportError: cannot import name 'iter_features'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/app/pipelines/genbank_parse.py` (and add `from urllib.parse import quote` plus `from app.pipelines.annotation_parse import Feature` to the imports at the top):

```python
# The feature key starts at column 6 and the location at column 22. A line
# with content before column 6 is a section keyword, not a feature.
_KEY_COLUMN = 5
_LOCATION_COLUMN = 21

# RNA feature types whose kind is itself the useful biotype -- GenBank has no
# /biotype qualifier, and "tRNA" is exactly what a biotype filter wants.
_RNA_TYPES = frozenset(
    {"tRNA", "rRNA", "ncRNA", "mRNA", "misc_RNA", "tmRNA", "precursor_RNA"}
)


def _serialize(qualifiers: dict[str, str]) -> str:
    """Qualifiers as a GFF3-style `key=value;` string.

    Percent-encoded exactly as GFF3 column 9 is, so the existing detail-row
    renderer and `parse_gff_attributes` read a GenBank feature's attributes
    without knowing it came from GenBank. This is what preserves qualifiers
    that no column promotes.
    """
    return ";".join(
        f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in qualifiers.items()
    )


def iter_features(lines, *, accession: str):
    """Every feature in one record's FEATURES block, as Feature rows.

    A multi-segment location yields a parent row spanning the outer bounds
    followed by one child row per segment, so nothing downstream ever sees a
    single interval that covers a gap the feature does not occupy (#294).

    Feature IDs are synthetic and positional. GenBank features have no
    identifier, and /locus_tag is shared between a gene and its CDS -- using
    it would repeat the collision `parse_gtf_line` documents.
    """
    index = 0
    key: str | None = None
    location_parts: list[str] = []
    qualifier_lines: list[str] = []

    def build():
        nonlocal index
        if key is None:
            return []
        location = parse_location("".join(location_parts))
        if location is None:
            # Counted as malformed by the caller; one bad location must not
            # cost the rest of the record.
            index += 1
            return []

        quals = parse_qualifiers(qualifier_lines)
        feature_id = f"gb:{accession}:{index}"
        index += 1

        name = quals.get("gene") or quals.get("locus_tag") or quals.get("product")
        biotype = key if key in _RNA_TYPES else quals.get("mol_type")
        attributes = _serialize(quals)
        starts = [s for s, _ in location.segments]
        ends = [e for _, e in location.segments]

        parent = Feature(
            contig=accession,
            start=min(starts),
            end=max(ends),
            type=key,
            strand=location.strand,
            score=None,
            name=name,
            feature_id=feature_id,
            parent=None,
            biotype=biotype,
            attributes=attributes,
        )
        if len(location.segments) == 1:
            return [parent]

        rows = [parent]
        for n, (start, end) in enumerate(location.segments, start=1):
            rows.append(
                Feature(
                    contig=accession,
                    start=start,
                    end=end,
                    type=f"{key}_segment",
                    strand=location.strand,
                    score=None,
                    name=name,
                    feature_id=f"{feature_id}:seg{n}",
                    parent=feature_id,
                    biotype=biotype,
                    attributes=attributes,
                )
            )
        return rows

    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        stripped = line.strip()

        # A new feature key: content at column 6 that is not a qualifier.
        if (
            len(line) > _KEY_COLUMN
            and line[:_KEY_COLUMN].isspace()
            and not stripped.startswith("/")
            and not line[_LOCATION_COLUMN:_LOCATION_COLUMN + 1].isspace()
            and line[_KEY_COLUMN:_LOCATION_COLUMN].strip()
        ):
            yield from build()
            parts = stripped.split(None, 1)
            key = parts[0]
            location_parts = [parts[1].strip()] if len(parts) > 1 else []
            qualifier_lines = []
            continue

        if stripped.startswith("/"):
            qualifier_lines.append(stripped)
        elif qualifier_lines:
            # Continuation of a wrapped qualifier value.
            qualifier_lines.append(stripped)
        elif key is not None:
            # Continuation of a wrapped location.
            location_parts.append(stripped)

    yield from build()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_parse.py -v
```

Expected: PASS, 29 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/genbank_parse.py backend/tests/pipelines/test_genbank_parse.py
git commit -m "feat(pipelines): emit GenBank features as parent rows plus segment children"
```

---

### Task 5: Stream records without loading sequence

**Files:**
- Create: `backend/app/pipelines/genbank_reader.py`
- Create: `backend/tests/pipelines/test_genbank_reader.py`
- Create: `backend/tests/fixtures/genbank/two_records.gbff`

- [ ] **Step 1: Create the fixture**

Create `backend/tests/fixtures/genbank/two_records.gbff`:

```
LOCUS       NC_000001               2000 bp    DNA     circular CON 09-MAR-2022
ACCESSION   NC_000001
VERSION     NC_000001.3
SOURCE      Escherichia coli str. K-12 substr. MG1655
FEATURES             Location/Qualifiers
     source          1..2000
                     /organism="Escherichia coli"
                     /mol_type="genomic DNA"
     gene            100..400
                     /gene="thrA"
                     /locus_tag="b0001"
     CDS             join(100..200,300..400)
                     /gene="thrA"
                     /product="aspartokinase"
ORIGIN
        1 agcttttcat tctgactgca acgggcaata tgtctctgtg tggattaaaa aaagagtgtc
       61 tgatagcagc ttctgaactg gttacctgcc gtgagtaaat taaaatttta ttgacttagg
//
LOCUS       NC_000002                900 bp    DNA     linear   CON 09-MAR-2022
ACCESSION   NC_000002
VERSION     NC_000002.1
FEATURES             Location/Qualifiers
     gene            complement(50..250)
                     /gene="dnaK"
     tRNA            <1..72
                     /product="tRNA-Ala"
//
```

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/pipelines/test_genbank_reader.py`:

```python
"""Streaming GenBank records off disk.

The property under test that is not visible in the parser: a record's ORIGIN
block is stepped over line by line and never accumulated, so a file whose
bulk is sequence costs no more memory than one without it.
"""

from pathlib import Path

from app.pipelines.genbank_reader import iter_records

FIXTURES = Path(__file__).parent.parent / "fixtures" / "genbank"


class TestIterRecords:
    def test_splits_on_record_terminator(self):
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert len(records) == 2

    def test_accession_prefers_version(self):
        # VERSION, not ACCESSION or LOCUS: the versioned accession is what
        # NCBI's paired FASTA uses in its deflines, so the two agree.
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert records[0].accession == "NC_000001.3"
        assert records[1].accession == "NC_000002.1"

    def test_length_from_locus_line(self):
        # This is what lets coverage work with no paired reference.
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert records[0].length == 2000
        assert records[1].length == 900

    def test_reports_sequence_presence_per_record(self):
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert records[0].has_sequence is True
        assert records[1].has_sequence is False

    def test_origin_lines_are_not_retained(self):
        # The ORIGIN block must never reach feature_lines -- if it did, a
        # 300 MB record would cost 300 MB of RSS.
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        joined = "\n".join(records[0].feature_lines)
        assert "agcttttcat" not in joined

    def test_feature_lines_exclude_the_features_header(self):
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert not records[0].feature_lines[0].startswith("FEATURES")
        assert "source" in records[0].feature_lines[0]

    def test_source_is_captured(self):
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert "Escherichia coli" in records[0].source

    def test_gzipped_file_reads_identically(self, tmp_path):
        import gzip
        raw = (FIXTURES / "two_records.gbff").read_bytes()
        gz = tmp_path / "two_records.gbff.gz"
        gz.write_bytes(gzip.compress(raw))
        assert len(list(iter_records(gz))) == 2

    def test_truncated_final_record_is_still_emitted(self, tmp_path):
        # Downloads get truncated. Emit what was parsed rather than losing
        # the whole record for a missing terminator.
        text = (FIXTURES / "two_records.gbff").read_text()
        truncated = text[: text.rindex("//")]
        path = tmp_path / "truncated.gbff"
        path.write_text(truncated)
        assert len(list(iter_records(path))) == 2

    def test_record_without_features_block(self, tmp_path):
        # Valid GenBank. Contributes a contig length and zero features.
        path = tmp_path / "nofeat.gbff"
        path.write_text(
            "LOCUS       NC_9    500 bp    DNA     linear   CON 09-MAR-2022\n"
            "VERSION     NC_9.1\n"
            "//\n"
        )
        records = list(iter_records(path))
        assert len(records) == 1
        assert records[0].length == 500
        assert records[0].feature_lines == []
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_reader.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.genbank_reader'`.

- [ ] **Step 4: Write the implementation**

Create `backend/app/pipelines/genbank_reader.py`:

```python
"""Streaming reader for GenBank flat files.

The piece GFF did not need. A GFF feature is one line, so the handler can
loop over lines directly; a GenBank feature spans a location that may wrap
and a qualifier block that may wrap again, so features have to be grouped
into records before they can be parsed.

What this module guarantees is memory: a record's ORIGIN block is stepped
over line by line and never accumulated. A .gbff whose bulk is sequence
therefore costs no more than one whose bulk is features, which is what keeps
`build_annotation_db`'s streaming design intact.
"""

import gzip
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GenBankRecord:
    """One record: enough header to name a contig, plus its feature lines."""

    accession: str
    length: int | None = None
    source: str = ""
    has_sequence: bool = False
    feature_lines: list[str] = field(default_factory=list)


def _open_text(path: Path):
    """Gzip-aware line reader.

    Sniffed by magic bytes rather than extension, matching
    `annotation_handlers._open_text`: a file downloaded from NCBI is gzipped
    whether or not whoever renamed it kept the suffix.
    """
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


def iter_records(path: Path):
    """Yield one GenBankRecord at a time.

    Never holds more than one record's feature lines in memory, and never
    holds any sequence at all.
    """
    record: GenBankRecord | None = None
    locus_name = ""
    version = ""
    accession = ""
    in_features = False
    in_origin = False

    def flush():
        """Close the current record, naming its contig.

        VERSION, then ACCESSION, then the LOCUS name. The versioned accession
        is what NCBI's paired FASTA uses in its deflines, so a GenBank and its
        sibling FASTA agree on contig names -- which they must, because contig
        lengths may arrive from a reference's facts and are matched by name.
        """
        nonlocal record
        if record is None:
            return None
        record.accession = version or accession or locus_name or "unknown"
        out = record
        record = None
        return out

    with _open_text(path) as fh:
        for raw in fh:
            line = raw.rstrip("\n")

            if line.startswith("LOCUS"):
                previous = flush()
                if previous is not None:
                    yield previous
                in_features = in_origin = False
                locus_name = version = accession = ""
                record = GenBankRecord(accession="")
                parts = line.split()
                if len(parts) > 1:
                    locus_name = parts[1]
                # `LOCUS name 4641652 bp ...` -- the token before `bp`.
                for i, token in enumerate(parts):
                    if token == "bp" and i > 0 and parts[i - 1].isdigit():
                        record.length = int(parts[i - 1])
                        break
                continue

            if record is None:
                continue

            if line.startswith("//"):
                out = flush()
                if out is not None:
                    yield out
                in_features = in_origin = False
                continue

            if line.startswith("ORIGIN"):
                in_features = False
                in_origin = True
                continue

            if in_origin:
                # Stepped over, never stored. Any non-blank content here is
                # sequence, which is the whole point of this branch.
                if line.strip():
                    record.has_sequence = True
                continue

            if line.startswith("VERSION"):
                parts = line.split()
                if len(parts) > 1:
                    version = parts[1]
                continue

            if line.startswith("ACCESSION"):
                parts = line.split()
                if len(parts) > 1:
                    accession = parts[1]
                continue

            if line.startswith("SOURCE"):
                record.source = line[len("SOURCE"):].strip()
                continue

            if line.startswith("FEATURES"):
                in_features = True
                continue

            # A keyword in column 1 ends the feature block.
            if line[:1].strip():
                in_features = False
                continue

            if in_features:
                record.feature_lines.append(line)

    out = flush()
    if out is not None:
        yield out
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_reader.py -v
```

Expected: PASS, 10 tests.

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/genbank_reader.py backend/tests/pipelines/test_genbank_reader.py backend/tests/fixtures/genbank/
git commit -m "feat(pipelines): stream GenBank records without loading sequence"
```

---

### Task 6: Detect GenBank files

**Files:**
- Modify: `backend/app/storage/detect.py:28-49` (`EXTENSION_MAP`), `:197-213` (`_sniff_text`)
- Modify: `backend/app/storage/compress.py:37-49` (`COMPRESSIBLE_KINDS`)
- Test: `backend/tests/storage/test_detect.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/storage/test_detect.py`:

```python
class TestGenBankDetection:
    def test_extension_variants(self):
        for name in ("a.gb", "a.gbk", "a.gbff", "a.genbank"):
            assert detect_from_extension(name) is FormatKind.GENBANK

    def test_compressed_extension(self):
        # The compression suffix is skipped before the kind is read.
        assert detect_from_extension("a.gbff.gz") is FormatKind.GENBANK

    def test_locus_line_is_recognized(self, tmp_path):
        path = tmp_path / "x.txt"
        path.write_text(
            "LOCUS       NC_000913    4641652 bp    DNA     circular CON\n"
            "VERSION     NC_000913.3\n"
        )
        # Named .txt on purpose: content alone must carry the answer.
        assert detect(path).kind is FormatKind.GENBANK

    def test_locus_as_a_bare_word_is_not_genbank(self, tmp_path):
        # "LOCUS" must be followed by whitespace and a name. A prose file
        # opening with the word alone is not a GenBank record.
        path = tmp_path / "x.txt"
        path.write_text("LOCUSTS are a kind of insect\n")
        assert detect(path).kind is not FormatKind.GENBANK

    def test_gff_still_detects_as_gff(self, tmp_path):
        # The regression direction. Asserting only that GenBank works would
        # pass whether or not this change broke the other formats.
        path = tmp_path / "x.gff"
        path.write_text("##gff-version 3\nchr1\t.\tgene\t1\t100\t.\t+\t.\tID=g1\n")
        assert detect(path).kind is FormatKind.GFF

    def test_bed_still_detects_as_bed(self, tmp_path):
        path = tmp_path / "x.bed"
        path.write_text("chr1\t100\t200\tfeature1\n")
        assert detect(path).kind is FormatKind.BED

    def test_fasta_still_detects_as_fasta(self, tmp_path):
        path = tmp_path / "x.fa"
        path.write_text(">chr1\nACGTACGT\n")
        assert detect(path).kind is FormatKind.FASTA
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/storage/test_detect.py -k GenBank -v
```

Expected: FAIL — extension lookups return `None`.

- [ ] **Step 3: Add the extensions**

In `backend/app/storage/detect.py`, add to `EXTENSION_MAP` after the `"gtf"` entry:

```python
    "gb": FormatKind.GENBANK,
    "gbk": FormatKind.GENBANK,
    "gbff": FormatKind.GENBANK,
    "genbank": FormatKind.GENBANK,
```

- [ ] **Step 4: Add the content sniff**

In `_sniff_text`, insert immediately after the `first.startswith(">")` FASTA check and **before** the FASTQ check:

```python
    # A GenBank record must open with LOCUS in column 1 -- the format's own
    # spec fixes that, which makes this a real positive signal rather than a
    # shape heuristic like the tabular sniffing below. The trailing
    # whitespace check matters: a prose file starting "LOCUSTS ..." is not a
    # GenBank record.
    if first.startswith("LOCUS") and first[5:6].isspace():
        return FormatKind.GENBANK
```

Placement is deliberate: it must precede `_sniff_tabular`. A GenBank header is space-padded rather than tab-separated so it would not reach the tabular branch anyway, but relying on that coincidence is the trap `_looks_like_gfa`'s docstring warns about.

GenBank is **not** added to the `ext_kind in (BED, GTF, GFF)` override near the end of `detect()`. That override compensates for weak sniffing; GenBank's signal is strong, so magic wins outright as it does for VCF and FASTA.

- [ ] **Step 5: Add to COMPRESSIBLE_KINDS**

In `backend/app/storage/compress.py`, add to the `COMPRESSIBLE_KINDS` frozenset after `FormatKind.GTF`:

```python
        FormatKind.GENBANK,
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/storage/test_detect.py tests/storage/test_compress.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/storage/detect.py backend/app/storage/compress.py backend/tests/storage/test_detect.py
git commit -m "feat(formats): detect GenBank by extension and LOCUS header"
```

---

### Task 7: Wire GenBank into the annotation handler

**Files:**
- Modify: `backend/app/queue/annotation_handlers.py:30-34` (`_PARSERS`), `:85-129` (`_rows`)
- Create: `backend/tests/pipelines/test_genbank_stats.py`

This generalizes `_PARSERS` from line functions to row iterators, and carries the coverage subtlety the spec flags.

- [ ] **Step 1: Write the failing test for coverage correctness**

Create `backend/tests/pipelines/test_genbank_stats.py`:

```python
"""The failure a unit test of the parser cannot catch.

`_ContigCoverage` merges overlapping intervals. A join feature's parent row
spans its introns, so feeding that parent to the coverage accumulator after
its segments fills the intron back in -- silently, with nothing raising and
no test failing on its own. The number is just wrong.
"""

from app.pipelines.annotation_stats import AnnotationAccumulator
from app.pipelines.genbank_parse import iter_features

JOIN_FEATURE = [
    "     CDS             join(100..200,300..400)",
    '                     /gene="thrA"',
]


def test_intron_is_not_counted_as_covered():
    rows = list(iter_features(JOIN_FEATURE, accession="NC_1"))
    parent, *segments = rows

    acc = AnnotationAccumulator(contig_lengths={"NC_1": 1000})
    for row in segments:
        acc.add(row)
    facts = acc.finish()

    # 101 bases + 101 bases. NOT 301, which is what the parent's outer span
    # would give and which would claim the 99-base intron as covered.
    assert facts["annotation_per_contig"][0]["covered_bases"] == 202


def test_feeding_the_parent_would_overcount():
    # Pins the reason the handler excludes segment-bearing parents from
    # coverage. If this ever stops being true, the exclusion can go.
    rows = list(iter_features(JOIN_FEATURE, accession="NC_1"))

    acc = AnnotationAccumulator(contig_lengths={"NC_1": 1000})
    for row in rows:  # parent included -- the bug
        acc.add(row)

    assert acc.finish()["annotation_per_contig"][0]["covered_bases"] == 301
```

- [ ] **Step 2: Run the tests to verify the first fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_stats.py -v
```

Expected: both PASS — they test existing components. If `test_intron_is_not_counted_as_covered` fails, the parser from Task 4 is wrong and must be fixed before continuing.

- [ ] **Step 3: Generalize `_PARSERS` to row iterators**

In `backend/app/queue/annotation_handlers.py`, replace the `_PARSERS` dict (lines 30–34) with:

```python
def _line_rows(parse_line, source: Path, ctx, acc, header: list[str], fmt: str):
    """Row iterator for the line-oriented formats.

    GFF, GTF and BED are one feature per line, so this is the original loop
    unchanged -- it lives behind the same iterator interface GenBank needs so
    the handler has one code path.
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


def _genbank_rows(source: Path, ctx, acc, facts: dict):
    """Row iterator for GenBank.

    Two things differ from the line formats. Contig lengths come from each
    record's own LOCUS line rather than the payload, so coverage works with
    no paired reference. And a segment-bearing parent is counted but kept out
    of the coverage accumulator: `_ContigCoverage` merges intervals, so its
    outer span would fill in the introns its own children carve out.
    """
    lengths: dict[str, int] = {}
    records = 0
    has_sequence = False
    names: list[str] = []

    for record in genbank_reader.iter_records(source):
        ctx.check_cancel()
        records += 1
        has_sequence = has_sequence or record.has_sequence
        names.append(record.accession)
        if record.length:
            lengths[record.accession] = record.length

        seen_parents: set[str] = set()
        for feature in genbank_parse.iter_features(
            record.feature_lines, accession=record.accession
        ):
            if feature.parent is not None:
                seen_parents.add(feature.parent)
            yield feature

        # Second pass over nothing: the accumulator is fed below, after the
        # parents that own segments are known.
        for feature in genbank_parse.iter_features(
            record.feature_lines, accession=record.accession
        ):
            if feature.feature_id in seen_parents:
                # Counted, but its outer span never reaches coverage.
                acc.add_without_coverage(feature)
            else:
                acc.add(feature)
            if feature.attributes:
                acc.add_attribute_keys(
                    annotation_parse.parse_gff_attributes(feature.attributes).keys()
                )

    facts["genbank_record_count"] = records
    facts["genbank_has_sequence"] = has_sequence
    facts["genbank_locus_names"] = names
    facts["_contig_lengths"] = lengths
```

Add to the imports at the top of the file:

```python
from app.pipelines import genbank_parse, genbank_reader
```

- [ ] **Step 4: Add `add_without_coverage` to the accumulator**

In `backend/app/pipelines/annotation_stats.py`, refactor `add` and add the new method. Replace the body of `add` with a call to a shared helper:

```python
    def add(self, f: Feature) -> None:
        """Count a feature and let it contribute to coverage."""
        self._count(f)
        cov = self._coverage.get(f.contig)
        if cov is None:
            cov = self._coverage[f.contig] = _ContigCoverage()
        cov.add(f.start, f.end)

    def add_without_coverage(self, f: Feature) -> None:
        """Count a feature whose span must not contribute to coverage.

        A GenBank join feature's parent row spans its introns. Its segment
        children carry the real intervals, so feeding the parent here too
        would merge those introns back into the covered total -- silently,
        since `_ContigCoverage` merges rather than raising.
        """
        self._count(f)
        # The contig must still be known even if nothing covers it yet, or a
        # record of only join features would report zero contigs.
        self._coverage.setdefault(f.contig, _ContigCoverage())

    def _count(self, f: Feature) -> None:
        self._total += 1
        if f.parent is None:
            self._top_level += 1
        if f.type:
            self._types[f.type] = self._types.get(f.type, 0) + 1
        if f.biotype:
            self._biotypes[f.biotype] = self._biotypes.get(f.biotype, 0) + 1
        self._per_contig_count[f.contig] = self._per_contig_count.get(f.contig, 0) + 1

        length = f.end - f.start + 1
        for i, edge in enumerate(_LENGTH_BINS):
            if length <= edge:
                self._length_counts[i] += 1
                break
        else:
            self._length_counts[-1] += 1
```

- [ ] **Step 5: Dispatch on format in the handler**

In `run_annotation_stats`, replace the parser lookup (lines 64–67) and the `_rows` definition with:

```python
    fmt = ctx.payload.get("format_kind")
    if fmt not in ("gff", "gtf", "bed", "genbank"):
        raise PermanentError(f"run_annotation_stats cannot read format {fmt!r}")
```

and replace the `rows=_rows()` argument at the `build_annotation_db` call with a dispatch:

```python
    extra_facts: dict = {}
    if fmt == "genbank":
        rows = _genbank_rows(source, ctx, acc, extra_facts)
    else:
        parse_line = {
            "gff": annotation_parse.parse_gff_line,
            "gtf": annotation_parse.parse_gtf_line,
            "bed": annotation_parse.parse_bed_line,
        }[fmt]
        rows = _line_rows(parse_line, source, ctx, acc, header, fmt)

    total = annotation_db.build_annotation_db(rows=rows, db_path=tmp_db)
```

Then, where `contig_lengths` is built (lines 75–78), prefer GenBank's own lengths:

```python
    # GenBank states each contig's length on its own LOCUS line, so it needs
    # no reference. The payload's lengths remain the source for GFF/GTF/BED.
    parsed_lengths = extra_facts.pop("_contig_lengths", None)
    if parsed_lengths:
        acc._contig_lengths = parsed_lengths
```

and merge the new facts into the returned dict:

```python
    facts = {
        "annotation_stats_status": "ok",
        **acc.finish(),
        **annotation_stats.parse_header_directives(header),
        **extra_facts,
    }
```

- [ ] **Step 6: Run the full annotation test suite**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_stats.py tests/pipelines/test_annotation_parse.py tests/pipelines/test_genbank_stats.py -v
```

Expected: PASS. The existing GFF/GTF/BED tests must be unaffected — that is the regression signal for the `_PARSERS` refactor.

- [ ] **Step 7: Commit**

```bash
git add backend/app/queue/annotation_handlers.py backend/app/pipelines/annotation_stats.py backend/tests/pipelines/test_genbank_stats.py
git commit -m "feat(annotation): build feature tables and stats from GenBank files"
```

---

### Task 8: Make GenBank eligible for annotation Results

**Files:**
- Modify: `backend/app/services/pipeline_service.py:2295` (`_ANNOTATION_STATS_FORMATS`), `:3017` (`_is_annotation`)
- Test: `backend/tests/pipelines/test_annotation_stats_launch.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_annotation_stats_launch.py`:

```python
def test_genbank_is_annotation_stats_callable():
    from app.services.pipeline_service import _check_annotation_stats_callable

    obj = SimpleNamespace(
        id="x",
        name="ecoli.gbff",
        status=ObjectStatus.READY,
        format=SimpleNamespace(kind=FormatKind.GENBANK),
    )
    # Does not raise.
    _check_annotation_stats_callable(obj)


def test_genbank_counts_as_an_annotation():
    from app.services.pipeline_service import _is_annotation

    obj = SimpleNamespace(
        status=ObjectStatus.READY,
        format=SimpleNamespace(kind=FormatKind.GENBANK),
    )
    assert _is_annotation(obj) is True
```

Match the import style already at the top of that test file; add `SimpleNamespace` from `types` if it is not already imported.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_stats_launch.py -k genbank -v
```

Expected: FAIL — `ValidationError: ... is genbank, not an annotation file`.

- [ ] **Step 3: Add GENBANK to both predicates**

In `backend/app/services/pipeline_service.py`, update `_ANNOTATION_STATS_FORMATS`:

```python
_ANNOTATION_STATS_FORMATS = (
    FormatKind.GFF,
    FormatKind.GTF,
    FormatKind.BED,
    FormatKind.GENBANK,
)
```

and its error message in `_check_annotation_stats_callable`:

```python
            f"{obj.name!r} is {obj.format.kind.value}, not an annotation file "
            f"(GFF, GTF, BED, or GenBank)",
```

and `_is_annotation`:

```python
    return obj.format.kind in (
        FormatKind.GFF,
        FormatKind.GTF,
        FormatKind.GENBANK,
    )
```

**Do not touch `ANNOTATION_KINDS`** at line 2762. It stays `{FormatKind.GFF}` because `bcftools csq` reads GFF3 only — adding GenBank would queue a job that dies in the worker, which is exactly what that constant's comment documents about BED.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_annotation_stats_launch.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/pipelines/test_annotation_stats_launch.py
git commit -m "feat(annotation): make GenBank files eligible for annotation results"
```

---

### Task 9: Show GenBank in the UI

**Files:**
- Modify: `frontend/src/lib/format.ts:73`
- Modify: `frontend/src/icons/getFileIcon.ts:11-29`

There is no headless component-testing setup in this repo (CLAUDE.md), so verification here is the browser.

- [ ] **Step 1: Add the format label**

In `frontend/src/lib/format.ts`, add beside the `gff` entry:

```typescript
  genbank: "GenBank",
```

- [ ] **Step 2: Add the icon mapping**

In `frontend/src/icons/getFileIcon.ts`, map `genbank` to the existing GFF icon — a GenBank file is an annotation, and drawing it with the annotation icon is correct. Add to the icon lookup object beside `gff`:

```typescript
  genbank: gff,
```

- [ ] **Step 3: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

Expected: UI on 5273, API on 8100. The main stack on 5173 keeps running main.

- [ ] **Step 4: Verify in the browser**

Upload `backend/tests/fixtures/genbank/two_records.gbff` at http://localhost:5273 and confirm: the explorer shows "GenBank" as the format with the annotation icon; the Results tab offers annotation results; the feature table lists features with `thrA` expandable to two `CDS_segment` children; the per-contig chart shows two contigs with lengths 2000 and 900.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/format.ts frontend/src/icons/getFileIcon.ts
git commit -m "feat(ui): label and icon GenBank files as annotations"
```

---

### Task 10: Verify against a real NCBI file

**Files:**
- Create: `backend/tests/fixtures/genbank/ecoli_slice.gbff`
- Create: `backend/tests/fixtures/genbank/two_records.gbff.gz`
- Modify: `backend/tests/pipelines/test_genbank_reader.py`

Hand-built fixtures prove the parser handles what its author imagined. CLAUDE.md's Actions-tab lesson is that this is not the same as handling real files.

- [ ] **Step 1: Fetch a real record**

```bash
curl -s "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=NC_000913.3&rettype=gbwithparts&retmode=text&seq_start=1&seq_stop=40000" -o backend/tests/fixtures/genbank/ecoli_slice.gbff
```

Verify it is a real record and not an error page:

```bash
head -1 backend/tests/fixtures/genbank/ecoli_slice.gbff
```

Expected: a line beginning `LOCUS       NC_000913`.

- [ ] **Step 2: Create the gzipped fixture**

```bash
gzip -kc backend/tests/fixtures/genbank/two_records.gbff > backend/tests/fixtures/genbank/two_records.gbff.gz
```

- [ ] **Step 3: Write the real-file test**

Append to `backend/tests/pipelines/test_genbank_reader.py`:

```python
class TestRealNcbiFile:
    """Against a real NCBI record, not a hand-built one.

    A fixture written by the parser's own author tends to look the way the
    parser expects. This one was not.
    """

    def test_parses_without_error(self):
        records = list(iter_records(FIXTURES / "ecoli_slice.gbff"))
        assert len(records) == 1
        assert records[0].accession.startswith("NC_000913")

    def test_finds_features(self):
        from app.pipelines.genbank_parse import iter_features

        record = next(iter(iter_records(FIXTURES / "ecoli_slice.gbff")))
        rows = list(iter_features(record.feature_lines, accession=record.accession))
        # The first 40kb of K-12 holds dozens of genes and CDSs.
        assert len(rows) > 20
        assert any(r.type == "CDS" for r in rows)
        assert any(r.name == "thrA" for r in rows)

    def test_every_feature_id_is_unique(self):
        # The property the whole parent/child scheme rests on. A collision
        # would attach children to the wrong parent silently.
        from app.pipelines.genbank_parse import iter_features

        record = next(iter(iter_records(FIXTURES / "ecoli_slice.gbff")))
        ids = [
            r.feature_id
            for r in iter_features(record.feature_lines, accession=record.accession)
        ]
        assert len(ids) == len(set(ids))

    def test_gzipped_fixture_matches_plain(self):
        plain = list(iter_records(FIXTURES / "two_records.gbff"))
        gzipped = list(iter_records(FIXTURES / "two_records.gbff.gz"))
        assert [r.accession for r in plain] == [r.accession for r in gzipped]
        assert [r.length for r in plain] == [r.length for r in gzipped]
```

- [ ] **Step 4: Run the tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_reader.py -v
```

Expected: PASS. A failure here is a real parser bug the hand-built fixtures missed — fix the parser, not the test.

- [ ] **Step 5: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. Read the count, not just the exit code (CLAUDE.md).

- [ ] **Step 6: Commit**

```bash
git add backend/tests/fixtures/genbank/ backend/tests/pipelines/test_genbank_reader.py
git commit -m "test(pipelines): verify GenBank parsing against a real NCBI record"
```

---

### Task 11: Close out and open the PR

- [ ] **Step 1: Check a real object in the running stack**

CLAUDE.md: check a rule against the real database, not only its unit tests.

```bash
docker compose -p bioflow-wt exec api python -c "
from pathlib import Path
from app.pipelines.genbank_reader import iter_records
from app.pipelines.genbank_parse import iter_features
p = Path('tests/fixtures/genbank/ecoli_slice.gbff')
for r in iter_records(p):
    rows = list(iter_features(r.feature_lines, accession=r.accession))
    print(r.accession, r.length, r.has_sequence, len(rows))
"
```

Expected: one line naming `NC_000913.3`, a length, `True`, and a feature count above 20.

- [ ] **Step 2: Tear down the worktree stack**

```bash
./ops/worktree-up.sh --down
```

A stack brought up for testing is yours to bring back down (CLAUDE.md) — leftover stacks corrupt other test runs through the shared `biopipe_test` database.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --title "feat(formats): add GenBank annotation support" --body "$(cat <<'EOF'
Adds GenBank flat files (`.gb`, `.gbk`, `.gbff`) as a first-class annotation
format, flowing into the annotation Results view built by #257.

## Why

GenBank annotations previously arrived as unknown or text content: no Results
view, absent from annotation pickers, uncompressed at ingest.

## Approach

Two new pure modules produce the same `Feature` rows `annotation_db` and
`AnnotationAccumulator` already consume, so nothing downstream of the parser
changed. The parser is hand-written rather than Biopython -- `SeqIO.parse`
materializes each record's sequence in memory, which fights the streaming
design `build_annotation_db` exists to preserve.

Two decisions worth review:

- **A `join(...)` location becomes a parent row plus one child row per
  segment**, reusing the hierarchy machinery GFF3 already uses. This satisfies
  the issue's constraint that complex locations must not flatten into false
  single intervals.
- **Segment-bearing parents are counted but excluded from coverage.**
  `_ContigCoverage` merges intervals, so a parent's outer span would fill back
  in the introns its own children carve out -- silently. `test_genbank_stats.py`
  pins both directions.

GenBank supplies its own contig lengths from each `LOCUS` line, so coverage
statistics work on a standalone file with no paired reference.

`ANNOTATION_KINDS` deliberately stays `{FormatKind.GFF}`: `bcftools csq` reads
GFF3 only.

Closes #294

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Label the PR**

`.github/release.yml` categorizes release notes by label, not by the title prefix — an unlabelled PR lands under "Other changes".

```bash
gh pr edit --add-label "type:feature" --add-label "area:pipelines" --add-label "area:frontend"
```

- [ ] **Step 5: Watch CI to completion**

```bash
gh pr checks --watch
```

Poll until every check reports pass or fail, not just until the command returns. If `ruff` fails on import order (`I001`) — which has bitten this repo before on exactly this kind of change — apply the fix ruff suggests, push, and re-poll.

```bash
gh pr view --json mergeable,mergeStateStatus
```

`UNSTABLE` means checks are still running; keep waiting. A real conflict means rebase on `origin/main` and push again.

- [ ] **Step 6: Report the PR URL**

Only once checks are green and `mergeable` is clean. Do not merge — the user reviews and merges (CLAUDE.md).

---

## Self-review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Sequence skipped, `genbank_has_sequence` | 5, 7 |
| Hand-written parser | 2, 3, 4, 5 |
| Complex locations → parent + segments | 2, 4 |
| Synthetic positional IDs | 4, 10 |
| One record = one contig, VERSION-named | 5 |
| Row shape / field mapping | 4 |
| Qualifiers preserved in `attributes` | 4 |
| Coverage correctness | 7 |
| New facts | 7 |
| Detection | 6 |
| Registry updates | 1, 6, 8, 9 |
| Error handling | 2, 3, 5 |
| Testing (3 layers + live check) | 2–7, 10, 11 |

No gaps.

**Type consistency:** `Location(segments, strand, fuzzy)` is defined in Task 2 and used unchanged in 4. `GenBankRecord(accession, length, source, has_sequence, feature_lines)` is defined in Task 5 and used in 7 and 10. `iter_features(lines, *, accession)` and `iter_records(path)` keep the same signatures throughout. `add_without_coverage` is defined in Task 7 step 4 and used in step 3 of the same task.

**Known rough edge:** Task 7's `_genbank_rows` iterates each record's feature lines twice — once to yield rows to the database, once to feed the accumulator after segment-owning parents are known. The lines are already in memory per record, so this is string work rather than a second file read, and it does not defeat the one-pass-over-the-file property. It mirrors the documented double-parse already in `annotation_handlers._rows`. An implementer who finds a single-pass shape that stays readable should take it.
