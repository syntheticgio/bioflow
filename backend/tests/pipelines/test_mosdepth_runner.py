"""Unit tests for the pure mosdepth runner.

Every fixture string in this file was captured from a real mosdepth 0.3.14
run inside the backend image on 2026-08-20, not hand-written to match the
parser. See `TestParseSummary` for the two shapes that captured output
revealed and a guessed fixture would have missed: the interleaved
`<contig>_region` rows and the `total`/`total_region` pair.
"""

import gzip

import pytest

from app.pipelines import gc_tracks
from app.pipelines import mosdepth_runner as mr

# Captured verbatim from `mosdepth --by win.bed --no-per-base -t 1 cov a.bam`
# against a two-contig BAM (chr1 2000bp, chr2 600bp).
SUMMARY_FIXTURE = """chrom\tlength\tbases\tmean\tmin\tmax
chr1\t2000\t3000\t1.50\t0\t2
chr1_region\t2000\t3000\t1.50\t0\t2
chr2\t600\t400\t0.67\t0\t1
chr2_region\t600\t400\t0.67\t0\t1
total\t2600\t3400\t1.31\t0\t2
total_region\t2600\t3400\t1.31\t0\t2
"""

REGIONS_FIXTURE = """chr1\t0\t500\t1.85
chr1\t500\t1000\t2.00
chr1\t1000\t2000\t1.07
chr2\t0\t600\t0.67
"""

# The same run with a 4-column (named) BED: mosdepth propagates the name into
# column 4 and shifts depth to column 5.
NAMED_REGIONS_FIXTURE = """chr1\t0\t500\tgeneA\t1.85
chr2\t0\t600\tgeneB\t0.67
"""

# `.mosdepth.global.dist.txt`: cumulative fraction of bases at >= depth,
# per contig, plus a `total` series.
DIST_FIXTURE = """chr1\t2\t0.74
chr1\t1\t0.76
chr1\t0\t1.00
chr2\t1\t0.67
chr2\t0\t1.00
total\t2\t0.71
total\t1\t0.74
total\t0\t1.00
"""


class TestBuildCommand:
    def test_windowed_mode_emits_by_windows_bed(self, tmp_path):
        bam = tmp_path / "aln.bam"
        windows = tmp_path / "windows.bed"
        cmd = mr.build_command(bam=bam, windows_bed=windows, prefix=tmp_path / "cov")
        assert "--by" in cmd
        assert str(windows) in cmd
        assert str(bam) in cmd
        assert "-t" in cmd

    def test_region_mode_emits_by_regions_bed(self, tmp_path):
        bam = tmp_path / "aln.bam"
        regions = tmp_path / "regions.bed"
        cmd = mr.build_command(bam=bam, regions_bed=regions, prefix=tmp_path / "cov")
        assert "--by" in cmd
        assert str(regions) in cmd

    def test_bam_is_the_final_argument(self, tmp_path):
        """mosdepth's positional order is `<prefix> <bam>`; swapping them
        makes mosdepth treat the BAM as the output prefix and write files
        next to the alignment."""
        bam = tmp_path / "aln.bam"
        cmd = mr.build_command(
            bam=bam, windows_bed=tmp_path / "w.bed", prefix=tmp_path / "cov"
        )
        assert cmd[-1] == str(bam)
        assert cmd[-2] == str(tmp_path / "cov")

    def test_suppresses_per_base_output(self, tmp_path):
        """The per-base file is one line per base -- gigabytes on a real
        genome, and nothing in the coverage report reads it."""
        cmd = mr.build_command(
            bam=tmp_path / "a.bam",
            windows_bed=tmp_path / "w.bed",
            prefix=tmp_path / "cov",
        )
        assert "--no-per-base" in cmd

    def test_rejects_both_sources(self, tmp_path):
        """Exactly one --by source. mosdepth takes a single --by, so passing
        both silently drops one -- the caller must choose."""
        with pytest.raises(ValueError):
            mr.build_command(
                bam=tmp_path / "a.bam",
                windows_bed=tmp_path / "w.bed",
                regions_bed=tmp_path / "r.bed",
                prefix=tmp_path / "cov",
            )

    def test_rejects_neither_source(self, tmp_path):
        with pytest.raises(ValueError):
            mr.build_command(bam=tmp_path / "a.bam", prefix=tmp_path / "cov")

    def test_threads_are_configurable(self, tmp_path):
        cmd = mr.build_command(
            bam=tmp_path / "a.bam",
            windows_bed=tmp_path / "w.bed",
            prefix=tmp_path / "cov",
            threads=4,
        )
        assert cmd[cmd.index("-t") + 1] == "4"


class TestBuildWindowsBed:
    def test_tiles_each_contig_like_gc_tracks(self):
        lengths = {"chr1": 5_000_000, "tiny": 50}
        beds = mr.build_windows_bed(lengths)
        chr1 = [b for b in beds if b[0] == "chr1"]
        assert len(chr1) == min(mr.WINDOW_COUNT, 5_000_000 // mr.MIN_WINDOW_BASES)
        # A contig shorter than one window yields nothing, matching
        # gc_tracks' `window_count == 0: continue`.
        assert [b for b in beds if b[0] == "tiny"] == []

    def test_emits_exactly_window_count_windows(self):
        """The count must match gc_tracks exactly, because the depth track is
        drawn on the same axis as the GC track.

        `range(0, length, width)` -- the obvious construction -- emits a
        stray short final window whenever width does not divide length, which
        would put the two tracks a window out of step.
        """
        beds = mr.build_windows_bed({"chr1": 1_000_003})
        expected = min(mr.WINDOW_COUNT, 1_000_003 // mr.MIN_WINDOW_BASES)
        assert len(beds) == expected

    def test_final_window_reaches_the_contig_end(self):
        """gc_tracks' last chunk runs to the end of the sequence, so the last
        window here must too -- otherwise the tail of every contig is missing
        from the depth track."""
        beds = mr.build_windows_bed({"chr1": 1_000_003})
        assert beds[-1][2] == 1_000_003

    def test_windows_are_contiguous_and_ordered(self):
        beds = mr.build_windows_bed({"chr1": 100_000})
        assert beds[0][1] == 0
        # strict=False deliberately: the two slices differ in length by one
        # by construction, which is the point of pairing them.
        for prev, cur in zip(beds, beds[1:], strict=False):
            assert prev[2] == cur[1], "windows must tile without gaps or overlap"

    def test_short_contig_still_windows_when_at_least_one_fits(self):
        beds = mr.build_windows_bed({"c": 250})
        assert len(beds) == 2
        assert beds[0] == ("c", 0, 125)
        assert beds[-1][2] == 250

    def test_matches_gc_tracks_window_constants(self):
        """These are imported rather than redeclared; this asserts the import
        has not been replaced by a local copy that can drift."""
        assert mr.WINDOW_COUNT == gc_tracks.WINDOW_COUNT
        assert mr.MIN_WINDOW_BASES == gc_tracks.MIN_WINDOW_BASES

    def test_renders_bed_text_as_tab_separated_lines(self):
        text = mr.render_windows_bed([("chr1", 0, 100), ("chr1", 100, 200)])
        assert text == "chr1\t0\t100\nchr1\t100\t200\n"


class TestContigLengthsFromFai:
    def test_reads_name_and_length_in_reference_order(self, tmp_path):
        fai = tmp_path / "ref.fa.fai"
        # Real .fai columns: name, length, offset, linebases, linewidth.
        fai.write_text("chr2\t600\t10\t60\t61\nchr1\t2000\t700\t60\t61\n")
        lengths = mr.contig_lengths_from_fai(fai)
        assert lengths == {"chr2": 600, "chr1": 2000}
        # Order is the .fai's, which is the order mosdepth reports in.
        assert list(lengths) == ["chr2", "chr1"]

    def test_skips_blank_and_malformed_rows(self, tmp_path):
        fai = tmp_path / "ref.fa.fai"
        fai.write_text("chr1\t100\t0\t60\t61\n\nbroken\n")
        assert mr.contig_lengths_from_fai(fai) == {"chr1": 100}


class TestParseSummary:
    def test_reads_per_contig_mean_and_ignores_region_rows(self, tmp_path):
        """mosdepth writes a `<contig>_region` row beside every contig row
        when --by is used. Counting those as contigs doubles the contig list
        and halves nothing else -- captured output, not a hypothetical."""
        path = tmp_path / "cov.mosdepth.summary.txt"
        path.write_text(SUMMARY_FIXTURE)
        parsed = mr.parse_summary(path)
        assert [c["name"] for c in parsed["contigs"]] == ["chr1", "chr2"]
        assert parsed["contigs"][0] == {
            "name": "chr1",
            "length": 2000,
            "bases": 3000,
            "mean": 1.50,
            "min": 0,
            "max": 2,
        }

    def test_reads_the_total_row_as_the_genome_wide_figure(self, tmp_path):
        path = tmp_path / "s.txt"
        path.write_text(SUMMARY_FIXTURE)
        parsed = mr.parse_summary(path)
        assert parsed["total"]["mean"] == 1.31
        assert parsed["total"]["length"] == 2600
        assert parsed["total"]["bases"] == 3400

    def test_total_row_is_not_listed_as_a_contig(self, tmp_path):
        path = tmp_path / "s.txt"
        path.write_text(SUMMARY_FIXTURE)
        parsed = mr.parse_summary(path)
        assert "total" not in [c["name"] for c in parsed["contigs"]]
        assert "total_region" not in [c["name"] for c in parsed["contigs"]]

    def test_missing_file_yields_empty_rather_than_raising(self, tmp_path):
        parsed = mr.parse_summary(tmp_path / "absent.txt")
        assert parsed == {"contigs": [], "total": None}

    def test_a_contig_genuinely_named_with_region_suffix_survives(self, tmp_path):
        """The `_region` filter keys on a matching base row, not on the
        suffix alone: a real contig called `scaffold_region` (assemblers do
        emit such names) must not be dropped."""
        path = tmp_path / "s.txt"
        path.write_text(
            "chrom\tlength\tbases\tmean\tmin\tmax\n"
            "scaffold_region\t900\t450\t0.50\t0\t1\n"
            "total\t900\t450\t0.50\t0\t1\n"
        )
        parsed = mr.parse_summary(path)
        assert [c["name"] for c in parsed["contigs"]] == ["scaffold_region"]


class TestParseRegions:
    def _gz(self, tmp_path, text, name="cov.regions.bed.gz"):
        path = tmp_path / name
        with gzip.open(path, "wt") as fh:
            fh.write(text)
        return path

    def test_reads_per_window_depth(self, tmp_path):
        path = self._gz(tmp_path, REGIONS_FIXTURE)
        parsed = mr.parse_regions(path)
        assert parsed["chr1"] == [
            {"start": 0, "end": 500, "depth": 1.85, "name": None},
            {"start": 500, "end": 1000, "depth": 2.00, "name": None},
            {"start": 1000, "end": 2000, "depth": 1.07, "name": None},
        ]
        assert parsed["chr2"] == [
            {"start": 0, "end": 600, "depth": 0.67, "name": None}
        ]

    def test_reads_names_from_a_five_column_bed(self, tmp_path):
        """With a 4-column target BED, mosdepth carries the name into column
        4 and moves depth to column 5 -- so a parser hardcoding column 4 as
        depth reads a gene name as a float."""
        path = self._gz(tmp_path, NAMED_REGIONS_FIXTURE)
        parsed = mr.parse_regions(path)
        assert parsed["chr1"][0]["name"] == "geneA"
        assert parsed["chr1"][0]["depth"] == 1.85
        assert parsed["chr2"][0]["name"] == "geneB"

    def test_missing_file_yields_empty(self, tmp_path):
        assert mr.parse_regions(tmp_path / "absent.bed.gz") == {}

    def test_blank_lines_are_skipped(self, tmp_path):
        path = self._gz(tmp_path, REGIONS_FIXTURE + "\n")
        parsed = mr.parse_regions(path)
        assert len(parsed["chr2"]) == 1


class TestParseDist:
    def test_reads_breadth_thresholds_from_the_total_series(self, tmp_path):
        path = tmp_path / "cov.mosdepth.global.dist.txt"
        path.write_text(DIST_FIXTURE)
        parsed = mr.parse_dist(path)
        assert parsed[1] == pytest.approx(0.74)
        assert parsed[2] == pytest.approx(0.71)

    def test_missing_file_yields_empty(self, tmp_path):
        assert mr.parse_dist(tmp_path / "absent.txt") == {}


class TestSummarize:
    def _report(self, tmp_path):
        summary = tmp_path / "cov.mosdepth.summary.txt"
        summary.write_text(SUMMARY_FIXTURE)
        dist = tmp_path / "cov.mosdepth.global.dist.txt"
        dist.write_text(DIST_FIXTURE)
        regions = tmp_path / "cov.regions.bed.gz"
        with gzip.open(regions, "wt") as fh:
            fh.write(REGIONS_FIXTURE)
        return mr.build_report(prefix=tmp_path / "cov")

    def test_build_report_collects_every_output(self, tmp_path):
        report = self._report(tmp_path)
        assert report["total"]["mean"] == 1.31
        assert len(report["contigs"]) == 2
        assert "chr1" in report["regions"]

    def test_facts_carry_mean_depth_and_breadth(self, tmp_path):
        facts = mr.summarize(self._report(tmp_path))
        assert facts["coverage_mean_depth"] == 1.31
        assert facts["coverage_bases_covered"] == 3400
        assert facts["coverage_reference_length"] == 2600
        assert facts["coverage_contig_count"] == 2

    def test_facts_include_breadth_percentages(self, tmp_path):
        facts = mr.summarize(self._report(tmp_path))
        # 0.74 of bases at >= 1x, as a percentage.
        assert facts["coverage_pct_at_1x"] == pytest.approx(74.0)

    def test_summarize_tolerates_an_empty_report(self):
        """A mosdepth run that produced nothing must yield no facts rather
        than a dict of Nones that renders as blank rows in the UI."""
        assert mr.summarize({"contigs": [], "total": None, "regions": {}}) == {}
