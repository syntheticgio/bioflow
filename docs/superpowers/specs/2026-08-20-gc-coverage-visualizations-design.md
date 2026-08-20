# GC vs coverage visualizations — design

Date: 2026-08-20.

Covers [#640](https://github.com/syntheticgio/bioflow/issues/640) (coverage vs
GC bias curve) and [#641](https://github.com/syntheticgio/bioflow/issues/641)
(per-contig GC vs coverage blobplot). Both are children of
[#633](https://github.com/syntheticgio/bioflow/issues/633).

**One document because they share their missing input.** Neither plot exists
because nothing joins reference GC against alignment depth; once that join
exists, #640 aggregates it by GC bin and #641 aggregates it by contig. Specced
together so the join is designed once, then shipped as two independent stages.

The two answer genuinely different questions and both are worth having: #640
diagnoses *library* bias on one organism, #641 diagnoses *which organisms are
present*.

## What exists today

Verified against this worktree on 2026-08-20:

- **`gc_tracks.py`** computes per-contig, per-window GC and GC skew by a full
  ordered scan of a reference FASTA (a sampler cannot do it — GC skew is
  cumulative and order-dependent). `WINDOW_COUNT = 500`,
  `MIN_WINDOW_BASES = 100`. Peak memory is the largest contig, ~250 MB for
  human chr1, inside the handler's 2 GB budget.
- **`mosdepth_runner.py` landed** (#626) and **already reuses that exact
  windowing**: `WINDOW_COUNT = gc_tracks.WINDOW_COUNT`,
  `MIN_WINDOW_BASES = gc_tracks.MIN_WINDOW_BASES`, with
  `build_windows_bed` tiling `window_count = min(WINDOW_COUNT, length //
  MIN_WINDOW_BASES)` per contig. **So per-window depth and per-window GC are
  already computed on the same grid.** This is the fact that changes both
  issues' answers, and it was not true when they were filed.
- **`bam_stats_runner.py`** has per-contig `length`, `reads`,
  `covered_bases`, `mean_depth`, `mean_baseq`, `mean_mapq` — **no GC**.
- **`ContigDepthChart.tsx`** plots per-contig depth as bars from the capped
  `bam_stats_contigs_top` fact; its own docstring says 50 bars is the readable
  maximum.
- **`MAX_STORED_CONTIGS = 50`** in `storage/parsers.py`, with the comment that
  contig lists run to hundreds of thousands of entries on scaffold-level
  assemblies — a bounded sample plus the true count.
- **`DepthHistogramChart`** shows depth distribution shape but cannot say why.

## Decision V1: reuse the shared window grid; do not build a third windowing

#640's stated main design question is whether `gc_tracks`' windows can be
reused, and it raises the right objection: fixed-*count* windows have
different physical widths per contig, which would weight GC bins
inconsistently.

**Reuse the grid, and fix the weighting at aggregation time rather than by
re-windowing.**

Why reuse: mosdepth already tiles depth on precisely this grid, so a join is a
key lookup — `(contig, window_index)` — with no resampling, no second scan of
the reference, and no third windowing constant for a future reader to
reconcile against the other two. Introducing fixed-width windows would mean
depth and GC live on different grids and one of them must be resampled, which
is both work and a source of error.

Why the weighting objection does not force re-windowing: **weight each window
by its physical width when binning by GC.** A window's contribution to its GC
bin is `mean_depth × width`, and the bin's value is
`Σ(depth × width) / Σ(width)`. That is the correct aggregate regardless of
whether widths are uniform, and it makes the variable-width property
irrelevant rather than merely tolerable.

**This must be a written, tested property**, not an incidental consequence of
the arithmetic: a naive `mean(window_depths)` per bin looks right, passes a
uniform-width test, and silently over-weights short-contig windows on exactly
the fragmented assemblies where the plot matters most. The unit test needs
contigs of *different* lengths, or it cannot distinguish the two
implementations.

## Decision V2: #640 needs a reference-GC input, and must refuse without one

The join needs per-window GC of **the reference the BAM was aligned to**.
`gc_tracks` runs as its own job against a reference FASTA, so the GC may or may
not have been computed for the relevant reference.

The card therefore has a real precondition, and the honest posture — the one
this repo keeps arriving at — is to **refuse with a reason that names the
missing step**, not to silently produce nothing or to auto-run the other job:

- BAM's alignment target does not resolve
  (`reference_assembly.resolve_alignment_target_for_bam` raised) → unavailable,
  saying the alignment has no recorded reference.
- Target resolves but has no GC tracks → unavailable, naming the GC-tracks
  action as the thing to run first.
- Depth not computed (no mosdepth run on this BAM) → unavailable, naming it.

*Rejected:* chaining the missing jobs automatically. Two multi-minute jobs
fired from one click on a card that advertised a chart is a surprise, and the
existing cards do not behave that way.

## Decision V3: #641's per-contig GC comes from aggregating the same windows

#641 asks where per-contig GC should be computed: added to `bam_stats`, or
derived from the reference.

**Neither — aggregate the windows from V1.** A contig's GC is
`Σ(gc_count) / Σ(window_bases)` over its windows, which is already available
wherever the join is. Adding a reference scan to `bam_stats_runner` would make
a BAM-stats job depend on a reference FASTA it does not otherwise need, and
would compute a third time what `gc_tracks` already computed exactly.

This also means #640 and #641 share one computation and differ only in
aggregation axis — GC bin versus contig — which is the strongest argument for
specifying them together.

## Decision V4: cap by cumulative length, and say so on the chart

#641 proposes capping by cumulative length rather than count (keep contigs
covering 99% of bases). **Adopted**, with one addition.

Why it is right: the count cap that governs `ContigDepthChart` exists because
50 bars is the readable maximum, and a scatter has no such limit — clustering
gets *clearer* with more points. But a fact document carrying 500,000 contigs
is its own problem, so a ceiling is still needed. Cumulative length drops only
contigs too small to plot meaningfully.

The addition: **a contaminant is often many small contigs**, so a
cumulative-length cap can drop precisely the cluster the plot exists to find.
So the chart must state what was dropped — "showing 4,812 contigs covering 99%
of bases; 38,140 shorter contigs omitted" — rather than presenting a filtered
view as complete. Without that line, a clean-looking blobplot cannot be
distinguished from one whose contamination was truncated away.

A hard ceiling on stored points is still required for pathological cases; when
it binds, that must be said too.

## Decision V5: both are read-only derived facts, not new objects

Both produce arrays derived from data already stored, cheap to recompute, and
useful only as charts. They belong as facts plus a report endpoint on the
BAM — the `coverage` posture (`_NO_NARRATIVE_STEP`), not the modkit posture.

Storage: the per-GC-bin curve is small (tens of bins) and can live in facts.
The per-contig array is bounded by V4 but can still be thousands of entries —
serve it from a report endpoint, as mosdepth serves its per-window array.

## Staging

| Stage | Delivers | Closes |
|---|---|---|
| 1 | The join + GC-binned bias curve + chart | #640 |
| 2 | Per-contig aggregation + blobplot scatter | #641 |

Stage 1 builds the join; stage 2 is a second aggregation of it plus a
frontend. Either is independently useful.

## Components

**Backend (stage 1)**

- A pure module (`gc_coverage.py`) holding the join and both aggregations:
  `join_windows(gc_windows, depth_windows) -> list[JoinedWindow]`,
  `bias_curve(joined, *, bins=20) -> list[dict]` implementing V1's
  width-weighting, and (stage 2) `per_contig(joined) -> list[dict]`. Pure,
  unit-tested, no queue or filesystem.
- A handler joining the two stored artifacts; resources modest (this is
  arithmetic over stored arrays, not a scan).
- A card per V2, with its three distinct refusal reasons.
- `running_now.ENDPOINT_JOB_TYPES`, `_NO_NARRATIVE_STEP` (V5), and the
  `node_types` partition — run as whole classes.

**Frontend**

- Stage 1: a bias-curve line chart beside `DepthHistogramChart`, hand-rolled
  SVG like the existing charts. The InfoMarker must teach the reading — dome =
  PCR bias, flat = fine, monotonic rise = capture artifact — since that
  interpretation is the entire value and #640's text already has the wording.
- Stage 2: the scatter. Log scale on depth; point **area** (not radius)
  proportional to contig length; V4's omission line always rendered. The
  InfoMarker must say clusters are **unlabelled** — taxonomic labelling is
  #625 — so a user is not misled into thinking a cluster is identified.
- `metricInfo.METRIC_INFO` entries per `<Stat metric>`; missing, the
  InfoMarker renders nothing silently (`metricInfo.test.ts`).

## Testing

- **Width weighting (V1)** — the load-bearing test, with contigs of
  *different* lengths so a naive unweighted mean fails it. A uniform-width
  fixture cannot distinguish the implementations.
- **Join** — windows present in one input and not the other (a contig in the
  reference with no aligned reads) resolve to depth 0, not a dropped window;
  a dropped window silently biases the curve upward.
- **Card refusals, failing direction first** — each of V2's three reasons
  asserted on the *message*, not just the status.
- **Cap accounting (V4)** — the omission line reports the true dropped count,
  and is present whenever anything was dropped.
- Registry partitions as whole classes.
- **Real-data check** — against a real BAM with a known GC-biased library if
  one is available; otherwise confirm a flat curve on a PCR-free dataset,
  which is itself the falsifiable prediction.

## Verify before implementing

1. **Does `gc_tracks` store per-window GC in a retrievable form**, or only what
   the Circos plot needed? The join depends on the stored shape, not the
   computed one.
2. **Does mosdepth's stored per-window array carry the window bounds**, or only
   depths in order? V1's width weighting needs widths.
3. **`MAX_STORED_CONTIGS = 50` interaction** — `reference_names` and
   `reference_lengths` are capped at 50, so contig lengths for a fragmented
   assembly may need to come from the BAM header via idxstats rather than
   stored facts.

## Out of scope

- **Taxonomic labelling of blobplot clusters** — that is a true BlobTools plot
  and needs classification against a database; tracked in #625. V5's
  InfoMarker wording exists so users are not misled in the meantime.
- **Automatic remediation advice.** The chart explains the shape; deciding to
  redo a library prep is the user's.
- **Cross-sample comparison** of bias curves. #645's territory.
- **Re-windowing at finer resolution.** V1 — if a future need demands it, it
  demands it for depth and GC together, not for one plot.
