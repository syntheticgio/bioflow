"""Parsing GFF3, GTF, and BED lines into one normalized Feature.

Kept free of I/O so every case is a plain function call: this is where the
format edge cases live, and they are the part most likely to be wrong.
"""

import pytest
from app.pipelines.annotation_parse import (
    Feature,
    parse_bed_line,
    parse_gff_attributes,
    parse_gff_line,
    parse_gtf_attributes,
    parse_gtf_line,
)


class TestGffAttributes:
    def test_parses_key_value_pairs(self):
        attrs = parse_gff_attributes("ID=gene1;Name=BRCA1;gene_biotype=protein_coding")
        assert attrs == {
            "ID": "gene1",
            "Name": "BRCA1",
            "gene_biotype": "protein_coding",
        }

    def test_url_decodes_values(self):
        # GFF3 percent-encodes reserved characters; a product name with a
        # comma or equals sign is common in NCBI annotations.
        attrs = parse_gff_attributes("product=alpha%2Cbeta%3Dgamma")
        assert attrs["product"] == "alpha,beta=gamma"

    def test_empty_attributes_column(self):
        assert parse_gff_attributes(".") == {}
        assert parse_gff_attributes("") == {}

    def test_ignores_malformed_pairs(self):
        # A bare token with no '=' is skipped rather than raising.
        attrs = parse_gff_attributes("ID=gene1;junk;Name=X")
        assert attrs == {"ID": "gene1", "Name": "X"}


class TestGtfAttributes:
    def test_parses_quoted_pairs(self):
        attrs = parse_gtf_attributes('gene_id "ENSG01"; transcript_id "ENST01";')
        assert attrs == {"gene_id": "ENSG01", "transcript_id": "ENST01"}

    def test_tolerates_missing_trailing_semicolon(self):
        attrs = parse_gtf_attributes('gene_id "ENSG01"')
        assert attrs == {"gene_id": "ENSG01"}

    def test_unquoted_values(self):
        attrs = parse_gtf_attributes("exon_number 3;")
        assert attrs == {"exon_number": "3"}


class TestGffLine:
    def test_parses_a_gene_row(self):
        line = (
            "chr1\tHAVANA\tgene\t1000\t2000\t.\t+\t.\t"
            "ID=gene1;Name=BRCA1;gene_biotype=protein_coding"
        )
        f = parse_gff_line(line)
        assert f == Feature(
            contig="chr1",
            start=1000,
            end=2000,
            type="gene",
            strand="+",
            score=None,
            name="BRCA1",
            feature_id="gene1",
            parents=(),
            biotype="protein_coding",
            attributes="ID=gene1;Name=BRCA1;gene_biotype=protein_coding",
        )

    def test_child_records_its_parent(self):
        line = "chr1\tHAVANA\texon\t1000\t1100\t.\t+\t.\tID=exon1;Parent=gene1"
        f = parse_gff_line(line)
        assert f.parents == ("gene1",)
        assert f.type == "exon"

    def test_gff_multi_parent_keeps_every_parent(self):
        # GFF3 permits Parent=a,b for a feature shared by two transcripts.
        # Every named parent must be kept -- storage writes one row per
        # relationship, so dropping any here would silently lose a
        # gene/transcript link for later tasks to reconstruct.
        line = "chr1\t.\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1,t2"
        feature = parse_gff_line(line)
        assert feature.parents == ("t1", "t2")

    def test_gff_multi_parent_strips_whitespace(self):
        line = "chr1\t.\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1, t2"
        assert parse_gff_line(line).parents == ("t1", "t2")

    def test_gff_multi_parent_drops_empty_tokens_from_malformed_commas(self):
        line = "chr1\t.\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1,,t2,"
        assert parse_gff_line(line).parents == ("t1", "t2")

    def test_score_parsed_when_numeric(self):
        line = "chr1\t.\tgene\t1\t9\t42.5\t+\t.\tID=g1"
        assert parse_gff_line(line).score == 42.5

    def test_missing_score_is_none(self):
        # None rather than 0.0: an absent score is not a score of zero.
        line = "chr1\t.\tgene\t1\t9\t.\t+\t.\tID=g1"
        assert parse_gff_line(line).score is None

    def test_falls_back_to_id_for_name(self):
        line = "chr1\t.\tgene\t1\t9\t.\t+\t.\tID=gene1"
        assert parse_gff_line(line).name == "gene1"

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "#comment",
            "##gff-version 3",
            "chr1\t.\tgene\t1",  # too few columns
            "chr1\t.\tgene\tNOTANUMBER\t9\t.\t+\t.\tID=g1",
        ],
    )
    def test_unparseable_returns_none(self, line):
        assert parse_gff_line(line) is None


class TestGtfLine:
    def test_transcript_parent_is_its_gene(self):
        line = (
            'chr1\tENSEMBL\ttranscript\t1000\t2000\t.\t+\t.\t'
            'gene_id "ENSG01"; transcript_id "ENST01";'
        )
        f = parse_gtf_line(line)
        assert f.feature_id == "ENST01"
        assert f.parents == ("ENSG01",)

    def test_exon_parent_is_its_transcript(self):
        line = (
            'chr1\tENSEMBL\texon\t1000\t1100\t.\t+\t.\t'
            'gene_id "ENSG01"; transcript_id "ENST01"; exon_number "1";'
        )
        f = parse_gtf_line(line)
        assert f.parents == ("ENST01",)

    def test_gene_row_is_top_level(self):
        line = 'chr1\tENSEMBL\tgene\t1000\t2000\t.\t+\t.\tgene_id "ENSG01";'
        f = parse_gtf_line(line)
        assert f.feature_id == "ENSG01"
        assert f.parents == ()

    def test_cds_without_transcript_id_falls_back_to_gene(self):
        # Some GTFs omit transcript_id on CDS rows. Attaching to the gene
        # keeps the row in the tree rather than orphaning it at top level,
        # where it would inflate the parent count.
        line = 'chr1\tX\tCDS\t1000\t1100\t.\t+\t0\tgene_id "ENSG01";'
        assert parse_gtf_line(line).parents == ("ENSG01",)

    def test_gtf_transcript_parent_is_its_gene(self):
        line = 'chr1\t.\ttranscript\t100\t200\t.\t+\t.\tgene_id "g1"; transcript_id "t1";'
        assert parse_gtf_line(line).parents == ("g1",)

    def test_gtf_exon_falls_back_to_gene_without_transcript_id(self):
        line = 'chr1\t.\texon\t100\t200\t.\t+\t.\tgene_id "g1";'
        assert parse_gtf_line(line).parents == ("g1",)

    def test_sibling_exons_do_not_collide_on_feature_id(self):
        # children_of(parent_id) is a string-equality lookup on feature_id,
        # so two exons under one transcript must not both claim the
        # transcript's own feature_id -- that would make them
        # indistinguishable from each other and from the transcript itself,
        # and would make the transcript appear to parent itself.
        exon1 = parse_gtf_line(
            'chr1\tX\texon\t1000\t1100\t.\t+\t.\t'
            'gene_id "ENSG01"; transcript_id "ENST01"; exon_number "1";'
        )
        exon2 = parse_gtf_line(
            'chr1\tX\texon\t1200\t1300\t.\t+\t.\t'
            'gene_id "ENSG01"; transcript_id "ENST01"; exon_number "2";'
        )
        assert exon1.parents == exon2.parents == ("ENST01",)
        assert exon1.feature_id not in exon1.parents
        assert exon2.feature_id not in exon2.parents
        # Neither exon usurps the transcript's own identifier.
        assert exon1.feature_id != "ENST01"
        assert exon2.feature_id != "ENST01"
        # GTF gives exon/CDS/UTR rows no identifier of their own.
        assert exon1.feature_id is None
        assert exon2.feature_id is None

    def test_biotype_from_gene_biotype(self):
        line = (
            'chr1\tX\tgene\t1\t9\t.\t+\t.\t'
            'gene_id "G1"; gene_biotype "lncRNA";'
        )
        assert parse_gtf_line(line).biotype == "lncRNA"


class TestBedLine:
    def test_three_column_bed(self):
        f = parse_bed_line("chr1\t0\t100")
        assert f.contig == "chr1"
        assert f.type is None
        assert f.parents == ()
        assert f.name is None

    def test_converts_to_one_based_inclusive(self):
        # BED is half-open and zero-based: [0,100) is bases 1..100 in GFF's
        # 1-based inclusive terms. Getting this wrong is an off-by-one that
        # stays invisible until someone compares against a genome browser.
        f = parse_bed_line("chr1\t0\t100")
        assert (f.start, f.end) == (1, 100)

    def test_named_bed_uses_column_four(self):
        f = parse_bed_line("chr1\t0\t100\tpeak1\t960\t+")
        assert f.name == "peak1"
        assert f.score == 960.0
        assert f.strand == "+"

    def test_bed_score_dot_is_none(self):
        f = parse_bed_line("chr1\t0\t100\tpeak1\t.\t+")
        assert f.score is None

    def test_bed_has_no_parents(self):
        assert parse_bed_line("chr1\t99\t200\tpeak1").parents == ()

    @pytest.mark.parametrize(
        "line",
        ["", "#comment", "track name=x", "browser position chr1", "chr1\t0"],
    )
    def test_unparseable_returns_none(self, line):
        assert parse_bed_line(line) is None


class TestCoordinateAgreement:
    def test_bed_and_gff_describe_the_same_interval_identically(self):
        """The invariant the whole normalization exists for.

        The same 100-base interval written in either format must produce the
        same start/end, or the locus jump means different things depending on
        which file you opened.
        """
        bed = parse_bed_line("chr1\t999\t2000")
        gff = parse_gff_line("chr1\t.\tgene\t1000\t2000\t.\t+\t.\tID=g1")
        assert (bed.start, bed.end) == (gff.start, gff.end) == (1000, 2000)


class TestFeatureLine:
    def test_feature_defaults_to_no_line_number(self):
        """The parsers are pure functions of one string and cannot know the line.

        The handler's loop sets it. AE-1/AE-2.
        """
        line = "chr1\t.\tgene\t100\t200\t.\t+\t.\tID=g1"
        feature = parse_gff_line(line)
        assert feature.line is None

    def test_feature_line_is_settable_via_replace(self):
        """The handler sets the line with dataclasses.replace on a frozen Feature."""
        import dataclasses

        line = "chr1\t.\tgene\t100\t200\t.\t+\t.\tID=g1"
        feature = parse_gff_line(line)
        numbered = dataclasses.replace(feature, line=7)
        assert numbered.line == 7
        assert numbered.feature_id == "g1"
