"""Join reference GC (gc_tracks) against alignment depth (mosdepth) on their
shared per-window grid, and aggregate the join two ways.

A pure module: no queue, no filesystem, no subprocess. gc_tracks and
mosdepth already tile every contig identically (`window_count = min(
WINDOW_COUNT, length // MIN_WINDOW_BASES)` per contig, mosdepth_runner.py
imports the constants rather than redeclaring them), so the join is a
`(contig, window_index)` key lookup -- no resampling, no second reference
scan.

Windows vary in physical width across contigs (a contig with fewer windows
than WINDOW_COUNT has wider ones), so every aggregation here weights by
`width` rather than averaging window values directly. See `bias_curve`'s
docstring and its test for why the unweighted mean is a real bug, not a
simplification: it silently over-weights short-contig windows on exactly the
fragmented assemblies where these plots matter most.
"""

from __future__ import annotations

from typing import TypedDict


class JoinedWindow(TypedDict):
    contig: str
    index: int
    start: int
    end: int
    width: int
    gc: float | None
    depth: float


def join_windows(
    gc_contigs: list[dict], depth_regions: dict[str, list[dict]]
) -> list[JoinedWindow]:
    """Join gc_tracks' per-contig window arrays against mosdepth's per-contig
    region rows, keyed by (contig, window index).

    A window present in the reference GC but with no matching depth row
    (mosdepth found no windows for a contig -- e.g. it was too short for
    ANY window, or the BAM had zero reads on it) resolves to depth 0.0,
    never a dropped window: a real GC dropout must read as "no coverage
    here", not silently vanish and bias the curve upward by omission.

    Contigs present only in depth_regions (should not happen -- mosdepth
    windows are built from the same reference's .fai -- but a mismatched
    reference input is not this function's job to detect) are ignored:
    there is no GC to join them to.
    """
    joined: list[JoinedWindow] = []
    for contig in gc_contigs:
        name = contig["name"]
        gc_list = contig["gc"]
        rows = depth_regions.get(name)
        if rows and len(rows) == len(gc_list):
            # Depth rows carry their own (start, end) -- mosdepth's own
            # source of truth, not re-derived from window_bases, so a
            # future divergence between the two tilings would show up as a
            # length mismatch (the `else` branch below) rather than being
            # silently masked by recomputing bounds independently.
            for i, (row, gc) in enumerate(zip(rows, gc_list, strict=True)):
                joined.append(JoinedWindow(
                    contig=name, index=i,
                    start=row["start"], end=row["end"],
                    width=row["end"] - row["start"],
                    gc=gc, depth=row["depth"],
                ))
        else:
            # No depth rows for this contig (zero aligned reads, or a
            # length mismatch that should not happen against the same
            # reference) -- reconstruct window bounds from gc_tracks' own
            # tiling rule and default depth to 0. Mirrors
            # mosdepth_runner.build_windows_bed's rule exactly: the last
            # window absorbs the remainder.
            window_bases = contig["window_bases"]
            length = contig["length"]
            window_count = len(gc_list)
            for i, gc in enumerate(gc_list):
                start = i * window_bases
                end = length if i == window_count - 1 else start + window_bases
                joined.append(JoinedWindow(
                    contig=name, index=i, start=start, end=end,
                    width=end - start, gc=gc, depth=0.0,
                ))
    return joined


def bias_curve(joined: list[JoinedWindow], *, bins: int = 20) -> list[dict]:
    """Aggregate joined windows into `bins` fixed-width GC bins, 0-100%.

    Each bin's value is the width-weighted mean depth of every window whose
    GC falls in that bin: `sum(depth * width) / sum(width)`, NOT
    `mean(depth)`. A window with gc=None (an all-N stretch gc_tracks could
    not score) is excluded from every bin -- it has no GC to bin by.

    Empty bins are omitted from the result, not zero-filled: a bias curve is
    read as a line through the GC values that were actually observed, and a
    zero-depth bin at an unobserved GC value would misrepresent a gap as
    "sequenced here at zero depth".
    """
    bin_width = 100.0 / bins
    sums: dict[int, float] = {}
    widths: dict[int, float] = {}
    counts: dict[int, int] = {}

    for w in joined:
        if w["gc"] is None:
            continue
        idx = min(int(w["gc"] / bin_width), bins - 1)
        sums[idx] = sums.get(idx, 0.0) + w["depth"] * w["width"]
        widths[idx] = widths.get(idx, 0.0) + w["width"]
        counts[idx] = counts.get(idx, 0) + 1

    result = []
    for idx in sorted(sums):
        result.append({
            "gc_min": round(idx * bin_width, 2),
            "gc_max": round((idx + 1) * bin_width, 2),
            "mean_depth": round(sums[idx] / widths[idx], 4),
            "window_count": counts[idx],
        })
    return result
