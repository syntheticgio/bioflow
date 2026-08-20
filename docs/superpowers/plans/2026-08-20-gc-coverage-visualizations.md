# GC vs coverage visualizations — implementation plan

Date: 2026-08-20.

Closes [#640](https://github.com/syntheticgio/bioflow/issues/640) (stage 1) and
[#641](https://github.com/syntheticgio/bioflow/issues/641) (stage 2). Companion
to `docs/superpowers/specs/2026-08-20-gc-coverage-visualizations-design.md`
(decisions V1–V5).

Two stages, one PR each. Stage 1 builds the join and ships the bias curve;
stage 2 is a second aggregation of the same join plus a scatter. Either is
independently useful, so do not start stage 2 until stage 1 is merged and has
been looked at against real data.

## Spike first (blocks stage 1)

These three decide the shape of the join, and guessing any of them produces a
module that unit-tests green against a contract that does not exist.

- **S-1. What does `gc_tracks` actually *store*?** It computes per-contig,
  per-window GC and skew, but the stored shape is whatever the Circos plot
  needed. Read a real stored fact/report, not the function's return value.
- **S-2. Does mosdepth's stored per-window array carry window bounds, or only
  depths in order?** V1's width weighting needs widths. If only depths are
  stored, widths are re-derivable from `build_windows_bed`'s rule
  (`min(WINDOW_COUNT, length // MIN_WINDOW_BASES)`) given contig lengths —
  confirm that reproduces the stored array exactly, position for position,
  before relying on it.
- **S-3. Where do contig lengths come from for a fragmented assembly?**
  `reference_names`/`reference_lengths` are capped at `MAX_STORED_CONTIGS = 50`
  (`storage/parsers.py:29`), so stored facts are insufficient above 50 contigs
  — which is every assembly #641 cares about. `samtools idxstats` on the BAM is
  the likely source.

Record the answers in the spec as an "Amended" note.

## Stage 1 — the join and the bias curve (#640)

| File | Change |
|---|---|
| `backend/app/pipelines/gc_coverage.py` | **New, pure.** `join_windows(gc_windows, depth_windows, contig_lengths) -> list[JoinedWindow]` and `bias_curve(joined, *, bins=20) -> list[dict]`. No queue, no filesystem, no I/O. |
| `backend/app/queue/<gc_coverage>_handlers.py` | **New handler.** Loads the two stored artifacts, joins, aggregates, returns facts + report. Modest resources — arithmetic over stored arrays, not a scan. |
| `backend/app/queue/results.py` | Applier: merge the curve onto the BAM by per-key `facts.<key>` paths (never whole-dict, #606). |
| `backend/app/services/pipeline_service.py` | `launch_gc_bias(bam_id, owner)` with V2's three preconditions checked at launch, not only in the card. |
| `backend/app/api/v1/pipelines.py` | `POST /pipelines/gc-bias` + report endpoint. |
| `backend/app/services/suggestion_service.py` | `build_gc_bias_card` per V2, three distinct refusal reasons. |
| `backend/app/services/running_now.py` | `ENDPOINT_JOB_TYPES` entry. |
| `backend/app/services/provenance_walker.py` | `_NO_NARRATIVE_STEP` (V5) — facts written back onto an existing object, the `coverage` posture. |
| `backend/app/pipelines/node_types.py` | Spec + adapter, or `EXCLUDED_LAUNCHES` — it is a **partition**. |
| `frontend/src/components/GcBiasChart.tsx` | **New.** Hand-rolled SVG line chart beside `DepthHistogramChart`. |
| `frontend/src/lib/metricInfo.ts` | Entry per new `<Stat metric>`. |

### Ordered steps

1. **`bias_curve`, and its weighting test, first.** This is the one piece of
   real logic in the whole feature. Per V1 the bin value is
   `Σ(depth × width) / Σ(width)`, **not** `mean(depths)`.
   **Write the test with contigs of different lengths.** A uniform-width
   fixture passes against both implementations, so it would certify the wrong
   one — and the naive version over-weights short-contig windows on exactly
   the fragmented assemblies where this plot matters most.
2. **`join_windows`.** The case to write first: a window present in the
   reference GC but absent from depth (a contig with no aligned reads)
   resolves to **depth 0**, not a dropped window. Dropping it silently biases
   the curve upward — a real GC dropout would render as "no data here" rather
   than "no coverage here", which inverts the plot's meaning.
3. **Handler + applier**, using S-1/S-2's answers. Restart the worker after
   editing (`docker compose restart worker`, from the **main** repo root) —
   it does not hot-reload.
4. **Launch + route**, repeating V2's precondition checks. The card is a
   convenience; the launch is the gate.
5. **Card, failing direction first.** Three refusals — no resolvable
   reference, no GC tracks, no depth — each asserted on the **message**, since
   the message naming the missing step is the deliverable. A status-only
   assertion passes against a bare "unavailable" that leaves the user stuck.
   **Do not auto-chain the missing jobs** (V2): two multi-minute jobs from one
   click on a card advertising a chart is a surprise no existing card springs.
6. **Registries**, then the whole `TestExhaustiveness` class and the
   provenance partition — partitions, so a half-fix passes one test and fails
   its sibling (#355).
7. **Chart.** The InfoMarker carries the entire value: dome = PCR
   amplification bias (fixable at the bench, not by re-aligning), flat with
   extreme-tail drop = normal, monotonic rise = library or capture artifact.
   #640's issue text has usable wording. Verify at `http://localhost:5273`
   (worktree stack via `./ops/worktree-up.sh`), not 5173.

## Stage 2 — the blobplot (#641)

Only after stage 1 is merged.

| File | Change |
|---|---|
| `backend/app/pipelines/gc_coverage.py` | **Extend.** `per_contig(joined) -> list[dict]` — GC as `Σ(gc_count) / Σ(window_bases)` per V3, plus mean depth and length. |
| Handler / applier | Second aggregation of the same join; per-contig array served from the **report endpoint**, not facts (thousands of entries). |
| `frontend/src/components/ContigBlobChart.tsx` | **New.** Scatter, hand-rolled SVG. |

### Ordered steps

1. **`per_contig`.** Per V3 this aggregates the stage-1 windows — do **not**
   add a reference scan to `bam_stats_runner`, which would make a BAM-stats
   job depend on a reference FASTA it does not otherwise need and recompute a
   third time what `gc_tracks` already computed.
2. **The cap, per V4.** Keep contigs covering 99% of bases, plus a hard
   ceiling for pathological cases. **The dropped count is part of the output,
   not a log line** — a contaminant is often many small contigs, so this cap
   can drop precisely the cluster the plot exists to find. Test that the
   reported count is the true one.
3. **Scatter.** Log scale on depth. Point **area** proportional to contig
   length, not radius — radius-proportional exaggerates large contigs
   quadratically, and the whole reason to weight by length is to show whether
   an off-cluster group is a trivial or substantial fraction of the assembly.
4. **The omission line, always rendered when anything was dropped**: "showing
   N contigs covering 99% of bases; M shorter contigs omitted". Without it a
   clean-looking blobplot cannot be told from one whose contamination was
   truncated away.
5. **InfoMarker must say the clusters are unlabelled.** Taxonomic labelling is
   #625; a user who reads a cluster as identified has been misled by this
   chart, and #641's own text asks for this note.

## Verification

```bash
./backend/run-worktree-tests.sh tests/ -q
```

From the worktree, never `docker compose exec api`. Then `ruff check --config
backend/pyproject.toml backend/app backend/tests ops e2e`, fixing everything
including pre-existing findings.

Real-data check: a known GC-biased library if one exists; otherwise a PCR-free
dataset should produce a flat curve, which is the falsifiable prediction.

## Out of scope

Per the spec: taxonomic labelling (#625), remediation advice, cross-sample
comparison (#645), and re-windowing at finer resolution.
