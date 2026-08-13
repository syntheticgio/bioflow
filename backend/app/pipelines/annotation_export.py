"""Exporting a filtered subset of an annotation as a new file.

The rule that governs this module: **never rebuild a feature line from
`Feature`**. `parse_gff_line` keeps neither the GFF `source` column nor
`phase`, and `parse_bed_line` converts BED to one-based coordinates, so a
reconstructed line would silently lose reading frame. Export copies original
bytes.

That is why the unit of work here is a *line number* rather than a feature.
`closure_lines` decides which lines belong in the output; `write_subset`
copies them out of the source verbatim.
"""

import sqlite3
from pathlib import Path

from app.logging import get_logger
from app.pipelines import annotation_db
from app.pipelines.annotation_hierarchy import DEPTH_CAP

log = get_logger(__name__)


def closure_lines(
    *, db_path: Path, filters: annotation_db.FeatureFilters
) -> set[int]:
    """Source line numbers for every feature the export must contain.

    That is the filter's matches plus every ancestor and every descendant of
    a match (AE-5 through AE-7). A filter matches *rows*, but a valid
    annotation needs whole trees: exporting a gene without its exons leaves
    `Parent=` references dangling, and exporting exons without their gene
    leaves orphans.

    `top_level_only` is forced off (AE-10). It is how the table pages, not a
    statement about content, and honouring it would exclude exactly the child
    rows the closure exists to re-add.

    Both walks are bounded by DEPTH_CAP for the reason `_assign_depths`
    documents: a file whose parent references form a cycle is a real thing,
    and the walk must terminate on one (AE-9).

    Rows with no line number contribute nothing -- that is GenBank, whose
    features span several lines (AE-2).
    """
    # The export never pages, so the table's paging flag must not narrow it.
    filters = annotation_db.FeatureFilters(
        contig=filters.contig,
        start_min=filters.start_min,
        start_max=filters.start_max,
        feature_type=filters.feature_type,
        biotype=filters.biotype,
        name_query=filters.name_query,
        strand=filters.strand,
        top_level_only=False,
        parent_status=filters.parent_status,
    )
    where, args = annotation_db._where(filters)

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # The seed: rowids of every matched row. rowid rather than feature_id
        # because a row may have neither a feature_id nor a parent (BED), and
        # it still has to reach the output.
        seed = [r[0] for r in con.execute(f"SELECT rowid FROM features{where}", args)]
        if not seed:
            return set()

        lines: set[int] = set()
        placeholders = ",".join("?" for _ in seed)

        # Matched rows themselves (AE-5).
        for (line,) in con.execute(
            f"SELECT line FROM features WHERE rowid IN ({placeholders}) "
            "AND line IS NOT NULL",
            seed,
        ):
            lines.add(line)

        # Descendants (AE-6). Walks feature_id -> parent, level by level, so
        # the cap counts tree depth rather than rows visited.
        frontier = {
            r[0]
            for r in con.execute(
                f"SELECT feature_id FROM features WHERE rowid IN ({placeholders}) "
                "AND feature_id IS NOT NULL",
                seed,
            )
        }
        seen_down = set(frontier)
        for _ in range(DEPTH_CAP):
            if not frontier:
                break
            ph = ",".join("?" for _ in frontier)
            rows = con.execute(
                f"SELECT feature_id, line FROM features WHERE parent IN ({ph})",
                list(frontier),
            ).fetchall()
            nxt = set()
            for fid, line in rows:
                if line is not None:
                    lines.add(line)
                if fid is not None and fid not in seen_down:
                    seen_down.add(fid)
                    nxt.add(fid)
            frontier = nxt

        # Ancestors (AE-7). Walks parent -> feature_id, the other direction.
        frontier = {
            r[0]
            for r in con.execute(
                f"SELECT parent FROM features WHERE rowid IN ({placeholders}) "
                "AND parent IS NOT NULL",
                seed,
            )
        }
        seen_up = set(frontier)
        for _ in range(DEPTH_CAP):
            if not frontier:
                break
            ph = ",".join("?" for _ in frontier)
            rows = con.execute(
                f"SELECT parent, line FROM features WHERE feature_id IN ({ph})",
                list(frontier),
            ).fetchall()
            nxt = set()
            for parent, line in rows:
                if line is not None:
                    lines.add(line)
                if parent is not None and parent not in seen_up:
                    seen_up.add(parent)
                    nxt.add(parent)
            frontier = nxt

        return lines
    finally:
        con.close()


class StaleIndexError(Exception):
    """The source file no longer matches the index that describes it.

    Raised when a line about to be emitted does not parse to the feature the
    index recorded for that line number. The caller turns this into a
    PermanentError: re-running cannot help, because the index must be
    recomputed first.
    """


# Pragmas that describe the whole source file and would be wrong on a subset.
_DROPPED_PRAGMA_PREFIXES = ("##sequence-region",)

_PARSERS = {
    "gff": "parse_gff_line",
    "gtf": "parse_gtf_line",
    "bed": "parse_bed_line",
}


def _header_for(header: list[str], fmt: str) -> list[str]:
    """The comment lines the output should carry.

    A GFF3 file without `##gff-version` is not valid GFF3, so one is
    synthesized when the source did not have it (AE-13). GTF and BED have no
    mandatory header and get nothing invented (AE-13a).
    """
    kept = [
        line for line in header
        if not line.startswith(_DROPPED_PRAGMA_PREFIXES)
    ]
    if fmt == "gff" and not any(ln.startswith("##gff-version") for ln in kept):
        kept.insert(0, "##gff-version 3")
    return kept


def write_subset(
    *,
    source: Path,
    dest: Path,
    db_path: Path,
    lines: set[int],
    header: list[str],
    fmt: str,
) -> int:
    """Copy `lines` out of `source` into `dest`, verbatim and verified.

    Iterates the *source* and emits lines whose number is in the set, rather
    than iterating the set and seeking. That is what makes source order a
    structural property rather than something a later change could regress
    (AE-12), and it means one sequential pass over a file rather than a seek
    per feature.

    Every emitted line is re-parsed and checked against what the index
    recorded for that line number. A mismatch means the file changed under
    the index, and the export must fail rather than emit a plausible, wrong
    file -- see StaleIndexError (AE-14).

    Returns the number of feature lines written.
    """
    from app.pipelines import annotation_parse
    from app.queue.annotation_handlers import _open_text

    parse_line = getattr(annotation_parse, _PARSERS[fmt])

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        expected = {
            line: (contig, start, end)
            for line, contig, start, end in con.execute(
                "SELECT DISTINCT line, contig, start, end FROM features "
                "WHERE line IS NOT NULL"
            )
        }
    finally:
        con.close()

    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0

    with open(dest, "w") as out:
        for pragma in _header_for(header, fmt):
            out.write(pragma + "\n")

        with _open_text(source) as fh:
            for i, raw in enumerate(fh, start=1):
                if i not in lines:
                    continue
                stripped = raw.rstrip("\n")
                feature = parse_line(stripped)
                if feature is None:
                    raise StaleIndexError(
                        f"line {i} of {source.name} no longer parses as a "
                        f"{fmt} feature; the annotation index is out of date"
                    )
                want = expected.get(i)
                if want is not None and (
                    feature.contig, feature.start, feature.end
                ) != want:
                    raise StaleIndexError(
                        f"line {i} of {source.name} is "
                        f"{feature.contig}:{feature.start}-{feature.end}, but "
                        f"the index recorded {want[0]}:{want[1]}-{want[2]}; "
                        "the annotation index is out of date"
                    )
                out.write(stripped + "\n")
                written += 1

    return written
