# Coverage Depth Histogram Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-base depth-frequency histogram and a per-chromosome depth plot to the BAM Results tab.

**Architecture:** `run_bam_stats` already streams every per-base line from `samtools depth -a` through `bin_depth`, which averages the values into 1000 regional bins and discards them. A new `DepthHistogram` accumulator is passed into that same pass as an optional sink, so the new data costs no extra tool run and no second read of the depth file. Bucket width adapts to the genome's mean depth, which `samtools coverage` has already produced two phases earlier. The per-chromosome plot is frontend-only, reading the existing `bam_stats_contigs_top` fact.

**Tech Stack:** Python 3 / pytest (pure functions in `backend/app/pipelines/`), React 18 + TypeScript, hand-rolled SVG (this repo has no charting library).

**Spec:** `docs/superpowers/specs/2026-08-10-coverage-depth-histogram-design.md`

---

## Background the engineer needs

**Why not compute this on the frontend.** `bam_stats_coverage_bins` is 1000 *regional means*. Averaging destroys the distribution: a genome half at 60x and half at 0x produces bins identical to one evenly covered at 30x — exactly the bimodal case (contamination, large copy-number variation) this histogram exists to reveal. `bam_stats_cumulative` is five threshold points, itself computed over those means. Neither can produce a true depth histogram. The data must come from the per-base stream.

**Why an optional sink instead of a third return value.** `bin_depth` returns a 2-tuple, unpacked in six places (`align_handlers.py:831` plus five tests). Widening the return type breaks all six, and `test_bin_count_is_constant_regardless_of_reference_size` indexes `small[0]` positionally — it would keep passing while meaning something different. An optional parameter leaves every existing caller untouched.

**Running tests from this worktree.** Use `./backend/run-worktree-tests.sh`, never `docker compose exec api python -m pytest`. The `api` container bind-mounts the *main* checkout, so the latter silently tests main's code and reports results describing the wrong tree.

## File structure

| File | Responsibility |
|---|---|
| `backend/app/pipelines/bam_stats_runner.py` (modify) | `DepthHistogram` class, `histogram_bucket_width()`, `bin_depth`'s new optional sink |
| `backend/tests/pipelines/test_bam_stats_runner.py` (modify) | Unit tests for all of the above |
| `backend/app/queue/align_handlers.py` (modify, ~line 820-860) | Wire the accumulator into `run_bam_stats`, emit two new facts |
| `frontend/src/api/types.ts` (modify) | `DepthHistogramBucket`, two new `BamStatsFacts` fields |
| `frontend/src/components/DepthHistogramChart.tsx` (create) | The depth histogram chart |
| `frontend/src/components/ContigDepthChart.tsx` (create) | The per-chromosome depth plot |
| `frontend/src/components/BamResults.tsx` (modify) | Render both |

No changes are needed to `backend/app/queue/results.py` — `_apply_run_bam_stats` merges the whole `facts` dict onto the object generically, with no per-key allowlist.

---

### Task 1: `DepthHistogram` accumulator and adaptive bucket width

**Files:**
- Modify: `backend/app/pipelines/bam_stats_runner.py`
- Test: `backend/tests/pipelines/test_bam_stats_runner.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/pipelines/test_bam_stats_runner.py`:

```python
class TestDepthHistogram:
    def test_bucket_width_spans_three_times_mean_depth(self):
        """A 40x genome: 3 * 40 / 60 buckets == 2.0 per bucket."""
        assert histogram_bucket_width(mean_depth=40.0) == 2.0

    def test_bucket_width_floors_at_one(self):
        """samtools depth reports integers, so buckets finer than 1x would be
        a comb of structurally empty slots, not a distribution."""
        assert histogram_bucket_width(mean_depth=5.0) == 1.0

    def test_bucket_width_is_none_for_empty_or_zero_depth(self):
        """No mean depth means no sensible axis -- emit nothing rather than
        dividing by zero."""
        assert histogram_bucket_width(mean_depth=0.0) is None

    def test_counts_land_in_the_bucket_for_their_depth(self):
        h = DepthHistogram(bucket_width=2.0, buckets=5)
        for depth in (0.0, 1.0, 2.0, 3.0, 9.0):
            h.add(depth)
        facts = h.to_facts()
        # bucket 0 spans [0,2) and caught depths 0 and 1
        assert facts[0] == {"depth": 0.0, "count": 2}
        # bucket 1 spans [2,4) and caught depths 2 and 3
        assert facts[1] == {"depth": 2.0, "count": 2}
        # bucket 4 spans [8,10) and caught depth 9
        assert facts[4] == {"depth": 8.0, "count": 1}

    def test_depths_beyond_the_span_land_in_the_overflow_bucket(self):
        """The overflow bucket is what keeps a high-copy contaminant visible
        instead of silently dropped."""
        h = DepthHistogram(bucket_width=1.0, buckets=3)
        h.add(0.5)
        h.add(500.0)
        facts = h.to_facts()
        assert len(facts) == 4  # buckets + 1 overflow
        assert facts[-1] == {"depth": 3.0, "count": 1}

    def test_emits_every_bucket_including_empty_ones(self):
        """A gap in the middle of the distribution is signal. Omitting empty
        buckets would make the chart's x-axis lie about spacing."""
        h = DepthHistogram(bucket_width=1.0, buckets=4)
        h.add(0.0)
        h.add(3.0)
        assert [f["count"] for f in h.to_facts()] == [1, 0, 0, 1, 0]
```

Add `DepthHistogram` and `histogram_bucket_width` to the existing import block at the top of the file (alongside `bin_depth`, `allocate_bins`, etc.).

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_bam_stats_runner.py -k DepthHistogram -q
```

Expected: FAIL — `ImportError: cannot import name 'DepthHistogram'`.

- [ ] **Step 3: Write the implementation**

In `backend/app/pipelines/bam_stats_runner.py`, add these constants next to the existing `BIN_COUNT` / `COVERAGE_THRESHOLDS` block:

```python
# The depth histogram's x-axis spans 0 .. 3x the genome's mean depth across a
# fixed number of buckets, so a 30x WGS run and a 2000x amplicon panel both
# get a readable curve rather than one of them collapsing into a single bar.
# Spanning 3x the mean keeps the main peak in the left third with room to
# show a high-depth second mode -- a duplicated region or a high-copy
# contaminant -- which is the signal the chart exists for.
HISTOGRAM_BUCKETS = 60
HISTOGRAM_MEAN_MULTIPLE = 3.0
# samtools depth reports integers, so a width below 1x would give several
# buckets per representable depth, most of them structurally empty: a comb
# rather than a distribution.
HISTOGRAM_MIN_BUCKET_WIDTH = 1.0
```

Then add, after `bin_depth`:

```python
def histogram_bucket_width(*, mean_depth: float) -> float | None:
    """Bucket width for the depth histogram, derived from mean depth.

    Returns None when there is no usable mean (an empty or wholly uncovered
    reference), which callers treat as "emit no histogram" rather than
    dividing by zero.
    """
    if mean_depth <= 0:
        return None
    width = mean_depth * HISTOGRAM_MEAN_MULTIPLE / HISTOGRAM_BUCKETS
    return max(width, HISTOGRAM_MIN_BUCKET_WIDTH)


class DepthHistogram:
    """Counts reference positions by their depth, into fixed-width buckets.

    Accumulated during bin_depth's single pass over `samtools depth -a`
    output -- the per-base values that pass would otherwise average away.
    The distribution's shape is the point: a tight peak is a healthy uniform
    library, a long tail is coverage bias, and two modes flag contamination
    or a large copy-number change. None of those survive the regional
    averaging in `bam_stats_coverage_bins`.
    """

    def __init__(self, *, bucket_width: float, buckets: int = HISTOGRAM_BUCKETS):
        self.bucket_width = bucket_width
        self.buckets = buckets
        # One extra slot: everything at or beyond the span, so a high-copy
        # contaminant stays visible rather than being dropped.
        self._counts = [0] * (buckets + 1)

    def add(self, depth: float) -> None:
        idx = int(depth / self.bucket_width)
        if idx >= self.buckets:
            idx = self.buckets
        self._counts[idx] += 1

    def to_facts(self) -> list[dict]:
        """Every bucket, including empty ones: a gap mid-distribution is
        signal, and omitting it would misrepresent the x-axis spacing.

        `depth` is the bucket's lower bound. The final entry is the overflow
        bucket, which the frontend labels with a leading '>='.
        """
        return [
            {"depth": round(i * self.bucket_width, 4), "count": n}
            for i, n in enumerate(self._counts)
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_bam_stats_runner.py -k DepthHistogram -q
```

Expected: PASS, 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/bam_stats_runner.py backend/tests/pipelines/test_bam_stats_runner.py
git commit -m "feat(pipelines): count reference positions by depth into an adaptive histogram"
```

---

### Task 2: Feed the histogram from `bin_depth`'s existing pass

**Files:**
- Modify: `backend/app/pipelines/bam_stats_runner.py:274-325` (`bin_depth`)
- Test: `backend/tests/pipelines/test_bam_stats_runner.py`

- [ ] **Step 1: Write the failing tests**

Add to `class TestDepthHistogram`:

```python
    def test_bin_depth_feeds_the_histogram_from_the_same_pass(self):
        """The histogram must see per-base depths, not the regional means
        bin_depth produces -- averaging is precisely what destroys the
        distribution this chart reports."""
        h = DepthHistogram(bucket_width=1.0, buckets=10)
        bins, boundaries = bin_depth(
            contig_lengths=[("chr1", 4)],
            depth_lines=iter(["chr1\t1\t2", "chr1\t2\t2", "chr1\t3\t8", "chr1\t4\t8"]),
            bin_count=2,
            histogram=h,
        )
        # Two bins of two positions each, averaged: the shape is gone here.
        assert bins == [2.0, 8.0]
        # The histogram kept both modes.
        counts = {f["depth"]: f["count"] for f in h.to_facts()}
        assert counts[2.0] == 2
        assert counts[8.0] == 2

    def test_bin_depth_without_a_histogram_is_unchanged(self):
        """The sink is optional; omitting it must behave exactly as before."""
        bins, boundaries = bin_depth(
            contig_lengths=[("chr1", 4)],
            depth_lines=iter(["chr1\t1\t10", "chr1\t3\t20"]),
            bin_count=4,
        )
        assert bins == [10.0, 0.0, 20.0, 0.0]

    def test_histogram_skips_depths_for_contigs_not_in_the_geometry(self):
        """A depth line for an unknown contig is already skipped for binning;
        it must not be counted in the histogram either, or the two outputs
        would describe different reference sets."""
        h = DepthHistogram(bucket_width=1.0, buckets=5)
        bin_depth(
            contig_lengths=[("chr1", 2)],
            depth_lines=iter(["chr1\t1\t1", "chrUnknown\t1\t4"]),
            bin_count=2,
            histogram=h,
        )
        counts = {f["depth"]: f["count"] for f in h.to_facts()}
        assert counts[1.0] == 1
        assert counts[4.0] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_bam_stats_runner.py -k "bin_depth_feeds or without_a_histogram or skips_depths" -q
```

Expected: FAIL — `TypeError: bin_depth() got an unexpected keyword argument 'histogram'`.

- [ ] **Step 3: Write the implementation**

Change `bin_depth`'s signature in `backend/app/pipelines/bam_stats_runner.py`:

```python
def bin_depth(
    *,
    contig_lengths: list[tuple[str, int]],
    depth_lines: Iterator[str],
    bin_count: int = BIN_COUNT,
    histogram: "DepthHistogram | None" = None,
) -> tuple[list[float], list[dict]]:
```

Append to its docstring, after the existing "Returns `(bins, boundaries)`" paragraph:

```
    `histogram`, when given, is fed every per-base depth during this same
    pass. It is an output parameter rather than a third return value because
    the 2-tuple return is unpacked at six call sites, one of which indexes it
    positionally -- widening it would break them all and silently change that
    one's meaning. Passing None leaves behaviour identical.
```

Inside the loop, add the `histogram` call immediately after the existing `bin_n[idx] += 1` line:

```python
        bin_sum[idx] += float(depth_str)
        bin_n[idx] += 1
        if histogram is not None:
            histogram.add(float(depth_str))
```

Note this sits *after* the `if contig not in geometry: continue` guard, which is what makes the third test pass.

- [ ] **Step 4: Run the whole file's tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_bam_stats_runner.py -q
```

Expected: PASS. **Every pre-existing test in this file must still pass unmodified** — that is the regression check for the refactor. If any existing assertion about `bins` had to change, the change is wrong; revert and reconsider.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/bam_stats_runner.py backend/tests/pipelines/test_bam_stats_runner.py
git commit -m "feat(pipelines): accumulate the depth histogram during bin_depth's existing pass"
```

---

### Task 3: Emit the histogram facts from `run_bam_stats`

**Files:**
- Modify: `backend/app/queue/align_handlers.py:820-860`

There is no unit test for this task: `run_bam_stats` is a queue handler that shells out to samtools, and the repo deliberately keeps the testable logic in `bam_stats_runner.py`'s pure functions (both now covered). Verification is the real-BAM check in Task 6.

- [ ] **Step 1: Compute the bucket width before the depth pass**

In `run_bam_stats`, the `contigs` table is built from `samtools coverage` *before* the depth phase. Insert this immediately after the `contig_lengths = [...]` line and before the `with open(depth_path...)` block:

```python
    # Mean depth is already known from `samtools coverage`, two phases back,
    # so the histogram's axis can be sized before the depth pass rather than
    # needing a second one.
    provisional = bam_stats_runner.genome_summary(contigs=contigs, bins=[])
    bucket_width = bam_stats_runner.histogram_bucket_width(
        mean_depth=provisional["mean_depth"]
    )
    histogram = (
        bam_stats_runner.DepthHistogram(bucket_width=bucket_width)
        if bucket_width is not None
        else None
    )
```

`genome_summary(bins=[])` returns the mean depth without the `pct_covered_*` keys, which need bins; the full summary is still computed as it is today further down.

- [ ] **Step 2: Pass the accumulator into `bin_depth`**

Change the existing call:

```python
    with open(depth_path, errors="replace") as fh:
        bins, boundaries = bam_stats_runner.bin_depth(
            contig_lengths=contig_lengths, depth_lines=fh, histogram=histogram
        )
```

- [ ] **Step 3: Add the two facts**

In the `facts = {...}` dict, after `"bam_stats_cumulative": cumulative,`:

```python
        # Absent rather than empty when there is no usable mean depth, so the
        # frontend can tell "not computed" from "measured as flat".
        **(
            {
                "bam_stats_depth_histogram": histogram.to_facts(),
                "bam_stats_depth_bucket_width": bucket_width,
            }
            if histogram is not None
            else {}
        ),
```

- [ ] **Step 4: Verify the backend suite is still green**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count — a green exit code after a collection error is not green.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/align_handlers.py
git commit -m "feat(pipelines): record the depth histogram alongside the coverage bins"
```

---

### Task 4: Frontend types

**Files:**
- Modify: `frontend/src/api/types.ts:1234-1265`

- [ ] **Step 1: Add the bucket type**

After the existing `InsertSizeHistogramBucket` interface:

```typescript
export interface DepthHistogramBucket {
  /** The bucket's lower bound. The final bucket is the overflow bucket. */
  depth: number;
  /** Reference positions at this depth -- not reads. */
  count: number;
}
```

- [ ] **Step 2: Add the two fields to `BamStatsFacts`**

After `bam_stats_cumulative?: CumulativeCoveragePoint[];`:

```typescript
  bam_stats_depth_histogram?: DepthHistogramBucket[];
  bam_stats_depth_bucket_width?: number;
```

- [ ] **Step 3: Typecheck**

```bash
cd frontend && npm run lint
```

Expected: no output, exit 0.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts
git commit -m "feat(frontend): type the depth histogram facts"
```

---

### Task 5: The two charts

**Files:**
- Create: `frontend/src/components/DepthHistogramChart.tsx`
- Create: `frontend/src/components/ContigDepthChart.tsx`
- Modify: `frontend/src/components/BamResults.tsx`

This repo has **no charting library** and adding one is not justified for two static shapes — follow `CoverageChart.tsx`'s hand-rolled SVG idiom (fixed `viewBox`, `var(--accent)` / `var(--border)` / `var(--text-faint)` for colour, `<title>` for hover).

- [ ] **Step 1: Write `DepthHistogramChart.tsx`**

```tsx
import type { DepthHistogramBucket } from "../api/types";

/**
 * How many reference positions sit at each depth.
 *
 * The shape is the point, and it is the one thing the birds-eye chart cannot
 * show: those bins are regional means, and averaging turns a genome that is
 * half 60x and half 0x into something indistinguishable from a flat 30x one.
 * A tight peak here is a uniform library, a long right tail is coverage bias,
 * and two modes flag contamination or a large copy-number change.
 */
export function DepthHistogramChart({
  buckets,
  bucketWidth,
  meanDepth,
}: {
  buckets: DepthHistogramBucket[];
  bucketWidth: number;
  meanDepth?: number;
}) {
  if (!buckets?.length) return null;

  const w = 360;
  const h = 160;
  const pad = { top: 10, right: 10, bottom: 26, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxCount = Math.max(...buckets.map((b) => b.count), 1);
  const barW = plotW / buckets.length;
  const x = (i: number) => pad.left + i * barW;
  const y = (count: number) => pad.top + plotH - (count / maxCount) * plotH;

  const label = (b: DepthHistogramBucket, i: number) =>
    i === buckets.length - 1
      ? `≥${Math.round(b.depth)}×`
      : `${Math.round(b.depth)}–${Math.round(b.depth + bucketWidth)}×`;

  // Where the mean falls, so a skewed distribution reads as skewed rather
  // than as a peak the viewer has to place against the summary row by eye.
  const meanIdx =
    meanDepth != null && bucketWidth > 0 ? meanDepth / bucketWidth : null;
  const meanX =
    meanIdx != null && meanIdx < buckets.length ? x(meanIdx) : null;

  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
        Reference positions by depth
      </div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block", marginTop: 4 }}
      >
        {buckets.map((b, i) => (
          <rect
            key={i}
            x={x(i)}
            y={y(b.count)}
            width={Math.max(barW - 1, 1)}
            height={pad.top + plotH - y(b.count)}
            fill="var(--accent)"
            opacity={0.8}
          >
            <title>
              {label(b, i)}: {b.count.toLocaleString()} positions
            </title>
          </rect>
        ))}

        {meanX != null && (
          <>
            <line
              x1={meanX}
              x2={meanX}
              y1={pad.top}
              y2={pad.top + plotH}
              stroke="var(--border)"
              strokeWidth="1"
              strokeDasharray="3 2"
            />
            <text
              x={meanX}
              y={pad.top - 2}
              textAnchor="middle"
              fontSize="9"
              fill="var(--text-faint)"
            >
              mean
            </text>
          </>
        )}

        <text x={pad.left} y={h - 4} fontSize="9" fill="var(--text-faint)">
          0×
        </text>
        <text
          x={w - pad.right}
          y={h - 4}
          fontSize="9"
          fill="var(--text-faint)"
          textAnchor="end"
        >
          {label(buckets[buckets.length - 1], buckets.length - 1)}
        </text>
      </svg>
    </div>
  );
}
```

- [ ] **Step 2: Write `ContigDepthChart.tsx`**

```tsx
import type { ContigCoverage } from "../api/types";

const MAX_BARS = 50;

/**
 * Mean depth per contig, against the genome-wide mean.
 *
 * Reads the capped `bam_stats_contigs_top` fact rather than the full report:
 * 50 bars is already the readable limit for this, and a fragmented assembly
 * with thousands of scaffolds would be unreadable at any cap. The complete
 * table stays available as ContigTable and its TSV download.
 *
 * The reference line is what makes this worth plotting rather than reading
 * off the table -- an aneuploidy or a dropped contig reads as a departure
 * from the genome mean, not as an absolute number needing interpretation.
 */
export function ContigDepthChart({
  contigs,
  meanDepth,
  totalContigs,
}: {
  contigs: ContigCoverage[];
  meanDepth?: number;
  totalContigs?: number;
}) {
  if (!contigs?.length) return null;

  const shown = contigs.slice(0, MAX_BARS);
  const rowH = 14;
  const w = 360;
  const labelW = 78;
  const h = shown.length * rowH + 18;

  const maxDepth = Math.max(...shown.map((c) => c.mean_depth), meanDepth ?? 0, 1);
  const barLen = (d: number) => ((w - labelW - 10) * d) / maxDepth;
  const meanX = meanDepth != null ? labelW + barLen(meanDepth) : null;

  const capped = totalContigs != null && totalContigs > shown.length;

  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
        Mean depth per contig
        {capped ? ` (top ${shown.length} of ${totalContigs.toLocaleString()} by mapped reads)` : ""}
      </div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block", marginTop: 4 }}
      >
        {shown.map((c, i) => (
          <g key={c.contig}>
            <text
              x={labelW - 4}
              y={i * rowH + 10}
              textAnchor="end"
              fontSize="9"
              fill="var(--text-faint)"
            >
              {c.contig.length > 12 ? `${c.contig.slice(0, 11)}…` : c.contig}
            </text>
            <rect
              x={labelW}
              y={i * rowH + 3}
              width={Math.max(barLen(c.mean_depth), 1)}
              height={rowH - 5}
              fill="var(--accent)"
              opacity={0.8}
            >
              <title>
                {c.contig}: {c.mean_depth.toFixed(1)}× over{" "}
                {c.length.toLocaleString()} bp
              </title>
            </rect>
          </g>
        ))}

        {meanX != null && (
          <line
            x1={meanX}
            x2={meanX}
            y1={0}
            y2={shown.length * rowH}
            stroke="var(--border)"
            strokeWidth="1"
            strokeDasharray="3 2"
          />
        )}
        {meanDepth != null && (
          <text
            x={meanX ?? 0}
            y={h - 4}
            textAnchor="middle"
            fontSize="9"
            fill="var(--text-faint)"
          >
            genome mean {meanDepth.toFixed(1)}×
          </text>
        )}
      </svg>
    </div>
  );
}
```

- [ ] **Step 3: Render both from `BamResults.tsx`**

Add to the imports:

```tsx
import { ContigDepthChart } from "./ContigDepthChart";
import { DepthHistogramChart } from "./DepthHistogramChart";
```

Replace the existing cumulative-coverage block:

```tsx
          {f.bam_stats_cumulative && f.bam_stats_cumulative.length > 0 && (
            <div className="section">
              <CumulativeCoverageChart curve={f.bam_stats_cumulative} />
            </div>
          )}
```

with:

```tsx
          <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
            {f.bam_stats_cumulative && f.bam_stats_cumulative.length > 0 && (
              <div className="section" style={{ flex: "1 1 300px" }}>
                <CumulativeCoverageChart curve={f.bam_stats_cumulative} />
              </div>
            )}
            {f.bam_stats_depth_histogram &&
              f.bam_stats_depth_histogram.length > 0 &&
              f.bam_stats_depth_bucket_width != null && (
                <div className="section" style={{ flex: "1 1 300px" }}>
                  <DepthHistogramChart
                    buckets={f.bam_stats_depth_histogram}
                    bucketWidth={f.bam_stats_depth_bucket_width}
                    meanDepth={f.bam_stats_summary?.mean_depth}
                  />
                </div>
              )}
          </div>

          {f.bam_stats_contigs_top && f.bam_stats_contigs_top.length > 0 && (
            <div className="section">
              <ContigDepthChart
                contigs={f.bam_stats_contigs_top}
                meanDepth={f.bam_stats_summary?.mean_depth}
                totalContigs={f.bam_stats_summary?.total_contigs}
              />
            </div>
          )}
```

The two coverage-shape charts sit side by side because they answer adjacent questions ("what depth did I get" vs "was it deep enough"), matching the existing insert-size/MAPQ pair below them.

- [ ] **Step 4: Typecheck**

```bash
cd frontend && npm run lint
```

Expected: no output, exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/DepthHistogramChart.tsx frontend/src/components/ContigDepthChart.tsx frontend/src/components/BamResults.tsx
git commit -m "feat(frontend): plot the depth distribution and per-contig depth"
```

---

### Task 6: Verify against a real BAM

Unit tests here feed hand-built strings to the parsers, which is exactly the shape of green-suite-wrong-behaviour this repo has been bitten by before. This task is not optional.

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

UI on 5273, API on 8100, with its own database seeded from the main stack. Do **not** use plain `docker compose` from this worktree — a hook blocks it, because it would silently repoint the main 5173 stack at this branch.

- [ ] **Step 2: Recompute results for a BAM that already has coverage facts**

Three objects in the seeded database carry `bam_stats_coverage_bins`. `ERR17609896.bam` is a good subject — it is paired-end WGS with a full 0–60 MAPQ range. Open it at http://localhost:5273, go to its Results tab, and press **recompute results**.

- [ ] **Step 3: Confirm the histogram matches the summary**

Check, in the running UI:

- The depth histogram renders, with its peak near the dashed mean line. **A peak that does not sit near the mean reported in the summary row means the bucket width or the accumulator is wrong** — this is the check no fixture can make for you.
- The final bar is the overflow bucket, labelled `≥N×`.
- The per-contig chart renders with its reference line, and bar lengths agree with the `mean_depth` column in the per-contig table below it.

- [ ] **Step 4: Confirm the facts directly**

```bash
docker compose -p biopipe-issue-129-37ae8a exec -T api python -c "
import asyncio
from app.db.client import connect_to_mongo, get_db
async def main():
    await connect_to_mongo()
    db = get_db()
    o = await db.objects.find_one({'facts.bam_stats_depth_histogram': {'\$exists': True}})
    f = o['facts']
    hist = f['bam_stats_depth_histogram']
    total = sum(b['count'] for b in hist)
    peak = max(hist, key=lambda b: b['count'])
    print('object:', o['name'])
    print('buckets:', len(hist), '(expect 61)')
    print('width:', f['bam_stats_depth_bucket_width'])
    print('positions counted:', total, 'vs reference length', f['bam_stats_summary']['total_length'])
    print('peak bucket depth:', peak['depth'], 'vs mean depth', f['bam_stats_summary']['mean_depth'])
asyncio.run(main())
"
```

Expected: 61 buckets; total positions counted equal to the reference length (`samtools depth -a` emits every position, so these must match — a shortfall means depth lines are being skipped); peak bucket near the reported mean depth.

- [ ] **Step 5: Tear the stack down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 6: Run the full backend suite once more**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the printed count.

---

### Task 7: Open the PR

- [ ] **Step 1: Push**

```bash
git push -u origin HEAD
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --base main --title "feat(pipelines): show the coverage depth distribution and per-contig depth" --body "$(cat <<'EOF'
Adds the depth-frequency histogram and a per-chromosome depth plot to the BAM
Results tab.

The histogram cannot be derived from the facts already stored:
`bam_stats_coverage_bins` holds 1000 regional means, and averaging destroys
exactly the bimodal shape (contamination, large CNV) the chart exists to
reveal. It has to come from the per-base stream.

That stream is already running, though. `run_bam_stats` pipes every
`samtools depth -a` line through `bin_depth`, which averages the values and
drops them; this adds a second accumulator on the same pass, so there is no
new tool, no second read of the depth file, and no meaningful added runtime.

Bucket width adapts to the genome's mean depth -- available from
`samtools coverage` before the depth pass, so no second pass is needed to size
the axis -- with a 1x floor, since samtools reports integer depths and a finer
width would produce a comb of empty buckets rather than a distribution.

The histogram is passed as an optional sink rather than returned as a third
element: `bin_depth`'s 2-tuple is unpacked at six call sites, one of which
indexes it positionally, so widening the return type would break them all and
silently change that one's meaning.

Per-contig plot is frontend-only, reading the existing `bam_stats_contigs_top`.

Verified against a real BAM in a worktree stack, not only fixtures: the
histogram's peak sits at the mean depth the summary row reports, and the
positions counted equal the reference length.

Closes #155
EOF
)"
```

- [ ] **Step 3: Label the PR**

```bash
gh pr edit --add-label "type:feature" --add-label "area:pipelines" --add-label "area:frontend"
```

`.github/release.yml` categorizes release notes by label, not by the title's prefix — an unlabelled PR lands under "Other changes".

- [ ] **Step 4: Report the PR URL and stop.** Do not merge; the user reviews and merges.

---

## Self-review

**Spec coverage**

| Spec requirement | Task |
|---|---|
| Optional-sink API, existing callers untouched | 2 |
| `DepthHistogram` with `add` / `to_facts` | 1 |
| `HISTOGRAM_BUCKETS` = 60, 3x mean span | 1 |
| 1x bucket-width floor with rationale | 1 |
| Overflow bucket | 1, 5 (`≥N×` label) |
| Guard on zero/empty mean depth | 1 (`histogram_bucket_width` → None), 3 (facts omitted) |
| Bucket width computed before the depth pass | 3 |
| Facts `bam_stats_depth_histogram` + `_bucket_width` | 3 |
| Reuse of the chart idiom | 5 (hand-rolled SVG per `CoverageChart.tsx`) |
| Per-contig plot from `bam_stats_contigs_top`, cap visible | 5 |
| Bimodal / uniform / overflow / zero-depth tests | 1, 2 |
| Real-BAM verification | 6 |

The spec floated possibly lifting `BamResults.tsx`'s inline `Histogram` into a shared module. This plan writes a separate `DepthHistogramChart` instead: the depth chart needs a mean reference line, a bucket-range hover label, and overflow-bucket handling, none of which that component has — generalizing it would mean a third set of props on a component documented as deliberately single-use. Left as-is, matching the spec's "the bar is a real second need" condition.

**Placeholder scan:** none — every step carries its code or its exact command.

**Type consistency:** `DepthHistogram(bucket_width=, buckets=)`, `histogram_bucket_width(mean_depth=)`, and `bin_depth(..., histogram=)` are used identically in Tasks 1-3. `DepthHistogramBucket.depth` / `.count` (Task 4) match `to_facts()`'s keys (Task 1) and the chart's reads (Task 5). `ContigCoverage.mean_depth` / `.contig` / `.length` match `types.ts:1201-1210`. `BamStatsSummary.mean_depth` / `.total_contigs` are existing fields.
