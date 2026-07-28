"""Alignment command construction, progress, and flagstat parsing.

The pipefail tests carry the most weight. In a shell pipe the exit status is
the *last* command's, so `bwa-mem2 | samtools sort` reports samtools' success
even when the aligner died halfway -- and the result is a truncated BAM that
looks perfectly fine. The failure mode is a silently wrong answer rather than
an error, which is exactly the kind that needs a test rather than a review.
"""

import subprocess
from pathlib import Path

import pytest

from app.errors import ValidationError
from app.pipelines import align_runner
from app.pipelines.align_runner import (
    AlignParams,
    AlignProgress,
    Preset,
    ReadChemistry,
    ReadGroup,
    preset_for_chemistry,
)
from app.pipelines.aligners import Aligner


def rg(**kw) -> ReadGroup:
    base = dict(sample="SAMP1", library="LIB1", platform="ILLUMINA")
    return ReadGroup(**{**base, **kw})


def align_cmd(**kw) -> list[str]:
    base = dict(
        aligner=Aligner.MINIMAP2,
        aligner_path="minimap2",
        samtools_path="samtools",
        reference=Path("/w/genome.fna"),
        r1=Path("/w/r1.fq.gz"),
        r2=None,
        output=Path("/w/out.bam"),
        read_group=rg(),
        params=AlignParams(),
    )
    return align_runner.build_align_command(**{**base, **kw})


class TestPipefail:
    """The design calls for this explicitly, and it is testable for real."""

    def test_the_command_sets_pipefail(self):
        cmd = align_cmd()
        assert cmd[:3] == ["/bin/sh", "-o", "pipefail"]

    def test_a_failing_first_stage_fails_the_pipeline(self):
        """Run against a real shell rather than asserting on the string: the
        property under test is the shell's behavior, not our spelling of it."""
        proc = subprocess.run(
            ["/bin/sh", "-o", "pipefail", "-c", "false | true"], check=False
        )
        assert proc.returncode != 0

    def test_without_pipefail_the_failure_is_invisible(self):
        """The bug being prevented, asserted directly. If this ever starts
        failing, the shell has changed and the guard above may be moot."""
        proc = subprocess.run(["/bin/sh", "-c", "false | true"], check=False)
        assert proc.returncode == 0

    def test_a_successful_pipeline_still_reports_success(self):
        proc = subprocess.run(
            ["/bin/sh", "-o", "pipefail", "-c", "true | true"], check=False
        )
        assert proc.returncode == 0

    def test_the_built_command_actually_runs(self, tmp_path):
        """End to end through /bin/sh with stand-in binaries, so a quoting
        error in the generated pipeline shows up here rather than as a job
        failure. Uses `false` as the aligner to also prove pipefail survives
        command construction."""
        cmd = align_cmd(
            aligner_path="false",
            samtools_path="true",
            output=tmp_path / "out.bam",
        )
        assert subprocess.run(cmd, check=False).returncode != 0


class TestAlignCommand:
    def test_pipes_the_aligner_into_samtools_sort(self):
        """Never materializing the intermediate SAM, which is several times the
        size of the resulting BAM and pure waste to write."""
        script = align_cmd()[-1]
        assert "|" in script
        assert "samtools sort" in script

    def test_carries_the_read_group(self):
        """GATK and most variant callers refuse to run without @RG, and adding
        it afterwards means rewriting the whole BAM."""
        script = align_cmd()[-1]
        assert "@RG" in script
        assert "SM:SAMP1" in script

    def test_passes_both_mates_when_paired(self):
        script = align_cmd(r2=Path("/w/r2.fq.gz"))[-1]
        assert "r1.fq.gz" in script
        assert "r2.fq.gz" in script

    def test_omits_the_second_mate_when_single_end(self):
        script = align_cmd()[-1]
        assert "r2.fq.gz" not in script

    def test_minimap2_gets_its_preset(self):
        script = align_cmd(params=AlignParams(preset=Preset.MAP_ONT))[-1]
        assert "-x map-ont" in script

    def test_minimap2_emits_sam_not_paf(self):
        """samtools sort cannot read PAF, which is minimap2's default output."""
        assert " -a " in align_cmd()[-1]

    def test_bwa_mem2_uses_mem(self):
        script = align_cmd(aligner=Aligner.BWA_MEM2, aligner_path="bwa-mem2")[-1]
        assert "bwa-mem2 mem" in script

    def test_sort_memory_is_per_thread(self):
        script = align_cmd(params=AlignParams(sort_memory_mb=2048))[-1]
        assert "-m 2048M" in script

    def test_a_filename_with_a_space_is_quoted(self):
        """Object names are user-facing and mutable. Unquoted, a space would
        split one path into two arguments and produce a confusing 'no such
        file' from a command that is otherwise correct."""
        script = align_cmd(r1=Path("/w/my reads.fq.gz"))[-1]
        assert "'/w/my reads.fq.gz'" in script

    def test_a_filename_cannot_inject_a_shell_command(self):
        """The command runs through `sh -c`, so quoting is load-bearing rather
        than cosmetic."""
        script = align_cmd(r1=Path("/w/x.fq; rm -rf /"))[-1]
        assert "; rm -rf /" not in script.replace("'/w/x.fq; rm -rf /'", "")


class TestReadGroup:
    def test_builds_a_tab_separated_header(self):
        assert rg().as_sam_header() == "@RG\\tID:SAMP1\\tSM:SAMP1\\tLB:LIB1\\tPL:ILLUMINA"

    def test_id_defaults_to_the_sample(self):
        assert "ID:SAMP1" in rg().as_sam_header()

    def test_an_explicit_id_wins(self):
        assert "ID:RUN7" in rg(identifier="RUN7").as_sam_header()

    @pytest.mark.parametrize("missing", ["sample", "library", "platform"])
    def test_every_required_field_is_enforced(self, missing):
        """Required rather than optional, because a BAM without them has to be
        rewritten end to end to add them later."""
        data = {"sample": "S", "library": "L", "platform": "ILLUMINA"}
        del data[missing]
        with pytest.raises(ValidationError, match="Read group requires"):
            ReadGroup.from_dict(data)

    def test_round_trips_through_a_dict(self):
        assert ReadGroup.from_dict(rg().as_dict()) == rg()


class TestAlignParams:
    def test_defaults_to_minimap2_short_read(self):
        """minimap2 is the aligner available on every platform this runs on."""
        params = AlignParams.from_dict({})
        assert params.aligner is Aligner.MINIMAP2
        assert params.preset == Preset.SHORT_READ

    def test_rejects_an_unknown_preset(self):
        """The wrong preset produces silently poor alignments rather than an
        error, so an unrecognized one must be refused at launch."""
        with pytest.raises(ValidationError, match="preset"):
            AlignParams.from_dict({"preset": "map-nonsense"})

    @pytest.mark.parametrize("preset", Preset.ALL)
    def test_accepts_every_documented_preset(self, preset):
        assert AlignParams.from_dict({"preset": preset}).preset == preset

    def test_rejects_zero_threads(self):
        with pytest.raises(ValidationError, match="threads"):
            AlignParams.from_dict({"threads": 0})

    def test_rejects_a_sort_memory_that_would_thrash(self):
        with pytest.raises(ValidationError, match="sort_memory_mb"):
            AlignParams.from_dict({"sort_memory_mb": 8})

    def test_round_trips(self):
        params = AlignParams(aligner=Aligner.BWA_MEM2, preset="", threads=8)
        assert AlignParams.from_dict(params.as_dict()).threads == 8


class TestPresetForChemistry:
    """HiFi and CLR are both PACBIO_SMRT in SRA and both PACBIO in SAM, so
    platform alone cannot pick the right preset -- this is the piece that
    actually distinguishes them, and getting HiFi wrong is the bug this plan
    exists to fix."""

    @pytest.mark.parametrize("chemistry", list(ReadChemistry))
    def test_every_chemistry_maps_to_a_validated_preset(self, chemistry):
        assert preset_for_chemistry(chemistry) in Preset.ALL

    def test_hifi_gets_the_hifi_preset_not_pacbio_clr(self):
        """The regression guard for the actual bug: today every PacBio file
        gets map-pb, which is tuned for CLR's 10-15% error rate and silently
        wastes HiFi's ~99.9% accuracy."""
        assert preset_for_chemistry(ReadChemistry.HIFI) == Preset.MAP_HIFI
        assert preset_for_chemistry(ReadChemistry.HIFI) != Preset.MAP_PB

    def test_clr_keeps_the_pacbio_preset(self):
        assert preset_for_chemistry(ReadChemistry.CLR) == Preset.MAP_PB

    def test_ont_simplex_gets_the_ont_preset(self):
        assert preset_for_chemistry(ReadChemistry.ONT_SIMPLEX) == Preset.MAP_ONT

    def test_ont_duplex_gets_the_high_accuracy_preset(self):
        assert preset_for_chemistry(ReadChemistry.ONT_DUPLEX) == Preset.LR_HQ

    def test_short_gets_the_short_read_preset(self):
        assert preset_for_chemistry(ReadChemistry.SHORT) == Preset.SHORT_READ

    @pytest.mark.parametrize(
        "chemistry", [ReadChemistry.ONT_SIMPLEX, ReadChemistry.ONT_DUPLEX]
    )
    def test_an_ont_chemistry_never_yields_short_read(self, chemistry):
        assert preset_for_chemistry(chemistry) != Preset.SHORT_READ


class TestIndexCommands:
    def test_bwa_mem2_writes_beside_the_reference(self):
        """It takes no output path -- the five files land next to the input,
        which is why materialize() has to put the reference somewhere writable."""
        cmd = align_runner.build_index_command(
            aligner=Aligner.BWA_MEM2, tool_path="bwa-mem2", reference=Path("/w/g.fna")
        )
        assert cmd == ["bwa-mem2", "index", "/w/g.fna"]

    def test_minimap2_writes_where_it_is_told(self):
        cmd = align_runner.build_index_command(
            aligner=Aligner.MINIMAP2,
            tool_path="minimap2",
            reference=Path("/w/g.fna"),
            output=Path("/w/g.fna.mmi"),
        )
        assert cmd == ["minimap2", "-d", "/w/g.fna.mmi", "/w/g.fna"]

    def test_minimap2_requires_an_output_path(self):
        with pytest.raises(ValidationError, match="output path"):
            align_runner.build_index_command(
                aligner=Aligner.MINIMAP2, tool_path="minimap2", reference=Path("/w/g.fna")
            )


class TestFlagstat:
    SAMPLE = """1000 + 0 in total (QC-passed reads + QC-failed reads)
0 + 0 secondary
0 + 0 supplementary
120 + 0 duplicates
950 + 0 mapped (95.00% : N/A)
1000 + 0 paired in sequencing
500 + 0 read1
500 + 0 read2
900 + 0 properly paired (90.00% : N/A)
"""

    def test_extracts_the_headline_numbers(self):
        facts = align_runner.parse_flagstat(self.SAMPLE)
        assert facts["total_reads"] == 1000
        assert facts["mapped_reads"] == 950
        assert facts["properly_paired_reads"] == 900
        assert facts["duplicate_reads"] == 120

    def test_derives_the_rates(self):
        facts = align_runner.parse_flagstat(self.SAMPLE)
        assert facts["mapped_pct"] == 95.0
        assert facts["properly_paired_pct"] == 90.0
        assert facts["duplicate_pct"] == 12.0

    def test_counts_qc_failed_reads_too(self):
        """The second number is QC-failed. Ignoring it would understate the
        total and inflate every derived rate."""
        facts = align_runner.parse_flagstat("100 + 20 in total (QC-passed + QC-failed)")
        assert facts["total_reads"] == 120

    def test_an_empty_alignment_does_not_divide_by_zero(self):
        """flagstat prints 'N/A' for its own percentages here, which is why the
        rates are derived rather than parsed."""
        facts = align_runner.parse_flagstat("0 + 0 in total\n0 + 0 mapped (N/A : N/A)")
        assert facts["total_reads"] == 0
        assert "mapped_pct" not in facts

    def test_unrecognized_output_yields_nothing_rather_than_raising(self):
        assert align_runner.parse_flagstat("something unexpected") == {}


class TestProgress:
    def test_accumulates_batches(self):
        """bwa-mem2 reports per batch, not a running total."""
        p = AlignProgress(expected_reads=1000)
        p.feed("[M::mem_process_seqs] Processed 400 reads in 1.2 CPU sec")
        p.feed("[M::mem_process_seqs] Processed 300 reads in 1.1 CPU sec")
        assert p.processed == 700

    def test_caps_below_complete(self):
        """The read total is an estimate extrapolated from the first 1000
        records at ingest, so a bar that reaches 100% is claiming a certainty
        it does not have."""
        p = AlignProgress(expected_reads=100)
        p.feed("Processed 100000 reads")
        assert p.pct == align_runner.MAX_MEASURED_PCT

    def test_reports_indeterminate_without_an_estimate(self):
        """Better an honest indeterminate bar than an invented number."""
        p = AlignProgress(expected_reads=None)
        p.feed("Processed 500 reads")
        assert p.pct is None

    def test_notices_the_sort_phase(self):
        p = AlignProgress()
        assert p.feed("[bam_sort_core] merging from 4 files and 1 in-memory blocks...")
        assert p.phase == "sorting"

    def test_ignores_unrelated_output(self):
        p = AlignProgress()
        assert not p.feed("[M::main] Real time: 12.3 sec")
        assert p.processed == 0
