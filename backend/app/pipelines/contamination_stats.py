"""Adapter content and duplication levels from a whole-file FASTQ scan.

Unlike `storage/sequence_stats.py`, which samples 200k reads at ingest, this
reads the entire file. That is deliberate and is the reason the module exists
separately: FastQC's duplication correction extrapolates from the point where
its sequence dictionary froze to the file's *total* read count, so a sampled
`total_count` would extrapolate to the sample rather than to the library, and
report ">1k duplicates" meaning ">1k within a 200k window".

Runs inside the QC job, which the user has already opted into and which
already passes fastp over the same whole file.
"""

import gzip
import threading
from pathlib import Path

from app.errors import JobCancelled
from app.logging import get_logger
from app.models import Compression

log = get_logger(__name__)

# FastQC matches the first 12bp of each adapter rather than the whole thing:
# long enough to be specific, short enough to still match a read that ran off
# the end of the fragment with only part of the adapter present.
PROBE_LENGTH = 12

# The kits FastQC ships, plus the two homopolymer artifacts. PolyG is not
# optional decoration: on NovaSeq/NextSeq two-colour chemistry, *absence* of
# signal reads as G, so poly-G tails are among the most common artifacts in
# current data.
KNOWN_ADAPTERS: tuple[tuple[str, str], ...] = (
    ("Illumina Universal", "AGATCGGAAGAG"),
    ("Illumina Small RNA 3'", "TGGAATTCTCGG"),
    ("Illumina Small RNA 5'", "GATCGTCGGACT"),
    ("Nextera Transposase", "CTGTCTCTTATA"),
    ("PolyA", "AAAAAAAAAAAA"),
    ("PolyG", "GGGGGGGGGGGG"),
)


def build_probes(detected: list[str | None]) -> list[tuple[str, str]]:
    """The probe set for one file: the known kits plus whatever fastp found.

    The detected sequence is what the fixed list cannot supply -- a custom or
    unusual adapter still gets a curve. It is dropped when it duplicates a
    known kit, because two identical overlapping lines on the chart read as a
    rendering bug rather than as agreement.
    """
    probes = list(KNOWN_ADAPTERS)
    known = {seq for _, seq in KNOWN_ADAPTERS}

    for seq in detected:
        if not seq or len(seq) < PROBE_LENGTH:
            continue
        head = seq[:PROBE_LENGTH].upper()
        if head in known:
            continue
        probes.append(("Detected", head))
        known.add(head)

    return probes


def get_corrected_count(
    count_at_limit: int,
    total_count: int,
    duplication_level: int,
    number_of_observations: int,
) -> float:
    """Estimate how many sequences at this duplication level we never saw.

    Ported from FastQC's `DuplicationLevel.getCorrectedCount`. The dictionary
    stops accepting new sequences at 100k distinct entries, so a file larger
    than that contributes sequences we never recorded. This computes the
    probability of *not* having seen a sequence with this duplication level
    within the first `count_at_limit` reads, inverts it, and scales the
    observed count by the result.

    Both early exits are from the original and are not merely optimisations:
    they are the cases where the correction is provably 1.0.
    """
    # Nothing froze: every distinct sequence in the file is in the dictionary.
    if count_at_limit == total_count:
        return float(number_of_observations)

    # Not enough reads left to hide another sequence at this level.
    if total_count - number_of_observations < count_at_limit:
        return float(number_of_observations)

    # The probability below which correcting would not move the count by even
    # 0.01 of an observation. Past this point the corrected value is so close
    # to the observed one that continuing the loop buys nothing.
    limit_of_caring = 1.0 - (
        number_of_observations / (number_of_observations + 0.01)
    )

    # count_at_limit is always a snapshot of total_count taken no later than
    # the point total_count reaches its final value (DuplicationTracker),
    # so it can never exceed it; guard 1 has already ruled out equality here.
    # That keeps every (total_count - i) below strictly positive.
    assert count_at_limit <= total_count
    p_not_seeing = 1.0
    for i in range(count_at_limit):
        p_not_seeing *= (
            (total_count - i) - duplication_level
        ) / (total_count - i)
        if p_not_seeing < limit_of_caring:
            p_not_seeing = 0.0
            break

    p_seeing = 1.0 - p_not_seeing
    if p_seeing == 0.0:
        return float(number_of_observations)

    return number_of_observations / p_seeing


DUPLICATION_SLOTS = 16

DUPLICATION_LABELS: tuple[str, ...] = (
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
    ">10", ">50", ">100", ">500", ">1k", ">5k", ">10k",
)


def slot_for_level(duplication_level: int) -> int:
    """Which histogram slot a duplication level falls in.

    Binning is on `duplication_level - 1`, matching FastQC. That is why ">50"
    starts at level 51 rather than 50 -- a detail worth preserving, since the
    whole point of porting the algorithm is that the bins line up with the
    FastQC reports people compare against.
    """
    temp = duplication_level - 1

    # The negative guard is FastQC's, for duplication levels past 2^31.
    if temp > 9999 or temp < 0:
        return 15
    if temp > 4999:
        return 14
    if temp > 999:
        return 13
    if temp > 499:
        return 12
    if temp > 99:
        return 11
    if temp > 49:
        return 10
    if temp > 9:
        return 9
    return temp


# FastQC's limits, kept identical so the numbers line up with its reports.
# The dictionary caps at ~100k x 50 chars, so memory is bounded at roughly
# 10-15 MB no matter how large the file is.
OBSERVATION_CUTOFF = 100_000
DUPLICATION_SEQUENCE_LENGTH = 50


class DuplicationTracker:
    """Counts distinct read prefixes, freezing the dictionary at the cutoff.

    Freezing stops the dictionary growing; it does *not* stop the scan. Reads
    past the freeze still increment sequences already known and still count
    toward `total_count`, which is what makes the correction in `result()`
    an extrapolation to the whole library rather than to a sample.
    """

    def __init__(self) -> None:
        self.sequences: dict[str, int] = {}
        self.total_count = 0
        self.count_at_unique_limit = 0
        self._frozen = False

    def add(self, seq: str) -> None:
        self.total_count += 1
        key = seq[:DUPLICATION_SEQUENCE_LENGTH]

        if key in self.sequences:
            self.sequences[key] += 1
            if not self._frozen:
                self.count_at_unique_limit = self.total_count
            return

        if self._frozen:
            return

        self.sequences[key] = 1
        self.count_at_unique_limit = self.total_count
        if len(self.sequences) >= OBSERVATION_CUTOFF:
            self._frozen = True

    def result(self) -> dict:
        """The corrected histogram, as percentages of the library."""
        if not self.total_count:
            return {}

        # "How many distinct sequences were seen exactly N times."
        collated: dict[int, int] = {}
        for count in self.sequences.values():
            collated[count] = collated.get(count, 0) + 1

        percentages = [0.0] * DUPLICATION_SLOTS
        dedup_total = 0.0
        raw_total = 0.0

        for level, observations in collated.items():
            corrected = get_corrected_count(
                self.count_at_unique_limit,
                self.total_count,
                level,
                observations,
            )
            dedup_total += corrected
            raw_total += corrected * level
            percentages[slot_for_level(level)] += corrected * level

        if raw_total == 0:
            return {}

        percentages = [round(100.0 * p / raw_total, 4) for p in percentages]

        return {
            "labels": list(DUPLICATION_LABELS),
            "percentages": percentages,
            "percent_unique": round(100.0 * dedup_total / raw_total, 2),
            "total_reads": self.total_count,
        }


# Same cap and reasoning as `sequence_stats.MAX_POSITIONS`: the useful detail
# is at the start of the read, and an uncapped array would allocate megabytes
# per file on long-read input.
MAX_POSITIONS = 1_000


class AdapterTracker:
    """Per-position cumulative adapter matches, one counter row per probe."""

    def __init__(
        self,
        probes: list[tuple[str, str]],
        *,
        max_positions: int = MAX_POSITIONS,
    ) -> None:
        self.probes = probes
        self.max_positions = max_positions
        self.counts: dict[str, list[int]] = {
            name: [0] * max_positions for name, _ in probes
        }
        self.reads = 0
        self.longest_read = 0

    def add(self, seq: str) -> None:
        self.reads += 1
        self.longest_read = max(
            self.longest_read, min(len(seq), self.max_positions)
        )

        for name, probe in self.probes:
            index = seq.find(probe)
            if index < 0 or index >= self.max_positions:
                continue
            # Cumulative: a read that has entered adapter stays in adapter for
            # the rest of its length.
            row = self.counts[name]
            for pos in range(index, self.max_positions):
                row[pos] += 1

    def result(self) -> dict:
        if not self.reads or not self.longest_read:
            return {}

        width = self.longest_read
        return {
            "positions": list(range(1, width + 1)),
            "series": [
                {
                    "name": name,
                    "values": [
                        round(100.0 * c / self.reads, 4)
                        for c in self.counts[name][:width]
                    ],
                }
                for name, _ in self.probes
            ],
        }
