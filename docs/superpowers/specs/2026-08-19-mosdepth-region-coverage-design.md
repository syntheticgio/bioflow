# Mosdepth per-region coverage-depth (issue #626)

Date: 2026-08-19. Design only — implementation is tracked separately.

## Problem

`bam_stats` (samtools-based) reports *global* alignment statistics — per-contig
mean depth, breadth of coverage, mapping rate, insert size — and builds a depth
**histogram** and **cumulative coverage curve** from `samtools depth -a` binned
into a fixed 1000 bins (`bam_stats_runner.BIN_COUNT`). That is a whole-genome /
per-contig birds-eye. What users actually want when they ask "did this region
get enough depth?" — a target panel, a gene of interest, or a set of exons — is
**per-region** coverage, and `bam_stats` cannot answer it: it is per-contig, not
per-region.

mosdepth is the standard, dramatically faster tool for this. Its `--by
<regions.bed>` mode emits per-region mean/min/max depth and breadth directly.
(It also does whole-genome windowed depth faster than `samtools depth`, which is
why it can *replace* the existing birds-eye sampling while adding the region
view.)

## Grounding — what already exists (and what does not)

- **Whole-genome windowed coverage already exists.** `bam_stats_runner.bin_depth`
  / `DepthHistogram` / `cumulative_coverage`, plus the frontend
  `ContigDepthChart` / `DepthHistogramChart` / `CumulativeCoverageChart`, already
  render the birds-eye. A second windowed view would be redundant. This is why
  the agreed scope is *region mode only* for new capability; the windowed
  birds-eye is only touched as a speed refactor.
- **BAMs are blobs, resolvable on demand.** `feature_coverage_handlers.run_feature_coverage`
  and `variant_handlers` materialize a BAM via `_resolve_blob(ctx.payload, "bam")`
  and symlink it into the workdir. An on-demand coverage card can do the same —
  no sidecar BAM needs to be retained.
- **Region sets already have a home.** `feature_coverage` consumes an *annotation*
  that may be `gff` or `bed` (`annotation_format` in the payload). So
  "user-uploaded BED" is exactly an existing BED annotation object, and
  "annotation-derived regions" is converting a GTF/GFF's gene features into
  intervals (e.g. `bedtools merge`, or min-start/max-end per `gene_id`), reusing
  `feature_coverage`'s annotation resolution and the already-present `bedtools`
  dependency.
- **The card gate pattern is known.** Cards gate on
  `obj.format.kind is FormatKind.BAM` (`build_quantify_card`,
  `build_feature_coverage_card`). A new coverage card follows that exactly.

## What this is NOT

- Not a new whole-genome windowed card (that overlaps `bam_stats`). The windowed
  birds-eye is only refactored for speed (MQ-4), not re-added.
- Not a new IGV-style track viewer. Region facts feed the *existing*
  `DepthHistogramChart` + a per-region table.
- Not a replacement of `bam_stats` as a user-facing capability — `bam_stats`
  keeps its per-contig stats; mosdepth only swaps its depth *sampling* and adds
  the region view.

## Design decisions (agreed before writing)

1. **Scope: region mode only for new capability.** mosdepth's per-region (`--by`)
   coverage is the gap `bam_stats` leaves. No new windowed card.
2. **Region source: both.** User-uploaded BED (a BED annotation object) **and**
   annotation-derived gene intervals (from a GTF/GFF). One card, two region-set
   inputs.
3. **Relationship to `bam_stats`: both.** (a) Refactor `bam_stats`'s *depth
   sampling* to use mosdepth instead of `samtools depth -a` (faster at genome
   scale; fact schema unchanged so existing charts/reports keep working). (b) Add
   a **new** "Coverage depth" card + handler that runs mosdepth `--by` for
   per-region coverage.
4. **Output: facts feeding existing charts.** Per-region depth facts merge onto
   the BAM object and feed the existing `DepthHistogramChart` (distribution of
   per-region mean depths) plus a per-region table (mirroring
   `feature_coverage`'s feature table).

## Requirements

**MQ-1 — Tool registration.** `backend/app/pipelines/tools.py` gains a mosdepth
probe (`def mosdepth() -> Tool`, mirroring `samtools()`) and a
`TOOL_META["mosdepth"]` entry: `pipelines=(PipelineType.UTILITY,)` (mirror
samtools' UTILITY membership; QC optional), `one_liner`, `summary`,
`strengths`, and the documented fields `homepage`
(`https://github.com/brentp/mosdepth` — verify against the repo), `citation`
(Pedersen BS, Quinlan AR. *mosdepth: Quick coverage calculation for genomes and
exomes.* Bioinformatics 2018 — verify), `license="MIT"` (verify), and `usage`
describing BioFlow's use (region coverage + the `bam_stats` depth source). Must
pass `test_every_tool_is_documented`.

**MQ-2 — Install.** `backend/scripts/install-mosdepth.sh` (mirror
`install-quast.sh`'s verified-install style) installs mosdepth into the image;
the image build invokes it (mirror quast). Tool is `BUNDLED`
(`Delivery.BUNDLED`, the default) — no on-demand download. `config.py` gains
`mosdepth_path: str = "mosdepth"`.

**MQ-3 — mosdepth runner (pure functions).** New
`backend/app/pipelines/mosdepth_runner.py`, pure over strings/paths (mirror
`bam_stats_runner` / `feature_coverage_runner`):
- `build_region_command(*, mosdepth_path, bam, regions_bed, prefix)` →
  `mosdepth --by <regions.bed> <prefix> <bam>`.
- `build_depth_source_command(*, mosdepth_path, bam, prefix)` → the mosdepth
  whole-genome command that yields per-base/quantized depth (replacement for
  `samtools depth -a`), for MQ-4.
- `parse_regions(text)` → list of per-region dicts
  `{region, length, mean_depth, min_depth, max_depth, breadth}` from
  `<prefix>.regions.bed.gz` (mosdepth emits mean/min/max columns; breadth from
  the covered fraction or as reported).
- `parse_summary(text)` → from `<prefix>.mosdepth.summary.txt`.

**MQ-4 — bam_stats depth-source refactor (low-risk).** `run_bam_stats`
(`align_handlers.py`) replaces `build_depth_command` / `samtools depth -a` with
mosdepth's per-base/quantized output, then feeds the **existing** pure functions
`bin_depth` / `DepthHistogram` / `cumulative_coverage` unchanged. The resulting
`bam_stats_*` facts and the `bam_stats_dir` TSV/report are byte-shape-identical,
so `ContigDepthChart` / `DepthHistogramChart` / `CumulativeCoverageChart` and
`get_bam_stats_report` are untouched. `samtools coverage` + `idxstats` are
unchanged.

**MQ-5 — Region handler.** New `backend/app/queue/mosdepth_handlers.py` with
`run_mosdepth` (`HandlerMode.SUBPROCESS`, mirror `run_feature_coverage`): resolve
`bam` + region-set blobs via `_resolve_blob`, run `mosdepth --by`, write a JSON
report to `settings.mosdepth_dir / str(bam_id)`, and return facts for a
`_apply_*_coverage`-style merge. Read-only like `run_bam_stats`; no derived
object.

**MQ-6 — Region source: uploaded BED.** When the card launches with a `bed_id`,
resolve that BED annotation object's blob as the `--by` regions file. Available
whenever the project has a BED annotation object (same detection `feature_coverage`
uses for `annotation_format == "bed"`).

**MQ-7 — Region source: annotation-derived.** When launched with an
`annotation_id` (GTF/GFF) + `derive_regions=True`, convert the annotation to one
interval per `gene_id` spanning the feature's min start..max end (exon/CDS
granularity is a follow-up; gene-span is the v1). Reuse `feature_coverage`'s
annotation resolution and `bedtools` (already a dependency) for the GTF→BED
conversion/merge.

**MQ-8 — Region card.** `build_coverage_depth_card(obj)` in `suggestion_service.py`:
returns `None` unless `obj.format.kind is FormatKind.BAM`; `UNAVAILABLE` (with
reason) when mosdepth is not installed or the project has no region source (no
BED annotation and no annotation to derive from); `AVAILABLE` otherwise, with
`launch.endpoint="/pipelines/mosdepth"` and body
`{bam_id, bed_id? | annotation_id?, derive_regions?}`. Clearly distinct from
`bam_stats` (`kind="coverage_depth"` vs the alignment-stats card).

**MQ-9 — Region facts (bounded, mirror feature_coverage).** Merged onto the BAM
object: `coverage_depth_status`, `coverage_depth_tool_version`,
`coverage_depth_computed_at`, `coverage_depth_region_count`,
`coverage_depth_median_mean_depth`, `coverage_depth_median_breadth`, counts of
regions below the 1x/10x/30x thresholds (`coverage_depth_regions_below_*`),
`coverage_depth_source_id` (the bed_id or annotation_id), and a **bounded**
per-region table (cap rows, mirror `feature_coverage`'s bounded facts) feeding
the histogram + table.

**MQ-10 — Storage + serving.** Region report JSON under
`settings.mosdepth_dir / str(bam_id)`; served via new
`GET /pipelines/mosdepth/{object_id}/report` (mirror
`GET /feature-coverage/{object_id}/report`). `config.py` adds `mosdepth_dir`.

**MQ-11 — Endpoint + launch.** `POST /pipelines/mosdepth`
(`MosDepthRequest`: `bam_id`, `bed_id?`, `annotation_id?`, `derive_regions?`) →
`pipeline_service.launch_mosdepth` (mirror `launch_feature_coverage`, with a
`dedup_key` over the bam + region-set blob shas).

**MQ-12 — Frontend.** New "Coverage depth" card on the BAM object's Actions tab
(mirror the feature-coverage card), and a region-coverage view that reuses
`DepthHistogramChart` (region mean-depth distribution) + a per-region table
(mirror `feature_coverage`'s feature table). No new viewer component.

**MQ-13 — Provenance.** Region facts carry the mosdepth tool version,
`computed_at`, and the exact region-source id (bed_id or annotation_id), so a
coverage-depth result is traceable to the BAM and the region set that produced
it (mirror `feature_coverage_annotation_id`).

**MQ-14 — Tests.**
- `backend/tests/pipelines/test_mosdepth_runner.py`: command construction +
  `parse_regions` / `parse_summary` against fixtures (mirror
  `test_bam_stats_runner.py` / `test_feature_coverage_runner.py`).
- `backend/tests/queue/test_mosdepth_handlers.py`: handler resolves blobs, runs
  mosdepth, merges facts.
- `backend/tests/api/test_mosdepth_reports.py`: endpoint + report route (mirror
  `test_feature_coverage_reports.py` / `test_bam_stats_reports.py`).
- `backend/tests/services/test_suggestion_service.py`: `build_coverage_depth_card`
  is `None` for non-BAM, `UNAVAILABLE` without a region source, `AVAILABLE` with
  one; distinct `kind`.
- bam_stats refactor: existing `test_bam_stats_*` continue to pass unchanged
  (MQ-4's invariant), plus a focused test asserting the fact schema is identical
  whether sampled by samtools or mosdepth.
- `test_every_tool_is_documented` covers the meta (MQ-1).

**MQ-15 — Docs / help page.** `TOOL_META` auto-renders on the Software help page
(`/help/software`); no manual page work. Release notes: this is a `feat:`
implementation PR (not the spec doc), categorized under `type:feature`.

## Success criteria (from the issue, mapped to requirements)

1. mosdepth installs and passes `test_every_tool_is_documented` → **MQ-1, MQ-2**.
2. Coverage calculation runs end-to-end against a BAM, producing **per-region**
   depth data → **MQ-3, MQ-5, MQ-6, MQ-7** (+ MQ-11, MQ-10).
3. Suggestion card is available for any completed alignment and clearly
   distinguished from the samtools-based `bam_stats` card → **MQ-8** (card) and
   **MQ-12** (distinct kind + view).

Plus the implicit refactor criterion: the `bam_stats` birds-eye still renders
identically after MQ-4 — covered by MQ-4 + existing tests.

## Risks

- **bam_stats coupling (MQ-4).** Mitigated by swapping only the *depth source*
  and reusing the existing pure binning/histogram/cumulative functions; guarded
  by unchanged `bam_stats_*` tests.
- **Annotation→region semantics (MQ-7).** Gene-span (min start..max end) is the
  v1; exon/CDS granularity is a follow-up. Must not double-count overlapping
  features.
- **mosdepth output-format stability.** `regions.bed.gz` column order is pinned
  by `parse_regions` unit tests on a fixture.
- **Large region sets / big BAMs.** mosdepth is built for this; note CRAM
  indexing if a CRAM is ever a BAM stand-in.
