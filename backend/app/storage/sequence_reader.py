"""Read one protein record's sequence from a FASTA file by byte offset.

The byte_offset points at the '>' character of the record's header line.
The record extends to the next '>' or EOF. Sequence lines are concatenated
with newlines stripped (including \r for CRLF files).
"""
from pathlib import Path


def read_protein_sequence(path: Path, byte_offset: int, length: int) -> str:
    """Return the amino-acid sequence of one protein record.
    
    Args:
        path: Path to the FASTA file.
        byte_offset: Byte offset of the '>' character (0-based).
        length: Number of amino acids (ProteinRecord.length).
    
    Returns:
        The concatenated sequence lines with whitespace stripped.
    """
    with open(path, "rb") as f:
        f.seek(byte_offset)
        lines = []
        for line in f:
            if line.startswith(b">") and lines:
                # We've hit the next record
                break
            # Strip \n, \r\n, and any trailing whitespace
            lines.append(line.decode("utf-8", errors="replace").strip())
    
    # First line is the header (starts with >) — remove it
    if lines and lines[0].startswith(">"):
        lines.pop(0)
    
    return "".join(lines)
