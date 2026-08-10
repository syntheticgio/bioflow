"""Transcript-model parsing and RNA-seq QC accumulation.

Pure functions over strings and lists, mirroring bam_stats_runner.py: no
queue, no filesystem, no pysam objects.
"""

import pytest

from app.pipelines.transcript_qc_runner import (
    MIN_TRANSCRIPT_LENGTH,
    Transcript,
    _attribute,
    parse_gtf_transcripts,
    representative_transcripts,
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
