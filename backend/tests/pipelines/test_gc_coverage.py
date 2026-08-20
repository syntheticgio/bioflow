import pytest

from app.pipelines.gc_coverage import (
    JoinedWindow,
    bias_curve,
    cap_by_cumulative_length,
    join_windows,
    per_contig,
)


def _window(contig, index, start, end, gc, depth):
    return JoinedWindow(
        contig=contig, index=index, start=start, end=end,
        width=end - start, gc=gc, depth=depth,
    )


def test_bias_curve_weights_by_window_width_not_uniform_mean():
    """Two contigs land in the same GC bin (both GC=50.0) but one contig's
    windows are 10x wider than the other's. A naive `mean(depths)` gives
    equal say to a 10bp window and a 100bp window and returns (2+20)/2=11.
    The correct width-weighted mean is dominated by the wide window:
    (2*10 + 20*100) / (10 + 100) = 2020/110 = 18.363636...

    Contigs of DIFFERENT lengths are required here: a uniform-width fixture
    produces the same number under both implementations and would pass
    against the naive, wrong one.
    """
    joined = [
        _window("short", 0, 0, 10, gc=50.0, depth=2.0),
        _window("long", 0, 0, 100, gc=50.0, depth=20.0),
    ]
    curve = bias_curve(joined, bins=1)
    assert len(curve) == 1
    assert curve[0]["mean_depth"] == pytest.approx(18.3636, abs=1e-4)
    assert curve[0]["window_count"] == 2


def test_join_windows_missing_depth_row_resolves_to_zero_not_dropped():
    """A contig gc_tracks scored but mosdepth found no aligned reads on (or
    the contig was too short to window under mosdepth's own floor, though
    the two use the same floor so this specific case is about zero-read
    contigs) must still appear in the join, at depth 0 -- not be silently
    absent. Dropping it would make the bias curve blind to exactly the
    "this GC content has no coverage" signal the plot exists to show.
    """
    gc_contigs = [
        {
            "name": "covered",
            "length": 20,
            "window_bases": 10,
            "gc": [40.0, 60.0],
            "skew": [0.0, 0.0],
        },
        {
            "name": "uncovered",
            "length": 10,
            "window_bases": 10,
            "gc": [50.0],
            "skew": [0.0],
        },
    ]
    depth_regions = {
        "covered": [
            {"start": 0, "end": 10, "depth": 5.0, "name": None},
            {"start": 10, "end": 20, "depth": 8.0, "name": None},
        ],
        # "uncovered" has no key at all -- mosdepth produced no rows for it.
    }

    joined = join_windows(gc_contigs, depth_regions)

    by_contig = {}
    for w in joined:
        by_contig.setdefault(w["contig"], []).append(w)

    assert len(by_contig["covered"]) == 2
    assert len(by_contig["uncovered"]) == 1
    assert by_contig["uncovered"][0]["depth"] == 0.0
    assert by_contig["uncovered"][0]["gc"] == 50.0


def test_join_windows_reconstructs_window_bounds_from_gc_tracks_shape():
    """gc_tracks stores window_bases (a per-contig constant) and a flat gc
    list; start/end for each window index must be reconstructed the same
    way gc_tracks/mosdepth_runner both tile: index * window_bases, with the
    LAST window absorbing the remainder to length (mirrors
    mosdepth_runner.build_windows_bed's own comment about why
    range(0, length, width) is wrong here)."""
    gc_contigs = [
        {
            "name": "c1",
            "length": 25,
            "window_bases": 10,
            "gc": [50.0, 50.0],
            "skew": [0.0, 0.0],
        },
    ]
    depth_regions = {
        "c1": [
            {"start": 0, "end": 10, "depth": 1.0, "name": None},
            # mosdepth's own last window also absorbs the remainder to 25.
            {"start": 10, "end": 25, "depth": 2.0, "name": None},
        ],
    }
    joined = join_windows(gc_contigs, depth_regions)
    assert [(w["start"], w["end"]) for w in joined] == [(0, 10), (10, 25)]
    assert [w["width"] for w in joined] == [10, 15]


def test_bias_curve_skips_none_gc_and_omits_empty_bins():
    joined = [
        _window("c", 0, 0, 10, gc=None, depth=99.0),  # excluded entirely
        _window("c", 1, 10, 20, gc=5.0, depth=3.0),
    ]
    curve = bias_curve(joined, bins=20)
    assert len(curve) == 1
    assert curve[0]["mean_depth"] == 3.0


def test_per_contig_gc_is_base_weighted_not_unweighted_mean():
    """Two windows, one 10bp at GC=100% (10 G/C bases of 10) and one 90bp at
    GC=0% (0 of 90). The unweighted mean of percentages is 50%. The correct
    per-contig GC, weighted by each window's base count, is
    (10 + 0) / (10 + 90) = 10%.
    """
    joined = [
        _window("c1", 0, 0, 10, gc=100.0, depth=5.0),
        _window("c1", 1, 10, 100, gc=0.0, depth=5.0),
    ]
    rows = per_contig(joined)
    assert len(rows) == 1
    assert rows[0]["contig"] == "c1"
    assert rows[0]["gc"] == pytest.approx(10.0)
    assert rows[0]["length"] == 100
    assert rows[0]["window_count"] == 2


def test_per_contig_mean_depth_is_width_weighted():
    joined = [
        _window("c1", 0, 0, 10, gc=50.0, depth=100.0),
        _window("c1", 1, 10, 110, gc=50.0, depth=1.0),
    ]
    rows = per_contig(joined)
    # (100*10 + 1*100) / 110 = 1100/110 = 10.0
    assert rows[0]["mean_depth"] == pytest.approx(10.0)


def test_per_contig_skips_none_gc_windows_in_the_gc_average_but_not_depth():
    """An all-N window contributes no G/C or total bases to the GC ratio
    (it truly has none), but its depth still counts toward mean_depth --
    depth was measured regardless of base composition."""
    joined = [
        _window("c1", 0, 0, 10, gc=None, depth=8.0),
        _window("c1", 1, 10, 20, gc=60.0, depth=2.0),
    ]
    rows = per_contig(joined)
    assert rows[0]["gc"] == 60.0
    assert rows[0]["mean_depth"] == pytest.approx((8.0 * 10 + 2.0 * 10) / 20)


def test_cap_by_cumulative_length_keeps_contigs_covering_target_fraction():
    contigs = [
        {"contig": "big", "length": 900, "gc": 50.0, "mean_depth": 10.0, "window_count": 9},
        {"contig": "small", "length": 90, "gc": 50.0, "mean_depth": 10.0, "window_count": 1},
        {"contig": "tiny", "length": 10, "gc": 50.0, "mean_depth": 10.0, "window_count": 1},
    ]
    kept, dropped = cap_by_cumulative_length(contigs, target_fraction=0.99, hard_ceiling=5000)
    # Sorted descending by length: big(900)=90%, +small(90)=99% exactly hits
    # target; tiny should be dropped.
    assert {c["contig"] for c in kept} == {"big", "small"}
    assert dropped == 1


def test_cap_by_cumulative_length_reports_true_dropped_count_not_a_log_line():
    """V4's whole point: a contaminant is often many small contigs, so this
    count must be exact and always available to the caller -- it becomes
    the chart's 'M shorter contigs omitted' line."""
    contigs = [{"contig": f"c{i}", "length": 1, "gc": 50.0, "mean_depth": 1.0, "window_count": 1}
               for i in range(100)]
    contigs[0]["length"] = 10_000  # one huge contig covers >99% alone
    kept, dropped = cap_by_cumulative_length(contigs, target_fraction=0.99, hard_ceiling=5000)
    assert dropped == 99
    assert len(kept) == 1


def test_cap_by_cumulative_length_hard_ceiling_binds_even_under_target_fraction():
    """A pathologically fragmented assembly where reaching 99% would need
    more than hard_ceiling contigs -- the ceiling must still bind, and the
    dropped count must still be exact."""
    contigs = [{"contig": f"c{i}", "length": 1, "gc": 50.0, "mean_depth": 1.0, "window_count": 1}
               for i in range(20)]
    kept, dropped = cap_by_cumulative_length(contigs, target_fraction=0.99, hard_ceiling=5)
    assert len(kept) == 5
    assert dropped == 15
