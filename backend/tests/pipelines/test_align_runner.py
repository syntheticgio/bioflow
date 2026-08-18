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
from app.pipelines import align_params, align_runner, aligner_registry
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


class TestMarkdupCommand:
    """samtools markdup needs the `ms` mate-score tag that only `fixmate -m`
    writes -- and fixmate needs name-sorted input while markdup needs
    coordinate order. Skipping fixmate is exactly the bug this guards:
    `samtools markdup: error, no ms score tag`."""

    def markdup_cmd(self, **kw):
        base = dict(
            samtools_path="samtools",
            source=Path("/w/sorted.bam"),
            output=Path("/w/marked.bam"),
            threads=4,
            paired=True,
        )
        return align_runner.build_markdup_command(**{**base, **kw})

    def test_single_end_runs_markdup_directly(self):
        """No mate to score, so there is nothing for fixmate to add."""
        cmd = self.markdup_cmd(paired=False)
        assert cmd == [
            "samtools",
            "markdup",
            "-@",
            "4",
            "/w/sorted.bam",
            "/w/marked.bam",
        ]

    def test_paired_runs_fixmate_before_markdup(self):
        script = self.markdup_cmd()[-1]
        assert "fixmate" in script
        assert script.index("fixmate") < script.index("markdup")

    def test_paired_name_sorts_before_fixmate(self):
        """fixmate requires mates adjacent in the stream, which only a
        name sort guarantees."""
        script = self.markdup_cmd()[-1]
        sort_n_pos = script.index("sort -@ 3 -n")
        assert sort_n_pos < script.index("fixmate")

    def test_paired_coordinate_sorts_after_fixmate_before_markdup(self):
        script = self.markdup_cmd()[-1]
        fixmate_pos = script.index("fixmate")
        # The second `sort` (coordinate order) must come after fixmate.
        second_sort_pos = script.index(" sort ", fixmate_pos)
        assert fixmate_pos < second_sort_pos < script.index("markdup")

    def test_paired_sets_pipefail(self):
        cmd = self.markdup_cmd()
        assert cmd[:3] == ["/bin/sh", "-o", "pipefail"]

    def test_paired_uses_tmp_prefix_for_the_coordinate_sort(self):
        script = self.markdup_cmd(tmp_prefix=Path("/w/tmp/markdup-sort"))[-1]
        assert "-T /w/tmp/markdup-sort" in script

    def test_paired_pipeline_actually_runs(self, tmp_path):
        """End to end through /bin/sh with stand-in binaries, so a quoting
        error in the generated pipeline shows up here rather than as a job
        failure."""
        cmd = self.markdup_cmd(
            samtools_path="true",
            source=tmp_path / "sorted.bam",
            output=tmp_path / "marked.bam",
        )
        assert subprocess.run(cmd, check=False).returncode == 0

    def test_bwa_mem2_uses_mem(self):
        script = align_cmd(
            aligner=Aligner.BWA_MEM2,
            aligner_path="bwa-mem2",
            params=align_params.Bwa2Params(),
        )[-1]
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

    @pytest.mark.parametrize("missing", ["sample", "library"])
    def test_every_required_field_is_enforced(self, missing):
        """Sample and library are required, because a BAM without them has to
        be rewritten end to end to add them later. Platform is not required --
        see test_read_group_pl.py::TestFromDictAcceptsAMissingPlatform -- since
        an unrecognized instrument model must not fail the whole alignment
        launch now that sam_platform can return None."""
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

    def test_bowtie2_defaults_the_basename_to_the_reference(self):
        cmd = align_runner.build_index_command(
            aligner=Aligner.BOWTIE2, tool_path="bowtie2-build", reference=Path("/w/g.fna")
        )
        assert cmd == ["bowtie2-build", "/w/g.fna", "/w/g.fna"]

    def test_hisat2_basename_can_differ_from_the_input_it_reads(self):
        """#560: hisat2-build cannot read a gzipped reference, so build_index
        feeds it a decompressed copy from a scratch directory -- but the index
        must still be written under the stored name, which is where the layout
        looks for the files afterwards. Reading from one path and writing to
        another is the whole point of `output` on this branch."""
        cmd = align_runner.build_index_command(
            aligner=Aligner.HISAT2,
            tool_path="hisat2-build",
            reference=Path("/w/build-input/g.fna"),
            output=Path("/w/ref/g.fna.gz"),
        )
        assert cmd == ["hisat2-build", "/w/build-input/g.fna", "/w/ref/g.fna.gz"]

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

    def test_phase_index_starts_at_aligning(self):
        assert AlignProgress().phase_index == 1

    def test_phase_index_advances_to_sorting(self):
        p = AlignProgress()
        p.feed("[bam_sort_core] merging from 4 files and 1 in-memory blocks...")
        assert p.phase_index == 2
        assert len(align_runner.PHASE_ORDER) == 2


class TestBowtie2Command:
    def cmd(self, **kw):
        params = align_params.from_dict({"aligner": "bowtie2", **kw})
        return align_cmd(
            aligner=Aligner.BOWTIE2, aligner_path="bowtie2", params=params
        )

    def test_reads_are_passed_with_the_paired_flags(self):
        """bowtie2 does not take positional read files the way bwa does:
        R1 goes to -1 and R2 to -2, and a bare positional would be read as
        the index basename."""
        params = align_params.from_dict({"aligner": "bowtie2"})
        cmd = align_cmd(
            aligner=Aligner.BOWTIE2,
            aligner_path="bowtie2",
            params=params,
            r2=Path("/w/r2.fq.gz"),
        )
        joined = " ".join(cmd)
        assert "-1 /w/r1.fq.gz" in joined
        assert "-2 /w/r2.fq.gz" in joined

    def test_single_end_reads_use_the_unpaired_flag(self):
        joined = " ".join(self.cmd())
        assert "-U /w/r1.fq.gz" in joined

    def test_the_index_is_passed_with_dash_x(self):
        joined = " ".join(self.cmd())
        assert "-x /w/genome.fna" in joined

    def test_threads_use_dash_p(self):
        joined = " ".join(self.cmd(threads=8))
        assert "-p 8" in joined

    def test_sensitivity_reaches_the_command(self):
        joined = " ".join(self.cmd(sensitivity="--very-sensitive"))
        assert "--very-sensitive" in joined

    def test_local_mode_is_a_flag(self):
        assert "--local" in " ".join(self.cmd(local=True))
        assert "--local" not in " ".join(self.cmd(local=False))

    def test_maxins_reaches_the_command(self):
        cmd = align_cmd(
            aligner=Aligner.BOWTIE2,
            aligner_path="bowtie2",
            params=align_params.from_dict({"aligner": "bowtie2", "maxins": 800}),
            r2=Path("/w/r2.fq.gz"),
        )
        assert "-X 800" in cmd[-1]

    def test_bowtie2_paired_command_emits_geometry_and_reporting_flags(self):
        params = align_params.Bowtie2Params.from_dict(
            {
                "aligner": "bowtie2",
                "minins": 500,
                "maxins": 20000,
                "orientation": "RF",
                "dovetail": True,
                "no_contain": True,
                "no_overlap": True,
                "no_mixed": True,
                "no_discordant": True,
                "report_k": 10,
            }
        )
        script = align_cmd(
            aligner=Aligner.BOWTIE2,
            aligner_path="bowtie2",
            params=params,
            r2=Path("/w/r2.fq.gz"),
        )[-1]
        assert "-I 500" in script
        assert "-X 20000" in script
        assert "--rf" in script
        assert "--dovetail" in script
        assert "--no-contain" in script
        assert "--no-overlap" in script
        assert "--no-mixed" in script
        assert "--no-discordant" in script
        assert "-k 10" in script
        assert " -a " not in script

    def test_bowtie2_single_end_omits_pair_only_flags(self):
        params = align_params.Bowtie2Params.from_dict(
            {
                "aligner": "bowtie2",
                "minins": 500,
                "maxins": 20000,
                "orientation": "RF",
                "dovetail": True,
                "no_contain": True,
                "no_overlap": True,
                "no_mixed": True,
                "no_discordant": True,
            }
        )
        script = align_cmd(
            aligner=Aligner.BOWTIE2,
            aligner_path="bowtie2",
            params=params,
        )[-1]
        assert " -I " not in script
        assert " -X " not in script
        for flag in (
            "--fr",
            "--rf",
            "--ff",
            "--dovetail",
            "--no-contain",
            "--no-overlap",
            "--no-mixed",
            "--no-discordant",
        ):
            assert flag not in script

    @pytest.mark.parametrize(
        ("preset_id", "expected_controls", "omitted_controls"),
        [
            (
                "standard_short_read",
                ("--sensitive", "-X 500", "--fr"),
                ("--local", "-I ", "--rf", "--dovetail", "-k "),
            ),
            (
                "long_insert",
                ("--sensitive", "-I 500", "-X 20000", "--fr"),
                ("--local", "--rf", "--dovetail", "-k "),
            ),
            (
                "mate_pair",
                ("--sensitive", "-I 500", "-X 20000", "--rf"),
                ("--local", "--fr", "--dovetail", "-k "),
            ),
            (
                "adapter_partial_reference",
                ("--sensitive", "--local", "-X 500", "--fr"),
                ("-I ", "--rf", "--dovetail", "-k "),
            ),
            (
                "structural_variant",
                ("--sensitive", "-X 500", "--fr", "--dovetail"),
                ("--local", "-I ", "--rf", "-k "),
            ),
            (
                "repeat_multimapping",
                ("--sensitive", "-X 500", "--fr", "-k 10"),
                ("--local", "-I ", "--rf", "--dovetail"),
            ),
        ],
    )
    def test_every_bowtie2_preset_resolves_to_its_command_controls(
        self, preset_id, expected_controls, omitted_controls
    ):
        preset = aligner_registry.schema_for(Aligner.BOWTIE2)["presets"][preset_id]
        params = align_params.from_dict(
            {"aligner": "bowtie2", "preset": preset_id, **preset["values"]}
        )
        script = align_cmd(
            aligner=Aligner.BOWTIE2,
            aligner_path="bowtie2",
            params=params,
            r2=Path("/w/r2.fq.gz"),
        )[-1]

        assert params.preset == preset_id
        for control in expected_controls:
            assert control in script
        for control in omitted_controls:
            assert control not in script

    def test_report_k_is_omitted_when_zero(self):
        """0 means 'leave the flag off'. Passing -k 0 tells bowtie2 to report
        zero alignments, which silently produces an empty BAM."""
        assert " -k " not in " ".join(self.cmd(report_k=0))
        assert "-k 4" in " ".join(self.cmd(report_k=4))

    def test_report_all_emits_dash_a(self):
        script = self.cmd(report_all=True)[-1]
        assert " -a " in script
        assert " -k " not in script

    def test_report_k_zero_emits_neither_k_nor_a(self):
        script = self.cmd(report_k=0, report_all=False)[-1]
        assert " -k " not in script
        assert " -a " not in script

    def test_the_read_group_is_split_into_id_and_fields(self):
        """bowtie2 has no single -R: it takes --rg-id for the ID and one --rg
        per remaining field. Passing bwa's tab-joined @RG line would put a
        literal backslash-t into the BAM header."""
        joined = " ".join(self.cmd())
        assert "--rg-id" in joined
        assert "SM:SAMP1" in joined
        assert "@RG" not in joined


class TestHisat2Command:
    def cmd(self, **kw):
        params = align_params.from_dict({"aligner": "hisat2", **kw})
        return align_cmd(
            aligner=Aligner.HISAT2, aligner_path="hisat2", params=params
        )

    def test_the_index_is_passed_with_dash_x(self):
        assert "-x /w/genome.fna" in " ".join(self.cmd())

    def test_strandness_is_omitted_when_unstranded(self):
        """The flag has no 'unstranded' value -- omitting it is how you say
        that. Passing an empty string would make HISAT2 reject the argument."""
        assert "--rna-strandness" not in " ".join(self.cmd(rna_strandness=""))
        assert "--rna-strandness RF" in " ".join(self.cmd(rna_strandness="RF"))

    def test_max_intronlen_reaches_the_command(self):
        assert "--max-intronlen 20000" in " ".join(self.cmd(max_intronlen=20000))

    def test_no_spliced_alignment_is_a_flag(self):
        assert "--no-spliced-alignment" in " ".join(
            self.cmd(no_spliced_alignment=True)
        )

    def test_dta_is_a_flag(self):
        assert "--dta" in " ".join(self.cmd(dta=True))


class TestNewAlignersKeepPipefail:
    """The truncated-BAM failure applies to every aligner, not just the two
    that existed when the pipe was written."""

    @pytest.mark.parametrize(
        "aligner", [Aligner.BOWTIE2, Aligner.HISAT2, Aligner.STAR, Aligner.WINNOWMAP]
    )
    def test_pipefail_is_set(self, aligner):
        params = align_params.from_dict({"aligner": aligner.value})
        kwargs = {}
        if aligner is Aligner.WINNOWMAP:
            kwargs["winnowmap_repetitive_kmers"] = Path("/w/genome.fna.repetitive_k15.txt")
        cmd = align_cmd(
            aligner=aligner, aligner_path=aligner.value, params=params, **kwargs
        )
        assert cmd[:3] == ["/bin/sh", "-o", "pipefail"]


class TestWinnowmapCommand:
    def cmd(self, **kw):
        params = align_params.from_dict({"aligner": "winnowmap", **kw})
        return align_cmd(
            aligner=Aligner.WINNOWMAP,
            aligner_path="winnowmap",
            params=params,
            winnowmap_repetitive_kmers=Path("/w/genome.fna.repetitive_k15.txt"),
        )

    def test_missing_repetitive_kmers_is_rejected(self):
        """Reaching the command builder for winnowmap with no repetitive-
        k-mer file is a caller bug -- there is no way to run winnowmap
        without the file meryl produces."""
        params = align_params.from_dict({"aligner": "winnowmap"})
        with pytest.raises(ValidationError):
            align_cmd(aligner=Aligner.WINNOWMAP, aligner_path="winnowmap", params=params)

    def test_dash_w_carries_the_repetitive_kmer_file(self):
        joined = " ".join(self.cmd())
        assert "-W /w/genome.fna.repetitive_k15.txt" in joined

    def test_preset_reaches_the_command_like_minimap2(self):
        """winnowmap shares minimap2's -a -x <preset> shape -- verified
        against a real build of winnowmap, which is built on minimap2's own
        codebase and shares its argument parser."""
        joined = " ".join(self.cmd(preset="map-hifi"))
        assert " -a " in joined
        assert "-x map-hifi" in joined

    def test_carries_the_read_group_like_minimap2(self):
        """Verified against a real build: `winnowmap --help` documents -R
        in minimap2's exact phrasing."""
        joined = " ".join(self.cmd())
        assert "@RG" in joined
        assert "SM:SAMP1" in joined

    def test_reads_are_positional_like_minimap2(self):
        joined = " ".join(self.cmd())
        assert "/w/r1.fq.gz" in joined

    def test_passes_both_mates_when_paired(self):
        params = align_params.from_dict({"aligner": "winnowmap"})
        cmd = align_cmd(
            aligner=Aligner.WINNOWMAP,
            aligner_path="winnowmap",
            params=params,
            r2=Path("/w/r2.fq.gz"),
            winnowmap_repetitive_kmers=Path("/w/genome.fna.repetitive_k15.txt"),
        )
        joined = " ".join(cmd)
        assert "/w/r1.fq.gz" in joined
        assert "/w/r2.fq.gz" in joined


class TestStarCommand:
    """STAR's calling convention shares nothing with the other four.

    Two of these guard failures that are silent rather than loud, which is
    why they are here rather than left to a manual run: without
    `--readFilesCommand zcat` STAR reads a gzipped FASTQ as text and reports
    every read as too short, and without `--outSAMunmapped Within` the
    mapped-percentage on the alignment report is computed over mapped reads
    only and always reads 100%.
    """

    def cmd(self, **kw):
        overrides = {k: kw.pop(k) for k in ("r1", "r2", "scratch") if k in kw}
        params = align_params.from_dict({"aligner": "star", **kw})
        return align_cmd(
            aligner=Aligner.STAR,
            aligner_path="STAR",
            params=params,
            **overrides,
        )

    def test_the_index_is_passed_as_a_genome_directory(self):
        joined = " ".join(self.cmd())
        assert "--genomeDir /w/genome.fna.STARindex" in joined
        # Not the reference itself, which STAR reads as a missing directory.
        assert "--genomeDir /w/genome.fna " not in joined

    def test_sam_is_written_to_stdout_for_samtools(self):
        """The whole point of staying inside the shared align-and-sort pipe.
        Without --outStd, STAR writes Aligned.out.sam to a file and samtools
        sorts an empty stream."""
        joined = " ".join(self.cmd())
        assert "--outStd SAM" in joined
        assert "--outSAMtype SAM" in joined

    def test_gzipped_reads_get_the_decompression_command(self):
        joined = " ".join(self.cmd(r1=Path("/w/r1.fq.gz")))
        assert "--readFilesCommand zcat" in joined

    def test_plain_reads_do_not(self):
        """zcat on an uncompressed FASTQ fails outright, so this cannot just
        be passed unconditionally."""
        joined = " ".join(self.cmd(r1=Path("/w/r1.fq")))
        assert "readFilesCommand" not in joined

    def test_a_gzipped_mate_is_enough_to_decompress(self):
        joined = " ".join(self.cmd(r1=Path("/w/r1.fq"), r2=Path("/w/r2.fq.gz")))
        assert "--readFilesCommand zcat" in joined

    def test_paired_reads_are_two_arguments_to_one_flag(self):
        joined = " ".join(self.cmd(r1=Path("/w/r1.fq"), r2=Path("/w/r2.fq")))
        assert "--readFilesIn /w/r1.fq /w/r2.fq" in joined

    def test_the_read_group_uses_stars_own_flag(self):
        """STAR takes the fields as separate arguments and rejects the
        tab-escaped @RG line the other aligners want."""
        joined = " ".join(self.cmd())
        assert "--outSAMattrRGline ID:SAMP1 SM:SAMP1 LB:LIB1 PL:ILLUMINA" in joined
        assert "\\t" not in joined

    def test_unmapped_reads_are_kept_by_default(self):
        assert "--outSAMunmapped Within" in " ".join(self.cmd())

    def test_unmapped_reads_can_be_dropped(self):
        assert "outSAMunmapped" not in " ".join(self.cmd(out_sam_unmapped=False))

    def test_two_pass_is_off_unless_asked_for(self):
        assert "twopassMode" not in " ".join(self.cmd())
        assert "--twopassMode Basic" in " ".join(self.cmd(two_pass=True))

    def test_intron_max_of_zero_leaves_the_flag_off(self):
        """0 and STAR's derived ceiling are the same behaviour, but passing
        the flag would make the recorded parameters claim a decision nobody
        made."""
        assert "alignIntronMax" not in " ".join(self.cmd())
        assert "--alignIntronMax 1" in " ".join(self.cmd(align_intron_max=1))

    def test_scratch_becomes_a_prefix_with_a_trailing_separator(self):
        """STAR concatenates the prefix with its own filenames rather than
        joining as a path. Without the slash, Log.final.out lands beside the
        directory as `starLog.final.out`."""
        joined = " ".join(self.cmd(scratch=Path("/w/star")))
        assert "--outFileNamePrefix /w/star/" in joined

    def test_threads_use_run_thread_n(self):
        assert "--runThreadN 8" in " ".join(self.cmd(threads=8))


class TestStarIndexSizing:
    """STAR's defaults are sized for a mammalian genome and go wrong quietly
    on anything else -- which is the reason these are computed at all."""

    def test_a_human_sized_genome_keeps_the_defaults(self):
        sa, chr_bin = align_runner.star_index_sizing(
            genome_length=3_100_000_000, contigs=25
        )
        assert sa == 14
        assert chr_bin == 18

    def test_a_small_genome_shrinks_the_suffix_array_index(self):
        """A 5 kb virus at the default 14 builds an index far larger than the
        genome and maps almost nothing -- while exiting 0, so the job is
        green and the BAM is empty."""
        sa, _ = align_runner.star_index_sizing(genome_length=5_000, contigs=1)
        assert sa < 14
        assert sa >= 1

    def test_a_fragmented_assembly_shrinks_the_chromosome_bins(self):
        """A bin is allocated per contig at 2^chrBinNbits bytes, so a draft
        assembly with tens of thousands of scaffolds exhausts memory during
        the build at the default 18."""
        _, chr_bin = align_runner.star_index_sizing(
            genome_length=50_000_000, contigs=40_000
        )
        assert chr_bin < 18

    def test_a_degenerate_reference_does_not_raise(self):
        """log2(0) is undefined. STAR's own error about an unusable genome is
        a better message than anything invented here, so this has to survive
        long enough to reach it."""
        sa, chr_bin = align_runner.star_index_sizing(genome_length=0, contigs=0)
        assert sa >= 1
        assert chr_bin >= 1


class TestStarIndexCommand:
    def cmd(self, **kw):
        base = dict(
            tool_path="STAR",
            reference=Path("/w/genome.fna"),
            genome_dir=Path("/w/genome.fna.STARindex"),
            threads=4,
            genome_length=3_100_000_000,
            contigs=25,
            scratch=Path("/w/index"),
        )
        return align_runner.build_star_index_command(**{**base, **kw})

    def test_it_is_a_genome_generate_run(self):
        joined = " ".join(self.cmd())
        assert "--runMode genomeGenerate" in joined
        assert "--genomeDir /w/genome.fna.STARindex" in joined
        assert "--genomeFastaFiles /w/genome.fna" in joined

    def test_sizing_reaches_the_command(self):
        joined = " ".join(self.cmd(genome_length=5_000, contigs=1))
        assert "--genomeSAindexNbases" in joined
        assert "--genomeSAindexNbases 14" not in joined

    def test_no_gtf_omits_the_annotation_flags(self):
        """The de novo path this application already ships -- 9,818 splices
        found with no GTF on real yeast data -- must stay reachable."""
        joined = " ".join(self.cmd())
        assert "--sjdbGTFfile" not in joined
        assert "--sjdbOverhang" not in joined

    def test_a_gtf_adds_the_annotation_flags(self):
        joined = " ".join(self.cmd(gtf=Path("/w/genes.gtf")))
        assert "--sjdbGTFfile /w/genes.gtf" in joined
        assert f"--sjdbOverhang {align_runner.STAR_SJDB_OVERHANG}" in joined

    def test_sjdb_overhang_is_overridable(self):
        joined = " ".join(self.cmd(gtf=Path("/w/genes.gtf"), sjdb_overhang=149))
        assert "--sjdbOverhang 149" in joined

    def test_the_generic_builder_refuses_star(self):
        """Rather than falling through to bowtie2's two-positional shape,
        which would run STAR with a reference where a subcommand belongs."""
        with pytest.raises(ValidationError):
            align_runner.build_index_command(
                aligner=Aligner.STAR, tool_path="STAR", reference=Path("/w/genome.fna")
            )
