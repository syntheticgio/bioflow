"""Reading flow-cell coordinates out of read headers, and summarising quality
by tile.

Kept separate from the job handler for the same reason `fastp_runner` is: the
parts worth testing are pure functions over strings, with no queue and no
filesystem involved.

Only Illumina headers carry a tile. Nanopore writes a read UUID and channel,
PacBio writes a ZMW hole number, and the SRA toolkit strips machine headers
entirely in favour of an accession. All three must parse to None -- a wrong
tile number is worse than no tile number, because it produces a plausible
heatmap of nothing.
"""

from dataclasses import dataclass

# Field indices within the colon-delimited header. The instrument, run, and
# flowcell fields ahead of these are not used, but their presence is what
# makes index 4 the tile rather than something else -- hence the length check.
_TILE_FIELD = 4
_X_FIELD = 5
_Y_FIELD = 6
_MIN_FIELDS = 7


@dataclass(frozen=True)
class ReadPosition:
    """Where on the flow cell a single read's cluster sat."""

    tile: int
    x: int
    y: int


def parse_header(header: str) -> ReadPosition | None:
    """Extract tile and x/y from an Illumina header, or None if it is not one.

    Returns None rather than raising: a file whose headers do not carry tiles
    is an ordinary, expected input, not an error.
    """
    if not header:
        return None

    # The read/filter/control/index fields after the space are not needed.
    name = header.split(" ", 1)[0].lstrip("@")
    fields = name.split(":")
    if len(fields) < _MIN_FIELDS:
        return None

    try:
        return ReadPosition(
            tile=int(fields[_TILE_FIELD]),
            x=int(fields[_X_FIELD]),
            y=int(fields[_Y_FIELD]),
        )
    except ValueError:
        # A colon-bearing header that is not Illumina's -- shape matched, the
        # contents did not.
        return None
