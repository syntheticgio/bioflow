# Annotation file results

Design for [#257](https://github.com/syntheticgio/bioflow/issues/257).

Annotation files have no results experience. A GFF3, GTF, or BED opens to
metadata and nothing else — there is no way to see what the file contains or
what an annotation run produced. This design adds a Results tab for those
formats.

## What exists today

Ingest runs `_parse_tabular` (`backend/app/storage/parsers.py:706`) over the
first 5000 lines and stores `sampled_records`, `column_counts`,
`header_lines`, and `reference_names`. Nothing in that is GFF-aware: no
feature types, no attributes, no per-contig counts. On a human GFF3 of a few
million lines, `sampled_records` is a truncated count that answers no
question anyone asks.

`DetailPanel` gates the Results tab on `bam`/`vcf`/`bcf`/counts
(`frontend/src/components/DetailPanel.tsx:352`), so GFF/GTF/BED get no tab at
all.

The only GFF parsing in the repo is `bakta_runner.parse_gff3`
(`backend/app/pipelines/bakta_runner.py:50`), which keeps only `gene`
features to feed a Circos gene-density ring. It is not an object-facts path
and is not reused here.

## Decisions

### Scope: GFF/GTF and BED, one view that degrades

The `ANNOTATION` role deliberately spans a published NCBI GFF3, a Bakta
output, a GTF, and user BED files such as peak calls or blacklists
(`backend/app/models/object.py:89`). These share a genuinely useful core —
how many intervals, on which contigs, covering what fraction of the
reference, at what size distribution — and that core is exactly what a
BED-only user cannot see today.

So: one view, two layers. The interval core renders for every supported
format. The GFF-specific layer (feature types, biotypes, attribute
summaries, hierarchy) renders only when the data is there.

Covering GFF/GTF alone would ship a Results tab that a blacklist BED still
cannot open, reproducing the issue's complaint one format over.

### Trigger: on-demand compute, not ingest

Every number in this view needs a full-file pass. That pass happens in a
queued job behind a "Compute results" button, matching `run_vcf_stats`.

The alternative — extending `_parse_tabular` to read whole files at ingest —
was rejected on two grounds. It breaks the pattern BAM, VCF, and counts
already establish, for no user-visible gain. And `_parse_tabular` runs
*inline* during ingest, where the 5000-line cap is what keeps ingestion fast
and bounded; making every annotation upload pay a full parse before the
object goes `READY` slows the common case (uploading a reference bundle
whose GFF nobody has opened yet) to serve the uncommon one, and a
pathological file stalls ingest rather than failing a retryable job.

The accepted cost is that opening a GFF3's Results tab shows a button rather
than an answer. That is the cost everywhere else in this app.

### Storage: SQLite per object

Aggregates go to object facts; the feature table goes to a SQLite database
under a new `settings.annotation_stats_dir`, laid out by object id.

This is not a new pattern — `vcf_stats_dir` already stores `variants.db` and
serves it through a paged endpoint (`backend/app/api/v1/pipelines.py:804`).
`variant_db.py` is the worked example this design follows throughout.

SQLite over a sorted TSV with an offset index, or over bgzip+tabix, because
the access pattern is a database pattern rather than a genomics one. Tabix
answers "features overlapping this interval" superbly, but "type = CDS and
name contains *kinase*, sorted by position, rows 200–250, and how many match
in total" is not that question, and every filter combination would become
application code scanning tabix output. SQLite makes the filter × locus ×
page × count matrix one query planner's job. The total-match count in
particular is the query most annoying to hand-roll against either
alternative.

The cost is a binary blob rather than a greppable text file, and a larger
file on disk. Neither outweighs the query flexibility.

### Table: hierarchy-aware, paged over parents

A GFF3 line is not a feature in the sense a person means: one protein-coding
gene is a `gene` line, an `mRNA` child, several `exon` lines, a `CDS`, and
sometimes UTRs. Opening a file to three million exons with the genes buried
among them is the failure `parse_gff3`'s comment already complains about.

Every line is stored — throwing lines away makes the table stop being a view
of the file, and BED needs the flat shape regardless. But the table renders
top-level features (`parent IS NULL`) as rows, each expandable into its
children.

**Paging applies to parents.** `LIMIT`/`OFFSET` and the total count are over
top-level rows; children are fetched per-parent on expand and are neither
paged nor counted. This keeps the endpoint contract identical to the variant
table's, so the paging subtleties documented at
`backend/app/api/v1/pipelines.py:790` transfer unchanged.

The rejected alternative was a materialized sort key making expansion a
server-side pagination concern. It is more correct and buys nothing a user
would notice, while making every expand/collapse repaginate.

**Filters and the locus jump also apply to parents.** Jumping to
chr1:1,000,000 finds the genes there, not their exons.

### Attribute summaries: keys plus biotype

A GFF3's 9th column is an open vocabulary. The aggregate layer stores
attribute *key* frequency (what the file carries) and value counts for
`gene_biotype`/`biotype` specifically.

Biotype is the one attribute with a small, meaningful vocabulary people want
counted — protein-coding vs. pseudogene vs. ncRNA. Everything else is an
identifier or free text, where a value distribution is noise. A general
per-key value distribution is a possible follow-up, not a guess to make now.

## Architecture

One compute path parallel to `run_vcf_stats`.

**Trigger.** `DetailPanel`'s `hasResults` gate gains `gff`, `gtf`, and `bed`.
The tab renders `AnnotationResults`, which branches on an
`annotation_stats_status` fact: absent renders the `NodeSelector` and the
compute button; `"ok"` renders the view.

**Compute.** `api.launchAnnotationStats(objectId, targetNode)` enqueues
`run_annotation_stats`. The handler lives in a new
`backend/app/queue/annotation_handlers.py` rather than joining
`assembly_qc_handlers.py`, whose annotation-adjacent code is Bakta's
gene-density path and shares nothing with this but the word.

The handler makes **one** streaming pass, producing both the aggregate facts
and the SQLite rows. The file is never read twice; the pass is the expensive
part.

**Storage.** `settings.annotation_stats_dir / <object_id> / features.db`,
laid out like `vcf_stats_dir`. Added to the reaper tuple at
`backend/app/queue/handlers.py:895` — a one-line change, since that sweep
reaps directories by object id and never inspects their contents.

**Apply.** `_apply_run_annotation_stats` in `results.py`, registered in the
dispatch dict beside `run_vcf_stats` (`backend/app/queue/results.py:2629`),
merges facts and sets `annotation_stats_status`.

**Serve.** Two endpoints in `pipelines.py`. Both run the ownership check
before building the path, for the reason those routes already document: the
directory is keyed by object id alone, so the lookup is the only thing
between one profile and another's data.

Data flow: file → single streaming pass → (facts → Mongo → React Query) and
(rows → SQLite → paged endpoint → table).

## Parsing

`backend/app/pipelines/annotation_db.py`, the analog of `variant_db.py`,
streams the file and yields normalized rows. Format differences live entirely
in how a line becomes a row; everything downstream sees one shape.

- **GFF3** — 9 tab-separated columns, attributes as `key=value;` pairs.
  `ID` and `Parent` come straight out.
- **GTF** — same 8 leading columns, attributes as `key "value";`. No
  `ID`/`Parent`: hierarchy is inferred from `gene_id`/`transcript_id`, where
  a `transcript` row's parent is its `gene_id` and an `exon`'s is its
  `transcript_id`. This is the one real parsing asymmetry and needs its own
  tests.
- **BED** — 3 to 12 columns. No type, no attributes, no hierarchy; name from
  column 4 when present.

Malformed lines are skipped and counted rather than raised — the posture
`parse_gff3` documents (`backend/app/pipelines/bakta_runner.py:59`). The
count reaches the facts, so a half-garbage file says so instead of silently
under-reporting.

**BED's half-open zero-based coordinates are converted to GFF's 1-based
inclusive at parse time.** `start` then means the same thing in every row and
the locus jump need not know the source format. Getting this wrong is an
off-by-one invisible until someone compares against a genome browser.

## Schema

```sql
CREATE TABLE features (
  rowid INTEGER PRIMARY KEY,
  contig TEXT NOT NULL,
  start INTEGER NOT NULL,   -- 1-based inclusive; BED converted at parse
  end INTEGER NOT NULL,
  type TEXT,                -- null for BED
  strand TEXT,
  score REAL,
  name TEXT,                -- Name, gene_name, gene_id, or BED column 4
  feature_id TEXT,          -- GFF ID / GTF transcript_id|gene_id
  parent TEXT,              -- null = top-level
  biotype TEXT,
  attributes TEXT           -- raw 9th column, for the expanded detail row
);
CREATE INDEX ix_features_locus ON features(contig, start);
CREATE INDEX ix_features_parent ON features(parent);
CREATE INDEX ix_features_type ON features(type);
CREATE INDEX ix_features_name ON features(name);
```

`ix_features_parent` is what makes the expand-a-gene query cheap, which is
the entire basis for paging over parents.

Build follows `build_variant_db`: `PRAGMA journal_mode=OFF`,
`PRAGMA synchronous=OFF`, batched `executemany`, indexes created after the
bulk insert.

## Aggregates

Accumulated during the same pass, merged onto the object as facts:

- total feature count, contig count
- per-type counts (GFF/GTF)
- per-contig counts and features per Mb
- feature length distribution, as histogram bins
- fraction of each contig covered by features
- `biotype` value counts, when present
- attribute-key frequency
- malformed-line count
- source and tool metadata from `##` header directives

**Contig coverage merges intervals per contig as it accumulates.**
Overlapping exons must not double-count bases; summing lengths would report
coverage above 100% on any dense annotation.

## Query layer

```python
@dataclass
class FeatureFilters:
    contig: str | None = None
    start_min: int | None = None      # locus jump
    start_max: int | None = None
    feature_type: str | None = None
    biotype: str | None = None
    name_query: str | None = None     # substring, case-insensitive
    strand: str | None = None
    top_level_only: bool = True

def build_annotation_db(*, rows, db_path) -> int
def query_features(*, db_path, filters, offset, limit) -> list[dict]
def count_features(*, db_path, filters) -> int
def children_of(*, db_path, parent_id) -> list[dict]
```

`_where` composes filters as its variant counterpart does. `top_level_only`
becomes `AND parent IS NULL`.

**The type filter must clear `top_level_only`.** Filtering to `exon` has to
search all rows; leaving the flag set returns an empty table on a
well-formed GFF3. This interaction is the only real logic in the layer and
gets its own test.

## Endpoints

`GET /annotationstats/features/{object_id}` — filters as query params, plus
`offset`, `limit`, `skip_count`. Returns `{total, rows}`. `skip_count`
carries over from the variant route for the reason documented there:
`COUNT(*)` cannot use a single index across composed filters, so the client
re-sends its cached total when only the page changed.

`GET /annotationstats/children/{object_id}?parent_id=...` — unpaged, all
children of one feature. Unpaged deliberately: a transcript has tens of
exons, not thousands, and paging inside an expanded row is complexity with
no payoff.

A missing `features.db` raises `NotFoundError` with the existing sentence,
"No computed results for this file. Compute results first." — the same
wording, so a user who met it on a VCF recognizes it.

## UI

`frontend/src/components/AnnotationResults.tsx`, structured like
`VariantResults`.

**Empty state.** `NodeSelector` and a compute button, with a note naming what
it produces: feature counts by type, coverage across the reference, and the
full searchable feature table. Once results exist, a recompute affordance
sits in the provenance line.

**Provenance line.** Source and tool from the `##` directives, total feature
count, contig count, and the malformed-line count *only when nonzero* — a
clean file should not display a zero.

**Summary and charts.** Universal core, rendering for every format:
per-contig feature density, contig coverage, feature length distribution.
GFF/GTF-only, absent rather than empty for BED: feature type breakdown, and
biotype breakdown when biotypes were present.

**Table.** Sticky filter bar with contig, type, and biotype dropdowns — all
populated from aggregates already in facts, costing no extra query — plus a
name search box, a strand toggle, and a locus input accepting
`chr1:1,000,000-1,050,000`. Rows are top-level features; a chevron expands
one into its children through the children endpoint, cached per parent so
re-expanding is free.

Paging follows the variant table: offset/limit, cached total, `skip_count`
on page-only changes. The name search debounces before firing.

**Expansion state must not survive a page change.** A stale parent's children
leaking into a new page is the second failure mode to guard, after the type
filter interaction above.

## Testing

Backend, in `backend/tests/`:

- parser tests per format: GFF3 attribute parsing, GTF `gene_id`/
  `transcript_id` hierarchy inference, BED coordinate conversion, malformed
  line handling
- a coordinate test asserting BED and GFF3 descriptions of the same interval
  produce identical `start`/`end`
- filter composition, including the type filter clearing `top_level_only`
- merged-interval coverage against overlapping exons, asserting coverage
  never exceeds contig length

Run from a worktree with `./backend/run-worktree-tests.sh tests/ -q`.

Frontend verification is manual at localhost:5273 via
`./ops/worktree-up.sh`, per this repo's convention — there is no component
test setup and none is expected.

Per CLAUDE.md, check `backend/app/services/suggestion_service.py` for any
card whose `unavailable` reason this work makes untrue.

## Follow-ups, deliberately out of scope

- **Per-key attribute value distributions** beyond biotype.
- **Auditing the on-demand-compute paradigm** across all five instances
  (FASTQ QC, BAM stats, VCF stats, transcript QC, and this one), which
  duplicate their empty state, `NodeSelector`, and recompute button.
  `AiSummary`'s `launchFn` prop is the one place the pattern was factored
  rather than copied. Tracked separately.
