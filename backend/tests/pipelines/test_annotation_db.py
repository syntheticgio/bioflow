"""The SQLite database backing the feature table.

Separate from annotation_stats because this is the only stateful part of the
feature and the only place SQL lives -- the same split variant_db documents.
"""

import pytest

from app.pipelines.annotation_parse import Feature
from app.pipelines.annotation_db import (
    FeatureFilters,
    build_annotation_db,
    children_of,
    count_features,
    query_features,
)
from app.pipelines.annotation_hierarchy import resolve_hierarchy


def _f(contig, start, end, type, feature_id, parent=None, name=None, biotype=None):
    return Feature(
        contig=contig,
        start=start,
        end=end,
        type=type,
        strand="+",
        score=None,
        name=name or feature_id,
        feature_id=feature_id,
        parents=(parent,) if parent else (),
        biotype=biotype,
        attributes=f"ID={feature_id}",
    )


def _multi(contig, start, end, type, feature_id, parents):
    return Feature(
        contig=contig, start=start, end=end, type=type, strand="+", score=None,
        name=feature_id, feature_id=feature_id, parents=parents,
        biotype=None, attributes=f"ID={feature_id}",
    )


def _features():
    """Two genes on chr1 with exons, one gene on chr2, one bare BED-ish row."""
    return iter(
        [
            _f("chr1", 1000, 2000, "gene", "g1", name="BRCA1", biotype="protein_coding"),
            _f("chr1", 1000, 1200, "exon", "e1", parent="g1"),
            _f("chr1", 1800, 2000, "exon", "e2", parent="g1"),
            _f("chr1", 5000, 6000, "gene", "g2", name="KINASE1", biotype="lncRNA"),
            _f("chr1", 5000, 5100, "exon", "e3", parent="g2"),
            _f("chr2", 100, 900, "gene", "g3", name="TP53", biotype="protein_coding"),
            _f("chr2", 2000, 2500, None, "b1", name=None),
        ]
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "features.db"
    build_annotation_db(rows=_features(), db_path=path)
    return path


class TestBuild:
    def test_inserts_every_row(self, tmp_path):
        path = tmp_path / "features.db"
        assert build_annotation_db(rows=_features(), db_path=path) == 7

    def test_rebuild_replaces_rather_than_appends(self, tmp_path):
        path = tmp_path / "features.db"
        build_annotation_db(rows=_features(), db_path=path)
        assert build_annotation_db(rows=_features(), db_path=path) == 7
        assert count_features(db_path=path, filters=FeatureFilters(top_level_only=False)) == 7


class TestTopLevelPaging:
    def test_defaults_to_top_level_only(self, db):
        """The table opens on parents, not on three million exons."""
        rows = query_features(db_path=db, filters=FeatureFilters(), offset=0, limit=50)
        assert [r["feature_id"] for r in rows] == ["g1", "g2", "g3", "b1"]

    def test_count_matches_the_page(self, db):
        """The page and its total must agree or pagination misreports."""
        filters = FeatureFilters()
        assert count_features(db_path=db, filters=filters) == 4

    def test_offset_and_limit(self, db):
        rows = query_features(db_path=db, filters=FeatureFilters(), offset=1, limit=2)
        assert [r["feature_id"] for r in rows] == ["g2", "g3"]

    def test_rows_carry_a_has_children_flag(self, db):
        """The chevron must not render on a row with nothing under it."""
        rows = query_features(db_path=db, filters=FeatureFilters(), offset=0, limit=50)
        by_id = {r["feature_id"]: r for r in rows}
        assert by_id["g1"]["has_children"] is True
        assert by_id["g3"]["has_children"] is False


class TestChildren:
    def test_returns_children_in_position_order(self, db):
        rows = children_of(db_path=db, parent_id="g1")
        assert [r["feature_id"] for r in rows] == ["e1", "e2"]

    def test_empty_for_a_leaf(self, db):
        assert children_of(db_path=db, parent_id="g3") == []

    def test_empty_for_an_unknown_parent(self, db):
        assert children_of(db_path=db, parent_id="nope") == []


class TestFilters:
    def test_filter_by_contig(self, db):
        filters = FeatureFilters(contig="chr2")
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert {r["feature_id"] for r in rows} == {"g3", "b1"}
        assert count_features(db_path=db, filters=filters) == 2

    def test_type_filter_searches_children_too(self, db):
        """The interaction that silently breaks the table.

        The route clears top_level_only whenever a type filter is set -- every
        exon has a parent, so leaving it set returns an empty table on a valid
        GFF3. This layer honors the flag it is given; Task 6 (the route) is
        what sets it.
        """
        filters = FeatureFilters(feature_type="exon", top_level_only=False)
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert {r["feature_id"] for r in rows} == {"e1", "e2", "e3"}
        assert count_features(db_path=db, filters=filters) == 3

    def test_type_filter_alone_finds_no_children(self, db):
        """The failure the route exists to prevent, pinned at this layer."""
        filters = FeatureFilters(feature_type="exon")
        assert count_features(db_path=db, filters=filters) == 0

    def test_biotype_filter(self, db):
        filters = FeatureFilters(biotype="protein_coding")
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert {r["feature_id"] for r in rows} == {"g1", "g3"}

    def test_name_search_is_substring_and_case_insensitive(self, db):
        filters = FeatureFilters(name_query="kinase")
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert [r["feature_id"] for r in rows] == ["g2"]

    def test_name_search_escapes_like_wildcards(self, db):
        """A literal % must not match everything."""
        filters = FeatureFilters(name_query="%")
        assert count_features(db_path=db, filters=filters) == 0

    def test_strand_filter(self, db):
        filters = FeatureFilters(strand="-")
        assert count_features(db_path=db, filters=filters) == 0

    def test_filters_compose(self, db):
        filters = FeatureFilters(contig="chr1", biotype="protein_coding")
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert [r["feature_id"] for r in rows] == ["g1"]


class TestLocusJump:
    def test_finds_features_overlapping_a_window(self, db):
        """Overlap, not containment: a gene straddling the window's edge is
        in the window as far as anyone looking at that locus is concerned."""
        filters = FeatureFilters(contig="chr1", start_min=1900, start_max=5200)
        rows = query_features(db_path=db, filters=filters, offset=0, limit=50)
        assert {r["feature_id"] for r in rows} == {"g1", "g2"}

    def test_excludes_features_outside_the_window(self, db):
        filters = FeatureFilters(contig="chr1", start_min=3000, start_max=4000)
        assert count_features(db_path=db, filters=filters) == 0


def test_a_multi_parent_feature_is_stored_once_per_relationship(tmp_path):
    """AH-11: expanding either transcript must show the shared exon."""
    db_path = tmp_path / "f.db"
    build_annotation_db(
        rows=iter([
            _f("chr1", 1, 500, "mRNA", "t1"),
            _f("chr1", 1, 500, "mRNA", "t2"),
            _multi("chr1", 10, 50, "exon", "e1", ("t1", "t2")),
        ]),
        db_path=db_path,
    )
    assert len(children_of(db_path=db_path, parent_id="t1")) == 1
    assert len(children_of(db_path=db_path, parent_id="t2")) == 1


def test_build_returns_source_feature_count_not_row_count(tmp_path):
    """AH-12: the summary total must not inflate with relationship count."""
    db_path = tmp_path / "f.db"
    total = build_annotation_db(
        rows=iter([
            _f("chr1", 1, 500, "mRNA", "t1"),
            _multi("chr1", 10, 50, "exon", "e1", ("t1", "t2")),
        ]),
        db_path=db_path,
    )
    assert total == 2


def test_a_feature_with_no_parents_stores_one_row(tmp_path):
    db_path = tmp_path / "f.db"
    build_annotation_db(rows=iter([_f("chr1", 1, 500, "gene", "g1")]), db_path=db_path)
    assert count_features(db_path=db_path, filters=FeatureFilters()) == 1


def test_parent_status_filter_selects_only_those_statuses(tmp_path):
    db_path = tmp_path / "f.db"
    build_annotation_db(
        rows=iter([
            _f("chr1", 1, 500, "gene", "g1"),
            _f("chr1", 10, 50, "exon", "e1", parent="nosuchgene"),
        ]),
        db_path=db_path,
    )
    resolve_hierarchy(db_path=db_path)
    filters = FeatureFilters(top_level_only=False, parent_status=("dangling",))
    rows = query_features(db_path=db_path, filters=filters, offset=0, limit=10)
    assert [r["feature_id"] for r in rows] == ["e1"]


def test_rows_carry_their_status_and_depth(tmp_path):
    db_path = tmp_path / "f.db"
    build_annotation_db(rows=iter([_f("chr1", 1, 500, "gene", "g1")]), db_path=db_path)
    resolve_hierarchy(db_path=db_path)
    row = query_features(
        db_path=db_path, filters=FeatureFilters(), offset=0, limit=1
    )[0]
    assert row["parent_status"] == "root"
    assert row["depth"] == 0
