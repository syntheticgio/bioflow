"""Filtlong command construction and report parsing.

Filtlong writes a plain-text summary to stdout/stderr, not JSON. This test
file validates the pure functions that build the command line and parse that
summary, using sample output taken from Filtlong 0.2.3.
"""

from pathlib import Path

from app.pipelines.filtlong_runner import (
    FiltlongParams,
    build_command,
    output_name,
    parse_report,
)

SAMPLE_REPORT = """Filtlong v0.2.3

Total reads:  10000
Total bases:  500000000 (462.3 Mb)
Reads kept:   8000 (80.0%)
Reads discarded:
  Too short:  1000 (10.0%)
  Too low quality: 1000 (10.0%)
Bases kept:   450000000 (429.6 Mb) (90.0%)
Bases discarded:
  Too short:  30000000 (6.0%)
  Too low quality: 20000000 (4.0%)
"""


def _parse_and_write(text: str, tmp_path: Path) -> dict:
    report_file = tmp_path / "filtlong_report.txt"
    report_file.write_text(text)
    return parse_report(report_file)


class TestParseReport:
    def test_extracts_before_counts(self, tmp_path):
        report = tmp_path / "r.txt"
        report.write_text(SAMPLE_REPORT)
        result = parse_report(report)

        assert result["before"]["total_reads"] == 10000
        assert result["before"]["total_bases"] == 500000000

    def test_extracts_after_counts(self, tmp_path):
        report = tmp_path / "r.txt"
        report.write_text(SAMPLE_REPORT)
        result = parse_report(report)

        assert result["after"]["total_reads"] == 8000
        assert result["after"]["total_bases"] == 450000000

    def test_extracts_discarded_reasons(self, tmp_path):
        report = tmp_path / "r.txt"
        report.write_text(SAMPLE_REPORT)
        result = parse_report(report)

        # "Reads discarded:" section carries per-reason read counts
        assert result["filtering"]["too_short_reads"] == 1000
        assert result["filtering"]["too_low_quality_reads"] == 1000

    def test_returns_empty_for_missing_file(self):
        result = parse_report(Path("/nonexistent/path/report.txt"))
        assert result == {}

    def test_reports_tool_name(self, tmp_path):
        report = tmp_path / "r.txt"
        report.write_text(SAMPLE_REPORT)
        result = parse_report(report)
        assert result["tool"] == "filtlong"


class TestOutputName:
    def test_preserves_gz_extension(self):
        assert output_name("sample.fastq.gz") == "sample.filtered.fastq.gz"

    def test_preserves_fq_extension(self):
        assert output_name("sample.fq") == "sample.filtered.fq"

    def test_handles_path(self):
        assert output_name("/data/reads.fastq") == "/data/reads.filtered.fastq"


class TestBuildCommand:
    def test_uses_default_params(self):
        params = FiltlongParams()
        cmd = build_command(
            filtlong_path="/usr/local/bin/filtlong",
            r1_in=Path("/data/input.fastq"),
            r1_out=Path("/data/output.fastq"),
            params=params,
        )

        assert cmd[0] == "/usr/local/bin/filtlong"
        assert "--min_length" in cmd
        assert "--min_mean_q" in cmd
        assert "--keep_percent" in cmd
        assert "-o" in cmd
        assert "/data/output.fastq" in cmd
        assert "/data/input.fastq" in cmd

    def test_includes_target_bases_when_set(self):
        params = FiltlongParams(target_bases=1000000)
        cmd = build_command(
            filtlong_path="/usr/local/bin/filtlong",
            r1_in=Path("/data/input.fastq"),
            r1_out=Path("/data/output.fastq"),
            params=params,
        )

        idx = cmd.index("--target_bases")
        assert cmd[idx + 1] == "1000000"

    def test_omits_target_bases_when_none(self):
        params = FiltlongParams()
        cmd = build_command(
            filtlong_path="/usr/local/bin/filtlong",
            r1_in=Path("/data/input.fastq"),
            r1_out=Path("/data/output.fastq"),
            params=params,
        )

        assert "--target_bases" not in cmd

    def test_uses_short_read1_for_mate(self):
        """The long-read input goes as the primary -o argument, and the mate
        goes as --short_read1."""
        cmd = build_command(
            filtlong_path="/usr/local/bin/filtlong",
            r1_in=Path("/data/long.fastq"),
            r1_out=Path("/data/out.fastq"),
            params=FiltlongParams(),
            r2_in=Path("/data/short_R2.fastq"),
        )

        assert "--short_read1" in cmd
        r2_idx = cmd.index("--short_read1")
        assert cmd[r2_idx + 1] == "/data/short_R2.fastq"
        # The long-read input is the trailing positional argument
        assert "/data/long.fastq" in cmd
