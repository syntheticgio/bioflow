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


def per_contig(joined: list[JoinedWindow]) -> list[dict]:
    """One row per contig: GC (base-weighted, per V3 -- Σgc_count/Σwidth,
    reconstructing each window's approximate G/C base count from its stored
    percentage since gc_tracks does not retain the raw count), mean depth
    (width-weighted, same reasoning as bias_curve), total length, and how
    many windows contributed.

    Feeds #641's blobplot: each contig becomes one scatter point, GC on one
    axis and mean_depth on the other, point area proportional to length.

    Note: JoinedWindow stores gc as a percentage (not a raw G/C base count),
    so we reconstruct an approximate G/C count as round(gc / 100 * width)
    per window. This introduces float rounding error proportional to window
    count, but is acceptable since gc_tracks' own stored value is already
    rounded to 2 decimals.
    """
    by_contig: dict[str, list[JoinedWindow]] = {}
    for w in joined:
        by_contig.setdefault(w["contig"], []).append(w)

    rows = []
    for contig, windows in by_contig.items():
        gc_bases = 0.0
        gc_total_bases = 0.0
        depth_sum = 0.0
        width_sum = 0
        for w in windows:
            width_sum += w["width"]
            depth_sum += w["depth"] * w["width"]
            if w["gc"] is not None:
                gc_bases += (w["gc"] / 100.0) * w["width"]
                gc_total_bases += w["width"]
        rows.append({
            "contig": contig,
            "gc": round(gc_bases / gc_total_bases * 100.0, 2) if gc_total_bases else None,
            "mean_depth": round(depth_sum / width_sum, 4) if width_sum else 0.0,
            "length": width_sum,
            "window_count": len(windows),
        })
    return rows


def cap_by_cumulative_length(
    contigs: list[dict], *, target_fraction: float = 0.99, hard_ceiling: int = 5000
) -> tuple[list[dict], int]:
    """Keep the longest contigs covering `target_fraction` of total bases,
    capped at `hard_ceiling` regardless.

    Sorted descending by length, so the cut point is always the shortest
    contigs -- a contaminant that is many SMALL contigs is exactly what this
    can drop (V4), which is why the dropped count is returned rather than
    only logged: the caller must always be able to say what was omitted.
    """
    by_length = sorted(contigs, key=lambda c: c["length"], reverse=True)
    total = sum(c["length"] for c in by_length)
    if total <= 0:
        return [], 0

    target = total * target_fraction
    kept = []
    cumulative = 0
    for c in by_length:
        if len(kept) >= hard_ceiling:
            break
        kept.append(c)
        cumulative += c["length"]
        if cumulative >= target:
            break

    return kept, len(contigs) - len(kept)
