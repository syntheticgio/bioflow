"""Parent resolution: which records have a real parent, and which do not.

The point of this module is that a record whose Parent names nothing is
*visible* rather than silently dropped, so most of these tests assert on
counts that must reconcile with the number of rows put in.
"""

import sqlite3

from app.pipelines.annotation_hierarchy import (
    DEPTH_CAP,
    build_gene_table,
    gene_mode,
    query_genes,
    resolve_hierarchy,
)


def _build(tmp_path, rows):
    """A features table with just the columns resolution touches."""
    db_path = tmp_path / "features.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE features (
          contig TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
          type TEXT, strand TEXT, score REAL, name TEXT,
          feature_id TEXT, parent TEXT, biotype TEXT, attributes TEXT,
          parent_status TEXT NOT NULL DEFAULT 'root', depth INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    con.executemany(
        "INSERT INTO features (contig, start, end, type, feature_id, parent) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    con.execute("CREATE INDEX ix_features_feature_id ON features(feature_id)")
    con.execute("CREATE INDEX ix_features_parent ON features(parent)")
    con.commit()
    con.close()
    return db_path


def _statuses(db_path):
    con = sqlite3.connect(db_path)
    try:
        return dict(
            con.execute(
                "SELECT COALESCE(feature_id, parent), parent_status FROM features"
            ).fetchall()
        )
    finally:
        con.close()


def test_a_record_with_no_parent_is_root(tmp_path):
    db = _build(tmp_path, [("chr1", 1, 100, "gene", "g1", None)])
    resolve_hierarchy(db_path=db)
    assert _statuses(db)["g1"] == "root"


def test_a_record_whose_parent_exists_is_resolved(tmp_path):
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "g1", None),
        ("chr1", 1, 50, "exon", "e1", "g1"),
    ])
    resolve_hierarchy(db_path=db)
    assert _statuses(db)["e1"] == "resolved"


def test_a_record_whose_parent_does_not_exist_is_dangling(tmp_path):
    db = _build(tmp_path, [("chr1", 1, 50, "exon", "e1", "nosuchgene")])
    resolve_hierarchy(db_path=db)
    assert _statuses(db)["e1"] == "dangling"


def test_a_parent_matching_two_rows_is_ambiguous(tmp_path):
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "dup", None),
        ("chr2", 1, 100, "gene", "dup", None),
        ("chr1", 1, 50, "exon", "e1", "dup"),
    ])
    resolve_hierarchy(db_path=db)
    assert _statuses(db)["e1"] == "ambiguous"


def test_a_record_parented_to_itself_is_self(tmp_path):
    db = _build(tmp_path, [("chr1", 1, 100, "gene", "g1", "g1")])
    resolve_hierarchy(db_path=db)
    assert _statuses(db)["g1"] == "self"


def test_a_two_node_cycle_is_cyclic(tmp_path):
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "a", "b"),
        ("chr1", 1, 100, "gene", "b", "a"),
    ])
    resolve_hierarchy(db_path=db)
    assert _statuses(db) == {"a": "cyclic", "b": "cyclic"}


def test_depth_counts_from_zero_at_the_root(tmp_path):
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "g1", None),
        ("chr1", 1, 80, "mRNA", "t1", "g1"),
        ("chr1", 1, 50, "exon", "e1", "t1"),
    ])
    resolve_hierarchy(db_path=db)
    con = sqlite3.connect(db)
    depths = dict(con.execute("SELECT feature_id, depth FROM features").fetchall())
    con.close()
    assert depths == {"g1": 0, "t1": 1, "e1": 2}


def test_unresolvable_records_are_stored_at_the_cap(tmp_path):
    db = _build(tmp_path, [("chr1", 1, 50, "exon", "e1", "nosuchgene")])
    resolve_hierarchy(db_path=db)
    con = sqlite3.connect(db)
    depth = con.execute("SELECT depth FROM features").fetchone()[0]
    con.close()
    assert depth == DEPTH_CAP


def test_counts_reconcile_with_every_row_stored(tmp_path):
    """AH-10: nothing is dropped, whatever its status."""
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "g1", None),
        ("chr1", 1, 50, "exon", "e1", "g1"),
        ("chr1", 1, 50, "exon", "e2", "nosuchgene"),
        ("chr1", 1, 100, "gene", "s1", "s1"),
    ])
    result = resolve_hierarchy(db_path=db)
    counts = result["counts"]
    assert sum(counts.values()) == 4
    assert counts == {"root": 1, "resolved": 1, "dangling": 1, "self": 1}


def test_max_depth_ignores_unresolved_sentinels(tmp_path):
    """AH-14: an unresolved row sits at the cap, which is not a tree depth."""
    db = _build(tmp_path, [
        ("chr1", 1, 100, "gene", "g1", None),
        ("chr1", 1, 80, "mRNA", "t1", "g1"),
        ("chr1", 1, 50, "exon", "e1", "nosuchtranscript"),
    ])
    assert resolve_hierarchy(db_path=db)["max_depth"] == 1


def _genes(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return {r["feature_id"]: dict(r) for r in con.execute("SELECT * FROM genes")}
    finally:
        con.close()


def _three_level(tmp_path):
    """One gene, two transcripts, three exons -- one exon shared."""
    return _build(tmp_path, [
        ("chr1", 100, 900, "gene", "g1", None),
        ("chr1", 100, 500, "mRNA", "t1", "g1"),
        ("chr1", 400, 900, "mRNA", "t2", "g1"),
        ("chr1", 100, 200, "exon", "e1", "t1"),
        ("chr1", 400, 500, "exon", "shared", "t1"),
        ("chr1", 400, 500, "exon", "shared", "t2"),
        ("chr1", 800, 900, "exon", "e3", "t2"),
    ])


def test_typed_mode_stores_one_row_per_gene_typed_feature(tmp_path):
    db = _three_level(tmp_path)
    resolve_hierarchy(db_path=db)
    result = build_gene_table(db_path=db)
    assert result["mode"] == "typed"
    assert list(_genes(db)) == ["g1"]


def test_fallback_mode_stores_one_row_per_root(tmp_path):
    """AH-19: a flat Bakta-shaped file has no gene rows to page over."""
    db = _build(tmp_path, [
        ("chr1", 1, 100, "CDS", "c1", None),
        ("chr1", 200, 300, "CDS", "c2", None),
    ])
    resolve_hierarchy(db_path=db)
    result = build_gene_table(db_path=db)
    assert result["mode"] == "fallback"
    assert sorted(_genes(db)) == ["c1", "c2"]


def test_a_gene_carries_its_direct_child_count(tmp_path):
    db = _three_level(tmp_path)
    resolve_hierarchy(db_path=db)
    build_gene_table(db_path=db)
    assert _genes(db)["g1"]["child_count"] == 2


def test_a_shared_descendant_counts_once(tmp_path):
    """AH-36: two paths to one exon is one exon."""
    db = _three_level(tmp_path)
    resolve_hierarchy(db_path=db)
    build_gene_table(db_path=db)
    # t1, t2, e1, shared, e3 -- the shared exon reached twice, counted once.
    assert _genes(db)["g1"]["descendant_count"] == 5


def test_a_duplicated_direct_child_counts_once(tmp_path):
    """A malformed Parent=G,G stores two relationship rows for one child
    (Task 1's parser, Task 3's one-row-per-relationship storage); child_count
    must dedupe the same way descendant_count already does."""
    db = _build(tmp_path, [
        ("chr1", 100, 900, "gene", "g1", None),
        ("chr1", 100, 500, "mRNA", "t1", "g1"),
        ("chr1", 100, 500, "mRNA", "t1", "g1"),
    ])
    resolve_hierarchy(db_path=db)
    build_gene_table(db_path=db)
    gene = _genes(db)["g1"]
    assert gene["child_count"] == 1
    assert gene["descendant_count"] == 1


def test_a_gene_span_covers_its_descendants(tmp_path):
    db = _three_level(tmp_path)
    resolve_hierarchy(db_path=db)
    build_gene_table(db_path=db)
    gene = _genes(db)["g1"]
    assert (gene["span_start"], gene["span_end"]) == (100, 900)


def test_query_genes_pages_in_position_order(tmp_path):
    db = _build(tmp_path, [
        ("chr1", 500, 600, "gene", "g2", None),
        ("chr1", 100, 200, "gene", "g1", None),
    ])
    resolve_hierarchy(db_path=db)
    build_gene_table(db_path=db)
    rows = query_genes(db_path=db, offset=0, limit=10)
    assert [r["feature_id"] for r in rows] == ["g1", "g2"]


def test_the_gene_mode_is_recorded_at_build_time(tmp_path):
    """The route reads this back rather than recomputing it per page."""
    db = _three_level(tmp_path)
    resolve_hierarchy(db_path=db)
    build_gene_table(db_path=db)
    assert gene_mode(db_path=db) == "typed"
