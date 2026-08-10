"""Transcript-model parsing and RNA-seq QC accumulation.

Pure functions over strings and lists, mirroring bam_stats_runner.py: no
queue, no filesystem, no pysam objects.
"""

import pytest

from app.pipelines.transcript_qc_runner import (
    GENE_BODY_BINS,
    MIN_TRANSCRIPT_LENGTH,
    Transcript,
    _attribute,
    parse_gtf_transcripts,
    representative_transcripts,
    sampling_plan,
)
from app.pipelines.transcript_qc_runner import (  # noqa: E402
    FeatureCounts,
    GeneBodyCoverage,
    build_feature_index,
    classify_position,
    contig_overlap,
)

# Two transcripts of one gene, plus a minus-strand gene on another contig.
GTF = "\n".join(
    [
        '#!genome-build GRCh38',
        'chr1\tx\texon\t101\t200\t.\t+\t.\tgene_id "G1"; transcript_id "T1";',
        'chr1\tx\texon\t301\t400\t.\t+\t.\tgene_id "G1"; transcript_id "T1";',
        'chr1\tx\texon\t101\t150\t.\t+\t.\tgene_id "G1"; transcript_id "T2";',
        'chr2\tx\texon\t1001\t1300\t.\t-\t.\tgene_id "G2"; transcript_id "T3";',
        'chr1\tx\tCDS\t101\t200\t.\t+\t.\tgene_id "G1"; transcript_id "T1";',
    ]
)


class TestParseGtf:
    def test_groups_exons_by_transcript(self):
        ts = parse_gtf_transcripts(GTF.splitlines())
        assert {t.transcript_id for t in ts} == {"T1", "T2", "T3"}
        t1 = next(t for t in ts if t.transcript_id == "T1")
        assert t1.exons == [(101, 200), (301, 400)]
        assert t1.contig == "chr1"
        assert t1.strand == "+"
        assert t1.gene_id == "G1"

    def test_ignores_non_exon_features(self):
        """Counting a CDS as well as its exon would double-count those bases."""
        ts = parse_gtf_transcripts(GTF.splitlines())
        t1 = next(t for t in ts if t.transcript_id == "T1")
        assert len(t1.exons) == 2

    def test_skips_comments_and_blank_lines(self):
        ts = parse_gtf_transcripts(["", "# comment", "#!genome-build x"])
        assert ts == []

    def test_records_strand(self):
        ts = parse_gtf_transcripts(GTF.splitlines())
        assert next(t for t in ts if t.transcript_id == "T3").strand == "-"

    def test_transcript_length_is_summed_exon_length(self):
        ts = parse_gtf_transcripts(GTF.splitlines())
        t1 = next(t for t in ts if t.transcript_id == "T1")
        assert t1.length == 200  # 100 + 100


class TestAttribute:
    def test_does_not_match_a_key_that_is_only_a_prefix(self):
        """gene_id_2 must not satisfy a query for gene_id -- a real GTF can
        carry both, and prefix-matching would silently return the wrong
        transcript's/gene's id."""
        assert _attribute('gene_id_2 "WRONG"; gene_id "G1";', "gene_id") == "G1"

    def test_returns_none_when_the_key_is_absent(self):
        assert _attribute('gene_biotype "protein_coding";', "gene_id") is None


class TestTranscriptSpan:
    def test_span_is_correct_even_if_exons_are_constructed_out_of_order(self):
        """__post_init__ sorts on construction so this invariant can't be
        silently violated by a caller that doesn't go through
        parse_gtf_transcripts (e.g. a hand-built test fixture)."""
        t = Transcript(
            transcript_id="T1", gene_id="G1", contig="chr1", strand="+",
            exons=[(301, 400), (101, 200)],
        )
        assert t.span == (101, 400)


class TestRepresentativeTranscripts:
    def test_picks_the_longest_transcript_per_gene(self):
        """Averaging over isoforms blurs the 3' signal the chart exists to
        show: isoforms differ in length, so their positions do not align."""
        ts = representative_transcripts(parse_gtf_transcripts(GTF.splitlines()))
        by_gene = {t.gene_id: t.transcript_id for t in ts}
        assert by_gene["G1"] == "T1"  # 200 bp beats T2's 50 bp

    def test_drops_transcripts_below_the_length_floor(self):
        """Normalizing a very short transcript into 100 bins produces noise,
        not signal."""
        short = "\n".join(
            [
                f'c\tx\texon\t1\t{MIN_TRANSCRIPT_LENGTH - 1}\t.\t+\t.\t'
                'gene_id "S"; transcript_id "S1";'
            ]
        )
        assert representative_transcripts(parse_gtf_transcripts(short.splitlines())) == []


def _t(tid, contig, strand, exons, gene=None):
    return Transcript(
        transcript_id=tid, gene_id=gene or tid, contig=contig, strand=strand, exons=exons
    )


class TestGeneBodyCoverage:
    def test_uniform_coverage_gives_a_flat_curve(self):
        t = _t("T1", "chr1", "+", [(1, 1000)])
        g = GeneBodyCoverage()
        for pos in range(1, 1001):
            g.add_read(t, pos)
        curve = [p["coverage"] for p in g.to_facts()]
        assert max(curve) == 1.0
        assert min(curve) > 0.9  # flat within binning noise

    def test_three_prime_pileup_rises_toward_the_end(self):
        """The degraded-RNA signature: poly-A selection keeps only the 3'
        tail, so coverage climbs from 5' to 3'."""
        t = _t("T1", "chr1", "+", [(1, 1000)])
        g = GeneBodyCoverage()
        for pos in range(900, 1001):
            g.add_read(t, pos)
        curve = [p["coverage"] for p in g.to_facts()]
        assert curve[0] == 0.0
        assert curve[-1] == 1.0

    def test_minus_strand_transcripts_are_oriented_five_to_three(self):
        """A minus-strand transcript's 5' end is its *highest* coordinate.
        Skipping this inverts half the genes, and averaging the two opposing
        gradients flattens the curve into meaninglessness."""
        plus = _t("P", "chr1", "+", [(1, 1000)])
        minus = _t("M", "chr1", "-", [(1, 1000)])

        gp = GeneBodyCoverage()
        for pos in range(900, 1001):  # 3' end of a plus-strand transcript
            gp.add_read(plus, pos)

        gm = GeneBodyCoverage()
        for pos in range(1, 102):  # 3' end of a minus-strand transcript
            gm.add_read(minus, pos)

        assert [p["coverage"] for p in gp.to_facts()] == [
            p["coverage"] for p in gm.to_facts()
        ]

    def test_spliced_transcripts_measure_transcript_position_not_genomic(self):
        """Two exons with a large intron: a read at the start of exon 2 is at
        the transcript's midpoint, not at 90% of its genomic span."""
        t = _t("T1", "chr1", "+", [(1, 500), (9501, 10000)])
        g = GeneBodyCoverage()
        g.add_read(t, 9501)
        curve = [p["coverage"] for p in g.to_facts()]
        assert curve[50] == 1.0
        assert curve[90] == 0.0

    def test_curve_is_normalized_to_its_maximum(self):
        t = _t("T1", "chr1", "+", [(1, 1000)])
        g = GeneBodyCoverage()
        for pos in range(1, 1001):
            g.add_read(t, pos)
        assert max(p["coverage"] for p in g.to_facts()) == 1.0

    def test_emits_one_point_per_bin(self):
        t = _t("T1", "chr1", "+", [(1, 1000)])
        g = GeneBodyCoverage()
        g.add_read(t, 1)
        facts = g.to_facts()
        assert len(facts) == GENE_BODY_BINS
        assert facts[0]["percentile"] == 0
        assert facts[-1]["percentile"] == 99

    def test_no_reads_gives_an_empty_curve(self):
        assert GeneBodyCoverage().to_facts() == []


class TestFeatureClassification:
    TS = [
        _t("T1", "chr1", "+", [(101, 200), (301, 400)]),
        _t("T2", "chr2", "-", [(1001, 1100)]),
    ]

    def test_a_read_inside_an_exon_is_exonic(self):
        idx = build_feature_index(self.TS)
        assert classify_position(idx, "chr1", 150) == "exonic"

    def test_a_read_between_exons_of_one_gene_is_intronic(self):
        idx = build_feature_index(self.TS)
        assert classify_position(idx, "chr1", 250) == "intronic"

    def test_a_read_outside_every_gene_is_intergenic(self):
        idx = build_feature_index(self.TS)
        assert classify_position(idx, "chr1", 5000) == "intergenic"

    def test_a_read_on_an_unknown_contig_is_intergenic(self):
        idx = build_feature_index(self.TS)
        assert classify_position(idx, "chrUnplaced", 5) == "intergenic"

    def test_exonic_wins_when_a_position_is_both(self):
        """Overlapping genes on opposite strands are common. Exonic winning
        keeps the three categories mutually exclusive so they sum to the
        classified total."""
        overlapping = [
            _t("A", "chr1", "+", [(100, 200), (400, 500)], gene="GA"),
            _t("B", "chr1", "-", [(250, 260)], gene="GB"),
        ]
        idx = build_feature_index(overlapping)
        # 250 is inside GA's intron and inside GB's exon.
        assert classify_position(idx, "chr1", 250) == "exonic"

    def test_counts_sum_to_the_classified_total(self):
        c = FeatureCounts()
        c.add("exonic")
        c.add("intronic")
        c.add("intergenic")
        c.add("exonic")
        facts = c.to_facts()
        assert facts == {"exonic": 2, "intronic": 1, "intergenic": 1}
        assert sum(facts.values()) == c.total

    def test_multiple_transcripts_of_one_gene_merge_into_one_span(self):
        """build_feature_index can receive more than one transcript per gene
        (representative_transcripts already dedupes for the real caller, but
        this function doesn't assume that) -- the merged span must cover the
        union of both transcripts' extents, not just the last one seen."""
        two_transcripts_one_gene = [
            _t("T1", "chr1", "+", [(100, 200)], gene="G1"),
            _t("T2", "chr1", "+", [(1000, 1100)], gene="G1"),
        ]
        idx = build_feature_index(two_transcripts_one_gene)
        # A position between the two transcripts' spans is inside neither
        # transcript's own exons, but IS inside the gene's merged span.
        assert classify_position(idx, "chr1", 500) == "intronic"
        # A position before the first transcript's span is outside the gene
        # entirely.
        assert classify_position(idx, "chr1", 50) == "intergenic"


class TestCovers:
    def test_a_position_inside_a_very_long_interval_is_covered(self):
        """A gene/exon spanning more than any fixed distance bound must
        still be found -- the previous scan-back cutoff silently missed
        this exact shape."""
        from app.pipelines.transcript_qc_runner import _covers

        intervals = [(0, 5_000_000), (100, 200)]
        assert _covers(intervals, 4_500_000) is True

    def test_a_position_just_past_the_end_of_a_long_interval_is_not_covered(self):
        from app.pipelines.transcript_qc_runner import _covers

        intervals = [(0, 5_000_000)]
        assert _covers(intervals, 5_000_001) is False


class TestContigOverlap:
    def test_matching_names_overlap(self):
        assert contig_overlap({"chr1", "chr2"}, {"chr1", "chr3"}) == 1

    def test_ensembl_style_names_do_not_match_ucsc_style(self):
        """'1' vs 'chr1' is the classic silent failure: every read lands
        outside every gene and the result is a plausible-looking 100%
        intergenic with no error anywhere."""
        assert contig_overlap({"1", "2"}, {"chr1", "chr2"}) == 0


class TestSamplingPlan:
    def test_short_contigs_still_get_at_least_one_read(self):
        """A contig whose proportional share rounds to zero must not be
        silently skipped -- a genuine gene-bearing scaffold in a non-model
        genome could otherwise contribute nothing to either chart with no
        signal anywhere that it happened."""
        plan = sampling_plan(
            contig_lengths=[("chr1", 250_000_000), ("tiny_scaffold", 500)],
            budget=200_000,
        )
        by_contig = dict(plan)
        assert by_contig["tiny_scaffold"] >= 1

    def test_shares_are_roughly_proportional_to_length(self):
        plan = sampling_plan(
            contig_lengths=[("big", 900_000), ("small", 100_000)],
            budget=1000,
        )
        by_contig = dict(plan)
        assert by_contig["big"] > by_contig["small"]

    def test_zero_total_length_falls_back_to_the_first_contig(self):
        plan = sampling_plan(contig_lengths=[("c1", 0), ("c2", 0)], budget=500)
        assert plan == [("c1", 500)]
