# Per-Tile Sequence Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-tile sequence quality heatmap to the QC tab, so a physical flow-cell defect reads as the spatial pattern it is rather than an unexplained dip in the aggregate quality curve.

**Architecture:** A new `tile_scanner` module makes one sequential pass over the R1 FASTQ, parsing the tile field from every Illumina header and decoding quality for an adaptive subsample. The resulting tile x position matrix is written as a sidecar JSON file in the QC report directory (it is far too large for the object document), with only scalars in `facts`. A new JSON API route serves the sidecar — deliberately *not* the existing `get_qc_report` route, whose sandbox CSP would block the fetch. The frontend draws it as a canvas/SVG heatmap with two selectable colour scales.

**Tech Stack:** Python 3 stdlib (`gzip`, `json`) on the backend; FastAPI for the route; React + hand-rolled SVG/Canvas on the frontend (this repo uses no chart library). Tests are `pytest`.

**Spec:** [docs/superpowers/specs/2026-08-09-per-tile-quality-design.md](../specs/2026-08-09-per-tile-quality-design.md)

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/pipelines/tile_scanner.py` | **Create.** Header parsing, the scanning pass, matrix serialisation. Pure functions over strings and paths — no queue, no database. Mirrors how `fastp_runner.py` is kept separate from its handler. |
| `backend/tests/pipelines/test_tile_scanner.py` | **Create.** Unit tests for parsing, bail-out, sampling, guardrails. |
| `backend/app/queue/pipeline_handlers.py` | **Modify** (`_run_short_read_qc`, ~line 505). Call the scanner beside `_run_fastqc`, swallowing failure to a warning. |
| `backend/app/api/v1/pipelines.py` | **Modify.** Add `get_qc_tile_matrix` JSON route. |
| `backend/tests/api/test_tile_matrix_route.py` | **Create.** Route ownership, traversal, and 404 behaviour. |
| `frontend/src/api/types.ts` | **Modify.** Extend `QcFacts` with the tile scalars; add `TileMatrix`. |
| `frontend/src/api/client.ts` | **Modify.** Add `qcTileMatrix` fetch. |
| `frontend/src/components/TileQualityChart.tsx` | **Create.** The heatmap. Its own file rather than appended to `SequenceCharts.tsx` — canvas rendering, two colour scales, and binning is enough responsibility to stand alone, and `SequenceCharts.tsx` is already 400+ lines. |
| `frontend/src/components/DetailPanel.tsx` | **Modify** (~line 1030). Mount the chart in the `qc-charts` block. |

**Task order rationale:** backend scanner first (it defines the data shape everything else consumes), then the handler wiring, then the route, then the frontend. Each task ends green and committed.

---

## Task 1: Header parsing

**Files:**
- Create: `backend/app/pipelines/tile_scanner.py`
- Test: `backend/tests/pipelines/test_tile_scanner.py`

The Illumina header format, which is what field index 4 comes from:

```
@M01939:146:000000000-D3WVL:1:1101:15351:1594 1:N:0:1
 0      1   2               3 4    5     6
 instr  run flowcell        ln tile x     y
```

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_tile_scanner.py`:

```python
"""Parsing the spatial fields out of a read header.

The formats that are *not* Illumina matter as much as the one that is: an
SRA-stripped file and a Nanopore file must both parse to None rather than to
a wrong tile number, because a wrong tile silently produces a wrong heatmap.
"""

from app.pipelines import tile_scanner


def test_parses_illumina_header():
    header = "@M01939:146:000000000-D3WVL:1:1101:15351:1594 1:N:0:1"
    assert tile_scanner.parse_header(header) == tile_scanner.ReadPosition(
        tile=1101, x=15351, y=1594
    )


def test_parses_header_without_the_trailing_read_field():
    # The space-separated remainder is optional in some writers' output.
    header = "@M01939:146:000000000-D3WVL:1:1101:15351:1594"
    assert tile_scanner.parse_header(header) == tile_scanner.ReadPosition(
        tile=1101, x=15351, y=1594
    )


def test_sra_stripped_header_yields_none():
    assert tile_scanner.parse_header("@SRR123456.1 1 length=100") is None


def test_nanopore_uuid_header_yields_none():
    header = "@a1b2c3d4-e5f6-7890-abcd-ef1234567890 runid=xyz ch=42"
    assert tile_scanner.parse_header(header) is None


def test_pacbio_zmw_header_yields_none():
    assert tile_scanner.parse_header("@m54238_180901_011437/4194374/0_1000") is None


def test_truncated_header_yields_none():
    assert tile_scanner.parse_header("@M01939:146:000000000-D3WVL:1") is None


def test_non_numeric_tile_yields_none():
    header = "@M01939:146:000000000-D3WVL:1:notatile:15351:1594"
    assert tile_scanner.parse_header(header) is None


def test_empty_and_bare_at_yield_none():
    assert tile_scanner.parse_header("") is None
    assert tile_scanner.parse_header("@") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run from the worktree root:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tile_scanner.py -q
```

Expected: collection error — `ModuleNotFoundError` / `ImportError: cannot import name 'tile_scanner'`.

- [ ] **Step 3: Write the minimal implementation**

Create `backend/app/pipelines/tile_scanner.py`:

```python
"""Reading flow-cell coordinates out of read headers, and summarising quality
by tile.

Kept separate from the job handler for the same reason `fastp_runner` is: the
parts worth testing are pure functions over strings, with no queue and no
filesystem involved.

Only Illumina headers carry a tile. Nanopore writes a read UUID and channel,
PacBio writes a ZMW hole number, and the SRA toolkit strips machine headers
entirely in favour of an accession. All three must parse to None -- a wrong
tile number is worse than no tile number, because it produces a plausible
heatmap of nothing.
"""

from dataclasses import dataclass

from app.logging import get_logger

log = get_logger(__name__)

# Field indices within the colon-delimited header. The instrument, run, and
# flowcell fields ahead of these are not used, but their presence is what
# makes index 4 the tile rather than something else -- hence the length check.
_TILE_FIELD = 4
_X_FIELD = 5
_Y_FIELD = 6
_MIN_FIELDS = 7


@dataclass(frozen=True)
class ReadPosition:
    """Where on the flow cell a single read's cluster sat."""

    tile: int
    x: int
    y: int


def parse_header(header: str) -> ReadPosition | None:
    """Extract tile and x/y from an Illumina header, or None if it is not one.

    Returns None rather than raising: a file whose headers do not carry tiles
    is an ordinary, expected input, not an error.
    """
    if not header:
        return None

    # The read/filter/control/index fields after the space are not needed.
    name = header.split(" ", 1)[0].lstrip("@")
    fields = name.split(":")
    if len(fields) < _MIN_FIELDS:
        return None

    try:
        return ReadPosition(
            tile=int(fields[_TILE_FIELD]),
            x=int(fields[_X_FIELD]),
            y=int(fields[_Y_FIELD]),
        )
    except ValueError:
        # A colon-bearing header that is not Illumina's -- shape matched, the
        # contents did not.
        return None
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tile_scanner.py -q
```

Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/tile_scanner.py backend/tests/pipelines/test_tile_scanner.py
git commit -m "feat(pipelines): read flow-cell tile and coordinates from read headers"
```

---

## Task 2: The scanning pass

**Files:**
- Modify: `backend/app/pipelines/tile_scanner.py`
- Test: `backend/tests/pipelines/test_tile_scanner.py`

Three behaviours, all of which have a silent-failure mode if wrong:

1. **Early bail** when the first 1,000 headers carry no tile — otherwise a 30GB SRA file is read in full to learn nothing.
2. **Adaptive sampling** targeting ~2M decoded reads — a small file samples fully, a large one thins.
3. **Guardrails** at 2,000 tiles / 1,000 positions, so a malformed file cannot grow the accumulator without bound.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_tile_scanner.py`:

```python
import gzip

import pytest


def _write_fastq(path, records):
    """Write (header, seq, qual) triples as a 4-line-per-record FASTQ."""
    lines = []
    for header, seq, qual in records:
        lines += [header, seq, "+", qual]
    text = "\n".join(lines) + "\n"
    if str(path).endswith(".gz"):
        with gzip.open(path, "wt") as fh:
            fh.write(text)
    else:
        path.write_text(text)
    return path


def _illumina(tile, x=1000, y=2000, qual="IIII"):
    return (f"@M01939:146:FC:1:{tile}:{x}:{y} 1:N:0:1", "ACGT", qual)


def test_scan_groups_quality_by_tile_and_position(tmp_path):
    # 'I' is Phred 40, '5' is Phred 20 in Sanger encoding (ord - 33).
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [_illumina(1101, qual="IIII"), _illumina(1102, qual="5555")],
    )
    result = tile_scanner.scan(path)

    assert result.source == "present"
    assert result.tile_count == 2
    assert result.matrix[1101] == [40.0, 40.0, 40.0, 40.0]
    assert result.matrix[1102] == [20.0, 20.0, 20.0, 20.0]


def test_scan_averages_reads_within_a_tile(tmp_path):
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [_illumina(1101, qual="IIII"), _illumina(1101, qual="5555")],
    )
    result = tile_scanner.scan(path)
    # Mean of Q40 and Q20 at every position.
    assert result.matrix[1101] == [30.0, 30.0, 30.0, 30.0]


def test_scan_reads_gzipped_input(tmp_path):
    path = _write_fastq(tmp_path / "r1.fastq.gz", [_illumina(1101)])
    result = tile_scanner.scan(path)
    assert result.source == "present"
    assert result.tile_count == 1


def test_scan_bails_out_early_on_headers_without_tiles(tmp_path):
    # More records than the probe window, so a scan that did not bail would
    # read past it. All are SRA-stripped.
    records = [(f"@SRR123456.{i}", "ACGT", "IIII") for i in range(3000)]
    path = _write_fastq(tmp_path / "r1.fastq", records)

    result = tile_scanner.scan(path)

    assert result.source == "absent"
    assert result.matrix == {}
    # The probe window bounds what was inspected, not the file's length.
    assert result.records_inspected <= tile_scanner.PROBE_RECORDS


def test_scan_keeps_going_when_tiles_appear_within_the_probe_window(tmp_path):
    # A handful of junk headers ahead of real ones must not trigger the bail.
    records = [("@SRR1.1", "ACGT", "IIII")] * 10 + [_illumina(1101)] * 20
    path = _write_fastq(tmp_path / "r1.fastq", records)

    result = tile_scanner.scan(path)

    assert result.source == "present"
    assert result.matrix[1101] == [40.0, 40.0, 40.0, 40.0]


def test_scan_samples_a_small_file_completely(tmp_path):
    path = _write_fastq(tmp_path / "r1.fastq", [_illumina(1101)] * 50)
    result = tile_scanner.scan(path, target_sampled_reads=1000)
    assert result.sample_rate == 1
    assert result.sampled_reads == 50


def test_scan_thins_a_file_larger_than_the_target(tmp_path):
    path = _write_fastq(tmp_path / "r1.fastq", [_illumina(1101)] * 100)
    # Estimating from a 100-record file against a 10-read target: the rate
    # must thin, and the decoded count must respect it.
    result = tile_scanner.scan(path, target_sampled_reads=10)
    assert result.sample_rate > 1
    assert result.sampled_reads < 100


def test_scan_records_per_tile_coordinate_extents(tmp_path):
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [_illumina(1101, x=10, y=20), _illumina(1101, x=90, y=80)],
    )
    result = tile_scanner.scan(path)
    extent = result.extents[1101]
    assert (extent.x_min, extent.x_max) == (10, 90)
    assert (extent.y_min, extent.y_max) == (20, 80)
    assert extent.reads == 2


def test_scan_truncates_beyond_the_tile_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(tile_scanner, "MAX_TILES", 3)
    records = [_illumina(1100 + i) for i in range(10)]
    path = _write_fastq(tmp_path / "r1.fastq", records)

    result = tile_scanner.scan(path)

    assert result.truncated is True
    assert len(result.matrix) == 3


def test_scan_truncates_beyond_the_position_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(tile_scanner, "MAX_POSITIONS", 2)
    path = _write_fastq(tmp_path / "r1.fastq", [_illumina(1101, qual="IIII")])

    result = tile_scanner.scan(path)

    assert result.truncated is True
    assert len(result.matrix[1101]) == 2


def test_scan_identifies_the_worst_tile(tmp_path):
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [_illumina(1101, qual="IIII"), _illumina(1102, qual="5555")],
    )
    result = tile_scanner.scan(path)
    assert result.worst_tile == 1102


def test_scan_handles_a_truncated_final_record(tmp_path):
    # A file cut off mid-record must not raise -- QC runs on files that are
    # still being written often enough for this to matter.
    path = tmp_path / "r1.fastq"
    path.write_text("@M01939:146:FC:1:1101:1:2 1:N:0:1\nACGT\n+\n")
    result = tile_scanner.scan(path)
    assert result.source in ("present", "absent")


def test_scan_ignores_quality_longer_than_the_matrix_row(tmp_path):
    # Variable read lengths within one file: the row grows to the longest.
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [
            ("@M01939:146:FC:1:1101:1:2 1:N:0:1", "ACGT", "IIII"),
            ("@M01939:146:FC:1:1101:1:3 1:N:0:1", "ACGTAA", "IIIIII"),
        ],
    )
    result = tile_scanner.scan(path)
    assert len(result.matrix[1101]) == 6
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tile_scanner.py -q
```

Expected: FAIL — `AttributeError: module 'app.pipelines.tile_scanner' has no attribute 'scan'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/app/pipelines/tile_scanner.py`:

```python
import gzip
from pathlib import Path
from typing import IO

# How many records to inspect before deciding a file has no tiles at all.
# Large enough to survive a few junk headers at the top of an otherwise
# ordinary file; small enough that bailing costs a fraction of a second
# rather than a full read of a 30GB SRA download.
PROBE_RECORDS = 1000

# Decode quality for roughly this many reads. The rate adapts to the file's
# size so a small file is sampled completely and a large one is thinned. A
# tile/position cell on a MiSeq run still receives hundreds of reads here.
TARGET_SAMPLED_READS = 2_000_000

# Guardrails. A NovaSeq S4 has ~1408 tiles and reads run to ~300bp, so both
# caps sit well above any real file; they exist so a malformed one cannot
# grow the accumulator without bound.
MAX_TILES = 2000
MAX_POSITIONS = 1000

# Sanger/Illumina 1.8+ quality encoding.
_PHRED_OFFSET = 33


@dataclass
class TileExtent:
    """The bounding box of clusters seen on one tile, and how many there were.

    Stored instead of every read's coordinates: the full set is millions of
    points nothing renders, while the box is nearly free and is what a
    within-tile spatial view would need.
    """

    x_min: int
    x_max: int
    y_min: int
    y_max: int
    reads: int


@dataclass
class ScanResult:
    """What one pass over a FASTQ learned about its flow cell."""

    source: str  # "present" | "absent"
    matrix: dict[int, list[float]]
    extents: dict[int, TileExtent]
    tile_count: int
    sampled_reads: int
    sample_rate: int
    records_inspected: int
    truncated: bool
    worst_tile: int | None


def _open_fastq(path: Path) -> IO[str]:
    """Open plain or gzipped FASTQ as text, by magic number rather than name.

    Sniffing the bytes rather than trusting the extension: ingest bgzips
    files, and a `.fastq` that is actually compressed would otherwise be read
    as line noise and parse to zero tiles.
    """
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", errors="replace")
    return open(path, "rt", errors="replace")


def _estimate_sample_rate(path: Path, target: int) -> int:
    """Pick a 1-in-N sampling rate from the file's size.

    Estimates the record count by measuring the first 1000 records' mean
    on-disk length. Approximate on purpose -- the rate only has to land in
    the right order of magnitude, since the cell counts it produces are in
    the hundreds either way.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 1

    sampled_bytes = 0
    sampled_records = 0
    try:
        with _open_fastq(path) as fh:
            for i, line in enumerate(fh):
                sampled_bytes += len(line)
                if i % 4 == 3:
                    sampled_records += 1
                    if sampled_records >= 1000:
                        break
    except OSError:
        return 1

    if sampled_records == 0 or sampled_bytes == 0:
        return 1

    bytes_per_record = sampled_bytes / sampled_records
    # A gzipped file holds more records per on-disk byte than this estimate
    # from decompressed lines suggests; undershooting the rate samples more
    # than asked, which is the safe direction.
    estimated_records = size / bytes_per_record
    if estimated_records <= target:
        return 1
    return max(1, int(estimated_records // target))


def scan(
    path: Path, target_sampled_reads: int = TARGET_SAMPLED_READS
) -> ScanResult:
    """Walk a FASTQ, summarising mean quality by tile and read position.

    Headers are parsed for every record -- a string split, cheap enough to do
    exhaustively -- while quality is decoded only for a subsample, because
    decoding is what actually costs.
    """
    rate = _estimate_sample_rate(path, target_sampled_reads)

    sums: dict[int, list[float]] = {}
    counts: dict[int, list[int]] = {}
    extents: dict[int, TileExtent] = {}
    records = 0
    sampled = 0
    truncated = False
    saw_tile = False

    with _open_fastq(path) as fh:
        while True:
            header = fh.readline()
            if not header:
                break
            seq = fh.readline()
            plus = fh.readline()
            qual = fh.readline()
            if not qual:
                # Truncated final record -- a file still being written.
                break
            del seq, plus

            records += 1
            pos = parse_header(header.rstrip("\n"))

            if pos is None:
                if not saw_tile and records >= PROBE_RECORDS:
                    # Nothing in the probe window carried a tile. This is an
                    # SRA-stripped or long-read file; stop rather than read
                    # the whole thing to learn the same thing again.
                    return ScanResult(
                        source="absent",
                        matrix={},
                        extents={},
                        tile_count=0,
                        sampled_reads=0,
                        sample_rate=rate,
                        records_inspected=records,
                        truncated=False,
                        worst_tile=None,
                    )
                continue

            saw_tile = True
            _record_extent(extents, pos)

            if records % rate != 0 and rate > 1:
                continue

            if pos.tile not in sums:
                if len(sums) >= MAX_TILES:
                    truncated = True
                    continue
                sums[pos.tile] = []
                counts[pos.tile] = []

            sampled += 1
            if _accumulate(sums[pos.tile], counts[pos.tile], qual.rstrip("\n")):
                truncated = True

    matrix = {
        tile: [
            s / c if c else 0.0
            for s, c in zip(sums[tile], counts[tile])
        ]
        for tile in sums
    }

    return ScanResult(
        source="present" if saw_tile else "absent",
        matrix=matrix,
        extents=extents,
        tile_count=len(matrix),
        sampled_reads=sampled,
        sample_rate=rate,
        records_inspected=records,
        truncated=truncated,
        worst_tile=_worst_tile(matrix),
    )


def _record_extent(extents: dict[int, TileExtent], pos: ReadPosition) -> None:
    """Widen a tile's bounding box to include one more cluster."""
    current = extents.get(pos.tile)
    if current is None:
        extents[pos.tile] = TileExtent(
            x_min=pos.x, x_max=pos.x, y_min=pos.y, y_max=pos.y, reads=1
        )
        return
    extents[pos.tile] = TileExtent(
        x_min=min(current.x_min, pos.x),
        x_max=max(current.x_max, pos.x),
        y_min=min(current.y_min, pos.y),
        y_max=max(current.y_max, pos.y),
        reads=current.reads + 1,
    )


def _accumulate(sums: list[float], counts: list[int], qual: str) -> bool:
    """Add one read's per-base quality to a tile's running totals.

    Rows grow to the longest read seen, so a file of mixed lengths is not
    silently truncated to its first read's length. Returns whether the
    position cap was hit.
    """
    hit_cap = False
    for i, ch in enumerate(qual):
        if i >= MAX_POSITIONS:
            hit_cap = True
            break
        if i >= len(sums):
            sums.append(0.0)
            counts.append(0)
        sums[i] += ord(ch) - _PHRED_OFFSET
        counts[i] += 1
    return hit_cap


def _worst_tile(matrix: dict[int, list[float]]) -> int | None:
    """The tile with the lowest mean quality across all its positions."""
    if not matrix:
        return None
    return min(matrix, key=lambda t: sum(matrix[t]) / len(matrix[t]) if matrix[t] else 0.0)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tile_scanner.py -q
```

Expected: `21 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/tile_scanner.py backend/tests/pipelines/test_tile_scanner.py
git commit -m "feat(pipelines): summarise read quality by flow-cell tile and position"
```

---

## Task 3: Serialise the matrix and build the facts

**Files:**
- Modify: `backend/app/pipelines/tile_scanner.py`
- Test: `backend/tests/pipelines/test_tile_scanner.py`

The `ScanResult` becomes two things: a sidecar JSON file, and a handful of scalars for the object document. Keeping this in the scanner (rather than the handler) keeps the handler a wiring layer.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_tile_scanner.py`:

```python
import json


def test_write_matrix_produces_a_sidecar_and_scalar_facts(tmp_path):
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [_illumina(1101, qual="IIII"), _illumina(1102, qual="5555")],
    )
    result = tile_scanner.scan(path)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    facts = tile_scanner.write_matrix(result, report_dir)

    assert facts["qc_tile_source"] == "present"
    assert facts["qc_tile_count"] == 2
    assert facts["qc_tile_matrix"] == tile_scanner.TILE_MATRIX_FILENAME
    assert facts["qc_tile_worst"]["tile"] == 1102
    # The sidecar holds the matrix; the facts must not.
    assert "matrix" not in facts

    written = json.loads((report_dir / tile_scanner.TILE_MATRIX_FILENAME).read_text())
    assert written["tiles"] == [1101, 1102]
    assert written["matrix"][0] == [40.0, 40.0, 40.0, 40.0]
    assert written["positions"] == 4


def test_write_matrix_writes_no_sidecar_when_tiles_are_absent(tmp_path):
    records = [(f"@SRR123456.{i}", "ACGT", "IIII") for i in range(1200)]
    path = _write_fastq(tmp_path / "r1.fastq", records)
    result = tile_scanner.scan(path)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    facts = tile_scanner.write_matrix(result, report_dir)

    assert facts["qc_tile_source"] == "absent"
    assert "qc_tile_matrix" not in facts
    assert not (report_dir / tile_scanner.TILE_MATRIX_FILENAME).exists()


def test_write_matrix_sorts_tiles_into_physical_order(tmp_path):
    # Encounter order is 1103 then 1101; stored order must be ascending, since
    # the chart's rows are meant to mirror the physical layout.
    path = _write_fastq(
        tmp_path / "r1.fastq", [_illumina(1103), _illumina(1101)]
    )
    result = tile_scanner.scan(path)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    tile_scanner.write_matrix(result, report_dir)

    written = json.loads((report_dir / tile_scanner.TILE_MATRIX_FILENAME).read_text())
    assert written["tiles"] == [1101, 1103]


def test_write_matrix_pads_ragged_rows(tmp_path):
    # Two tiles whose reads differ in length must still form a rectangle, or
    # the frontend cannot index the matrix by position.
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [
            ("@M01939:146:FC:1:1101:1:2 1:N:0:1", "ACGT", "IIII"),
            ("@M01939:146:FC:1:1102:1:3 1:N:0:1", "ACGTAA", "IIIIII"),
        ],
    )
    result = tile_scanner.scan(path)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    tile_scanner.write_matrix(result, report_dir)

    written = json.loads((report_dir / tile_scanner.TILE_MATRIX_FILENAME).read_text())
    assert written["positions"] == 6
    assert all(len(row) == 6 for row in written["matrix"])
    # The short row's missing positions are null, not zero -- zero is a real
    # Phred score and would draw as a defect.
    assert written["matrix"][0][4] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tile_scanner.py -q
```

Expected: FAIL — `AttributeError: module 'app.pipelines.tile_scanner' has no attribute 'write_matrix'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/app/pipelines/tile_scanner.py`:

```python
import json

# Filename of the sidecar, relative to the object's QC report directory --
# the same convention `qc_fastp_report` uses.
TILE_MATRIX_FILENAME = "tile_quality.json"


def write_matrix(result: ScanResult, report_dir: Path) -> dict:
    """Write the matrix as a sidecar and return the facts describing it.

    The matrix is deliberately *not* in the returned facts. A NovaSeq run is
    ~1408 tiles by 150 positions -- over 200,000 floats -- and object
    documents are read by the detail panel, summary prompts, and provenance,
    none of which want to carry it. `fastp_runner.parse_report` declined to
    inline "several hundred floats" for the same reason.
    """
    facts: dict = {
        "qc_tile_source": result.source,
        "qc_tile_count": result.tile_count,
        "qc_tile_sampled_reads": result.sampled_reads,
        "qc_tile_sample_rate": result.sample_rate,
        "qc_tile_truncated": result.truncated,
    }

    if result.source != "present" or not result.matrix:
        return facts

    # Ascending tile number: Illumina encodes surface, swath, and position in
    # it, so ascending order is what makes a smudge across adjacent tiles read
    # as one shape rather than scattered rows.
    tiles = sorted(result.matrix)
    positions = max(len(result.matrix[t]) for t in tiles)

    # Padded with null, not 0.0. Zero is a real Phred score and would draw as
    # a defect at every position a shorter read did not reach.
    matrix = [
        result.matrix[t] + [None] * (positions - len(result.matrix[t]))
        for t in tiles
    ]

    payload = {
        "tiles": tiles,
        "positions": positions,
        "matrix": matrix,
        "extents": {
            str(t): {
                "x_min": e.x_min,
                "x_max": e.x_max,
                "y_min": e.y_min,
                "y_max": e.y_max,
                "reads": e.reads,
            }
            for t, e in sorted(result.extents.items())
        },
        "sampled_reads": result.sampled_reads,
        "sample_rate": result.sample_rate,
        "truncated": result.truncated,
    }

    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / TILE_MATRIX_FILENAME).write_text(json.dumps(payload))

    facts["qc_tile_matrix"] = TILE_MATRIX_FILENAME
    if result.worst_tile is not None:
        row = result.matrix[result.worst_tile]
        worst_mean = sum(row) / len(row) if row else 0.0
        all_means = [
            sum(r) / len(r) for r in result.matrix.values() if r
        ]
        overall = sum(all_means) / len(all_means) if all_means else 0.0
        facts["qc_tile_worst"] = {
            "tile": result.worst_tile,
            "mean_quality": round(worst_mean, 2),
            "deficit": round(overall - worst_mean, 2),
        }

    return facts
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tile_scanner.py -q
```

Expected: `25 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/tile_scanner.py backend/tests/pipelines/test_tile_scanner.py
git commit -m "feat(pipelines): write the tile matrix as a sidecar, not into object facts"
```

---

## Task 4: Wire the scanner into the QC handler

**Files:**
- Modify: `backend/app/queue/pipeline_handlers.py` (`_run_short_read_qc`, after the `_run_fastqc` block at ~line 507-511)

The scanner runs under the rule already established for FastQC in this function: **its failure is a warning, never the job's.** QC that yields fastp's numbers without a tile matrix is a good outcome.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_tile_scan_handler.py`:

```python
"""The tile scan's contract with the QC handler: it contributes facts when it
can, and it never fails the job when it cannot."""

from pathlib import Path

import pytest

from app.queue import pipeline_handlers


def test_tile_facts_failure_is_swallowed(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(pipeline_handlers.tile_scanner, "scan", boom)

    # Returns empty rather than raising: a broken scan must not deny the user
    # the fastp facts that did parse.
    assert pipeline_handlers._tile_facts(tmp_path / "r1.fastq", tmp_path) == {}


def test_tile_facts_returns_scanner_facts(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pipeline_handlers.tile_scanner,
        "scan",
        lambda path, **kw: "sentinel-result",
    )
    monkeypatch.setattr(
        pipeline_handlers.tile_scanner,
        "write_matrix",
        lambda result, report_dir: {"qc_tile_source": "present"},
    )

    facts = pipeline_handlers._tile_facts(tmp_path / "r1.fastq", tmp_path)

    assert facts == {"qc_tile_source": "present"}


def test_tile_facts_reports_absent_for_a_file_without_tiles(tmp_path):
    """A file whose headers carry no tiles is an ordinary outcome.

    The handler must record `absent` rather than nothing at all: the frontend
    branches on this fact, and its absence would be indistinguishable from a
    QC run that predates this feature.
    """
    path = tmp_path / "r1.fastq"
    path.write_text("".join(f"@SRR123456.{i}\nACGT\n+\nIIII\n" for i in range(1200)))

    facts = pipeline_handlers._tile_facts(path, tmp_path / "reports")

    assert facts["qc_tile_source"] == "absent"
    assert "qc_tile_matrix" not in facts
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tile_scan_handler.py -q
```

Expected: FAIL — `AttributeError: module 'app.queue.pipeline_handlers' has no attribute '_tile_facts'`.

- [ ] **Step 3: Write the implementation**

In `backend/app/queue/pipeline_handlers.py`, add `tile_scanner` to the existing `from app.pipelines import (...)` import block, keeping alphabetical order.

Add this function next to `_run_fastqc`:

```python
def _tile_facts(reads_in: Path, report_dir: Path) -> dict:
    """Summarise quality by flow-cell tile, or return nothing.

    Swallows every failure to a warning, for the same reason `_run_fastqc`
    does: this is an extra on top of the fastp facts, and a file with an
    unexpected header format must not cost the user the numbers that did
    parse. Files without tiles in their headers -- SRA-stripped downloads,
    long reads -- are an ordinary outcome here, not a failure; the scanner
    reports them as `absent` after a short probe rather than reading the
    whole file.
    """
    try:
        result = tile_scanner.scan(reads_in)
        return tile_scanner.write_matrix(result, report_dir)
    except (OSError, ValueError) as e:
        log.warning("qc_tile_scan_failed", error=str(e))
        return {}
```

Then, in `_run_short_read_qc`, immediately after the existing FastQC block:

```python
    ctx.progress(phase="fastqc", pct=fastp_runner.MAX_MEASURED_PCT, message="running FastQC")
    fastqc_name = _run_fastqc(ctx, reads_in, report_dir, log_path)
    if fastqc_name:
        facts["qc_fastqc_report"] = fastqc_name

    ctx.progress(
        phase="tiles", pct=fastp_runner.MAX_MEASURED_PCT, message="scanning flow-cell tiles"
    )
    facts.update(_tile_facts(reads_in, report_dir))
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tile_scan_handler.py tests/pipelines/test_tile_scanner.py -q
```

Expected: `28 passed`.

- [ ] **Step 5: Run the full backend suite for regressions**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: the suite's usual count, all passing. **Read the count, not just the exit code.** If DB-touching tests fail in a rotating pattern, re-read the private-Mongo note in CLAUDE.md — that is two test runs sharing a database, not a bug in this change.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/pipeline_handlers.py backend/tests/pipelines/test_tile_scan_handler.py
git commit -m "feat(pipelines): scan flow-cell tiles as part of short-read QC"
```

---

## Task 5: The JSON route

**Files:**
- Modify: `backend/app/api/v1/pipelines.py`
- Test: `backend/tests/api/test_tile_matrix_route.py`

**Why not reuse `get_qc_report`:** that route serves under `sandbox` + `default-src 'none'`, which exists because FastQC embeds sequence bytes taken verbatim from the reads. That header set is exactly what would block a `fetch` from the app's own JavaScript. The new route keeps the ownership check and the traversal guard and drops the CSP, because it returns JSON the app parses rather than HTML a browser renders.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_tile_matrix_route.py`:

```python
"""The tile matrix route: same guards as the report route, different response.

The traversal cases matter as much as the happy path -- report directories
are named by object id and nothing else, so without the ownership check the
filesystem layout would be the access rule.
"""

import json

import pytest


@pytest.mark.asyncio
async def test_returns_the_matrix(client, qc_object, qc_report_dir):
    (qc_report_dir / "tile_quality.json").write_text(
        json.dumps({"tiles": [1101], "positions": 2, "matrix": [[40.0, 39.0]]})
    )

    res = await client.get(f"/api/v1/pipelines/qc/tiles/{qc_object.id}")

    assert res.status_code == 200
    assert res.json()["tiles"] == [1101]


@pytest.mark.asyncio
async def test_missing_matrix_is_a_404(client, qc_object, qc_report_dir):
    res = await client.get(f"/api/v1/pipelines/qc/tiles/{qc_object.id}")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_unknown_object_is_a_404(client):
    res = await client.get("/api/v1/pipelines/qc/tiles/000000000000000000000000")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_response_is_json_not_sandboxed_html(client, qc_object, qc_report_dir):
    # The sandbox CSP on get_qc_report would block the frontend's fetch. This
    # route must not carry it.
    (qc_report_dir / "tile_quality.json").write_text(json.dumps({"tiles": []}))

    res = await client.get(f"/api/v1/pipelines/qc/tiles/{qc_object.id}")

    assert "application/json" in res.headers["content-type"]
    assert "sandbox" not in res.headers.get("content-security-policy", "")
```

**Note on fixtures:** `client`, `qc_object`, and `qc_report_dir` may not exist yet. Check `backend/tests/api/conftest.py` and `backend/tests/conftest.py` first; reuse the existing client and object fixtures under whatever names they carry there, and add a `qc_report_dir` fixture that creates `settings.qc_reports_dir / str(object.id)` if none exists. Match the surrounding files' style rather than introducing a second one.

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/api/test_tile_matrix_route.py -q
```

Expected: FAIL with 404 on every case (route not registered).

- [ ] **Step 3: Write the implementation**

In `backend/app/api/v1/pipelines.py`, add `tile_scanner` to the `from app.pipelines import (...)` block, and add this route beside `get_qc_report`:

```python
@router.get("/qc/tiles/{object_id}")
async def get_qc_tile_matrix(object_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """Serve the per-tile quality matrix for an object.

    Deliberately not routed through `get_qc_report`, despite serving from the
    same directory. That route wraps its response in `sandbox` +
    `default-src 'none'` because FastQC's HTML embeds sequence bytes taken
    verbatim from the reads -- and that same header set would block the
    `fetch` this endpoint exists to answer. Here the payload is JSON the
    application parses rather than a document the browser renders, so the CSP
    is unnecessary and actively harmful.

    `OwnerDep`, not `LinkableOwnerDep`: this is fetched by the app's own code
    with the profile header attached, never opened as a bare link.

    The object read is discarded -- it is there to make the 404 happen.
    Report directories are named by object id and nothing else, so without it
    any caller holding an id could read any profile's matrix.
    """
    await object_service.get_object(object_id, owner=owner)

    root = (settings.qc_reports_dir / str(object_id)).resolve()
    target = (root / tile_scanner.TILE_MATRIX_FILENAME).resolve()

    # The filename is a module constant rather than user input, so traversal
    # is not reachable through it -- but the resolve-and-recheck costs a stat
    # and does not depend on that staying true.
    if not target.is_relative_to(root) or not target.is_file():
        raise NotFoundError(f"No tile matrix for object {object_id}")

    return json.loads(target.read_text())
```

Add `import json` to the module's imports if it is not already present.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/api/test_tile_matrix_route.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/tests/api/test_tile_matrix_route.py
git commit -m "feat(api): serve the per-tile quality matrix as JSON"
```

---

## Task 6: Frontend types and client

**Files:**
- Modify: `frontend/src/api/types.ts` (extend `QcFacts`, ~line 1201)
- Modify: `frontend/src/api/client.ts` (add fetch beside `qcReportUrl`, ~line 678)

- [ ] **Step 1: Extend the types**

In `frontend/src/api/types.ts`, add to the `QcFacts` interface, after `qc_fastqc_report`:

```typescript
  /** Whether the reads' headers carried flow-cell tile coordinates.
   *  "absent" covers SRA-stripped downloads and long reads -- an ordinary
   *  outcome, not a failure. The chart renders nothing unless "present". */
  qc_tile_source?: "present" | "absent";
  qc_tile_count?: number;
  qc_tile_sampled_reads?: number;
  qc_tile_sample_rate?: number;
  qc_tile_truncated?: boolean;
  /** Filename of the sidecar holding the matrix, which is too large to live
   *  in this document -- fetched separately via `api.qcTileMatrix`. */
  qc_tile_matrix?: string;
  qc_tile_worst?: {
    tile: number;
    mean_quality: number;
    /** How far below the run's overall mean this tile sits. */
    deficit: number;
  };
```

Add this exported interface near the other QC types:

```typescript
/** The per-tile quality matrix, served from its own route because it is far
 *  too large for the object document. Rows are tiles in ascending (physical)
 *  order; columns are read positions. A null cell is a position no read on
 *  that tile reached -- distinct from a genuine quality of zero. */
export interface TileMatrix {
  tiles: number[];
  positions: number;
  matrix: (number | null)[][];
  sampled_reads: number;
  sample_rate: number;
  truncated: boolean;
}
```

- [ ] **Step 2: Add the client call**

In `frontend/src/api/client.ts`, add beside `qcReportUrl`:

```typescript
  /**
   * The per-tile quality matrix. A `fetch`, not a link -- unlike
   * `qcReportUrl` above, which is a plain `<a href>` and therefore needs the
   * profile as a query param. This one rides the normal profile header.
   */
  qcTileMatrix: (objectId: string) =>
    request<TileMatrix>(`/pipelines/qc/tiles/${objectId}`),
```

Add `TileMatrix` to the existing `import type { ... } from "./types"` block.

- [ ] **Step 3: Verify the types compile**

```bash
docker compose -p bioflow-worktree exec web npx tsc --noEmit
```

If that container is not running, run `./ops/worktree-up.sh` first. Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(frontend): type the per-tile quality matrix and its fetch"
```

---

## Task 7: The heatmap component

**Files:**
- Create: `frontend/src/components/TileQualityChart.tsx`

Two colour scales over one matrix. **Absolute is the default** because its failure mode is loud — a mediocre run ambers everywhere and you know it. The relative scale isolates a single bad tile better but *erases* a run-wide dip, since when every tile drops together no tile deviates; that blind spot is why it is a toggle with a caption rather than the default.

- [ ] **Step 1: Write the component**

Create `frontend/src/components/TileQualityChart.tsx`:

```tsx
import { useEffect, useMemo, useRef, useState } from "react";
import type { TileMatrix } from "../api/types";

/** Above this many cells, one <rect> per cell is too many DOM nodes and the
 *  chart draws to a canvas instead. A NovaSeq matrix is 200,000+; the SVG
 *  path is for MiSeq-scale runs where crisp vector cells are nicer. */
const CANVAS_THRESHOLD = 20_000;

type Scale = "absolute" | "relative";

/**
 * Mean quality by flow-cell tile and read position.
 *
 * Rows are tiles in ascending order, which is physical order -- Illumina
 * encodes surface, swath, and position in the tile number, so a smudge
 * covering adjacent tiles reads as one band rather than scattered rows.
 * Sorting by "worst first" would find the bad tile faster and destroy exactly
 * the spatial structure this chart exists to show; `qc_tile_worst` in the
 * facts does that job instead.
 */
export function TileQualityChart({ data }: { data: TileMatrix }) {
  const [scale, setScale] = useState<Scale>("absolute");
  const [hover, setHover] = useState<{ tile: number; pos: number; q: number } | null>(null);

  const rows = data.tiles.length;
  const cols = data.positions;
  const cells = rows * cols;

  // Per-position mean across tiles: the baseline the relative scale compares
  // each cell against. Nulls are skipped rather than counted as zero.
  const positionMeans = useMemo(() => {
    const means: number[] = [];
    for (let p = 0; p < cols; p++) {
      let sum = 0;
      let n = 0;
      for (let t = 0; t < rows; t++) {
        const v = data.matrix[t]?.[p];
        if (v != null) {
          sum += v;
          n += 1;
        }
      }
      means.push(n ? sum / n : 0);
    }
    return means;
  }, [data, rows, cols]);

  const colorFor = useMemo(() => {
    return (q: number | null, p: number): string => {
      if (q == null) return "transparent";
      if (scale === "absolute") return absoluteColor(q);
      return relativeColor(q - positionMeans[p]);
    };
  }, [scale, positionMeans]);

  return (
    <div>
      <div className="tile-scale-toggle">
        <button
          className={scale === "absolute" ? "active" : ""}
          onClick={() => setScale("absolute")}
        >
          Absolute
        </button>
        <button
          className={scale === "relative" ? "active" : ""}
          onClick={() => setScale("relative")}
        >
          Relative
        </button>
      </div>

      {cells > CANVAS_THRESHOLD ? (
        <TileCanvas data={data} colorFor={colorFor} onHover={setHover} />
      ) : (
        <TileSvg data={data} colorFor={colorFor} onHover={setHover} />
      )}

      <div className="tile-readout">
        {hover
          ? `Tile ${hover.tile} · position ${hover.pos} · Q${hover.q.toFixed(1)}`
          : `${rows.toLocaleString()} tiles · ${cols} positions · 1 in ${data.sample_rate} reads sampled`}
      </div>

      {scale === "relative" && (
        /* Load-bearing caption. This scale shows deviation from each
           position's mean, so a dip that hits every tile equally -- a
           fluidics stumble at one cycle -- produces no deviation anywhere and
           renders as a clean plot. Without this line that reads as "nothing
           wrong". */
        <div className="tile-note">
          Showing each tile’s deviation from the average at that position. A dip
          affecting every tile equally will not appear here — check the absolute
          scale for that.
        </div>
      )}

      {data.truncated && (
        <div className="tile-note">
          Showing the first {rows.toLocaleString()} tiles; this file has more.
        </div>
      )}
    </div>
  );
}

/** Absolute Phred on the same thresholds QualityChart uses, so a colour means
 *  the same thing on both charts. */
function absoluteColor(q: number): string {
  if (q >= 30) {
    const t = Math.min((q - 30) / 8, 1);
    return mix([31, 90, 45], [63, 185, 80], t);
  }
  if (q >= 20) return mix([248, 81, 73], [210, 153, 34], (q - 20) / 10);
  return mix([90, 20, 18], [248, 81, 73], Math.max(q / 20, 0));
}

/** Deviation from the position mean. At or above it is a flat cool tone --
 *  deliberately unshowy, so the eye goes only to what is below. */
function relativeColor(delta: number): string {
  if (delta >= -0.5) {
    return mix([30, 42, 58], [74, 158, 255], Math.min(Math.max(delta, 0) / 3, 1) * 0.45);
  }
  return mix([30, 42, 58], [248, 81, 73], Math.min(-delta / 10, 1));
}

function mix(a: number[], b: number[], t: number): string {
  const c = (i: number) => Math.round(a[i] + (b[i] - a[i]) * t);
  return `rgb(${c(0)},${c(1)},${c(2)})`;
}

type CellRenderer = {
  data: TileMatrix;
  colorFor: (q: number | null, p: number) => string;
  onHover: (h: { tile: number; pos: number; q: number } | null) => void;
};

const PAD = { left: 42, bottom: 26, top: 4 };
const PLOT_W = 460;
const MAX_PLOT_H = 320;

/** Row height in pixels, and how many tiles share a row when they outnumber
 *  the pixels available. Binning is unavoidable on a big flow cell -- 1408
 *  rows do not fit in 320px -- so it is done explicitly here rather than left
 *  to the browser's image smoothing. */
function layout(rows: number) {
  const rowH = Math.max(1, Math.min(9, Math.floor(MAX_PLOT_H / rows)));
  const tilesPerRow = Math.max(1, Math.ceil(rows / Math.floor(MAX_PLOT_H / rowH)));
  const drawnRows = Math.ceil(rows / tilesPerRow);
  return { rowH, tilesPerRow, drawnRows, plotH: drawnRows * rowH };
}

function TileSvg({ data, colorFor, onHover }: CellRenderer) {
  const rows = data.tiles.length;
  const cols = data.positions;
  const { rowH, tilesPerRow, plotH } = layout(rows);
  const cellW = PLOT_W / cols;

  const rects: JSX.Element[] = [];
  for (let t = 0; t < rows; t++) {
    const drawnRow = Math.floor(t / tilesPerRow);
    for (let p = 0; p < cols; p++) {
      const q = data.matrix[t]?.[p] ?? null;
      rects.push(
        <rect
          key={`${t}-${p}`}
          x={PAD.left + p * cellW}
          y={PAD.top + drawnRow * rowH}
          width={Math.ceil(cellW)}
          height={rowH}
          fill={colorFor(q, p)}
          onMouseEnter={() =>
            q != null && onHover({ tile: data.tiles[t], pos: p + 1, q })
          }
        />,
      );
    }
  }

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${PAD.left + PLOT_W + 6} ${PAD.top + plotH + PAD.bottom}`}
      style={{ maxWidth: PAD.left + PLOT_W + 6, display: "block" }}
      onMouseLeave={() => onHover(null)}
    >
      {rects}
      <Axes data={data} plotH={plotH} rowH={rowH} tilesPerRow={tilesPerRow} />
    </svg>
  );
}

function TileCanvas({ data, colorFor, onHover }: CellRenderer) {
  const ref = useRef<HTMLCanvasElement>(null);
  const rows = data.tiles.length;
  const cols = data.positions;
  const { rowH, tilesPerRow, drawnRows, plotH } = layout(rows);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = PLOT_W * dpr;
    canvas.height = plotH * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, PLOT_W, plotH);

    const cellW = PLOT_W / cols;
    // One pass, painting each source tile into its binned row. Later tiles in
    // a bin overpaint earlier ones; with a bin of a handful of adjacent tiles
    // that is visually indistinguishable from averaging them, and it keeps
    // this to a single loop over the matrix.
    for (let t = 0; t < rows; t++) {
      const y = PAD.top + Math.floor(t / tilesPerRow) * rowH;
      for (let p = 0; p < cols; p++) {
        const q = data.matrix[t]?.[p] ?? null;
        if (q == null) continue;
        ctx.fillStyle = colorFor(q, p);
        ctx.fillRect(p * cellW, y - PAD.top, Math.ceil(cellW), rowH);
      }
    }
  }, [data, colorFor, rows, cols, rowH, tilesPerRow, plotH]);

  function handleMove(e: React.MouseEvent<HTMLCanvasElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const p = Math.floor(((e.clientX - rect.left) / rect.width) * cols);
    const drawnRow = Math.floor(((e.clientY - rect.top) / rect.height) * drawnRows);
    // Read from the unbinned data so the tooltip names a tile that exists,
    // even when its row is several tiles wide.
    const t = Math.min(drawnRow * tilesPerRow, rows - 1);
    const q = data.matrix[t]?.[p];
    if (q == null) {
      onHover(null);
      return;
    }
    onHover({ tile: data.tiles[t], pos: p + 1, q });
  }

  return (
    <div style={{ position: "relative", paddingLeft: PAD.left }}>
      <canvas
        ref={ref}
        style={{ width: PLOT_W, height: plotH, display: "block" }}
        onMouseMove={handleMove}
        onMouseLeave={() => onHover(null)}
      />
    </div>
  );
}

function Axes({
  data,
  plotH,
  rowH,
  tilesPerRow,
}: {
  data: TileMatrix;
  plotH: number;
  rowH: number;
  tilesPerRow: number;
}) {
  const cols = data.positions;
  const step = Math.max(1, Math.round(cols / 5));
  const ticks: JSX.Element[] = [];

  for (let p = 0; p < cols; p += step) {
    ticks.push(
      <text
        key={`x${p}`}
        x={PAD.left + (p / cols) * PLOT_W}
        y={PAD.top + plotH + 14}
        fontSize="9"
        fill="var(--text-faint)"
        textAnchor="middle"
      >
        {p + 1}
      </text>,
    );
  }

  const rowStep = Math.max(1, Math.round(data.tiles.length / tilesPerRow / 4));
  for (let r = 0; r * tilesPerRow < data.tiles.length; r += rowStep) {
    ticks.push(
      <text
        key={`y${r}`}
        x={PAD.left - 6}
        y={PAD.top + r * rowH + 7}
        fontSize="9"
        fill="var(--text-faint)"
        textAnchor="end"
      >
        {data.tiles[r * tilesPerRow]}
      </text>,
    );
  }

  return (
    <>
      {ticks}
      <text
        x={PAD.left + PLOT_W / 2}
        y={PAD.top + plotH + PAD.bottom - 2}
        fontSize="10"
        fill="var(--text-dim)"
        textAnchor="middle"
      >
        Position in read (bp)
      </text>
    </>
  );
}
```

- [ ] **Step 2: Add the styles**

Append to `frontend/src/styles.css`:

```css
.tile-scale-toggle {
  display: flex;
  gap: 4px;
  margin-bottom: 8px;
}

.tile-scale-toggle button {
  font-size: 11px;
  padding: 2px 9px;
  border-radius: 4px;
  border: 1px solid var(--border, #2a3038);
  background: var(--bg-elevated);
  color: var(--text-dim);
  cursor: pointer;
}

.tile-scale-toggle button.active {
  color: var(--text);
  border-color: var(--accent);
}

.tile-readout {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 6px;
  min-height: 16px;
}

.tile-note {
  font-size: 11px;
  color: var(--text-faint);
  margin-top: 6px;
  max-width: 460px;
}
```

- [ ] **Step 3: Verify it compiles**

```bash
docker compose -p bioflow-worktree exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/TileQualityChart.tsx frontend/src/styles.css
git commit -m "feat(ui): draw per-tile quality as a heatmap with two colour scales"
```

---

## Task 8: Mount the chart on the QC tab

**Files:**
- Modify: `frontend/src/components/DetailPanel.tsx` (the `qc-charts` block, ~line 1005-1031)

- [ ] **Step 1: Add the fetch hook**

In `DetailPanel.tsx`, add the import:

```tsx
import { TileQualityChart } from "./TileQualityChart";
import type { TileMatrix } from "../api/types";
```

Add near the component's other state, before the QC tab's JSX:

```tsx
  // Fetched only when the QC tab is showing and the file actually has tiles.
  // The matrix is far larger than the object document it is described by, so
  // it must not ride along with the detail panel's own load.
  const tileSource = obj.facts.qc_tile_source as string | undefined;
  const [tiles, setTiles] = useState<TileMatrix | null>(null);

  useEffect(() => {
    if (tab !== "qc" || tileSource !== "present") {
      setTiles(null);
      return;
    }
    let cancelled = false;
    api
      .qcTileMatrix(obj.id)
      .then((m) => !cancelled && setTiles(m))
      // A missing or unreadable matrix renders nothing, the same as a file
      // that never had tiles. It is an extra, not a promise.
      .catch(() => !cancelled && setTiles(null));
    return () => {
      cancelled = true;
    };
  }, [obj.id, tab, tileSource]);
```

**Note:** match the existing tab-state variable's name — check what the surrounding code calls it (it may not be `tab`) and use that.

- [ ] **Step 2: Render it**

Inside the `qc-charts` block in `DetailPanel.tsx`, after the `{curve && ...}` chart and before `{showChromStrip && ...}`:

```tsx
          {tiles && (
            <div className="qc-chart">
              <div className="section-title">Quality per tile</div>
              <TileQualityChart data={tiles} />
            </div>
          )}
```

Also extend the block's render condition so the chart can appear on a file with no other chart data — change:

```tsx
      {(composition || curve || showChromStrip) && (
```

to:

```tsx
      {(composition || curve || showChromStrip || tiles) && (
```

- [ ] **Step 3: Verify it compiles**

```bash
docker compose -p bioflow-worktree exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx
git commit -m "feat(ui): show the per-tile quality heatmap on the QC tab"
```

---

## Task 9: Verify against real data

This is the task that catches what fixtures cannot. Per CLAUDE.md: hand-built objects that already look the way the code expects will pass a green suite while real inputs fail — that is exactly how the suggestion rules shipped counting `protein.faa` as an alignable reference.

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

UI on 5273, API on 8100. Do **not** use plain `docker compose` here — it would repoint the main 5173 stack at this worktree.

- [ ] **Step 2: Check header parsing against real reads**

```bash
docker compose -p bioflow-worktree exec api python -c "
from pathlib import Path
from app.pipelines import tile_scanner
import sys
p = Path(sys.argv[1])
r = tile_scanner.scan(p)
print('source', r.source, 'tiles', r.tile_count, 'sampled', r.sampled_reads, 'rate', r.sample_rate)
print('worst', r.worst_tile, 'truncated', r.truncated)
" /path/to/a/real/reads_R1.fastq.gz
```

Substitute a real FASTQ from the user's `/data`. Expect `source present` and a plausible tile count on an Illumina file. On an SRA download expect `source absent` — and confirm it returned quickly rather than reading the whole file.

- [ ] **Step 3: Measure the cost**

This is the spec's named open question — the full sequential pass is unmeasured on real data.

```bash
docker compose -p bioflow-worktree exec api python -c "
import time
from pathlib import Path
from app.pipelines import tile_scanner
import sys
p = Path(sys.argv[1])
t = time.time()
r = tile_scanner.scan(p)
print(f'{p.stat().st_size / 1e9:.2f} GB in {time.time() - t:.1f}s -> {r.tile_count} tiles')
" /path/to/a/real/reads_R1.fastq.gz
```

**Record the number in the PR description.** If it is bad enough to be a problem, the fix is to make the pass opt-in per run — *not* to cap it to a prefix, which reads the first few tiles completely and the rest not at all.

- [ ] **Step 4: Run QC end-to-end and look at the chart**

Run a QC job on a real Illumina file from the UI at localhost:5273, then open the file's QC tab. Confirm:
- The heatmap renders and its tile numbers look like real Illumina tile numbers.
- Both scale toggles work and the relative caption appears.
- Hovering names a tile and a position.
- A long-read or SRA file's QC tab shows no heatmap at all, and no error.

- [ ] **Step 5: Full suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Read the count. All passing.

- [ ] **Step 6: Tear down**

```bash
./ops/worktree-up.sh --down
```

---

## Task 10: Open the PR

- [ ] **Step 1: Push**

```bash
git push -u origin HEAD
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --base main --fill
```

The title lands in the release notes verbatim. Use:

```
feat(ui): show read quality per flow-cell tile, not just per position
```

In the description, cover the "why" (a per-position curve cannot distinguish a run-wide dip from one bad tile), and **include the runtime number measured in Task 9**. Label the PR with its `type:` and `area:` labels — `.github/release.yml` categorises by label, and an unlabelled PR lands under "Other changes".

- [ ] **Step 3: Report the URL and stop**

Do not merge. The user reviews and merges.

---

## Notes for the implementer

**Things that will look wrong but are deliberate:**

- **The relative scale hides a real defect.** That is a property of the metric, not a bug to fix. The caption is the mitigation. Do not "improve" it by making the relative scale also flag low absolute values — that produces a third metric that is neither, and the toggle stops meaning anything.
- **Padding is `null`, not `0.0`.** Zero is a real Phred score and would draw as a defect everywhere a short read did not reach.
- **Tiles sort ascending, not worst-first.** Physical order is what makes a smudge legible as one shape.
- **R1 only.** A flow-cell defect appears in both mates; scanning R2 doubles the I/O to confirm what R1 already said.

**If the full suite goes red in a rotating pattern** — different DB-touching tests each run, all passing in isolation — that is two test runs sharing one Mongo, not this change. See the private-replica-set note in CLAUDE.md.

**`worker` does not hot-reload.** After changing `pipeline_handlers.py`, the worktree stack's worker keeps running the old in-memory code until restarted, which reads as "the fix didn't work":

```bash
docker compose -p bioflow-worktree restart worker
```
