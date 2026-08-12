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


class TestRealNcbiFile:
    """Against a real NCBI record, not a hand-built one.

    A fixture written by the parser's own author tends to look the way the
    parser expects. This one was not.
    """

    def test_parses_without_error(self):
        records = list(iter_records(FIXTURES / "ecoli_slice.gbff"))
        assert len(records) == 1
        assert records[0].accession.startswith("NC_000913")

    def test_finds_features(self):
        from app.pipelines.genbank_parse import iter_features

        record = next(iter(iter_records(FIXTURES / "ecoli_slice.gbff")))
        rows = list(iter_features(record.feature_lines, accession=record.accession))
        # 83 rows (parents + segments) as of this fixture's annotation date
        # (LOCUS line: 09-DEC-2025). A loose lower bound would still pass if
        # the parser silently dropped half the real features on some
        # real-world qualifier or location shape the hand-built fixtures
        # never exercised -- exactly the failure this task exists to catch.
        # Some headroom for benign RefSeq annotation-release drift, none for
        # a parser regression.
        assert len(rows) >= 75
        assert any(r.type == "CDS" for r in rows)
        assert any(r.name == "thrA" for r in rows)

    def test_every_feature_id_is_unique(self):
        # The property the whole parent/child scheme rests on. A collision
        # would attach children to the wrong parent silently.
        from app.pipelines.genbank_parse import iter_features

        record = next(iter(iter_records(FIXTURES / "ecoli_slice.gbff")))
        ids = [
            r.feature_id
            for r in iter_features(record.feature_lines, accession=record.accession)
        ]
        assert len(ids) == len(set(ids))

    def test_gzipped_fixture_matches_plain(self):
        plain = list(iter_records(FIXTURES / "two_records.gbff"))
        gzipped = list(iter_records(FIXTURES / "two_records.gbff.gz"))
        assert [r.accession for r in plain] == [r.accession for r in gzipped]
        assert [r.length for r in plain] == [r.length for r in gzipped]
