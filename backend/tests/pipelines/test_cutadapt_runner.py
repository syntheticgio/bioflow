"""cutadapt command construction and JSON report extraction.

The sample JSON in these tests is drawn from cutadapt's own documented report
schema (stable since 3.5) rather than invented -- the parsing exists to
survive that exact shape.
"""

import json
from pathlib import Path

import pytest

from app.pipelines import cutadapt_runner
from app.pipelines.cutadapt_runner import CutadaptParams


def cmd_for(**kw):
    defaults = dict(
        cutadapt_path="/usr/bin/cutadapt",
        r1_in=Path("in_R1.fastq.gz"),
        r1_out=Path("out_R1.fastq.gz"),
        json_out=Path("cutadapt.json"),
        params=CutadaptParams(),
    )
    defaults.update(kw)
    return cutadapt_runner.build_command(**defaults)


def flag_value(cmd: list[str], flag: str) -> str | None:
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


class TestBuildCommand:
    def test_single_end_has_no_second_input_or_output(self):
        cmd = cmd_for()
        assert "-p" not in cmd
        assert "-A" not in cmd
        assert flag_value(cmd, "-o") == "out_R1.fastq.gz"
        assert cmd[-1] == "in_R1.fastq.gz"

    def test_paired_end_passes_both_sides(self):
        cmd = cmd_for(
            r2_in=Path("in_R2.fastq.gz"), r2_out=Path("out_R2.fastq.gz")
        )
        assert flag_value(cmd, "-o") == "out_R1.fastq.gz"
        assert flag_value(cmd, "-p") == "out_R2.fastq.gz"
        assert cmd[-2:] == ["in_R1.fastq.gz", "in_R2.fastq.gz"]

    def test_paired_input_without_an_output_is_rejected(self):
        with pytest.raises(ValueError, match="second output"):
            cmd_for(r2_in=Path("in_R2.fastq.gz"))

    def test_json_report_path_is_passed(self):
        cmd = cmd_for(json_out=Path("report.cutadapt.json"))
        assert "--json=report.cutadapt.json" in cmd

    def test_quality_and_length_thresholds_are_passed(self):
        cmd = cmd_for(params=CutadaptParams(quality_cutoff=20, min_length=50))
        assert flag_value(cmd, "-q") == "20"
        assert flag_value(cmd, "-m") == "50"

    def test_cores_are_passed(self):
        assert flag_value(cmd_for(params=CutadaptParams(threads=8)), "-j") == "8"

    def test_adapter_r1_uses_lowercase_a(self):
        cmd = cmd_for(params=CutadaptParams(adapter_r1="AGATCGGAAGAGC"))
        assert flag_value(cmd, "-a") == "AGATCGGAAGAGC"
        assert "-A" not in cmd

    def test_adapter_r2_uses_uppercase_a_and_requires_pairing(self):
        cmd = cmd_for(
            r2_in=Path("in_R2.fastq.gz"),
            r2_out=Path("out_R2.fastq.gz"),
            params=CutadaptParams(
                adapter_r1="AGATCGGAAGAGC", adapter_r2="AGATCGGAAGAGC"
            ),
        )
        assert flag_value(cmd, "-A") == "AGATCGGAAGAGC"

    def test_no_adapter_given_omits_a_and_bigA(self):
        """Unlike fastp, cutadapt has no auto-detect mode -- omitting the
        adapter just runs quality trimming with no adapter search."""
        cmd = cmd_for()
        assert "-a" not in cmd
        assert "-A" not in cmd

    def test_minlen_defaults_to_one(self):
        """Without -m, cutadapt keeps zero-length reads, which breaks
        downstream tools -- CutadaptParams defaults min_length to 1."""
        assert CutadaptParams().min_length == 1


class TestCutadaptParams:
    def test_round_trip(self):
        p = CutadaptParams(quality_cutoff=25, min_length=30, threads=2)
        assert CutadaptParams.from_dict(p.as_dict()) == p

    def test_unknown_keys_are_ignored(self):
        assert CutadaptParams.from_dict({"bogus": 1}) == CutadaptParams()

    def test_none_values_fall_back_to_defaults(self):
        assert CutadaptParams.from_dict({"quality_cutoff": None}) == CutadaptParams()


SAMPLE_REPORT = {
    "tag": "Cutadapt report",
    "schema_version": [0, 3],
    "cutadapt_version": "4.9",
    "cores": 4,
    "input": {"path1": "in_R1.fastq.gz", "path2": "in_R2.fastq.gz", "paired": True},
    "read_counts": {
        "input": 100000,
        "filtered": {"too_short": 251},
        "output": 97688,
        "read1_with_adapter": 2254,
        "read2_with_adapter": 2201,
    },
    "basepair_counts": {
        "input": 10100000,
        "quality_trimmed": 842048,
        "output": 9037053,
    },
}


class TestParseReport:
    def test_extracts_scalar_summary(self, tmp_path):
        path = tmp_path / "cutadapt.json"
        path.write_text(json.dumps(SAMPLE_REPORT))

        report = cutadapt_runner.parse_report(path)

        assert report["tool"] == "cutadapt"
        assert report["tool_version"] == "4.9"
        assert report["before"]["total_reads"] == 100000
        assert report["before"]["total_bases"] == 10100000
        assert report["after"]["total_reads"] == 97688
        assert report["after"]["total_bases"] == 9037053
        assert report["filtering"]["too_short_reads"] == 251
        assert report["adapters"]["trimmed_reads_r1"] == 2254
        assert report["adapters"]["trimmed_reads_r2"] == 2201

    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert cutadapt_runner.parse_report(tmp_path / "nope.json") == {}

    def test_malformed_json_returns_empty_dict(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert cutadapt_runner.parse_report(path) == {}


class TestOutputName:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("sample_R1.fastq.gz", "sample_R1.trimmed.fastq.gz"),
            ("sample.fq", "sample.trimmed.fq"),
            ("sample_R2.fastq", "sample_R2.trimmed.fastq"),
        ],
    )
    def test_preserves_suffix_and_mate_token(self, source, expected):
        assert cutadapt_runner.output_name(source) == expected
