"""Subset export: which lines are selected, and whether they can be trusted.

Separate from annotation_db because this is the only place the parent/child
closure lives, and from annotation_hierarchy because that resolves status
for the whole file while this walks one filtered set.
"""

import pytest

from app.pipelines.annotation_db import (
    FeatureFilters,
    build_annotation_db,
)
from app.pipelines.annotation_export import closure_lines
from app.pipelines.annotation_hierarchy import resolve_hierarchy
from app.pipelines.annotation_parse import Feature


def _f(feature_id, parent, line_no, type="gene", start=1, end=100):
    return Feature(
        contig="chr1", start=start, end=end, type=type, strand="+",
        score=None, name=feature_id, feature_id=feature_id,
        parents=(parent,) if parent else (), biotype=None,
        attributes=f"ID={feature_id}", line_no=line_no,
    )


@pytest.fixture
def gene_tree(tmp_path):
    """gene g1 -> mRNA t1 -> exons e1,e2; plus an unrelated gene g2."""
    db = tmp_path / "f.db"
    build_annotation_db(
        rows=[
            _f("g1", None, 1),
            _f("t1", "g1", 2, type="mRNA"),
            _f("e1", "t1", 3, type="exon"),
            _f("e2", "t1", 4, type="exon"),
            _f("g2", None, 5),
        ],
        db_path=db,
    )
    resolve_hierarchy(db_path=db)
    return db


class TestClosure:
    def test_includes_matched_features(self, gene_tree):
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(feature_type="exon", top_level_only=False),
        )
        assert {3, 4} <= lines

    def test_includes_ancestors(self, gene_tree):
        """A Parent= naming a feature absent from the file makes the export
        fail in downstream tools."""
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(feature_type="exon", top_level_only=False),
        )
        assert {1, 2} <= lines

    def test_includes_descendants(self, gene_tree):
        """A gene exported without its transcripts is valid and useless."""
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(name_query="g1", top_level_only=False),
        )
        assert lines == {1, 2, 3, 4}

    def test_excludes_unrelated_features(self, gene_tree):
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(name_query="g1", top_level_only=False),
        )
        assert 5 not in lines

    def test_terminates_on_a_cycle(self, tmp_path):
        """A malformed file whose parents form a loop must not hang."""
        db = tmp_path / "cycle.db"
        build_annotation_db(
            rows=[_f("a", "b", 1), _f("b", "a", 2)], db_path=db
        )
        resolve_hierarchy(db_path=db)
        lines = closure_lines(
            db_path=db,
            filters=FeatureFilters(name_query="a", top_level_only=False),
        )
        assert lines == {1, 2}

    def test_features_without_a_line_are_skipped(self, tmp_path):
        """GenBank rows carry no line number and cannot be re-emitted."""
        db = tmp_path / "nl.db"
        build_annotation_db(rows=[_f("g1", None, None)], db_path=db)
        resolve_hierarchy(db_path=db)
        lines = closure_lines(
            db_path=db, filters=FeatureFilters(top_level_only=False)
        )
        assert lines == set()
