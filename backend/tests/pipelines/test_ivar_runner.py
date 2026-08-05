"""iVar command construction and consensus stderr parsing.

Command shapes and output format verified against a real installed 1.4.4
binary on 2026-08-05, not assumed from --help text: synthetic reads
(wgsim) aligned with minimap2 against a 2kb random reference, trimmed with
a real 2-primer BED, sorted, piped through `samtools mpileup -A -d 0 -Q 0`
into `ivar consensus`. Confirmed there:

- `ivar trim -p trimmed` writes `trimmed.bam`, unsorted -- the sort step
  the design calls out as easy to omit is real, not theoretical.
- `ivar consensus -p <prefix>` writes `<prefix>.fa` and `<prefix>.qual.txt`.
- The summary iVar prints (Reference length / positions with 0 depth /
  positions with depth below N) goes to **stderr**, not a file.
- Low-depth positions are literal `N` characters in the FASTA body.
"""

from pathlib import Path

from app.pipelines.ivar_runner import (
    ConsensusParams,
    build_consensus_command,
    build_sort_command,
    build_trim_command,
    parse_consensus_stderr,
)


class TestBuildTrimCommand:
    def test_command_shape(self):
        cmd = build_trim_command(
            ivar_path="/usr/bin/ivar",
            bam=Path("/w/aln.sorted.bam"),
            primer_bed=Path("/w/primers.bed"),
            out_prefix=Path("/w/trimmed"),
        )
        assert cmd == [
            "/usr/bin/ivar",
            "trim",
            "-i",
            "/w/aln.sorted.bam",
            "-b",
            "/w/primers.bed",
            "-p",
            "/w/trimmed",
        ]


class TestBuildSortCommand:
    def test_command_shape(self):
        cmd = build_sort_command(
            samtools_path="/usr/bin/samtools",
            bam=Path("/w/trimmed.bam"),
            out=Path("/w/trimmed.sorted.bam"),
        )
        assert cmd == [
            "/usr/bin/samtools",
            "sort",
            "-o",
            "/w/trimmed.sorted.bam",
            "/w/trimmed.bam",
        ]


class TestBuildConsensusCommand:
    def test_pipeline_shape_is_an_sh_pipefail_invocation(self):
        """Same shape align_runner.build_align_command uses for aligner |
        samtools sort, and for the same reason: the exit status of a shell
        pipe is the *last* command's, so `samtools mpileup | ivar consensus`
        would report ivar's success over a pileup that never happened
        without an explicit pipefail wrapper. sh, not bash: the base image
        has no bash."""
        cmd = build_consensus_command(
            samtools_path="/usr/bin/samtools",
            ivar_path="/usr/bin/ivar",
            bam=Path("/w/trimmed.sorted.bam"),
            reference=Path("/w/ref.fa"),
            out_prefix=Path("/w/consensus"),
            params=ConsensusParams(min_quality=20, min_freq=0.0, min_depth=10),
        )
        assert cmd[:3] == ["/bin/sh", "-o", "pipefail"]
        assert cmd[3] == "-c"
        pipeline = cmd[4]
        assert pipeline == (
            "/usr/bin/samtools mpileup -A -d 0 -Q 0 --reference /w/ref.fa "
            "/w/trimmed.sorted.bam | /usr/bin/ivar consensus -p /w/consensus "
            "-q 20 -t 0.0 -m 10"
        )

    def test_thresholds_are_never_silently_defaulted_by_ivar(self):
        """iVar defaults -t (min freq) to 0 and -q to 20 on its own, but the
        design records these as facts on the output object -- so the runner
        must always pass them explicitly rather than omitting flags and
        letting iVar's own defaults apply silently."""
        cmd = build_consensus_command(
            samtools_path="samtools",
            ivar_path="ivar",
            bam=Path("/w/trimmed.sorted.bam"),
            reference=Path("/w/ref.fa"),
            out_prefix=Path("/w/consensus"),
            params=ConsensusParams(min_quality=15, min_freq=0.5, min_depth=5),
        )
        pipeline = cmd[4]
        assert "-q 15" in pipeline
        assert "-t 0.5" in pipeline
        assert "-m 5" in pipeline

    def test_paths_with_spaces_are_shell_quoted(self):
        """The pipeline is a shell string, so an unquoted path with a space
        would silently split into two arguments -- shlex.quote is what
        align_runner's own _quote uses for the same reason."""
        cmd = build_consensus_command(
            samtools_path="samtools",
            ivar_path="ivar",
            bam=Path("/w/my bam/trimmed.sorted.bam"),
            reference=Path("/w/ref.fa"),
            out_prefix=Path("/w/consensus"),
            params=ConsensusParams(),
        )
        pipeline = cmd[4]
        assert "'/w/my bam/trimmed.sorted.bam'" in pipeline


class TestParseConsensusStderr:
    # Verbatim from the real 1.4.4 run described in the module docstring.
    REAL_STDERR = (
        "Minimum Quality: 20\n"
        "Threshold: 0\n"
        "Minimum depth: 10\n"
        "Minimum Insert Threshold: 0.8\n"
        "Regions with depth less than minimum depth covered by: N\n"
        "Reference length: 198\n"
        "Positions with 0 depth: 0\n"
        "Positions with depth below 10: 6\n"
    )

    def test_parses_real_stderr(self):
        facts = parse_consensus_stderr(self.REAL_STDERR)
        assert facts["consensus_reference_length"] == 198
        assert facts["consensus_zero_depth_positions"] == 0
        assert facts["consensus_low_depth_positions"] == 6

    def test_returns_empty_for_unparseable_text(self):
        """Same posture as parse_summary in completeness_runner: a summary
        that failed to parse must not fail a job that already produced a
        consensus FASTA."""
        assert parse_consensus_stderr("iVar exploded, no useful text here") == {}

    def test_returns_empty_for_empty_text(self):
        assert parse_consensus_stderr("") == {}
