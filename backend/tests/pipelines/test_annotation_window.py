"""Window queries for the annotation track viewer.

Separate from test_annotation_db because the table's concerns (paging,
filters, children-on-expand) and the viewer's (bins, gene models, packing)
have different fixtures and different failure modes.
"""

import pytest

from app.pipelines.annotation_db import (
    bin_counts,
    build_annotation_db,
    count_in_window,
    features_in_window,
)
from app.pipelines.annotation_parse import Feature
from app.pipelines.annotation_window import pack_rows


class TestPackRows:
    def test_non_overlapping_features_share_one_row(self):
        items = [(100, 200), (300, 400), (500, 600)]
        assert pack_rows(items) == [0, 0, 0]

    def test_overlapping_features_get_distinct_rows(self):
        items = [(100, 500), (200, 600), (300, 700)]
        assert pack_rows(items) == [0, 1, 2]

    def test_row_is_reused_once_it_is_free(self):
        # Third feature starts after the first ends, so it reuses row 0.
        items = [(100, 200), (150, 250), (300, 400)]
        assert pack_rows(items) == [0, 1, 0]

    def test_touching_features_do_not_share_a_row(self):
        # End is inclusive: a feature ending at 200 and one starting at 200
        # overlap at that base and must not be drawn on one line.
        items = [(100, 200), (200, 300)]
        assert pack_rows(items) == [0, 1]

    def test_features_beyond_the_cap_report_none(self):
        items = [(100, 500)] * 15
        rows = pack_rows(items, max_rows=12)
        assert rows[:12] == list(range(12))
        assert rows[12:] == [None, None, None]


def _f(contig, start, end, ftype="gene", feature_id="f", parent=None, strand="+"):
    return Feature(
        contig=contig, start=start, end=end, type=ftype, strand=strand,
        score=None, name=feature_id, feature_id=feature_id, parent=parent,
        biotype=None, attributes=f"ID={feature_id}",
    )


@pytest.fixture
def db(tmp_path):
    """Ten genes at 1000-base spacing on chr1, plus one on chr2."""
    rows = [
        _f("chr1", 1000 * i, 1000 * i + 500, feature_id=f"g{i}")
        for i in range(1, 11)
    ]
    rows.append(_f("chr2", 1000, 1500, feature_id="other"))
    path = tmp_path / "features.db"
    build_annotation_db(rows=iter(rows), db_path=path)
    return path


class TestCountInWindow:
    def test_counts_only_the_requested_contig(self, db):
        assert count_in_window(db_path=db, contig="chr2", start=0, end=100_000) == 1

    def test_counts_overlap_not_containment(self, db):
        # g1 spans 1000-1500. A window starting at 1200 still contains part
        # of it, so it counts.
        assert count_in_window(db_path=db, contig="chr1", start=1200, end=1300) == 1

    def test_excludes_children(self, tmp_path):
        rows = [
            _f("chr1", 100, 900, feature_id="g1"),
            _f("chr1", 100, 200, ftype="exon", feature_id="e1", parent="g1"),
        ]
        path = tmp_path / "c.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        assert count_in_window(db_path=path, contig="chr1", start=0, end=1000) == 1


class TestBinCounts:
    def test_returns_exactly_the_requested_number_of_bins(self, db):
        counts = bin_counts(
            db_path=db, contig="chr1", start=0, end=10_000, bins=10
        )
        assert len(counts) == 10
        # Bins are 1000 bases wide. Bin 0 covers 0-999 and holds no gene
        # (g1 starts at 1000); bin 1 covers 1000-1999 and holds g1.
        assert counts[0] == 0
        assert counts[1] == 1
        # g10 starts at exactly 10000, the window's last base, so it lands in
        # the final bin alongside g9.
        assert counts[9] == 2

    def test_empty_bins_are_zero_not_missing(self, tmp_path):
        rows = [_f("chr1", 100, 200, feature_id="only")]
        path = tmp_path / "sparse.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        counts = bin_counts(db_path=path, contig="chr1", start=0, end=1000, bins=10)
        # 100-base bins, so bp 100 is the first base of bin 1, not bin 0.
        assert counts == [0, 1, 0, 0, 0, 0, 0, 0, 0, 0]

    def test_feature_straddling_a_bin_edge_counts_once(self, tmp_path):
        # Counted by start coordinate, so a feature crossing an edge lands in
        # exactly one bin rather than being double-counted.
        rows = [_f("chr1", 450, 650, feature_id="straddle")]
        path = tmp_path / "edge.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        counts = bin_counts(db_path=path, contig="chr1", start=0, end=1000, bins=2)
        assert counts == [1, 0]

    def test_window_shorter_than_bins_floors_at_one_base(self, tmp_path):
        rows = [_f("chr1", 5, 5, feature_id="point")]
        path = tmp_path / "tiny.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        counts = bin_counts(db_path=path, contig="chr1", start=0, end=9, bins=600)
        # A 9-base span cannot make 600 bins; it makes 9 one-base bins, and
        # the returned length is authoritative rather than the request.
        assert len(counts) == 9
        assert counts[5] == 1


class TestFeaturesInWindow:
    @pytest.fixture
    def gene_db(self, tmp_path):
        """One gene with two exons, and an exon whose parent is absent."""
        rows = [
            _f("chr1", 1000, 2000, feature_id="g1"),
            _f("chr1", 1000, 1200, ftype="exon", feature_id="e1", parent="g1"),
            _f("chr1", 1800, 2000, ftype="exon", feature_id="e2", parent="g1"),
            _f("chr1", 5000, 5100, ftype="exon", feature_id="orphan", parent="ghost"),
        ]
        path = tmp_path / "genes.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        return path

    def test_children_are_attached_to_their_parent(self, gene_db):
        got = features_in_window(
            db_path=gene_db, contig="chr1", start=0, end=3000
        )
        assert len(got) == 1
        assert got[0]["feature_id"] == "g1"
        assert [c["feature_id"] for c in got[0]["children"]] == ["e1", "e2"]

    def test_orphaned_child_is_returned_detached(self, gene_db):
        # Its parent does not exist. Dropping it would show empty sequence
        # where a feature is, so it is drawn on its own.
        got = features_in_window(
            db_path=gene_db, contig="chr1", start=4000, end=6000
        )
        assert [f["feature_id"] for f in got] == ["orphan"]
        assert got[0]["children"] == []

    def test_child_whose_parent_is_offscreen_is_returned(self, gene_db):
        # Window covers e2 but not g1's start. e2 is at this locus, so it
        # appears rather than vanishing.
        got = features_in_window(
            db_path=gene_db, contig="chr1", start=1900, end=1950
        )
        assert [f["feature_id"] for f in got] == ["g1"]

    def test_self_referencing_parent_is_returned_not_dropped(self, tmp_path):
        # parent == feature_id resolves to itself in by_id.get(), so the
        # naive "parent is None" check is false and the row never lands in
        # out -- it must be treated like a missing parent, not silently
        # dropped from the window.
        rows = [
            _f("chr1", 100, 200, feature_id="self", parent="self"),
        ]
        path = tmp_path / "self_parent.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        got = features_in_window(
            db_path=path, contig="chr1", start=0, end=1000
        )
        assert [f["feature_id"] for f in got] == ["self"]
        assert got[0]["children"] == []

    def test_filters_by_type(self, gene_db):
        got = features_in_window(
            db_path=gene_db, contig="chr1", start=0, end=6000, feature_type="exon"
        )
        assert {f["feature_id"] for f in got} == {"e1", "e2", "orphan"}

    def test_filters_by_strand(self, tmp_path):
        rows = [
            _f("chr1", 100, 200, feature_id="plus", strand="+"),
            _f("chr1", 300, 400, feature_id="minus", strand="-"),
        ]
        path = tmp_path / "strand.db"
        build_annotation_db(rows=iter(rows), db_path=path)
        got = features_in_window(
            db_path=path, contig="chr1", start=0, end=1000, strand="-"
        )
        assert [f["feature_id"] for f in got] == ["minus"]
