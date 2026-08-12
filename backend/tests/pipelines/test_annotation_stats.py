"""Bounded aggregates over a stream of features.

The coverage accumulator is the one with a real invariant: overlapping
features must not double-count bases, or a dense annotation reports coverage
above 100%.
"""

from app.pipelines.annotation_parse import Feature
from app.pipelines.annotation_stats import AnnotationAccumulator, parse_header_directives


def _feature(contig="chr1", start=1, end=100, type="gene", parent=None, biotype=None):
    return Feature(
        contig=contig,
        start=start,
        end=end,
        type=type,
        strand="+",
        score=None,
        name="x",
        feature_id="x1",
        parent=parent,
        biotype=biotype,
        attributes="ID=x1",
    )


class TestCounts:
    def test_counts_features_and_types(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(type="gene"))
        acc.add(_feature(type="exon", parent="x1"))
        acc.add(_feature(type="exon", parent="x1"))
        facts = acc.finish()
        assert facts["annotation_feature_count"] == 3
        assert facts["annotation_type_counts"] == {"gene": 1, "exon": 2}

    def test_counts_top_level_separately(self):
        """The table pages over parents, so its total must be countable."""
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(type="gene"))
        acc.add(_feature(type="exon", parent="x1"))
        facts = acc.finish()
        assert facts["annotation_top_level_count"] == 1

    def test_counts_biotypes_when_present(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(biotype="protein_coding"))
        acc.add(_feature(biotype="protein_coding"))
        acc.add(_feature(biotype="lncRNA"))
        facts = acc.finish()
        assert facts["annotation_biotype_counts"] == {
            "protein_coding": 2,
            "lncRNA": 1,
        }

    def test_biotype_counts_absent_when_no_biotypes(self):
        """Absent rather than an empty dict: the UI renders the block only
        when there is something in it, and {} would render an empty chart."""
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature())
        assert "annotation_biotype_counts" not in acc.finish()

    def test_per_contig_counts(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000, "chr2": 500})
        acc.add(_feature(contig="chr1"))
        acc.add(_feature(contig="chr1"))
        acc.add(_feature(contig="chr2"))
        per = acc.finish()["annotation_per_contig"]
        by_name = {c["name"]: c for c in per}
        assert by_name["chr1"]["count"] == 2
        assert by_name["chr2"]["count"] == 1

    def test_records_malformed_line_count(self):
        acc = AnnotationAccumulator(contig_lengths={})
        acc.add_malformed()
        acc.add_malformed()
        assert acc.finish()["annotation_malformed_lines"] == 2

    def test_malformed_absent_when_zero(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 10})
        acc.add(_feature())
        assert "annotation_malformed_lines" not in acc.finish()


class TestCoverage:
    def test_simple_coverage_fraction(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(start=1, end=100))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["chr1"]["covered_bases"] == 100
        assert per["chr1"]["covered_fraction"] == 0.1

    def test_overlapping_features_do_not_double_count(self):
        """The invariant this accumulator exists for. Two exons overlapping
        by 50 bases cover 150 bases, not 200."""
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(start=1, end=100))
        acc.add(_feature(start=51, end=150))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["chr1"]["covered_bases"] == 150

    def test_fully_contained_feature_adds_nothing(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(start=1, end=100))
        acc.add(_feature(start=20, end=30))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["chr1"]["covered_bases"] == 100

    def test_coverage_never_exceeds_contig_length(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 100})
        for start in range(1, 100, 5):
            acc.add(_feature(start=start, end=start + 40))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["chr1"]["covered_bases"] <= 100
        assert per["chr1"]["covered_fraction"] <= 1.0

    def test_unordered_input_still_merges(self):
        """Features arrive in file order, which is not guaranteed sorted."""
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add(_feature(start=51, end=150))
        acc.add(_feature(start=1, end=100))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["chr1"]["covered_bases"] == 150

    def test_contig_with_no_length_reports_null_fraction(self):
        """A contig absent from the reference's lengths gets a count but no
        fraction -- null, not 0.0: unknown and zero are different claims."""
        acc = AnnotationAccumulator(contig_lengths={})
        acc.add(_feature(contig="scaffold_99"))
        per = {c["name"]: c for c in acc.finish()["annotation_per_contig"]}
        assert per["scaffold_99"]["count"] == 1
        assert per["scaffold_99"]["covered_fraction"] is None


class TestLengthHistogram:
    def test_bins_feature_lengths(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 10_000})
        acc.add(_feature(start=1, end=10))
        acc.add(_feature(start=1, end=10))
        acc.add(_feature(start=1, end=5000))
        hist = acc.finish()["annotation_length_histogram"]
        assert sum(b["count"] for b in hist) == 3
        assert all("min" in b and "max" in b for b in hist)


class TestAttributeKeys:
    def test_counts_attribute_keys(self):
        acc = AnnotationAccumulator(contig_lengths={"chr1": 1000})
        acc.add_attribute_keys(["ID", "Name", "Parent"])
        acc.add_attribute_keys(["ID", "Name"])
        assert acc.finish()["annotation_attribute_keys"] == {
            "ID": 2,
            "Name": 2,
            "Parent": 1,
        }


class TestHeaderDirectives:
    def test_reads_gff_version_and_source(self):
        header = [
            "##gff-version 3",
            "#!genome-build GRCh38.p13",
            "#!annotation-source NCBI RefSeq GCF_000001405.39",
        ]
        meta = parse_header_directives(header)
        assert meta["gff_version"] == "3"
        assert meta["genome_build"] == "GRCh38.p13"
        assert meta["annotation_source"] == "NCBI RefSeq GCF_000001405.39"

    def test_empty_for_no_directives(self):
        assert parse_header_directives(["chr1\t.\tgene\t1\t9\t.\t+\t.\tID=g1"]) == {}
