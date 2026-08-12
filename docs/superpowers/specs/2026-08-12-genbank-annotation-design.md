# GenBank annotation support

Issue: [#294](https://github.com/syntheticgio/bioflow/issues/294)
Follow-up to [#257](https://github.com/syntheticgio/bioflow/issues/257), which
built the annotation Results experience for GFF/GTF/BED and put GenBank
explicitly out of scope.

BioFlow recognizes GFF3, GTF and BED, but a GenBank flat file (`.gb`, `.gbk`,
`.gbff`) arrives as unknown or text content. It cannot use the annotation
Results view, does not appear in annotation pickers, and is not compressed at
ingest. This adds GenBank as a first-class annotation format producing the same
feature rows every downstream consumer already reads.

## What exists today

`annotation_parse.py` holds three pure line functions -- `parse_gff_line`,
`parse_gtf_line`, `parse_bed_line` -- each returning a frozen `Feature` or
`None`. `queue/annotation_handlers.py` selects one from a `_PARSERS` dict keyed
by a format string, then streams rows in a single pass into two consumers:
`annotation_db.build_annotation_db` (SQLite, indexed after bulk insert) and
`annotation_stats.AnnotationAccumulator` (bounded aggregates). The API routes,
`AnnotationResults.tsx`, `AnnotationFeatureTable.tsx` and `AnnotationCharts.tsx`
read only what those two produce.

That downstream stack is format-agnostic. Nothing in it needs to change.

The seam that does not fit is `_PARSERS`. It maps a format to a function taking
one line, and a GenBank feature spans many lines: a location that may wrap, then
a qualifier block that may wrap again. GenBank needs a streaming *record*
reader, not a line function.

Biopython is not a dependency. `pysam` is present; nothing currently parses
GenBank.

## Decisions

### Sequence: skipped, but recorded

A GenBank record's `ORIGIN` block holds the nucleotides, often the bulk of the
file. The parser skips it line by line and never accumulates it, and records
`genbank_has_sequence` when a non-empty `ORIGIN` block was seen.

Extracting that sequence into a derived FASTA usable as an alignment reference
is a real feature the user wants, but it drags in derived objects, provenance
and sidecar roles for something no current workflow asks for. It is deferred to
its own issue, for which `genbank_has_sequence` is the signal that extraction is
possible.

The compensation is better than it sounds: each record's `LOCUS` line states its
own length, so GenBank supplies its own contig lengths and coverage statistics
work on a standalone file with no paired reference. GFF cannot do this today.

### Parser: hand-written, not Biopython

Biopython's `SeqIO.parse` is correct by default on the hard cases and is the
field-standard library, but it materializes each record's full sequence in
memory -- a 300 MB record becomes 300 MB of RSS, which is exactly what the
streaming design of `build_annotation_db` exists to avoid. Reaching past it to
`Bio.GenBank.Scanner` means depending on a semi-internal API. The package also
lands in the API image, the worker image and the launcher bundle.

Against that: GenBank's feature-table grammar is small and has been stable for
decades, and `annotation_parse.py` already establishes the pattern of pure,
individually-testable format functions. A hand-written parser is the one that
looks like the rest of this codebase.

The accepted cost is that qualifier-continuation and location-grammar bugs are
ours to find, which is why the fixture set must include a real NCBI file rather
than only hand-built cases.

### Complex locations: parent row plus segment children

`join(complement(1..100),200..300)` must not become a single `1..300` interval;
that span covers an intron the feature does not occupy. The issue names this
constraint directly.

A feature with a multi-segment location emits one parent row spanning the outer
bounds, plus one child row per segment. This reuses machinery that already
exists for GFF3 gene/transcript/exon hierarchies: the `parent` column,
`ix_features_parent`, `children_of()`, and the table's expand-on-click. The
parent's bounds are outer bounds honestly, the same way a GFF3 gene's bounds
span its introns, because the children state the real extent.

A single-segment location emits one row with no children, so the common case
does not get more complicated.

Rejected: one row per segment with no parent (the feature count then misreports
how many CDS a file has), and a JSON segments column (nothing downstream reads
it -- the locus filter, coverage accumulator and table all query `start`/`end`).

Known consequence: `annotation_top_level_count` counts such a CDS once, and
`annotation_feature_count` counts three rows. This is the same double-count
GFF3 already has for gene/transcript/exon, so it is at least consistent across
formats.

### Feature identity: synthetic positional IDs

GenBank features have no identifier. `/locus_tag`, `/gene` and `/protein_id` are
real biological identifiers but are not guaranteed present, not guaranteed
unique, and a gene and its CDS typically share one `/locus_tag` -- which would
make a feature its own sibling.

IDs are therefore synthetic and positional: `gb:{accession}:{index}` for a
feature, `gb:{accession}:{index}:seg{n}` for a segment. Unique by construction,
stable for a given file, assigned in a single forward pass without holding the
record in memory.

This repo has already made the opposite mistake once. `parse_gtf_line`'s comment
records a bug where reusing `transcript_id` as an exon's `feature_id` made every
exon under a transcript collide. Uniqueness is what the column is for;
human-readability is what `name` is for, and `/locus_tag` still lands there and
in the raw attributes.

### Multi-record files: one record is one contig

A `.gbff` holds many records, each with its own `LOCUS` line, its own coordinate
space starting at 1, and its own feature table. Each becomes one contig; no
coordinate adjustment is needed.

The contig name is `VERSION` (e.g. `NC_000913.3`), falling back to `ACCESSION`,
then to the `LOCUS` name. The versioned accession is what NCBI's paired FASTA
uses in its deflines, so a GenBank and its sibling FASTA agree on contig names
-- which matters because `contig_lengths` may arrive from a reference's facts
and the two must match for coverage to compute. The `LOCUS` name is the
guaranteed last resort but is often truncated or local.

The handler prefers `LOCUS`-parsed lengths and falls back to the payload's.

## Architecture

Two new modules, mirroring the existing split.

**`backend/app/pipelines/genbank_parse.py`** -- pure functions, no I/O, sibling
of `annotation_parse.py`:

- `parse_location(text) -> Location | None` -- the grammar. Returns a strand and
  an ordered list of `(start, end)` segments, plus fuzzy-bound flags.
- `parse_qualifiers(lines) -> dict[str, str]` -- the `/key="value"` block.
- `iter_features(lines, accession, ...) -> Iterator[Feature]` -- walks one
  record's `FEATURES` block, emitting parent-then-segments.

**`backend/app/pipelines/genbank_reader.py`** -- the streaming layer, the piece
GFF did not need. Consumes a text handle and yields `(record_header, feature_lines)`
per record without holding a record in memory, recognizing section keywords in
column 1 (`LOCUS`, `VERSION`, `ACCESSION`, `SOURCE`, `DBLINK`, `FEATURES`,
`ORIGIN`, `//`) and skipping the `ORIGIN` block by line.

**Handler change** -- `_PARSERS` generalizes from "format to line function" to
"format to row iterator over a path". GFF/GTF/BED keep their existing line loop
behind a thin adapter, so one code path still builds the database and the
accumulator.

## Row shape

A `CDS` at `join(complement(1..100),200..300)` in record `NC_000913.3`, seventh
feature in the file:

| contig | start | end | type | strand | feature_id | parent | name |
|---|---|---|---|---|---|---|---|
| NC_000913.3 | 1 | 300 | CDS | - | `gb:NC_000913.3:7` | *null* | thrA |
| NC_000913.3 | 1 | 100 | CDS_segment | - | `gb:NC_000913.3:7:seg1` | `gb:NC_000913.3:7` | thrA |
| NC_000913.3 | 200 | 300 | CDS_segment | - | `gb:NC_000913.3:7:seg2` | `gb:NC_000913.3:7` | thrA |

Field mapping:

- `name` <- `/gene`, else `/locus_tag`, else `/product`
- `biotype` <- `/mol_type`, or the feature type for RNA kinds
- `score` -- always null; GenBank has no score column
- `attributes` <- the full qualifier block re-serialized as `key=value;` pairs,
  percent-encoded as GFF3 column 9 is, so the existing detail-row renderer and
  `parse_gff_attributes` read it unchanged

That last point is what satisfies the issue's "must preserve qualifiers it
cannot promote": every qualifier survives in `attributes` even when no column
promotes it.

## Coverage correctness

The accumulator receives the segment rows, so an intron is never counted as
covered. But `_ContigCoverage` merges overlapping intervals, so feeding the
parent's `1..300` after its two segments would fill the intron back in.

The parser therefore marks segment-bearing parents, and the handler excludes
those parents from the coverage accumulator while still counting them in totals.
This is the subtlest part of the design and carries an explicit test.

## New facts

- `genbank_record_count`
- `genbank_has_sequence`
- `genbank_locus_names`
- `annotation_source` / `genome_build` from `SOURCE` / `DBLINK`, reusing the
  provenance keys `parse_header_directives` already emits

## Detection

`FormatKind.GENBANK` is added to the enum.

Extensions `gb`, `gbk`, `gbff`, `genbank` join `EXTENSION_MAP`. Compressed
variants work through the existing `COMPRESSION_EXTENSIONS` stripping.

Content sniffing keys on the format's own guarantee: a record must begin with
`LOCUS` in column 1. `_sniff_text` gains that check, placed *before* the tabular
heuristic. A GenBank header is space-padded rather than tab-separated so it
would not reach `_sniff_tabular` anyway, but relying on that coincidence is the
trap `_looks_like_gfa`'s docstring warns about, so the check is explicit.

Because the signal is strong, GenBank is treated like VCF/FASTA/SAM -- magic
wins outright. It does **not** join the `ext_kind in (BED, GTF, GFF)` override
at the end of `detect()`, which exists to compensate for weak sniffing GenBank
does not have.

## Registry updates

The hand-maintained registries CLAUDE.md flags, each of which silently skips an
enum member it has no entry for:

- `COMPRESSIBLE_KINDS` += GENBANK -- plain text, and sequence compresses well
- `FORMAT_FIELDS[GENBANK] = INTERVAL_FIELDS`, matching GFF/GTF
- `_ANNOTATION_STATS_FORMATS` += GENBANK, and its error message updated
- `_is_annotation()` += GENBANK, so it appears in annotation pickers
- `getFileIcon.ts` and the frontend format-label map, reusing the annotation icon

`ANNOTATION_KINDS` stays `{FormatKind.GFF}`. `bcftools csq` reads GFF3 only;
adding GenBank would queue a job that dies in the worker, which is what that
constant's comment already documents about BED.

GENBANK must be placed in exactly one of `FORMAT_COMMON_ONLY` /
`FORMAT_DERIVED_ROLES`. Their exhaustiveness tests fail until it is; treat that
failure as the checklist.

## Error handling

Match the existing posture: malformed input is counted and skipped, not raised.
A half-garbage file should report that in its provenance line rather than fail a
job that could have summarized the good half.

- Unparseable location -- count the feature malformed, skip it, continue the
  record. An unrecognized location grammar must never abort a file.
- Malformed qualifier -- skip the qualifier, keep the feature, as
  `parse_gff_attributes` does.
- Truncated final record with no `//` -- emit what was parsed and count it.
  Downloads get truncated.
- Record with no `FEATURES` block -- valid GenBank; contributes its contig
  length and zero features.

`annotation_malformed_lines` surfaces the count.

The one case that fails loudly is a non-GenBank file reaching the handler:
`PermanentError`, as the handler already does for unknown formats.

## Testing

**Pure functions.** Table-driven tests per location grammar: plain,
`complement`, `join`, nested `join(complement(...))`, `order`, fuzzy `<1..100`
and `100..>200`, single position `467`, between-position `102^103`, and
malformed input returning `None`. Same for qualifiers: wrapped values, valueless
(`/pseudo`), embedded quotes, multi-line `/note`.

**Real files.** A small multi-record `.gbff`, the same file gzipped, and a real
NCBI record (an *E. coli* K-12 chromosome section) rather than only hand-built
cases. Assert record count, feature count, a known `join` CDS producing parent
plus two segments, and contig lengths matching the `LOCUS` lines.

**The two failures unit tests would miss:**

- Coverage double-count -- assert a `join` feature's covered bases equal the sum
  of its segments, not the outer span.
- Detection -- assert a `.gbff` detects as GENBANK, *and* that GFF, BED and
  FASTA still detect as themselves. Per CLAUDE.md, the negative direction is the
  one that fails when the seam breaks; asserting only the positive can pass
  whether or not the change worked.

**Live database check** before calling it done: run the parser against a real
GenBank in the store via `docker compose exec api python -c ...`. The
Actions-tab rules passed a full green suite while being wrong about real
objects; fixtures that already look the way the parser expects prove less than
one real file.

## Out of scope

- Sequence extraction into a derived FASTA reference -- its own issue, signalled
  by `genbank_has_sequence`
- Bakta artifact promotion -- [#214](https://github.com/syntheticgio/bioflow/issues/214)
- Any change to GFF/GTF/BED behavior from #257
