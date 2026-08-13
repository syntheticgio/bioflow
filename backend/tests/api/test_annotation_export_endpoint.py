"""The export route, and the filter builder it shares with the page route."""

from app.api.v1.pipelines import build_feature_filters


class TestSharedFilterBuilder:
    """One definition of what a filter means. The page count and the export
    must agree, or the matched-versus-exported counts are meaningless."""

    def test_type_filter_clears_top_level_only(self):
        """Every exon has a parent, so leaving the flag set returns an empty
        table on a perfectly good GFF3."""
        f = build_feature_filters(feature_type="exon", view="all")
        assert f.top_level_only is False

    def test_no_type_filter_keeps_top_level_only(self):
        f = build_feature_filters(view="all")
        assert f.top_level_only is True

    def test_unresolved_view_clears_top_level_only(self):
        f = build_feature_filters(view="unresolved")
        assert f.top_level_only is False

    def test_unresolved_view_sets_parent_status(self):
        from app.pipelines import annotation_hierarchy

        f = build_feature_filters(view="unresolved")
        assert f.parent_status == annotation_hierarchy.UNRESOLVED_STATUSES

    def test_passes_through_the_plain_filters(self):
        f = build_feature_filters(
            contig="chr1", biotype="protein_coding", strand="+",
            name_query="BRCA", start_min=10, start_max=20, view="all",
        )
        assert f.contig == "chr1"
        assert f.biotype == "protein_coding"
        assert f.strand == "+"
        assert f.name_query == "BRCA"
        assert f.start_min == 10
        assert f.start_max == 20
