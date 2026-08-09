# Read length distribution chart

## Problem

The reads QC tab shows base composition and per-position quality, but nothing
about the distribution of read lengths themselves. For raw Illumina data this
is usually one sharp peak and not very interesting, but it becomes important
after trimming (did trimming leave enough usable length?) and for long-read
platforms (PacBio/ONT), where read length varies over several orders of
magnitude and directly determines whether the data is usable for assembly or
long-range mapping.

FastQC computes this ("Sequence Length Distribution") but the app only keeps
FastQC's HTML report — the underlying module data is never parsed
(`backend/app/queue/pipeline_handlers.py` `_run_fastqc`, `pipeline_handlers.py:508-510`).
fastp's JSON also contains a length histogram, but `parse_qc_facts`
(`backend/app/pipelines/fastp_runner.py:197-229`) only extracts scalar fields
(`total_reads`, `q20_rate`, `gc_content`, etc.) from it. NanoPlot's
`NanoStats.txt`, scraped by `_parse_nanoplot_stats`
(`pipeline_handlers.py:600-639`), is scalar-only by construction. No length
histogram exists anywhere in the codebase today, for either read type.

## Goals

- A read length distribution chart in the QC tab, in the app's existing
  hand-rolled-SVG house style, for both short reads (Illumina) and long reads
  (PacBio/ONT).
- Available immediately at ingest, not gated on a QC pipeline job having run
  — consistent with how base composition and per-position quality already
  work.
- Correct visual behavior across the vastly different length ranges of short
  vs. long reads (single peak ~35-300bp vs. a spread that can run
  100bp-100kb+).

## Non-goals

- Parsing FastQC's or fastp's own length-histogram output. The existing
  ingest-time sampler (`sequence_stats.py`) already reads every sampled
  record for other stats; adding length counting to that same pass is
  strictly cheaper and matches how every other chart on this tab is sourced.
- BAM-based long-read ingestion. Long reads currently arrive and are parsed
  as FASTQ only (`_run_long_read_qc` in `pipeline_handlers.py:519-575` hard-codes
  `--fastq`, never `--bam`, despite NanoPlot supporting it and `pysam` being a
  real dependency used elsewhere for aligned BAM/CRAM). Wiring BAM into
  long-read intake is a separate, larger change and out of scope here.
- Platform-aware binning in the backend sampler. See Design below — the
  sampler stays platform-agnostic; only the frontend's axis rendering
  branches on platform.

## Design

### Backend: one histogram fact, computed in the existing sampler

`backend/app/storage/sequence_stats.py`'s `fastq_stats()` already iterates
every sampled read (up to `DEFAULT_SAMPLE_READS` = 200,000) to build base
composition and the per-position quality curve. Add a length `Counter` to
that same loop — no new I/O pass, no new pipeline step.

Bin at a fixed 10bp width (matching `INSERT_SIZE_BIN_WIDTH`'s existing
convention for `alignment_stats`'s insert-size histogram, `sequence_stats.py:36`),
uncapped rather than clamped to a max like `INSERT_SIZE_MAX` — insert size
has a real biological ceiling (fragment library prep), but PacBio HiFi reads
routinely exceed 20kb, so an artificial cap would flatten the exact shape
long-read users need to see. Emit as:

```python
facts["read_length_histogram"] = [
    {"length_bin": bin_start, "count": n}
    for bin_start, n in sorted(counter.items())
]
```

This mirrors the existing `insert_size_histogram` / `mapq_histogram` shape
(`sequence_stats.py:459-482`) exactly — same "sorted list of bucket/count
dicts" pattern, same additive-facts-dict merge already used by
`_parse_fastq` (`parsers.py:389-391`) and `_parse_bam`-equivalent
(`parsers.py:151-153`).

Add the identical counting logic to `alignment_stats()` (the BAM/SAM/CRAM
path, `sequence_stats.py:335-484`) at the same time, since it already loops
every sampled record and the two functions are otherwise kept in sync (both
produce `base_composition`, `quality_per_position`, etc.). This covers
already-aligned BAM/CRAM files ingested through the existing generic
alignment-parsing path (`parsers.py:147-154`) — an unrelated, pre-existing
intake route, not the long-read-specific BAM support excluded under
Non-goals (which is about teaching `_run_long_read_qc`/NanoPlot to accept
`.bam` as a *long-read sequencing* input, a different and larger change).

**Why not bin by platform (linear vs. log) in the backend:** `_parse_fastq`
has no `platform` parameter (`parsers.py:310-312`) and none is available at
ingest time — `qc_platform` is only written later, by the long-read QC
pipeline step, from inferred chemistry
(`frontend/src/api/types.ts:1205-1207` confirms this: "Written by the
long-read QC path"). Forcing platform-awareness into the ingest-time sampler
would mean either blocking ingest facts on a QC job, or guessing platform
from filename/length heuristics — both worse than the alternative: store one
platform-agnostic histogram, and let the chart choose a linear or log x-axis
at render time, when `qc_platform` is actually known. Bin width stays fixed
at 10bp either way; only the axis *scale* (not the data) depends on
platform.

### Frontend: one chart component, platform-aware axis

New `LengthDistributionChart` in `frontend/src/components/SequenceCharts.tsx`,
following `QualityChart`'s existing conventions (`SequenceCharts.tsx:174-301`):
hand-rolled inline SVG, `--accent` line with ~0.13-opacity area fill under
it, `--border` gridlines, `--text-faint` axis labels, hover crosshair with a
text summary line below the SVG, `.section-title` header, and a
"sampled N reads" footnote sourced from `facts.stats_sampled_reads`.

Axis scale: linear if `obj.facts.qc_platform` is unset or a short-read
platform value; log-scale (ticks at round order-of-magnitude points, e.g.
100bp/1kb/10kb/100kb) if `qc_platform` matches a long-read platform
(`OXFORD_NANOPORE` / `PACBIO_SMRT`, per `LONG_READ_PLATFORMS` in
`backend/app/pipelines/qc_stats.py:24-27`). A file with no `qc_platform` yet
set (QC pipeline never run) defaults to linear, matching the common case
(short-read raw upload) and the reference image's shape.

Wiring, following the exact pattern `curve`/`composition` already use in
`DetailPanel.tsx`'s `QcTab` (`DetailPanel.tsx:955-964`):

```tsx
const lengthHistogram =
  Array.isArray(obj.facts.read_length_histogram)
    ? obj.facts.read_length_histogram
    : null;
```

Rendered as a third card in the existing `.qc-charts` grid
(`styles.css:3828-3838`), alongside `BaseCompositionChart` and
`QualityChart`. No new guard like `!isReference` is needed — a FASTA
reference has no `read_length_histogram` fact (only FASTQ/BAM sampling
paths compute it), so `lengthHistogram` is naturally `null` there and the
card simply doesn't render, same mechanism already used for `curve`.

### Types

Add to `frontend/src/api/types.ts`, next to the existing histogram-bucket
interfaces (`MapqHistogramBucket`, `InsertSizeHistogramBucket`, lines
1127-1135):

```ts
export interface ReadLengthHistogramBucket {
  length_bin: number;
  count: number;
}
```

Referenced as `read_length_histogram?: ReadLengthHistogramBucket[]` — added
where `mapq_histogram`/`insert_size_histogram` live today. (Those currently
sit on a `BamStatsFacts` interface separate from `QcFacts`; since this fact
is produced by both `fastq_stats` and `alignment_stats`, it should be typed
wherever `base_composition`/`quality_per_position` conceptually belong —
those two are read directly off `obj.facts` untyped today, so
`read_length_histogram` can follow the same loose typing rather than forcing
a new shared interface split.)

## Error handling

No new failure modes: this reuses the existing sampler's try/except
(`sequence_stats.py:126-130` for FASTQ, `:428-434` for alignment records),
which already returns `{}` on read/decode failure without losing other
facts. A file with zero sampled reads already returns `{}` overall
(`sequence_stats.py:132-133`); the length histogram simply won't be present,
and the frontend's `Array.isArray` guard handles that the same as any other
missing fact.

## Testing

- Backend: extend existing `sequence_stats` tests (wherever `fastq_stats`/
  `alignment_stats` are currently tested) with a case asserting
  `read_length_histogram` bucket contents for a small synthetic FASTQ with
  known read lengths, and one for `alignment_stats` with synthetic BAM
  records.
- Manual verification per repo convention (no frontend component tests
  exist): upload a short-read FASTQ and a long-read (ONT/PacBio) FASTQ via
  `localhost:5173` (or a worktree's `ops/worktree-up.sh` instance), confirm
  the chart renders in the QC tab with a linear axis for the short-read file
  and, after running long-read QC (so `qc_platform` is set), a log axis for
  the long-read file.
