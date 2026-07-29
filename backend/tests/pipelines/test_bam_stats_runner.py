"""Command construction and output parsing for BAM results statistics.

Pure functions over strings and paths, mirroring align_runner.py: the parts
worth testing in isolation, with no queue or filesystem involved.
"""

from pathlib import Path

from app.pipelines.bam_stats_runner import (
    bin_depth,
    build_coverage_command,
    build_depth_command,
    build_idxstats_command,
    coerce_tsv_value,
    contigs_from_coverage,
    contigs_tsv,
    cumulative_coverage,
    genome_summary,
    parse_coverage,
    parse_idxstats,
)


class TestCommandConstruction:
    def test_idxstats_command(self):
        cmd = build_idxstats_command(
            samtools_path="/usr/bin/samtools", bam=Path("/work/a.bam")
        )
        assert cmd == ["/usr/bin/samtools", "idxstats", "/work/a.bam"]

    def test_coverage_command(self):
        cmd = build_coverage_command(
            samtools_path="/usr/bin/samtools", bam=Path("/work/a.bam")
        )
        assert cmd == ["/usr/bin/samtools", "coverage", "/work/a.bam"]

    def test_depth_command_is_unfiltered_and_all_positions(self):
        """-a includes zero-depth positions -- required for a birds-eye view
        that must not silently skip uncovered regions -- and -a reports every
        position rather than only covered ones."""
        cmd = build_depth_command(
            samtools_path="/usr/bin/samtools", bam=Path("/work/a.bam")
        )
        assert cmd == ["/usr/bin/samtools", "depth", "-a", "/work/a.bam"]


IDXSTATS_OUTPUT = (
    "chr1\t248956422\t1200000\t300\n"
    "chr2\t242193529\t980000\t150\n"
    "*\t0\t0\t42\n"
)

COVERAGE_OUTPUT = (
    "#rname\tstartpos\tendpos\tnumreads\tcovbases\tcoverage\tmeandepth\tmeanbaseq\tmeanmapq\n"
    "chr1\t1\t248956422\t1200000\t248900000\t99.98\t32.4\t36.1\t58.2\n"
    "chr2\t1\t242193529\t980000\t241800000\t99.83\t28.1\t35.9\t57.8\n"
)


class TestParseIdxstats:
    def test_parses_each_contig(self):
        rows = parse_idxstats(IDXSTATS_OUTPUT)
        assert rows[0] == {
            "contig": "chr1",
            "length": 248956422,
            "mapped_reads": 1200000,
            "unmapped_reads": 300,
        }
        assert rows[1]["contig"] == "chr2"

    def test_the_trailing_star_row_is_unplaced_not_a_contig(self):
        """The '*' row holds unmapped reads with no coordinate at all -- not a
        real contig, and length 0 would otherwise poison a birds-eye plot that
        assumes every row has a positive length."""
        rows = parse_idxstats(IDXSTATS_OUTPUT)
        names = [r["contig"] for r in rows]
        assert "*" not in names

    def test_empty_output_is_empty_list(self):
        assert parse_idxstats("") == []


class TestParseCoverage:
    def test_parses_each_contig(self):
        rows = parse_coverage(COVERAGE_OUTPUT)
        assert rows[0] == {
            "contig": "chr1",
            "start": 1,
            "end": 248956422,
            "reads": 1200000,
            "covered_bases": 248900000,
            "coverage_pct": 99.98,
            "mean_depth": 32.4,
            "mean_baseq": 36.1,
            "mean_mapq": 58.2,
        }
        assert rows[1]["contig"] == "chr2"

    def test_header_line_is_not_a_data_row(self):
        rows = parse_coverage(COVERAGE_OUTPUT)
        assert len(rows) == 2

    def test_empty_output_is_empty_list(self):
        assert parse_coverage("") == []


class TestContigsFromCoverage:
    def test_merges_idxstats_and_coverage_by_contig_name(self):
        """The table needs both: coverage has depth and breadth, idxstats has
        the unmapped count coverage does not report."""
        idx = parse_idxstats(IDXSTATS_OUTPUT)
        cov = parse_coverage(COVERAGE_OUTPUT)
        contigs = contigs_from_coverage(idxstats_rows=idx, coverage_rows=cov)
        chr1 = next(c for c in contigs if c["contig"] == "chr1")
        assert chr1["length"] == 248956422
        assert chr1["reads"] == 1200000
        assert chr1["unmapped_reads"] == 300
        assert chr1["mean_depth"] == 32.4

    def test_sorted_by_reads_descending(self):
        """The table's default order and the top-N slice both want the most
        active contigs first."""
        idx = parse_idxstats(IDXSTATS_OUTPUT)
        cov = parse_coverage(COVERAGE_OUTPUT)
        contigs = contigs_from_coverage(idxstats_rows=idx, coverage_rows=cov)
        assert [c["contig"] for c in contigs] == ["chr1", "chr2"]


class TestBinDepth:
    def test_bins_a_single_contig_into_the_requested_count(self):
        """10 positions binned into 5 bins -- each bin covers 2 positions."""
        contig_lengths = [("chr1", 10)]
        depth_lines = [f"chr1\t{p}\t{p}" for p in range(1, 11)]  # depth == position
        bins, boundaries = bin_depth(
            contig_lengths=contig_lengths, depth_lines=iter(depth_lines), bin_count=5
        )
        assert len(bins) == 5
        # bin 0 covers positions 1-2 (depth 1,2) -> mean 1.5
        assert bins[0] == 1.5
        # bin 4 covers positions 9-10 (depth 9,10) -> mean 9.5
        assert bins[4] == 9.5
        assert boundaries == [{"contig": "chr1", "bin_start": 0}]

    def test_a_contig_shorter_than_one_bin_still_gets_a_bin(self):
        """A 3-contig genome where one contig is tiny must not vanish: every
        contig gets at least one bin regardless of its share of total length."""
        contig_lengths = [("chr1", 1000), ("scaffold_1", 1), ("chr2", 1000)]
        depth_lines = (
            [f"chr1\t{p}\t10" for p in range(1, 1001)]
            + ["scaffold_1\t1\t99"]
            + [f"chr2\t{p}\t20" for p in range(1, 1001)]
        )
        bins, boundaries = bin_depth(
            contig_lengths=contig_lengths, depth_lines=iter(depth_lines), bin_count=10
        )
        contig_names = [b["contig"] for b in boundaries]
        assert "scaffold_1" in contig_names
        # The scaffold's one bin reflects its own depth, not blended with a
        # neighbour's -- 99, not something between 10 and 20.
        scaffold_bin_start = next(
            b["bin_start"] for b in boundaries if b["contig"] == "scaffold_1"
        )
        assert bins[scaffold_bin_start] == 99

    def test_bin_count_is_constant_regardless_of_reference_size(self):
        small = bin_depth(
            contig_lengths=[("c1", 100)],
            depth_lines=iter(f"c1\t{p}\t5" for p in range(1, 101)),
            bin_count=1000,
        )
        large = bin_depth(
            contig_lengths=[("c1", 1_000_000)],
            depth_lines=iter(f"c1\t{p}\t5" for p in range(1, 1_000_001)),
            bin_count=1000,
        )
        assert len(small[0]) == len(large[0]) == 1000

    def test_positions_absent_from_depth_output_count_as_zero(self):
        """`-a` should mean every position is present, but a defensive default
        of zero-depth for a missing position keeps a truncated tool run from
        producing a bin count mismatch rather than a silently wrong plot."""
        bins, _ = bin_depth(
            contig_lengths=[("chr1", 4)],
            depth_lines=iter(["chr1\t1\t10", "chr1\t3\t20"]),  # positions 2, 4 missing
            bin_count=4,
        )
        assert bins == [10.0, 0.0, 20.0, 0.0]


class TestCumulativeCoverage:
    def test_all_bins_at_or_above_threshold_are_counted(self):
        """A flat depth-10 genome: 100% of bins are at or above every
        threshold up to 10, and 0% above it."""
        curve = cumulative_coverage(bins=[10.0] * 100, thresholds=[1, 5, 10, 20])
        by_threshold = {c["depth"]: c["fraction"] for c in curve}
        assert by_threshold[1] == 1.0
        assert by_threshold[10] == 1.0
        assert by_threshold[20] == 0.0

    def test_mixed_depth_gives_a_partial_fraction(self):
        bins = [0.0] * 25 + [15.0] * 75  # 75% of the genome at depth 15
        curve = cumulative_coverage(bins=bins, thresholds=[1, 10, 20])
        by_threshold = {c["depth"]: c["fraction"] for c in curve}
        assert by_threshold[1] == 0.75
        assert by_threshold[10] == 0.75
        assert by_threshold[20] == 0.0

    def test_empty_bins_is_an_empty_curve(self):
        assert cumulative_coverage(bins=[], thresholds=[1, 10]) == []


class TestGenomeSummary:
    def test_summarizes_across_all_contigs(self):
        contigs = [
            {
                "contig": "chr1", "length": 100, "reads": 50, "unmapped_reads": 5,
                "covered_bases": 90, "coverage_pct": 90.0, "mean_depth": 10.0,
                "mean_baseq": 35.0, "mean_mapq": 55.0, "start": 1, "end": 100,
            },
            {
                "contig": "chr2", "length": 200, "reads": 80, "unmapped_reads": 2,
                "covered_bases": 200, "coverage_pct": 100.0, "mean_depth": 20.0,
                "mean_baseq": 36.0, "mean_mapq": 58.0, "start": 1, "end": 200,
            },
        ]
        summary = genome_summary(contigs=contigs, bins=[5.0] * 5 + [15.0] * 5)
        assert summary["total_contigs"] == 2
        assert summary["mapped_reads"] == 130
        assert summary["unmapped_reads"] == 7
        # Length-weighted mean depth: (100*10 + 200*20) / 300 = 16.67
        assert round(summary["mean_depth"], 2) == 16.67
        assert summary["pct_covered_1x"] == 100.0  # all 10 bins are >0
        assert summary["pct_covered_10x"] == 50.0  # 5 of 10 bins are >=10


class TestContigsTsv:
    def test_header_and_rows(self):
        contigs = [
            {
                "contig": "chr1", "length": 100, "reads": 50, "unmapped_reads": 5,
                "covered_bases": 90, "coverage_pct": 90.0, "mean_depth": 10.0,
                "mean_baseq": 35.0, "mean_mapq": 55.0, "start": 1, "end": 100,
            },
        ]
        text = contigs_tsv(contigs)
        lines = text.splitlines()
        assert lines[0] == (
            "contig\tlength\treads\tunmapped_reads\tcovered_bases"
            "\tcoverage_pct\tmean_depth\tmean_baseq\tmean_mapq"
        )
        assert lines[1] == "chr1\t100\t50\t5\t90\t90.0\t10.0\t35.0\t55.0"

    def test_empty_contigs_is_header_only(self):
        text = contigs_tsv([])
        assert text.splitlines() == [
            "contig\tlength\treads\tunmapped_reads\tcovered_bases"
            "\tcoverage_pct\tmean_depth\tmean_baseq\tmean_mapq"
        ]


class TestCoerceTsvValue:
    def test_integer_columns_become_int(self):
        assert coerce_tsv_value("length", "1000") == 1000
        assert isinstance(coerce_tsv_value("reads", "50"), int)

    def test_float_columns_become_float(self):
        assert coerce_tsv_value("coverage_pct", "99.98") == 99.98
        assert isinstance(coerce_tsv_value("mean_depth", "20.0"), float)

    def test_contig_column_stays_a_string(self):
        assert coerce_tsv_value("contig", "chr1") == "chr1"

    def test_unknown_column_stays_a_string(self):
        assert coerce_tsv_value("mystery", "42") == "42"
