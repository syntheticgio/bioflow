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


class ExportMismatch(Exception):
    """A source line no longer parses to the feature the index recorded.

    Raised rather than skipped: a subset that quietly drops or substitutes
    features is a wrong-but-plausible annotation file, which is worse than
    no file at all.
    """


# The filters that make a readable name, in the order they are tried. Kept
# to the ones a person would actually say out loud about a subset.
_NAME_KEYS = ("contig", "feature_type", "biotype", "strand")

# Every suffix that is part of an annotation's name rather than a filter
# slot, so `a.gff3.gz` keeps both.
_COMPOUND_SUFFIXES = (".gz", ".bgz")


def subset_name(source_name: str, active: dict) -> str:
    """The exported file's name: the source's, with up to two filters.

    Past two the name stops being readable, so it falls back to `subset`.
    The complete filter is recorded in the object's facts either way, so
    nothing is lost -- this only decides what is legible in a file list.
    """
    stem = source_name
    suffixes = ""
    for compound in _COMPOUND_SUFFIXES:
        if stem.endswith(compound):
            suffixes = compound + suffixes
            stem = stem[: -len(compound)]
            break
    if "." in stem:
        stem, _, ext = stem.rpartition(".")
        suffixes = f".{ext}" + suffixes

    parts = [str(active[k]) for k in _NAME_KEYS if active.get(k)]
    label = ".".join(parts) if 0 < len(parts) <= 2 else "subset"
    return f"{stem}.{label}{suffixes}"


def write_subset(
    *,
    source: Path,
    dest: Path,
    lines: set[int],
    verify: dict[int, dict] | None,
    parse_line=None,
) -> int:
    """Copy `lines` from `source` to `dest`, header first, in file order.

    One sequential pass rather than seeking per line: the lines are spread
    through the file and a 3M-line GFF3 read once is cheaper than tens of
    thousands of seeks.

    `verify` maps a line number to the contig/start/end the index recorded
    for it. Each selected line is re-parsed and compared; a disagreement
    raises. Pass None only in tests that are exercising emission itself.

    `parse_line` is the format-appropriate parser (parse_gff_line,
    parse_gtf_line, or parse_bed_line) used to re-parse each selected line
    for verification. Defaults to parse_gff_line for backward compatibility
    with callers that don't yet pass it explicitly; a caller handling GTF or
    BED must pass the matching parser, or verification will spuriously fail
    every line -- the formats are structurally different, not just
    differently named.

    Headers are read from the file directly rather than through
    `run_annotation_stats`'s `_HEADER_SCAN_LINES`, which bounds what is
    *displayed* and would silently truncate a long ##sequence-region block.

    Returns the number of feature lines written.
    """
    from app.pipelines import annotation_parse

    written = 0
    with open(source, errors="replace") as fh, open(dest, "w") as out:
        in_header = True
        for i, line in enumerate(fh, start=1):
            stripped = line.rstrip("\n")
            if in_header and stripped.startswith("#"):
                out.write(line if line.endswith("\n") else line + "\n")
                continue
            if stripped:
                in_header = False
            if i not in lines:
                continue
            if verify is not None:
                expected = verify.get(i)
                parser = parse_line or annotation_parse.parse_gff_line
                parsed = parser(stripped, i)
                if expected is not None and (
                    parsed is None
                    or parsed.contig != expected["contig"]
                    or parsed.start != expected["start"]
                    or parsed.end != expected["end"]
                ):
                    raise ExportMismatch(
                        f"line {i} of {source.name} no longer matches the "
                        f"computed index; recompute results and try again"
                    )
            out.write(line if line.endswith("\n") else line + "\n")
            written += 1
    return written
