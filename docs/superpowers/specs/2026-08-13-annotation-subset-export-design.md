# Export a filtered annotation subset as a new object

Design for [#358](https://github.com/syntheticgio/bioflow/issues/358).

Filtering the annotation feature table shows a subset but offers no way to
keep it. This design exports the current filter as a new project object, so a
chromosome-subset GFF3, a CDS-only GTF, or a filtered peak BED can be
downloaded or fed to another pipeline.

## What exists today

`AnnotationFeatureTable.tsx` filters through
`GET /pipelines/annotationstats/features/{object_id}`
(`backend/app/api/v1/pipelines.py:932`), which builds a
`FeatureFilters` (`backend/app/pipelines/annotation_db.py:33`) and pages over
the SQLite feature table at
`settings.annotation_stats_dir / <object_id> / features.db`.

That database is built by `run_annotation_stats`
(`backend/app/queue/annotation_handlers.py:154`) in a single pass over the
source file, then classified by `resolve_hierarchy` and renamed into place.

Nothing today can turn a filtered view into a file.

## Why this is separate from #297

[#297](https://github.com/syntheticgio/bioflow/issues/297) asks for annotation
editing *and* export. The two halves cost very different amounts, because four
things about the current code decide it:

- `Feature` (`backend/app/pipelines/annotation_parse.py:16`) has no field for
  the GFF `source` column or `phase`, and `parse_bed_line` converts BED to
  one-based. A feature rebuilt from the index loses reading frame.
- No feature is addressable: `_COLUMNS`
  (`backend/app/pipelines/annotation_db.py:27`) returns no `rowid`, and no
  source line number is stored.
- The index is disposable. `build_annotation_db` unlinks it on every
  recompute, and the handler renames a freshly built file into place.
- GenBank emits synthetic parent/segment rows
  (`backend/app/pipelines/genbank_parse.py:237`) matching no single source
  line.

Field editing needs per-feature identity, a store surviving recompute, and a
full round-trip serializer. Subset export needs none of them, **provided it
re-emits original source lines rather than reconstructing them.** That
constraint is the basis of this design.

#297 stays open for the editing half. The line number added here is also the
first half of per-feature identity, so this moves editing closer rather than
around it.

## Decisions

### Re-emit source lines, never reconstruct

The export writes bytes copied from the source file, selected by line number.
It never serializes a `Feature` back to text.

This is forced by the parser, not chosen for simplicity: `source` and `phase`
are not stored at all, so a reconstructed GFF3 CDS line would carry a `.`
where a reading frame belongs — valid syntax, wrong biology, and silent. BED's
one-based conversion is the same hazard in the other direction.

The consequence is that export is a *subset* operation only. It cannot convert
formats, and it cannot reflect edits. Both are correct exclusions here.

### One shared filter builder

`top_level_only` is currently derived inside the route
(`backend/app/api/v1/pipelines.py:986`):

```python
top_level_only = feature_type is None and not unresolved
```

That rule decides whether "matched" counts genes or exons. The page count and
the export must agree about it, or the matched-versus-exported counts below
are meaningless.

A single function builds `FeatureFilters` from the request arguments, called
by both the page route and the export route. `features_in_window`
(`backend/app/pipelines/annotation_db.py:367`) is deliberately **not** routed
through it — it sets `top_level_only=False` unconditionally for the track
viewer, which is a different rule, and folding two rules into one builder to
share three lines would create the drift this decision exists to prevent.

### Closure, not matched rows

The export includes every matched feature plus its ancestors and descendants.

Ancestors are non-negotiable: a `Parent=` reference to a feature not in the
file makes the output fail in downstream tools. Descendants matter for the
opposite case — a filter matching a gene, exported without its transcripts and
exons, produces a valid file that is useless.

The cost is that the output is larger than the view, and sometimes
surprisingly so: filtering to `exon` pulls in every ancestor mRNA and gene, so
a "CDS-only" export is not literally CDS-only. This is a UI problem rather
than a correctness one, addressed by the counts below.

The closure walk iterates level by level, bounded by
`annotation_hierarchy.DEPTH_CAP`, matching `_assign_depths`
(`backend/app/pipelines/annotation_hierarchy.py:119`). That bound terminates
on cycles and counts depth in the same units the rest of the module does.

### Both counts are shown before the user commits

The dialog states the matched count and the exported count. The difference is
the closure expansion, and unexplained it reads as a bug.

### Line numbers, verified

Each row records the 1-based line number it was parsed from. Rejected byte
offsets: they bind the index to exact bytes and produce plausible-but-wrong
exports when the file shifts.

At export, each recorded line is re-read and re-parsed, and the result is
compared against the indexed row. A mismatch fails the job permanently — a
wrong-but-plausible annotation file is worse than no file.

### A whole-file identity check, before any line is read

Per-line verification alone can pass on a genuinely stale index. The features
database is rebuilt on every recompute and derived from a file that may since
have been replaced; where most lines are unchanged, per-line checks succeed on
the exported lines while the subset silently mixes two versions of the file.

So the source object's `blob_sha256` (`backend/app/models/object.py:253`) is
recorded when the database is built, and compared before export begins. A
mismatch fails immediately with a message the user can act on: the annotation
changed since results were computed, so recompute and retry.

`blob_sha256` is nullable — hashing may not have finished, and a
register-in-place file may never be hashed. When it is null the export
proceeds on per-line verification alone and records
`annotation_subset_source_verified: false` in the exported object's facts.
Blocking instead would turn a rare, invisible background state into a hard
stop with a confusing cause, in a tool whose stated posture
(`CLAUDE.md`) is single-user and non-critical. The weaker guarantee is made
auditable rather than hidden.

### A derived object, not a download

The export creates a `DataObject` with role `ObjectRole.ANNOTATION`,
`derived_from` the source annotation, via `object_service.ingest_local_file`
(`backend/app/services/object_service.py:185`) from a file under
`settings.tmp_dir`.

Download comes free through the existing object UI, and the object can be used
as pipeline input. `ObjectRole.ANNOTATION` is already in
`FORMAT_DERIVED_ROLES` (`backend/app/metadata/schemas.py:456`), so the
metadata registry needs no new entry.

### Name from the filter, capped; full filter in facts

The exported name uses the one or two most distinctive active filters —
`GRCh38.chr21.gff3`, `GRCh38.exon.gff3` — falling back to `subset` when more
than two are set. The complete filter is recorded in the exported object's
facts regardless.

Three objects named "subset" in a project are indistinguishable without
opening them; a name built from four active filters is unreadable. The cap
handles the common case and the facts guarantee nothing is lost when the name
gives up.

### GenBank is excluded

Its features span multiple lines and its segment children are synthetic
(`backend/app/pipelines/genbank_parse.py:237`), so exporting it is a format
conversion rather than a subset. The export control is hidden for GenBank
sources, and the route rejects them. Its own issue.

## Requirements

Identifiers are permanent and are not reused.

### Recording line numbers

- **AE-1** — `Feature` carries the 1-based line number of the source line it
  was parsed from.
- **AE-2** — `parse_gff_line`, `parse_gtf_line`, and `parse_bed_line` each set
  that line number from a value supplied by the caller.
- **AE-2a** — That parameter is optional in all three parsers, defaulting to
  null. The three are dispatched uniformly through one dict in `_line_rows`
  (`backend/app/queue/annotation_handlers.py:189`) and called with a single
  positional argument, so their signatures must stay interchangeable; existing
  callers and tests pass only the line.
- **AE-2b** — The line number counts every line of the source file, including
  comment and blank lines, so that it addresses the file rather than the
  features in it. `_line_rows`'s existing `enumerate` index already counts
  this way and is 0-based; the stored value is that index plus one.
- **AE-3** — The features table stores the line number in a `line_no` column.
- **AE-4** — A feature parsed from a multi-line or synthetic source (GenBank)
  stores a null line number. GenBank cannot be exported (AE-33), so this is
  not a path to an export; it is what keeps the column honest, so that a null
  means "not addressable by line" rather than "not recorded yet". Any future
  reader of `line_no` can trust a non-null value.
- **AE-5** — A GFF3 record with `Parent=a,b` writes one row per relationship,
  and every such row records the same line number.

### Filter agreement

- **AE-6** — One function builds a `FeatureFilters` from the table's request
  arguments.
- **AE-7** — The feature page route builds its filters by calling that
  function.
- **AE-8** — The export route builds its filters by calling that function.
- **AE-9** — The count of features matching an export's filter equals the
  `total` the page route reports for the same arguments.

### Closure

- **AE-10** — The exported set includes every feature matching the filter.
- **AE-11** — The exported set includes every ancestor of every matched
  feature.
- **AE-12** — The exported set includes every descendant of every matched
  feature.
- **AE-13** — No exported GFF3 line carries a `Parent=` value naming a feature
  absent from the export.
- **AE-14** — The closure walk terminates on a file whose parent references
  form a cycle.
- **AE-15** — The closure walk visits no more than `DEPTH_CAP` levels.

### Verification

- **AE-16** — The export fails permanently when the source object's
  `blob_sha256` differs from the value recorded when the features database was
  built.
- **AE-17** — That failure names the annotation and states that results must
  be recomputed.
- **AE-18** — The export proceeds when the recorded `blob_sha256` is null.
- **AE-19** — An export that proceeded without a hash comparison records
  `annotation_subset_source_verified: false` in the created object's facts.
- **AE-20** — Each exported line is re-parsed at export and compared against
  the indexed row it came from.
- **AE-21** — The export fails permanently when a re-parsed line disagrees
  with its indexed row.

### The exported object

- **AE-22** — A successful export creates a `DataObject` with role
  `ObjectRole.ANNOTATION`.
- **AE-23** — That object's `derived_from` names the source annotation.
- **AE-24** — That object's facts record the complete filter that produced it.
- **AE-25** — That object's facts record the matched and exported feature
  counts.
- **AE-26** — The object's name is derived from at most two active filters.
- **AE-27** — The object's name falls back to `subset` when more than two
  filters are active.
- **AE-28** — The exported file begins with the source file's leading comment
  lines, copied verbatim, before any feature line. A GFF3 lacking its
  `##gff-version 3` directive is malformed, and headers are not features so
  the closure never selects them; they are copied as a separate step.
- **AE-28a** — Header copying reads the source file directly rather than the
  `_HEADER_SCAN_LINES` cap in `run_annotation_stats`
  (`backend/app/queue/annotation_handlers.py:35`), which exists to bound what
  is *displayed* and would silently truncate a long
  `##sequence-region` header on export.
- **AE-29** — Exported lines appear in source file order.

### The control

- **AE-30** — The feature table offers an export control when a filter is
  active.
- **AE-31** — The control shows the matched count and the exported count
  before the export is started.
- **AE-32** — The control is absent for a GenBank source.
- **AE-33** — The export route rejects a GenBank source.

## Testing

Three things need testing that the existing suite's habits would miss.

**Fidelity against a real NCBI GFF3, not hand-built lines.** Hand-built
fixtures feed the code objects that already look the way it expects. This is
how the suggestion-rules and STAR failures passed a green suite while being
wrong (`CLAUDE.md`). The fidelity test asserts that exported lines are
byte-identical to the corresponding source lines, including the `source` and
`phase` columns that `Feature` does not store — which is the whole reason for
the re-emission constraint, and cannot be checked against a fixture built from
`Feature` objects.

**The `_APPLIERS` entry gets its own assertion.** That dict
(`backend/app/queue/results.py:2692`) silently skips unknown job types, so a
missing entry means the export job succeeds and no object is ever created. The
docstring at `results.py:53` records eleven appliers that were already in that
state, found only by running a real job. A test asserts the export job type is
present in `_APPLIERS`.

**Verification is tested in the failing direction.** A test asserting an
export succeeds passes whether or not the hash check works. The tests that
matter assert the export *fails* when the recorded hash disagrees (AE-16) and
when a re-parsed line disagrees (AE-21).

## Out of scope

- Field editing ([#297](https://github.com/syntheticgio/bioflow/issues/297)).
- GenBank export and format conversion.
- Adding features.
- Automatic results computation on the exported object
  ([#298](https://github.com/syntheticgio/bioflow/issues/298)).
