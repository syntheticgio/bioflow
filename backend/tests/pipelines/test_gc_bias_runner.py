"""Tests for gc_bias_runner: the coverage-vs-GC bias curve computation."""

from __future__ import annotations

from app.pipelines import gc_bias_runner


class TestComputeGcBias:
    """The join is the one piece of real logic in the feature.

    A uniform-width fixture (all contigs the same length) would pass against
    the naive ``mean(depths)`` too, so it would certify the wrong
    implementation which over-weights short-contig windows on exactly the
    fragmented assemblies where this plot matters.  The test therefore uses
    contigs of *different* lengths.
    """

    def test_flat_coverage_returns_flat_curve(self):
        """When depth is uniform across GC bins, the curve is flat at 1.0."""
        gc_tracks = {
            "window_count": 2,
            "contigs": [
                {
                    "name": "chr1",
                    "length": 2000,
                    "window_bases": 1000,
                    "gc": [40.0, 60.0],
                },
                {
                    "name": "chr2",
                    "length": 400,
                    "window_bases": 200,
                    "gc": [50.0, 50.0],
                },
            ],
        }

        coverage_regions = {
            "chr1": [
                {"start": 0, "end": 1000, "depth": 30.0, "name": None},
                {"start": 1000, "end": 2000, "depth": 30.0, "name": None},
            ],
            "chr2": [
                {"start": 0, "end": 200, "depth": 30.0, "name": None},
                {"start": 200, "end": 400, "depth": 30.0, "name": None},
            ],
        }

        result = gc_bias_runner.compute_gc_bias(gc_tracks, coverage_regions)
        bins = result["gc_bias_bins"]

        # All depth is 30.0, so genome avg is 30.0, normalized is 1.0
        for b in bins:
            if b["window_count"] > 0:
                assert b["normalized_coverage"] == 1.0, f"bin {b['gc_pct']}%"
        assert result["genome_avg_depth"] == 30.0

    def test_gc_bias_dome_shape(self):
        """Higher depth in mid-GC windows produces a dome."""
        gc_tracks = {
            "window_count": 3,
            "contigs": [
                {
                    "name": "chr1",
                    "length": 3000,
                    "window_bases": 1000,
                    "gc": [30.0, 50.0, 70.0],
                },
            ],
        }

        coverage_regions = {
            "chr1": [
                {"start": 0, "end": 1000, "depth": 10.0, "name": None},
                {"start": 1000, "end": 2000, "depth": 50.0, "name": None},
                {"start": 2000, "end": 3000, "depth": 10.0, "name": None},
            ],
        }

        result = gc_bias_runner.compute_gc_bias(gc_tracks, coverage_regions)
        bins = result["gc_bias_bins"]

        bin_30 = next(b for b in bins if b["gc_pct"] == 30)
        bin_50 = next(b for b in bins if b["gc_pct"] == 50)
        bin_70 = next(b for b in bins if b["gc_pct"] == 70)

        assert bin_30["normalized_coverage"] < 1.0, "low GC should be below average"
        assert bin_50["normalized_coverage"] > 1.0, "mid GC should be above average"
        assert bin_70["normalized_coverage"] < 1.0, "high GC should be below average"
        assert bin_50["normalized_coverage"] > bin_30["normalized_coverage"]

    def test_contigs_of_different_lengths(self):
        """Short contigs must not be over-weighted by naive mean(depths)."""
        gc_tracks = {
            "window_count": 2,
            "contigs": [
                {
                    "name": "long",
                    "length": 2000,
                    "window_bases": 1000,
                    "gc": [40.0, 40.0],
                },
                {
                    "name": "short",
                    "length": 200,
                    "window_bases": 100,
                    "gc": [40.0, 40.0],
                },
            ],
        }

        coverage_regions = {
            "long": [
                {"start": 0, "end": 1000, "depth": 30.0, "name": None},
                {"start": 1000, "end": 2000, "depth": 30.0, "name": None},
            ],
            "short": [
                {"start": 0, "end": 100, "depth": 100.0, "name": None},
                {"start": 100, "end": 200, "depth": 100.0, "name": None},
            ],
        }

        result = gc_bias_runner.compute_gc_bias(gc_tracks, coverage_regions)
        bins = result["gc_bias_bins"]

        # Total weighted depth = (30*1000 + 30*1000 + 100*100 + 100*100)
        #                       = 60,000 + 20,000 = 80,000
        # Total weight = 1000 + 1000 + 100 + 100 = 2,200
        # Genome avg = 80,000 / 2,200 ≈ 36.36
        # The short contig at 100x has less weight (200/2200 ≈ 9%) than its
        # naive contribution (2/4 windows = 50%), so the genome avg stays
        # closer to 30x.
        assert 30.0 < result["genome_avg_depth"] < 50.0, (
            f"genome_avg_depth should be between 30 and 50, "
            f"got {result['genome_avg_depth']}"
        )

        # The 40% GC bin should have normalized coverage based on weighted mean
        bin_40 = next(b for b in bins if b["gc_pct"] == 40)
        # Weighted mean depth at 40% GC = (30*1000 + 30*1000 + 100*100 + 100*100) / 2200 = 36.36
        # Normalized = 36.36 / 36.36 = 1.0
        assert abs(bin_40["normalized_coverage"] - 1.0) < 0.01, (
            f"normalized_coverage should be ~1.0, got {bin_40['normalized_coverage']}"
        )

    def test_window_present_in_coverage_but_absent_from_gc(self):
        """A window in coverage but beyond the GC track's window count
        is skipped rather than crashing, and does not affect genome avg."""
        gc_tracks = {
            "window_count": 1,
            "contigs": [
                {
                    "name": "chr1",
                    "length": 1000,
                    "window_bases": 1000,
                    "gc": [40.0],
                },
            ],
        }

        coverage_regions = {
            "chr1": [
                {"start": 0, "end": 1000, "depth": 30.0, "name": None},
                {"start": 1000, "end": 2000, "depth": 50.0, "name": None},
            ],
        }

        result = gc_bias_runner.compute_gc_bias(gc_tracks, coverage_regions)
        bins = result["gc_bias_bins"]

        # The extra window contributes to genome avg but is not binned
        assert result["genome_avg_depth"] == 40.0

        # Only the first window is binned
        bin_40 = next(b for b in bins if b["gc_pct"] == 40)
        assert bin_40["window_count"] == 1
        assert bin_40["normalized_coverage"] == 30.0 / 40.0  # = 0.75

    def test_empty_coverage_regions(self):
        """No coverage data returns empty bins with genome_avg_depth=0."""
        gc_tracks = {
            "window_count": 1,
            "contigs": [
                {
                    "name": "chr1",
                    "length": 1000,
                    "window_bases": 1000,
                    "gc": [40.0],
                },
            ],
        }

        result = gc_bias_runner.compute_gc_bias(gc_tracks, {})
        assert result["genome_avg_depth"] == 0.0
        assert all(b["normalized_coverage"] == 0.0 for b in result["gc_bias_bins"])
        assert all(b["window_count"] == 0 for b in result["gc_bias_bins"])

    def test_contig_in_gc_but_not_in_coverage(self):
        """A contig present in GC but absent from coverage is skipped.

        The genome average is computed only from windows present in coverage,
        so when no coverage contig matches a GC contig, the average is 0.
        """
        gc_tracks = {
            "window_count": 1,
            "contigs": [
                {
                    "name": "chr1",
                    "length": 1000,
                    "window_bases": 1000,
                    "gc": [40.0],
                },
            ],
        }

        coverage_regions = {
            "chr2": [
                {"start": 0, "end": 1000, "depth": 30.0, "name": None},
            ],
        }

        result = gc_bias_runner.compute_gc_bias(gc_tracks, coverage_regions)
        # No matching contig between GC and coverage, so genome average is 0
        assert result["genome_avg_depth"] == 0.0
        # The 40% GC bin should be empty since chr1's GC data is not matched
        bin_40 = next(b for b in result["gc_bias_bins"] if b["gc_pct"] == 40)
        assert bin_40["window_count"] == 0

    def test_all_101_bins_are_present(self):
        """The output always contains 101 bins (0-100% GC)."""
        gc_tracks = {
            "window_count": 1,
            "contigs": [],
        }
        result = gc_bias_runner.compute_gc_bias(gc_tracks, {})
        assert len(result["gc_bias_bins"]) == 101
        assert result["gc_bias_computed"] is True
