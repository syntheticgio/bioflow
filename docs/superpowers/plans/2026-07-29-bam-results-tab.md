# BAM Results Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-object Results tab for BAM files showing alignment summary, birds-eye coverage across the reference, a cumulative depth curve, a paginated/downloadable per-contig table, insert-size and MAPQ histograms, and provenance — backed by a new read-only `run_bam_stats` job that works on any ready BAM, imported or pipeline-produced.

**Architecture:** A new pure-function module (`bam_stats_runner.py`) builds samtools commands and parses their text output; a new job handler (in `align_handlers.py`, since it shares that module's BAM/reference resolution helpers) runs `idxstats` + `coverage` + binned `depth`, and the existing sampled pass in `sequence_stats.alignment_stats` gains insert-size and MAPQ histograms almost for free. Results split across storage: a bounded summary (~1000-bin coverage array, top-N contigs, histograms) merges into `facts` exactly like QC does; the complete per-contig table is written as a TSV under a new `bam_stats_dir`, served by a new paginated/download route that reuses `get_qc_report`'s path-traversal guards. The frontend gets a new "Results" tab, a hand-rolled SVG coverage chart (matching `SequenceCharts.tsx`'s no-library convention), and a paginated contig table — `AlignmentReport` moves there from QC outright and gains a fallback so it renders for imported BAMs too.

**Tech Stack:** Python (FastAPI, Beanie/Mongo, pysam, samtools via subprocess), React + TanStack Query + react-router, hand-rolled SVG for charts, pytest for backend tests, manual browser verification for the frontend (per CLAUDE.md — no headless component-testing setup exists or is expected).

---

## Before you start

This repo runs as a single Docker Compose stack with hot reload on `api` (uvicorn --reload) and `web` (vite dev) but **not** on `worker`. After any change touching `align_handlers.py`, `pipeline_handlers.py`, `bam_stats_runner.py`, or anything they import, run:

```bash
docker compose restart worker
```

before testing a job through the UI — otherwise the worker silently keeps running the old in-memory code.

Backend tests run inside the container, not a host venv:

```bash
docker compose exec api python -m pytest tests/ -q
```

---

## Task 1: `bam_stats_runner` — command construction and parsing (pure functions)

**Files:**
- Create: `backend/app/pipelines/bam_stats_runner.py`
- Test: `backend/tests/pipelines/test_bam_stats_runner.py`

This mirrors `align_runner.py`: pure functions over strings/paths, no queue, no filesystem, no pysam — so every case here is a fast unit test.

- [ ] **Step 1: Write the failing tests for command construction**

```python
"""Command construction and output parsing for BAM results statistics.

Pure functions over strings and paths, mirroring align_runner.py: the parts
worth testing in isolation, with no queue or filesystem involved.
"""

from pathlib import Path

from app.pipelines.bam_stats_runner import (
    BIN_COUNT,
    bin_depth,
    build_coverage_command,
    build_depth_command,
    build_idxstats_command,
    contigs_from_coverage,
    parse_coverage,
    parse_idxstats,
)


class TestCommandConstruction:
    def test_idxstats_command(self):
        cmd = build_idxstats_command(
            samtools_path="/usr/bin/samtools", bam=Path("/work/a.bam")
        )
        assert cmd == ["/usr/bin/samtools", "idxstats", "/work/a.bam"]

    def test_coverage_command(self):
        cmd = build_coverage_command(
            samtools_path="/usr/bin/samtools", bam=Path("/work/a.bam")
        )
        assert cmd == ["/usr/bin/samtools", "coverage", "/work/a.bam"]

    def test_depth_command_is_unfiltered_and_all_positions(self):
        """-a includes zero-depth positions -- required for a birds-eye view
        that must not silently skip uncovered regions -- and -a reports every
        position rather than only covered ones."""
        cmd = build_depth_command(
            samtools_path="/usr/bin/samtools", bam=Path("/work/a.bam")
        )
        assert cmd == ["/usr/bin/samtools", "depth", "-a", "/work/a.bam"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipelines.bam_stats_runner'`

- [ ] **Step 3: Implement command construction**

```python
"""Building and parsing samtools output for the BAM Results tab.

Kept separate from the job handler so the parts worth testing -- command
construction, idxstats/coverage parsing, depth binning -- are pure functions
over strings, lists, and paths, with no queue or filesystem involved. Mirrors
align_runner.py's split for the same reason.
"""

from pathlib import Path

# Fixed regardless of genome size, so the array in `facts` is a constant size
# whether the reference is a 5 kb plasmid or a 3 Gb human genome. A contig
# shorter than one bin still gets one bin (see bin_depth), so small contigs
# never vanish from the plot.
BIN_COUNT = 1000


def build_idxstats_command(*, samtools_path: str, bam: Path) -> list[str]:
    """Reads and unmapped counts per contig, from the index alone.

    No traversal of the BAM body -- just the `.bai`'s own counters -- so this
    is effectively instant regardless of file size.
    """
    return [samtools_path, "idxstats", str(bam)]


def build_coverage_command(*, samtools_path: str, bam: Path) -> list[str]:
    """Per-contig mean depth, breadth of coverage, and mean base/mapping
    quality. One pass over the BAM."""
    return [samtools_path, "coverage", str(bam)]


def build_depth_command(*, samtools_path: str, bam: Path) -> list[str]:
    """Per-base depth for every position on every contig.

    `-a` outputs zero-depth positions too -- omitting them would make an
    uncovered region indistinguishable from "not read yet" when this output is
    binned, and the whole point of the birds-eye view is to show gaps.
    """
    return [samtools_path, "depth", "-a", str(bam)]
```

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/bam_stats_runner.py backend/tests/pipelines/test_bam_stats_runner.py
git commit -m "feat: bam_stats_runner command construction"
```

---

## Task 2: Parsing `idxstats` and `coverage` output

**Files:**
- Modify: `backend/app/pipelines/bam_stats_runner.py`
- Modify: `backend/tests/pipelines/test_bam_stats_runner.py`

`samtools idxstats` output is tab-separated, one line per contig plus a trailing `*` line for unmapped reads with no coordinate:
```
chr1	248956422	1200000	300
chr2	242193529	980000	150
*	0	0	42
```
Columns: name, length, mapped reads, unmapped reads.

`samtools coverage` output is a header line starting with `#` followed by tab-separated rows:
```
#rname	startpos	endpos	numreads	covbases	coverage	meandepth	meanbaseq	meanmapq
chr1	1	248956422	1200000	248900000	99.98	32.4	36.1	58.2
chr2	1	242193529	980000	241800000	99.83	28.1	35.9	57.8
```

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_bam_stats_runner.py`:

```python
IDXSTATS_OUTPUT = (
    "chr1\t248956422\t1200000\t300\n"
    "chr2\t242193529\t980000\t150\n"
    "*\t0\t0\t42\n"
)

COVERAGE_OUTPUT = (
    "#rname\tstartpos\tendpos\tnumreads\tcovbases\tcoverage\tmeandepth\tmeanbaseq\tmeanmapq\n"
    "chr1\t1\t248956422\t1200000\t248900000\t99.98\t32.4\t36.1\t58.2\n"
    "chr2\t1\t242193529\t980000\t241800000\t99.83\t28.1\t35.9\t57.8\n"
)


class TestParseIdxstats:
    def test_parses_each_contig(self):
        rows = parse_idxstats(IDXSTATS_OUTPUT)
        assert rows[0] == {
            "contig": "chr1",
            "length": 248956422,
            "mapped_reads": 1200000,
            "unmapped_reads": 300,
        }
        assert rows[1]["contig"] == "chr2"

    def test_the_trailing_star_row_is_unplaced_not_a_contig(self):
        """The '*' row holds unmapped reads with no coordinate at all -- not a
        real contig, and length 0 would otherwise poison a birds-eye plot that
        assumes every row has a positive length."""
        rows = parse_idxstats(IDXSTATS_OUTPUT)
        names = [r["contig"] for r in rows]
        assert "*" not in names

    def test_empty_output_is_empty_list(self):
        assert parse_idxstats("") == []


class TestParseCoverage:
    def test_parses_each_contig(self):
        rows = parse_coverage(COVERAGE_OUTPUT)
        assert rows[0] == {
            "contig": "chr1",
            "start": 1,
            "end": 248956422,
            "reads": 1200000,
            "covered_bases": 248900000,
            "coverage_pct": 99.98,
            "mean_depth": 32.4,
            "mean_baseq": 36.1,
            "mean_mapq": 58.2,
        }
        assert rows[1]["contig"] == "chr2"

    def test_header_line_is_not_a_data_row(self):
        rows = parse_coverage(COVERAGE_OUTPUT)
        assert len(rows) == 2

    def test_empty_output_is_empty_list(self):
        assert parse_coverage("") == []


class TestContigsFromCoverage:
    def test_merges_idxstats_and_coverage_by_contig_name(self):
        """The table needs both: coverage has depth and breadth, idxstats has
        the unmapped count coverage does not report."""
        idx = parse_idxstats(IDXSTATS_OUTPUT)
        cov = parse_coverage(COVERAGE_OUTPUT)
        contigs = contigs_from_coverage(idxstats_rows=idx, coverage_rows=cov)
        chr1 = next(c for c in contigs if c["contig"] == "chr1")
        assert chr1["length"] == 248956422
        assert chr1["reads"] == 1200000
        assert chr1["unmapped_reads"] == 300
        assert chr1["mean_depth"] == 32.4

    def test_sorted_by_reads_descending(self):
        """The table's default order and the top-N slice both want the most
        active contigs first."""
        idx = parse_idxstats(IDXSTATS_OUTPUT)
        cov = parse_coverage(COVERAGE_OUTPUT)
        contigs = contigs_from_coverage(idxstats_rows=idx, coverage_rows=cov)
        assert [c["contig"] for c in contigs] == ["chr1", "chr2"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: FAIL with `ImportError` (parse_idxstats, parse_coverage, contigs_from_coverage not defined)

- [ ] **Step 3: Implement the parsers**

Append to `backend/app/pipelines/bam_stats_runner.py`:

```python
def parse_idxstats(text: str) -> list[dict]:
    """Reads and unmapped counts per contig from `samtools idxstats`.

    The trailing `*` row (unmapped reads with no coordinate) is dropped: it is
    not a contig, has no length, and would poison anything that assumes every
    row spans a positive-length interval.
    """
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4 or parts[0] == "*":
            continue
        rows.append(
            {
                "contig": parts[0],
                "length": int(parts[1]),
                "mapped_reads": int(parts[2]),
                "unmapped_reads": int(parts[3]),
            }
        )
    return rows


def parse_coverage(text: str) -> list[dict]:
    """Per-contig depth and breadth from `samtools coverage`.

    The header line starts with '#' and is skipped; every other line is one
    contig's row.
    """
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 9:
            continue
        rows.append(
            {
                "contig": parts[0],
                "start": int(parts[1]),
                "end": int(parts[2]),
                "reads": int(parts[3]),
                "covered_bases": int(parts[4]),
                "coverage_pct": float(parts[5]),
                "mean_depth": float(parts[6]),
                "mean_baseq": float(parts[7]),
                "mean_mapq": float(parts[8]),
            }
        )
    return rows


def contigs_from_coverage(*, idxstats_rows: list[dict], coverage_rows: list[dict]) -> list[dict]:
    """Merge idxstats and coverage by contig name into one per-contig table.

    `coverage` does not report unmapped reads and `idxstats` does not report
    depth or breadth, so the full table needs both. Sorted by mapped reads
    descending: the same order the table defaults to and the top-N summary
    slices from.
    """
    unmapped_by_contig = {r["contig"]: r["unmapped_reads"] for r in idxstats_rows}
    merged = [
        {**row, "unmapped_reads": unmapped_by_contig.get(row["contig"], 0)}
        for row in coverage_rows
    ]
    merged.sort(key=lambda r: r["reads"], reverse=True)
    return merged
```

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/bam_stats_runner.py backend/tests/pipelines/test_bam_stats_runner.py
git commit -m "feat: parse samtools idxstats and coverage output"
```

---

## Task 3: Binning depth for the birds-eye plot

**Files:**
- Modify: `backend/app/pipelines/bam_stats_runner.py`
- Modify: `backend/tests/pipelines/test_bam_stats_runner.py`

`samtools depth -a` output is tab-separated `contig  position  depth`, one line per base, in contig order. This must be binned into `BIN_COUNT` (1000) buckets **across the whole reference laid end-to-end**, without holding every line in memory (a human genome is ~3 billion lines).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_bam_stats_runner.py`:

```python
class TestBinDepth:
    def test_bins_a_single_contig_into_the_requested_count(self):
        """10 positions binned into 5 bins -- each bin covers 2 positions."""
        contig_lengths = [("chr1", 10)]
        depth_lines = [f"chr1\t{p}\t{p}" for p in range(1, 11)]  # depth == position
        bins, boundaries = bin_depth(
            contig_lengths=contig_lengths, depth_lines=iter(depth_lines), bin_count=5
        )
        assert len(bins) == 5
        # bin 0 covers positions 1-2 (depth 1,2) -> mean 1.5
        assert bins[0] == 1.5
        # bin 4 covers positions 9-10 (depth 9,10) -> mean 9.5
        assert bins[4] == 9.5
        assert boundaries == [{"contig": "chr1", "bin_start": 0}]

    def test_a_contig_shorter_than_one_bin_still_gets_a_bin(self):
        """A 3-contig genome where one contig is tiny must not vanish: every
        contig gets at least one bin regardless of its share of total length."""
        contig_lengths = [("chr1", 1000), ("scaffold_1", 1), ("chr2", 1000)]
        depth_lines = (
            [f"chr1\t{p}\t10" for p in range(1, 1001)]
            + ["scaffold_1\t1\t99"]
            + [f"chr2\t{p}\t20" for p in range(1, 1001)]
        )
        bins, boundaries = bin_depth(
            contig_lengths=contig_lengths, depth_lines=iter(depth_lines), bin_count=10
        )
        contig_names = [b["contig"] for b in boundaries]
        assert "scaffold_1" in contig_names
        # The scaffold's one bin reflects its own depth, not blended with a
        # neighbour's -- 99, not something between 10 and 20.
        scaffold_bin_start = next(
            b["bin_start"] for b in boundaries if b["contig"] == "scaffold_1"
        )
        assert bins[scaffold_bin_start] == 99

    def test_bin_count_is_constant_regardless_of_reference_size(self):
        small = bin_depth(
            contig_lengths=[("c1", 100)],
            depth_lines=iter(f"c1\t{p}\t5" for p in range(1, 101)),
            bin_count=1000,
        )
        large = bin_depth(
            contig_lengths=[("c1", 1_000_000)],
            depth_lines=iter(f"c1\t{p}\t5" for p in range(1, 1_000_001)),
            bin_count=1000,
        )
        assert len(small[0]) == len(large[0]) == 1000

    def test_positions_absent_from_depth_output_count_as_zero(self):
        """`-a` should mean every position is present, but a defensive default
        of zero-depth for a missing position keeps a truncated tool run from
        producing a bin count mismatch rather than a silently wrong plot."""
        bins, _ = bin_depth(
            contig_lengths=[("chr1", 4)],
            depth_lines=iter(["chr1\t1\t10", "chr1\t3\t20"]),  # positions 2, 4 missing
            bin_count=4,
        )
        assert bins == [10.0, 0.0, 20.0, 0.0]
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: FAIL with `ImportError: cannot import name 'bin_depth'`

- [ ] **Step 3: Implement binning**

Append to `backend/app/pipelines/bam_stats_runner.py`:

```python
from collections.abc import Iterator


def bin_depth(
    *,
    contig_lengths: list[tuple[str, int]],
    depth_lines: Iterator[str],
    bin_count: int = BIN_COUNT,
) -> tuple[list[float], list[dict]]:
    """Bin per-base depth into a fixed-size array across the whole reference.

    Bins are allocated proportionally to each contig's length, laid end to
    end, with one floor: every contig gets at least one bin regardless of its
    share of the total, so a short scaffold is never averaged away into a
    neighbour's bin or omitted from the plot entirely.

    `depth_lines` is consumed once, in the streaming order `samtools depth -a`
    produces (contig order, then position order) -- never materialized as a
    list, since a whole-genome depth file is one line per base.

    Returns `(bins, boundaries)`: `bins` is the mean depth per bin, and
    `boundaries` marks which bin index starts each contig, for drawing
    separators and axis labels.
    """
    total_length = sum(length for _, length in contig_lengths)
    if total_length <= 0 or bin_count <= 0:
        return [], []

    # Proportional allocation with a floor of 1 bin per contig. Remaining bins
    # (after the floor) are handed out by length share; any leftover from
    # rounding goes to the last contig so the total is always exactly
    # bin_count.
    n = len(contig_lengths)
    floor_bins = min(bin_count, n)
    remaining_bins = bin_count - floor_bins
    remaining_length = total_length

    contig_bin_counts: dict[str, int] = {}
    for name, length in contig_lengths:
        share = round(remaining_bins * length / remaining_length) if remaining_length else 0
        contig_bin_counts[name] = 1 + share

    allocated = sum(contig_bin_counts.values())
    if allocated != bin_count and contig_lengths:
        last_name = contig_lengths[-1][0]
        contig_bin_counts[last_name] += bin_count - allocated

    bin_sum = [0.0] * bin_count
    bin_n = [0] * bin_count

    boundaries = []
    bin_offset = 0
    contig_start_bin = {}
    for name, length in contig_lengths:
        contig_start_bin[name] = bin_offset
        boundaries.append({"contig": name, "bin_start": bin_offset})
        bins_for_contig = contig_bin_counts[name]
        positions_per_bin = max(length / bins_for_contig, 1)

        current_contig_meta = (bin_offset, positions_per_bin, length)
        bin_offset += bins_for_contig
        contig_bin_meta = current_contig_meta
        # Stash for use while consuming depth_lines below.
        contig_lengths_meta = contig_bin_meta  # noqa: F841

    # Second pass: consume the stream, mapping each line to its bin.
    # Rebuilt per-contig geometry here rather than reusing the loop above,
    # since that loop's per-contig state does not survive it.
    geometry: dict[str, tuple[int, float]] = {}
    offset = 0
    for name, length in contig_lengths:
        bins_for_contig = contig_bin_counts[name]
        positions_per_bin = max(length / bins_for_contig, 1)
        geometry[name] = (offset, positions_per_bin)
        offset += bins_for_contig

    for line in depth_lines:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        contig, pos_str, depth_str = parts
        if contig not in geometry:
            continue
        start_bin, positions_per_bin = geometry[contig]
        position = int(pos_str)
        offset_in_contig = min(
            int((position - 1) / positions_per_bin), contig_bin_counts[contig] - 1
        )
        idx = start_bin + offset_in_contig
        bin_sum[idx] += float(depth_str)
        bin_n[idx] += 1

    bins = [bin_sum[i] / bin_n[i] if bin_n[i] else 0.0 for i in range(bin_count)]
    return bins, boundaries
```

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Clean up the dead intermediate variables from the first pass**

The implementation above computes contig geometry twice (an artifact of drafting) — the first loop's `contig_bin_meta`/`contig_lengths_meta` locals are never used. Replace the whole function body with the cleaned-up single-pass-of-setup version:

```python
def bin_depth(
    *,
    contig_lengths: list[tuple[str, int]],
    depth_lines: Iterator[str],
    bin_count: int = BIN_COUNT,
) -> tuple[list[float], list[dict]]:
    """Bin per-base depth into a fixed-size array across the whole reference.

    Bins are allocated proportionally to each contig's length, laid end to
    end, with one floor: every contig gets at least one bin regardless of its
    share of the total, so a short scaffold is never averaged away into a
    neighbour's bin or omitted from the plot entirely.

    `depth_lines` is consumed once, in the streaming order `samtools depth -a`
    produces (contig order, then position order) -- never materialized as a
    list, since a whole-genome depth file is one line per base.

    Returns `(bins, boundaries)`: `bins` is the mean depth per bin, and
    `boundaries` marks which bin index starts each contig, for drawing
    separators and axis labels.
    """
    total_length = sum(length for _, length in contig_lengths)
    if total_length <= 0 or bin_count <= 0:
        return [], []

    n = len(contig_lengths)
    floor_bins = min(bin_count, n)
    remaining_bins = bin_count - floor_bins

    contig_bin_counts: dict[str, int] = {}
    for name, length in contig_lengths:
        share = round(remaining_bins * length / total_length) if total_length else 0
        contig_bin_counts[name] = 1 + share

    # Rounding can miss the exact total by a few bins either way; any
    # discrepancy is absorbed by the last contig so the sum is always exactly
    # bin_count.
    allocated = sum(contig_bin_counts.values())
    if allocated != bin_count and contig_lengths:
        last_name = contig_lengths[-1][0]
        contig_bin_counts[last_name] += bin_count - allocated

    geometry: dict[str, tuple[int, float]] = {}
    boundaries = []
    offset = 0
    for name, length in contig_lengths:
        bins_for_contig = contig_bin_counts[name]
        positions_per_bin = max(length / bins_for_contig, 1)
        geometry[name] = (offset, positions_per_bin)
        boundaries.append({"contig": name, "bin_start": offset})
        offset += bins_for_contig

    bin_sum = [0.0] * bin_count
    bin_n = [0] * bin_count

    for line in depth_lines:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        contig, pos_str, depth_str = parts
        if contig not in geometry:
            continue
        start_bin, positions_per_bin = geometry[contig]
        position = int(pos_str)
        offset_in_contig = min(
            int((position - 1) / positions_per_bin), contig_bin_counts[contig] - 1
        )
        idx = start_bin + offset_in_contig
        bin_sum[idx] += float(depth_str)
        bin_n[idx] += 1

    bins = [bin_sum[i] / bin_n[i] if bin_n[i] else 0.0 for i in range(bin_count)]
    return bins, boundaries
```

- [ ] **Step 6: Run to verify it still passes**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: PASS (14 tests)

- [ ] **Step 7: Commit**

```bash
git add backend/app/pipelines/bam_stats_runner.py
git commit -m "refactor: remove dead intermediate state in bin_depth"
```

---

## Task 4: Cumulative coverage curve and genome-wide summary

**Files:**
- Modify: `backend/app/pipelines/bam_stats_runner.py`
- Modify: `backend/tests/pipelines/test_bam_stats_runner.py`

The cumulative curve ("fraction of reference at ≥X depth") and the summary (mean depth, % covered at 1×/10×/30×) are both derived from the same per-contig `coverage` rows plus the binned array — no new samtools invocation.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_bam_stats_runner.py`:

```python
class TestCumulativeCoverage:
    def test_all_bins_at_or_above_threshold_are_counted(self):
        """A flat depth-10 genome: 100% of bins are at or above every
        threshold up to 10, and 0% above it."""
        curve = cumulative_coverage(bins=[10.0] * 100, thresholds=[1, 5, 10, 20])
        by_threshold = {c["depth"]: c["fraction"] for c in curve}
        assert by_threshold[1] == 1.0
        assert by_threshold[10] == 1.0
        assert by_threshold[20] == 0.0

    def test_mixed_depth_gives_a_partial_fraction(self):
        bins = [0.0] * 25 + [15.0] * 75  # 75% of the genome at depth 15
        curve = cumulative_coverage(bins=bins, thresholds=[1, 10, 20])
        by_threshold = {c["depth"]: c["fraction"] for c in curve}
        assert by_threshold[1] == 0.75
        assert by_threshold[10] == 0.75
        assert by_threshold[20] == 0.0

    def test_empty_bins_is_an_empty_curve(self):
        assert cumulative_coverage(bins=[], thresholds=[1, 10]) == []


class TestGenomeSummary:
    def test_summarizes_across_all_contigs(self):
        contigs = [
            {
                "contig": "chr1", "length": 100, "reads": 50, "unmapped_reads": 5,
                "covered_bases": 90, "coverage_pct": 90.0, "mean_depth": 10.0,
                "mean_baseq": 35.0, "mean_mapq": 55.0, "start": 1, "end": 100,
            },
            {
                "contig": "chr2", "length": 200, "reads": 80, "unmapped_reads": 2,
                "covered_bases": 200, "coverage_pct": 100.0, "mean_depth": 20.0,
                "mean_baseq": 36.0, "mean_mapq": 58.0, "start": 1, "end": 200,
            },
        ]
        summary = genome_summary(contigs=contigs, bins=[5.0] * 5 + [15.0] * 5)
        assert summary["total_contigs"] == 2
        assert summary["mapped_reads"] == 130
        assert summary["unmapped_reads"] == 7
        # Length-weighted mean depth: (100*10 + 200*20) / 300 = 16.67
        assert round(summary["mean_depth"], 2) == 16.67
        assert summary["pct_covered_1x"] == 100.0  # all 10 bins are >0
        assert summary["pct_covered_10x"] == 50.0  # 5 of 10 bins are >=10
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: FAIL with `ImportError: cannot import name 'cumulative_coverage'`

- [ ] **Step 3: Implement**

Append to `backend/app/pipelines/bam_stats_runner.py`:

```python
# The thresholds the summary and cumulative curve report against. 1x is
# "sequenced at all"; 10x and 30x are the conventional thresholds for calling
# a heterozygous and a somatic variant respectively.
COVERAGE_THRESHOLDS = (1, 10, 30)


def cumulative_coverage(*, bins: list[float], thresholds: list[int]) -> list[dict]:
    """Fraction of the binned reference at or above each depth threshold.

    Answers "did I sequence deep enough" directly, which a per-contig mean
    depth does not: a genome that is 50% at 60x and 50% at 0x has the same
    mean as one evenly covered at 30x, and only this curve tells them apart.
    """
    if not bins:
        return []
    total = len(bins)
    return [
        {"depth": t, "fraction": round(sum(1 for b in bins if b >= t) / total, 4)}
        for t in thresholds
    ]


def genome_summary(*, contigs: list[dict], bins: list[float]) -> dict:
    """Genome-wide totals: the numbers a person checks before looking at any
    per-contig detail."""
    total_length = sum(c["length"] for c in contigs)
    mapped_reads = sum(c["reads"] for c in contigs)
    unmapped_reads = sum(c["unmapped_reads"] for c in contigs)

    mean_depth = (
        round(sum(c["length"] * c["mean_depth"] for c in contigs) / total_length, 2)
        if total_length
        else 0.0
    )

    summary = {
        "total_contigs": len(contigs),
        "total_length": total_length,
        "mapped_reads": mapped_reads,
        "unmapped_reads": unmapped_reads,
        "mean_depth": mean_depth,
    }

    if bins:
        n = len(bins)
        for threshold in COVERAGE_THRESHOLDS:
            pct = round(100 * sum(1 for b in bins if b >= threshold) / n, 2)
            summary[f"pct_covered_{threshold}x"] = pct

    return summary
```

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: PASS (18 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/bam_stats_runner.py backend/tests/pipelines/test_bam_stats_runner.py
git commit -m "feat: cumulative coverage curve and genome-wide summary"
```

---

## Task 5: TSV serialization for the full per-contig report

**Files:**
- Modify: `backend/app/pipelines/bam_stats_runner.py`
- Modify: `backend/tests/pipelines/test_bam_stats_runner.py`

The complete per-contig table (every contig, no truncation) is written to disk as TSV. This function produces the text; the handler (Task 6) writes it to a file.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_bam_stats_runner.py`:

```python
class TestContigsTsv:
    def test_header_and_rows(self):
        contigs = [
            {
                "contig": "chr1", "length": 100, "reads": 50, "unmapped_reads": 5,
                "covered_bases": 90, "coverage_pct": 90.0, "mean_depth": 10.0,
                "mean_baseq": 35.0, "mean_mapq": 55.0, "start": 1, "end": 100,
            },
        ]
        text = contigs_tsv(contigs)
        lines = text.splitlines()
        assert lines[0] == (
            "contig\tlength\treads\tunmapped_reads\tcovered_bases"
            "\tcoverage_pct\tmean_depth\tmean_baseq\tmean_mapq"
        )
        assert lines[1] == "chr1\t100\t50\t5\t90\t90.0\t10.0\t35.0\t55.0"

    def test_empty_contigs_is_header_only(self):
        text = contigs_tsv([])
        assert text.splitlines() == [
            "contig\tlength\treads\tunmapped_reads\tcovered_bases"
            "\tcoverage_pct\tmean_depth\tmean_baseq\tmean_mapq"
        ]
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: FAIL with `ImportError: cannot import name 'contigs_tsv'`

- [ ] **Step 3: Implement**

Append to `backend/app/pipelines/bam_stats_runner.py`:

```python
CONTIGS_TSV_COLUMNS = (
    "contig",
    "length",
    "reads",
    "unmapped_reads",
    "covered_bases",
    "coverage_pct",
    "mean_depth",
    "mean_baseq",
    "mean_mapq",
)


def contigs_tsv(contigs: list[dict]) -> str:
    """The complete per-contig table as TSV, for the downloadable report.

    Every contig, no truncation -- unlike `bam_stats_contigs_top` in facts,
    which is capped for storage. Column order matches CONTIGS_TSV_COLUMNS
    exactly, which the report-serving route's pagination also reads by.
    """
    lines = ["\t".join(CONTIGS_TSV_COLUMNS)]
    for c in contigs:
        lines.append("\t".join(str(c[col]) for col in CONTIGS_TSV_COLUMNS))
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: PASS (20 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/bam_stats_runner.py backend/tests/pipelines/test_bam_stats_runner.py
git commit -m "feat: TSV serialization for the per-contig report"
```

---

## Task 6: Insert-size and MAPQ histograms on the existing sampled pass

**Files:**
- Modify: `backend/app/storage/sequence_stats.py:242-352` (the `alignment_stats` function)
- Modify: `backend/tests/pipelines/test_qc_stats.py` — actually create `backend/tests/storage/test_sequence_stats.py` (no existing test file for this module; check first)

- [ ] **Step 0: Check for an existing test file**

Run: `find backend/tests -iname "*sequence_stats*"`
Expected: no output (confirms a new file is needed). If a file exists, add to it instead of creating a new one.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/storage/test_sequence_stats.py`:

```python
"""Alignment statistics from a bounded sample of BAM records.

Builds tiny real BAM files with pysam rather than mocking it: the reverse-
complement handling and MAPQ/insert-size bookkeeping are exactly the kind of
off-by-one logic that a mock would hide.
"""

from pathlib import Path

import pysam
import pytest

from app.storage import sequence_stats


def _write_bam(path: Path, records: list[dict]) -> None:
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": "chr1", "LN": 1000}],
    }
    with pysam.AlignmentFile(str(path), "wb", header=header) as af:
        for r in records:
            a = pysam.AlignedSegment(af.header)
            a.query_name = r["name"]
            a.query_sequence = r.get("seq", "ACGT")
            a.flag = r.get("flag", 0)
            a.reference_id = 0
            a.reference_start = r.get("pos", 0)
            a.mapping_quality = r.get("mapq", 0)
            a.cigarstring = r.get("cigar", f"{len(r.get('seq', 'ACGT'))}M")
            a.query_qualities = pysam.qualitystring_to_array("I" * len(r.get("seq", "ACGT")))
            if "template_length" in r:
                a.template_length = r["template_length"]
            af.write(a)


@pytest.fixture
def bam_path(tmp_path):
    return tmp_path / "test.bam"


class TestMapqHistogram:
    def test_bucketed_by_mapping_quality(self, bam_path):
        _write_bam(
            bam_path,
            [
                {"name": "r1", "mapq": 0},
                {"name": "r2", "mapq": 0},
                {"name": "r3", "mapq": 60},
            ],
        )
        from app.models import FormatKind

        facts = sequence_stats.alignment_stats(bam_path, FormatKind.BAM)
        histogram = {h["mapq"]: h["count"] for h in facts["mapq_histogram"]}
        assert histogram[0] == 2
        assert histogram[60] == 1

    def test_unmapped_reads_are_excluded(self, bam_path):
        """Flag 4 is unmapped; mapping_quality on an unmapped read is not a
        meaningful measurement and must not appear in the histogram."""
        _write_bam(
            bam_path,
            [
                {"name": "r1", "flag": 4, "mapq": 0},
                {"name": "r2", "mapq": 30},
            ],
        )
        from app.models import FormatKind

        facts = sequence_stats.alignment_stats(bam_path, FormatKind.BAM)
        total = sum(h["count"] for h in facts["mapq_histogram"])
        assert total == 1


class TestInsertSizeHistogram:
    def test_positive_template_lengths_are_binned(self, bam_path):
        _write_bam(
            bam_path,
            [
                {"name": "r1", "flag": 3, "template_length": 300},
                {"name": "r2", "flag": 3, "template_length": 305},
                {"name": "r3", "flag": 3, "template_length": 500},
            ],
        )
        from app.models import FormatKind

        facts = sequence_stats.alignment_stats(bam_path, FormatKind.BAM)
        assert "insert_size_histogram" in facts
        total = sum(h["count"] for h in facts["insert_size_histogram"])
        assert total == 3

    def test_unpaired_reads_produce_no_insert_size_histogram(self, bam_path):
        """A single-end BAM has no meaningful template length -- absent, not
        a bucket of zeros, so the frontend can tell 'unpaired' from 'measured
        as zero'."""
        _write_bam(bam_path, [{"name": "r1", "flag": 0}])
        from app.models import FormatKind

        facts = sequence_stats.alignment_stats(bam_path, FormatKind.BAM)
        assert "insert_size_histogram" not in facts
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec api python -m pytest tests/storage/test_sequence_stats.py -v`
Expected: FAIL — `mapq_histogram` / `insert_size_histogram` KeyError, since `alignment_stats` does not produce them yet.

- [ ] **Step 3: Implement the histograms**

Modify `backend/app/storage/sequence_stats.py`. Add near the top-level constants (after `CANCEL_CHECK_READS`):

```python
# Insert size is binned at 10 bp resolution up to 2 kb -- fine enough to see a
# library-prep problem's characteristic shape, coarse enough that the array
# stays small regardless of how many reads are sampled.
INSERT_SIZE_BIN_WIDTH = 10
INSERT_SIZE_MAX = 2000
```

Inside `alignment_stats`, add two accumulators before the `try` block (near `mapq_sum = 0` / `mapq_n = 0`):

```python
    mapq_histogram: Counter[int] = Counter()
    insert_size_histogram: Counter[int] = Counter()
    saw_paired = False
```

Inside the `for rec in af:` loop, in the `if not rec.is_unmapped:` branch (right after `mapq_n += 1`), add:

```python
                    mapq_histogram[rec.mapping_quality] += 1
```

And after the existing `if rec.is_duplicate:` block, add:

```python
                if rec.is_paired:
                    saw_paired = True
                    # template_length is signed (mate orientation); only the
                    # forward-oriented record of a properly paired mate pair
                    # reports the positive fragment size once, so counting
                    # only positive values avoids double-counting each pair.
                    tlen = rec.template_length
                    if tlen > 0:
                        capped = min(tlen, INSERT_SIZE_MAX)
                        bucket = (capped // INSERT_SIZE_BIN_WIDTH) * INSERT_SIZE_BIN_WIDTH
                        insert_size_histogram[bucket] += 1
```

And before the final `return facts` in `alignment_stats`, add:

```python
    if mapq_histogram:
        facts["mapq_histogram"] = [
            {"mapq": mapq, "count": n} for mapq, n in sorted(mapq_histogram.items())
        ]
    if saw_paired and insert_size_histogram:
        facts["insert_size_histogram"] = [
            {"insert_size": size, "count": n}
            for size, n in sorted(insert_size_histogram.items())
        ]
```

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec api python -m pytest tests/storage/test_sequence_stats.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full existing sequence_stats-adjacent suite to check nothing broke**

Run: `docker compose exec api python -m pytest tests/storage/ tests/pipelines/ -q`
Expected: All PASS (no regressions in ingest-time parsing tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/storage/sequence_stats.py backend/tests/storage/test_sequence_stats.py
git commit -m "feat: MAPQ and insert-size histograms on the sampled alignment pass"
```

---

## Task 7: Settings — `bam_stats_dir`

**Files:**
- Modify: `backend/app/config.py`
- Test: `backend/tests/test_config.py` — check if this exists first

- [ ] **Step 0: Check for an existing config test file**

Run: `find backend/tests -maxdepth 1 -iname "*config*"`
If none exists, this task's test goes in a new `backend/tests/test_config.py`; if one exists, add to it.

- [ ] **Step 1: Write the failing test**

Create (or add to) `backend/tests/test_config.py`:

```python
"""Settings properties derived from bioinfo_home."""

from pathlib import Path

from app.config import Settings


class TestBamStatsDir:
    def test_derived_from_bioinfo_home(self):
        s = Settings(bioinfo_home=Path("/data"))
        assert s.bam_stats_dir == Path("/data/bam_stats")
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec api python -m pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'bam_stats_dir'`

- [ ] **Step 3: Implement**

In `backend/app/config.py`, add a new property immediately after the `qc_reports_dir` property (around line 132-139):

```python
    @property
    def bam_stats_dir(self) -> Path:
        """Generated BAM Results reports (the full per-contig TSV), keyed by
        object id.

        Outside objects/ deliberately, same rationale as qc_reports_dir: this
        is derivative and regenerable from the BAM itself, so content-
        addressing it would buy deduplication of something never shared and
        cost a blob record per run.
        """
        return self.bioinfo_home / "bam_stats"
```

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec api python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_config.py
git commit -m "feat: bam_stats_dir setting"
```

---

## Task 8: Launch-time prerequisite checks in `pipeline_service`

**Files:**
- Modify: `backend/app/services/pipeline_service.py`
- Test: `backend/tests/pipelines/test_bam_stats_launch.py`

Mirrors `_check_variant_callable` and the `.bai` check in `launch_variant_calling` (lines 1021-1135): refuse with an actionable `ValidationError` rather than auto-chaining `index_bam`, per the design decision.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_bam_stats_launch.py`:

```python
"""BAM Results launch rules: what may be computed and what blocks it.

Mirrors test_align_launch.py's FakeObject approach -- pure decisions, no
database or HTTP.
"""

import pytest

from app.errors import ValidationError
from app.models import FormatKind, ObjectStatus
from app.services import pipeline_service


class FakeObject:
    def __init__(
        self,
        name="aligned.bam",
        *,
        kind=FormatKind.BAM,
        status=ObjectStatus.READY,
        facts=None,
    ):
        self.name = name
        self.format = type("F", (), {"kind": kind})()
        self.status = status
        self.facts = facts or {}
        self.id = name


class TestBamStatsCallable:
    def test_accepts_a_ready_sorted_bam(self):
        pipeline_service._check_bam_stats_callable(
            FakeObject(facts={"sort_order": "coordinate"})
        )

    @pytest.mark.parametrize(
        "status",
        [ObjectStatus.UPLOADING, ObjectStatus.HASHING, ObjectStatus.INGESTING,
         ObjectStatus.ERROR, ObjectStatus.MISSING],
    )
    def test_rejects_a_file_that_is_not_ready(self, status):
        with pytest.raises(ValidationError, match="not ready"):
            pipeline_service._check_bam_stats_callable(
                FakeObject(status=status, facts={"sort_order": "coordinate"})
            )

    @pytest.mark.parametrize("kind", [FormatKind.FASTQ, FormatKind.FASTA, FormatKind.VCF])
    def test_rejects_anything_that_is_not_bam(self, kind):
        with pytest.raises(ValidationError, match="not a BAM alignment"):
            pipeline_service._check_bam_stats_callable(
                FakeObject(kind=kind, facts={"sort_order": "coordinate"})
            )

    def test_rejects_a_bam_that_is_not_coordinate_sorted(self):
        with pytest.raises(ValidationError, match="not coordinate-sorted"):
            pipeline_service._check_bam_stats_callable(
                FakeObject(facts={"sort_order": "queryname"})
            )

    def test_rejects_a_bam_with_no_recorded_sort_order(self):
        """Absent sort_order is treated the same as 'not coordinate-sorted' --
        the header parse records sort_order whenever the BAM declares one, so
        a missing value means it never declared coordinate order."""
        with pytest.raises(ValidationError, match="not coordinate-sorted"):
            pipeline_service._check_bam_stats_callable(FakeObject(facts={}))
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_launch.py -v`
Expected: FAIL with `AttributeError: module 'app.services.pipeline_service' has no attribute '_check_bam_stats_callable'`

- [ ] **Step 3: Implement `_check_bam_stats_callable`**

In `backend/app/services/pipeline_service.py`, add near `_check_variant_callable` (after its definition, before `_variant_dedup_key`):

```python
BAM_STATS_CALLABLE_KINDS = {FormatKind.BAM}


def _check_bam_stats_callable(obj: DataObject) -> None:
    """Whether a BAM is eligible for the Results job.

    Coordinate sort is checked here rather than left to samtools to fail on,
    because `coverage`/`idxstats` on an unsorted BAM do not error -- they
    quietly produce numbers that look plausible and are wrong. Missing index
    is checked separately by the caller (see launch_bam_stats), matching how
    launch_variant_calling separates "wrong file type" from "fixable
    precondition".
    """
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready for results (status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind not in BAM_STATS_CALLABLE_KINDS:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not a BAM alignment",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )
    if obj.facts.get("sort_order") != "coordinate":
        raise ValidationError(
            f"{obj.name!r} is not coordinate-sorted. Coverage statistics "
            f"require a coordinate-sorted BAM.",
            details={"object_id": str(obj.id)},
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_launch.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/pipelines/test_bam_stats_launch.py
git commit -m "feat: launch-time eligibility checks for BAM results"
```

---

## Task 9: `launch_bam_stats` service function

**Files:**
- Modify: `backend/app/services/pipeline_service.py`
- Modify: `backend/tests/pipelines/test_bam_stats_launch.py`

This is the async function the API route calls: resolves the BAM, checks the `.bai` sidecar, enqueues the job.

- [ ] **Step 1: Write the failing test**

This needs a database-backed test since `launch_bam_stats` is async and touches `DataObject.get`/queue. Check the existing pattern other async launch tests use:

Run: `find backend/tests -iname "*launch*" | xargs grep -l "async def test" 2>/dev/null`

If results show a fixture-based pattern (e.g. `backend/tests/fixtures/`), follow it. Add to `backend/tests/pipelines/test_bam_stats_launch.py`:

```python
import pytest_asyncio
from beanie import PydanticObjectId

from app.models import DataObject, ObjectRole, ObjectStatus, SidecarRole
from app.services import pipeline_service


@pytest.mark.anyio
class TestLaunchBamStats:
    async def test_missing_bai_is_reported_as_actionable(self, initialized_db, project):
        """No .bai sidecar: refuse with 'index it first' rather than silently
        queueing index_bam -- matching launch_variant_calling's decision."""
        bam = DataObject(
            project_id=project.id,
            name="aligned.bam",
            size=100,
            status=ObjectStatus.READY,
            blob_sha256="a" * 64,
            format={"kind": "bam", "confidence": "high"},
            facts={"sort_order": "coordinate"},
        )
        await bam.insert()

        with pytest.raises(Exception) as exc_info:
            await pipeline_service.launch_bam_stats(object_id=bam.id)
        assert "index" in str(exc_info.value).lower()
```

Note: adapt `initialized_db`/`project` fixture names to whatever `conftest.py` actually defines — inspect `backend/tests/conftest.py` and any `pipelines/conftest.py` before writing this step, and match the fixture names used by `test_align_launch.py`'s async counterparts (search for `async def test` across `backend/tests/pipelines/` to find one that hits the database, e.g. a trim or align launch test) so this test uses real, working fixtures rather than invented ones.

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_launch.py -v`
Expected: FAIL with `AttributeError: module 'app.services.pipeline_service' has no attribute 'launch_bam_stats'`

- [ ] **Step 3: Implement `launch_bam_stats`**

In `backend/app/services/pipeline_service.py`, add after `launch_variant_calling` (or near it, after its full body ends):

```python
async def launch_bam_stats(*, object_id: PydanticObjectId):
    """Queue the Results computation for a BAM: coverage, idxstats-derived
    per-contig counts, and binned depth across the reference.

    Read-only, like QC: no derived objects, just facts merged onto the object
    plus one TSV report on disk. Requires a coordinate-sorted, indexed BAM --
    checked here rather than left for samtools to fail confusingly on, and
    refused with an actionable message rather than auto-chaining index_bam,
    matching launch_variant_calling's documented precedent.
    """
    from app.queue import queue

    tools.require(tools.samtools())

    bam = await DataObject.get(object_id)
    if bam is None:
        raise NotFoundError(f"Object not found: {object_id}")
    _check_bam_stats_callable(bam)

    bai = await _sidecar_of_role(bam, SidecarRole.BAI)
    if bai is None:
        raise ValidationError(
            f"{bam.name!r} has no BAM index (.bai). Index it first.",
            details={"object_id": str(bam.id), "needs": "index_bam"},
        )

    digest, path = await _resolve_readable(bam)
    payload: dict = {
        "object_id": str(bam.id),
        "project_id": str(bam.project_id),
        "bam_name": bam.name,
    }
    if digest:
        payload["bam_sha256"] = digest
    if path:
        payload["bam_path"] = path

    # No parameters, like QC: a repeat over unchanged content is the same
    # result, so the object id alone is the dedup key.
    job = await queue.enqueue(
        "run_bam_stats",
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"bamstats:{bam.id}",
        project_id=bam.project_id,
        object_id=bam.id,
    )
    if job is None:
        raise ConflictError(
            "Results are already queued or running for this file",
            details={"object_id": str(bam.id)},
        )
    return job
```

Check the imports at the top of `pipeline_service.py` already include `NotFoundError`, `ConflictError`, `SidecarRole`, `JobClass`, `JobResources`, `IoClass` (they should, since `launch_variant_calling` and `launch_qc` already use them) — if any are missing, add them to the existing import block rather than a new one.

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_launch.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/pipelines/test_bam_stats_launch.py
git commit -m "feat: launch_bam_stats service function"
```

---

## Task 10: `run_bam_stats` job handler

**Files:**
- Modify: `backend/app/queue/align_handlers.py` (shares `_resolve_blob`, `tools`, `align_runner` imports already there)
- Modify: `backend/app/queue/handlers.py` — no change needed, `align_handlers` is already imported

This orchestrates: resolve the BAM, run the three samtools passes, bin depth, merge with the contig table, write the TSV, return facts.

- [ ] **Step 1: Add the handler**

In `backend/app/queue/align_handlers.py`, add the import at the top (alongside the existing `from app.pipelines import align_runner, aligners, tools`):

```python
from app.pipelines import bam_stats_runner
```

Add near the end of the file, after `index_bam`:

```python
@handler(
    "run_bam_stats",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # LIGHT io: idxstats reads only the .bai, and coverage/depth are each one
    # sequential pass -- lighter than the random access an alignment does.
    resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.LIGHT),
    max_attempts=2,
)
def run_bam_stats(ctx: JobContext) -> dict:
    """Coverage, per-contig, and binned-depth statistics for the Results tab.

    Read-only, like run_qc: derives no files except the regenerable per-contig
    TSV report. The bounded summary (binned depth, top-N contigs, histograms)
    returns as facts for `_apply_run_bam_stats` to merge onto the object; the
    complete per-contig table is written straight to settings.bam_stats_dir
    and referenced by filename.

    Prerequisites (coordinate sort, presence of a .bai) are checked before
    this job is even enqueued -- see pipeline_service.launch_bam_stats -- so a
    failure here is an actual tool problem, not a missing precondition.
    """
    samtools = tools.require(tools.samtools())

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("run_bam_stats requires an 'object_id'")

    work = _prepare_workdir(ctx, "bam_stats")
    bam_source = _resolve_blob(ctx.payload, "bam")

    bam_name = ctx.payload.get("bam_name") or "aligned.bam"
    bam = work / Path(bam_name).name
    bam.unlink(missing_ok=True)
    bam.symlink_to(bam_source)

    # The .bai must sit beside the BAM under the matching name for samtools to
    # find it; index_bam's own workdir does the same symlink dance.
    bai_source = bam_source.parent / f"{bam_source.name}{aligners.BAI_SUFFIX}"
    if not bai_source.exists():
        # Managed blobs store the .bai as a sidecar object addressed by its
        # own hash, not beside the BAM's blob -- that path comes through
        # ctx.payload directly instead.
        bai_source = Path(ctx.payload["bai_path"]) if ctx.payload.get("bai_path") else None
    if bai_source and bai_source.exists():
        bai_link = work / f"{bam.name}{aligners.BAI_SUFFIX}"
        bai_link.unlink(missing_ok=True)
        bai_link.symlink_to(bai_source)

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="idxstats", pct=0.1, message="reading index statistics")
    idxstats_path = work / "idxstats.txt"
    code = run_subprocess(
        ctx,
        bam_stats_runner.build_idxstats_command(samtools_path=samtools.path, bam=bam),
        log_path=str(idxstats_path),
    )
    if code != 0:
        raise _failure(code, idxstats_path, "samtools idxstats")
    idxstats_rows = bam_stats_runner.parse_idxstats(idxstats_path.read_text(errors="replace"))

    ctx.progress(phase="coverage", pct=0.3, message="computing per-contig coverage")
    coverage_path = work / "coverage.txt"
    code = run_subprocess(
        ctx,
        bam_stats_runner.build_coverage_command(samtools_path=samtools.path, bam=bam),
        log_path=str(coverage_path),
    )
    if code != 0:
        raise _failure(code, coverage_path, "samtools coverage")
    coverage_rows = bam_stats_runner.parse_coverage(coverage_path.read_text(errors="replace"))

    contigs = bam_stats_runner.contigs_from_coverage(
        idxstats_rows=idxstats_rows, coverage_rows=coverage_rows
    )

    ctx.progress(phase="depth", pct=0.5, message="binning coverage across the reference")
    depth_path = work / "depth.txt"
    code = run_subprocess(
        ctx,
        bam_stats_runner.build_depth_command(samtools_path=samtools.path, bam=bam),
        log_path=str(depth_path),
    )
    if code != 0:
        raise _failure(code, depth_path, "samtools depth")

    contig_lengths = [(c["contig"], c["end"]) for c in contigs]
    with open(depth_path, errors="replace") as fh:
        bins, boundaries = bam_stats_runner.bin_depth(
            contig_lengths=contig_lengths, depth_lines=fh
        )

    cumulative = bam_stats_runner.cumulative_coverage(
        bins=bins, thresholds=list(bam_stats_runner.COVERAGE_THRESHOLDS)
    )
    summary = bam_stats_runner.genome_summary(contigs=contigs, bins=bins)

    ctx.progress(phase="report", pct=0.9, message="writing the per-contig report")
    report_dir = settings.bam_stats_dir / str(object_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_name = "contigs.tsv"
    (report_dir / report_name).write_text(bam_stats_runner.contigs_tsv(contigs))

    # Capped for facts storage; the full table is the TSV written above.
    top_n = contigs[:50]

    facts = {
        "bam_stats_status": "ok",
        "bam_stats_tool_version": samtools.version,
        "bam_stats_computed_at": datetime.now(UTC).isoformat(),
        "bam_stats_summary": summary,
        "bam_stats_coverage_bins": bins,
        "bam_stats_coverage_boundaries": boundaries,
        "bam_stats_cumulative": cumulative,
        "bam_stats_contigs_top": top_n,
        "bam_stats_report": report_name,
    }

    ctx.progress(phase="done", pct=1.0, message="results complete")
    log.info(
        "bam_stats_finished",
        job_id=ctx.job_id,
        object_id=object_id,
        contigs=len(contigs),
        mean_depth=summary.get("mean_depth"),
    )

    return {
        "object_id": object_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }
```

Note: this handler assumes the BAM's `.bai` is discoverable via `_resolve_blob`'s directory convention or an explicit `bai_path` in the payload. Before finalizing, check how `variant_handlers.py` resolves the `.bai` for its BAM input (it has the identical problem — an indexed BAM whose sidecar is a separate managed object) and match that resolution exactly rather than the ad-hoc fallback sketched above. Run:

```bash
grep -n "bai" backend/app/queue/variant_handlers.py
```

and adapt the `.bai` resolution in `run_bam_stats` to reuse whatever helper or payload convention `variant_handlers.py` already established, updating `launch_bam_stats` in Task 9 to pass the same payload keys that convention expects (e.g. if it passes `bai_sha256`/`bai_path` explicitly, add that resolution to `launch_bam_stats` using `_sidecar_of_role`'s already-fetched `bai` object and `_resolve_readable(bai)`).

- [ ] **Step 2: Reconcile `.bai` resolution with `variant_handlers.py`'s convention**

Read `backend/app/queue/variant_handlers.py`'s `.bai` handling, and:
- If it passes `bai_sha256`/`bai_path` in the payload, update `launch_bam_stats` (Task 9) to do the same: fetch the `bai` object already found by `_sidecar_of_role`, call `await _resolve_readable(bai)`, and add `bai_sha256`/`bai_path` to the payload the same way `reference`/`r2` keys are added elsewhere in this file.
- Update `run_bam_stats` above to resolve the `.bai` the same way it resolves `bam` — via `_resolve_blob(ctx.payload, "bai")` — rather than the guessed fallback in Step 1, and simplify the symlink block to match `index_bam`'s existing bai-placement pattern (`bam.parent / f"{bam.name}{aligners.BAI_SUFFIX}"`).

Concretely, replace the bai-resolution block from Step 1 with:

```python
    bai_source = _resolve_blob(ctx.payload, "bai")
    bai_link = work / f"{bam.name}{aligners.BAI_SUFFIX}"
    bai_link.unlink(missing_ok=True)
    bai_link.symlink_to(bai_source)
```

And in `launch_bam_stats` (Task 9), add before the `payload` dict is finalized:

```python
    bai_digest, bai_path = await _resolve_readable(bai)
```

and add to `payload`:

```python
    if bai_digest:
        payload["bai_sha256"] = bai_digest
    if bai_path:
        payload["bai_path"] = bai_path
```

- [ ] **Step 3: Manual verification through the running app**

No unit test for this step — it is a subprocess-orchestration handler, and the project's existing pattern (e.g. `run_qc`, `align_reads`) leaves this class of handler to integration-level manual testing rather than mocking subprocess calls. Verify per Task 14's checklist once the frontend exists. For now, confirm the module imports cleanly:

Run: `docker compose exec api python -c "from app.queue import align_handlers"`
Expected: no output, exit code 0

- [ ] **Step 4: Commit**

```bash
git add backend/app/queue/align_handlers.py
git commit -m "feat: run_bam_stats job handler"
```

---

## Task 11: `_apply_run_bam_stats` result applier and API route

**Files:**
- Modify: `backend/app/queue/results.py`
- Modify: `backend/app/api/v1/pipelines.py`
- Test: `backend/tests/api/test_bam_stats_reports.py`

- [ ] **Step 1: Implement `_apply_run_bam_stats`**

In `backend/app/queue/results.py`, add after `_apply_index_bam` (before the dispatch dict):

```python
async def _apply_run_bam_stats(result: dict) -> None:
    """Record a Results computation's numbers on the BAM it described.

    Read-only like QC: no files to ingest, just facts merged onto the object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("bam_stats_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "bam_stats_applied",
        object_id=object_id,
        mean_depth=facts.get("bam_stats_summary", {}).get("mean_depth"),
    )
```

Add to the dispatch dict:

```python
    "run_bam_stats": _apply_run_bam_stats,
```

- [ ] **Step 2: Write the failing report-route test**

Create `backend/tests/api/test_bam_stats_reports.py`, closely following `test_qc_reports.py`'s structure (fixture setup, path-traversal parametrization) but for the paginated/download BAM stats report route:

```python
"""Serving the per-contig BAM stats report: pagination, download, traversal.

Structured like test_qc_reports.py -- exercised through the real route rather
than a reimplementation of its path handling.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers

OBJECT_ID = "507f1f77bcf86cd799439011"
OTHER_ID = "507f191e810c19729de860ea"

CONTIGS_TSV = (
    "contig\tlength\treads\tunmapped_reads\tcovered_bases"
    "\tcoverage_pct\tmean_depth\tmean_baseq\tmean_mapq\n"
    "chr1\t1000\t500\t10\t990\t99.0\t20.0\t35.0\t55.0\n"
    "chr2\t2000\t300\t5\t1900\t95.0\t12.0\t34.0\t54.0\n"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)

    reports = tmp_path / "bam_stats"
    (reports / OBJECT_ID).mkdir(parents=True)
    (reports / OBJECT_ID / "contigs.tsv").write_text(CONTIGS_TSV)
    (reports / OTHER_ID).mkdir(parents=True)
    (reports / OTHER_ID / "contigs.tsv").write_text("contig\tlength\nchrX\t500\n")

    (tmp_path / "secret.txt").write_text("blob bytes")

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    return TestClient(app)


def get(client, path: str, object_id: str = OBJECT_ID, **params):
    return client.get(
        f"/pipelines/bamstats/report/{object_id}/{path}",
        params=params,
        follow_redirects=False,
    )


class TestDownload:
    def test_download_returns_the_whole_tsv(self, client):
        r = get(client, "contigs.tsv", download=1)
        assert r.status_code == 200
        assert "chr1" in r.text
        assert "chr2" in r.text
        assert r.headers["content-type"].startswith("text/tab-separated-values")

    def test_download_content_type_is_not_sniffed(self, client):
        r = get(client, "contigs.tsv", download=1)
        assert r.headers["x-content-type-options"] == "nosniff"


class TestPagination:
    def test_default_page_returns_json_rows(self, client):
        r = get(client, "contigs.tsv")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert body["rows"][0]["contig"] == "chr1"

    def test_limit_and_offset(self, client):
        r = get(client, "contigs.tsv", limit=1, offset=1)
        body = r.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["contig"] == "chr2"

    def test_a_missing_report_is_a_404(self, client):
        assert get(client, "never_ran.tsv").status_code == 404


class TestPathTraversal:
    @pytest.mark.parametrize(
        "attack",
        [
            "../../secret.txt",
            "../../../etc/passwd",
            "/etc/passwd",
        ],
    )
    def test_traversal_out_of_the_report_tree_serves_nothing(self, client, attack):
        r = get(client, attack)
        assert "blob bytes" not in r.text
        assert "root:" not in r.text
```

- [ ] **Step 3: Run to verify it fails**

Run: `docker compose exec api python -m pytest tests/api/test_bam_stats_reports.py -v`
Expected: FAIL with 404s across the board (route does not exist yet) — the traversal tests may incidentally "pass" already since a nonexistent route also serves nothing, but download/pagination tests will fail clearly.

- [ ] **Step 4: Implement the route**

In `backend/app/api/v1/pipelines.py`, add near the top, after existing imports:

```python
from app.pipelines import bam_stats_runner
```

Add a new request/response section after the `get_qc_report` route (after line 166, before `class AlignRequest`):

```python
class BamStatsRequest(BaseModel):
    object_id: PydanticObjectId


@router.post("/bamstats", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_bam_stats(body: BamStatsRequest) -> JobOut:
    """Queue the Results computation for a BAM: coverage, per-contig table,
    binned depth. Read-only: produces facts and one TSV report."""
    job = await pipeline_service.launch_bam_stats(object_id=body.object_id)
    return JobOut.of(job)


@router.get("/bamstats/report/{object_id}/{report_path:path}")
async def get_bam_stats_report(
    object_id: PydanticObjectId,
    report_path: str,
    download: bool = False,
    offset: int = 0,
    limit: int = 100,
):
    """Serve the per-contig BAM stats report.

    Same containment rules as get_qc_report -- `..` and absolute paths are
    rejected outright, then the resolved path is re-checked against the report
    root. Unlike a QC report, this file is generated by this app from numeric
    samtools output rather than embedding read-derived strings, and it is
    never rendered as a document -- so the sandboxed CSP that HTML report
    serving needs does not apply here.

    Two modes: `?download=1` returns the whole TSV as an attachment; the
    default paginates it as JSON, which is what the Results tab's contig table
    reads from.
    """
    parts = PurePosixPath(report_path).parts
    if any(p in ("..", "") for p in parts) or PurePosixPath(report_path).is_absolute():
        raise NotFoundError(f"No such report: {report_path}")

    root = (settings.bam_stats_dir / str(object_id)).resolve()
    target = (root / report_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise NotFoundError(f"No such report: {report_path}")

    if download:
        return FileResponse(
            target,
            media_type="text/tab-separated-values",
            filename=Path(report_path).name,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    text = target.read_text(errors="replace")
    lines = text.splitlines()
    if not lines:
        return {"total": 0, "rows": []}

    header = lines[0].split("\t")
    data_lines = lines[1:]
    total = len(data_lines)
    page = data_lines[offset : offset + limit]

    rows = []
    for line in page:
        values = line.split("\t")
        row: dict = {}
        for col, value in zip(header, values):
            row[col] = bam_stats_runner.coerce_tsv_value(col, value)
        rows.append(row)

    return {"total": total, "rows": rows}
```

Add `Path` to the existing `pathlib` import at the top of the file (it currently imports only `PurePosixPath`):

```python
from pathlib import Path, PurePosixPath
```

- [ ] **Step 5: Add the missing `coerce_tsv_value` helper**

The route calls `bam_stats_runner.coerce_tsv_value`, which does not exist yet. Add it to `backend/app/pipelines/bam_stats_runner.py`, along with its test.

Append to `backend/tests/pipelines/test_bam_stats_runner.py`:

```python
class TestCoerceTsvValue:
    def test_integer_columns_become_int(self):
        assert coerce_tsv_value("length", "1000") == 1000
        assert isinstance(coerce_tsv_value("reads", "50"), int)

    def test_float_columns_become_float(self):
        assert coerce_tsv_value("coverage_pct", "99.98") == 99.98
        assert isinstance(coerce_tsv_value("mean_depth", "20.0"), float)

    def test_contig_column_stays_a_string(self):
        assert coerce_tsv_value("contig", "chr1") == "chr1"

    def test_unknown_column_stays_a_string(self):
        assert coerce_tsv_value("mystery", "42") == "42"
```

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py::TestCoerceTsvValue -v`
Expected: FAIL with `ImportError`

Append to `backend/app/pipelines/bam_stats_runner.py`:

```python
_TSV_INT_COLUMNS = {"length", "reads", "unmapped_reads", "covered_bases", "start", "end"}
_TSV_FLOAT_COLUMNS = {"coverage_pct", "mean_depth", "mean_baseq", "mean_mapq"}


def coerce_tsv_value(column: str, value: str) -> int | float | str:
    """Turn a TSV cell back into its numeric type for the JSON pagination
    response, by column name -- the same typing contigs_tsv used to write it."""
    if column in _TSV_INT_COLUMNS:
        return int(value)
    if column in _TSV_FLOAT_COLUMNS:
        return float(value)
    return value
```

Run: `docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v`
Expected: PASS (24 tests)

- [ ] **Step 6: Register the applier and run the full report-route test**

Run: `docker compose exec api python -m pytest tests/api/test_bam_stats_reports.py -v`
Expected: PASS

- [ ] **Step 7: Run the whole backend test suite to check for regressions**

Run: `docker compose exec api python -m pytest tests/ -q`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add backend/app/queue/results.py backend/app/api/v1/pipelines.py backend/app/pipelines/bam_stats_runner.py backend/tests/api/test_bam_stats_reports.py backend/tests/pipelines/test_bam_stats_runner.py
git commit -m "feat: bam stats result applier and report-serving route"
```

---

## Task 12: Restart the worker and verify the job end-to-end manually

**Files:** none (verification only)

- [ ] **Step 1: Restart the worker so it picks up the new handler**

```bash
docker compose up -d --build api web worker
docker compose restart worker
```

- [ ] **Step 2: Confirm the worker registered the new handler**

Run: `docker compose logs worker --tail 50 | grep -i handler`
Expected: no errors; `run_bam_stats` should be among the registered job types if the log line lists them (check `registry.load_handlers()`'s logging — if it does not log the list, instead confirm indirectly in Step 3).

- [ ] **Step 3: Launch a real run against an existing BAM in the running app**

Via `curl` or the API docs (`http://localhost:8000/docs` if FastAPI's default is enabled), find an existing coordinate-sorted, indexed BAM's object id from a previous align run, then:

```bash
curl -X POST http://localhost:8000/api/v1/pipelines/bamstats -H "Content-Type: application/json" -d '{"object_id": "<id>"}'
```

Expected: `201` with a job id. Poll `GET /api/v1/jobs/<id>` until `state == "succeeded"`, or check `docker compose logs worker --tail 100` for `bam_stats_finished`.

- [ ] **Step 4: Confirm facts landed on the object**

```bash
curl http://localhost:8000/api/v1/objects/<id> | python3 -m json.tool | grep bam_stats
```

Expected: `bam_stats_status`, `bam_stats_summary`, `bam_stats_coverage_bins`, etc. present.

- [ ] **Step 5: Confirm the report route serves the TSV**

```bash
curl "http://localhost:8000/api/v1/pipelines/bamstats/report/<id>/contigs.tsv?download=1"
```

Expected: TSV text with a header row and one row per contig.

If any step fails, diagnose against `docker compose logs worker` before proceeding — do not move to the frontend against a job that does not actually work.

---

## Task 13: Frontend types and API client

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add the BAM stats fact types**

In `frontend/src/api/types.ts`, add after the existing `AlignmentFacts` interface (around line 569):

```typescript
export interface ContigCoverage {
  contig: string;
  length: number;
  reads: number;
  unmapped_reads: number;
  covered_bases: number;
  coverage_pct: number;
  mean_depth: number;
  mean_baseq: number;
  mean_mapq: number;
}

export interface CoverageBoundary {
  contig: string;
  bin_start: number;
}

export interface CumulativeCoveragePoint {
  depth: number;
  fraction: number;
}

export interface BamStatsSummary {
  total_contigs: number;
  total_length: number;
  mapped_reads: number;
  unmapped_reads: number;
  mean_depth: number;
  pct_covered_1x?: number;
  pct_covered_10x?: number;
  pct_covered_30x?: number;
}

export interface MapqHistogramBucket {
  mapq: number;
  count: number;
}

export interface InsertSizeHistogramBucket {
  insert_size: number;
  count: number;
}

/** Facts produced by the run_bam_stats job. Read from ObjectDetail.facts
 * under the bam_stats_ prefix -- see BamResults.tsx. */
export interface BamStatsFacts {
  bam_stats_status?: "ok";
  bam_stats_tool_version?: string;
  bam_stats_computed_at?: string;
  bam_stats_summary?: BamStatsSummary;
  bam_stats_coverage_bins?: number[];
  bam_stats_coverage_boundaries?: CoverageBoundary[];
  bam_stats_cumulative?: CumulativeCoveragePoint[];
  bam_stats_contigs_top?: ContigCoverage[];
  bam_stats_report?: string;
  mapq_histogram?: MapqHistogramBucket[];
  insert_size_histogram?: InsertSizeHistogramBucket[];
}

export interface ContigsPage {
  total: number;
  rows: ContigCoverage[];
}
```

- [ ] **Step 2: Add API client methods**

In `frontend/src/api/client.ts`, add after `launchVariantCalling` (around line 366):

```typescript
  /** Queue the Results computation for a BAM. Read-only: produces facts and
   * one TSV report, no derived objects. */
  launchBamStats: (objectId: string) =>
    request<JobSummary>("/pipelines/bamstats", {
      method: "POST",
      body: JSON.stringify({ object_id: objectId }),
    }),

  /** A page of the per-contig table, sorted the same way the job wrote it
   * (mapped reads descending). */
  bamStatsContigs: (objectId: string, reportPath: string, offset: number, limit: number) =>
    request<import("./types").ContigsPage>(
      `/pipelines/bamstats/report/${objectId}/${reportPath}?offset=${offset}&limit=${limit}`,
    ),

  /** URL for downloading the complete per-contig TSV. */
  bamStatsDownloadUrl: (objectId: string, reportPath: string) =>
    `${BASE}/pipelines/bamstats/report/${objectId}/${reportPath}?download=1`,
```

Check the top of `client.ts` for how `types.ts` is already imported (likely a single `import type { ... } from "./types"` block) and add `ContigsPage` to that existing import instead of the inline `import("./types")` above if the file already uses a top-level type import — inline imports should only be used if no such block exists. Run:

```bash
grep -n "^import type" frontend/src/api/client.ts
```

If it lists named types, add `ContigsPage` there and simplify `bamStatsContigs`'s return type to `request<ContigsPage>(...)`.

- [ ] **Step 3: Verify TypeScript compiles**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors (or only pre-existing ones unrelated to this change — compare against a run before this task if unsure)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat: frontend types and API client for BAM results"
```

---

## Task 14: `AlignmentReport` gains a fallback and moves to Results

**Files:**
- Modify: `frontend/src/components/AlignmentReport.tsx`
- Modify: `frontend/src/components/DetailPanel.tsx`

`AlignmentReport` currently only reads flagstat-derived facts (`total_reads`, `mapped_reads`, etc.), which are absent on an imported BAM. It needs a fallback to `bam_stats_summary` so it renders for every BAM.

- [ ] **Step 1: Add the fallback to `AlignmentReport`**

Modify `frontend/src/components/AlignmentReport.tsx`. Replace the whole file:

```typescript
import type { AlignmentFacts, BamStatsSummary } from "../api/types";

/**
 * What an alignment actually produced.
 *
 * The four numbers a person checks before trusting a BAM: how much of the data
 * aligned at all, how much aligned as proper pairs, and how much was duplicate.
 * Read from `samtools flagstat` when this app produced the BAM (during
 * indexing, when the file was already being traversed) -- but an imported BAM
 * has no flagstat facts at all, so mapped/unmapped totals fall back to the
 * Results job's genome-wide summary, which every BAM gets once Results has
 * run. Properly-paired and duplicate rates are flagstat-only: bam_stats does
 * not currently compute them, so they are simply absent on an imported BAM
 * rather than approximated.
 */
export function AlignmentReport({ facts }: { facts: Record<string, unknown> }) {
  const f = facts as AlignmentFacts;
  const summary = facts.bam_stats_summary as BamStatsSummary | undefined;

  const totalReads = f.total_reads ?? summaryTotal(summary);
  if (totalReads == null) return null;

  const mappedReads = f.mapped_reads ?? summary?.mapped_reads;
  const mappedPct =
    f.mapped_pct ?? (mappedReads != null ? round1(100 * (mappedReads / totalReads)) : undefined);

  const paired = (f.properly_paired_reads ?? 0) > 0;

  return (
    <div className="section">
      <div className="section-title">Alignment</div>

      <table className="trim-table">
        <tbody>
          <Row label="Reads" value={count(totalReads)} />
          <Row
            label="Mapped"
            value={count(mappedReads)}
            pct={mappedPct}
            // Below this a run is usually wrong rather than merely poor: the
            // wrong reference, the wrong preset for long reads, or untrimmed
            // adapter. Worth flagging rather than leaving as a number to
            // interpret.
            warn={mappedPct != null && mappedPct < 70}
          />
          {paired && (
            <Row
              label="Properly paired"
              value={count(f.properly_paired_reads)}
              pct={f.properly_paired_pct}
              warn={f.properly_paired_pct != null && f.properly_paired_pct < 80}
            />
          )}
          {f.duplicate_reads != null && f.duplicate_reads > 0 && (
            <Row
              label="Duplicates"
              value={count(f.duplicate_reads)}
              pct={f.duplicate_pct}
            />
          )}
        </tbody>
      </table>

      {f.aligned_by && (
        <div className="align-provenance">
          {f.aligned_by}
          {f.aligner_version ? ` ${f.aligner_version}` : ""}
        </div>
      )}
    </div>
  );
}

function summaryTotal(summary: BamStatsSummary | undefined): number | undefined {
  if (!summary) return undefined;
  return summary.mapped_reads + summary.unmapped_reads;
}

function round1(n: number): number {
  return Math.round(n * 10) / 10;
}

function Row({
  label,
  value,
  pct,
  warn,
}: {
  label: string;
  value: string;
  pct?: number;
  warn?: boolean;
}) {
  return (
    <tr>
      <th>{label}</th>
      <td>{value}</td>
      <td className={warn ? "align-warn" : undefined}>
        {pct != null ? `${pct}%` : ""}
      </td>
    </tr>
  );
}

function count(n: number | undefined): string {
  return n == null ? "—" : n.toLocaleString();
}
```

- [ ] **Step 2: Remove `AlignmentReport` from the QC tab**

In `frontend/src/components/DetailPanel.tsx`, remove the import (it moves to the new `BamResults.tsx` created in Task 15, not deleted from the file entirely yet — for this step, just remove it from `QcTab`):

Remove this block from `QcTab` (currently the last thing rendered, right before the closing `</>`):

```typescript
      {/* On the BAM itself: whether an alignment is worth keeping is a
          question about the output, not about the reads that went in. */}
      <AlignmentReport facts={obj.facts} />
```

Leave the `import { AlignmentReport } from "./AlignmentReport";` line in place for now — Task 15 moves its usage into the new `BamResults` component in the same file area, and Task 16 wires the tab in. If Task 15's component lives in a new file, this import in `DetailPanel.tsx` becomes unused once Task 16 is done — that cleanup happens in Task 16 explicitly, not left dangling.

- [ ] **Step 3: Manual check the QC tab no longer shows alignment info**

This is verified visually in Task 17's manual pass, not standalone — there is no headless component test setup in this repo (per CLAUDE.md), so skip a dedicated automated step here.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AlignmentReport.tsx frontend/src/components/DetailPanel.tsx
git commit -m "feat: AlignmentReport falls back to bam_stats when flagstat facts are absent"
```

---

## Task 15: `CoverageChart` component (birds-eye + cumulative curve)

**Files:**
- Create: `frontend/src/components/CoverageChart.tsx`

Hand-rolled SVG, matching `SequenceCharts.tsx`'s conventions exactly (viewBox scaling, `var(--...)` CSS custom properties, no charting library).

- [ ] **Step 1: Write the component**

```typescript
import { useState } from "react";
import type { CoverageBoundary, CumulativeCoveragePoint } from "../api/types";

/**
 * Coverage across the whole reference, and the cumulative depth curve.
 *
 * Hand-rolled SVG, matching SequenceCharts.tsx: these are fixed, simple
 * shapes and a charting library would outweigh the rest of the bundle.
 *
 * The birds-eye view is deliberately a summary -- ~1000 bins regardless of
 * genome size -- not a genome browser. Anything finer belongs in IGV.
 */

export function BirdsEyeCoverageChart({
  bins,
  boundaries,
}: {
  bins: number[];
  boundaries: CoverageBoundary[];
}) {
  const [logScale, setLogScale] = useState(false);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  if (!bins?.length) return null;

  const w = 720;
  const h = 160;
  const pad = { top: 10, right: 12, bottom: 20, left: 40 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const scaled = (v: number) => (logScale ? Math.log10(v + 1) : v);
  const maxVal = Math.max(...bins.map(scaled), 1);

  const barW = plotW / bins.length;
  const x = (i: number) => pad.left + i * barW;
  const y = (v: number) => pad.top + plotH - (scaled(v) / maxVal) * plotH;

  const hovered = hoverIdx != null ? bins[hoverIdx] : null;
  const hoveredContig =
    hoverIdx != null
      ? [...boundaries].reverse().find((b) => b.bin_start <= hoverIdx)?.contig
      : null;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
          Coverage across the reference
        </div>
        <label style={{ fontSize: 11, color: "var(--text-faint)", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={logScale}
            onChange={(e) => setLogScale(e.target.checked)}
            style={{ marginRight: 4 }}
          />
          log scale
        </label>
      </div>

      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block", marginTop: 4 }}
        onMouseLeave={() => setHoverIdx(null)}
      >
        {bins.map((depth, i) => (
          <rect
            key={i}
            x={x(i)}
            y={y(depth)}
            width={Math.max(barW, 1)}
            height={pad.top + plotH - y(depth)}
            fill="var(--accent)"
            opacity={hoverIdx === i ? 1 : 0.75}
            onMouseEnter={() => setHoverIdx(i)}
          />
        ))}

        {/* Contig boundaries as thin separators, so the eye can tell where
            one contig ends and the next begins. */}
        {boundaries.map((b) => (
          <line
            key={b.contig}
            x1={x(b.bin_start)}
            x2={x(b.bin_start)}
            y1={pad.top}
            y2={pad.top + plotH}
            stroke="var(--border)"
            strokeWidth="1"
          />
        ))}

        <line
          x1={pad.left}
          x2={pad.left}
          y1={pad.top}
          y2={pad.top + plotH}
          stroke="var(--border)"
          strokeWidth="1"
        />
        <text x={pad.left - 5} y={pad.top + 4} textAnchor="end" fontSize="9" fill="var(--text-faint)">
          {Math.round(maxVal >= 1 && logScale ? Math.pow(10, maxVal) - 1 : maxVal)}
        </text>
        <text x={pad.left - 5} y={pad.top + plotH} textAnchor="end" fontSize="9" fill="var(--text-faint)">
          0
        </text>
      </svg>

      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
        {hovered != null
          ? `${hoveredContig ?? "—"}: ${hovered.toFixed(1)}× depth`
          : `${boundaries.length.toLocaleString()} contigs`}
      </div>
    </div>
  );
}

export function CumulativeCoverageChart({ curve }: { curve: CumulativeCoveragePoint[] }) {
  if (!curve?.length) return null;

  const w = 360;
  const h = 160;
  const pad = { top: 10, right: 10, bottom: 26, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxDepth = Math.max(...curve.map((c) => c.depth), 1);
  const x = (depth: number) => pad.left + (depth / maxDepth) * plotW;
  const y = (fraction: number) => pad.top + plotH - fraction * plotH;

  const sorted = [...curve].sort((a, b) => a.depth - b.depth);
  const line = sorted.map((p, i) => `${i ? "L" : "M"} ${x(p.depth)} ${y(p.fraction)}`).join(" ");

  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
        Fraction of reference at or above depth
      </div>
      <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{ maxWidth: w, display: "block", marginTop: 4 }}>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}>
            <line
              x1={pad.left}
              x2={w - pad.right}
              y1={y(f)}
              y2={y(f)}
              stroke="var(--border)"
              strokeWidth="1"
            />
            <text x={pad.left - 5} y={y(f) + 3} textAnchor="end" fontSize="9" fill="var(--text-faint)">
              {Math.round(f * 100)}%
            </text>
          </g>
        ))}
        <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.8" />
        {sorted.map((p) => (
          <circle key={p.depth} cx={x(p.depth)} cy={y(p.fraction)} r={2.5} fill="var(--accent)" />
        ))}
        {sorted.map((p) => (
          <text
            key={`label-${p.depth}`}
            x={x(p.depth)}
            y={h - pad.bottom + 12}
            textAnchor="middle"
            fontSize="9"
            fill="var(--text-faint)"
          >
            {p.depth}×
          </text>
        ))}
      </svg>
    </div>
  );
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no new errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/CoverageChart.tsx
git commit -m "feat: birds-eye coverage and cumulative coverage charts"
```

---

## Task 16: `ContigTable` component (paginated, sortable, downloadable)

**Files:**
- Create: `frontend/src/components/ContigTable.tsx`

- [ ] **Step 1: Write the component**

```typescript
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

const PAGE_SIZE = 25;

/**
 * The complete per-contig table, paginated server-side against the TSV
 * report -- not the capped `bam_stats_contigs_top` in facts, which only
 * covers the visualization's top-N slice.
 */
export function ContigTable({
  objectId,
  reportPath,
}: {
  objectId: string;
  reportPath: string;
}) {
  const [page, setPage] = useState(0);

  const { data, isLoading } = useQuery({
    queryKey: ["bamstats", "contigs", objectId, reportPath, page],
    queryFn: () => api.bamStatsContigs(objectId, reportPath, page * PAGE_SIZE, PAGE_SIZE),
  });

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <div className="section">
      <div
        className="section-title"
        style={{ display: "flex", alignItems: "center", gap: 8 }}
      >
        <span>Per-contig coverage</span>
        <a
          href={api.bamStatsDownloadUrl(objectId, reportPath)}
          style={{ marginLeft: "auto", fontSize: 11 }}
        >
          Download TSV
        </a>
      </div>

      {isLoading || !data ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>Loading…</div>
      ) : (
        <>
          <table className="trim-table">
            <thead>
              <tr>
                <th>Contig</th>
                <th>Length</th>
                <th>Reads</th>
                <th>Coverage</th>
                <th>Mean depth</th>
                <th>Mean MAPQ</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row) => (
                <tr key={row.contig}>
                  <td className="mono">{row.contig}</td>
                  <td>{row.length.toLocaleString()}</td>
                  <td>{row.reads.toLocaleString()}</td>
                  <td>{row.coverage_pct.toFixed(1)}%</td>
                  <td>{row.mean_depth.toFixed(1)}×</td>
                  <td>{row.mean_mapq.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginTop: 8,
              fontSize: 11,
              color: "var(--text-faint)",
            }}
          >
            <span>
              {data.total.toLocaleString()} contig{data.total === 1 ? "" : "s"}
            </span>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <button
                type="button"
                className="btn"
                style={{ padding: "1px 8px", fontSize: 11 }}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
              >
                Prev
              </button>
              <span>
                Page {page + 1} of {totalPages}
              </span>
              <button
                type="button"
                className="btn"
                style={{ padding: "1px 8px", fontSize: 11 }}
                onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                disabled={page + 1 >= totalPages}
              >
                Next
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no new errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ContigTable.tsx
git commit -m "feat: paginated per-contig table with TSV download"
```

---

## Task 17: `BamResults` tab component and histograms

**Files:**
- Create: `frontend/src/components/BamResults.tsx`

Assembles: `AlignmentReport`, `BirdsEyeCoverageChart`, `CumulativeCoverageChart`, `ContigTable`, MAPQ/insert-size histograms (small inline bar charts, no need for a shared component given they're single-use and simple), provenance, empty/prerequisite states, and the Compute button.

- [ ] **Step 1: Write the component**

```typescript
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type {
  BamStatsFacts,
  InsertSizeHistogramBucket,
  MapqHistogramBucket,
  ObjectDetail as ObjectDetailData,
} from "../api/types";
import { AlignmentReport } from "./AlignmentReport";
import { BirdsEyeCoverageChart, CumulativeCoverageChart } from "./CoverageChart";
import { ContigTable } from "./ContigTable";

/**
 * What the alignment produced: mapped/unmapped totals, coverage across the
 * reference at a glance, the complete per-contig table, and the shape of
 * insert size and mapping quality.
 *
 * Works for every BAM, imported or pipeline-produced -- unlike the flagstat
 * numbers alone, which only exist for a BAM this app aligned. See
 * AlignmentReport's fallback to bam_stats_summary.
 */
export function BamResults({ obj }: { obj: ObjectDetailData }) {
  const qc = useQueryClient();
  const f = obj.facts as BamStatsFacts;

  const compute = useMutation({
    mutationFn: () => api.launchBamStats(obj.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Computing results");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const hasResults = f.bam_stats_status === "ok";
  const sortedCoordinate = obj.facts.sort_order === "coordinate";
  const hasIndex = obj.facts.has_index === true;

  return (
    <>
      <AlignmentReport facts={obj.facts} />

      {!hasResults && (
        <div className="section">
          <div className="section-title">Coverage &amp; per-contig detail</div>
          {!sortedCoordinate ? (
            <div className="warn-box">
              This BAM is not coordinate-sorted, which coverage statistics
              require.
            </div>
          ) : !hasIndex ? (
            <div className="warn-box">
              This BAM has no index (.bai). Index it first, from the Align
              button or the Metadata tab, then compute results.
            </div>
          ) : (
            <div style={{ color: "var(--text-faint)", fontSize: 12, marginBottom: 8 }}>
              Coverage across the reference, a per-contig breakdown, and
              insert-size/MAPQ distributions — computed on demand from the
              BAM and its index.
            </div>
          )}
          <button
            type="button"
            className="btn"
            onClick={() => compute.mutate()}
            disabled={compute.isPending || !sortedCoordinate || !hasIndex}
          >
            {compute.isPending ? "Computing…" : "Compute results"}
          </button>
        </div>
      )}

      {hasResults && (
        <>
          <div className="section">
            {f.bam_stats_coverage_bins && f.bam_stats_coverage_boundaries && (
              <BirdsEyeCoverageChart
                bins={f.bam_stats_coverage_bins}
                boundaries={f.bam_stats_coverage_boundaries}
              />
            )}
            <SummaryRow summary={f.bam_stats_summary} />
          </div>

          {f.bam_stats_cumulative && f.bam_stats_cumulative.length > 0 && (
            <div className="section">
              <CumulativeCoverageChart curve={f.bam_stats_cumulative} />
            </div>
          )}

          {f.bam_stats_report && (
            <ContigTable objectId={obj.id} reportPath={f.bam_stats_report} />
          )}

          <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
            {f.insert_size_histogram && f.insert_size_histogram.length > 0 && (
              <div className="section" style={{ flex: "1 1 300px" }}>
                <div className="section-title">Insert size</div>
                <Histogram
                  data={f.insert_size_histogram}
                  xKey="insert_size"
                  yKey="count"
                  xLabel={(v) => `${v}`}
                />
              </div>
            )}
            {f.mapq_histogram && f.mapq_histogram.length > 0 && (
              <div className="section" style={{ flex: "1 1 300px" }}>
                <div className="section-title">Mapping quality</div>
                <Histogram
                  data={f.mapq_histogram}
                  xKey="mapq"
                  yKey="count"
                  xLabel={(v) => `${v}`}
                />
              </div>
            )}
          </div>

          <div className="section">
            <div className="section-title">Provenance</div>
            <dl className="kv">
              {obj.facts.aligned_by != null && (
                <>
                  <dt>Aligner</dt>
                  <dd>
                    {String(obj.facts.aligned_by)}
                    {obj.facts.aligner_version ? ` ${obj.facts.aligner_version}` : ""}
                  </dd>
                </>
              )}
              {Array.isArray(obj.facts.program_chain) && obj.facts.program_chain.length > 0 && (
                <>
                  <dt>Program chain</dt>
                  <dd>{(obj.facts.program_chain as string[]).join(" → ")}</dd>
                </>
              )}
              {Array.isArray(obj.facts.sample_names) && obj.facts.sample_names.length > 0 && (
                <>
                  <dt>Samples</dt>
                  <dd>{(obj.facts.sample_names as string[]).join(", ")}</dd>
                </>
              )}
              {Array.isArray(obj.facts.platforms) && obj.facts.platforms.length > 0 && (
                <>
                  <dt>Platforms</dt>
                  <dd>{(obj.facts.platforms as string[]).join(", ")}</dd>
                </>
              )}
              {obj.facts.sort_order != null && (
                <>
                  <dt>Sort order</dt>
                  <dd>{String(obj.facts.sort_order)}</dd>
                </>
              )}
              <dt>Index</dt>
              <dd>{hasIndex ? "present" : "missing"}</dd>
            </dl>
            <button
              type="button"
              onClick={() => compute.mutate()}
              disabled={compute.isPending}
              style={{
                marginTop: 6,
                color: "var(--accent)",
                fontSize: 11,
                textTransform: "none",
                letterSpacing: 0,
              }}
            >
              {compute.isPending ? "recomputing…" : "recompute results"}
            </button>
          </div>
        </>
      )}
    </>
  );
}

function SummaryRow({ summary }: { summary?: BamStatsFacts["bam_stats_summary"] }) {
  if (!summary) return null;
  return (
    <div style={{ display: "flex", gap: 20, flexWrap: "wrap", fontSize: 12, marginTop: 8 }}>
      <Stat label="Contigs" value={summary.total_contigs.toLocaleString()} />
      <Stat label="Mean depth" value={`${summary.mean_depth.toFixed(1)}×`} />
      {summary.pct_covered_1x != null && (
        <Stat label="≥1×" value={`${summary.pct_covered_1x.toFixed(1)}%`} />
      )}
      {summary.pct_covered_10x != null && (
        <Stat label="≥10×" value={`${summary.pct_covered_10x.toFixed(1)}%`} />
      )}
      {summary.pct_covered_30x != null && (
        <Stat label="≥30×" value={`${summary.pct_covered_30x.toFixed(1)}%`} />
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ color: "var(--text-faint)", fontSize: 10 }}>{label}</div>
      <div style={{ fontWeight: 600 }}>{value}</div>
    </div>
  );
}

/** A small inline bar histogram. Single-use and simple enough not to share
 * SequenceCharts.tsx's more general axis machinery. */
function Histogram<T extends MapqHistogramBucket | InsertSizeHistogramBucket>({
  data,
  xKey,
  yKey,
  xLabel,
}: {
  data: T[];
  xKey: keyof T;
  yKey: keyof T;
  xLabel: (v: number) => string;
}) {
  const w = 320;
  const h = 120;
  const pad = { top: 6, right: 6, bottom: 18, left: 6 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxCount = Math.max(...data.map((d) => Number(d[yKey])), 1);
  const barW = plotW / data.length;

  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{ maxWidth: w, display: "block" }}>
      {data.map((d, i) => {
        const count = Number(d[yKey]);
        const barH = (count / maxCount) * plotH;
        return (
          <rect
            key={i}
            x={pad.left + i * barW}
            y={pad.top + plotH - barH}
            width={Math.max(barW - 1, 1)}
            height={barH}
            fill="var(--accent)"
            opacity={0.8}
          >
            <title>
              {xLabel(Number(d[xKey]))}: {count.toLocaleString()}
            </title>
          </rect>
        );
      })}
      <text x={pad.left} y={h - 4} fontSize="9" fill="var(--text-faint)">
        {xLabel(Number(data[0][xKey]))}
      </text>
      <text x={w - pad.right} y={h - 4} fontSize="9" fill="var(--text-faint)" textAnchor="end">
        {xLabel(Number(data[data.length - 1][xKey]))}
      </text>
    </svg>
  );
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no new errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/BamResults.tsx
git commit -m "feat: BamResults tab component"
```

---

## Task 18: Wire the Results tab into `DetailPanel`

**Files:**
- Modify: `frontend/src/components/DetailPanel.tsx`

- [ ] **Step 1: Add the tab conditionally, only for BAMs**

`TABS` is currently a fixed constant (line 225-229). It needs to become conditional on format kind, since Results only applies to BAMs. Replace:

```typescript
/** Ordered so the panel opens on the question people ask most: is this file good? */
const TABS: TabDef[] = [
  { id: "qc", label: "QC" },
  { id: "metadata", label: "Metadata" },
  { id: "actions", label: "Actions" },
];
```

with a function, since the tab list now depends on the object:

```typescript
/** Ordered so the panel opens on the question people ask most: is this file
 * good? Results sits next to QC -- they answer adjacent questions -- and only
 * appears for BAMs, which is the only format it currently describes. */
function tabsFor(formatKind: string): TabDef[] {
  const tabs: TabDef[] = [{ id: "qc", label: "QC" }];
  if (formatKind === "bam") {
    tabs.push({ id: "results", label: "Results" });
  }
  tabs.push({ id: "metadata", label: "Metadata" }, { id: "actions", label: "Actions" });
  return tabs;
}
```

- [ ] **Step 2: Update `ObjectDetail` to use `tabsFor` and render the new tab**

Find this block inside `ObjectDetail` (around line 276-284):

```typescript
  const raw = params.get("tab");
  const tab = TABS.some((t) => t.id === raw) ? raw! : "qc";
```

Replace with:

```typescript
  const tabs = tabsFor(obj?.format.kind ?? "unknown");
  const raw = params.get("tab");
  const tab = tabs.some((t) => t.id === raw) ? raw! : "qc";
```

This references `obj` before its `isLoading` guard runs — check the surrounding code: `obj` comes from `useQuery` a few lines above and the `isLoading || !obj` early return happens later (around line 340). Since `tabsFor` is called before that guard, and `obj` may be `undefined` at that point, guard it: `obj?.format.kind ?? "unknown"` (as written above) already handles this safely since `tabsFor` accepts any string and simply omits Results for a non-`"bam"` value including `"unknown"`.

Find the `<Tabs tabs={TABS} ... />` line (around line 499) and change to:

```typescript
        <Tabs tabs={tabs} active={tab} onChange={setTab} idPrefix="obj" />
```

- [ ] **Step 3: Render the Results tab panel**

After the existing `{tab === "qc" && (...)}` block and before `{tab === "metadata" && (...)}` (around line 505-507), add:

```typescript
        {tab === "results" && (
          <TabPanel id="results" idPrefix="obj">
            <BamResults obj={obj} />
          </TabPanel>
        )}
```

- [ ] **Step 4: Add the import and remove the now-unused `AlignmentReport` import from `DetailPanel`**

`AlignmentReport` is no longer used directly in `DetailPanel.tsx` (it's used inside `BamResults.tsx` now, per Task 14). Change:

```typescript
import { AlignmentReport } from "./AlignmentReport";
```

to nothing (remove the line), and add:

```typescript
import { BamResults } from "./BamResults";
```

in its place in the import block (keep imports alphabetically grouped consistent with the surrounding style — insert near the other component imports, e.g. after `AssemblyFacts`).

- [ ] **Step 5: Verify TypeScript compiles cleanly, including no unused-import errors**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx
git commit -m "feat: wire the Results tab into DetailPanel for BAM objects"
```

---

## Task 19: `ActivePipelineJobs` recognizes the new job type

**Files:**
- Modify: `frontend/src/components/ActivePipelineJobs.tsx`

Without this, a Results job in flight is invisible to the "already queued" indicator — a small but real inconsistency with how every other pipeline job is surfaced.

- [ ] **Step 1: Add the job type and label**

In `frontend/src/components/ActivePipelineJobs.tsx`, change:

```typescript
const PIPELINE_TYPES = new Set(["trim_reads", "align_reads", "build_index", "index_bam"]);

const LABELS: Record<string, string> = {
  trim_reads: "Trimming",
  align_reads: "Aligning",
  build_index: "Building index",
  index_bam: "Indexing BAM",
};
```

to:

```typescript
const PIPELINE_TYPES = new Set([
  "trim_reads",
  "align_reads",
  "build_index",
  "index_bam",
  "run_bam_stats",
]);

const LABELS: Record<string, string> = {
  trim_reads: "Trimming",
  align_reads: "Aligning",
  build_index: "Building index",
  index_bam: "Indexing BAM",
  run_bam_stats: "Computing results",
};
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ActivePipelineJobs.tsx
git commit -m "feat: surface run_bam_stats in the active-jobs indicator"
```

---

## Task 20: Full manual verification in the browser

**Files:** none (verification only)

Per CLAUDE.md, this is the real verification step for anything UI-facing — there is no headless component-testing setup in this repo and none is expected.

- [ ] **Step 1: Rebuild and restart everything**

```bash
docker compose up -d --build api web worker
```

- [ ] **Step 2: Open the app**

Navigate to `http://localhost:5173`.

- [ ] **Step 3: Verify on a pipeline-produced BAM**

Select a BAM this app aligned (has flagstat facts already). Confirm:
- QC tab no longer shows an "Alignment" section.
- A "Results" tab appears between QC and Metadata.
- Results tab shows the Alignment summary at top (using the existing flagstat facts).
- A "Compute results" prompt appears if bam_stats has never run; clicking it queues a job (check the Activity view or the active-jobs indicator on the panel header — it should say "Computing results").
- After the job finishes (poll or wait), the tab shows the birds-eye coverage chart, cumulative curve, per-contig table (paginated), insert-size and MAPQ histograms, and provenance.
- Click "Download TSV" and confirm a `contigs.tsv` file downloads with a full row per contig.
- Click through table pagination if there is more than one page.

- [ ] **Step 4: Verify on an imported BAM (no prior alignment via this app)**

Import or locate a BAM the app did not align. Confirm:
- The Alignment summary in Results now renders using the bam_stats fallback (previously it would have rendered nothing).
- If unindexed: the tab explains the missing `.bai` rather than offering a button that fails.
- If indexed and coordinate-sorted: Compute results works identically to the pipeline-produced case.

- [ ] **Step 5: Verify a non-BAM object has no Results tab**

Select a FASTQ or FASTA. Confirm only QC / Metadata / Actions appear — no Results tab.

- [ ] **Step 6: Check the browser console for errors**

Open devtools console while navigating through the above. Confirm no uncaught exceptions or React warnings related to the new components.

- [ ] **Step 7: Re-run the full backend test suite one more time**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass.

---

## Task 21: Merge to main

**Files:** none (git operations only)

- [ ] **Step 1: Confirm the working tree is clean and everything is committed**

```bash
git status
```

Expected: nothing to commit, working tree clean.

- [ ] **Step 2: Check what's ahead of main**

```bash
git log main..HEAD --oneline
```

- [ ] **Step 3: Merge into main**

```bash
git checkout main
git pull origin main
git merge claude/bam-results-tab-design-8aaf42
```

If there are conflicts, resolve them by re-reading the conflicting hunks, preferring this branch's new files outright (they are new, so true conflicts should only appear in shared files like `DetailPanel.tsx`, `types.ts`, `client.ts`, `pipeline_service.py`, `align_handlers.py`, `results.py`, `handlers.py` if main moved those in parallel) — merge both sides' intent rather than blindly taking one side, then re-run the full test suite before completing the merge commit.

- [ ] **Step 4: Re-run tests on main after merging**

```bash
docker compose exec api python -m pytest tests/ -q
docker compose exec web npx tsc --noEmit
```

Expected: all pass.

- [ ] **Step 5: Push main**

```bash
git push origin main
```
