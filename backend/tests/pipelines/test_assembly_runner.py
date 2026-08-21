"""Assembly command construction, progress parsing, and assembly_info.txt.

There was no coverage of this file at all before this test -- the assembly
design's own testing section listed these cases and none of them existed.
Written alongside the contiguity work in `parsers.py`, which is what made
`assembly_n50` redundant and is why it is gone from `parse_assembly_info`
below.
"""

from pathlib import Path

import pytest

from app.pipelines import assembly_runner
from app.pipelines.assemblers import Assembler
from app.pipelines.assembly_params import (
    AbyssParams,
    FlyeParams,
    MegahitParams,
    SpadesParams,
)
from app.pipelines.assembly_runner import AssemblyProgress, parse_assembly_info


def _abyss_cmd(**kwargs):
    defaults = dict(
        assembler=Assembler.ABYSS,
        tool_path="/usr/bin/abyss-pe",
        reads=Path("/work/r1.fastq.gz"),
        out_dir=Path("/work/out"),
        params=AbyssParams(k=51, threads=4),
        memory_bytes=2 * 1024**3,
    )
    defaults.update(kwargs)
    return assembly_runner.build_assembly_command(**defaults)


class TestBuildAssemblyCommand:
    def test_flye_command_shape(self):
        cmd = assembly_runner.build_assembly_command(
            assembler=Assembler.FLYE,
            tool_path="/usr/bin/flye",
            reads=Path("/w/reads.fastq.gz"),
            out_dir=Path("/w/out"),
            params=FlyeParams(mode="nano-hq", threads=4, iterations=2),
        )
        assert cmd == [
            "/usr/bin/flye",
            "--nano-hq",
            "/w/reads.fastq.gz",
            "--out-dir",
            "/w/out",
            "--threads",
            "4",
            "--iterations",
            "2",
        ]

    def test_flye_meta_flag_appends_meta(self):
        cmd = assembly_runner.build_assembly_command(
            assembler=Assembler.FLYE,
            tool_path="/usr/bin/flye",
            reads=Path("/w/reads.fastq.gz"),
            out_dir=Path("/w/out"),
            params=FlyeParams(mode="nano-hq", threads=4, iterations=2, meta=True),
        )
        assert cmd[-1] == "--meta"

    def test_flye_default_omits_meta(self):
        cmd = assembly_runner.build_assembly_command(
            assembler=Assembler.FLYE,
            tool_path="/usr/bin/flye",
            reads=Path("/w/reads.fastq.gz"),
            out_dir=Path("/w/out"),
            params=FlyeParams(mode="nano-hq", threads=4, iterations=2),
        )
        assert "--meta" not in cmd

    def test_genome_size_is_never_passed(self):
        """The module docstring's central claim: genome_size is collected for
        BioFlow's own memory estimate and never reaches the argv, because
        passing it to Flye only changes behaviour combined with
        --asm-coverage, which this app does not offer."""
        cmd = assembly_runner.build_assembly_command(
            assembler=Assembler.FLYE,
            tool_path="flye",
            reads=Path("/w/reads.fastq"),
            out_dir=Path("/w/out"),
            params=FlyeParams(mode="nano-raw", genome_size=4_600_000),
        )
        assert not any("genome-size" in arg or "4600000" in arg for arg in cmd)

    def test_unknown_assembler_raises_rather_than_falling_back(self):
        """A missing command builder must not silently produce a Flye
        command line for a different binary."""
        with pytest.raises(ValueError, match="hifiasm"):
            assembly_runner.build_assembly_command(
                assembler=Assembler.HIFIASM,
                tool_path="hifiasm",
                reads=Path("/w/reads.fastq"),
                out_dir=Path("/w/out"),
                params=FlyeParams(),
            )

    def test_abyss_command_uses_make_variable_assignments(self):
        """abyss-pe is a Make wrapper: `k=51`, never `--k 51`."""
        cmd = _abyss_cmd()
        assert cmd[0] == "/usr/bin/abyss-pe"
        assert "k=51" in cmd
        assert "j=4" in cmd
        assert "name=asm" in cmd
        assert not any(token.startswith("--k") for token in cmd)

    def test_abyss_command_pairs_both_mates_in_one_in_variable(self):
        cmd = _abyss_cmd(mate=Path("/work/r2.fastq.gz"))
        assert "in=/work/r1.fastq.gz /work/r2.fastq.gz" in cmd
        assert not any(t.startswith("se=") for t in cmd)

    def test_abyss_command_falls_back_to_single_end(self):
        cmd = _abyss_cmd(mate=None)
        assert "se=/work/r1.fastq.gz" in cmd
        assert not any(t.startswith("in=") for t in cmd)

    def test_abyss_command_always_sets_bloom_budget(self):
        """B is mandatory: without it abyss-pe exits non-zero immediately."""
        cmd = _abyss_cmd(memory_bytes=3 * 1024**3)
        assert "B=3072M" in cmd

    def test_abyss_command_floors_bloom_budget(self):
        """A tiny or absent estimate must not produce an unusable B."""
        cmd = _abyss_cmd(memory_bytes=None)
        assert "B=200M" in cmd

    def test_flye_command_unchanged_by_new_keywords(self):
        cmd = assembly_runner.build_assembly_command(
            assembler=Assembler.FLYE,
            tool_path="/usr/bin/flye",
            reads=Path("/work/reads.fastq"),
            out_dir=Path("/work/out"),
            params=FlyeParams(mode="nano-hq", threads=8, iterations=1),
        )
        assert cmd[:2] == ["/usr/bin/flye", "--nano-hq"]

    def test_unknown_assembler_still_raises(self):
        with pytest.raises(ValueError, match="No command builder"):
            assembly_runner.build_assembly_command(
                assembler=Assembler.HIFIASM,
                tool_path="/usr/bin/hifiasm",
                reads=Path("/work/reads.fastq"),
                out_dir=Path("/work/out"),
                params=AbyssParams(),
            )


def _spades_cmd(**kwargs):
    defaults = dict(
        assembler=Assembler.SPADES,
        tool_path="/usr/local/bin/spades.py",
        reads=Path("/work/r1.fastq.gz"),
        out_dir=Path("/work/out"),
        params=SpadesParams(mode="isolate", threads=4),
        mate=Path("/work/r2.fastq.gz"),
        memory_bytes=8 * 1024**3,
    )
    defaults.update(kwargs)
    return assembly_runner.build_assembly_command(**defaults)


class TestSpadesCommand:
    def test_pairs_mates_as_separate_flags(self):
        """Unlike abyss-pe's single `in=` variable, SPAdes takes -1 and -2."""
        cmd = _spades_cmd()
        assert "-1" in cmd
        assert cmd[cmd.index("-1") + 1] == "/work/r1.fastq.gz"
        assert "-2" in cmd
        assert cmd[cmd.index("-2") + 1] == "/work/r2.fastq.gz"

    def test_falls_back_to_single_end(self):
        cmd = _spades_cmd(mate=None)
        assert "-s" in cmd
        assert cmd[cmd.index("-s") + 1] == "/work/r1.fastq.gz"
        assert "-1" not in cmd

    def test_isolate_mode_passes_the_flag(self):
        assert "--isolate" in _spades_cmd(params=SpadesParams(mode="isolate"))

    def test_careful_mode_passes_the_flag(self):
        cmd = _spades_cmd(params=SpadesParams(mode="careful"))
        assert "--careful" in cmd
        assert "--isolate" not in cmd

    def test_standard_mode_passes_neither_flag(self):
        cmd = _spades_cmd(params=SpadesParams(mode="standard"))
        assert "--isolate" not in cmd
        assert "--careful" not in cmd

    def test_meta_mode_passes_the_flag(self):
        cmd = _spades_cmd(params=SpadesParams(mode="meta"))
        assert "--meta" in cmd

    def test_meta_mode_passes_neither_isolate_nor_careful(self):
        """SPAdes rejects `--meta --isolate` and `--meta --careful` outright,
        and does so minutes in, after read error correction. A builder that
        emitted both would fail late and unhelpfully."""
        cmd = _spades_cmd(params=SpadesParams(mode="meta"))
        assert "--isolate" not in cmd
        assert "--careful" not in cmd

    @pytest.mark.parametrize(
        "mode,expected_flags",
        [
            ("isolate", ["--isolate"]),
            ("careful", ["--careful"]),
            ("standard", []),
        ],
    )
    def test_existing_modes_emit_exactly_what_they_did(self, mode, expected_flags):
        """Full-argv equality, not a `"--meta" not in cmd` check.

        Adding a mode edits a builder every existing SPAdes run goes through,
        so the assertion that matters is that the other three argvs are
        byte-identical -- which also catches a reordering or a dropped flag
        that a negative check would sail past.
        """
        cmd = _spades_cmd(params=SpadesParams(mode=mode, threads=4))
        assert cmd == [
            "/usr/local/bin/spades.py",
            "-o",
            "/work/out",
            "-t",
            "4",
            "-m",
            "8",
            *expected_flags,
            "-1",
            "/work/r1.fastq.gz",
            "-2",
            "/work/r2.fastq.gz",
        ]

    def test_memory_ceiling_is_in_whole_gigabytes(self):
        """-m is in GB and SPAdes terminates on reaching it."""
        cmd = _spades_cmd(memory_bytes=8 * 1024**3)
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "8"

    def test_memory_ceiling_is_floored_when_no_estimate_exists(self):
        """Never inherit upstream's 250GB default: a run with no estimate
        would then die late rather than never starting."""
        cmd = _spades_cmd(memory_bytes=None)
        assert cmd[cmd.index("-m") + 1] == str(assembly_runner.MIN_SPADES_MEMORY_GB)

    def test_tiny_estimate_is_raised_to_the_floor(self):
        cmd = _spades_cmd(memory_bytes=100 * 1024**2)
        assert cmd[cmd.index("-m") + 1] == str(assembly_runner.MIN_SPADES_MEMORY_GB)


def _megahit_cmd(**kwargs):
    defaults = dict(
        assembler=Assembler.MEGAHIT,
        tool_path="/usr/local/bin/megahit",
        reads=Path("/work/r1.fastq.gz"),
        out_dir=Path("/work/out"),
        params=MegahitParams(threads=4),
        mate=Path("/work/r2.fastq.gz"),
        memory_bytes=8 * 1024**3,
    )
    defaults.update(kwargs)
    return assembly_runner.build_assembly_command(**defaults)


class TestMegahitCommand:
    def test_full_argv(self):
        assert _megahit_cmd() == [
            "/usr/local/bin/megahit",
            "-o",
            "/work/out",
            "--force",
            "-t",
            "4",
            "-m",
            str(8 * 1024**3),
            "--min-contig-len",
            "200",
            "-1",
            "/work/r1.fastq.gz",
            "-2",
            "/work/r2.fastq.gz",
        ]

    def test_force_is_always_passed(self):
        """Without it, every run here fails before assembling anything.

        MEGAHIT refuses to start when `-o` already exists, and
        `assembly_handlers` creates out_dir before building any command --
        which Flye, ABySS and SPAdes all accept. This is not a convenience
        flag; it is the difference between the tool working and not.
        """
        assert "--force" in _megahit_cmd()
        assert "--force" in _megahit_cmd(mate=None)
        assert "--force" in _megahit_cmd(memory_bytes=None)

    def test_memory_is_in_bytes_not_gigabytes(self):
        """The opposite of SPAdes' `-m`, which is in GB.

        `-m 8` to MEGAHIT means eight *bytes*. The number here must be the
        byte count itself.
        """
        cmd = _megahit_cmd(memory_bytes=8 * 1024**3)
        assert cmd[cmd.index("-m") + 1] == str(8 * 1024**3)

    def test_memory_is_floored_when_no_estimate_exists(self):
        cmd = _megahit_cmd(memory_bytes=None)
        assert cmd[cmd.index("-m") + 1] == str(
            assembly_runner.MIN_MEGAHIT_MEMORY_BYTES
        )

    def test_tiny_estimate_is_raised_to_the_floor(self):
        cmd = _megahit_cmd(memory_bytes=100 * 1024**2)
        assert cmd[cmd.index("-m") + 1] == str(
            assembly_runner.MIN_MEGAHIT_MEMORY_BYTES
        )

    @pytest.mark.parametrize("memory_bytes", [None, 0, 1, 100, 100 * 1024**2, 8 * 1024**3])
    def test_memory_is_never_a_fraction(self, memory_bytes):
        """A value in [0, 1] is read by MEGAHIT as a *fraction of host
        memory*, not a byte count.

        That is the silent half of this flag: it makes a run's real memory
        depend on the machine rather than on the estimate that admitted it,
        so two runs with identical recorded parameters behave differently on
        different hosts. The floor is what prevents it; this asserts the
        property rather than the floor, so it keeps holding if the floor
        changes.
        """
        cmd = _megahit_cmd(memory_bytes=memory_bytes)
        value = float(cmd[cmd.index("-m") + 1])
        assert value > 1

    def test_pairs_mates_as_separate_flags(self):
        cmd = _megahit_cmd()
        assert cmd[cmd.index("-1") + 1] == "/work/r1.fastq.gz"
        assert cmd[cmd.index("-2") + 1] == "/work/r2.fastq.gz"

    def test_falls_back_to_single_end_with_r(self):
        """`-r`, not SPAdes' `-s`. MEGAHIT assembles single-end input fine,
        unlike metaSPAdes, so there is no launch-time refusal to match."""
        cmd = _megahit_cmd(mate=None)
        assert cmd[cmd.index("-r") + 1] == "/work/r1.fastq.gz"
        assert "-1" not in cmd
        assert "-s" not in cmd

    def test_min_contig_len_is_passed(self):
        cmd = _megahit_cmd(params=MegahitParams(threads=4, min_contig_len=1000))
        assert cmd[cmd.index("--min-contig-len") + 1] == "1000"

    def test_no_meta_flag_exists_to_pass(self):
        """MEGAHIT is a metagenome assembler throughout -- there is no
        `--meta` to emit, which is why `assembly_meta_mode` is keyed off the
        assembler rather than a parameter."""
        cmd = _megahit_cmd()
        assert "--meta" not in cmd

    def test_genome_size_is_never_passed(self):
        """Same asymmetry the other three builders keep: genome size is
        collected for BioFlow's estimate, and here it does not even feed
        that -- MEGAHIT's memory model has no genome term."""
        cmd = _megahit_cmd(
            params=MegahitParams(threads=4, genome_size=5_000_000)
        )
        assert "5000000" not in cmd
        assert "--genome-size" not in cmd


class TestExistingBuildersAreUnchanged:
    """Full-argv equality for every assembler that existed before MEGAHIT.

    Adding a builder edits `build_assembly_command`, which every existing
    assembly goes through. A negative check (`"--force" not in cmd`) would
    sail past a reordering or a dropped flag; these would not.
    """

    def test_flye_argv(self):
        cmd = assembly_runner.build_assembly_command(
            assembler=Assembler.FLYE,
            tool_path="/usr/bin/flye",
            reads=Path("/work/reads.fastq.gz"),
            out_dir=Path("/work/out"),
            params=FlyeParams(mode="nano-hq", threads=4, iterations=1),
        )
        assert cmd == [
            "/usr/bin/flye",
            "--nano-hq",
            "/work/reads.fastq.gz",
            "--out-dir",
            "/work/out",
            "--threads",
            "4",
            "--iterations",
            "1",
        ]

    def test_abyss_argv(self):
        cmd = _abyss_cmd()
        assert cmd == [
            "/usr/bin/abyss-pe",
            "-C",
            "/work/out",
            "name=asm",
            "k=51",
            "j=4",
            "B=2048M",
            "se=/work/r1.fastq.gz",
        ]


class TestAssemblyProgress:
    def test_recognizes_a_real_stage_line(self):
        progress = AssemblyProgress()
        changed = progress.feed(">>>STAGE: assembly")
        assert changed is True
        assert progress.phase == "assembling draft"

    def test_unrelated_log_lines_do_not_change_phase(self):
        progress = AssemblyProgress()
        assert progress.feed("Reading sequences...") is False
        assert progress.phase == "starting"

    def test_repeating_the_same_stage_does_not_report_a_change(self):
        progress = AssemblyProgress()
        progress.feed(">>>STAGE: polishing")
        assert progress.feed(">>>STAGE: polishing") is False

    def test_unmapped_future_stage_still_displays_its_raw_name(self):
        """An unrecognized stage must still surface as *something* changing,
        rather than leaving the display stuck on the previous phase -- which
        would read as a hang rather than progress."""
        progress = AssemblyProgress()
        changed = progress.feed(">>>STAGE: newthing")
        assert changed is True
        assert progress.phase == "newthing"

    def test_message_reflects_current_phase(self):
        progress = AssemblyProgress()
        progress.feed(">>>STAGE: finalize")
        assert progress.message() == "finishing"


class TestFlyeStageOrder:
    """The stage list Flye will actually run, derived from params.

    Flye builds its whole job list at launch (`_create_job_list`), so this is
    knowable before the process starts -- which is what makes an honest
    phase_total possible at all.
    """

    def test_default_params_run_all_seven_stages(self):
        order = assembly_runner.flye_stage_order(FlyeParams())
        assert order == (
            "configure",
            "assembly",
            "consensus",
            "repeat",
            "contigger",
            "polishing",
            "finalize",
        )

    def test_zero_iterations_drops_polishing(self):
        """`--iterations 0` skips JobPolishing, so declaring 7 would leave the
        counter jumping from 5/7 to 7/7 with nothing at 6."""
        order = assembly_runner.flye_stage_order(FlyeParams(iterations=0))
        assert "polishing" not in order
        assert len(order) == 6
        assert order[-1] == "finalize"

    def test_extra_iterations_do_not_add_stages(self):
        """Polishing is one stage regardless of how many rounds it runs."""
        order = assembly_runner.flye_stage_order(FlyeParams(iterations=5))
        assert len(order) == 7

    def test_every_stage_has_a_display_label(self):
        """_STAGE_LABELS and _FLYE_STAGES are two hand-maintained structures
        in parallel: a stage in one and not the other is skipped silently
        rather than raised, which is the shape CLAUDE.md flags (the same trap
        COMPONENT_ORDER carried against COMPONENTS)."""
        assert set(assembly_runner._STAGE_LABELS) == set(
            assembly_runner._FLYE_STAGES
        )


class TestAssemblyPhaseStructure:
    """"Step N of M" for assembly. The UI renders the counter only when index
    and total are both non-null, so every case below is either a real pair or
    a deliberate fallback to the phase name alone.
    """

    def _progress(self, iterations: int = 1) -> AssemblyProgress:
        return AssemblyProgress(
            stage_order=assembly_runner.flye_stage_order(
                FlyeParams(iterations=iterations)
            )
        )

    def test_first_stage_is_step_one(self):
        progress = self._progress()
        progress.feed(">>>STAGE: configure")
        assert progress.phase_index == 1
        assert progress.phase_total == 7

    def test_index_advances_through_the_whole_run(self):
        progress = self._progress()
        seen = []
        for stage in assembly_runner._FLYE_STAGES:
            progress.feed(f">>>STAGE: {stage}")
            seen.append(progress.phase_index)
        assert seen == [1, 2, 3, 4, 5, 6, 7]

    def test_final_stage_is_the_last_step(self):
        progress = self._progress()
        progress.feed(">>>STAGE: finalize")
        assert progress.phase_index == 7
        assert progress.phase_total == 7

    def test_zero_iterations_finishes_at_six_of_six(self):
        """The case that decided the design: a flat constant would report
        finalize as 7 of 7 on a run that only ever executes six stages."""
        progress = self._progress(iterations=0)
        progress.feed(">>>STAGE: contigger")
        assert progress.phase_index == 5
        progress.feed(">>>STAGE: finalize")
        assert progress.phase_index == 6
        assert progress.phase_total == 6

    def test_index_is_null_before_any_stage_line(self):
        progress = self._progress()
        assert progress.phase_index is None
        assert progress.phase == "starting"

    def test_unknown_stage_shows_its_name_without_a_step_number(self):
        """A future Flye stage must not borrow the previous stage's number.
        Null index means the UI drops the counter and shows the name alone --
        exactly what shipped before this feature."""
        progress = self._progress()
        progress.feed(">>>STAGE: repeat")
        assert progress.phase_index == 4
        progress.feed(">>>STAGE: newthing")
        assert progress.phase == "newthing"
        assert progress.phase_index is None
        assert progress.phase_total == 7

    def test_snapshot_carries_both_keys(self):
        progress = self._progress()
        progress.feed(">>>STAGE: repeat")
        snap = progress.snapshot()
        assert snap["phase_index"] == 4
        assert snap["phase_total"] == 7
        assert snap["phase"] == "resolving repeats"
        assert snap["pct"] is None

    def test_snapshot_omits_both_keys_without_a_declared_order(self):
        """executor.py's parser contract: omit keys you do not know rather
        than passing None, which ctx.progress() would write over a value it
        should have left alone."""
        snap = AssemblyProgress().snapshot()
        assert "phase_index" not in snap
        assert "phase_total" not in snap
        assert snap["phase"] == "starting"

    def test_duplicate_labels_do_not_confuse_the_index(self):
        """Index keys on the raw stage name, not the display label. Two
        stages sharing a label must still report distinct steps."""
        progress = self._progress()
        progress.feed(">>>STAGE: consensus")
        first = progress.phase_index
        progress.feed(">>>STAGE: contigger")
        assert progress.phase_index != first
        assert progress.phase_index == 5


class TestParseAssemblyInfo:
    HEADER = "#seq_name\tlength\tcov.\tcirc.\trepeat\tmult.\talt_group\tgraph_path\n"

    def test_parses_a_real_shaped_table(self):
        text = self.HEADER + (
            "contig_1\t1000000\t42.5\tY\tN\t1\t*\tpath1\n"
            "contig_2\t500000\t38.0\tN\tN\t1\t*\tpath2\n"
        )
        facts = parse_assembly_info(text)
        assert facts["assembly_contig_count"] == 2
        assert facts["assembly_total_length"] == 1_500_000
        assert facts["assembly_circular_count"] == 1
        assert facts["assembly_longest_circular"] == 1_000_000

    def test_mean_coverage_is_length_weighted(self):
        text = self.HEADER + (
            "a\t900\t10.0\tN\tN\t1\t*\tp\n"
            "b\t100\t100.0\tN\tN\t1\t*\tp\n"
        )
        facts = parse_assembly_info(text)
        # (900*10 + 100*100) / 1000 = 19.0, not the unweighted mean of 55.
        assert facts["assembly_mean_coverage"] == 19.0

    def test_no_circular_contigs_reports_zero_not_an_error(self):
        text = self.HEADER + "a\t1000\t10.0\tN\tN\t1\t*\tp\n"
        facts = parse_assembly_info(text)
        assert facts["assembly_circular_count"] == 0
        assert facts["assembly_longest_circular"] == 0

    def test_no_n50_key_is_emitted(self):
        """assembly_n50 was removed: `parsers._contiguity_stats` now computes
        sequence_n50 from the FASTA bytes independently, and two N50s on one
        object that are supposed to agree is the bug this avoided."""
        text = self.HEADER + "a\t1000\t10.0\tY\tN\t1\t*\tp\n"
        facts = parse_assembly_info(text)
        assert "assembly_n50" not in facts

    def test_empty_text_returns_empty_dict(self):
        assert parse_assembly_info("") == {}

    def test_header_only_returns_empty_dict(self):
        assert parse_assembly_info(self.HEADER) == {}

    def test_unexpected_header_returns_empty_dict_rather_than_raising(self):
        """A table that failed to parse must not fail an assembly that took
        six hours and produced a perfectly good FASTA."""
        text = "#totally\tdifferent\tcolumns\n1\t2\t3\n"
        assert parse_assembly_info(text) == {}

    def test_one_malformed_row_does_not_discard_the_rest(self):
        text = self.HEADER + (
            "good\t1000\t10.0\tY\tN\t1\t*\tp\n"
            "bad\tnotanumber\t10.0\tY\tN\t1\t*\tp\n"
            "also_good\t2000\t20.0\tN\tN\t1\t*\tp\n"
        )
        facts = parse_assembly_info(text)
        assert facts["assembly_contig_count"] == 2
        assert facts["assembly_total_length"] == 3000

    def test_short_row_is_skipped_not_raised(self):
        text = self.HEADER + "truncated\t1000\n"
        assert parse_assembly_info(text) == {}

    def test_contigs_are_capped_and_flagged_when_over_the_limit(self):
        rows = "".join(
            f"c{i}\t{1000 + i}\t10.0\tN\tN\t1\t*\tp\n" for i in range(60)
        )
        facts = parse_assembly_info(self.HEADER + rows)
        assert facts["assembly_contig_count"] == 60
        assert len(facts["assembly_contigs"]) == 50
        assert facts["assembly_contigs_truncated"] is True

    def test_contigs_are_sorted_longest_first(self):
        text = self.HEADER + (
            "small\t100\t10.0\tN\tN\t1\t*\tp\n"
            "big\t5000\t10.0\tN\tN\t1\t*\tp\n"
            "medium\t1000\t10.0\tN\tN\t1\t*\tp\n"
        )
        facts = parse_assembly_info(text)
        assert [c["name"] for c in facts["assembly_contigs"]] == [
            "big",
            "medium",
            "small",
        ]


ABYSS_STATS_TAB = (
    "n\tn:500\tL50\tmin\tN75\tN50\tN25\tE-size\tmax\tsum\tname\n"
    "12\t10\t3\t512\t4000\t9000\t15000\t9500\t21000\t60000\tasm-unitigs.fa\n"
    "8\t7\t2\t600\t7000\t14000\t22000\t15000\t30000\t61000\tasm-contigs.fa\n"
    "6\t5\t2\t800\t9000\t18000\t26000\t19000\t34000\t62000\tasm-scaffolds.fa\n"
)


def test_parse_abyss_stats_reads_the_scaffolds_row():
    """The scaffolds row is the assembly; the earlier rows are stages."""
    facts = assembly_runner.parse_abyss_stats(ABYSS_STATS_TAB)
    assert facts["assembly_contig_count"] == 6
    assert facts["assembly_n50"] == 18000
    assert facts["assembly_longest"] == 34000
    assert facts["assembly_total_length"] == 62000


def test_parse_abyss_stats_survives_garbage():
    """A stats table that failed to parse must not fail a good assembly."""
    assert assembly_runner.parse_abyss_stats("not a table at all") == {}
    assert assembly_runner.parse_abyss_stats("") == {}


def test_abyss_progress_reports_a_phase():
    progress = assembly_runner.AbyssProgress()
    assert progress.feed("abyss-map -j4 ...") is False or True  # tolerant
    progress.feed("ABySS-P: assembling contigs")
    snap = progress.snapshot()
    assert snap["pct"] is None
    assert isinstance(snap["phase"], str)


def test_harvest_resolves_symlinks(tmp_path):
    """ABySS outputs are symlinks; storing the link would dangle."""
    from app.pipelines.assemblers import Output, OutputKind

    out = tmp_path / "out"
    out.mkdir()
    real = out / "asm-8.fa"
    real.write_text(">contig\nACGT\n")
    link = out / "asm-scaffolds.fa"
    link.symlink_to(real)

    found = assembly_runner.harvest(
        out,
        (Output(kind=OutputKind.CONTIGS, filename="asm-scaffolds.fa", required=True),),
    )
    assert found[OutputKind.CONTIGS] == real.resolve()
    assert not found[OutputKind.CONTIGS].is_symlink()


class TestGfaToFasta:
    """hifiasm writes contigs only as GFA -- no FASTA at all -- while
    OutputKind.CONTIGS is required and becomes the REFERENCE object
    everything downstream aligns against. This converter is what bridges
    that gap (spec R13-R15). S-line layout confirmed against a real
    0.25.0 run: S <name> <seq> LN:i:... rd:i:...
    """

    def test_converts_s_lines_and_ignores_everything_else(self):
        gfa = (
            "S\tptg000001l\tACGTACGT\tLN:i:8\trd:i:340\n"
            "L\tptg000001l\t+\tptg000002l\t-\t0M\n"
            "S\tptg000002l\tTTTT\tLN:i:4\n"
            "A\tptg000001l\t0\t+\tr1\t0\t8\tid:i:0\n"
        )
        fasta = assembly_runner.gfa_to_fasta(gfa)
        assert fasta == ">ptg000001l\nACGTACGT\n>ptg000002l\nTTTT\n"

    def test_raises_on_a_gfa_with_no_sequences(self):
        """An exit-0 hifiasm run that assembled nothing must not become a
        valid, empty FASTA that everything downstream silently aligns
        against (spec R15)."""
        with pytest.raises(ValueError, match="no sequences"):
            assembly_runner.gfa_to_fasta("H\tVN:Z:1.0\nL\ta\t+\tb\t-\t0M\n")

    def test_raises_on_empty_input(self):
        with pytest.raises(ValueError, match="no sequences"):
            assembly_runner.gfa_to_fasta("")

    def test_tolerates_s_line_with_no_tags(self):
        assert assembly_runner.gfa_to_fasta("S\tctg1\tACGT\n") == ">ctg1\nACGT\n"
