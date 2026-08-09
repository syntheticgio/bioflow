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
