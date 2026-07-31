# Variant Results tab

A Results tab for VCF files: what the call set looks like as a whole, and a
browsable table of the individual calls.

The BAM Results tab is the template. That feature already established every
mechanism this one needs -- a read-only compute job that merges facts onto the
object, a TSV report on disk paginated server-side, and a tab that offers a
"Compute results" button until the facts exist. This spec follows it
deliberately, and calls out the three places where a VCF genuinely differs.

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

**5. Extraction is effectively free.** `bcftools query` over 6,641 variants
into TSV took 8 ms. No progress reporting or chunking is needed for the table.

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
- `variants_tsv(rows)` / `coerce_tsv_value(column, value)` -> the report
  format and its inverse, matching `contigs_tsv`'s contract exactly so the
  pagination route can type cells by column name.

`VARIANTS_TSV_COLUMNS` is the analogue of `CONTIGS_TSV_COLUMNS`:
`chrom, pos, ref, alt, qual, filter, dp, gt`.

### `backend/app/queue/variant_handlers.py` -- `run_vcf_stats`

A new handler beside the existing `call_variants`. Read-only, like
`run_bam_stats`: derives no objects, returns facts for the applier to merge
and writes one TSV to `settings.vcf_stats_dir`.

`vcf_stats_dir` is a new `Settings` property, added alongside the existing
`bam_stats_dir` and following its rationale -- outside `objects/`, since a
regenerable report is not content-addressed storage.

Sequence: materialize the VCF (and `.tbi` if present) into the workdir,
`bcftools stats` -> parse -> re-bin -> `bcftools query` -> write TSV -> return
facts. Progress phases `stats` / `query` / `report`, matching how
`run_bam_stats` reports.

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
vcf_stats_report          "variants.tsv"
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
`contig`, `filter`, `type`, `min_qual`, `q` -- applied server-side over the
TSV before slicing. This is the one place the BAM feature's mechanism is
genuinely extended rather than copied: `get_bam_stats_report` does a plain
`offset:offset+limit` slice with no filtering.

Filtering a TSV in Python is acceptable here and does not need an index: the
file is one line per variant, extraction of 6,641 rows took 8 ms, and this is
a single-user local tool. `total` in the response reports the count *after*
filtering, so pagination stays correct; the unfiltered count is returned
alongside as `total_unfiltered` so the UI can show "1,210 of 6,641".

### Frontend

- **`VariantResults.tsx`** -- the tab, mirroring `BamResults.tsx`: a compute
  prompt when `vcf_stats_status !== "ok"`, the summary sections when it is, and
  a "recompute results" link in the provenance line.
- **`VariantTable.tsx`** -- the paginated, filtered table, modelled on
  `ContigTable.tsx` with filter controls added. Filter state is local
  component state; each change resets to page 0 and refetches.
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
- **Recompute.** Re-running merges over the previous facts. No staleness
  tracking: the facts describe immutable file content.

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
  nothing.

Per CLAUDE.md's warning about tests that silently read the host: assert the
bcftools gate by patching the probe **off** and checking the launch is
refused. The image ships bcftools installed, so asserting the available
direction passes whether or not the patch worked.

Frontend verification is manual at localhost:5173 against
`DRR1066343.bcftools.vcf.gz`, which has 6,641 variants across 17 contigs and
exercises the density strip, per-contig table, and pagination. Restart the
worker after handler changes -- `docker compose restart worker` -- since it
does not hot-reload.

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
