# BAM Results tab

A per-object Results tab for BAM files: what the alignment actually produced,
including a birds-eye coverage view across the reference and a complete
per-contig table.

## Problem

Alignment results are thin and misplaced today.

`AlignmentReport` renders four flagstat numbers inside the **QC** tab. That is
the wrong home: QC describes the reads that went in, and these numbers describe
the alignment that came out.

Worse, those numbers only exist for BAMs this app produced. `flagstat` runs
inside the align pipeline (`align_handlers.py`), so an **imported** BAM has no
`total_reads`, no `mapped_pct`, and `AlignmentReport` returns `null` — the
component renders nothing at all.

Nothing anywhere shows coverage, which is the question a BAM is usually opened
to answer.

## What already exists

Worth stating, because it decides what is free and what needs computing.

From the header parse at ingest (`storage/parsers.py:80`), for every BAM
regardless of origin:

- `sort_order`, `sam_version`
- `reference_count`, `reference_names`, `reference_lengths`, `reference_total_length`
- `read_group_count`, `sample_names`, `platforms`, `program_chain`
- `has_index`, `read_length_min`/`max`, `paired`

From a bounded 200k-record sample (`storage/sequence_stats.py:242`): base
composition, per-position quality, GC — and it already accumulates
`mapq_sum`/`mapq_n` while discarding the distribution's shape.

From `flagstat`, **only for pipeline-produced BAMs**: total/mapped/properly
paired/duplicate counts, `aligned_by`, `aligner_version`.

## Design

### The job: `run_bam_stats`

A read-only compute job following `run_qc` exactly: no derived objects, results
merged onto the object's `facts`, plus one report file on disk.

Launched from a **Results** button in the panel header, shown when
`status === "ready" && format.kind === "bam"`, beside the existing
QC/Align/Call variants buttons. Dedup key `bamstats:<object_id>` — the job takes
no parameters, so a repeat over unchanged content is the same run. Registered as
`run_bam_stats` with a `_apply_run_bam_stats` result applier in
`queue/results.py`.

#### Prerequisites

`samtools idxstats` and `samtools coverage` both require a coordinate-sorted,
indexed BAM.

The launch service **refuses with an actionable message rather than
auto-chaining**, matching the documented decision in `launch_variant_calling`
(`pipeline_service.py:1108`): an actionable "index it first" beats a job that
sits blocked behind work the user did not ask for.

- Not coordinate-sorted (`sort_order != "coordinate"`) → `ValidationError`
  naming the problem.
- Sorted but no `.bai` sidecar → `ValidationError` with
  `details.needs = "index_bam"`, exactly as variant calling reports it.

#### What it runs

Three bounded passes:

| Pass | Cost | Produces |
|---|---|---|
| `samtools idxstats` | index-only, instant | reads + unmapped per contig |
| `samtools coverage` | one pass | per-contig mean depth, covered bases, % covered, mean baseQ/mapQ |
| `samtools depth`, binned | one pass | genome-wide binned depth |

Insert-size and MAPQ histograms are **not** a fourth traversal. They are added
to the existing sampled pass in `sequence_stats.alignment_stats`, which already
decodes 200k records and already sums MAPQ — capturing the distribution instead
of only the mean is nearly free.

### Data split

The full per-contig table can be tens of thousands of rows for a draft
assembly. The visualization needs only a fixed-size summary. So they are stored
differently.

#### In `facts`, under a `bam_stats_` prefix

Everything the tab draws without a second request:

- `bam_stats_status`, `bam_stats_tool_version`, `bam_stats_computed_at`
- `bam_stats_coverage_bins` — binned depth across the whole reference, plus
  contig-boundary offsets for separators and axis labels
- `bam_stats_contigs_top` — top-N contigs by reads: the table's first page and
  the summary
- `bam_stats_summary` — genome-wide mean depth, % covered ≥1×/≥10×/≥30×, total
  contigs, mapped/unmapped totals
- `bam_stats_cumulative` — fraction of reference at ≥X depth
- `bam_stats_insert_size`, `bam_stats_mapq` — histograms
- `bam_stats_report` — filename of the full TSV

**Binning:** ~1000 fixed bins across the whole reference, so the array is a
constant size regardless of genome size. A contig shorter than one bin still
gets one bin, so small contigs never vanish from the plot.

#### On disk

The complete per-contig table, every contig, no truncation:

```
settings.bam_stats_dir / <object_id> / contigs.tsv
```

`bam_stats_dir` is a new `Settings` property beside `qc_reports_dir`, with the
same rationale: regenerable and derivative, so content-addressing it would buy
deduplication of something never shared and cost a blob record per run.

#### Serving it

`GET /pipelines/bamstats/report/{object_id}/{report_path}`, reusing the
path-traversal guards from `get_qc_report` (`api/v1/pipelines.py:140`) verbatim —
reject `..` and absolute paths outright, then resolve and re-check against the
root so a symlink cannot escape either.

Two modes:

- `?download=1` — the TSV as an attachment.
- default — paginated JSON with `offset`, `limit`, `sort`. The TSV is
  line-oriented, so a page is a bounded read rather than a full parse.

Unlike QC reports, this file is **generated by this app from numeric samtools
output**, not third-party HTML embedding read-derived strings. It is served as
`text/tab-separated-values` with `X-Content-Type-Options: nosniff` and an
attachment disposition; the sandbox CSP that `get_qc_report` needs does not
apply, because nothing here is rendered as a document.

### The tab

`{ id: "results", label: "Results" }` added to `TABS` in `DetailPanel.tsx:225`,
present only for BAMs. Order: QC, **Results**, Metadata, Actions — Results sits
beside QC because they answer adjacent questions.

Top to bottom:

1. **Alignment summary** — `AlignmentReport`, moved here from the QC tab. It
   also gains a fallback: when flagstat facts are absent, the equivalent numbers
   come from `bam_stats`, so it renders for imported BAMs too. This closes the
   gap described under Problem.
2. **Coverage across the reference** — the birds-eye plot. Contigs laid end to
   end, depth binned, boundaries marked, log-scale toggle. A summary by
   construction; it does not attempt to be a genome browser.
3. **Cumulative coverage curve** — fraction of the reference at ≥X depth. The
   plot that answers "did I sequence deep enough."
4. **Per-contig table** — paginated and sortable by reads, depth, or coverage,
   with a Download TSV button hitting the report route.
5. **Insert size** and **MAPQ** histograms, side by side. Insert size only when
   `paired`.
6. **Provenance** — aligner and version, `program_chain`, reference name, sample
   names, platforms, sort order, index status.

**Empty state.** When the job has never run, the tab explains what Results will
show and offers the Compute button, rather than rendering blank.

**Prerequisite state.** When the BAM is unsorted or unindexed, the tab says so
and names the fix, rather than offering a button that will fail on click.

### What stays in QC

QC keeps parsed facts, base composition, and per-position quality — properties
of the reads. Results takes everything about the alignment. `AlignmentReport`
moving across is what makes that line consistent; it is moved outright, not
duplicated, since the feature is still being built and no established habit
depends on it.

## Boundaries

- `pipelines/bam_stats_runner.py` — pure functions: command construction,
  parsing samtools output, binning depth. Testable without a queue, matching
  `align_runner.py`.
- `queue/pipeline_handlers.py` — the `run_bam_stats` handler: orchestration,
  progress, cancellation.
- `queue/results.py` — `_apply_run_bam_stats`: merge facts, never replace, same
  as `_apply_run_qc` (`results.py:474`).
- `services/pipeline_service.py` — `launch_bam_stats`: prerequisite checks and
  enqueue.
- `api/v1/pipelines.py` — launch route and report route.
- `frontend/src/components/BamResults.tsx` — the tab body.
- `frontend/src/components/CoverageChart.tsx` — the birds-eye and cumulative
  plots.
- `frontend/src/components/ContigTable.tsx` — paginated table plus download.

## Testing

Backend, via `pytest` in the `api` container:

- `bam_stats_runner` parsing and binning as pure-function tests: known
  `idxstats`/`coverage` output in, expected structures out; a contig shorter
  than one bin still yields one bin; bin count stays constant across genome
  sizes.
- Launch refusals: unsorted BAM and missing `.bai` each raise `ValidationError`
  with the expected `details.needs`.
- `_apply_run_bam_stats` merges rather than replaces existing facts.
- Report route: `..`, absolute paths, and symlink escapes are all rejected;
  pagination returns the expected slice.

Frontend: manual verification at localhost:5173, per CLAUDE.md — there is no
headless component-testing setup and none is expected. Check an imported BAM, a
pipeline-produced BAM, an unindexed BAM, and a BAM whose job has never run.

Note that `worker` does not hot-reload: `docker compose restart worker` is
required before re-testing the job after any handler change.

## Deliberately excluded

- A genome browser or per-base zoom. The birds-eye plot is a summary; anything
  finer belongs in IGV.
- A separate Mongo collection for coverage rows. Considered and rejected as
  overkill for a single-user local tool, with a lifecycle that would have to
  track object deletion.
- Auto-chaining `index_bam`. Contradicts the existing documented decision in
  `launch_variant_calling`.
