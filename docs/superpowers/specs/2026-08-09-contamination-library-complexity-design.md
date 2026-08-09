# Contamination & Library Complexity charts

Two new visualizations on the reads QC tab, answering a question the tab
cannot currently answer: is this run sequencing the target DNA, sequencing
adapters, or sequencing the same fragment over and over?

- **Adapter Content** -- a cumulative per-position curve showing what
  percentage of reads carry adapter sequence from each base position onward.
  When fragments are shorter than the read length, the sequencer reads through
  into the adapter; this is how the user sees that before it corrupts an
  alignment.
- **Sequence Duplication Levels** -- the proportion of the library made of
  sequences seen once, twice, ... up to >10k times. High duplication means
  over-amplification during PCR or a library with little biological
  complexity.

## Why the data does not already exist

Neither statistic is currently in our facts, and the reasons differ.

`qc_adapters.read1_sequence` records *which* adapter fastp detected, not where
it appears. fastp's JSON has no per-position adapter array at all, and QC runs
it with `--disable_adapter_trimming` (`fastp_runner.py`), so its
`adapter_cutting` block is largely empty by construction.

For duplication we store `qc_duplication_rate`, a single scalar from fastp.
fastp's JSON does carry a `duplication.histogram`, but see "The duplication
number conflict" below for why we do not use it.

## Where the work happens

A new module, `backend/app/pipelines/contamination_stats.py`:

```python
def scan_contamination(
    path: Path,
    compression: Compression,
    *,
    detected_adapters: Sequence[str] = (),
    cancel_event: threading.Event | None = None,
) -> dict
```

One **full-file pass** accumulating both statistics together. Two statistics
needing the same bytes is not a reason to read a 30 GB file twice.

Called from `_run_short_read_qc` in `backend/app/queue/pipeline_handlers.py`,
after FastQC, as a new progress phase (`"contamination"`). `detected_adapters`
comes from the fastp facts parsed a few lines above in the same function.

Three properties of that placement are deliberate:

- **QC time, not ingest time.** `sequence_stats.fastq_stats` runs during ingest
  (`parsers.py`), and nothing re-runs ingest -- facts added there would never
  reach the files already in a user's projects. QC is re-runnable, is where
  these charts appear, and is a cost the user has already opted into.
- **Failure is not the job's failure.** Same rule FastQC already follows here:
  a scan that raises is logged and skipped, and QC still persists every fact
  that did parse. The charts self-suppress when their facts are absent.
- **Short-read only.** `_run_long_read_qc` does not call it. A per-position
  adapter curve is meaningless for reads running 200 bp to 100 kb -- the same
  reason `QcReport` already renders two different shapes.

### Full file, not a sample

This is the one place the design deliberately spends more computation than the
surrounding code does.

`sequence_stats` samples 200k reads, and that is correct for base composition,
which converges to within ~0.3%. It is *not* correct here. FastQC's duplication
correction (below) extrapolates from `count_at_unique_limit` to `total_count`;
if `total_count` is a 200k sample rather than the file's real read count, the
correction extrapolates to the sample and the resulting "> 1k duplicates" means
"> 1k within a 200k window" -- a number that looks authoritative and is not.

Implementing the expensive correction and then denying it the input it corrects
for would be the worst of both options. The scan runs over the whole file, and
the reported numbers are real library numbers.

Cost is bounded in the ways that matter: memory is capped (below), the pass is
sequential I/O, and it sits inside a job that already runs fastp over the same
whole file.

## What the scan computes

### Adapter content

Per-position counters over a fixed probe set, first 12 bp of each, matching
FastQC's convention:

| Probe | Sequence (first 12 bp) |
|---|---|
| Illumina Universal | `AGATCGGAAGAG` |
| Illumina Small RNA 3' | `TGGAATTCTCGG` |
| Illumina Small RNA 5' | `GATCGTCGGACT` |
| Nextera Transposase | `CTGTCTCTTATA` |
| PolyA | `AAAAAAAAAAAA` |
| PolyG | `GGGGGGGGGGGG` |
| *Detected* | fastp's `read1_sequence`, truncated to 12 bp |

PolyG is included on purpose. On NovaSeq/NextSeq two-colour chemistry, absence
of signal reads as G, so poly-G tails are a real and common artifact of modern
data -- arguably more common now than adapter read-through.

The **detected** probe is what the fixed list cannot provide: a file with a
custom or unusual adapter still gets a curve. It is dropped when its first
12 bp equal a listed kit's, so a standard Illumina file does not render two
identical overlapping curves. When fastp detected nothing -- common on
single-end input, where it cannot do overlap analysis -- the set is just the
six known probes, and the chart still works.

For each read, find the earliest position at which each probe matches, then
increment that position **and every position after it**. That cumulative rule
is what makes the curve monotonic and is what the FastQC plot people recognize
actually shows. The value at each position is matched reads / total reads.

Per-position arrays are allocated to `MAX_POSITIONS` (1000), reusing the
constant and the reasoning already in `sequence_stats`.

### Duplication levels

FastQC's algorithm, taken from its source
(`uk/ac/babraham/FastQC/Modules/OverRepresentedSeqs.java` and
`DuplicationLevel.java`) rather than from recollection:

1. Truncate each read to its **first 50 bp**. This tolerates quality decay at
   read ends and catches fragments differing only in trailing adapter.
2. Maintain a dictionary of up to **100,000 distinct sequences**. On reaching
   that limit the dictionary **freezes**: existing keys keep incrementing, new
   keys are dropped. Record `count_at_unique_limit`, the total read count at
   the moment of freezing.
3. Collate into "how many distinct sequences were observed exactly N times".
4. Apply `get_corrected_count(count_at_limit, total_count, duplication_level,
   number_of_observations)` to each level -- the estimate of how many sequences
   at that level were missed because the dictionary froze. It computes the
   probability of *not* having seen a sequence with that duplication level
   within the first `count_at_limit` reads, inverts it, and scales the observed
   count. It carries two early-bail guards from the original: an exact return
   when `count_at_limit == total_count`, and a `limit_of_caring` threshold
   below which the correction cannot change the count by 0.01 of an
   observation. Both keep the loop bounded.
5. Bin into 16 slots by `duplication_level - 1`: exact counts `1`-`9`, then
   `>10`, `>50`, `>100`, `>500`, `>1k`, `>5k`, `>10k`.
6. Each slot's value is `count * duplication_level`, expressed as a percentage
   of `raw_total`.
7. `percent_different_seqs = (dedup_total / raw_total) * 100`, defined as 100
   when `raw_total` is 0.

Note that freezing the dictionary does **not** stop the file scan -- reads
continue to be counted against existing keys. This is what makes
`total_count` a true whole-file count and the correction meaningful.

### Bounds

- Dictionary: 100k entries x ~50 chars, roughly 10-15 MB, independent of file
  size.
- Per-position arrays: 7 probes x 1000 positions.
- Cancellation: `cancel_event` is checked on the `CANCEL_CHECK_READS` cadence
  `sequence_stats` uses. This is now a long-running full-file loop and must be
  interruptible.

## Facts

Written under the `qc_` prefix the detail panel already keys on:

```
qc_adapter_content: {
  positions: [1, 2, 3, ...],
  series: [{ name: "Nextera Transposase", values: [0.0, 0.0, 0.1, ...] }, ...]
}
qc_duplication_levels: {
  labels: ["1","2","3","4","5","6","7","8","9",
           ">10",">50",">100",">500",">1k",">5k",">10k"],
  percentages: [ ... ]
}
qc_percent_unique: 87.3
qc_duplication_scanned_reads: 412839201
```

`qc_duplication_scanned_reads` exists so the chart can state what it measured.
Unlike the sampled charts elsewhere in this tab, its message is that the number
covers the whole file.

### The duplication number conflict

`QcReport.tsx` currently renders a "Duplication" row from fastp's
`qc_duplication_rate`. We now have a better number: a whole-file measurement
with the sampling correction applied. Leaving both visible would put two
methods' answers side by side, disagreeing, on the same screen -- which erodes
trust in every other number on the panel.

Resolution:

- The **Duplication** row reads `100 - qc_percent_unique` when present.
- It falls back to `qc_duplication_rate` when the scan did not run: files QC'd
  before this change, or a scan that failed.
- `qc_duplication_rate` remains in the facts. It is a real measurement and
  provenance should not lose it; it simply stops being displayed when a better
  one exists. `FactsTable` already suppresses every `qc_*` key, so it does not
  leak into the generic table.

### Backward compatibility

Every new fact is optional. Files QC'd before this change render exactly as
they do today -- the new charts self-suppress, the Duplication row falls back.
No migration and no forced re-run; re-running QC picks up the new facts.

## Frontend

A new `frontend/src/components/ContaminationCharts.tsx`, exporting
`AdapterContentChart` and `DuplicationLevelsChart`.

Hand-rolled SVG, following `SequenceCharts.tsx` and its stated reasoning:
these are fixed, simple shapes, and the smallest charting dependency would
outweigh the entire rest of the bundle.

### Adapter Content

Cumulative multi-line plot. X is base position, Y is percentage of reads.

- **The Y axis scales to the maximum observed value, not to 100.** A 4% adapter
  curve flattened against a 100% axis communicates nothing, and 4% adapter
  contamination is worth seeing.
- Series that are 0% at every position are **dropped, not drawn**. On a clean
  file that is most of the probe set, and six flat lines along the axis is
  noise that hides the one line that matters.
- When *every* series is zero, the chart renders a single line: "No adapter
  sequence detected". That is the good outcome and should read as one, not as
  a broken chart.
- Legend by probe name -- "Nextera Transposase" is diagnostic in a way that an
  anonymous coloured line is not.
- Hover reads out the position and each probe's value there.

### Sequence Duplication Levels

Bar chart across the 16 slots, with `qc_percent_unique` as a called-out
headline ("87.3% of the library is unique").

Bars rather than FastQC's line chart: the x axis is ordinal bins of uneven
width (`1`, `2`, ... `>500`, `>1k`), and a connecting line implies an
interpolation between `>500` and `>1k` that does not exist.

### Placement

Both mount in the existing `.qc-charts` grid in `DetailPanel.tsx`, after Base
composition and Quality per position. That grid holds two items today; these
make four, wrapping to 2x2.

No separate "Contamination & Library Complexity" section heading. The tab is
already quality control, and a subsection dividing four peer charts adds
hierarchy without adding information.

The 2x2 wrap is the one item here that is asserted rather than known, and is
checked in the browser rather than reasoned about.

## Verification

Per CLAUDE.md there is no headless component-testing setup in this repo, so the
two halves are verified differently.

**Backend** -- pytest, in `backend/tests/pipelines/test_contamination_stats.py`,
run from this worktree via `./backend/run-worktree-tests.sh`:

- `get_corrected_count` pinned against hand-computed values, including both
  early-bail branches (`count_at_limit == total_count`, and the
  `limit_of_caring` threshold).
- Dictionary freeze behaviour: past 100k distinct sequences, new keys are
  dropped while existing keys keep incrementing, and `count_at_unique_limit`
  records the right total.
- Slot binning at the boundaries -- 9 vs 10, 50 vs 51, and the >10k overflow.
- The cumulative adapter rule: a probe matching at position *k* marks every
  position from *k* to the read's end.
- Detected-probe dedup: a detected sequence whose first 12 bp match a listed
  kit produces no second series.
- A zero-adapter file produces series that the frontend will drop, not absent
  facts.

**Frontend** -- manual, at localhost:5273 via `./ops/worktree-up.sh`, which
serves this worktree's code without disturbing the main stack on 5173.

Checked against a real project, on at least one file with genuinely high
duplication and one that is clean. A chart verified only against the healthy
case is not verified: the clean file exercises the empty-state path, and only
the duplicated file exercises the axis, the bins, and the correction.

The 2x2 grid wrap is confirmed here.

## Out of scope

- Long-read QC. The scan is not called from `_run_long_read_qc`.
- Overrepresented sequences and per-tile quality. FastQC computes both, and the
  dictionary this design builds would support the first almost for free, but
  they are separate cards answering separate questions.
- Backfilling existing files. Facts appear when QC is re-run; nothing forces
  it.
- Acting on the result. These charts inform a trimming decision; they do not
  make one, and no suggestion rule in `suggestion_service.py` reads them.
