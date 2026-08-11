"""Meryl k-mer spectra and repeat-density command builders and parsers.

Pure functions only: no I/O, no subprocess, no database. The handler in
`app.queue.assembly_qc_handlers` does the running; this module decides what
to run and what the output means, which is what makes both testable without
a tool installed.

`build_meryl_count_command` lives in `merqury_runner.py` and is reused
directly. This module adds statistics, histogram parsing, genome-size
estimation, and per-window repeat-density binning."""

from __future__ import annotations

from pathlib import Path

from app.storage.parsers import MAX_STORED_CONTIGS

# Reused rather than duplicated: the same windowing scheme gc_tracks.py
# fixes for the Circos plot, because #177 adds a ring to that same plot.
from app.pipelines.gc_tracks import MIN_WINDOW_BASES, WINDOW_COUNT

# A k-mer that appears more than 3 times in the assembly is considered
# repetitive. This is a low threshold because meryl counts at k=21: a
# 21-mer appearing 4+ times in a ~5 Mb bacterial genome is genuinely
# repetitive, not a statistical fluke.
REPEAT_DENSITY_THRESHOLD = 3


def build_meryl_statistics_command(
    *,
    meryl_path: str,
    database: Path,
) -> list[str]:
    """`meryl statistics <db>` — print k-mer frequency histogram to stdout."""
    return [meryl_path, "statistics", str(database)]


def build_meryl_print_gt_command(
    *,
    meryl_path: str,
    database: Path,
    threshold: int = REPEAT_DENSITY_THRESHOLD,
) -> list[str]:
    """`meryl print greater-than <N> <db>` — high-frequency k-mers with positions."""
    return [meryl_path, "print", f"greater-than={threshold}", str(database)]


def parse_meryl_histogram(text: str) -> list[list[int]]:
    """Parse `meryl statistics` output.

    Tab-separated, no header::

        1   12345678
        2   8901234
        3   4567890

    Returns a list of ``[frequency, count]`` pairs. Blank lines are skipped.
    """
    result: list[list[int]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        try:
            result.append([int(fields[0]), int(fields[1])])
        except (ValueError, IndexError):
            continue
    return result


def compute_genome_size(
    histogram: list[list[int]],
    *,
    k: int = 21,
) -> dict:
    """Estimate genome size and heterozygosity from a k-mer histogram.

    Under the simple model: genome_size ≈ total_kmers / peak_coverage.
    For a homozygous diploid (peak at 2×), the estimate is halved.

    Heterozygosity is detected from a bimodal distribution: the distance
    between the first (heterozygous) peak and the second (homozygous) peak
    yields the heterozygosity rate. When unimodal, heterozygosity is None.

    Returns a dict with keys present: total_kmers, distinct_kmers,
    and optionally genome_size_est and heterozygosity.
    """
    if not histogram:
        return {"total_kmers": 0, "distinct_kmers": 0}

    total_kmers = sum(freq * count for freq, count in histogram)
    distinct_kmers = sum(count for _, count in histogram)

    if total_kmers == 0 or len(histogram) < 2:
        return {"total_kmers": total_kmers, "distinct_kmers": distinct_kmers}

    # Find peaks: local maxima in the smoothed frequency distribution.
    frequencies = [f for f, _ in histogram]
    counts = [c for _, c in histogram]
    max_count = max(counts)

    # A peak must be at least 5% of the maximum count — below that it is
    # noise, not a real k-mer coverage peak.
    peak_threshold = int(max_count * 0.05)
    peaks: list[int] = []
    for i in range(1, len(counts) - 1):
        if counts[i] > peak_threshold and counts[i] >= counts[i - 1] and counts[i] >= counts[i + 1]:
            peaks.append(frequencies[i])

    if not peaks:
        # No clear peak — can't estimate.
        return {"total_kmers": total_kmers, "distinct_kmers": distinct_kmers}

    result: dict = {"total_kmers": total_kmers, "distinct_kmers": distinct_kmers}

    # If bimodal with roughly 1:2 frequency ratio, report heterozygosity.
    # The first peak (lower coverage) is heterozygous k-mers; the second
    # (higher coverage) is homozygous.
    if len(peaks) >= 2:
        low_peak = peaks[0]
        high_peak = peaks[-1]
        if high_peak > low_peak and abs(high_peak - 2 * low_peak) <= max(1, low_peak // 2):
            result["heterozygosity"] = round(1.0 / low_peak, 4)
            result["genome_size_est"] = total_kmers // high_peak
            return result

    # Unimodal: use the highest peak for coverage estimate.
    main_peak = max(peaks, key=lambda p: counts[frequencies.index(p)])
    result["genome_size_est"] = total_kmers // main_peak
    result["heterozygosity"] = None
    return result


def compute_repeat_density(
    lines: list[str],
    contig_lengths: dict[str, int],
    *,
    threshold: int = REPEAT_DENSITY_THRESHOLD,
    window_count: int = WINDOW_COUNT,
) -> dict:
    """Bin high-frequency k-mer hits into per-window density tracks.

    `lines` is the output of ``meryl print greater-than <N>``::

        >contig_1:1000-1021 AAAAAAAAAAAAAAAAAAAAA
        >contig_1:2500-2521 TTTTTTTTTTTTTTTTTTTTT

    Only the contig name and start position are parsed; the k-mer sequence is
    discarded. Each hit is binned into its window by ``pos // window_width``.

    `contig_lengths` is the ``sequence_lengths`` fact on the assembly —
    names mapped to base-pair lengths. Contigs that appear in lengths but
    have zero k-mer hits get ``null`` windows.

    Returns a dict matching #151's ``gc_tracks`` shape with ``density`` and
    ``count`` parallel arrays, plus ``repeat_density_partial`` when contigs
    were truncated at ``MAX_STORED_CONTIGS``.
    """
    # Build per-contig hit counts by window.
    from collections import defaultdict

    hit_bins: dict[str, list[int]] = {}
    for line in lines:
        line = line.strip()
        if not line or not line.startswith(">"):
            continue
        # Parse ">contig:pos-len" from the FASTA header meryl emits.
        header = line[1:].split()[0]  # strip '>' and k-mer sequence
        if ":" not in header:
            continue
        contig, rest = header.split(":", 1)
        pos_str = rest.split("-")[0] if "-" in rest else rest
        try:
            pos = int(pos_str)
        except ValueError:
            continue
        if contig not in hit_bins:
            hit_bins[contig] = []
        hit_bins[contig].append(pos)

    # Resolve per contig.
    resolved: list[dict] = []
    for name, total_length in contig_lengths.items():
        n_windows = min(window_count, total_length // MIN_WINDOW_BASES)
        if n_windows == 0:
            continue
        window_bases = total_length // n_windows
        if window_bases == 0:
            continue

        hits_list = hit_bins.get(name, [])
        if not hits_list:
            resolved.append({
                "name": name,
                "length": total_length,
                "window_bases": window_bases,
                "density": [None] * n_windows,
                "count": [None] * n_windows,
            })
            continue

        # Contig has hits: count per window.
        kmer_counts = [0] * n_windows  # type: list[int]
        for pos in hits_list:
            wi = min(pos // window_bases, n_windows - 1)
            kmer_counts[wi] += 1

        density: list[float | None] = []
        count_out: list[int | None] = []
        for wi in range(n_windows):
            c = kmer_counts[wi]
            count_out.append(c)
            if c > 0:
                density.append(round(c / max(1, (window_bases - 21 + 1)), 4))
            else:
                density.append(0.0)

        resolved.append({
            "name": name,
            "length": total_length,
            "window_bases": window_bases,
            "density": density,
            "count": count_out,
        })

    if not resolved:
        return {}

    # Keep longest contigs.
    resolved.sort(key=lambda c: c["length"], reverse=True)
    partial = len(resolved) > MAX_STORED_CONTIGS
    if partial:
        resolved = resolved[:MAX_STORED_CONTIGS]

    result: dict = {
        "k": 21,
        "threshold": threshold,
        "window_count": window_count,
        "contigs": resolved,
    }
    if partial:
        result["repeat_density_partial"] = True
    return result
