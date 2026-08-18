#!/usr/bin/env python3
"""Generate the reads-path fixtures: a tiny reference and reads drawn from it.

Small enough to upload quickly (well under MAX_SIMPLE_UPLOAD_BYTES), large
enough that fastp/minimap2 do not immediately reject them.

The reads are **exact substrings of the reference**, with an adapter appended
to R1 so trimming has something real to remove. That matters for the align
step: reads of random sequence upload and pass QC perfectly well, but map at
roughly zero, so an alignment assertion over them can only check that a BAM
exists -- which passes just as happily when the aligner is pointed at the
wrong index. Drawing the reads from the reference is what lets the test
assert a *mapping rate* and have that number mean something. Same reasoning
as the pytest live-data tier's use of phiX substrings.

The reference is synthetic rather than a real accession so the fixture stays
self-contained and offline: nothing here should need a network fetch.
"""

from __future__ import annotations

import gzip
import random
from pathlib import Path

REF_LEN = 20_000
REF_NAME = "e2e_ref"
READS = 200
LEN = 150
QUAL = "I"

# The Illumina stem. Appended to R1 only, so the trim step has a real adapter
# to find and the before/after read lengths differ in a checkable way.
ADAPTER = "AGATCGGAAGAGCACACGTCTGAACTCCAGTCAC"

random.seed(7)

_COMPLEMENT = str.maketrans("ACGT", "TGCA")


def _reference() -> str:
    return "".join(random.choice("ACGT") for _ in range(REF_LEN))


def _revcomp(seq: str) -> str:
    return seq.translate(_COMPLEMENT)[::-1]


def write_reference(path: Path, sequence: str) -> None:
    with path.open("w") as f:
        f.write(f">{REF_NAME}\n")
        for i in range(0, len(sequence), 60):
            f.write(sequence[i : i + 60] + "\n")


def write_reads(r1: Path, r2: Path, sequence: str) -> None:
    """Paired reads: R1 forward from the reference, R2 its reverse complement.

    R2 is the reverse complement of a downstream window rather than another
    forward read, because that is the orientation real paired-end data has
    (FR), and an aligner reports the pair as properly paired only in that
    orientation.
    """
    starts = random.sample(range(0, len(sequence) - LEN - 400), READS)
    with gzip.open(r1, "wt") as f1, gzip.open(r2, "wt") as f2:
        for i, start in enumerate(starts):
            fwd = sequence[start : start + LEN]
            mate_start = start + 300
            rev = _revcomp(sequence[mate_start : mate_start + LEN])
            # The adapter goes on R1 only; read-through past a short insert is
            # what puts it there in real data, and one side is enough for the
            # trim step to have work to do.
            f1.write(f"@read{i}/1\n{fwd + ADAPTER}\n+\n{QUAL * (LEN + len(ADAPTER))}\n")
            f2.write(f"@read{i}/2\n{rev}\n+\n{QUAL * LEN}\n")


if __name__ == "__main__":
    out = Path(__file__).parent
    sequence = _reference()
    ref = out / "reference.fna"
    r1, r2 = out / "reads_R1.fastq.gz", out / "reads_R2.fastq.gz"
    write_reference(ref, sequence)
    write_reads(r1, r2, sequence)
    print(f"wrote {ref}, {r1} and {r2}")
