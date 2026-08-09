# Read Length Distribution Chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-length-distribution chart to the reads QC tab, sourced from a new platform-agnostic `read_length_histogram` fact computed in the existing ingest-time sampler, rendered with a linear or log x-axis depending on the file's platform.

**Architecture:** Backend: add 10bp-wide-bucketed length counting to `sequence_stats.fastq_stats()` and `sequence_stats.alignment_stats()`, emitting `facts["read_length_histogram"]` in the same `{key, count}` list shape as the existing `insert_size_histogram`/`mapq_histogram`. Frontend: new hand-rolled-SVG `LengthDistributionChart` component in `SequenceCharts.tsx`, wired into `DetailPanel.tsx`'s `QcTab` as a third card in the existing `.qc-charts` grid, choosing a linear or log-scale x-axis based on `obj.facts.qc_platform`.

**Tech Stack:** Python (backend sampler + pytest), TypeScript/React (frontend chart component), no new dependencies.

Spec: `docs/superpowers/specs/2026-08-09-read-length-distribution-design.md`

---

## File Structure

- Modify: `backend/app/storage/sequence_stats.py` — add length counting to `fastq_stats()` and `alignment_stats()`.
- Modify: `backend/tests/storage/test_sequence_stats.py` — new test classes for the length histogram in both functions.
- Modify: `frontend/src/components/SequenceCharts.tsx` — new `LengthDistributionChart` component and its prop type.
- Modify: `frontend/src/api/types.ts` — new `ReadLengthHistogramBucket` interface.
- Modify: `frontend/src/components/DetailPanel.tsx` — wire the new chart into `QcTab`.
- Modify: `frontend/src/styles.css` — no new rules expected (reuses `.qc-chart`/`.section-title`), verified in Task 6.

---

### Task 1: Length histogram in `fastq_stats()`

**Files:**
- Modify: `backend/app/storage/sequence_stats.py:64-161`
- Test: `backend/tests/storage/test_sequence_stats.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/storage/test_sequence_stats.py` (new class, place near `TestInsertSizeHistogram` at the bottom of the file):

```python
class TestReadLengthHistogram:
    def test_bucketed_by_10bp_width(self, tmp_path):
        """Two reads of length 10 land in the same 10bp bucket as each other,
        a length-25 read lands in a different bucket."""
        path = tmp_path / "lengths.fastq"
        with open(path, "w") as f:
            f.write("@r1\n" + "A" * 10 + "\n+\n" + "I" * 10 + "\n")
            f.write("@r2\n" + "A" * 10 + "\n+\n" + "I" * 10 + "\n")
            f.write("@r3\n" + "A" * 25 + "\n+\n" + "I" * 25 + "\n")
        facts = ss.fastq_stats(path, Compression.NONE)
        histogram = {h["length_bin"]: h["count"] for h in facts["read_length_histogram"]}
        assert histogram[10] == 2
        assert histogram[20] == 1

    def test_uncapped_for_long_reads(self, tmp_path):
        """Unlike insert_size_histogram's 2kb cap, length has no ceiling --
        PacBio HiFi reads routinely exceed 20kb and must not be clamped."""
        path = tmp_path / "long.fastq"
        with open(path, "w") as f:
            f.write("@r1\n" + "A" * 25_000 + "\n+\n" + "I" * 25_000 + "\n")
        facts = ss.fastq_stats(path, Compression.NONE)
        histogram = {h["length_bin"]: h["count"] for h in facts["read_length_histogram"]}
        assert histogram[25_000] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/storage/test_sequence_stats.py::TestReadLengthHistogram -v`
Expected: FAIL — `KeyError: 'read_length_histogram'`

- [ ] **Step 3: Add a `READ_LENGTH_BIN_WIDTH` constant**

In `backend/app/storage/sequence_stats.py`, near the existing `INSERT_SIZE_BIN_WIDTH`/`INSERT_SIZE_MAX` constants (lines 33-37):

```python
# Read length is binned at the same 10 bp resolution as insert size, but with
# no ceiling: insert size has a real biological cap from library prep, while
# PacBio HiFi reads routinely exceed 20 kb and a cap would flatten the exact
# shape long-read users need to see.
READ_LENGTH_BIN_WIDTH = 10
```

- [ ] **Step 4: Add length counting to `fastq_stats()`**

In `backend/app/storage/sequence_stats.py`, inside `fastq_stats()` (currently lines 64-161):

Add a counter alongside the existing local variables (near line 80, alongside `counts: Counter[str] = Counter()`):

```python
    length_histogram: Counter[int] = Counter()
```

Inside the read loop, right after `reads += 1` (currently line 104), add:

```python
                bucket = (len(seq) // READ_LENGTH_BIN_WIDTH) * READ_LENGTH_BIN_WIDTH
                length_histogram[bucket] += 1
```

Before the `return facts` at the end of the function (currently line 161), add, following the same `if x: facts[...] = ...` style used for `quality_per_position` just above it:

```python
    if length_histogram:
        facts["read_length_histogram"] = [
            {"length_bin": length_bin, "count": n}
            for length_bin, n in sorted(length_histogram.items())
        ]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/storage/test_sequence_stats.py::TestReadLengthHistogram -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run the full sequence_stats suite to check nothing else broke**

Run: `./backend/run-worktree-tests.sh tests/storage/test_sequence_stats.py -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add backend/app/storage/sequence_stats.py backend/tests/storage/test_sequence_stats.py
git commit -m "feat(backend): add read length histogram to fastq_stats"
```

---

### Task 2: Length histogram in `alignment_stats()`

**Files:**
- Modify: `backend/app/storage/sequence_stats.py:335-484`
- Test: `backend/tests/storage/test_sequence_stats.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/storage/test_sequence_stats.py`, in the same new `TestReadLengthHistogram` class from Task 1:

```python
    def test_alignment_stats_also_bucketed(self, tmp_path):
        from app.models import FormatKind

        p = _write_bam(
            tmp_path / "lengths.bam",
            [
                {"name": "r1", "seq": "A" * 10},
                {"name": "r2", "seq": "A" * 10},
                {"name": "r3", "seq": "A" * 25},
            ],
        )
        facts = ss.alignment_stats(p, FormatKind.BAM)
        histogram = {h["length_bin"]: h["count"] for h in facts["read_length_histogram"]}
        assert histogram[10] == 2
        assert histogram[20] == 1
```

Note: `_write_bam`'s cigar is built from `len(seq)` (`test_sequence_stats.py:348`: `a.cigar = [(0, len(seq))]`), and its default `qual="I" * len(seq)` via `pysam.qualitystring_to_array("I" * len(seq))` — passing a `seq` of the desired length is sufficient, no other fixture changes needed.

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/storage/test_sequence_stats.py::TestReadLengthHistogram::test_alignment_stats_also_bucketed -v`
Expected: FAIL — `KeyError: 'read_length_histogram'`

- [ ] **Step 3: Add length counting to `alignment_stats()`**

In `backend/app/storage/sequence_stats.py`, inside `alignment_stats()` (currently lines 335-484):

Add a counter alongside the existing local variables (near line 360, alongside `counts: Counter[str] = Counter()`):

```python
    length_histogram: Counter[int] = Counter()
```

Inside the record loop, right after `reads += 1` (currently line 386), add:

```python
                bucket = (len(seq) // READ_LENGTH_BIN_WIDTH) * READ_LENGTH_BIN_WIDTH
                length_histogram[bucket] += 1
```

`seq` at this point is already the (possibly reverse-complemented) query sequence assigned at line 382 (`seq = rec.query_sequence`) — length is invariant under reverse-complementing, so this is correct regardless of strand, and no additional handling of `rec.is_reverse` is needed for the length count itself.

Before the final `return facts` (currently line 484), add, following the same pattern as `insert_size_histogram` just above it:

```python
    if length_histogram:
        facts["read_length_histogram"] = [
            {"length_bin": length_bin, "count": n}
            for length_bin, n in sorted(length_histogram.items())
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/storage/test_sequence_stats.py::TestReadLengthHistogram -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full sequence_stats suite**

Run: `./backend/run-worktree-tests.sh tests/storage/test_sequence_stats.py -v`
Expected: all tests PASS

- [ ] **Step 6: Run the full backend suite to check for unrelated regressions**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: all tests PASS (same total count as before this plan started, plus the 3 new tests)

- [ ] **Step 7: Commit**

```bash
git add backend/app/storage/sequence_stats.py backend/tests/storage/test_sequence_stats.py
git commit -m "feat(backend): add read length histogram to alignment_stats"
```

---

### Task 3: Frontend type for the new fact

**Files:**
- Modify: `frontend/src/api/types.ts:1127-1135`

- [ ] **Step 1: Add the type**

In `frontend/src/api/types.ts`, immediately after `InsertSizeHistogramBucket` (currently lines 1132-1135):

```ts
export interface ReadLengthHistogramBucket {
  length_bin: number;
  count: number;
}
```

- [ ] **Step 2: Verify the frontend still typechecks**

Run: `cd frontend && npm run lint` (this repo's `lint` script is `tsc --noEmit`, per `frontend/package.json:11`; this runs directly against the local `node_modules`, no container needed — TypeScript type-checking doesn't depend on the running app stack)

Expected: no new errors (this step only adds an unused-so-far exported interface, which TypeScript does not flag)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/types.ts
git commit -m "feat(frontend): add ReadLengthHistogramBucket type"
```

---

### Task 4: `LengthDistributionChart` component

**Files:**
- Modify: `frontend/src/components/SequenceCharts.tsx`

This task has no automated test — this repo has zero `.test.tsx` files by design (see `CLAUDE.md`, "Verifying changes"); manual browser verification happens in Task 6. Steps here are write-then-visually-inspect via the worktree preview.

- [ ] **Step 1: Add the prop type**

In `frontend/src/components/SequenceCharts.tsx`, alongside the existing `QualityPoint` interface (lines 17-21):

```tsx
export interface LengthBucket {
  length_bin: number;
  count: number;
}
```

- [ ] **Step 2: Write `LengthDistributionChart`**

Append to `frontend/src/components/SequenceCharts.tsx`, after the closing brace of `QualityChart` (currently line 301):

```tsx
/**
 * Linear x-axis for short reads (one sharp peak, matches the classic FastQC
 * shape); log-scale for long reads, where PacBio/ONT lengths span several
 * orders of magnitude and a linear axis would compress everything but the
 * tail into a few pixels. The underlying data is identical either way --
 * only axis scale changes, chosen by the caller via `logScale`.
 */
export function LengthDistributionChart({
  buckets,
  logScale,
  sampledReads,
}: {
  buckets: LengthBucket[];
  logScale: boolean;
  sampledReads?: number;
}) {
  const [hover, setHover] = useState<LengthBucket | null>(null);
  if (!buckets?.length) return null;

  const w = 460;
  const h = 210;
  const pad = { top: 10, right: 14, bottom: 26, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const minLen = buckets[0].length_bin;
  const maxLen = buckets[buckets.length - 1].length_bin;
  const maxCount = Math.max(...buckets.map((b) => b.count));

  // Log scale needs a positive domain; a 0bp bucket (empty read) is mapped to
  // the first bin width instead of dropped, so it still renders rather than
  // producing -Infinity.
  const toDomain = (len: number) => (logScale ? Math.log10(Math.max(len, 1)) : len);
  const domainMin = toDomain(minLen);
  const domainMax = Math.max(toDomain(maxLen), domainMin + 1);

  const x = (len: number) =>
    pad.left + ((toDomain(len) - domainMin) / (domainMax - domainMin)) * plotW;
  const y = (count: number) => pad.top + plotH - (count / maxCount) * plotH;

  const barW = Math.max(1, plotW / buckets.length - 1);

  const ticks = logScale
    ? [100, 1_000, 10_000, 100_000].filter((t) => t >= minLen && t <= maxLen)
    : [minLen, Math.round((minLen + maxLen) / 2), maxLen];

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block" }}
        onMouseLeave={() => setHover(null)}
      >
        <line
          x1={pad.left}
          x2={w - pad.right}
          y1={pad.top + plotH}
          y2={pad.top + plotH}
          stroke="var(--border)"
          strokeWidth="1"
        />

        {buckets.map((b) => (
          <rect
            key={b.length_bin}
            x={x(b.length_bin) - barW / 2}
            y={y(b.count)}
            width={barW}
            height={pad.top + plotH - y(b.count)}
            fill="var(--accent)"
            opacity={hover?.length_bin === b.length_bin ? 0.9 : 0.5}
            onMouseEnter={() => setHover(b)}
          />
        ))}

        {ticks.map((t) => (
          <text
            key={t}
            x={x(t)}
            y={h - 6}
            textAnchor="middle"
            fontSize="9"
            fill="var(--text-faint)"
          >
            {logScale && t >= 1000 ? `${t / 1000}kb` : `${t}bp`}
          </text>
        ))}
      </svg>

      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
        {hover
          ? `${hover.length_bin}–${hover.length_bin + 10}bp: ${hover.count.toLocaleString()} reads`
          : sampledReads
            ? `sampled ${sampledReads.toLocaleString()} reads · hover for detail`
            : "hover for detail"}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/SequenceCharts.tsx
git commit -m "feat(frontend): add LengthDistributionChart component"
```

---

### Task 5: Wire the chart into `QcTab`

**Files:**
- Modify: `frontend/src/components/DetailPanel.tsx:955-1030`

- [ ] **Step 1: Import the new symbols**

Find the existing import of `BaseCompositionChart`/`QualityChart` in `frontend/src/components/DetailPanel.tsx` (near the top of the file) and add `LengthDistributionChart` to it:

```tsx
import { BaseCompositionChart, QualityChart, LengthDistributionChart } from "./SequenceCharts";
```

(Match against whatever the existing import line actually looks like — add `LengthDistributionChart` to that same named-import list rather than creating a second import statement.)

- [ ] **Step 2: Read the fact and determine axis scale**

In `QcTab` (`DetailPanel.tsx`), immediately after the existing `curve` declaration (currently lines 961-964):

```tsx
  const lengthHistogram = Array.isArray(obj.facts.read_length_histogram)
    ? obj.facts.read_length_histogram
    : null;
  // Log axis for the platforms whose read lengths span orders of magnitude;
  // everything else (including "QC never run yet", the common raw-upload
  // case) defaults to linear, matching the reference FastQC single-peak
  // shape. Mirrors LONG_READ_PLATFORMS in backend/app/pipelines/qc_stats.py.
  const isLongReadPlatform =
    obj.facts.qc_platform === "OXFORD_NANOPORE" || obj.facts.qc_platform === "PACBIO_SMRT";
```

- [ ] **Step 3: Render the chart as a third card**

In the `.qc-charts` grid's condition and body (currently lines 1006-1030), change the opening condition and add the new card after the `curve` block:

```tsx
    {(composition || curve || lengthHistogram || showChromStrip) && (
      <div className="qc-charts">
        {composition && (
          <div className="qc-chart">
            <div className="section-title">Base composition</div>
            <BaseCompositionChart
              composition={composition as never}
              sampledReads={obj.facts.stats_sampled_reads as number | undefined}
              sampledBases={obj.facts.stats_sampled_bases as number | undefined}
              gcPercent={obj.facts.gc_content_percent as number | undefined}
            />
          </div>
        )}
        {curve && (
          <div className="qc-chart">
            <div className="section-title">Quality per position</div>
            <QualityChart curve={curve as never} />
          </div>
        )}
        {lengthHistogram && (
          <div className="qc-chart">
            <div className="section-title">Read length distribution</div>
            <LengthDistributionChart
              buckets={lengthHistogram as never}
              logScale={isLongReadPlatform}
              sampledReads={obj.facts.stats_sampled_reads as number | undefined}
            />
          </div>
        )}
        {showChromStrip && <ChromosomeStrip facts={obj.facts} />}
      </div>
    )}
```

- [ ] **Step 4: Verify the frontend typechecks**

Run: `docker compose exec web npm run lint`
Expected: no errors

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx
git commit -m "feat(frontend): render read length distribution chart in QC tab"
```

---

### Task 6: Manual verification in the browser

**Files:** none (verification only)

- [ ] **Step 1: Bring up this worktree's isolated stack**

```bash
./ops/worktree-up.sh
```

Expected output includes the UI URL, `http://localhost:5273`.

- [ ] **Step 2: Upload or select a short-read FASTQ**

Navigate to `http://localhost:5273`, open (or upload) a short-read (Illumina) FASTQ object, open its detail panel, go to the QC tab.

Expected: a third chart card titled "Read length distribution" appears in the `.qc-charts` grid, alongside Base composition and Quality per position, showing a bar chart with a **linear** x-axis (bp labels, not kb).

- [ ] **Step 3: Upload or select a long-read FASTQ and run long-read QC**

Open a PacBio or ONT FASTQ object (or upload one), run the long-read QC pipeline step on it (so `qc_platform` gets set to `OXFORD_NANOPORE` or `PACBIO_SMRT`), then reopen its QC tab.

Expected: the same chart renders with a **log-scale** x-axis (labels like `1kb`, `10kb`).

- [ ] **Step 4: Confirm hover interaction**

Hover over bars in both charts from steps 2 and 3.

Expected: the summary line below the SVG updates to show the hovered bucket's range and read count, matching the existing hover behavior of `QualityChart`.

- [ ] **Step 5: Confirm a FASTA reference shows no length-distribution card**

Open a reference (FASTA) object's QC tab.

Expected: no "Read length distribution" card appears (references have no `read_length_histogram` fact, since only `fastq_stats`/`alignment_stats` produce it, not `fasta_stats`).

- [ ] **Step 6: Tear down the worktree stack**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 7: Confirm the main 5173 stack is unaffected**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: mount source is the main checkout root, not this worktree's path — `worktree-up.sh` uses a separate `COMPOSE_PROJECT_NAME`, so this should already be true, but this step confirms nothing accidentally repointed the main stack.

---

## Self-Review Notes

- **Spec coverage:** Backend histogram in both `fastq_stats` and `alignment_stats` (Tasks 1-2), platform-agnostic bin width with no cap (Task 1 Step 3-4), frontend chart in house style with hover + sampled-N footnote (Task 4), platform-aware log/linear axis chosen at render time from `qc_platform` (Task 5 Step 2), wired into the existing `.qc-charts` grid as a third card (Task 5 Step 3), type addition (Task 3). All spec sections are covered.
- **Non-goals respected:** no FastQC/fastp histogram parsing, no BAM wiring into long-read intake, no platform-awareness added to the backend sampler (Task 1/2 stay platform-agnostic; only `DetailPanel.tsx` in Task 5 reads `qc_platform`).
- **Type consistency:** `length_bin`/`count` field names match between the Python dict emitted in Tasks 1-2, the `ReadLengthHistogramBucket`/`LengthBucket` TS interfaces in Tasks 3-4, and every consuming site in Task 5.
