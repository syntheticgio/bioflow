"""Salmon's command construction, output parsing, and the tx2gene refusal.

The refusal is the reason most of this file exists. A transcript-to-gene map
that silently falls back to "each transcript is its own gene" produces a
counts file that merges cleanly, passes every downstream check, and tests a
gene universe nobody intended -- the same silent-success shape that cost STAR
its index sidecars.
"""

import pytest
from pathlib import Path

from app.errors import ValidationError
from app.pipelines import salmon_runner

# A real quant.sf header plus three rows. Columns are Name, Length,
# EffectiveLength, TPM, NumReads -- NumReads last, and fractional, which is
# the whole reason a summarization step exists.
QUANT_SF = """Name\tLength\tEffectiveLength\tTPM\tNumReads
tx1\t1500\t1350.0\t120.5\t340.7
tx2\t900\t750.0\t80.25\t112.3
tx3\t2000\t1850.0\t0.0\t0.0
"""


class TestParseQuant:
    def test_reads_num_reads_per_transcript(self):
        per_tx, _ = salmon_runner.parse_quant(QUANT_SF)
        assert per_tx == {"tx1": 340.7, "tx2": 112.3, "tx3": 0.0}

    def test_counts_detected_separately_from_total(self):
        _, facts = salmon_runner.parse_quant(QUANT_SF)
        assert facts["transcripts_in_index"] == 3
        # tx3 is in the index and got nothing. "Detected" is the signal that
        # separates a bad sample from a wrong reference; the total alone
        # cannot say which.
        assert facts["transcripts_detected"] == 2
        assert facts["estimated_reads"] == pytest.approx(453.0)

    def test_ignores_a_blank_trailing_line(self):
        per_tx, _ = salmon_runner.parse_quant(QUANT_SF + "\n")
        assert len(per_tx) == 3

    def test_empty_table_is_not_an_error_here(self):
        # A header with no rows is a real Salmon output for an empty input.
        # The handler decides whether that is a failure; the parser does not.
        per_tx, facts = salmon_runner.parse_quant(
            "Name\tLength\tEffectiveLength\tTPM\tNumReads\n"
        )
        assert per_tx == {}
        assert facts["transcripts_in_index"] == 0


# Real NCBI CDS deflines, verbatim from GCF_000146045.2 (S. cerevisiae R64) --
# fetched and confirmed in this plan's defline-verification task rather than
# recalled or synthesized. Full-file counts on that download: 6027 total
# sequences, 100% carry locus_tag=, only 87.8% carry gene= -- so a fixture
# with every record having a gene= would never exercise the fallback path a
# real transcriptome hits on over 1 in 10 of its records.
HEADERS = [
    ">lcl|NC_001133.9_cds_NP_009332.1_1 [gene=PAU8] [locus_tag=YAL068C] "
    "[db_xref=SGD:S000002142,GeneID:851229] [protein=seripauperin PAU8] "
    "[protein_id=NP_009332.1] [location=complement(1807..2169)] [gbkey=CDS]",
    # No gene= at all -- a real record, not a synthetic edge case. This is
    # what makes the locus_tag-first fallback path realistic rather than
    # merely theoretical.
    ">lcl|NC_001133.9_cds_NP_878038.1_2 [locus_tag=YAL067W-A] "
    "[db_xref=SGD:S000028593,GeneID:1466426] "
    "[protein=uncharacterized protein] [protein_id=NP_878038.1] "
    "[location=2480..2707] [gbkey=CDS]",
    ">lcl|NC_001133.9_cds_NP_009333.1_3 [gene=SEO1] [locus_tag=YAL067C] "
    "[db_xref=SGD:S000000062,GeneID:851230] "
    "[protein=putative permease SEO1] [protein_id=NP_009333.1] "
    "[location=complement(7235..9016)] [gbkey=CDS]",
    # Second CDS of the same gene: the case that makes summarization more
    # than a rename.
    ">lcl|NC_001133.9_cds_NP_009334.1_4 [gene=SEO1] [locus_tag=YAL067C] "
    "[protein=Seo1p isoform] [protein_id=NP_009334.1] [gbkey=CDS]",
]


class TestParseTx2Gene:
    def test_maps_each_transcript_to_its_locus_tag(self):
        mapping = salmon_runner.parse_tx2gene(HEADERS)
        assert mapping == {
            "lcl|NC_001133.9_cds_NP_009332.1_1": "YAL068C",
            # No gene= on this record -- locus_tag is the only source.
            "lcl|NC_001133.9_cds_NP_878038.1_2": "YAL067W-A",
            "lcl|NC_001133.9_cds_NP_009333.1_3": "YAL067C",
            # Both CDS of SEO1 collapse onto one gene, which is what makes
            # summarization more than a rename.
            "lcl|NC_001133.9_cds_NP_009334.1_4": "YAL067C",
        }

    def test_prefers_locus_tag_over_gene(self):
        # locus_tag is what counts_runner.attributes_for_format groups NCBI
        # GFF3 by. Preferring `gene=` would produce a gene universe that does
        # not match featureCounts output for the same organism.
        mapping = salmon_runner.parse_tx2gene(
            [">lcl|X_cds_1 [gene=ABC1] [locus_tag=Y0001W]"]
        )
        assert mapping == {"lcl|X_cds_1": "Y0001W"}

    def test_falls_back_to_gene_when_no_locus_tag(self):
        mapping = salmon_runner.parse_tx2gene([">lcl|X_cds_1 [gene=ABC1]"])
        assert mapping == {"lcl|X_cds_1": "ABC1"}

    def test_refuses_a_header_it_cannot_map(self):
        # REQ-TX2GENE-1. The alternative -- treating the transcript as its own
        # gene -- yields a counts file that merges cleanly and is wrong.
        with pytest.raises(ValidationError) as exc:
            salmon_runner.parse_tx2gene([">some_bare_transcript_id"])
        assert "some_bare_transcript_id" in str(exc.value)

    def test_refusal_names_the_offending_header_not_just_a_count(self):
        with pytest.raises(ValidationError) as exc:
            salmon_runner.parse_tx2gene(HEADERS + [">unmappable_one"])
        assert "unmappable_one" in str(exc.value)


class TestSummarizeToGene:
    def test_sums_transcripts_belonging_to_one_gene(self):
        per_tx = {"t1": 10.4, "t2": 5.2, "t3": 4.4}
        tx2gene = {"t1": "geneA", "t2": "geneB", "t3": "geneB"}
        counts, _ = salmon_runner.summarize_to_gene(per_tx, tx2gene)
        # geneB is 5.2 + 4.4 = 9.6, rounded once at the end -> 10.
        assert counts == {"geneA": 10, "geneB": 10}

    def test_rounds_after_summing_not_before(self):
        # Three transcripts at 0.4 each are one read's worth of evidence.
        # Rounding per transcript first would discard all of it.
        per_tx = {"t1": 0.4, "t2": 0.4, "t3": 0.4}
        tx2gene = {"t1": "g", "t2": "g", "t3": "g"}
        counts, _ = salmon_runner.summarize_to_gene(per_tx, tx2gene)
        assert counts == {"g": 1}

    def test_genes_with_no_reads_are_kept(self):
        # The gene universe must be the reference's, not the sample's.
        # Dropping zero-count genes would make two samples disagree on their
        # gene sets, which de_runner.merge_counts refuses outright.
        per_tx = {"t1": 0.0, "t2": 5.0}
        tx2gene = {"t1": "geneA", "t2": "geneB"}
        counts, facts = salmon_runner.summarize_to_gene(per_tx, tx2gene)
        assert counts == {"geneA": 0, "geneB": 5}
        assert facts["genes_in_reference"] == 2
        assert facts["genes_detected"] == 1

    def test_refuses_a_transcript_absent_from_the_map(self):
        # Salmon reported a transcript the map does not know. Summing the rest
        # silently would drop reads from the totals.
        with pytest.raises(ValidationError) as exc:
            salmon_runner.summarize_to_gene({"unknown_tx": 5.0}, {"t1": "g"})
        assert "unknown_tx" in str(exc.value)

    def test_counted_fragments_reports_the_integer_total(self):
        per_tx = {"t1": 10.4, "t2": 5.2}
        tx2gene = {"t1": "geneA", "t2": "geneB"}
        _, facts = salmon_runner.summarize_to_gene(per_tx, tx2gene)
        assert facts["counted_fragments"] == 15


class TestIndexCommand:
    def test_builds_an_index_from_a_transcriptome(self):
        cmd = salmon_runner.index_command(
            transcriptome=Path("/w/tx.fna"),
            index_dir=Path("/w/idx"),
            salmon_path="/usr/bin/salmon",
            threads=8,
        )
        assert cmd[:2] == ["/usr/bin/salmon", "index"]
        assert "-t" in cmd and "/w/tx.fna" in cmd
        assert "-i" in cmd and "/w/idx" in cmd
        assert "-p" in cmd and "8" in cmd


class TestQuantCommand:
    def test_single_end_uses_unmated_reads_flag(self):
        cmd = salmon_runner.quant_command(
            index_dir=Path("/w/idx"),
            reads=[Path("/w/a.fastq.gz")],
            out_dir=Path("/w/out"),
            salmon_path="/usr/bin/salmon",
        )
        assert cmd[:2] == ["/usr/bin/salmon", "quant"]
        assert "-r" in cmd
        assert "-1" not in cmd

    def test_paired_end_uses_mate_flags(self):
        cmd = salmon_runner.quant_command(
            index_dir=Path("/w/idx"),
            reads=[Path("/w/r1.fastq.gz"), Path("/w/r2.fastq.gz")],
            out_dir=Path("/w/out"),
            salmon_path="/usr/bin/salmon",
        )
        assert "-1" in cmd and "/w/r1.fastq.gz" in cmd
        assert "-2" in cmd and "/w/r2.fastq.gz" in cmd
        assert "-r" not in cmd

    def test_library_type_is_always_automatic(self):
        # -l A. The featureCounts path needs the library orientation supplied
        # because a wrong -s yields near-zero counts that look like a failed
        # experiment; Salmon infers it, so there is no flag for a user to get
        # wrong and none is offered.
        cmd = salmon_runner.quant_command(
            index_dir=Path("/w/idx"),
            reads=[Path("/w/a.fastq.gz")],
            out_dir=Path("/w/out"),
            salmon_path="/usr/bin/salmon",
        )
        assert "-l" in cmd
        assert cmd[cmd.index("-l") + 1] == "A"

    def test_refuses_more_than_two_read_files(self):
        with pytest.raises(ValidationError):
            salmon_runner.quant_command(
                index_dir=Path("/w/idx"),
                reads=[Path("/w/a"), Path("/w/b"), Path("/w/c")],
                out_dir=Path("/w/out"),
                salmon_path="/usr/bin/salmon",
            )

    def test_refuses_no_reads(self):
        with pytest.raises(ValidationError):
            salmon_runner.quant_command(
                index_dir=Path("/w/idx"),
                reads=[],
                out_dir=Path("/w/out"),
                salmon_path="/usr/bin/salmon",
            )


class TestPaths:
    def test_quant_file_is_inside_the_output_directory(self):
        assert salmon_runner.quant_file(Path("/w/out")) == Path("/w/out/quant.sf")

    def test_output_name_is_derived_from_the_sample(self):
        assert salmon_runner.output_name("SRR123_1.fastq.gz").endswith(".counts.tsv")
        assert "SRR123" in salmon_runner.output_name("SRR123_1.fastq.gz")

    def test_command_line_is_copy_pasteable(self):
        assert salmon_runner.command_line(["salmon", "quant", "-i", "/a b"]) == (
            "salmon quant -i '/a b'"
        )
