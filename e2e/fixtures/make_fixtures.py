#!/usr/bin/env python3
"""Generate tiny paired-end FASTQ fixtures for the reads-path test.

Small enough to upload quickly (well under MAX_SIMPLE_UPLOAD_BYTES), large
enough that fastp/minimap2 do not immediately reject them. Tune read count /
length here if a live QC run wants more.
"""

from __future__ import annotations

import gzip
import random
from pathlib import Path

READS = 200
LEN = 150
QUAL = "I"

random.seed(7)


def _seq() -> str:
    return "".join(random.choice("ACGT") for _ in range(LEN))


def write(path: Path) -> None:
    with gzip.open(path, "wt") as f:
        for i in range(READS):
            seq = _seq()
            f.write(f"@read{i}/1\n{seq}\n+\n{QUAL * LEN}\n")


if __name__ == "__main__":
    out = Path(__file__).parent
    r1, r2 = out / "reads_R1.fastq.gz", out / "reads_R2.fastq.gz"
    write(r1)
    write(r2)
    print(f"wrote {r1} and {r2}")
