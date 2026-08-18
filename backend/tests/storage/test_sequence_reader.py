"""Test reading a protein record's sequence from a FASTA file by byte offset."""
import tempfile
from pathlib import Path
from app.storage.sequence_reader import read_protein_sequence


def test_read_first_record():
    """Read the first record of a two-record FASTA."""
    fasta = ">sp|P00924|ENO1_YEAST\nMVLSPADKTNVKAAW\nGKVGAHAGEYGAEALER\n>sp|P00925|ENO2_YEAST\nSEQUENCE2\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".fasta", delete=False) as f:
        f.write(fasta)
        path = f.name
    try:
        seq = read_protein_sequence(Path(path), byte_offset=0, length=3)
        assert seq == "MVLSPADKTNVKAAWGKVGAHAGEYGAEALER", f"Got: {seq}"
    finally:
        Path(path).unlink(missing_ok=True)


def test_read_last_record():
    """Read the last record — no trailing newline after sequence."""
    fasta = ">sp|P00924|ENO1_YEAST\nMVLSPADKTNVKAAW\n>sp|P00925|ENO2_YEAST\nSEQUENCE2"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".fasta", delete=False) as f:
        f.write(fasta)
        path = f.name
    try:
        seq = read_protein_sequence(
            Path(path),
            byte_offset=len(">sp|P00924|ENO1_YEAST\nMVLSPADKTNVKAAW\n"),
            length=2,
        )
        assert seq == "SEQUENCE2", f"Got: {seq}"
    finally:
        Path(path).unlink(missing_ok=True)


def test_single_record():
    """A single-record FASTA with trailing newline."""
    fasta = ">test\nACDEFGHIKLMNPQRSTVWY\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".fasta", delete=False) as f:
        f.write(fasta)
        path = f.name
    try:
        seq = read_protein_sequence(Path(path), byte_offset=0, length=1)
        assert seq == "ACDEFGHIKLMNPQRSTVWY", f"Got: {seq}"
    finally:
        Path(path).unlink(missing_ok=True)


def test_crlf_line_endings():
    """CRLF line endings must not produce stray \\r characters."""
    fasta = ">test\r\nACDEF\r\nGHIKL\r\n"
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".fasta", delete=False) as f:
        f.write(fasta.encode("utf-8"))
        path = f.name
    try:
        seq = read_protein_sequence(Path(path), byte_offset=0, length=1)
        assert seq == "ACDEFGHIKL", f"Got: {repr(seq)}"
    finally:
        Path(path).unlink(missing_ok=True)
