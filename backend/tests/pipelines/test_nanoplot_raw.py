"""Binning NanoPlot's per-read TSV into the two long-read distribution facts.

The check that matters most here is the base-conservation one: summed bases
across the histogram must equal the total, because a binning bug that drops or
double-counts a bin produces a chart that looks entirely plausible and is
wrong. Everything else is bounds and degradation.
"""

import gzip

import pytest

from app.pipelines import nanoplot_raw
from app.pipelines.nanoplot_raw import (
    LENGTH_BINS,
    MAX_LENGTH,
    MIN_LENGTH,
    QUALITY_BINS,
    bin_raw_reads,
    length_bin_index,
    length_bin_start,
    quality_bin_index,
)


def write_raw(path, rows, header="quals\tlengths"):
    """A gzipped TSV in NanoPlot's `--raw` shape. Rows are (qual, length)."""
    with gzip.open(path, "wt") as fh:
        fh.write(header + "\n")
        for qual, length in rows:
            fh.write(f"{qual}\t{length}\n")
    return path


class TestLengthBins:
    def test_the_floor_lands_in_the_first_bin(self):
        assert length_bin_index(MIN_LENGTH) == 0
        assert length_bin_index(1) == 0

    def test_the_ceiling_lands_in_the_last_bin(self):
        assert length_bin_index(MAX_LENGTH) == LENGTH_BINS - 1
        assert length_bin_index(MAX_LENGTH * 10) == LENGTH_BINS - 1

    def test_bins_are_log_spaced(self):
        # A decade apart is exactly BINS_PER_DECADE bins apart, which is the
        # property the whole log-spacing rests on.
        assert (
            length_bin_index(10_000) - length_bin_index(1_000)
            == nanoplot_raw.BINS_PER_DECADE
        )

    def test_bin_starts_ascend_and_bracket_their_members(self):
        for length in (150, 900, 5_400, 42_000, 300_000):
            idx = length_bin_index(length)
            assert length_bin_start(idx) <= length < length_bin_start(idx + 1)

    def test_no_length_indexes_past_the_axis(self):
        for length in (1, MIN_LENGTH, 999_999, MAX_LENGTH, 10**9):
            assert 0 <= length_bin_index(length) < LENGTH_BINS


class TestQualityBins:
    def test_quality_is_clamped_at_both_ends(self):
        assert quality_bin_index(-5) == 0
        assert quality_bin_index(0) == 0
        assert quality_bin_index(999) == QUALITY_BINS - 1

    def test_quality_bins_are_unit_wide(self):
        assert quality_bin_index(12.4) == 12
        assert quality_bin_index(12.9) == 12
        assert quality_bin_index(13.0) == 13


class TestBaseConservation:
    """The success criterion from the issue: bases must reconcile."""

    def test_summed_bases_equal_the_total(self, tmp_path):
        rows = [(12.0, n) for n in (150, 800, 2_400, 9_100, 31_000, 120_000)]
        facts = bin_raw_reads(write_raw(tmp_path / "raw.tsv.gz", rows))

        hist = facts["qc_length_bases_histogram"]
        assert sum(b["bases"] for b in hist["bins"]) == sum(n for _, n in rows)
        assert hist["total_bases"] == sum(n for _, n in rows)

    def test_reads_outside_the_axis_are_clamped_not_dropped(self, tmp_path):
        # A 40 bp read and a 3 Mb read both fall outside [100, 1e6]. Dropping
        # either would leave the summed bases disagreeing with qc_total_bases,
        # which is exactly the reconciliation the criterion exists to catch.
        rows = [(12.0, 40), (12.0, 5_000), (12.0, 3_000_000)]
        facts = bin_raw_reads(write_raw(tmp_path / "raw.tsv.gz", rows))

        hist = facts["qc_length_bases_histogram"]
        assert sum(b["bases"] for b in hist["bins"]) == 3_005_040
        assert hist["total_reads"] == 3

    def test_reads_are_counted_alongside_bases(self, tmp_path):
        # Two reads in one bin: the bin carries both sums, so the chart can
        # say "N reads, M bases" without a second fact.
        rows = [(12.0, 1_000), (12.0, 1_100)]
        facts = bin_raw_reads(write_raw(tmp_path / "raw.tsv.gz", rows))

        bins = facts["qc_length_bases_histogram"]["bins"]
        assert len(bins) == 1
        assert bins[0]["bases"] == 2_100
        assert bins[0]["reads"] == 2


class TestBoundedness:
    def test_the_histogram_stays_bounded_regardless_of_read_count(self, tmp_path):
        rows = [(10.0 + (i % 30) / 3, 200 + i * 37) for i in range(20_000)]
        facts = bin_raw_reads(write_raw(tmp_path / "raw.tsv.gz", rows))

        assert len(facts["qc_length_bases_histogram"]["bins"]) <= LENGTH_BINS

    def test_the_density_grid_stays_bounded_regardless_of_read_count(self, tmp_path):
        rows = [(10.0 + (i % 30) / 3, 200 + i * 37) for i in range(20_000)]
        facts = bin_raw_reads(write_raw(tmp_path / "raw.tsv.gz", rows))

        cells = facts["qc_length_quality_density"]["cells"]
        assert len(cells) <= LENGTH_BINS * QUALITY_BINS

    def test_empty_bins_are_omitted(self, tmp_path):
        # One read occupies one bin; the ~24 empty ones are not emitted.
        facts = bin_raw_reads(write_raw(tmp_path / "raw.tsv.gz", [(12.0, 5_000)]))
        assert len(facts["qc_length_bases_histogram"]["bins"]) == 1


class TestDensityGrid:
    def test_cells_carry_length_start_quality_bin_and_count(self, tmp_path):
        rows = [(12.4, 5_000), (12.9, 5_100), (30.0, 5_000)]
        facts = bin_raw_reads(write_raw(tmp_path / "raw.tsv.gz", rows))

        cells = facts["qc_length_quality_density"]["cells"]
        length_start = length_bin_start(length_bin_index(5_000))
        assert [length_start, 12, 2] in cells
        assert [length_start, 30, 1] in cells

    def test_max_count_matches_the_busiest_cell(self, tmp_path):
        rows = [(12.0, 5_000), (12.0, 5_100), (12.0, 5_200), (30.0, 5_000)]
        facts = bin_raw_reads(write_raw(tmp_path / "raw.tsv.gz", rows))

        assert facts["qc_length_quality_density"]["max_count"] == 3

    def test_cell_counts_sum_to_the_read_total(self, tmp_path):
        rows = [(10.0 + (i % 7), 400 + i * 91) for i in range(500)]
        facts = bin_raw_reads(write_raw(tmp_path / "raw.tsv.gz", rows))

        density = facts["qc_length_quality_density"]
        assert sum(c[2] for c in density["cells"]) == density["total_reads"] == 500


class TestColumnResolution:
    def test_columns_are_found_by_name_not_position(self, tmp_path):
        # The reversed header is the failure this guards: read positionally,
        # a 5000 bp read would be binned as quality 5000 and a Q12 read as a
        # 12 bp length -- a plausible-looking chart of nonsense.
        path = tmp_path / "raw.tsv.gz"
        with gzip.open(path, "wt") as fh:
            fh.write("lengths\tquals\n")
            fh.write("5000\t12.0\n")

        facts = bin_raw_reads(path)
        assert facts["qc_length_bases_histogram"]["total_bases"] == 5_000
        assert facts["qc_length_quality_density"]["cells"] == [
            [length_bin_start(length_bin_index(5_000)), 12, 1]
        ]

    def test_extra_columns_are_ignored(self, tmp_path):
        path = tmp_path / "raw.tsv.gz"
        with gzip.open(path, "wt") as fh:
            fh.write("quals\tlengths\tchannel\n")
            fh.write("12.0\t5000\t44\n")

        assert bin_raw_reads(path)["qc_length_bases_histogram"]["total_bases"] == 5_000

    def test_a_file_without_qualities_still_yields_the_histogram(self, tmp_path):
        # FASTA input: NanoPlot omits the column rather than writing nulls.
        path = tmp_path / "raw.tsv.gz"
        with gzip.open(path, "wt") as fh:
            fh.write("lengths\n")
            fh.write("5000\n7000\n")

        facts = bin_raw_reads(path)
        assert facts["qc_length_bases_histogram"]["total_bases"] == 12_000
        assert "qc_length_quality_density" not in facts


class TestDegradation:
    """Every failure costs the charts and nothing else."""

    def test_a_missing_file_yields_no_facts(self, tmp_path):
        assert bin_raw_reads(tmp_path / "absent.tsv.gz") == {}

    def test_an_unrecognised_header_yields_no_facts(self, tmp_path):
        path = tmp_path / "raw.tsv.gz"
        with gzip.open(path, "wt") as fh:
            fh.write("something\telse\n1\t2\n")

        assert bin_raw_reads(path) == {}

    def test_a_truncated_gzip_yields_no_facts(self, tmp_path):
        path = tmp_path / "raw.tsv.gz"
        path.write_bytes(b"\x1f\x8b\x08\x00 not really gzip")

        assert bin_raw_reads(path) == {}

    def test_an_empty_file_yields_no_facts(self, tmp_path):
        assert bin_raw_reads(write_raw(tmp_path / "raw.tsv.gz", [])) == {}

    def test_unparseable_rows_are_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "raw.tsv.gz"
        with gzip.open(path, "wt") as fh:
            fh.write("quals\tlengths\n")
            fh.write("12.0\t5000\n")
            fh.write("12.0\tnot-a-number\n")
            fh.write("short-row\n")
            fh.write("12.0\t0\n")
            fh.write("12.0\t3000\n")

        facts = bin_raw_reads(path)
        assert facts["qc_length_bases_histogram"]["total_reads"] == 2
        assert facts["qc_length_bases_histogram"]["total_bases"] == 8_000

    def test_a_row_with_an_unparseable_quality_still_counts_its_bases(self, tmp_path):
        # The length is the more valuable of the two; a bad quality should
        # not cost the base-conservation invariant.
        path = tmp_path / "raw.tsv.gz"
        with gzip.open(path, "wt") as fh:
            fh.write("quals\tlengths\n")
            fh.write("nan-ish\t5000\n")

        facts = bin_raw_reads(path)
        assert facts["qc_length_bases_histogram"]["total_bases"] == 5_000
        assert "qc_length_quality_density" not in facts


@pytest.mark.parametrize("length", [100, 101, 999, 1_000, 99_999, 999_999])
def test_every_bin_start_is_a_positive_ascending_edge(length):
    idx = length_bin_index(length)
    assert length_bin_start(idx) > 0
    assert length_bin_start(idx + 1) > length_bin_start(idx)


class TestHandlerIntegration:
    """`_bin_nanoplot_raw` -- the seam that bins and then deletes the TSV."""

    def test_the_raw_tsv_is_deleted_after_binning(self, tmp_path):
        from app.queue.pipeline_handlers import _bin_nanoplot_raw

        raw = write_raw(
            tmp_path / "NanoPlot-data.tsv.gz", [(12.0, 5_000), (12.0, 9_000)]
        )
        facts = _bin_nanoplot_raw(tmp_path)

        assert facts["qc_length_bases_histogram"]["total_bases"] == 14_000
        assert not raw.exists()

    def test_the_raw_tsv_is_deleted_even_when_it_cannot_be_binned(self, tmp_path):
        from app.queue.pipeline_handlers import _bin_nanoplot_raw

        raw = tmp_path / "NanoPlot-data.tsv.gz"
        raw.write_bytes(b"\x1f\x8b\x08\x00 not really gzip")

        assert _bin_nanoplot_raw(tmp_path) == {}
        assert not raw.exists()

    def test_an_absent_tsv_is_not_an_error(self, tmp_path):
        from app.queue.pipeline_handlers import _bin_nanoplot_raw

        assert _bin_nanoplot_raw(tmp_path) == {}
