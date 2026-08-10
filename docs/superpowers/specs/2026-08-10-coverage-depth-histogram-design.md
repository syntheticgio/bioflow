# Coverage depth histogram + per-chromosome coverage plot — design

**Date:** 2026-08-10
**Status:** Approved, not implemented
**Issue:** [#155](https://github.com/syntheticgio/bioflow/issues/155) (epic [#154](https://github.com/syntheticgio/bioflow/issues/154))

Adds the depth-frequency histogram — how many bases sit at each depth — and a
per-chromosome depth plot, to the Results tab's existing coverage section.

## What already exists, and why the ticket understates the backend

#155 describes itself as "close to a pure frontend task" because the depth
data is "already fully computed and persisted." That is half right. Two of the
three charts it asks for already ship:

- `BirdsEyeCoverageChart` (`frontend/src/components/CoverageChart.tsx`) —
  coverage across the reference, from `bam_stats_coverage_bins`.
- `CumulativeCoverageChart` — the fraction-at-or-above-depth curve, from
  `bam_stats_cumulative`.

What does **not** exist is the depth *histogram*, and it cannot be derived
from either stored fact:

- `bam_stats_coverage_bins` is 1000 **regional means**. Averaging destroys the
  distribution. A genome half at 60x and half at 0x produces bins identical to
  one evenly covered at 30x — precisely the bimodal case (contamination, large
  CNV) the histogram exists to reveal.
- `bam_stats_cumulative` is five points at the 1x/10x/30x thresholds, and is
  itself computed over the binned means rather than per-base depths
  (`cumulative_coverage` in `bam_stats_runner.py` takes `bins`, not the depth
  stream). It is a summary, not a distribution, and already an approximation.

Deriving the histogram on the frontend from either would produce a chart that
looks correct and reports the wrong shape. The data must come from the
per-base stream.

## Why this is nearly free anyway

`run_bam_stats` (`backend/app/queue/align_handlers.py`) already runs
`samtools depth -a` and streams every per-base line through `bin_depth`,
which accumulates each depth into a bin sum and then discards the value.

The histogram is a **second accumulator over that same stream**: one more
increment per line. No new tool, no new subprocess, no second pass over the
depth file, and no meaningful runtime cost — the expensive parts (samtools
traversing the BAM, writing one line per base) are already paid for.

Reading `depth.txt` a second time was rejected: that file is one line per
base of the reference, so a second pass doubles the slowest phase of the job
to avoid touching one well-tested function.

## Backend

### `bin_depth` returns the histogram too

`bin_depth` becomes a single pass producing both outputs:

```python
def bin_depth(
    *,
    contig_lengths: list[tuple[str, int]],
    depth_lines: Iterator[str],
    bin_count: int = BIN_COUNT,
    histogram_bucket_width: float | None = None,
) -> tuple[list[float], list[dict], list[dict]]:
    """... returns (bins, boundaries, histogram)."""
```

`bins` and `boundaries` keep their current meaning and values exactly. The
existing tests for `bin_depth` must pass unchanged apart from unpacking a
third element — if any existing assertion about `bins` changes, the refactor
is wrong.

When `histogram_bucket_width` is `None` the histogram comes back empty, so the
binning behaviour is available standalone (variant density reuses
`allocate_bins`, not `bin_depth`, but keeping the parameter optional avoids
forcing a bucket decision on any future caller).

### Adaptive bucket width

A fixed 1x bucket scheme fails on the two ends of this app's range: a 30x WGS
run needs sub-1x resolution to show its peak's width, while a 2000x amplicon
panel would spill everything into an overflow bucket and show nothing at all.

Bucket width is therefore derived from the genome's mean depth:

```python
HISTOGRAM_BUCKETS = 60           # bars, at the readable limit for the chart size
HISTOGRAM_MEAN_MULTIPLE = 3.0    # x-axis spans 0 .. 3x mean depth
HISTOGRAM_MIN_BUCKET_WIDTH = 1.0 # never finer than 1x; depth is an integer
```

- `bucket_width = max(mean_depth * HISTOGRAM_MEAN_MULTIPLE / HISTOGRAM_BUCKETS, HISTOGRAM_MIN_BUCKET_WIDTH)`

The floor matters at low depth: `samtools depth` reports integers, so on a 5x
genome an unfloored width of 0.25 would give four buckets per integer depth,
three of them structurally empty — a comb, not a distribution. At and below
20x mean depth the floor takes over and buckets are exactly 1x wide.
- Depths at or above `HISTOGRAM_BUCKETS * bucket_width` land in a final
  overflow bucket, mirroring how `INSERT_SIZE_MAX` caps insert size.

Spanning 3x the mean keeps the main peak in the left third with room to show a
high-depth second mode — a duplicated region or a contaminating high-copy
sequence — rather than clipping it into the overflow bucket, which is the
signal being looked for.

**Mean depth is available before the depth pass runs.** `genome_summary`
derives it from the `contigs` table, which comes from `samtools coverage` —
already parsed two phases earlier in `run_bam_stats`. So the bucket width is
computed up front and passed in; there is no chicken-and-egg requiring two
passes. The plan must order the handler's calls accordingly: `coverage` →
mean depth → bucket width → `depth`.

Guard: when `mean_depth` is 0 or the contigs table is empty, skip the
histogram (emit nothing) rather than dividing by zero.

### Fact shape

```python
"bam_stats_depth_histogram": [
    {"depth": 0.0, "count": 12345},   # depth = bucket's lower bound
    ...
]
"bam_stats_depth_bucket_width": 1.5,
```

`count` is a count of **reference positions**, not reads. The overflow bucket
is the last entry; the frontend labels it `≥N×`.

Storage is bounded at 60 entries regardless of genome size, consistent with
every other fact here being a fixed-size summary.

## Frontend

### Depth histogram

`BamResults.tsx` already has a local `Histogram` component used by the insert
size and MAPQ charts. It takes `xKey`/`yKey`/`xLabel` and is close to a direct
fit — the depth histogram reuses it, with `xLabel` rendering the bucket's
lower bound as `12×` and the final bucket as `≥90×`.

Placed in the existing coverage `section`, beside `CumulativeCoverageChart`,
since the two answer adjacent questions ("what depth did I get" vs "was it
deep enough").

If reuse turns out to need more than an `xLabel` change — e.g. the overflow
bucket wants distinct styling — lift `Histogram` into its own module rather
than growing a third set of props on the inline copy. Note the existing
component is deliberately documented as "single-use and simple enough not to
share SequenceCharts.tsx's more general axis machinery"; the bar for
generalizing it is a real second need, which this may or may not be.

### Per-chromosome coverage plot

Reads the existing `bam_stats_contigs_top` fact (top 50 contigs by mapped
reads) — no backend change. A horizontal bar per contig showing `mean_depth`,
with a reference line at the genome-wide mean so an aneuploidy or a dropped
contig reads as a departure from it rather than an absolute number.

The ticket asks whether the top-50 cap needs raising. It does not, for a plot:
50 bars is already at the readable limit, and a fragmented assembly with 3000
scaffolds would be unreadable at any cap. The complete table remains available
as the paginated `ContigTable` and its TSV download. The chart labels itself
"Top 50 contigs by mapped reads" when the full count exceeds 50, so the cap is
visible rather than implied.

## Testing

Pure functions, so unit tests over strings and lists — matching
`bam_stats_runner.py`'s existing split:

- `bin_depth` returns unchanged `bins`/`boundaries` for the existing fixtures.
- A synthetic bimodal depth stream produces two separated modes in the
  histogram — the case the feature exists for, and the one the binned means
  provably cannot represent.
- A uniform stream produces a single mode.
- Depths beyond the span land in the overflow bucket, and the bucket count is
  exactly `HISTOGRAM_BUCKETS + 1`.
- Zero/empty mean depth emits no histogram rather than raising.

Beyond unit tests, verify against a real BAM in the running app. Three BAMs in
the current database already carry `bam_stats_coverage_bins`, so they can be
recomputed and the histogram compared against the mean depth the summary row
reports — a peak that does not sit near the reported mean means the bucket
width or the accumulator is wrong, which no fixture will catch.

## Out of scope

- Raising the per-contig cap (see above).
- Replacing `cumulative_coverage`'s binned-mean approximation with a per-base
  computation. Now that the per-base stream is being accumulated anyway this
  becomes cheap and strictly more accurate, but it changes the values of an
  existing shipped chart, which is a separate change with its own before/after
  verification. Worth a follow-up issue.
