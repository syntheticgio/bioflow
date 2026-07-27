"""Matching paired-end read files by filename convention."""

import pytest

from app.pipelines import pairing


class TestSplitMate:
    @pytest.mark.parametrize(
        ("name", "key", "mate"),
        [
            ("sample_R1.fastq.gz", "sample", "R1"),
            ("sample_R2.fastq.gz", "sample", "R2"),
            ("sample_r1.fastq", "sample", "R1"),
            ("sample.R1.fastq.gz", "sample", "R1"),
            ("SRR123_1.fastq.gz", "SRR123", "R1"),
            ("SRR123_2.fq", "SRR123", "R2"),
            ("sample_R1.fq.bz2", "sample", "R1"),
        ],
    )
    def test_recognizes_the_common_conventions(self, name, key, mate):
        assert pairing.split_mate(name)[:2] == (key, mate)

    @pytest.mark.parametrize(
        "name",
        [
            "reads.fastq.gz",
            "reference.fasta",
            "sample_unpaired.fastq",
            "notes.txt",
        ],
    )
    def test_returns_none_without_a_mate_token(self, name):
        assert pairing.split_mate(name) is None

    def test_prefers_the_longer_token(self):
        """`_R1` and `_1` both match the end of `sample_R1`; taking `_1` would
        leave a key of `sample_R` and pair it with nothing."""
        assert pairing.split_mate("sample_R1.fastq.gz")[:2] == ("sample", "R1")

    def test_only_a_trailing_token_counts(self):
        """The case that makes anchoring necessary: this is an R1 whose name
        happens to end in `_2`. Matching the token anywhere would call it an R2
        and pair it with the wrong file."""
        key, mate, _ = pairing.split_mate("sample_R1_run_2.fastq.gz")
        assert mate == "R2"
        assert key == "sample_R1_run"

    def test_a_name_that_is_only_a_token_has_no_key(self):
        assert pairing.split_mate("_R1.fastq.gz")[:2] == ("", "R1")

    def test_strips_a_processing_marker(self):
        """`output_name` inserts `.trimmed` between the mate token and the
        suffixes, which would otherwise leave the token mid-stem."""
        assert pairing.split_mate("sample_R1.trimmed.fastq.gz")[:2] == ("sample", "R1")


class TestIsMateOf:
    def test_matches_opposite_halves(self):
        assert pairing.is_mate_of("sample_R1.fastq.gz", "sample_R2.fastq.gz")
        assert pairing.is_mate_of("sample_R2.fastq.gz", "sample_R1.fastq.gz")

    def test_rejects_the_same_half(self):
        """Two R1s from different runs are not a pair."""
        assert not pairing.is_mate_of("sample_R1.fastq.gz", "sample_R1.fastq.gz")

    def test_rejects_different_samples(self):
        assert not pairing.is_mate_of("alpha_R1.fastq.gz", "beta_R2.fastq.gz")

    def test_rejects_an_unpaired_file(self):
        assert not pairing.is_mate_of("sample_R1.fastq.gz", "reads.fastq.gz")

    def test_matches_across_compression_and_extension(self):
        """A pair whose halves were stored differently is still a pair."""
        assert pairing.is_mate_of("sample_R1.fastq.gz", "sample_R2.fq")

    def test_matches_across_case_in_the_key(self):
        assert pairing.is_mate_of("Sample_R1.fastq.gz", "sample_R2.fastq.gz")

    def test_does_not_match_across_differing_conventions(self):
        """`sample_R1` and `sample_2` reduce to the same key but come from
        different naming schemes; treating them as a pair is a guess too far
        when the launch dialog can simply ask."""
        assert not pairing.is_mate_of("sample_R1.fastq.gz", "sample_2.fastq.gz")

    def test_an_empty_key_never_matches(self):
        """`_R1.fastq` and `_R2.fastq` carry no identity beyond the token, so
        pairing them would be matching on nothing."""
        assert not pairing.is_mate_of("_R1.fastq.gz", "_R2.fastq.gz")

    def test_a_trailing_token_coincidence_does_not_create_a_false_pair(self):
        """`sample_R1_run_2` parses as R2 with key `sample_R1_run`; it must not
        pair with the real `sample_R1`."""
        assert not pairing.is_mate_of("sample_R1_run_2.fastq.gz", "sample_R1.fastq.gz")


class TestConvenienceAccessors:
    def test_pairing_key(self):
        assert pairing.pairing_key("sample_R1.fastq.gz") == "sample"
        assert pairing.pairing_key("reads.fastq.gz") is None

    def test_mate_of(self):
        assert pairing.mate_of("sample_R2.fastq.gz") == "R2"
        assert pairing.mate_of("reads.fastq.gz") is None

    def test_both_halves_share_a_key(self):
        """The property the database lookup relies on."""
        assert pairing.pairing_key("sample_R1.fastq.gz") == pairing.pairing_key(
            "sample_R2.fastq.gz"
        )

    def test_trimmed_output_still_pairs(self):
        """`output_name` inserts `.trimmed` before the suffixes, which must not
        break the pairing of the files it produces."""
        from app.pipelines.fastp_runner import output_name

        r1 = output_name("sample_R1.fastq.gz")
        r2 = output_name("sample_R2.fastq.gz")
        assert pairing.is_mate_of(r1, r2)
