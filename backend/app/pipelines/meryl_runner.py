"""Meryl k-mer spectra and repeat-density command builders and parsers.

No subprocess, no database. The handler in `app.queue.assembly_qc_handlers`
does the running; this module decides what to run and what the output means,
which is what makes both testable without a tool installed. One exception to
"no I/O": `iter_fasta_contigs` streams the assembly FASTA, mirroring
`gc_tracks.compute_gc_tracks` — meryl databases store no genomic positions,
so locating repeats means scanning the assembly ourselves (#612).

`build_meryl_count_command` lives in `merqury_runner.py` and is reused
directly. This module adds statistics, histogram parsing, genome-size
estimation, and per-window repeat-density binning."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from pathlib import Path

from app.errors import JobCancelled

# Reused rather than duplicated: the same windowing scheme gc_tracks.py
# fixes for the Circos plot, because #177 adds a ring to that same plot.
from app.pipelines.gc_tracks import MIN_WINDOW_BASES, WINDOW_COUNT
from app.storage.parsers import MAX_STORED_CONTIGS

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
    """`meryl print greater-than <N> <db>` — high-frequency k-mers.

    Two tokens for the filter, not ``greater-than=N``: meryl 1.4.2 misparses
    the one-token form as ``opLessThan`` (verified live against the worker
    image) and prints nothing.
    """
    return [meryl_path, "print", "greater-than", str(threshold), str(database)]


def parse_meryl_histogram(text: str) -> list[list[int]]:
    """Parse the frequency histogram out of `meryl statistics` output.

    Real meryl 1.4.2 output is a prose preamble followed by a space-aligned
    five-column table (fixture ``meryl-1.4.2-statistics.log``)::

        Number of 21-mers that are:
          unique                   8773  (exactly one instance ...)
          ...
        frequency        kmers     distinct        total       (1e-6)
        --------- ------------ ------------ ------------ ------------
                1         8773       0.9951       0.9813   111.856823

    A histogram row is any line whose first two whitespace-separated fields
    are both integers — the preamble lines all start with a word, so they
    self-select out. Returns ``[frequency, count]`` pairs.
    """
    result: list[list[int]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            result.append([int(fields[0]), int(fields[1])])
        except ValueError:
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


_KMER_CHARS = frozenset("ACGT")
_COMPLEMENT = str.maketrans("ACGT", "TGCA")


def parse_meryl_print_kmers(text: str) -> set[str]:
    """Parse ``meryl print greater-than <N>`` output into a k-mer set.

    Real output is ``KMER\\tCOUNT`` lines (fixture
    ``meryl-1.4.2-print-greater-than.log``), interleaved with banner lines
    ("Found 1 command tree.", "PROCESSING TREE ...") when stderr is merged
    into the same stream, as `run_subprocess` does. A k-mer line is any line
    whose first field is pure ACGT and whose second is an integer.

    Meryl databases store no genomic positions — only the sequences and
    their counts — so this set is the input to a scan of the assembly
    itself (`compute_repeat_density`), not a list of locations.
    """
    kmers: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        kmer = fields[0].upper()
        if not kmer or (set(kmer) - _KMER_CHARS):
            continue
        try:
            int(fields[1])
        except ValueError:
            continue
        kmers.add(kmer)
    return kmers


def expand_kmer_orientations(kmers: set[str]) -> set[str]:
    """Add the reverse complement of every k-mer.

    Meryl counts canonical k-mers (the lexicographically smaller of a k-mer
    and its reverse complement), so a repeat can sit on either strand of the
    assembly. Expanding the set once lets the scan test plain forward
    membership instead of canonicalizing at every position.
    """
    return kmers | {kmer.translate(_COMPLEMENT)[::-1] for kmer in kmers}


def iter_fasta_contigs(path: Path) -> Iterator[tuple[str, str]]:
    """Stream ``(name, uppercased sequence)`` per contig from a FASTA.

    Sniffs gzip by magic bytes rather than trusting a payload field, so it
    works for any job payload vintage. Buffers one contig at a time, like
    `gc_tracks.compute_gc_tracks`. An unreadable file yields nothing —
    the caller's empty-result path already handles that.
    """
    import gzip

    try:
        with open(path, "rb") as raw:
            is_gzip = raw.read(2) == b"\x1f\x8b"
        opener = gzip.open if is_gzip else open
        with opener(path, "rt", errors="replace") as fh:
            name: str | None = None
            buf: list[str] = []
            for line in fh:
                stripped = line.rstrip("\n\r")
                if stripped.startswith(">"):
                    if name is not None:
                        yield name, "".join(buf).upper()
                    name = stripped[1:].split()[0] if len(stripped) > 1 else ""
                    buf = []
                elif name is not None:
                    buf.append(stripped)
            if name is not None:
                yield name, "".join(buf).upper()
    except (OSError, EOFError, UnicodeDecodeError):
        return


def compute_repeat_density(
    contigs: Iterable[tuple[str, str]],
    repeat_kmers: set[str],
    *,
    k: int = 21,
    threshold: int = REPEAT_DENSITY_THRESHOLD,
    window_count: int = WINDOW_COUNT,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Scan contig sequences for repetitive k-mers, binned per window.

    `contigs` yields ``(name, sequence)`` — usually `iter_fasta_contigs`
    over the assembly. `repeat_kmers` is `parse_meryl_print_kmers`' set; both
    orientations are matched (see `expand_kmer_orientations`). Each position
    whose k-mer is in the set counts as one hit in its window; density is
    hits over k-mer positions per window. Windows with no hits are 0.0 — a
    real measurement, not missing data, now that the scan sees every contig.

    Returns a dict matching #151's ``gc_tracks`` shape with ``density`` and
    ``count`` parallel arrays, plus ``repeat_density_partial`` when contigs
    were truncated at ``MAX_STORED_CONTIGS``.
    """
    expanded = expand_kmer_orientations(repeat_kmers)

    resolved: list[dict] = []
    for name, seq in contigs:
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("repeat density scan cancelled")

        total_length = len(seq)
        n_windows = min(window_count, total_length // MIN_WINDOW_BASES)
        if n_windows == 0:
            continue
        window_bases = total_length // n_windows

        kmer_counts = [0] * n_windows
        if expanded and total_length >= k:
            for i in range(total_length - k + 1):
                if seq[i : i + k] in expanded:
                    wi = min(i // window_bases, n_windows - 1)
                    kmer_counts[wi] += 1

        positions_per_window = max(1, window_bases - k + 1)
        resolved.append({
            "name": name,
            "length": total_length,
            "window_bases": window_bases,
            "density": [round(c / positions_per_window, 4) for c in kmer_counts],
            "count": kmer_counts,
        })

    if not resolved:
        return {}

    # Keep longest contigs.
    resolved.sort(key=lambda c: c["length"], reverse=True)
    partial = len(resolved) > MAX_STORED_CONTIGS
    if partial:
        resolved = resolved[:MAX_STORED_CONTIGS]

    result: dict = {
        "k": k,
        "threshold": threshold,
        "window_count": window_count,
        "contigs": resolved,
    }
    if partial:
        result["repeat_density_partial"] = True
    return result
