"""Resolving a feature's declared parent against the file's own ID set.

A record whose Parent named a nonexistent feature used to be invisible in
every view: excluded from the parent page because its parent is not NULL,
and under no expanded row because nothing carries that feature_id. This
module is what makes such a record visible -- it is classified, counted, and
reachable.

Resolution runs as indexed UPDATEs against the built table rather than as a
second pass over the source file. The rejected alternative holds every
distinct ID in a Python set, which on a human GFF3 is hundreds of MB of RSS
at the same moment the worker carries an insert batch. SQLite holds the ID
set on disk, so peak memory here does not scale with the annotation.
"""

import sqlite3
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# The ancestor walk stops here. A fixed cap rather than true cycle detection:
# the walk must not hang on a hostile or pathological file, and a hierarchy
# deeper than a handful of levels does not occur in real annotation data --
# gene -> transcript -> exon is three. A depth-100 chain is a broken file
# whether or not it technically closes into a cycle, and both cases want the
# same treatment. The frontend uses the same constant to bound its recursion.
DEPTH_CAP = 100

# Every status a row can carry. Ordered as resolution assigns them: each pass
# claims rows the previous ones did not, so the order is load-bearing.
STATUSES = ("root", "self", "ambiguous", "dangling", "resolved", "cyclic")

UNRESOLVED_STATUSES = ("dangling", "ambiguous", "self", "cyclic")

# A sentinel outside STATUSES, used only to mark "not yet classified" for the
# duration of resolve_hierarchy. Rows arrive with parent_status defaulted to
# 'root' by the table schema (see annotation_db's DDL and this module's own
# tests), which is itself a real status value -- if the first pass's WHERE
# clause matched only rows still at that default, a genuine root row and an
# unclassified row would be indistinguishable, and a row that no later pass
# claims (e.g. the two ends of a cycle, which never satisfy the dangling
# check because their parent *does* exist) would be silently left at 'root'
# forever instead of falling through to 'cyclic'. Resetting to this sentinel
# first makes every subsequent WHERE NOT IN (...) check unambiguous.
_UNCLASSIFIED = "_unresolved"


def resolve_hierarchy(*, db_path: Path) -> dict:
    """Classify every row's parent reference and assign its depth.

    Returns `{"counts": {status: n}, "max_depth": n}`. The caller stores both
    as facts, and the table's own Unresolved view filters on the same column
    -- one query behind both, so the summary and the table cannot disagree.
    """
    con = sqlite3.connect(db_path)
    try:
        # Every row starts unclassified so later passes can tell "not yet
        # decided" apart from "decided root" -- see _UNCLASSIFIED above.
        con.execute("UPDATE features SET parent_status = ?", (_UNCLASSIFIED,))

        # Order matters: each statement claims only rows still unclassified.
        con.execute(
            "UPDATE features SET parent_status = 'root' WHERE parent IS NULL"
        )
        con.execute(
            "UPDATE features SET parent_status = 'self' "
            "WHERE parent IS NOT NULL AND parent = feature_id"
        )
        # Ambiguous before dangling: a parent naming two rows is a different
        # problem from one naming none, and the duplicate-ID subquery would
        # otherwise be masked by the NOT EXISTS check passing.
        con.execute(
            "UPDATE features SET parent_status = 'ambiguous' "
            "WHERE parent_status NOT IN ('root', 'self') AND parent IN ("
            "  SELECT feature_id FROM features WHERE feature_id IS NOT NULL"
            "  GROUP BY feature_id HAVING COUNT(*) > 1"
            ")"
        )
        con.execute(
            "UPDATE features SET parent_status = 'dangling' "
            "WHERE parent_status NOT IN ('root', 'self', 'ambiguous') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM features p WHERE p.feature_id = features.parent"
            ")"
        )
        con.execute(
            "UPDATE features SET parent_status = 'resolved' "
            "WHERE parent_status NOT IN ('root', 'self', 'ambiguous', 'dangling')"
        )
        con.commit()

        _assign_depths(con)
        con.commit()

        counts = dict(
            con.execute(
                "SELECT parent_status, COUNT(*) FROM features GROUP BY parent_status"
            ).fetchall()
        )
        # Only over rows whose depth is a tree position: an unresolved row
        # sits at the cap as a sentinel (AH-9), and reporting that as the
        # file's depth would say every annotation is 100 levels deep.
        max_depth = con.execute(
            "SELECT MAX(depth) FROM features "
            "WHERE parent_status IN ('root', 'resolved')"
        ).fetchone()[0]
    finally:
        con.close()

    # Logged as a nested dict rather than **counts: a status of 'self' would
    # otherwise collide with the bound logger method's own `self` parameter
    # and raise TypeError instead of logging.
    log.info("annotation_hierarchy_resolved", max_depth=max_depth, counts=counts)
    return {"counts": counts, "max_depth": max_depth or 0}


def _assign_depths(con: sqlite3.Connection) -> None:
    """Walk down from the roots, one level per iteration.

    Level-order rather than per-row recursion: each iteration is a single
    indexed UPDATE over the rows whose parents were assigned last round, so
    the whole walk is DEPTH_CAP statements at worst rather than one per row.

    Anything still unassigned when the cap is reached is part of a cycle or
    hangs off one, and is marked `cyclic`. Rows that failed to resolve at all
    sit at the cap too -- see AH-9: for them the number is a sentinel, not a
    position in a tree.
    """
    con.execute(
        f"UPDATE features SET depth = {DEPTH_CAP} "
        "WHERE parent_status != 'root'"
    )
    con.execute("UPDATE features SET depth = 0 WHERE parent_status = 'root'")

    for level in range(1, DEPTH_CAP):
        changed = con.execute(
            "UPDATE features SET depth = ? "
            "WHERE parent_status = 'resolved' AND depth = ? AND parent IN ("
            "  SELECT feature_id FROM features WHERE depth = ?"
            ")",
            (level, DEPTH_CAP, level - 1),
        ).rowcount
        if not changed:
            break

    # Resolved rows never reached by the walk are in or below a cycle.
    con.execute(
        "UPDATE features SET parent_status = 'cyclic' "
        f"WHERE parent_status = 'resolved' AND depth = {DEPTH_CAP}"
    )


# What counts as a gene when the file says so. Kept small and explicit: a
# type not in here is not silently treated as a gene, because a view labelled
# Genes that lists something else is the failure this feature exists to
# prevent. Extend deliberately, not by pattern-matching on substrings.
GENE_TYPES = ("gene", "pseudogene", "ncRNA_gene")


def build_gene_table(*, db_path: Path) -> dict:
    """One row per gene, with the counts the Genes view shows.

    Stored rather than computed on expand: per-row subtree counts mean
    walking two levels down for every visible row on every page turn, which
    degrades exactly on the large files where the view earns its place. The
    table is O(genes), not O(features).

    Returns the mode and the row count. `mode` is `typed` when the file has
    gene-typed features and `fallback` when it has none -- the frontend says
    which, so a file whose roots are NCBI `region` records does not present
    a list of contigs under a heading that says Genes.
    """
    con = sqlite3.connect(db_path)
    try:
        con.execute("DROP TABLE IF EXISTS genes")
        con.execute(
            """
            CREATE TABLE genes (
              feature_id TEXT,
              contig     TEXT NOT NULL,
              start      INTEGER NOT NULL,
              end        INTEGER NOT NULL,
              type       TEXT,
              strand     TEXT,
              name       TEXT,
              biotype    TEXT,
              child_count      INTEGER NOT NULL DEFAULT 0,
              descendant_count INTEGER NOT NULL DEFAULT 0,
              span_start INTEGER NOT NULL,
              span_end   INTEGER NOT NULL
            )
            """
        )

        placeholders = ",".join("?" for _ in GENE_TYPES)
        typed = con.execute(
            f"SELECT COUNT(*) FROM features WHERE type IN ({placeholders})",
            GENE_TYPES,
        ).fetchone()[0]

        if typed:
            mode = "typed"
            where, args = f"type IN ({placeholders})", list(GENE_TYPES)
        else:
            mode = "fallback"
            where, args = "parent_status = 'root'", []

        con.execute(
            f"""
            INSERT INTO genes (feature_id, contig, start, end, type, strand,
                               name, biotype, span_start, span_end)
            SELECT feature_id, contig, start, end, type, strand, name, biotype,
                   start, end
            FROM features WHERE {where}
            """,
            args,
        )
        con.execute("CREATE INDEX ix_genes_locus ON genes(contig, start)")
        con.commit()

        # The mode is stored, not recomputed. The route needs it on every
        # page request, and re-running the type count there would scan
        # `features` to answer a question already settled at build time.
        con.execute(
            "CREATE TABLE gene_meta (mode TEXT NOT NULL, gene_count INTEGER NOT NULL)"
        )
        con.commit()

        count = con.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        con.execute("INSERT INTO gene_meta VALUES (?, ?)", (mode, count))
        _fill_gene_counts(con)
        con.commit()
    finally:
        con.close()

    log.info("annotation_gene_table_built", mode=mode, genes=count)
    return {"mode": mode, "count": count}


def gene_mode(*, db_path: Path) -> str:
    """Which rule built the genes table, as recorded at build time.

    Falls back to `typed` when the meta table is missing, which is the shape
    of a database built before this feature -- the route stays serving
    rather than 500ing on a stale artifact the user has not recomputed.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT mode FROM gene_meta").fetchone()
        return row[0] if row else "typed"
    except sqlite3.OperationalError:
        return "typed"
    finally:
        con.close()


def _fill_gene_counts(con: sqlite3.Connection) -> None:
    """Descendant counts and spans, one gene at a time.

    The walk keeps a `seen` set per gene, which is what makes a descendant
    reached by two paths count once (AH-36) -- a shared exon under two
    transcripts of the same gene is one exon. Bounded by DEPTH_CAP so a cycle
    that survived resolution cannot spin here.

    Per-gene rather than one sweeping query because the de-duplication is not
    expressible as a GROUP BY over a join: the same row reached twice has to
    be recognised as the same row, not counted twice.
    """
    genes = con.execute(
        "SELECT rowid, feature_id, start, end FROM genes WHERE feature_id IS NOT NULL"
    ).fetchall()

    for rowid, feature_id, start, end in genes:
        seen: set[str] = set()
        span_start, span_end = start, end
        frontier = [feature_id]
        child_count = 0

        for level in range(DEPTH_CAP):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            children = con.execute(
                f"SELECT feature_id, start, end FROM features "
                f"WHERE parent IN ({placeholders})",
                frontier,
            ).fetchall()

            next_frontier: list[str] = []
            for child_id, c_start, c_end in children:
                span_start = min(span_start, c_start)
                span_end = max(span_end, c_end)
                # A child with no ID of its own (GTF exons) is a leaf: it
                # counts, but nothing can hang off it. Keyed by identity so
                # two such rows are two descendants.
                key = child_id or f"\x00leaf:{c_start}:{c_end}"
                if key in seen:
                    continue
                seen.add(key)
                if child_id:
                    next_frontier.append(child_id)
            frontier = next_frontier

            # Read after the dedup loop, not from the raw row count: a
            # feature listed twice as a direct child (a malformed
            # `Parent=G,G` produces two stored relationship rows for one
            # feature_id, per Task 3's one-row-per-relationship storage)
            # must count as one child, the same way descendant_count already
            # collapses it via `seen`.
            if level == 0:
                child_count = len(seen)

        con.execute(
            "UPDATE genes SET child_count = ?, descendant_count = ?, "
            "span_start = ?, span_end = ? WHERE rowid = ?",
            (child_count, len(seen), span_start, span_end, rowid),
        )


def query_genes(*, db_path: Path, offset: int, limit: int) -> list[dict]:
    """One page of the Genes view, in position order.

    Ordered explicitly, unlike `query_features`: the genes table is built by
    a SELECT whose order is not guaranteed to be file order, and it is small
    enough (O(genes)) that the sort is cheap.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT * FROM genes ORDER BY contig, start LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def count_genes(*, db_path: Path) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
    finally:
        con.close()
