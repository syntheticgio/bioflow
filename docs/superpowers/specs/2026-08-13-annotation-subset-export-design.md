# Annotation subset export

Design for [#297](https://github.com/syntheticgio/bioflow/issues/297).

[#257](https://github.com/syntheticgio/bioflow/issues/257) shipped an
annotation Results view that is inspectable but read-only. #297 asks for
editing and export. This design delivers the export half — filtering an
annotation to a subset and materializing that subset as a new project object
— and deliberately declines the editing half, for reasons the next section
makes concrete.

## What exists today

Four designs have landed on the annotation stack since #297 was written, and
together they change what the issue should ask for:

- `annotation_parse.py` normalizes GFF3, GTF, and BED to one `Feature` shape.
- `annotation_db.py` stores every row in a per-object SQLite index and serves
  filtered, paged queries through `FeatureFilters`.
- `annotation_hierarchy.py` classifies every parent reference and assigns a
  `depth`, bounded by `DEPTH_CAP = 100`.
- `genbank_parse.py` adds GenBank as a fourth format.

### Why this design is export-only

#297's scope reads "define safe editing for feature coordinates, types,
source fields, and format-specific attributes" and, separately, "export valid
GFF3 or GTF". Four properties of the code that landed make the first of those
substantially more expensive than the issue assumes, and make the second
nearly free if attempted on its own:

- **`Feature` is lossy.** `parse_gff_line` keeps neither the `source` column
  nor `phase` (`backend/app/pipelines/annotation_parse.py:129`). A CDS
  reconstructed from the index would lose the reading frame. BED is
  additionally converted to one-based at parse time, so its original
  coordinates exist nowhere in the index.
- **No feature is addressable.** `_COLUMNS`
  (`backend/app/pipelines/annotation_db.py:27`) does not return `rowid`, and
  no source line number is stored. Nothing today can name a single feature
  for a client to edit.
- **The index is disposable by design.** It is documented as "a derived
  artifact rebuilt from the annotation on demand"
  (`backend/app/pipelines/annotation_db.py:71`), is opened read-only, and
  `build_annotation_db` unlinks and recreates it. Edits stored there would be
  destroyed by the next recompute.
- **Some rows are synthetic.** A GenBank `join(...)` location emits a parent
  row plus one child per segment, rows that correspond to no single line in
  the source file.

Field-level editing therefore requires per-feature identity, a writable store
that survives recompute, and a full round-trip serializer that recovers the
columns `Feature` discards. Subset export requires none of those, because it
never reconstructs a feature: it re-emits the original bytes.

That difference is the whole basis of this design, and it is worth stating as
a constraint rather than an implementation detail. **The exporter must never
rebuild a feature line from `Feature`.** The moment it does, phase, the
source column, and BED's original coordinates are wrong, and the failure is
silent — a plausible-looking file that misrepresents reading frame.

## Decisions

### Scope: filter and export, not field editing

The first slice filters an existing annotation and writes the matching
subset as a new object. No field values change.

This answers #297's open question ("whether the first slice supports
attribute-only edits or coordinates as well") with "neither, yet". Editing
is not abandoned; it is separated, because it needs the three mechanisms
listed above and this does not. The line number added here is also the first
half of per-feature identity, so this slice moves editing closer rather than
around it.

The user-visible capability is real on its own: a chromosome-subset GFF3, a
CDS-only GTF, or a filtered peak BED can be fed to another pipeline or
downloaded, which is what a user filtering the table today cannot do.

### Export the closure of what matched, not the matched rows

A filter matches rows; a valid annotation needs trees. Filtering to a contig
matches genes, but a gene's transcripts and exons are separate rows, and
exporting the gene alone yields `Parent=` references that dangle. Filtering
to `exon` matches rows whose parents are absent from the output entirely.

So the export includes every matched feature, plus every ancestor and every
descendant of a matched feature.

Rejected: matched rows only (produces structurally broken files), and a
user-facing toggle (its only function is to let someone produce a broken
file). The count difference is surfaced instead of hidden, because an export
that silently returns more features than the table showed reads as a bug.

### Verbatim re-emission by line number

The exporter copies original source lines rather than serializing `Feature`.
Each feature records the line it came from; the export job re-scans the
source once and emits the lines in the closure.

Rejected alternatives:

- **Byte offsets.** Faster, but they bind the index to exact file bytes. If
  the source shifts, offsets point at wrong-but-valid lines and the export is
  plausible and wrong. That is the worst available failure mode.
- **Re-running the filter during the export scan.** Requires no new column,
  but states the filter twice — once as SQL in the index, once as Python in
  the exporter — and the two will drift. It also cannot compute the closure
  without the hierarchy the index already holds.

Line numbers are additionally self-checking: the content at a recorded line
can be re-parsed and compared to what the index stored, which byte offsets
cannot do cheaply. That check is required (AE-14), not optional — it is the
entire reason this option beats byte offsets.

### GenBank is out of scope

GenBank features span multiple lines (a location line plus continuation-
wrapped qualifiers), and its segment children are synthetic rows with no
line of their own. Line-number re-emission does not express either.

Exporting GenBank as GFF3 is a format *conversion*: it must synthesize lines
that never existed, which is the reconstruction path this design exists to
avoid. It belongs to its own issue.

`Feature.line` is therefore `None` for GenBank, making the exclusion visible
in the type rather than in a comment.

### Output is a new object, not a download

The subset is registered as a project object with role `ANNOTATION` and
`derived_from` naming the source.

Download comes free: the existing object UI already serves it. A
download-only export would be cheaper to build but forfeits the reason to
build it inside BioFlow — a filtered annotation that can be used as pipeline
input.

The new object does **not** automatically compute its own annotation
results. Whether ingestion should trigger analysis is
[#298](https://github.com/syntheticgio/bioflow/issues/298)'s question, and
answering it here by side effect would pre-empt it.

### Export is a queued job

A new handler beside `run_annotation_stats`, not a synchronous endpoint.

A synchronous route would be instant on a small BED, but it puts an unbounded
file re-scan inside a request handler with no cancellation, no progress, and
no retry. `annotation_handlers.py` documents rejecting exactly this trade for
the stats pass. A route also cannot create an object, forfeiting the
preceding decision.

The accepted cost is that exporting a 200-line BED involves watching a queue
entry.

## Requirements

### Line recording

- **AE-1** — `Feature` carries the 1-based line number of the source line it
  was parsed from.
- **AE-2** — A `Feature` parsed from a GenBank record carries no line number.
- **AE-3** — The compute job stores each feature's line number in the
  annotation index.
- **AE-4** — A feature stored under multiple parents records the same line
  number on every stored row.

### Closure

- **AE-5** — The export includes every feature matching the requested
  filters.
- **AE-6** — The export includes every descendant of a matched feature,
  transitively.
- **AE-7** — The export includes every ancestor of a matched feature,
  transitively.
- **AE-8** — The export includes each source line at most once, however many
  stored rows reference it.
- **AE-9** — Closure traversal terminates on a file whose hierarchy contains
  a cycle.
- **AE-10** — The export ignores the `top_level_only` filter, which is a
  paging device rather than a statement about content.

### Output file

- **AE-11** — Each exported feature line is byte-identical to the
  corresponding line in the source file.
- **AE-12** — Exported feature lines appear in the same relative order as in
  the source file.
- **AE-13** — An export of a GFF3 source begins with a `##gff-version`
  pragma, whether or not the source carried one.
- **AE-13a** — An export of a GTF or BED source carries the source's comment
  lines and synthesizes none, neither format having a mandatory header.
- **AE-14** — The export job fails when a line it is about to emit does not
  parse to a feature whose contig, start, and end match what the index
  recorded for that line.
- **AE-15** — A failure under AE-14 is not retryable.
- **AE-16** — The export job fails when the requested filters match no
  feature.
- **AE-17** — The export does not carry `##sequence-region` pragmas from the
  source, which describe the full file rather than the subset.

### Result object

- **AE-18** — A successful export registers a new object with role
  `ANNOTATION`.
- **AE-19** — The new object records the source annotation in
  `derived_from`.
- **AE-20** — The new object carries no annotation results facts.

### Interface

- **AE-21** — A user can request an export from the annotation feature table
  in one interaction.
- **AE-22** — The export request applies the filters the table is currently
  displaying.
- **AE-23** — The user can see both the number of features matched and the
  number to be exported before the export runs.
- **AE-24** — Export is unavailable for an annotation whose results have not
  been computed.
- **AE-25** — Export is unavailable for a GenBank annotation.

## Components

### `annotation_parse.py`

`Feature` gains `line: int | None`. The parse functions do not set it — they
stay pure functions of a single string, which is what makes the format edge
cases testable as plain calls. The handler's loop sets it.

### `annotation_db.py`

The `features` DDL gains a `line INTEGER` column, and both `INSERT`
statements carry it. No new index: the export selects by filter and reads
`line` out, never looking a feature up by line.

`_COLUMNS` is left unchanged, so the table's row shape does not change.

### `annotation_export.py` (new)

Separate from `annotation_db.py`, which is documented as the read-only query
surface behind the table. Export is a different consumer with a different
lifetime, and the closure walk is substantial enough to test on its own.

- `closure_lines(*, db_path, filters) -> set[int]` — applies the existing
  `_where()`, then walks descendants and ancestors with recursive CTEs
  bounded by `DEPTH_CAP`, returning line numbers. Reusing `_where()` rather
  than restating it is what keeps the exported subset and the displayed table
  from drifting.
- `write_subset(*, source, dest, lines, header, fmt) -> int` — the verified
  single-pass re-scan. Returns the number of lines written.

`write_subset` iterates the source and emits lines whose number is in the
set, rather than iterating the set and seeking. That is what satisfies AE-12
structurally: source order is preserved because the source is what is being
walked. Iterating the set would require it to be sorted, and would make
ordering a property someone could regress without noticing.

### `annotation_handlers.py`

`export_annotation_subset`, beside `run_annotation_stats`. It resolves the
source object, calls the two functions above, and registers the result.

The handler never re-derives the filter: it receives `FeatureFilters`, passes
it to `closure_lines`, and works in line numbers from there.

Cancellation is checked on the same 100,000-line cadence `_line_rows` uses.

### API

`POST /annotationstats/export/{object_id}`, taking the same filter query
parameters `GET /annotationstats/features/{object_id}` accepts, and returning
a job. It 404s when the index is absent, as the features route already does.

A companion count is served so AE-23 can be satisfied before launching:
the closure size for the current filters.

### `AnnotationFeatureTable.tsx`

An "Export filtered" control beside the existing filter row, disabled per
AE-24 and AE-25, showing matched and closure counts per AE-23.

## Testing

Backend only. This repo has no headless component testing, so the control
itself is verified manually at localhost:5273.

Unit tests over a temp SQLite index, following `test_annotation_db.py`:

- Closure downward from a gene reaches its transcripts and exons.
- Closure upward from an exon reaches its transcript and gene.
- Closure from a mid-tree match reaches both directions.
- A multi-parent exon contributes one line, not two (AE-8).
- A cyclic hierarchy terminates (AE-9).
- `unresolved_only` returns matched rows and no ancestors — correct, since
  those rows' parents by definition do not resolve, and worth pinning so the
  interaction is not later mistaken for a bug.
- An empty match raises rather than writing a file (AE-16).

Two tests carry most of the value:

- **Verification works.** Build an index, then mutate the source file so line
  numbering shifts, and assert the export fails permanently. Without this,
  the argument for line numbers over byte offsets is untested reasoning.
- **Round-trip fidelity.** Export with a filter matching everything and
  assert the feature lines are byte-identical to the source's. This is the
  strongest available statement of AE-11, and it fails loudly if a later
  refactor starts rebuilding lines from `Feature`.

The fidelity test runs against a real NCBI GFF3 fixture, not hand-built
lines. Hand-built fixtures are the shape that let the suggestion-rules and
STAR registry failures pass while being wrong, and the GenBank design imposed
the same requirement on itself for the same reason.

Handler-level tests assert the new object's role and `derived_from`, and
assert the *absence* of annotation results facts (AE-20), since that is the
boundary against #298.

## Out of scope

- **Field editing** — coordinates, types, names, attributes. Needs
  per-feature identity, a store surviving index recompute, and a full
  serializer. #297 remains open for it after this lands.
- **GenBank export**, and annotation format conversion generally.
- **Adding features** not present in the source.
- **Automatic results computation** on the exported object —
  [#298](https://github.com/syntheticgio/bioflow/issues/298).
- **Shared results infrastructure** —
  [#299](https://github.com/syntheticgio/bioflow/issues/299).
