"""Streaming GenBank records off disk.

The property under test that is not visible in the parser: a record's ORIGIN
block is stepped over line by line and never accumulated, so a file whose
bulk is sequence costs no more memory than one without it.
"""

from pathlib import Path

from app.pipelines.genbank_reader import iter_records

FIXTURES = Path(__file__).parent.parent / "fixtures" / "genbank"


class TestIterRecords:
    def test_splits_on_record_terminator(self):
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert len(records) == 2

    def test_accession_prefers_version(self):
        # VERSION, not ACCESSION or LOCUS: the versioned accession is what
        # NCBI's paired FASTA uses in its deflines, so the two agree.
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert records[0].accession == "NC_000001.3"
        assert records[1].accession == "NC_000002.1"

    def test_length_from_locus_line(self):
        # This is what lets coverage work with no paired reference.
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert records[0].length == 2000
        assert records[1].length == 900

    def test_reports_sequence_presence_per_record(self):
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert records[0].has_sequence is True
        assert records[1].has_sequence is False

    def test_origin_lines_are_not_retained(self):
        # The ORIGIN block must never reach feature_lines -- if it did, a
        # 300 MB record would cost 300 MB of RSS.
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        joined = "\n".join(records[0].feature_lines)
        assert "agcttttcat" not in joined

    def test_feature_lines_exclude_the_features_header(self):
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert not records[0].feature_lines[0].startswith("FEATURES")
        assert "source" in records[0].feature_lines[0]

    def test_source_is_captured(self):
        records = list(iter_records(FIXTURES / "two_records.gbff"))
        assert "Escherichia coli" in records[0].source

    def test_gzipped_file_reads_identically(self, tmp_path):
        import gzip
        raw = (FIXTURES / "two_records.gbff").read_bytes()
        gz = tmp_path / "two_records.gbff.gz"
        gz.write_bytes(gzip.compress(raw))
        assert len(list(iter_records(gz))) == 2

    def test_truncated_final_record_is_still_emitted(self, tmp_path):
        # Downloads get truncated. Emit what was parsed rather than losing
        # the whole record for a missing terminator.
        text = (FIXTURES / "two_records.gbff").read_text()
        truncated = text[: text.rindex("//")]
        path = tmp_path / "truncated.gbff"
        path.write_text(truncated)
        assert len(list(iter_records(path))) == 2

    def test_record_without_features_block(self, tmp_path):
        # Valid GenBank. Contributes a contig length and zero features.
        path = tmp_path / "nofeat.gbff"
        path.write_text(
            "LOCUS       NC_9    500 bp    DNA     linear   CON 09-MAR-2022\n"
            "VERSION     NC_9.1\n"
            "//\n"
        )
        records = list(iter_records(path))
        assert len(records) == 1
        assert records[0].length == 500
        assert records[0].feature_lines == []
