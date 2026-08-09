# Per-tile sequence quality — design

**Date:** 2026-08-09
**Status:** Approved, not implemented

A heatmap on the QC tab showing mean quality by flow-cell tile and read
position, so a physical defect on the chip -- a bubble, a smudge, a fluidics
stumble -- is visible as the spatial pattern it is, rather than as an
unexplained dip in the aggregate quality curve.

## Why this, and why not the cheaper version

Illumina writes the cluster's physical coordinates into every read's header:

```
@M01939:146:000000000-D3WVL:1:1101:15351:1594 1:N:0:1
                            ^ lane
                              ^ tile
                                   ^ x    ^ y
```

Grouping reads by that tile field and averaging quality per position gives the
matrix this chart draws.

FastQC already computes a per-tile module, and BioFlow already runs FastQC
(`pipeline_handlers._run_fastqc`) and keeps its HTML. Parsing FastQC's
`fastqc_data.txt` out of its zip would have been the cheap route. It was
rejected for three reasons:

- FastQC is the *optional* half of the short-read QC pair. It needs a JRE, and
  `_run_short_read_qc` is explicitly written so a missing one still produces
  fastp's facts. A chart built on FastQC inherits that fragility.
- FastQC's per-tile values are deviation-from-mean, not absolute Phred. That
  metric has a silent blind spot (below), and sourcing from FastQC would make
  it the only metric available.
- Parsing headers ourselves yields x/y coordinates as a by-product, which is
  the groundwork for a within-tile spatial view later.

So: BioFlow parses the headers itself, in its own pass.

## Collection

New module `backend/app/pipelines/tile_scanner.py`, called from
`_run_short_read_qc` beside the existing `_run_fastqc` call and governed by the
same rule already established there: **a failure in this pass is a warning, not
the job's failure.** QC that yields fastp's numbers and no tile matrix is a good
outcome. QC that fails because a header parse hit something unexpected is not.

One sequential pass over the R1 FASTQ:

- **Parse every record's header** -- split on `:`, field index 4 is the tile.
  Cheap enough to do for all records; it is a string split, not a decode.
- **Decode quality for a subsample only.** The rate is adaptive, targeting
  ~2,000,000 sampled reads: a small file is sampled completely, a large one
  thinned harder. A tile/position cell on a MiSeq run still receives hundreds
  of reads at that rate.
- **Early bail.** If the first 1,000 headers yield no parseable tile, stop and
  record `qc_tile_source: "absent"`. This is the SRA-stripped
  (`@SRR123456.1`), Nanopore-UUID, and PacBio-ZMW case. It costs a fraction of
  a second instead of a full read of a 30GB file to learn nothing.
- **Accumulate** a `tile x position` running sum and count, plus per-tile x/y
  min/max and a read count. The extents are nearly free and are what a future
  within-tile spatial view would need.
- **Guardrails.** Cap at 2,000 distinct tiles (well above any real flow cell --
  a NovaSeq S4 has ~1,408) and 1,000 positions. Past either, stop accumulating
  new keys and set a `truncated` flag rather than growing without bound on a
  malformed file.

**R1 only.** A flow-cell defect is physical and appears in both mates; doubling
the I/O to confirm it twice is not worth the cost.

### The cost, stated plainly

This full sequential read is the expensive part of the feature. On a large
gzipped FASTQ it may add minutes to a QC job that currently takes seconds --
gzip decompression dominates. That number is unknown until it runs on real
data.

Two rejected alternatives, recorded because the first is an attractive trap:

- **Capping the pass at the first N million reads is wrong.** Illumina writes
  FASTQ in tile order, so reading a prefix reads the first few tiles completely
  and the rest not at all -- precisely the failure this chart exists to catch,
  and it produces a plot that looks complete while being blank for most of the
  flow cell.
- **Seeking to evenly spaced offsets** would bound the cost and cover the whole
  cell, but gzip is not seekable without an index, and real files are gzipped.

If the measured cost proves unacceptable, the fallback is to make the tile pass
**opt-in per run**, not to cap the prefix.

## Storage

A NovaSeq matrix is ~1,408 tiles x 150 positions = **over 200,000 cells**,
roughly 850KB of floats. It cannot live in the object document.
`fastp_runner.parse_report` already declined to store "several hundred floats
per read direction" inline for the same reason; this is three orders of
magnitude worse, and object documents are read by the detail panel, summary
prompts, and provenance, none of which want it.

Aggressive binning to fit inline was also rejected: binning 1,408 tiles into 40
rows averages away the single bad tile the chart exists to find, and does so
worst on the largest files, which are the likeliest to have a defect.

So the matrix is written as a **sidecar JSON file** in the QC report directory,
alongside the FastQC output, and the facts carry only scalars:

| Fact | Meaning |
|---|---|
| `qc_tile_source` | `"present"` / `"absent"` / `"unparsed"` -- what the frontend branches on |
| `qc_tile_count` | Distinct tiles observed |
| `qc_tile_sampled_reads` | How many reads' quality was actually decoded |
| `qc_tile_sample_rate` | The adaptive rate used |
| `qc_tile_worst` | Worst tile's id and its mean-Q deficit |
| `qc_tile_matrix` | Filename of the sidecar |
| `qc_tile_truncated` | Whether a guardrail cap was hit |

`qc_tile_worst` earns its place independently of the chart: it is a row the
`QcReport` `<dl>` can show, and a value a future suggestion rule could read,
without loading the matrix at all.

Tiles are stored **unbinned**. Binning is the chart's job, done to fit available
pixels, so the tooltip can always name the real tile number.

### A new route, deliberately not `get_qc_report`

`get_qc_report` (`backend/app/api/v1/pipelines.py:471`) serves QC reports under
a hard `sandbox` CSP with `default-src 'none'`. That is load-bearing: FastQC
embeds overrepresented sequences verbatim from the reads, so a crafted FASTQ can
put attacker-chosen bytes into that HTML, and the CSP is what stops the page
reaching the API's session.

The tile matrix is JSON fetched by the application's own JavaScript. The exact
header set that makes the existing route safe for untrusted HTML is what would
block that fetch. So the matrix gets its own route: **same ownership check and
same path-traversal guard, without the sandbox CSP.** This is a case where
reusing the visually similar route would be wrong.

## The chart

New `TileQualityChart` in `frontend/src/components/SequenceCharts.tsx`, matching
the house style there -- hand-rolled SVG, no chart library, theme variables,
hover readout, self-suppressing when its data is absent.

### Colour scale: absolute by default, relative on a toggle

Three scales were mocked with synthetic data containing two defects: a dead tile
band (tiles 1107-1110) and a run-wide dip at cycles 17-20. What the mockups
showed:

- **Absolute Phred**, on the same green/amber/red thresholds `QualityChart`
  already uses: **both defects visible.** Its weakness is that a
  mediocre-but-normal run turns amber everywhere and the signal drowns -- a
  loud, obvious failure mode.
- **Deviation from the position mean** (what FastQC measures): the clean run is
  beautifully featureless and the tile band is unmistakable, but **the cycle-17
  dip vanishes completely.** When every tile drops together, no tile deviates.
  A run-wide fluidics problem renders as "nothing wrong" -- a silent failure
  mode.
- **FastQC's rainbow**: same blind spot as the relative scale, since it is the
  same metric, plus it is not colour-blind safe and shares no vocabulary with
  the rest of the app's palette.

The relative scale's blind spot is the deciding fact, and it was worse than
predicted before the mockup: the defect is *erased*, not merely muted.

**Therefore: absolute Phred is the default**, because when it fails it fails
loudly. **The relative scale is available on a toggle**, because it genuinely
outperforms absolute at the specific job of isolating one bad tile from its
neighbours. Both colour the same stored matrix client-side, so the second scale
costs nothing on the backend and switching requires no fetch.

In relative mode the chart carries a one-line note stating that it shows
deviation from each position's mean and that a dip affecting all tiles equally
will not appear. Without that caption the blind spot becomes a wrong conclusion.

Having both scales also answers a question neither answers alone: whether a dip
already visible in `QualityChart`'s aggregate curve is run-wide or
tile-localised.

### Rendering

- **Canvas above ~20,000 cells, SVG below.** One `<rect>` per cell is fine at
  the mockup's 8,400 and is far too many DOM nodes at a real matrix's 200,000+.
- **Rows bin to available pixels** when tiles outnumber them. The tooltip reads
  unbinned data and names a real tile number even when its row is an average.
- **Physical tile order, not worst-first.** Illumina tile numbers encode
  surface, swath, and position, so physical ordering is what makes a spatial
  pattern -- a smudge covering adjacent tiles -- legible. `qc_tile_worst` does
  the "which is worst" job instead.
- **Lazy fetch.** The sidecar loads when the QC tab renders and
  `qc_tile_source === "present"`, not with the detail panel.

## Where it does not appear

- **Long reads** never reach this code. `_run_short_read_qc` is the only caller,
  and the NanoPlot path has no tile concept.
- **SRA-stripped and non-Illumina files** set `qc_tile_source: "absent"`.

Both render nothing at all, the same self-suppressing behaviour `QcReport`
already has. Neither is an error state.

## Testing

Backend, via `./backend/run-worktree-tests.sh` from the worktree:

- Header parsing: valid Illumina, SRA-stripped, Nanopore UUID, PacBio ZMW,
  truncated, and a header with colons in unexpected positions.
- Early bail fires on a headerless file without reading it through.
- Adaptive rate: a small file samples fully, a large one thins.
- Truncation guardrails trip at both the tile and position caps.
- Facts shape, including that `qc_tile_worst` names the tile the fixture made
  worst.

**Plus a check against a real FASTQ**, per CLAUDE.md's rule about registries and
rules that pass green suites while being wrong. The suggestion-rule precedent
applies directly: hand-built fixtures that already look the way the parser
expects will pass while real-world headers fail. Header formats in the wild are
where this breaks, so:

```bash
docker compose exec api python -c "..."
```

against a real project's reads is worth more than another fixture.

Frontend verification is the browser -- `./ops/worktree-up.sh`, then
localhost:5273. There is no headless component-testing setup in this repo and
none is expected.

## Open question for implementation

The runtime cost of the full pass on a large gzipped FASTQ. Measure it on real
data; if it is unacceptable, make the pass opt-in per run rather than capping
the prefix.
