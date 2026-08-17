"""Counting parameters, and the two that fail silently when wrong.

Most of this file is about `strandedness_for_align_params` and
`attributes_for_format`. Neither is complicated; both are here because getting
them wrong produces a counts file that is structurally perfect and
biologically meaningless, which no downstream check can detect.
"""

import pytest

from app.errors import ValidationError
from app.models import FormatKind
from app.pipelines import counts_runner


class TestStrandednessFromAlignment:
    """HISAT2's --rna-strandness to featureCounts' -s.

    A mismatch here does not error. featureCounts attributes reads to the
    opposite strand and returns near-zero counts throughout, which reads as a
    failed experiment rather than a wrong flag -- so the mapping is pinned in
    both directions rather than trusted.
    """

    @pytest.mark.parametrize(
        ("rna_strandness", "expected"),
        [
            ("F", counts_runner.FORWARD),
            ("FR", counts_runner.FORWARD),
            ("R", counts_runner.REVERSE),
            ("RF", counts_runner.REVERSE),
            # Case is not guaranteed: the value round-trips through a payload
            # and a form.
            ("rf", counts_runner.REVERSE),
        ],
    )
    def test_each_orientation_maps_to_its_flag(self, rna_strandness, expected):
        assert (
            counts_runner.strandedness_for_align_params(
                {"rna_strandness": rna_strandness}
            )
            == expected
        )

    def test_the_paired_and_single_spellings_agree(self):
        """FR and F describe the same orientation; -s does not distinguish
        them, because `-p` is what tells featureCounts there are two mates."""
        strand = counts_runner.strandedness_for_align_params
        assert strand({"rna_strandness": "FR"}) == strand({"rna_strandness": "F"})
        assert strand({"rna_strandness": "RF"}) == strand({"rna_strandness": "R"})

    @pytest.mark.parametrize("params", [None, {}, {"rna_strandness": ""}])
    def test_nothing_recorded_is_none_not_unstranded(self, params):
        """None means "nothing to infer", which the caller turns into a
        default it can label as a guess. Returning UNSTRANDED here instead
        would make a stranded library silently count as unstranded with the
        dialog reporting it as derived from the alignment."""
        assert counts_runner.strandedness_for_align_params(params) is None

    def test_a_non_hisat2_alignment_infers_nothing(self):
        """STAR and bwa-mem2 record no rna_strandness at all."""
        assert (
            counts_runner.strandedness_for_align_params(
                {"aligner": "star", "two_pass": False}
            )
            is None
        )

    def test_an_unrecognised_value_infers_nothing(self):
        assert (
            counts_runner.strandedness_for_align_params({"rna_strandness": "??"})
            is None
        )


class TestAnnotationAttributes:
    """The GTF/GFF3 split.

    Checked against the files NCBI Datasets actually ships. On
    GCF_000146045.2 the GFF3's exon lines carry `locus_tag` on all 6852 of
    them, `gene` on 5790, and `gene_id` on none -- so the conventional
    `-g gene_id` stops with "failed to find the gene identifier attribute",
    and `-g gene` would silently drop the ~15% of features never given a name.
    """

    def test_gtf_gets_the_conventional_pair(self):
        assert counts_runner.attributes_for_format(FormatKind.GTF) == (
            "exon",
            "gene_id",
        )

    def test_gff3_groups_on_locus_tag(self):
        assert counts_runner.attributes_for_format(FormatKind.GFF) == (
            "exon",
            "locus_tag",
        )

    def test_gff3_does_not_group_on_gene_id(self):
        """The direction that matters: `gene_id` is absent from every GFF3
        exon line, so choosing it is not a worse default but a hard failure."""
        _, attribute = counts_runner.attributes_for_format(FormatKind.GFF)
        assert attribute != "gene_id"

    def test_a_string_kind_works_like_the_enum(self):
        """Facts round-trip through JSON, so the caller may hold either."""
        assert counts_runner.attributes_for_format("gff") == ("exon", "locus_tag")

    @pytest.mark.parametrize("kind", [None, "", "unknown", FormatKind.BAM])
    def test_an_unknown_format_falls_back_to_gtf(self, kind):
        """GTF's pair is what featureCounts itself assumes and what any
        documentation a user reads will show, so it is the least surprising
        answer for a file we cannot classify."""
        assert counts_runner.attributes_for_format(kind) == ("exon", "gene_id")


class TestPairedFromFacts:
    def test_a_positive_count_is_paired(self):
        assert counts_runner.paired_from_facts({"properly_paired_reads": 1885414})

    def test_zero_is_ambiguous_rather_than_single_end(self):
        """flagstat prints this line for every BAM and prints 0 for a
        single-end one -- but also for a paired run where nothing aligned
        concordantly. The caller falls back to the alignment's own inputs
        rather than guessing from an answer that cannot distinguish them."""
        assert counts_runner.paired_from_facts({"properly_paired_reads": 0}) is None

    @pytest.mark.parametrize("facts", [None, {}, {"total_reads": 100}])
    def test_never_measured_is_none(self, facts):
        assert counts_runner.paired_from_facts(facts) is None


class TestCommandConstruction:
    def _params(self, **kw):
        return counts_runner.CountsParams(**kw)

    def test_paired_passes_both_flags_not_just_p(self):
        """`-p` alone is the 1.x meaning -- "the input is paired-end" -- and
        still counts reads. `--countReadPairs` is what switches the unit to
        fragments. Passing only `-p` doubles every count on paired data.

        Verified against real output: a BAM of 2,176,214 reads counted
        1,088,107 fragments, exactly half.
        """
        cmd = counts_runner.build_command(
            bam="a.bam",
            annotation="a.gtf",
            output="out.tsv",
            params=self._params(paired=True),
        )
        assert "-p" in cmd
        assert "--countReadPairs" in cmd

    def test_single_end_passes_neither(self):
        cmd = counts_runner.build_command(
            bam="a.bam",
            annotation="a.gtf",
            output="out.tsv",
            params=self._params(paired=False),
        )
        assert "-p" not in cmd
        assert "--countReadPairs" not in cmd

    def test_strandedness_reaches_the_command(self):
        cmd = counts_runner.build_command(
            bam="a.bam",
            annotation="a.gtf",
            output="out.tsv",
            params=self._params(strandedness=counts_runner.REVERSE),
        )
        assert cmd[cmd.index("-s") + 1] == "2"

    def test_multi_mapping_is_off_unless_asked(self):
        cmd = counts_runner.build_command(
            bam="a.bam", annotation="a.gtf", output="o.tsv", params=self._params()
        )
        assert "-M" not in cmd

    def test_the_bam_is_last(self):
        """featureCounts takes input files positionally after the flags."""
        cmd = counts_runner.build_command(
            bam="reads.bam",
            annotation="a.gtf",
            output="o.tsv",
            params=self._params(),
        )
        assert cmd[-1] == "reads.bam"


class TestParamValidation:
    def test_an_out_of_range_strandedness_is_refused(self):
        with pytest.raises(ValidationError):
            counts_runner.CountsParams.from_dict({"strandedness": 7})

    def test_zero_threads_is_refused(self):
        with pytest.raises(ValidationError):
            counts_runner.CountsParams.from_dict({"threads": 0})

    def test_defaults_are_unstranded_single_end_gtf(self):
        p = counts_runner.CountsParams.from_dict({})
        assert (p.strandedness, p.paired, p.attribute) == (
            counts_runner.UNSTRANDED,
            False,
            "gene_id",
        )


class TestOutputParsing:
    COUNTS = (
        "# Program:featureCounts v2.0.8\n"
        "Geneid\tChr\tStart\tEnd\tStrand\tLength\tsample.bam\n"
        "YAL068C\tNC_001133.9\t1807\t2169\t-\t363\t42\n"
        "YAL067W\tNC_001133.9\t2480\t2707\t+\t228\t0\n"
        "YAL066W\tNC_001133.9\t7235\t9016\t+\t1782\t7\n"
    )

    def test_counts_are_read_from_the_last_column(self):
        counts, _ = counts_runner.parse_counts(self.COUNTS)
        assert counts == {"YAL068C": 42, "YAL067W": 0, "YAL066W": 7}

    def test_the_comment_and_header_lines_are_skipped(self):
        counts, _ = counts_runner.parse_counts(self.COUNTS)
        assert "Geneid" not in counts
        assert not any(g.startswith("#") for g in counts)

    def test_genes_detected_excludes_zero_count_genes(self):
        """The signal that separates "this sample is bad" from "this
        parameter is wrong": a strandedness error flattens detection while
        leaving the annotation's gene count untouched."""
        _, facts = counts_runner.parse_counts(self.COUNTS)
        assert facts["genes_in_annotation"] == 3
        assert facts["genes_detected"] == 2
        assert facts["counted_fragments"] == 49

    def test_summary_reports_the_assignment_rate(self):
        summary = (
            "Status\tsample.bam\n"
            "Assigned\t733174\n"
            "Unassigned_NoFeatures\t128697\n"
            "Unassigned_Ambiguity\t36656\n"
        )
        facts = counts_runner.parse_summary(summary)
        assert facts["assigned_fragments"] == 733174
        assert facts["assigned_pct"] == pytest.approx(81.6, abs=0.1)

    def test_an_empty_summary_yields_no_facts_rather_than_a_zero_rate(self):
        """A rate of 0% and "no summary was written" mean different things,
        and only one of them should light up the low-assignment warning."""
        assert counts_runner.parse_summary("") == {}
