"""Exporting a filtered slice of an annotation as a new file.

The constraint everything here follows from: original source lines are
re-emitted, never reconstructed. `annotation_parse.Feature` stores neither
the GFF `source` column nor `phase`, and converts BED to one-based, so a
rebuilt CDS line would carry a `.` where a reading frame belongs -- valid
syntax, wrong biology, and silent. So the unit of export is a line number,
not a feature.
"""

import sqlite3
from pathlib import Path

from app.pipelines.annotation_db import FeatureFilters, _connect, _where
from app.pipelines.annotation_hierarchy import DEPTH_CAP


def closure_lines(*, db_path: Path, filters: FeatureFilters) -> set[int]:
    """Source lines of every matched feature, its ancestors, and its
    descendants.

    Ancestors are not optional: a `Parent=` reference to a feature absent
    from the output makes the file fail in downstream tools. Descendants
    are not either -- a gene without its transcripts is valid and useless.

    Walked level by level rather than with a recursive CTE, matching
    `_assign_depths`: the DEPTH_CAP bound then counts tree depth in the
    same units the rest of the module does, and a cycle terminates at the
    cap instead of recursing.

    Features with no line number (GenBank's multi-line and synthetic rows)
    are skipped -- they are not addressable and cannot be re-emitted.
    """
    where, args = _where(filters)
    con = _connect(db_path)
    try:
        matched_ids = {
            row[0]
            for row in con.execute(
                f"SELECT feature_id FROM features{where}", args
            )
            if row[0]
        }
        lines = {
            row[0]
            for row in con.execute(
                f"SELECT line_no FROM features{where}", args
            )
            if row[0] is not None
        }

        lines |= _walk(con, matched_ids, "up")
        lines |= _walk(con, matched_ids, "down")
    finally:
        con.close()
    return lines


def _walk(con: sqlite3.Connection, seed_ids: set[str], direction: str) -> set[int]:
    """Line numbers reached from `seed_ids` by following parent links.

    `up` follows each row's `parent` to its parent's row; `down` finds rows
    whose `parent` is one of the frontier. Bounded by DEPTH_CAP levels, and
    by `seen` so a cycle cannot revisit a node.
    """
    lines: set[int] = set()
    seen: set[str] = set(seed_ids)
    frontier = set(seed_ids)

    for _ in range(DEPTH_CAP):
        if not frontier:
            break
        placeholders = ",".join("?" for _ in frontier)
        if direction == "up":
            sql = (
                f"SELECT parent.feature_id, parent.line_no FROM features child "
                f"JOIN features parent ON parent.feature_id = child.parent "
                f"WHERE child.feature_id IN ({placeholders})"
            )
        else:
            sql = (
                f"SELECT feature_id, line_no FROM features "
                f"WHERE parent IN ({placeholders})"
            )
        rows = con.execute(sql, list(frontier)).fetchall()

        next_frontier: set[str] = set()
        for feature_id, line_no in rows:
            if line_no is not None:
                lines.add(line_no)
            if feature_id and feature_id not in seen:
                seen.add(feature_id)
                next_frontier.add(feature_id)
        frontier = next_frontier

    return lines
