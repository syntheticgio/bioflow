"""Subset export: closure, verified re-emission, and round-trip fidelity."""

from app.pipelines import annotation_db, annotation_export, annotation_hierarchy, annotation_parse


def _feature(**kw):
    """A Feature with sensible defaults, so each test states only what it means."""
    base = dict(
        contig="chr1", start=100, end=200, type="gene", strand="+", score=None,
        name=None, feature_id=None, parents=(), biotype=None, attributes=None,
        line=None,
    )
    base.update(kw)
    return annotation_parse.Feature(**base)


def _build(tmp_path, rows):
    """Build an index and resolve its hierarchy, as the real job does."""
    db_path = tmp_path / "features.db"
    annotation_db.build_annotation_db(rows=rows, db_path=db_path)
    annotation_hierarchy.resolve_hierarchy(db_path=db_path)
    return db_path


# A three-level tree: one gene, one transcript, two exons.
def _gene_tree():
    return [
        _feature(type="gene", feature_id="g1", name="g1", start=100, end=900, line=1),
        _feature(type="transcript", feature_id="t1", parents=("g1",), start=100, end=900, line=2),
        _feature(type="exon", feature_id="e1", parents=("t1",), start=100, end=200, line=3),
        _feature(type="exon", feature_id="e2", parents=("t1",), start=800, end=900, line=4),
    ]


def test_closure_reaches_descendants(tmp_path):
    """AE-6: filtering to a gene exports its transcript and exons too."""
    db_path = _build(tmp_path, _gene_tree())
    filters = annotation_db.FeatureFilters(feature_type="gene", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2, 3, 4}


def test_closure_reaches_ancestors(tmp_path):
    """AE-7: filtering to exons exports the transcript and gene above them."""
    db_path = _build(tmp_path, _gene_tree())
    filters = annotation_db.FeatureFilters(feature_type="exon", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2, 3, 4}


def test_closure_reaches_both_directions_from_mid_tree(tmp_path):
    """A transcript match pulls the gene above and the exons below."""
    db_path = _build(tmp_path, _gene_tree())
    filters = annotation_db.FeatureFilters(feature_type="transcript", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2, 3, 4}


def test_closure_excludes_unmatched_trees(tmp_path):
    """A second gene on another contig is not dragged in."""
    rows = _gene_tree() + [
        _feature(type="gene", feature_id="g2", contig="chr2", start=10, end=20, line=5),
    ]
    db_path = _build(tmp_path, rows)
    filters = annotation_db.FeatureFilters(contig="chr1", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2, 3, 4}


def test_multi_parent_feature_contributes_one_line(tmp_path):
    """AE-8: an exon shared by two transcripts is two rows but one line."""
    rows = [
        _feature(type="gene", feature_id="g1", start=100, end=900, line=1),
        _feature(type="transcript", feature_id="t1", parents=("g1",), line=2),
        _feature(type="transcript", feature_id="t2", parents=("g1",), line=3),
        _feature(type="exon", feature_id="e1", parents=("t1", "t2"), line=4),
    ]
    db_path = _build(tmp_path, rows)
    filters = annotation_db.FeatureFilters(feature_type="gene", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2, 3, 4}


def test_closure_terminates_on_a_cycle(tmp_path):
    """AE-9: a hierarchy that points at itself must not hang the walk."""
    rows = [
        _feature(type="gene", feature_id="a", parents=("b",), line=1),
        _feature(type="gene", feature_id="b", parents=("a",), line=2),
    ]
    db_path = _build(tmp_path, rows)
    filters = annotation_db.FeatureFilters(feature_type="gene", top_level_only=False)

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {1, 2}


def test_closure_ignores_top_level_only(tmp_path):
    """AE-10: top_level_only is a paging device, not a statement about content."""
    db_path = _build(tmp_path, _gene_tree())

    with_flag = annotation_export.closure_lines(
        db_path=db_path,
        filters=annotation_db.FeatureFilters(feature_type="exon", top_level_only=True),
    )
    without_flag = annotation_export.closure_lines(
        db_path=db_path,
        filters=annotation_db.FeatureFilters(feature_type="exon", top_level_only=False),
    )

    assert with_flag == without_flag == {1, 2, 3, 4}


def test_unresolved_filter_returns_matches_without_ancestors(tmp_path):
    """Correct but worth pinning: an unresolved row's parent by definition
    does not resolve, so the upward walk finds nothing to add."""
    rows = [
        _feature(type="gene", feature_id="g1", line=1),
        _feature(type="exon", feature_id="e1", parents=("nonexistent",), line=2),
    ]
    db_path = _build(tmp_path, rows)
    filters = annotation_db.FeatureFilters(
        top_level_only=False,
        parent_status=annotation_hierarchy.UNRESOLVED_STATUSES,
    )

    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    assert lines == {2}


def test_empty_match_returns_no_lines(tmp_path):
    """AE-16 is enforced by the handler; the query itself returns an empty set."""
    db_path = _build(tmp_path, _gene_tree())
    filters = annotation_db.FeatureFilters(contig="chrZ", top_level_only=False)

    assert annotation_export.closure_lines(db_path=db_path, filters=filters) == set()


def test_features_without_a_line_are_skipped(tmp_path):
    """GenBank rows carry no line and cannot be exported (AE-2)."""
    rows = [_feature(type="gene", feature_id="g1", line=None)]
    db_path = _build(tmp_path, rows)
    filters = annotation_db.FeatureFilters(feature_type="gene", top_level_only=False)

    assert annotation_export.closure_lines(db_path=db_path, filters=filters) == set()
