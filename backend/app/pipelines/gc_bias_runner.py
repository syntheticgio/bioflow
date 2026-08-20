"""Coverage-versus-GC bias curve: normalized mean coverage per GC-content bin.

Reuses the same window grid as ``gc_tracks`` and ``mosdepth``
(``WINDOW_COUNT``, ``MIN_WINDOW_BASES``), so per-window depth and per-window
GC are already computed on the same grid and the join is a (contig,
window_index) lookup with no resampling.

Each window is weighted by its physical width.  A naive ``mean(depths)``
over-weights short-contig windows on the fragmented assemblies where this plot
matters most, so the weighted mean is the correct aggregation::

    Σ(depth × width) / Σ(width)

A window present in the reference GC but absent from coverage resolves to
**depth 0**, not "no data here".  Dropping it would render a real GC dropout
as 'no data here' rather than 'no coverage here', inverting the plot's
meaning.

Output bins span 0-100 % GC at 1 % resolution.  Normalized coverage is the
per-bin weighted mean depth divided by the genome-wide weighted mean depth, so
a flat curve at y=1.0 means uniform coverage across GC content.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

# GC bins from 0 % to 100 %, 1 % per bin.  101 bins total.
GC_BIN_COUNT = 101


def compute_gc_bias(
    gc_tracks: dict,
    coverage_regions: dict[str, list[dict]],
) -> dict:
    """Join per-window depth with per-window GC and bin by GC percentage.

    Parameters
    ----------
    gc_tracks:
        The ``gc_tracks`` result dict with per-contig GC data (same shape
        ``compute_gc_tracks`` returns).
    coverage_regions:
        The mosdepth coverage regions dict keyed by contig name (same shape
        as the ``regions`` field of the mosdepth report).

    Returns
    -------
    dict
        ``{"gc_bias_bins": [...], "genome_avg_depth": float,
        "gc_bias_computed": true}``.
    """
    gc_contigs = {c["name"]: c for c in gc_tracks.get("contigs", [])}

    # Accumulators per GC bin, weighted by window width.
    weighted_depth_sum: list[float] = [0.0] * GC_BIN_COUNT
    weight_sum: list[float] = [0.0] * GC_BIN_COUNT
    window_count: list[int] = [0] * GC_BIN_COUNT

    total_weighted_depth = 0.0
    total_weight = 0.0

    for contig_name, windows in coverage_regions.items():
        gc_contig = gc_contigs.get(contig_name)
        if gc_contig is None:
            # Contig present in coverage but absent from GC tracks — skip.
            continue

        gc_values: list[float | None] | None = gc_contig.get("gc")

        for i, window in enumerate(windows):
            if gc_values is None or i >= len(gc_values):
                # Window present in coverage but beyond the GC track's
                # window count — treat as depth 0 (no coverage).
                gc_pct = None
            else:
                gc_pct = gc_values[i]

            depth = window["depth"]
            width = window["end"] - window["start"]

            if gc_pct is not None:
                # Round to nearest integer bin.
                bin_idx = min(int(round(gc_pct)), GC_BIN_COUNT - 1)
                weighted_depth_sum[bin_idx] += depth * width
                weight_sum[bin_idx] += width
                window_count[bin_idx] += 1

            total_weighted_depth += depth * width
            total_weight += width

    genome_avg_depth = total_weighted_depth / total_weight if total_weight > 0 else 0.0

    bins: list[dict] = []
    for gc_pct in range(GC_BIN_COUNT):
        w = weight_sum[gc_pct]
        if w > 0:
            mean_depth = weighted_depth_sum[gc_pct] / w
            normalized = mean_depth / genome_avg_depth if genome_avg_depth > 0 else 0.0
        else:
            mean_depth = 0.0
            normalized = 0.0

        bins.append({
            "gc_pct": gc_pct,
            "normalized_coverage": round(normalized, 4),
            "window_count": window_count[gc_pct],
        })

    return {
        "gc_bias_bins": bins,
        "genome_avg_depth": round(genome_avg_depth, 2),
        "gc_bias_computed": True,
    }
