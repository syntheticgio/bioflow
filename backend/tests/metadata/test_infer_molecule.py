"""FASTQ base-composition sampling for the manual molecule-type inference button.

Never automatic -- this only runs when a user clicks Infer. See
docs/superpowers/specs/2026-08-10-molecule-type-library-source-design.md for why:
most RNA-seq reads as T (reverse-transcribed to cDNA before sequencing), so
"no U found" is DNA by elimination, not by positive evidence.
"""

import gzip
from pathlib import Path

import pytest

from app.metadata.infer_molecule import infer_molecule_type


def _write_fastq(path: Path, records: list[tuple[str, str]]) -> None:
    """records is a list of (header_suffix, sequence) pairs."""
    lines = []
    for i, (header, seq) in enumerate(records):
        lines.append(f"@read{i} {header}")
        lines.append(seq)
        lines.append("+")
        lines.append("I" * len(seq))
    path.write_text("\n".join(lines) + "\n")


class TestBaseComposition:
    def test_all_t_sequence_is_dna(self, tmp_path):
        fq = tmp_path / "reads.fastq"
        _write_fastq(fq, [("", "ACGTACGTACGT")] * 10)
        result = infer_molecule_type(fq)
        assert result["molecule_type"] == "DNA"
        assert "no U found" in result["basis"]

    def test_sequence_with_u_is_rna(self, tmp_path):
        fq = tmp_path / "reads.fastq"
        _write_fastq(fq, [("", "ACGUACGUACGU")] * 10)
        result = infer_molecule_type(fq)
        assert result["molecule_type"] == "RNA"
        assert "U present" in result["basis"]

    def test_lowercase_u_counts(self, tmp_path):
        fq = tmp_path / "reads.fastq"
        _write_fastq(fq, [("", "acguacgu")])
        result = infer_molecule_type(fq)
        assert result["molecule_type"] == "RNA"

    def test_u_in_a_later_sampled_record_is_still_found(self, tmp_path):
        fq = tmp_path / "reads.fastq"
        records = [("", "ACGTACGT")] * 50 + [("", "ACGUACGU")]
        _write_fastq(fq, records)
        result = infer_molecule_type(fq, sample_reads=51)
        assert result["molecule_type"] == "RNA"


class TestGzipTransparency:
    def test_gzipped_all_t_is_dna(self, tmp_path):
        fq = tmp_path / "reads.fastq.gz"
        content = "@r1\nACGTACGT\n+\nIIIIIIII\n"
        with gzip.open(fq, "wt") as fh:
            fh.write(content)
        result = infer_molecule_type(fq)
        assert result["molecule_type"] == "DNA"

    def test_gzipped_with_u_is_rna(self, tmp_path):
        fq = tmp_path / "reads.fastq.gz"
        content = "@r1\nACGUACGU\n+\nIIIIIIII\n"
        with gzip.open(fq, "wt") as fh:
            fh.write(content)
        result = infer_molecule_type(fq)
        assert result["molecule_type"] == "RNA"


class TestSamplingBound:
    def test_only_samples_requested_number_of_reads(self, tmp_path):
        """A U past the sample window must not be found -- otherwise
        sample_reads is not actually bounding the read."""
        fq = tmp_path / "reads.fastq"
        records = [("", "ACGTACGT")] * 5 + [("", "ACGUACGU")]
        _write_fastq(fq, records)
        result = infer_molecule_type(fq, sample_reads=5)
        assert result["molecule_type"] == "DNA"


class TestEdgeCases:
    def test_empty_file_returns_none_molecule_type(self, tmp_path):
        fq = tmp_path / "empty.fastq"
        fq.write_text("")
        result = infer_molecule_type(fq)
        assert result["molecule_type"] is None
        assert "no sequence" in result["basis"].lower()

    def test_truncated_file_with_no_full_record_returns_none(self, tmp_path):
        fq = tmp_path / "truncated.fastq"
        fq.write_text("@r1\n")  # header only, no sequence line
        result = infer_molecule_type(fq)
        assert result["molecule_type"] is None

    def test_does_not_raise_on_malformed_file(self, tmp_path):
        fq = tmp_path / "garbage.fastq"
        fq.write_bytes(b"\x00\x01\x02not a fastq at all")
        result = infer_molecule_type(fq)
        assert result["molecule_type"] is None
