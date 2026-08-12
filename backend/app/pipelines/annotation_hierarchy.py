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
