"""Building and parsing samtools output for the BAM Results tab.

Kept separate from the job handler so the parts worth testing -- command
construction, idxstats/coverage parsing, depth binning -- are pure functions
over strings, lists, and paths, with no queue or filesystem involved. Mirrors
align_runner.py's split for the same reason.
"""

from collections.abc import Iterator
from pathlib import Path

# Fixed regardless of genome size, so the array in `facts` is a constant size
# whether the reference is a 5 kb plasmid or a 3 Gb human genome. A contig
# shorter than one bin still gets one bin (see bin_depth), so small contigs
# never vanish from the plot.
BIN_COUNT = 1000

# The thresholds the summary and cumulative curve report against. 1x is
# "sequenced at all"; 10x and 30x are the conventional thresholds for calling
# a heterozygous and a somatic variant respectively.
COVERAGE_THRESHOLDS = (1, 10, 30)

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

_TSV_INT_COLUMNS = {"length", "reads", "unmapped_reads", "covered_bases", "start", "end"}
_TSV_FLOAT_COLUMNS = {"coverage_pct", "mean_depth", "mean_baseq", "mean_mapq"}


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
        {
            **row,
            # coverage reports start/end, not a length column directly;
            # derived here once so every downstream consumer (genome_summary,
            # contigs_tsv, bin_depth's contig_lengths) can rely on `length`
            # being present without recomputing it.
            "length": row["end"] - row["start"] + 1,
            "unmapped_reads": unmapped_by_contig.get(row["contig"], 0),
        }
        for row in coverage_rows
    ]
    merged.sort(key=lambda r: r["reads"], reverse=True)
    return merged


def allocate_bins(
    *,
    contig_lengths: list[tuple[str, int]],
    bin_count: int,
) -> tuple[dict[str, tuple[int, float]], list[dict], dict[str, int]]:
    """Lay contigs end to end across a fixed number of bins.

    Bins are allocated proportionally to each contig's length, with one floor:
    every contig gets at least one bin regardless of its share, so a short
    scaffold is never averaged away into a neighbour's bin or omitted
    entirely. Independent per-contig rounding can land the total a few bins
    over `bin_count` (each contig rounds to its nearest bin count, and those
    errors can accumulate in the same direction); the excess is then taken
    one bin at a time from the largest contigs that can spare one, so no
    single contig -- least of all whichever happens to be last -- is ever
    driven below the floor. The allocation always sums to exactly
    `bin_count`.

    That floor is unsatisfiable when there are more contigs than bins (a
    fragmented draft assembly can have thousands of scaffolds against a
    default `bin_count` in the hundreds) -- there simply are not enough bins
    for one each. Rather than let the floor go negative, this degrades to
    grouping contigs by index into `bin_count` contiguous runs (`floor(i *
    bin_count / n)`), one bin per run: every contig in a run maps its full
    length onto that single shared bin, and only the run's first contig is
    credited in `counts` so the total still sums to `bin_count`. Per-contig
    resolution is lost in that regime -- unavoidable when scaffolds outnumber
    bins -- but every `start_bin` stays in range and no allocation goes
    negative.

    Extracted from `bin_depth` so variant density (which counts per bin) and
    read depth (which averages per bin) share one definition of where a
    contig sits on the axis. Returns `(geometry, boundaries, counts)`:
    `geometry` maps contig -> (start_bin, positions_per_bin), `boundaries`
    marks which bin index starts each contig for drawing separators, and
    `counts` maps contig -> how many bins it was given (0 for a contig that
    shares its run's bin with an earlier contig).
    """
    total_length = sum(length for _, length in contig_lengths)
    if total_length <= 0 or bin_count <= 0:
        return {}, [], {}

    n = len(contig_lengths)

    if n > bin_count:
        # More scaffolds than bins: the one-bin-per-contig floor cannot hold,
        # so fall back to one bin per contiguous run of contigs. `i *
        # bin_count // n` is non-decreasing in `i` and hits every value in
        # `0..bin_count-1` at least once, so every bin is covered and every
        # start_bin is in range by construction -- no correction pass needed.
        contig_bin_counts = {}
        geometry = {}
        boundaries = []
        prev_start = -1
        for i, (name, length) in enumerate(contig_lengths):
            start_bin = (i * bin_count) // n
            # The whole contig collapses onto its one shared bin.
            positions_per_bin = max(length, 1)
            geometry[name] = (start_bin, positions_per_bin)
            if start_bin != prev_start:
                contig_bin_counts[name] = 1
                boundaries.append({"contig": name, "bin_start": start_bin})
                prev_start = start_bin
            else:
                # Shares its run's bin with the contig before it -- credited
                # with 0 bins of its own so `counts` still sums to
                # `bin_count`. `start_bin` above is still the correct (in
                # range) shared bin; bin_depth treats a 0 count as "this
                # contig's whole span is bin 0 of its run" rather than
                # letting the offset clamp go negative.
                contig_bin_counts[name] = 0
        return geometry, boundaries, contig_bin_counts

    floor_bins = min(bin_count, n)
    remaining_bins = bin_count - floor_bins

    contig_bin_counts: dict[str, int] = {}
    for name, length in contig_lengths:
        share = round(remaining_bins * length / total_length) if total_length else 0
        contig_bin_counts[name] = 1 + share

    allocated = sum(contig_bin_counts.values())
    excess = allocated - bin_count
    if excess > 0:
        # Take the excess back one bin at a time from the largest contigs
        # that can still spare one (more than the floor of 1) -- dumping it
        # all on a single contig, especially whichever happens to be last,
        # can drive that contig to 0 or negative bins and crash the geometry
        # pass below with a ZeroDivisionError. Largest-first keeps the
        # length-proportionality the allocation is meant to express: a big
        # contig giving up one of its many bins barely moves its share,
        # while a small one giving up its only bin would erase it.
        by_size_desc = sorted(
            contig_lengths, key=lambda item: item[1], reverse=True
        )
        while excess > 0:
            took_one = False
            for name, _ in by_size_desc:
                if contig_bin_counts[name] > 1:
                    contig_bin_counts[name] -= 1
                    excess -= 1
                    took_one = True
                    if excess == 0:
                        break
            if not took_one:
                # Every contig is already at the floor of 1 -- cannot happen
                # on this path since n <= bin_count guarantees at least
                # `bin_count` bins are available to give back, but breaking
                # here rather than looping forever is the safe failure mode
                # if that invariant is ever violated.
                break
    elif excess < 0 and contig_lengths:
        # Under-allocated (roundings summed short): the last contig is a
        # fine place to add the shortfall since adding bins never risks
        # crossing the floor.
        last_name = contig_lengths[-1][0]
        contig_bin_counts[last_name] += -excess

    geometry: dict[str, tuple[int, float]] = {}
    boundaries = []
    offset = 0
    for name, length in contig_lengths:
        bins_for_contig = contig_bin_counts[name]
        positions_per_bin = max(length / bins_for_contig, 1)
        geometry[name] = (offset, positions_per_bin)
        boundaries.append({"contig": name, "bin_start": offset})
        offset += bins_for_contig

    return geometry, boundaries, contig_bin_counts


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
    neighbour's bin or omitted from the plot entirely. When a fragmented
    assembly has more contigs than bins, that floor degrades instead of
    overflowing -- see `allocate_bins` for how contigs then share bins.

    `depth_lines` is consumed once, in the streaming order `samtools depth -a`
    produces (contig order, then position order) -- never materialized as a
    list, since a whole-genome depth file is one line per base.

    Returns `(bins, boundaries)`: `bins` is the mean depth per bin, and
    `boundaries` marks which bin index starts each contig, for drawing
    separators and axis labels.
    """
    geometry, boundaries, contig_bin_counts = allocate_bins(
        contig_lengths=contig_lengths, bin_count=bin_count
    )
    if not geometry:
        return [], []

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
        # `contig_bin_counts[contig]` can be 0 when contigs outnumber bins
        # (see allocate_bins): that contig shares its run's single bin
        # rather than owning one, so clamp against a span of at least 1
        # instead of letting the upper bound go negative and push idx
        # before the run's start_bin.
        span = max(contig_bin_counts[contig], 1)
        offset_in_contig = min(int((position - 1) / positions_per_bin), span - 1)
        idx = start_bin + offset_in_contig
        bin_sum[idx] += float(depth_str)
        bin_n[idx] += 1

    bins = [bin_sum[i] / bin_n[i] if bin_n[i] else 0.0 for i in range(bin_count)]
    return bins, boundaries


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


def coerce_tsv_value(column: str, value: str) -> int | float | str:
    """Turn a TSV cell back into its numeric type for the JSON pagination
    response, by column name -- the same typing contigs_tsv used to write it."""
    if column in _TSV_INT_COLUMNS:
        return int(value)
    if column in _TSV_FLOAT_COLUMNS:
        return float(value)
    return value
