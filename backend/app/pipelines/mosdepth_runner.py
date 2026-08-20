"""mosdepth command-building, windows-BED generation, and output parsing.

A pure module, on the feature_coverage_runner.py model: no subprocess calls
live here, so every function is unit-testable without the binary installed.
The handler in app/queue/mosdepth_handlers.py supplies the process.

Windowing deliberately reuses gc_tracks' scheme rather than picking its own,
because the depth track is drawn on the same axis as the GC track -- a
different window count would put the two a window out of step.

Output shapes are pinned to a real mosdepth 0.3.14 run captured on
2026-08-20; see tests/pipelines/test_mosdepth_runner.py for the fixtures and
for the two traps captured output revealed (the `<contig>_region` summary
rows, and depth moving from column 4 to column 5 when the target BED carries
names).
"""

from __future__ import annotations

import gzip
from pathlib import Path

import structlog

from app.pipelines import gc_tracks

log = structlog.get_logger(__name__)

# Imported, not redeclared: a local copy silently drifts from the GC track's
# axis, and the depth track is drawn against that same axis.
WINDOW_COUNT = gc_tracks.WINDOW_COUNT  # 500
MIN_WINDOW_BASES = gc_tracks.MIN_WINDOW_BASES  # 100

# Depths reported on the coverage card as "% of bases at >= Nx".
BREADTH_THRESHOLDS = (1, 10, 30)


def build_command(
    *,
    bam: Path,
    prefix: Path,
    windows_bed: Path | None = None,
    regions_bed: Path | None = None,
    threads: int = 1,
) -> list[str]:
    """Build `mosdepth --by <bed> --no-per-base -t N <prefix> <bam>`.

    Exactly one of `windows_bed` / `regions_bed` must be given: mosdepth
    accepts a single `--by`, so passing both would silently drop one.

    `--no-per-base` is not an optimisation to revisit -- the per-base file is
    one row per reference base, which is gigabytes on a real genome, and
    nothing in the coverage report reads it.
    """
    if bool(windows_bed) == bool(regions_bed):
        raise ValueError(
            "build_command needs exactly one of windows_bed or regions_bed; "
            f"got windows_bed={windows_bed!r}, regions_bed={regions_bed!r}"
        )
    by = windows_bed or regions_bed
    return [
        "mosdepth",
        "--by",
        str(by),
        "--no-per-base",
        "-t",
        str(threads),
        # Positional order is `<prefix> <bam>`. Swapped, mosdepth treats the
        # BAM path as an output prefix and writes its outputs next to the
        # alignment -- inside objects/, which is content-addressed.
        str(prefix),
        str(bam),
    ]


def build_windows_bed(contig_lengths: dict[str, int]) -> list[tuple[str, int, int]]:
    """Tile each contig into windows, mirroring gc_tracks' scheme.

    `window_count = min(WINDOW_COUNT, length // MIN_WINDOW_BASES)` per contig,
    so a contig shorter than MIN_WINDOW_BASES yields no windows at all -- the
    same floor gc_tracks applies, and the reason a 50bp contig is absent from
    both tracks rather than present in one.

    Emits exactly `window_count` windows per contig, with the last running to
    the contig end. The obvious `range(0, length, width)` instead emits a
    stray short final window whenever width does not divide length evenly,
    which is off-by-one against the GC track it has to line up with.
    """
    beds: list[tuple[str, int, int]] = []
    for chrom, length in contig_lengths.items():
        window_count = min(WINDOW_COUNT, length // MIN_WINDOW_BASES)
        if window_count == 0:
            continue
        window_bases = length // window_count
        if window_bases == 0:
            continue
        for index in range(window_count):
            start = index * window_bases
            # The final window absorbs the remainder, matching gc_tracks'
            # `seq[start:]` for its last chunk.
            end = length if index == window_count - 1 else start + window_bases
            beds.append((chrom, start, end))
    return beds


def render_windows_bed(windows: list[tuple[str, int, int]]) -> str:
    """Serialize windows to BED text for `--by`."""
    return "".join(f"{chrom}\t{start}\t{end}\n" for chrom, start, end in windows)


def parse_summary(path: Path) -> dict:
    """Parse `<prefix>.mosdepth.summary.txt`.

    The file interleaves a `<contig>_region` row after every contig row when
    `--by` is used, and ends with `total` and `total_region`. Treating those
    as contigs doubles the contig list, so the `_region` rows are dropped --
    but by matching them against a real base row rather than on the suffix
    alone, since `scaffold_region` is a name an assembler can genuinely emit.

    Returns `{"contigs": [...], "total": {...} | None}`. A missing file is
    empty rather than an exception: a mosdepth run that failed has already
    been reported by the handler, and raising here would replace that error
    with a less specific one.
    """
    empty: dict = {"contigs": [], "total": None}
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError) as e:
        log.warning("mosdepth_summary_unreadable", path=str(path), error=str(e))
        return empty

    rows: dict[str, dict] = {}
    order: list[str] = []
    for line in text.splitlines():
        fields = line.rstrip("\n").split("\t")
        if len(fields) < 6 or fields[0] == "chrom":
            continue
        name = fields[0]
        try:
            row = {
                "name": name,
                "length": int(fields[1]),
                "bases": int(fields[2]),
                "mean": float(fields[3]),
                "min": int(fields[4]),
                "max": int(fields[5]),
            }
        except ValueError:
            log.warning("mosdepth_summary_bad_row", path=str(path), row=line)
            continue
        rows[name] = row
        order.append(name)

    total = rows.get("total")
    contigs = [
        rows[name]
        for name in order
        if name not in ("total", "total_region")
        # Drop `chr1_region` only when `chr1` is itself present; a contig
        # genuinely named `<x>_region` has no such partner and survives.
        and not (name.endswith("_region") and name[: -len("_region")] in rows)
    ]
    return {"contigs": contigs, "total": total}


def parse_regions(path: Path) -> dict[str, list[dict]]:
    """Parse `<prefix>.regions.bed.gz` into `{chrom: [window, ...]}`.

    Column count varies with the target BED: a plain 3-column BED yields
    `chrom start end depth`, while a 4-column one propagates the name and
    yields `chrom start end name depth`. Hardcoding depth at column 4 reads a
    gene name as a float on the second shape.
    """
    regions: dict[str, list[dict]] = {}
    try:
        with gzip.open(path, "rt") as fh:
            for line in fh:
                fields = line.rstrip("\n").split("\t")
                if len(fields) < 4:
                    continue
                chrom = fields[0]
                try:
                    start = int(fields[1])
                    end = int(fields[2])
                    if len(fields) >= 5:
                        name: str | None = fields[3]
                        depth = float(fields[4])
                    else:
                        name = None
                        depth = float(fields[3])
                except ValueError:
                    log.warning("mosdepth_regions_bad_row", path=str(path), row=line)
                    continue
                regions.setdefault(chrom, []).append(
                    {"start": start, "end": end, "depth": depth, "name": name}
                )
    except (OSError, EOFError, gzip.BadGzipFile) as e:
        log.warning("mosdepth_regions_unreadable", path=str(path), error=str(e))
        return {}
    return regions


def parse_dist(path: Path) -> dict[int, float]:
    """Parse `<prefix>.mosdepth.global.dist.txt` into `{depth: fraction}`.

    Each row is `<contig> <depth> <fraction of bases at >= depth>`, with a
    `total` series across the whole reference. Only `total` is kept -- the
    per-contig series are what the per-window regions already describe.
    """
    dist: dict[int, float] = {}
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError) as e:
        log.warning("mosdepth_dist_unreadable", path=str(path), error=str(e))
        return {}
    for line in text.splitlines():
        fields = line.rstrip("\n").split("\t")
        if len(fields) < 3 or fields[0] != "total":
            continue
        try:
            dist[int(fields[1])] = float(fields[2])
        except ValueError:
            log.warning("mosdepth_dist_bad_row", path=str(path), row=line)
    return dist


def build_report(*, prefix: Path) -> dict:
    """Collect every mosdepth output beside `prefix` into one report dict.

    This is the JSON written to `coverage_dir` and served by the report
    route; `summarize()` reduces it to the handful of facts merged onto the
    BAM object.
    """
    prefix = Path(prefix)
    summary = parse_summary(prefix.with_suffix(prefix.suffix + ".mosdepth.summary.txt"))
    return {
        "contigs": summary["contigs"],
        "total": summary["total"],
        "regions": parse_regions(
            prefix.with_suffix(prefix.suffix + ".regions.bed.gz")
        ),
        "dist": parse_dist(
            prefix.with_suffix(prefix.suffix + ".mosdepth.global.dist.txt")
        ),
        "window_count": WINDOW_COUNT,
    }


def summarize(report: dict) -> dict:
    """Reduce a report to the `coverage_*` facts merged onto the BAM.

    Returns `{}` for a report with no total row rather than a dict of Nones:
    the fact renderer draws a row per key, so Nones become blank rows that
    read as a broken job rather than as one that produced nothing.
    """
    total = report.get("total")
    if not total:
        return {}

    facts: dict = {
        "coverage_mean_depth": total["mean"],
        "coverage_reference_length": total["length"],
        "coverage_bases_covered": total["bases"],
        "coverage_max_depth": total["max"],
        "coverage_contig_count": len(report.get("contigs") or []),
    }

    dist = report.get("dist") or {}
    for threshold in BREADTH_THRESHOLDS:
        fraction = dist.get(threshold)
        if fraction is not None:
            facts[f"coverage_pct_at_{threshold}x"] = round(100.0 * fraction, 2)
    return facts
