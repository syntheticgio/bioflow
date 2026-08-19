"""Per-read length and quality distributions, binned out of NanoPlot's raw TSV.

NanoPlot computes a length and a mean quality for every read on its way to the
HTML report and then throws them away; `--raw` is the flag that makes it write
them out instead, as a gzipped two-column TSV beside the report. Adding the
flag is a smaller change than a second pass over the FASTQ in Python, which
would re-read a file NanoPlot has already read once and duplicate its parsing.

The TSV itself is not kept. On a multi-million-read ONT run it is tens of
megabytes of per-read rows whose only consumer is this module, so the caller
bins it during the job and deletes it -- the churn is transient rather than
sitting in every object's report directory forever.

Two facts come out of the one pass, because the expensive part is the pass:

- **A base-weighted length histogram.** The Y axis is total bases per length
  bin, not reads per bin. For assembly the question is never "how many reads
  are longer than 20 kb" but "what fraction of my sequence data is in reads
  that long", because that is what decides whether a repeat gets spanned. An
  ONT run's read *count* is dominated by short reads while its *bases* are
  dominated by long ones, so the two histograms of the same file look nothing
  alike and only the base-weighted one predicts contiguity. N50 is the scalar
  version of this number; the histogram is the distribution behind it.
- **A length-versus-quality density grid.** The 2D view separates failure
  modes no average can: a cloud of short bad reads dragging the mean down
  versus a uniformly mediocre run, or a HiFi run with a CLR-like second
  population at lower quality.

Both are binned rather than stored per read. A run is millions of reads and
Mongo's document cap is 16 MB; a fixed grid keeps each fact bounded regardless
of run size, the same reasoning `gc_tracks.py` uses for its 500 windows per
contig.

Length bins are log-spaced. Long-read lengths span orders of magnitude -- a
run reaching from 200 bp to 100 kb puts every bin but the last few against the
axis on a linear scale, which is the same reason `NxChart.tsx` uses a log Y
axis.
"""

import gzip
import math
import zlib
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger()

# Bins per factor of ten of read length. Six is fine enough to show a
# short-read spike sitting beside a long tail (the shape the base-weighted
# histogram exists to reveal) without producing a bin so narrow that a
# moderate run leaves visible gaps in it.
BINS_PER_DECADE = 6

# The length axis covers 100 bp to 1 Mb. Below the floor is not a long read by
# any definition, above the ceiling is past the longest reads any current
# platform produces; reads outside the range are clamped into the end bins
# rather than dropped, so the summed bases still reconcile with the
# `qc_total_bases` fact.
MIN_LENGTH = 100
MAX_LENGTH = 1_000_000

# Quality axis for the density grid. Q0-Q50 in 1-unit steps covers ONT simplex
# (Q10-Q20), duplex and PacBio HiFi (Q20-Q40) with room past the top; a value
# outside is clamped the same way lengths are.
MIN_QUALITY = 0
MAX_QUALITY = 50
QUALITY_BINS = 50

_LOG_MIN = math.log10(MIN_LENGTH)
_LOG_MAX = math.log10(MAX_LENGTH)
LENGTH_BINS = round((_LOG_MAX - _LOG_MIN) * BINS_PER_DECADE)


@dataclass
class RawColumns:
    """Which TSV column holds what.

    Resolved from the header row rather than assumed by position: NanoPlot
    currently writes `quals` then `lengths`, but the order is not part of any
    documented contract and a silently swapped pair would produce a plausible
    chart of nonsense -- reads a few tens of thousands of "quality" units long.
    """

    length: int
    quality: int | None


def _resolve_columns(header: str) -> RawColumns | None:
    """Locate the length and quality columns, or None if there is no length.

    Quality is optional. A FASTA input has no qualities, and NanoPlot omits
    the column entirely rather than writing nulls; the length histogram is
    still worth having in that case, so only a missing *length* column is
    fatal to the parse.
    """
    fields = [f.strip().lower() for f in header.rstrip("\n").split("\t")]
    try:
        length = fields.index("lengths")
    except ValueError:
        return None
    quality = fields.index("quals") if "quals" in fields else None
    return RawColumns(length=length, quality=quality)


def length_bin_index(length: float) -> int:
    """Which log-spaced bin a read length falls in, clamped to the axis."""
    if length <= MIN_LENGTH:
        return 0
    if length >= MAX_LENGTH:
        return LENGTH_BINS - 1
    idx = int((math.log10(length) - _LOG_MIN) * BINS_PER_DECADE)
    # A length landing exactly on the top edge would index one past the end.
    return min(idx, LENGTH_BINS - 1)


def length_bin_start(index: int) -> int:
    """The lower edge, in bp, of a log-spaced length bin."""
    return round(10 ** (_LOG_MIN + index / BINS_PER_DECADE))


def quality_bin_index(quality: float) -> int:
    """Which quality bin a mean read quality falls in, clamped to the axis."""
    if quality <= MIN_QUALITY:
        return 0
    if quality >= MAX_QUALITY:
        return QUALITY_BINS - 1
    return min(int(quality), QUALITY_BINS - 1)


def bin_raw_reads(raw_tsv: Path) -> dict:
    """Bin NanoPlot's per-read TSV into the two distribution facts.

    Streams the file a line at a time: the whole point of binning during the
    job is that the per-read data never has to be held in memory or in Mongo,
    and reading it into a list first would give back the memory cost this
    avoids.

    An unreadable or malformed file costs the charts and nothing else -- the
    scalar NanoPlot stats are parsed separately and the HTML report is already
    written by the time this runs, so returning {} degrades to exactly the QC
    output that existed before this fact did.
    """
    if not raw_tsv.exists():
        log.warning("nanoplot_raw_missing", path=str(raw_tsv))
        return {}

    length_bases = [0] * LENGTH_BINS
    length_reads = [0] * LENGTH_BINS
    # Sparse: an ONT run occupies a small fraction of the 50x50 grid, and the
    # empty cells cost document size for nothing.
    density: dict[tuple[int, int], int] = {}
    total_reads = 0
    total_bases = 0
    skipped = 0

    try:
        with gzip.open(raw_tsv, "rt") as fh:
            header = fh.readline()
            columns = _resolve_columns(header)
            if columns is None:
                log.warning(
                    "nanoplot_raw_unrecognised_header", header=header.strip()[:200]
                )
                return {}

            needed = max(
                columns.length, columns.quality if columns.quality is not None else 0
            )
            for line in fh:
                fields = line.rstrip("\n").split("\t")
                if len(fields) <= needed:
                    skipped += 1
                    continue
                try:
                    length = int(float(fields[columns.length]))
                except ValueError:
                    skipped += 1
                    continue
                if length <= 0:
                    skipped += 1
                    continue

                idx = length_bin_index(length)
                length_bases[idx] += length
                length_reads[idx] += 1
                total_reads += 1
                total_bases += length

                if columns.quality is not None:
                    try:
                        quality = float(fields[columns.quality])
                    except ValueError:
                        continue
                    key = (idx, quality_bin_index(quality))
                    density[key] = density.get(key, 0) + 1
    # zlib.error covers a stream that starts as valid gzip and then is not --
    # a TSV half-written by a job the reaper killed, which gzip only discovers
    # partway through rather than at open().
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as e:
        log.warning("nanoplot_raw_unreadable", path=str(raw_tsv), error=str(e))
        return {}

    if not total_reads:
        log.warning("nanoplot_raw_empty", path=str(raw_tsv))
        return {}

    if skipped:
        log.warning("nanoplot_raw_rows_skipped", path=str(raw_tsv), skipped=skipped)

    facts: dict = {
        "qc_length_bases_histogram": {
            "bins_per_decade": BINS_PER_DECADE,
            "min_length": MIN_LENGTH,
            # Only the occupied bins are emitted. A run whose reads all sit
            # between 1 kb and 30 kb would otherwise carry a long run of
            # zeroes at each end that the chart draws as nothing anyway.
            "bins": [
                {
                    "length_bin": length_bin_start(i),
                    "length_bin_end": length_bin_start(i + 1),
                    "bases": length_bases[i],
                    "reads": length_reads[i],
                }
                for i in range(LENGTH_BINS)
                if length_bases[i]
            ],
            "total_bases": total_bases,
            "total_reads": total_reads,
        }
    }

    if density:
        facts["qc_length_quality_density"] = {
            "bins_per_decade": BINS_PER_DECADE,
            "min_length": MIN_LENGTH,
            "quality_bins": QUALITY_BINS,
            # [length_bin_start, quality_bin, read_count] triples rather than
            # named keys: this is the one fact with thousands of entries, and
            # three keys repeated per cell is most of its document size.
            "cells": [
                [length_bin_start(li), qi, n]
                for (li, qi), n in sorted(density.items())
            ],
            "max_count": max(density.values()),
            "total_reads": total_reads,
        }

    return facts
