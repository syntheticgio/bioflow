# mosdepth coverage depth — design

Design for [#626](https://github.com/syntheticgio/bioflow/issues/626),
"Add per-region/windowed coverage-depth analysis (mosdepth)".

This design adds **per-base / per-window / per-region read-depth analysis**
alongside the existing `bam_stats` global statistics. `bam_stats` already
reports mapping rate, insert-size distribution, and per-contig read counts,
but it cannot answer "how uniform is my coverage" or "how deep is this
region" — questions only a depth calculator answers. `mosdepth` fills that
gap. The design follows the **read-only job template** established by
`feature_coverage` (a BAM in, a JSON report + merged facts out, no derived
objects), because mosdepth is the same shape of computation.

## What exists today

Verified against this worktree on 2026-08-19:

- **`bam_stats_runner.py`** computes global samtools-derived stats: mapping
  rate, insert size, error rate, and per-contig read counts
  (`contigs_tsv`). It deliberately does **not** compute depth — there is no
  per-window or per-region depth anywhere in the app today.
- **`feature_coverage_handlers.py`** is the closest structural analog. It is a
  `SUBPROCESS` job (`@handler("feature_coverage", mode=HandlerMode.SUBPROCESS,
  job_class=JobClass.COMPUTE, resources=JobResources(cpu=1, mem_mb=1024,
  io=IoClass.HEAVY), max_attempts=2)`) that runs `bedtools coverage`, writes
  one JSON report to disk, and merges summary facts onto the BAM via
  `_apply_feature_coverage` in `queue/results.py`. mosdepth mirrors this
  shape exactly.
- **`gc_tracks.py`** already defines the windowing scheme the issue wants to
  reuse: `WINDOW_COUNT = 500` and `MIN_WINDOW_BASES = 100`, used as
  `min(WINDOW_COUNT, length // MIN_WINDOW_BASES)` windows per contig in
  `gc_tracks.windows()`. A contig shorter than 100 bp yields zero windows.
  The issue attributes this scheme to "CLAUDE.md's registry-audit notes";
  it actually lives in `gc_tracks.py`. This design reuses those constants so mosdepth's windows
  match the rest of the app's windowed track rendering.
- **`config.py`** exposes `bam_stats_dir`, `feature_coverage_dir`,
  `vcf_stats_dir` as `@property` dirs under `bioinfo_home` — derivative,
  regenerable output kept *outside* `objects/`. mosdepth needs the same.
- **`tools.py`** `ToolMeta` requires `pipelines`, `summary`, `strengths`,
  `homepage`, `citation`, `license`, `usage`; `test_every_tool_is_documented`
  enforces the four bibliographic fields. **`bedtools` is `PipelineType.
  UTILITY`** — the precedent for a card-invoked read-only tool.
- **`suggestion_service.py`** has ~18 `build_*_card` functions returning
  `SuggestionCard | None`, registered in `CARD_BUILDERS`. `build_feature_
  coverage_card` is the closest analog (a BAM card, read-only, optionally
  taking an annotation).
- **`Dockerfile`** installs tools from Debian trixie; **mosdepth is not yet
  installed** and must be added to the `apt-get install` block.

## Decisions (with rationale)

**D1. `PipelineType` = `UTILITY`.** Do **not** add a `COVERAGE`
`PipelineType`. mosdepth is launched only via a suggestion card, never
auto-selected by the pipeline thread, so its `pipelines` membership is purely
cosmetic (Software help page + tool selector). A new enum member would create
an empty tool-selector screen. `UTILITY` is what `bedtools` uses for the same
kind of card-invoked read-only tool, and it satisfies `test_every_tool_is_
documented` with no API/UI churn.

**D2. Card category = `ASSEMBLY_QC`.** Mirrors `build_feature_coverage_card`
exactly (a post-align read-coverage card), so no new frontend category
infrastructure is needed. Distinctness from `bam_stats` is carried by the
card title/why/description, not by a new section. *Alternative:* a new
`COVERAGE` category — the frontend renders `card.category` as a plain label
(`PipelineSuggestions.tsx`), so it is cheap, but it adds a section header for
one card. Default: `ASSEMBLY_QC`.

**D3. Output shape = per-window depth facts + summary facts, not summary-only.**
Windowed mode reuses the `gc_tracks` 500-window/100bp scheme so the resulting
per-window depth feeds `BirdsEyeCoverageChart`-style track viz without a new
scheme. Summary facts (mean/median depth, % bases ≥1×/5×/10×, total bases,
window count, mode) go on the object for fast listing; the full per-window
array is served via the report endpoint for large genomes.

**D4. Two modes.** Default **whole-genome windowed**: a windows BED generated
from the BAM's contig lengths (per-contig `WINDOW_COUNT` windows, floored at
`MIN_WINDOW_BASES`) passed to mosdepth via `--by <windows.bed>`. Optional
**BED-region mode**: `--by <regions.bed>` when the launch carries a regions
`DataObject`, producing per-region depth instead of uniform windows.

**D5. Handler resources = `mem_mb=2048`.** `feature_coverage` uses 1024, but
mosdepth with thousands of windows holds one accumulator per window and
benefits from more headroom; 2048 is safe and still small.

**D6. Card availability = any completed BAM with mosdepth installed.** No
annotation required (unlike `feature_coverage`), matching "available for any
completed alignment." When a regions `DataObject` of the same reference
resolves, the card offers region mode; otherwise it offers windowed mode.

## Staging

Three stages, one PR each, each independently mergeable and green.

| Stage | Delivers | New install? |
|---|---|---|
| 0 | Tool registration: probe + `TOOL_META` + Dockerfile | yes (mosdepth) |
| 1 | End-to-end windowed coverage: runner, config dir, handler, launch, endpoint, results applier, card | no |
| 2 | BED-region mode + track-style frontend viz | no |

Stage 0 closes the "passes `test_every_tool_is_documented`" and "visible on
`/help/software`" criteria. Stage 1 closes "runs end-to-end" and "suggestion
card available for any completed BAM, distinguished from bam_stats." Stage 2
closes the track-viz half of the issue.

## Stage 0 — tool registration

**R0-1.** `tools.py` gains a `mosdepth()` probe following the existing pattern
(`mosdepth --version`, captured and parsed for a version string), cached by
`tool_cache` like every other probe.

**R0-2.** `TOOL_META` gains a `mosdepth` entry with `pipelines=
(PipelineType.UTILITY,)`, a `summary`, `strengths`, and the four bibliographic
fields filled in. License and citation are **verified against mosdepth's own
repository at implementation time, not recalled** (CLAUDE.md rule):
MIT; Pedersen BS, Quinlan AR. *Mosdepth: quick coverage calculation for
genomes and exomes.* Bioinformatics. 2018;34(5):867–868.
doi:10.1093/bioinformatics/btx699; homepage https://github.com/brentp/mosdepth.
`usage` describes behavior ("computes per-window and per-region read depth
for the coverage report"), not flag strings.

**R0-3.** The Dockerfile `apt-get install` block (currently lines ~95–124)
adds `mosdepth`, with a build-time comment recording the trixie version,
matching the style of the existing tool comments (e.g. "samtools 1.21").

**R0-4.** A user opening `/help/software` sees mosdepth listed with version,
license, citation, and usage — the observable outcome of this stage, and the
first success criterion.

## Stage 1 — end-to-end windowed coverage

The question this answers, which nothing in the app answers today: **"how
uniform is my read coverage, and where are the dropouts?"** `bam_stats` is
alignment-wide; it never computes depth.

**R1-1.** A "Per-window coverage" card (kind `coverage`, category `ASSEMBLY_QC`)
appears on a completed BAM object when mosdepth is installed. Its title and
why text contrast it with `bam_stats` ("depth across the genome, not just
alignment-wide stats").

**R1-2.** When mosdepth is not installed, the card renders unavailable with a
reason naming the missing tool — same shape as existing cards.

**R1-3.** `backend/app/pipelines/mosdepth_runner.py` is a pure module (the
`quast_runner.py` / `feature_coverage_runner.py` model):
- `build_command(bam, windows_bed) -> list[str]` — unit-tested without the
  binary. `mosdepth --by <windows.bed> <prefix> <bam>` plus `--no-per-base`
  unless per-base is wanted, and `-t 1` (single-threaded; the job already
  caps cpu at 1).
- `build_windows_bed(contig_lengths) -> list[tuple]` — reuses `WINDOW_COUNT`
  and `MIN_WINDOW_BASES` from `gc_tracks.py` and tiles each contig the same
  way `gc_tracks.windows()` does: `window_count = min(WINDOW_COUNT,
  length // MIN_WINDOW_BASES)` windows per contig (so a contig shorter than
  100 bp yields zero windows, not one padded window), width `length //
  window_count`. This matches the app's existing track axis exactly. Pure
  and unit-tested.
- `parse_summary(summary_txt) -> dict` and `parse_regions(regions_bed_gz) ->
  dict` — pure parsers turning mosdepth's `.mosdepth.summary.txt` and
  `.regions.bed.gz` into the report dict, unit-tested against captured
  fixture output.
- `summarize(report) -> dict` — derives the summary facts (mean/median depth,
  % bases ≥1×/5×/10×, total bases, window count).

**R1-4.** `config.py` gains `coverage_dir` (a `@property` under
`bioinfo_home`, derivative/regenerable, outside `objects/`), mirroring
`feature_coverage_dir`.

**R1-5.** `backend/app/queue/mosdepth_handlers.py` mirrors
`feature_coverage_handlers.py`: `@handler("coverage", mode=SUBPROCESS,
job_class=COMPUTE, resources=JobResources(cpu=1, mem_mb=2048, io=HEAVY),
max_attempts=2)`. It requires `bam_id`, resolves the BAM, generates the
windows BED from the BAM's contig lengths (via `samtools idxstats` or the
stored `contigs_tsv` facts), runs the command, parses output, and returns
`{"object_id": ..., "facts": {...}, "report_path": ...}`. Imported for
side-effects in `handlers.py` like every other handler module.

**R1-6.** `pipeline_service.py` gains `launch_coverage(bam, owner) -> JobOut`
mirroring `launch_feature_coverage` (eligibility check on the BAM, payload
assembly, `enqueue`). `api/v1/pipelines.py` gains `POST /pipelines/coverage`
(returns `JobOut`, 201) and `GET /pipelines/coverage/{object_id}/report`
(returns the stored JSON report), mirroring the feature-coverage routes.

**R1-7.** `queue/results.py` gains `_apply_coverage` mirroring
`_apply_feature_coverage`: merges the summary facts onto the BAM object,
read-only, no files ingested. Summary fact keys namespaced `coverage_*`
(`coverage_mean_depth`, `coverage_median_depth`, `coverage_bases_ge_1x`,
`coverage_bases_ge_5x`, `coverage_bases_ge_10x`, `coverage_total_bases`,
`coverage_window_count`, `coverage_mode="windowed"`).

**R1-8.** The results view (BamResults) shows the coverage summary facts
beside the existing `bam_stats` panel, so the two are visible together and
clearly distinct. The full per-window array is reachable via the report
endpoint for large genomes.

## Stage 2 — BED-region mode + track viz

**R2-1.** `build_coverage_card` offers **region mode** when a regions
`DataObject` (BED/GFF of the same reference) resolves for the BAM, passing
its id in the launch body; otherwise it offers windowed mode. The card's why
text names which mode it will run.

**R2-2.** `build_command` accepts an optional `regions_bed`; when present it
emits `--by <regions.bed>` instead of the generated windows BED, and the
parser records per-region depth (`coverage_mode="regions"`).

**R2-3.** The per-window depth fact is stored in a shape `BirdsEyeCoverageChart`
(or a dedicated CoverageChart panel) can render as a depth track across
contigs, reusing the `gc_tracks` windowing so the axis matches existing GC
tracks. A user sees a coverage-depth track alongside GC content, not just the
headline numbers.

**R2-4.** Region-mode results render as a sortable per-region depth table
(defaulting to lowest depth first), with the summary stats above it.

## Cross-cutting obligations (each has bitten this repo before)

- **RS-1.** Every new launch function is classified in `node_types.py`
  (a `NodeTypeSpec` in the `NODE_TYPES` dict plus a `_launch_*` adapter),
  and the PR runs the node-types exhaustiveness test that compares
  `launch_function_names()` against `NODE_TYPES`, not just the one test a
  gap names (CLAUDE.md #355/#366 trap).
- **RS-2.** Any stored sidecar/report role is present in its registry with the
  exhaustiveness test updated (the STAR `_SIDECAR_ROLES` trap).
- **RS-3.** The suggestion rule has tests in
  `tests/services/test_suggestion_service.py` **in both directions**,
  including "card flips to unavailable when the probe is patched off" —
  patching `spec_for`-style seams, not frozen-at-import function objects
  (CLAUDE.md trap).
- **RS-4.** Before calling the stage done, the rule is checked against the
  **real database** (`docker compose exec api python -c ...` with a real
  aligned BAM), not only fixtures — the protein.faa/duplicate-assembly lesson.
- **RS-5.** The handler reads the BAM's contig lengths from a resolved source
  (stored `contigs_tsv` facts or `samtools idxstats`), never assuming a
  particular ordering, and writes the windows BED atomically before launch.

## Error handling

- Tool-missing at launch is prevented by card availability (RS-3 direction
  tests); tool failure at run time surfaces through the normal job-failure
  path with stderr captured, like existing runners.
- A BAM with no resolvable contig lengths (e.g. an unsorted or index-less
  BAM) fails the job with an honest reason, not a silent empty report — the
  same contract `bam_stats` enforces via `_check_bam_stats_callable`.
- Malformed regions input (a BED mosdepth rejects) fails the job with the
  tool's own error preserved.

## Testing

- Pure command-builders, the windows-BED generator, and parsers: unit tests
  with fixture outputs captured from real mosdepth runs.
- Suggestion rule: both-direction tests per RS-3.
- Registry / node-type exhaustiveness: per RS-1/RS-2.
- Real-database spot check per RS-4 before each stage's PR merges.
- Backend suite runs via `backend/run-worktree-tests.sh` from the worktree
  (private Mongo, per CLAUDE.md); UI verification is manual against the
  worktree stack on 5273. `worker` does **not** hot-reload, so
  `docker compose restart worker` is required after editing the handler.

## Verify before implementing (not asserted above)

1. **Debian trixie `mosdepth` package name and version**, read from the
   trixie archive at implementation time; record it in the Dockerfile comment.
2. **mosdepth license and citation**, read from the project's own repository,
   per R0-2.
3. **Exact flag set** the command-builders emit (`--by`, `--no-per-base`,
   `-t`, `--quantize` if used) accepted by the trixie mosdepth version.
4. **`--by` behavior** with a windows BED and a regions BED on the installed
   version, against a real BAM the app produced — confirm the `.regions.bed.gz`
   and `.mosdepth.summary.txt` column layouts the parsers expect.
5. **`BirdsEyeCoverageChart`'s expected fact/array shape**, so R2-3 stores
   windowed depth in a form the chart renders without a new data contract.

## Out of scope

- Replacing or folding into `bam_stats`. The two answer different questions;
  `bam_stats` stays as the alignment-wide stats, mosdepth adds depth.
- Per-base BED as a downloadable artifact (mosdepth's `.per-base.bed.gz`). The
  report endpoint serves the parsed windowed/region data; exporting the raw
  per-base BED is a separate concern.
- Multi-BAM or cross-sample coverage comparisons.
- Calling mosdepth from the align thread (auto-run). It is a deliberate,
  user-launched card, like `feature_coverage`.
