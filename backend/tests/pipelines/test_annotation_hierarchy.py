"""Parent resolution: which records have a real parent, and which do not.

The point of this module is that a record whose Parent names nothing is
*visible* rather than silently dropped, so most of these tests assert on
counts that must reconcile with the number of rows put in.
"""

import sqlite3

from app.pipelines.annotation_hierarchy import DEPTH_CAP, resolve_hierarchy


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
