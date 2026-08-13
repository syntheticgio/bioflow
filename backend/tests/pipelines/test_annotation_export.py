"""Subset export: closure, verified re-emission, and round-trip fidelity."""

import dataclasses

import pytest

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


_GFF_SOURCE = """\
##gff-version 3
##sequence-region chr1 1 1000
chr1\t.\tgene\t100\t900\t.\t+\t.\tID=g1
chr1\t.\texon\t100\t200\t.\t+\t0\tID=e1;Parent=g1
chr2\t.\tgene\t10\t20\t.\t-\t.\tID=g2
"""


def _write_source(tmp_path, text=_GFF_SOURCE):
    source = tmp_path / "in.gff3"
    source.write_text(text)
    return source


def _index_for_source(tmp_path, source):
    """Build an index whose line numbers match the file on disk."""
    rows = []
    for i, raw in enumerate(source.read_text().splitlines(), start=1):
        if raw.startswith("#"):
            continue
        feature = annotation_parse.parse_gff_line(raw)
        if feature is not None:
            rows.append(dataclasses.replace(feature, line=i))
    return _build(tmp_path, rows)


def test_written_lines_are_byte_identical(tmp_path):
    """AE-11: output lines are copied, never reconstructed."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={3, 4},
        header=["##gff-version 3"], fmt="gff",
    )

    written = dest.read_text().splitlines()
    assert "chr1\t.\texon\t100\t200\t.\t+\t0\tID=e1;Parent=g1" in written


def test_phase_survives_the_round_trip(tmp_path):
    """The reason this design copies bytes: Feature does not keep phase, so a
    reconstructed CDS line would lose its reading frame."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={4},
        header=["##gff-version 3"], fmt="gff",
    )

    exon = [ln for ln in dest.read_text().splitlines() if not ln.startswith("#")][0]
    assert exon.split("\t")[7] == "0"


def test_output_preserves_source_order(tmp_path):
    """AE-12: satisfied structurally by walking the source, not the line set."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={5, 3},
        header=["##gff-version 3"], fmt="gff",
    )

    features = [ln for ln in dest.read_text().splitlines() if not ln.startswith("#")]
    assert features[0].startswith("chr1")
    assert features[1].startswith("chr2")


def test_gff_version_pragma_is_written(tmp_path):
    """AE-13: a GFF3 export starts with the version pragma."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={3},
        header=["##gff-version 3"], fmt="gff",
    )

    assert dest.read_text().splitlines()[0] == "##gff-version 3"


def test_gff_version_pragma_is_synthesized_when_absent(tmp_path):
    """AE-13: the output must be valid GFF3 even when the input was sloppy."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={3},
        header=[], fmt="gff",
    )

    assert dest.read_text().splitlines()[0] == "##gff-version 3"


def test_sequence_region_pragmas_are_dropped(tmp_path):
    """AE-17: they describe the whole source and are wrong on a subset."""
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)
    dest = tmp_path / "out.gff3"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={3},
        header=["##gff-version 3", "##sequence-region chr1 1 1000"], fmt="gff",
    )

    assert "sequence-region" not in dest.read_text()


def test_bed_gets_no_synthesized_header(tmp_path):
    """AE-13a: BED has no mandatory header, so none is invented."""
    source = tmp_path / "in.bed"
    source.write_text("chr1\t99\t200\tpeak1\n")
    rows = [dataclasses.replace(annotation_parse.parse_bed_line("chr1\t99\t200\tpeak1"), line=1)]
    db_path = _build(tmp_path, rows)
    dest = tmp_path / "out.bed"

    annotation_export.write_subset(
        source=source, dest=dest, db_path=db_path, lines={1}, header=[], fmt="bed",
    )

    assert dest.read_text() == "chr1\t99\t200\tpeak1\n"


def test_a_shifted_source_file_fails_the_export(tmp_path):
    """AE-14/AE-15: the guardrail that makes line numbers safe.

    Delete a line from the source after indexing, so every later line number
    now points one row off. The export must refuse rather than emit a
    plausible, wrong file.
    """
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)

    kept = [ln for ln in source.read_text().splitlines() if "g1" not in ln]
    source.write_text("\n".join(kept) + "\n")

    with pytest.raises(annotation_export.StaleIndexError):
        annotation_export.write_subset(
            source=source, dest=tmp_path / "out.gff3", db_path=db_path,
            lines={3, 4}, header=["##gff-version 3"], fmt="gff",
        )


def test_write_subset_returns_the_line_count(tmp_path):
    source = _write_source(tmp_path)
    db_path = _index_for_source(tmp_path, source)

    written = annotation_export.write_subset(
        source=source, dest=tmp_path / "out.gff3", db_path=db_path,
        lines={3, 4}, header=["##gff-version 3"], fmt="gff",
    )

    assert written == 2
