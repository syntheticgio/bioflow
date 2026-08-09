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

import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import IO

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

            # Bound extents by the same MAX_TILES cap as the quality matrix --
            # tracking a fabricated tile's coordinates unconditionally would
            # let a malformed file grow this dict without bound even while
            # sums/counts correctly stopped, defeating half the guardrail.
            if pos.tile in extents or len(extents) < MAX_TILES:
                _record_extent(extents, pos)
            else:
                truncated = True

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

    # Every index in counts[tile] is created and incremented together with
    # the matching sums[tile] entry in _accumulate, so c is never 0 here --
    # no fallback needed, and one would risk masking a real Q0 read as the
    # same 0.0 a missing sample would produce.
    matrix = {
        tile: [s / c for s, c in zip(sums[tile], counts[tile])]
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
