"""Geometry for the annotation track viewer.

Pure functions over coordinates: no database, no I/O. Kept out of
annotation_db.py because that module is where SQL lives, and packing is not
a query.
"""

# Rows drawn per strand before the track stops growing. A dense locus would
# otherwise push the feature table off the page; the viewer reports the
# overflow instead. See the spec's row-packing section.
MAX_ROWS_PER_STRAND = 12


def pack_rows(
    items: list[tuple[int, int]], *, max_rows: int = MAX_ROWS_PER_STRAND
) -> list[int | None]:
    """Assign each (start, end) a row index so no two on a row overlap.

    Greedy by input order, which the callers supply in coordinate order: the
    first row whose last feature ends before this one starts wins. Returns
    `None` for a feature that would need a row beyond `max_rows` -- the
    caller counts those and renders "+N more" rather than growing.

    Coordinates are treated as inclusive on both ends, matching the index:
    a feature ending at 200 and one starting at 200 share that base and
    therefore cannot share a row.
    """
    row_ends: list[int] = []
    out: list[int | None] = []

    for start, end in items:
        placed: int | None = None
        for i, row_end in enumerate(row_ends):
            if start > row_end:
                row_ends[i] = end
                placed = i
                break
        else:
            if len(row_ends) < max_rows:
                row_ends.append(end)
                placed = len(row_ends) - 1
        out.append(placed)

    return out
