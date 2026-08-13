"""Extraction of a GenBank record's ORIGIN block into FASTA.

The inverse of `genbank_reader`, and deliberately a separate module. That
reader guarantees it never accumulates sequence, and `build_annotation_db`
depends on the guarantee; this module exists to do the one thing that
guarantee forbids. Two passes with opposite priorities are easier to reason
about than one pass with a mode flag.

Memory here is bounded the same way: a sequence line is written to the output
handle as it is read, so a 300MB ORIGIN block never becomes a 300MB string.
"""


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
