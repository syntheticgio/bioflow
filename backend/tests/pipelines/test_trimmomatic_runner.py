"""Trimmomatic command construction and -summary file parsing.

Trimmomatic has no JSON report, but both TrimmomaticPE and TrimmomaticSE
support `-summary <file>`, which writes a clean Key: Value-per-line file --
confirmed against a real run in the Docker image during planning (see
plans/cutadapt-trimmomatic-runners.md Task 1). parse_summary reads that file,
not stdout -- a real file beats parsing conversational completion-line text.
"""

from pathlib import Path

import pytest
from app.pipelines import trimmomatic_runner
from app.pipelines.trimmomatic_runner import TrimmomaticParams


def cmd_for(**kw):
    defaults = dict(
        trimmomatic_pe_path="/usr/bin/TrimmomaticPE",
        trimmomatic_se_path="/usr/bin/TrimmomaticSE",
        adapters_dir="/usr/share/trimmomatic",
        r1_in=Path("in_R1.fastq.gz"),
        r1_out=Path("out_R1.fastq.gz"),
        summary_out=Path("summary.txt"),
        params=TrimmomaticParams(),
    )
    defaults.update(kw)
    return trimmomatic_runner.build_command(**defaults)


class TestBuildCommand:
    def test_single_end_uses_the_se_binary(self):
        cmd = cmd_for()
        assert cmd[0] == "/usr/bin/TrimmomaticSE"
        assert "in_R1.fastq.gz" in cmd
        assert "out_R1.fastq.gz" in cmd

    def test_paired_end_uses_the_pe_binary_and_four_outputs(self):
        cmd = cmd_for(
            r2_in=Path("in_R2.fastq.gz"),
            r2_out=Path("out_R2.fastq.gz"),
            unpaired_r1_out=Path("unpaired_R1.fastq.gz"),
            unpaired_r2_out=Path("unpaired_R2.fastq.gz"),
        )
        assert cmd[0] == "/usr/bin/TrimmomaticPE"
        # PE takes two inputs then FOUR outputs: paired1, unpaired1, paired2, unpaired2.
        assert "in_R1.fastq.gz" in cmd
        assert "in_R2.fastq.gz" in cmd
        assert "out_R1.fastq.gz" in cmd
        assert "unpaired_R1.fastq.gz" in cmd
        assert "out_R2.fastq.gz" in cmd
        assert "unpaired_R2.fastq.gz" in cmd

    def test_paired_input_without_unpaired_outputs_is_rejected(self):
        with pytest.raises(ValueError, match="unpaired output"):
            cmd_for(r2_in=Path("in_R2.fastq.gz"), r2_out=Path("out_R2.fastq.gz"))

    def test_threads_flag_precedes_the_inputs(self):
        cmd = cmd_for(params=TrimmomaticParams(threads=6))
        assert "-threads" in cmd
        assert cmd[cmd.index("-threads") + 1] == "6"

    def test_phred33_and_summary_path_are_always_passed(self):
        """-phred33 avoids Trimmomatic's "Unable to detect quality encoding"
        failure on inputs too short to autodetect from -- confirmed against a
        real run during planning. -summary is what parse_summary reads."""
        cmd = cmd_for(summary_out=Path("run_summary.txt"))
        assert "-phred33" in cmd
        assert "-summary" in cmd
        assert cmd[cmd.index("-summary") + 1] == "run_summary.txt"

    def test_illuminaclip_step_uses_the_configured_adapter_file(self):
        cmd = cmd_for(params=TrimmomaticParams(adapter_file="TruSeq3-SE.fa"))
        clip_steps = [a for a in cmd if a.startswith("ILLUMINACLIP:")]
        assert clip_steps == ["ILLUMINACLIP:/usr/share/trimmomatic/TruSeq3-SE.fa:2:30:10"]

    def test_no_adapter_file_omits_illuminaclip(self):
        cmd = cmd_for(params=TrimmomaticParams(adapter_file=None))
        assert not any(a.startswith("ILLUMINACLIP:") for a in cmd)

    def test_sliding_window_and_minlen_steps(self):
        cmd = cmd_for(
            params=TrimmomaticParams(
                sliding_window_size=4, sliding_window_quality=15, min_length=36
            )
        )
        assert "SLIDINGWINDOW:4:15" in cmd
        assert "MINLEN:36" in cmd

    def test_paired_end_picks_the_pe_adapter_file_by_default(self):
        """TruSeq3-PE.fa for paired input, TruSeq3-SE.fa for single-end --
        using the wrong one is a silent quality regression, not an error."""
        se_cmd = cmd_for()
        assert "ILLUMINACLIP:/usr/share/trimmomatic/TruSeq3-SE.fa:2:30:10" in se_cmd

        pe_cmd = cmd_for(
            r2_in=Path("in_R2.fastq.gz"),
            r2_out=Path("out_R2.fastq.gz"),
            unpaired_r1_out=Path("unpaired_R1.fastq.gz"),
            unpaired_r2_out=Path("unpaired_R2.fastq.gz"),
        )
        assert "ILLUMINACLIP:/usr/share/trimmomatic/TruSeq3-PE.fa:2:30:10" in pe_cmd


class TestTrimmomaticParams:
    def test_round_trip(self):
        p = TrimmomaticParams(min_length=50, threads=2)
        assert TrimmomaticParams.from_dict(p.as_dict()) == p

    def test_unknown_keys_are_ignored(self):
        assert TrimmomaticParams.from_dict({"bogus": 1}) == TrimmomaticParams()


# Both fixtures are byte-for-byte what `-summary <file>` wrote in a real
# TrimmomaticSE/PE run against biopipe-api:latest during planning -- not
# invented from the upstream doc. See plans/cutadapt-trimmomatic-runners.md
# Task 1 for the exact commands that produced these.
SE_SUMMARY_FILE = """\
Input Reads: 200
Surviving Reads: 200
Surviving Read Percent: 100.00
Dropped Reads: 0
Dropped Read Percent: 0.00
"""

PE_SUMMARY_FILE = """\
Input Read Pairs: 200
Both Surviving Reads: 200
Both Surviving Read Percent: 100.00
Forward Only Surviving Reads: 0
Forward Only Surviving Read Percent: 0.00
Reverse Only Surviving Reads: 0
Reverse Only Surviving Read Percent: 0.00
Dropped Reads: 0
Dropped Read Percent: 0.00
"""

# A realistic non-trivial SE run, to exercise Surviving < Input.
SE_SUMMARY_WITH_DROPS = """\
Input Reads: 100000
Surviving Reads: 97688
Surviving Read Percent: 97.69
Dropped Reads: 2312
Dropped Read Percent: 2.31
"""

PE_SUMMARY_WITH_DROPS = """\
Input Read Pairs: 100000
Both Surviving Reads: 97688
Both Surviving Read Percent: 97.69
Forward Only Surviving Reads: 1200
Forward Only Surviving Read Percent: 1.20
Reverse Only Surviving Reads: 800
Reverse Only Surviving Read Percent: 0.80
Dropped Reads: 312
Dropped Read Percent: 0.31
"""


class TestParseSummary:
    def test_single_end_file(self, tmp_path):
        path = tmp_path / "summary.txt"
        path.write_text(SE_SUMMARY_WITH_DROPS)

        report = trimmomatic_runner.parse_summary(path, paired=False)

        assert report["tool"] == "trimmomatic"
        assert report["before"]["total_reads"] == 100000
        assert report["after"]["total_reads"] == 97688
        assert report["filtering"]["dropped_reads"] == 2312

    def test_paired_end_file(self, tmp_path):
        path = tmp_path / "summary.txt"
        path.write_text(PE_SUMMARY_WITH_DROPS)

        report = trimmomatic_runner.parse_summary(path, paired=True)

        assert report["tool"] == "trimmomatic"
        # "Both Surviving" is the pair-level survival count, which is what
        # `after.total_reads` means for every other tool's report too: reads
        # that came out the far end usable, not a per-mate tally.
        assert report["before"]["total_reads"] == 100000
        assert report["after"]["total_reads"] == 97688
        assert report["filtering"]["dropped_reads"] == 312

    def test_zero_drops_file(self, tmp_path):
        path = tmp_path / "summary.txt"
        path.write_text(SE_SUMMARY_FILE)

        report = trimmomatic_runner.parse_summary(path, paired=False)

        assert report["before"]["total_reads"] == 200
        assert report["after"]["total_reads"] == 200
        assert report["filtering"]["dropped_reads"] == 0

    def test_paired_zero_drops_file(self, tmp_path):
        path = tmp_path / "summary.txt"
        path.write_text(PE_SUMMARY_FILE)

        report = trimmomatic_runner.parse_summary(path, paired=True)

        assert report["after"]["total_reads"] == 200
        assert report["filtering"]["dropped_reads"] == 0

    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert trimmomatic_runner.parse_summary(tmp_path / "nope.txt", paired=False) == {}

    def test_malformed_file_returns_empty_dict(self, tmp_path):
        path = tmp_path / "summary.txt"
        path.write_text("not a summary file\njust some text\n")
        assert trimmomatic_runner.parse_summary(path, paired=False) == {}


class TestOutputName:
    def test_preserves_suffix(self):
        assert (
            trimmomatic_runner.output_name("sample_R1.fastq.gz")
            == "sample_R1.trimmed.fastq.gz"
        )
