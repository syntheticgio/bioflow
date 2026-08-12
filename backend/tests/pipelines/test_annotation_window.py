"""Window queries for the annotation track viewer.

Separate from test_annotation_db because the table's concerns (paging,
filters, children-on-expand) and the viewer's (bins, gene models, packing)
have different fixtures and different failure modes.
"""

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
