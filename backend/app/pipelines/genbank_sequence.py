"""Extraction of a GenBank record's ORIGIN block into FASTA.

The inverse of `genbank_reader`, and deliberately a separate module. That
reader guarantees it never accumulates sequence, and `build_annotation_db`
depends on the guarantee; this module exists to do the one thing that
guarantee forbids. Two passes with opposite priorities are easier to reason
about than one pass with a mode flag.

Memory here is bounded the same way: a sequence line is written to the output
handle as it is read, so a 300MB ORIGIN block never becomes a 300MB string.
"""

import gzip
from pathlib import Path
from typing import TextIO

from app.pipelines.genbank_reader import accession_for

# FASTA convention, and what NCBI emits.
_WRAP = 60


def sequence_line_bases(line: str) -> str:
    """The bases on one ORIGIN line.

    A line is a right-aligned base counter followed by up to six
    space-separated 10-base blocks:

        1 agcttttcat tctgactgca acgggcaata

    Dropping the leading numeric token and removing whitespace recovers the
    sequence. A line with no counter is read as all bases, since the counter
    is a convenience for human readers rather than something to rely on.
    """
    parts = line.split()
    if not parts:
        return ""
    if parts[0].isdigit():
        parts = parts[1:]
    return "".join(parts)


def _open_text(path: Path) -> TextIO:
    """Gzip-aware line reader.

    Sniffed by magic bytes rather than extension, matching
    `genbank_reader._open_text`: a file downloaded from NCBI is gzipped
    whether or not whoever renamed it kept the suffix.
    """
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


class _WrappedWriter:
    """Writes bases at a fixed column width without buffering the record.

    The carry is at most `_WRAP` characters, which is what keeps this
    module's memory flat: a 300MB ORIGIN block is written as it is read
    rather than assembled into a 300MB string first.
    """

    def __init__(self, fh: TextIO):
        self._fh = fh
        self._carry = ""

    def write(self, bases: str) -> None:
        chunk = self._carry + bases
        cut = len(chunk) - (len(chunk) % _WRAP)
        for i in range(0, cut, _WRAP):
            self._fh.write(chunk[i : i + _WRAP] + "\n")
        self._carry = chunk[cut:]

    def finish(self) -> None:
        """Flush the trailing partial line, if any."""
        if self._carry:
            self._fh.write(self._carry + "\n")
            self._carry = ""


def write_fasta(*, source: Path, dest: Path) -> int:
    """Write every ORIGIN block in `source` to `dest` as FASTA.

    Returns the number of records written, which is not the number of records
    in the file: a record with no ORIGIN block contributes nothing. A caller
    that needs "this file had no sequence at all" checks for a zero return.

    One pass, streaming both ways. The feature block is skipped rather than
    parsed -- that is `genbank_reader`'s job, and doing it again here would
    make this module the second place that has to be right about qualifiers.
    """
    written = 0
    version = accession = locus_name = ""
    in_origin = False
    writer: _WrappedWriter | None = None

    with _open_text(source) as fh, open(dest, "w") as out:
        for raw in fh:
            line = raw.rstrip("\n")

            if line.startswith("LOCUS"):
                if writer is not None:
                    writer.finish()
                    writer = None
                in_origin = False
                version = accession = locus_name = ""
                parts = line.split()
                if len(parts) > 1:
                    locus_name = parts[1]
                continue

            if line.startswith("//"):
                if writer is not None:
                    writer.finish()
                    writer = None
                in_origin = False
                continue

            if line.startswith("ORIGIN"):
                in_origin = True
                name = accession_for(
                    version=version, accession=accession, locus_name=locus_name
                )
                out.write(f">{name}\n")
                writer = _WrappedWriter(out)
                written += 1
                continue

            if in_origin:
                if writer is not None:
                    writer.write(sequence_line_bases(line))
                continue

            if line.startswith("VERSION"):
                parts = line.split()
                if len(parts) > 1:
                    version = parts[1]
                continue

            if line.startswith("ACCESSION"):
                parts = line.split()
                if len(parts) > 1:
                    accession = parts[1]
                continue

        if writer is not None:
            writer.finish()

    return written
