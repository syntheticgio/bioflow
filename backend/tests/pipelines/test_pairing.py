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


class TestVerdict:
    """Real strings pulled from the running database (see the spec). A
    hand-built fixture that put the same `length=` on both mates would pass an
    implementation that rejects every real pair -- these strings are the
    test's whole value."""

    def test_valid_mates_are_confirmed(self):
        """The case a naive whole-header compare fails: the `length=` field
        differs between real mates because the reads are different lengths."""
        a = pairing.PairInput(
            name="ERR17609896_1.fastq",
            facts={
                "first_read_ids": [
                    "ERR17609896.1 LH00201:115:22JLCTLT4:1:1101:22244:1112 length=150",
                ]
            },
        )
        b = pairing.PairInput(
            name="ERR17609896_2.fastq",
            facts={
                "first_read_ids": [
                    "ERR17609896.1 LH00201:115:22JLCTLT4:1:1101:22244:1112 length=149",
                ]
            },
        )
        assert pairing.verdict(a, b) == pairing.Verdict.CONFIRMED

    def test_collision_with_differing_run_fields_is_rejected(self):
        a = pairing.PairInput(
            name="foo_1.fastq",
            facts={"first_read_ids": ["ERR17609896.1 length=150"]},
        )
        b = pairing.PairInput(
            name="foo_2.fastq",
            facts={"first_read_ids": ["SRR39891651.1 1 length=2882"]},
        )
        assert pairing.verdict(a, b) == pairing.Verdict.REJECTED_READ_IDS

    def test_single_end_layout_is_rejected_regardless_of_read_ids(self):
        a = pairing.PairInput(name="foo_1.fastq", metadata={"read_type": "single-end"})
        b = pairing.PairInput(name="foo_2.fastq", metadata={"read_type": "single-end"})
        assert pairing.verdict(a, b) == pairing.Verdict.REJECTED_LAYOUT

    def test_derivative_trap_is_not_confirmed_by_name_alone(self):
        """A file and its own trimmed derivative have byte-identical first read
        IDs -- read ID equality alone cannot distinguish this from a real pair.
        Their names do not propose a pairing in the first place, which is the
        actual protection: `verdict()` never gets asked about this case."""
        a = pairing.PairInput(
            name="SRR39891651.fastq",
            facts={"first_read_ids": ["SRR39891651.1 1 length=2882"]},
        )
        b = pairing.PairInput(
            name="SRR39891651.trimmed.fastq",
            facts={"first_read_ids": ["SRR39891651.1 1 length=2882"]},
        )
        assert pairing.verdict(a, b) == pairing.Verdict.NO_MATCH

    def test_filter_offset_does_not_veto(self):
        """Trimming dropped the first 587 reads on one side; the first tokens
        differ (so this cannot be CONFIRMED) but the run field agrees (so it
        must not be REJECTED_READ_IDS either) -- it falls through to
        NAME_ONLY, today's behavior, rather than being treated as evidence of
        non-mateship."""
        a = pairing.PairInput(
            name="ERR16145610_1.fastq",
            facts={"first_read_ids": ["ERR16145610.1 00194bc7-... length=57"]},
        )
        b = pairing.PairInput(
            name="ERR16145610_2.fastq",
            facts={"first_read_ids": ["ERR16145610.588 966a917f-... length=128"]},
        )
        assert pairing.verdict(a, b) == pairing.Verdict.NAME_ONLY

    def test_both_signals_absent_falls_through_to_name_only(self):
        """The fast path: today's behavior, unchanged."""
        a = pairing.PairInput(name="sample_R1.fastq.gz")
        b = pairing.PairInput(name="sample_R2.fastq.gz")
        assert pairing.verdict(a, b) == pairing.Verdict.NAME_ONLY

    def test_names_that_do_not_match_are_no_match(self):
        a = pairing.PairInput(name="sample_R1.fastq.gz")
        b = pairing.PairInput(name="other.fastq.gz")
        assert pairing.verdict(a, b) == pairing.Verdict.NO_MATCH

    def test_layout_veto_runs_even_without_read_ids(self):
        a = pairing.PairInput(name="foo_1.fastq", metadata={"read_type": "single-end"})
        b = pairing.PairInput(name="foo_2.fastq")
        assert pairing.verdict(a, b) == pairing.Verdict.REJECTED_LAYOUT


class TestReadIdHelpers:
    def test_first_token_stops_at_whitespace(self):
        assert (
            pairing._first_token(
                "ERR17609896.1 LH00201:115:22JLCTLT4:1:1101:22244:1112 length=150"
            )
            == "ERR17609896.1"
        )

    def test_run_field_sra_style(self):
        assert pairing._run_field("ERR17609896.1 length=150") == "ERR17609896"

    def test_run_field_illumina_style(self):
        assert pairing._run_field("LH00201:115:22JLCTLT4:1:1101:22244:1112") == "LH00201"

    def test_read_ids_agree_on_shared_first_token(self):
        assert pairing.read_ids_agree(["ERR1.1 length=150"], ["ERR1.1 length=149"])

    def test_read_ids_conflict_on_differing_run_fields(self):
        assert pairing.read_ids_conflict(["ERR1.1"], ["SRR2.1"])

    def test_read_ids_do_not_conflict_on_shared_run_field(self):
        """The filter-offset case: differing first tokens, same run field."""
        assert not pairing.read_ids_conflict(["ERR1.1"], ["ERR1.588"])
