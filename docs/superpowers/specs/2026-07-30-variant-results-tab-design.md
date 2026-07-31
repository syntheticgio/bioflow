# Variant Results tab

A Results tab for VCF files: what the call set looks like as a whole, and a
browsable table of the individual calls.

The BAM Results tab is the template. That feature already established every
mechanism this one needs -- a read-only compute job that merges facts onto the
object, a report on disk paginated server-side, and a tab that offers a
"Compute results" button until the facts exist. This spec follows it
deliberately, and calls out the places where a VCF genuinely differs.

The largest of those differences is scale. The BAM tab's per-contig table has
one row per contig -- tens, occasionally thousands. A plant resequencing VCF
has one row per variant, which is millions. That single fact is why the
variant table is backed by SQLite rather than the flat TSV its BAM
counterpart uses; see finding 5.

## Why

A called VCF currently shows only what its header says: sample names, contig
names, INFO/FORMAT keys. Nothing about the calls themselves. "Did this run
work?" and "what did it find?" are both unanswerable from the UI, which is a
gap the BAM tab does not have.

Verified against the three VCFs in the live database (see Findings below), the
header facts today include `record_count` on two of them and nothing at all on
the third -- so even the crudest question, how many variants, has no reliable
answer.

## Scope

Both halves, as agreed:

- **Summary** -- totals, Ti/Tv, filter breakdown, QUAL and depth
  distributions, substitution types, per-contig counts, variant density across
  the reference.
- **Variant browser** -- a paginated, filterable table of individual calls.

Filtering is in scope (contig/position search, FILTER, variant type, minimum
QUAL). Sample switching re-filters the table only; the summary always
describes the whole file.

## Findings from real data

Checked against the live database rather than fixtures, per CLAUDE.md. Three
results changed the design.

**1. `bcftools stats` supplies everything the summary needs.** Confirmed on
`DRR1066343.bcftools.vcf.gz` (6,641 variants, 17 contigs, *S. cerevisiae*):
`SN` gives records/SNPs/indels/multiallelic counts, `TSTV` gives Ti/Tv (2.42),
`ST` gives all 12 substitution types, `QUAL` and `DP` give distributions, `IDD`
gives indel length distribution. One pass, exit 0, on output from both Clair3
and bcftools. No second tool is needed.

**2. The QUAL and DP sections are far too granular to plot directly.** That
file yields 805 `QUAL` rows and 211 `DP` rows -- one per distinct value, not a
histogram. Storing them raw would bloat `facts` and render as noise. They must
be re-binned to a fixed bucket count before storage. This is new work with no
BAM-side equivalent.

**3. `FILTER` is frequently `.` (absent), not `PASS`.** In that same file every
record carries `.` -- bcftools call does not stamp PASS. Clair3 does emit real
values (`RefCall` observed). So the Filters panel and the FILTER dropdown must
treat `.` as its own displayed category ("no filter applied") rather than
silently coercing it to PASS, which would overstate quality. A "94.2% PASS"
headline is misleading on a file where nothing was ever filtered; the summary
therefore reports the PASS rate only when the file actually uses FILTER.

**4. `bin_depth` is not reusable verbatim for the density strip.** Its
signature is `(*, contig_lengths, depth_lines, bin_count)` and it computes a
*mean* per bin from `contig/pos/depth` lines. Variant density is a *count* per
bin. The bin-allocation logic -- proportional bins with a one-bin floor per
contig, so short contigs never vanish -- is the valuable part and should be
extracted rather than reimplemented or contorted.

**5. Extraction is cheap; serving a flat TSV is not, at plant scale.**
`bcftools query` over 6,641 variants into TSV took 8 ms. But this tool targets
plant genomes, where a resequencing VCF holds millions of calls, not
thousands -- roughly 0.7M variants for *Arabidopsis*, 3M for tomato, 16M for
maize, 32M for wheat.

Benchmarked on a synthetic 5M-variant TSV (180 MB) in the `api` container,
the flat-file approach `get_bam_stats_report` uses -- `read_text()` then
`splitlines()` then filter in Python -- costs **440 MB of RSS per in-flight
request** and ~0.9 s per page. That scales to ~1.4 GB for maize and ~2.8 GB
for wheat, *per request*, in a container sharing 12.4 GB with the worker and
Mongo. Two browser tabs paging a maize VCF would be enough to matter.

The same data in SQLite with indexes on `(chrom,pos)` and `filter`: **14 MB
RSS**, 0.2-0.4 ms for a filtered page, 36 ms for a deep offset. Build cost is
6.8 s to load plus 3.4 s to index, paid once at compute time, for a 360 MB
database. That is the difference between a feature that works on plant data
and one that does not, so the variant table is backed by SQLite rather than a
TSV.

`COUNT(*)` is the one weak spot: 13 ms unfiltered, 79 ms on an indexed
filter, but 404 ms for a combined `qual + filter` predicate that cannot use a
single index. The API therefore counts only when the filter set changes, not
on every page turn (see below).

## Architecture

Four backend pieces and three frontend ones, mirroring the BAM feature's split.

### `backend/app/pipelines/vcf_stats_runner.py`

Pure functions over strings and paths, no queue or filesystem -- the same
split as `bam_stats_runner.py` and for the same reason: command construction
and output parsing are the parts worth testing.

- `build_stats_command(*, bcftools_path, vcf)` -> `bcftools stats`
- `build_query_command(*, bcftools_path, vcf, sample=None)` -> `bcftools query`
  with the fixed format string for the variant TSV
- `parse_stats(text)` -> the `SN`/`TSTV`/`ST`/`QUAL`/`DP`/`IDD` sections as a
  dict of typed rows. Section-marker driven, tolerant of sections being absent
  (an empty VCF emits headers with no rows -- verified).
- `rebin_distribution(rows, *, bucket_count)` -> collapses the 805-row QUAL
  and 211-row DP sections into fixed-size histograms. Finding 2.
- `variant_summary(stats)` -> the headline numbers, including Ti/Tv, SNP/indel
  split, and the PASS rate *only when the file uses FILTER* (finding 3).
- `bin_positions(*, contig_lengths, positions, bin_count)` -> variant counts
  per bin. Shares bin allocation with `bin_depth` via a new
  `allocate_bins(*, contig_lengths, bin_count)` extracted from it (finding 4).
  Extracting that helper is a small, targeted refactor of `bam_stats_runner`
  covered by its existing tests.
- `build_variant_db(*, rows, db_path)` -> streams parsed variant rows into a
  SQLite table and builds its indexes. Kept here with the other pure-ish
  functions since it takes an iterator and a path and touches nothing else.
- `query_variants(*, db_path, filters, offset, limit)` -> one page, and
  `count_variants(*, db_path, filters)` -> the filtered total. Both build
  parameterized SQL from the same filter dataclass, so the page and its count
  can never disagree about what is being filtered.

`VARIANT_COLUMNS` is the analogue of `CONTIGS_TSV_COLUMNS`:
`chrom, pos, ref, alt, qual, filter, dp, gt`.

### The variant database

One SQLite file per object at `settings.vcf_stats_dir / <object_id> /
variants.db`, written by the compute job and read by the API.

```sql
CREATE TABLE variants (
  chrom TEXT, pos INTEGER, ref TEXT, alt TEXT,
  qual REAL, filter TEXT, dp INTEGER, gt TEXT
);
CREATE INDEX ix_variants_locus  ON variants(chrom, pos);
CREATE INDEX ix_variants_filter ON variants(filter);
```

Written with `journal_mode=OFF` and `synchronous=OFF` -- it is a derived
artifact rebuilt from the VCF whenever results are recomputed, so durability
buys nothing and costs load time. Indexes are created *after* the bulk insert,
which is why the load is 6.8 s rather than several minutes.

Opened read-only by the API (`file:...?mode=ro` URI). SQLite handles
concurrent readers without coordination, and nothing but the compute job ever
writes.

A TSV is still written beside it as `variants.tsv` -- it is what the Download
button serves, and it is the format a user can actually take elsewhere. It is
streamed to the response rather than read into memory.

### `backend/app/queue/variant_handlers.py` -- `run_vcf_stats`

A new handler beside the existing `call_variants`. Read-only, like
`run_bam_stats`: derives no objects, returns facts for the applier to merge
and writes one TSV to `settings.vcf_stats_dir`.

`vcf_stats_dir` is a new `Settings` property, added alongside the existing
`bam_stats_dir` and following its rationale -- outside `objects/`, since a
regenerable report is not content-addressed storage.

Sequence: materialize the VCF (and `.tbi` if present) into the workdir,
`bcftools stats` -> parse -> re-bin -> `bcftools query` streamed into both the
TSV and the SQLite database -> build indexes -> return facts. Progress phases
`stats` / `query` / `index`, matching how `run_bam_stats` reports.

The `bcftools query` output is consumed as a stream, never materialized as a
list: at 32M variants (wheat) that list alone would exhaust the container.
Per-contig counts and the density bins are accumulated during the same pass,
so the file is read once.

The `index` phase is worth reporting separately because at plant scale it is
seconds, not milliseconds -- 3.4 s at 5M variants.

### `backend/app/services/pipeline_service.py` -- `launch_vcf_stats`

Mirrors `launch_bam_stats`, minus the index chaining. A VCF's `.tbi` is not
required for either `bcftools stats` or `bcftools query` -- both stream the
whole file -- so there is no missing-precondition case to repair and nothing to
chain. `_check_vcf_stats_callable` enforces status READY and
`format.kind in (VCF, BCF)`.

`tools.require(tools.bcftools())` gates it, exactly as the BAM path gates on
samtools.

### `backend/app/queue/results.py` -- `_apply_run_vcf_stats`

Merges the returned facts onto the object and registers in `_APPLIERS` under
`run_vcf_stats`. Structurally identical to `_apply_run_bam_stats`.

### Facts written

Namespaced `vcf_stats_*` so they cannot collide with the header facts the
ingest parser already writes:

```
vcf_stats_status          "ok"
vcf_stats_tool_version    bcftools version string
vcf_stats_summary         { variants, snps, indels, multiallelic, ts, tv,
                            ti_tv, pass_count, pass_pct?, no_filter_count }
vcf_stats_qual_histogram  [{ qual, count }]        re-binned
vcf_stats_depth_histogram [{ depth, count }]       re-binned
vcf_stats_substitutions   [{ type, count }]        12 rows
vcf_stats_filters         [{ filter, count }]      includes "." as a category
vcf_stats_indel_lengths   [{ length, count }]
vcf_stats_density_bins    [float]                  variant counts per bin
vcf_stats_density_bounds  [{ contig, bin_start }]
vcf_stats_contigs         [{ contig, length, variants, per_kb, snps, indels }]
vcf_stats_report          "variants.tsv"    the downloadable export
vcf_stats_db              "variants.db"     what the table queries
```

`pass_pct` is absent when the file does not use FILTER, which is what lets the
UI omit the PASS stat rather than print a misleading 0% or 100%.

### API

Two routes in `api/v1/pipelines.py`, both modelled on their BAM counterparts:

- `POST /pipelines/vcfstats` -> `launch_vcf_stats`
- `GET /pipelines/vcfstats/report/{object_id}/{report_path}` -> the paginated
  variant table, reusing the containment checks in `get_bam_stats_report`
  verbatim (`..` and absolute paths rejected, resolved path re-checked against
  the report root).

The pagination route takes the filter parameters as query arguments --
`contig`, `pos_min`, `pos_max`, `filter`, `type`, `min_qual` -- translated into
a parameterized `WHERE` clause. This is where the BAM feature's mechanism is
genuinely replaced rather than copied: `get_bam_stats_report` reads the whole
TSV into memory and slices it, which finding 5 shows does not survive plant
scale.

Path containment still matters even though the payload is now a database:
`object_id` comes from the route and `settings.vcf_stats_dir` is fixed, so the
resolved path is re-checked against the report root exactly as
`get_bam_stats_report` does.

**Counting.** `total` reports the count after filtering, so pagination stays
correct. Because a combined predicate costs ~400 ms (finding 5), the route
accepts `skip_count=true` and the client sends it when only the page number
changed. The UI keeps the previous total in that case -- the filter set did
not change, so neither did the count.

`total_unfiltered` comes from `vcf_stats_summary.variants` in facts rather
than a second query: it is already known and never changes.

### Frontend

- **`VariantResults.tsx`** -- the tab, mirroring `BamResults.tsx`: a compute
  prompt when `vcf_stats_status !== "ok"`, the summary sections when it is, and
  a "recompute results" link in the provenance line.
- **`VariantTable.tsx`** -- the paginated, filtered table, modelled on
  `ContigTable.tsx` with filter controls added. Filter state is local
  component state; each change resets to page 0 and refetches.

  Two concessions to plant scale. The text inputs (position range, min QUAL)
  are debounced ~300 ms, so typing "1000000" is one query rather than seven.
  And a page-number change sends `skip_count=true` while a filter change does
  not -- the mechanism finding 5 calls for. React Query's `keepPreviousData`
  keeps the current page visible during the refetch instead of flashing a
  loading state on every page turn.

  Page-number pagination is kept rather than keyset: at 100 rows a maize VCF
  is ~160,000 pages, so "Page 1 of 160,000" is admittedly not a navigation
  device -- but the filters are how anyone actually finds a variant, and a
  deep offset still returns in 36 ms. Keyset pagination would complicate the
  API for a case the filters already answer better.
- **`VariantCharts.tsx`** -- the density strip and the QUAL/DP histograms, as
  inline SVG in the manner of `CoverageChart.tsx`.

`tabsFor()` in `DetailPanel.tsx` widens its existing BAM-only condition rather
than adding a second push -- one tab id, three formats:

```ts
const hasResults =
  obj.format.kind === "bam" ||
  obj.format.kind === "vcf" ||
  obj.format.kind === "bcf";
if (hasResults) tabs.push({ id: "results", label: "Results" });
```

The existing `tab === "results"` panel then dispatches on format kind,
rendering `<BamResults>` or `<VariantResults>`. Keeping one tab id matters:
`tab` is persisted in the URL alongside `?sel=`, so a shared link stays on
Results when the selection moves between a BAM and its called VCF.

Client methods in `api/client.ts`: `launchVcfStats`, `vcfStatsVariants`,
`vcfStatsDownloadUrl` -- named after their BAM equivalents.

## Visual design

Broadsheet is the app's active theme, so the tab is designed against
`frontend/src/styles/broadsheet.css` rather than `styles.css`'s classic
tokens. The agreed mockup is in `.superpowers/brainstorm/` (gitignored).

Components must use the existing class vocabulary so the theme applies without
new theme rules:

- `.section-title` for group headings -- Broadsheet renders these as 22px
  Source Serif subheads with no rule beneath.
- `.trim-table` for both tables -- 14px, `--space-2` cells, uppercase
  small-caps column headers, tabular numerals.
- `.badge` for FILTER values, picking up the ramp-paired pill treatment.
  `.badge.ready` for PASS, `.badge.error` for a failing filter, bare `.badge`
  for `.` and other values.
- Charts fill with `var(--accent)`, which Broadsheet maps to `#006786`.
- The chart-label register (11px, `0.14em` tracking, uppercase, `--ink-62`)
  comes from `.qc-chart > .section-title`, so the histograms should sit inside
  `.qc-chart` containers.

No new CSS variables. Anything that needs a Broadsheet-specific rule is a
signal the markup has drifted from the existing vocabulary.

## Multi-sample VCFs

The sample picker re-filters the table only. `bcftools query` runs once with
all samples' GT columns extracted; the picker selects which column the table
displays. The summary always describes the whole call set.

Per-sample summary statistics are deliberately out of scope. They would mean
either N `bcftools stats -s` passes stored as N fact sets, or recomputation on
every switch. This pipeline produces single-sample VCFs, so multi-sample is an
imported edge case. Revisit if it becomes a real constraint.

When `sample_count <= 1` the picker is not rendered.

## Error handling

- **Empty VCF.** Verified as real: two of the three VCFs in the database hold
  0 and 1 records. `bcftools stats` exits 0 and emits headers with no data
  rows. The runner must parse that to a summary of zero rather than raising,
  and the tab renders "No variants in this file" instead of empty charts. This
  is a normal outcome of a strict caller, not a failure.
- **Tool missing.** `tools.require(tools.bcftools())` raises before enqueueing,
  the same as the samtools gate.
- **Malformed VCF.** A non-zero `bcftools` exit raises through the existing
  `_failure(code, log_path, tool)` path, surfacing the tool's own stderr.
- **Recompute.** Re-running merges over the previous facts and rebuilds the
  database. Written to a temporary path and renamed into place, so a failed
  recompute leaves the previous working database rather than a half-built one
  the table would query.
- **Missing database.** If facts say `vcf_stats_status == "ok"` but the file
  is gone -- deleted by hand, or lost with a data directory -- the table
  reports that results need recomputing rather than 500ing. The summary still
  renders from facts, which are in Mongo and unaffected.
- **Disk.** A maize database is ~1 GB. This is derived data under the same
  data directory as everything else, and `vcf_stats_dir` sits outside
  `objects/` so it is never mistaken for content-addressed storage.

## Testing

Backend, in `backend/tests/`:

- `parse_stats` against captured real `bcftools stats` output from both
  callers -- including the empty-VCF case, which fixtures would otherwise miss.
- `rebin_distribution` on the real 805-row QUAL section.
- `variant_summary` asserting `pass_pct` is **absent** when FILTER is all `.`
  -- finding 3, and the assertion that fails if that logic regresses.
- `bin_positions` against `bin_depth` on shared allocation, confirming the
  extracted `allocate_bins` did not change BAM behaviour.
- The pagination route's filter combinations, including a filter matching
  nothing, and `skip_count=true` returning a page without a total.
- `build_variant_db` on a synthetic multi-contig set, asserting both indexes
  exist -- an index silently not created is the failure that turns a 0.3 ms
  query into a full scan, and nothing else in the suite would catch it.
- A memory ceiling test: build a database from a large synthetic stream and
  assert the handler's peak RSS stays bounded. This is the regression that
  matters -- the natural refactor of "collect rows into a list, then insert"
  passes every other test while reintroducing exactly the problem finding 5
  identified.

Per CLAUDE.md's warning about tests that silently read the host: assert the
bcftools gate by patching the probe **off** and checking the launch is
refused. The image ships bcftools installed, so asserting the available
direction passes whether or not the patch worked.

Frontend verification is manual at localhost:5173 against
`DRR1066343.bcftools.vcf.gz`, which has 6,641 variants across 17 contigs and
exercises the density strip, per-contig table, and pagination. Restart the
worker after handler changes -- `docker compose restart worker` -- since it
does not hot-reload.

That file is too small to expose the problem finding 5 is about, so the
performance claims must also be checked against a plant-scale file before this
is called done: ingest an *Arabidopsis* resequencing VCF (~0.7M variants, the
smallest genuinely plant-scale case) and confirm compute time, database size,
and that paging and filtering stay responsive. A tab that feels instant on
6,641 yeast variants proves nothing about the case this design exists for.

## Actions tab

Per CLAUDE.md, a new computation needs `suggestion_service.py` revisited. This
adds no new *pipeline* -- it is a compute action on an existing file, like BAM
results, which has no suggestion card either. No rule changes expected;
confirmed by the absence of a `run_bam_stats` card.

## Out of scope

- Variant annotation (snpEff/VEP) and consequence prediction
- Comparing two VCFs against each other
- Genome-browser-style locus visualization
- Per-sample summary statistics
- Editing or filtering the VCF into a new file -- this tab reports, it does
  not derive
