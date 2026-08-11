"""Manual, opt-in inference of molecule type from a FASTQ's own bases.

Never called from `enrich_from_sra`, `ingest_headers`, or any scheduled job --
only from the user-triggered `POST /{object_id}/infer-molecule-type` endpoint.
See docs/superpowers/specs/2026-08-10-molecule-type-library-source-design.md.

The signal: presence of `U` in sampled sequence lines means RNA (direct RNA
sequencing -- rare but unambiguous). Its absence defaults to DNA. This is a
real limitation, not an edge case -- most RNA-seq data is reverse-transcribed
to cDNA before sequencing and reads as `T`, identical to DNA, so "no U found"
is DNA by elimination, not by positive evidence. Callers surface `basis`
alongside the result so this doesn't read as more certain than it is.

Why the gzip-sniff helper below is duplicated rather than imported from
`app.pipelines.tile_scanner`: that module lives in a different layer
(pipeline stage vs. metadata inference), and importing across layers for one
seven-line function would create a dependency where none otherwise exists.
Duplicating the (tiny, stable) magic-number check keeps this module
self-contained, mirroring `detect_sequence_type` in `enrich.py`, which also
does its own file reading rather than reaching into `pipelines/`.
"""

import gzip
from pathlib import Path
from typing import IO


def _open_fastq(path: Path) -> IO[str]:
    """Open plain or gzipped FASTQ as text, by magic number rather than name."""
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


def infer_molecule_type(path: Path, *, sample_reads: int = 2000) -> dict:
    """Sample a FASTQ's sequence lines and report DNA or RNA by base content.

    Never raises for a well-formed or malformed FASTQ; returns
    {"molecule_type": None, "basis": "..."} if the file is empty or no
    sequence lines are found in the sampled region. Caller translates that
    into a 4xx/204 at the API layer -- this function only reads and classifies.
    """
    sequences_seen = 0
    found_u = False
    try:
        with _open_fastq(path) as fh:
            for i, line in enumerate(fh):
                if i % 4 != 1:
                    continue
                sequences_seen += 1
                if "u" in line.lower():
                    found_u = True
                    break
                if sequences_seen >= sample_reads:
                    break
    except OSError:
        return {"molecule_type": None, "basis": "file could not be opened"}

    if sequences_seen == 0:
        return {
            "molecule_type": None,
            "basis": "no sequence lines found in the sampled region",
        }

    if found_u:
        return {
            "molecule_type": "RNA",
            "basis": f"sampled {sequences_seen} reads, U present",
        }
    return {
        "molecule_type": "DNA",
        "basis": f"sampled {sequences_seen} reads, no U found",
    }
