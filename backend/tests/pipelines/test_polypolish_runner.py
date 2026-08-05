"""Polypolish command construction and stderr parsing.

The stderr fixtures here are verbatim excerpts from a real Polypolish 0.7.1
run on 2026-08-05 (synthetic 20kb draft, 20 planted single-base errors, 60x
synthetic Illumina pairs), not invented text. That matters for the
multi-contig case in particular: the format repeats per contig, and a parser
written against a plausible-looking single-block fixture reports the first
contig's tally as the whole run's.
"""

from pathlib import Path

import pytest

from app.pipelines import polypolish_runner as runner

# Real output, one contig.
SINGLE_CONTIG_STDERR = """
Polishing ctg1 (20,000 bp):
  mean read depth: 59.1x
  27 bp have a depth of zero (99.8650% coverage)
  20 positions changed (0.1000% of total positions)
  estimated pre-polishing sequence accuracy: 99.9000% (Q30.00)


Finished! (2026-08-05 18:45:51)
Polished sequence (to stdout):
  ctg1 (20,000 bp)
"""

# Real output, the same draft split into two contigs: 11 + 9 = 20.
TWO_CONTIG_STDERR = """
Polishing ctgA (10,000 bp):
  mean read depth: 57.5x
  17 bp have a depth of zero (99.8300% coverage)
  11 positions changed (0.1100% of total positions)
  estimated pre-polishing sequence accuracy: 99.8900% (Q29.59)
Polishing ctgB (10,000 bp):
  mean read depth: 59.9x
  14 bp have a depth of zero (99.8600% coverage)
  9 positions changed (0.0900% of total positions)
  estimated pre-polishing sequence accuracy: 99.9100% (Q30.46)
"""


class TestAlignCommand:
    def test_all_alignments_flag_is_present(self):
        """`-a` is the whole reason Polypolish beats a best-alignment polisher.

        Without it the aligner reports best alignments only, Polypolish
        silently degrades into the tool it replaced, and nothing errors --
        so this is asserted on the argv rather than trusted to survive a
        future tidy-up of the runner.
        """
        argv = runner.build_align_command(
            aligner_path="bwa-mem2", draft=Path("d.fasta"), reads=Path("r1.fastq")
        )
        assert "-a" in argv

    def test_one_invocation_per_read_file(self):
        """R1 and R2 are aligned separately, not as a pair.

        There is deliberately no paired form of this builder: pairing them
        would defeat `-a`, and `polypolish filter` is what reunites them.
        """
        argv = runner.build_align_command(
            aligner_path="bwa-mem2", draft=Path("d.fasta"), reads=Path("r1.fastq")
        )
        assert argv.count("r1.fastq") == 1
        assert "r2.fastq" not in argv

    def test_threads_are_passed_through(self):
        argv = runner.build_align_command(
            aligner_path="bwa-mem2",
            draft=Path("d.fasta"),
            reads=Path("r1.fastq"),
            threads=8,
        )
        assert argv[argv.index("-t") + 1] == "8"


class TestFilterCommand:
    def test_builds_paired_filter(self):
        argv = runner.build_filter_command(
            polypolish_path="polypolish",
            sam_in=[Path("a1.sam"), Path("a2.sam")],
            sam_out=[Path("f1.sam"), Path("f2.sam")],
        )
        assert argv[:2] == ["polypolish", "filter"]
        assert argv[argv.index("--in1") + 1] == "a1.sam"
        assert argv[argv.index("--out2") + 1] == "f2.sam"

    def test_refuses_a_single_file(self):
        """Single-end input has no inserts to filter on. The handler skips
        the step; this builder must not quietly invent a one-file form."""
        with pytest.raises(ValueError):
            runner.build_filter_command(
                polypolish_path="polypolish",
                sam_in=[Path("a1.sam")],
                sam_out=[Path("f1.sam")],
            )


class TestPolishCommand:
    def test_careful_flag_only_when_asked(self):
        plain = runner.build_polish_command(
            polypolish_path="polypolish",
            draft=Path("d.fasta"),
            sams=[Path("f1.sam"), Path("f2.sam")],
            params=runner.PolishParams(depth=60.0, careful=False),
        )
        careful = runner.build_polish_command(
            polypolish_path="polypolish",
            draft=Path("d.fasta"),
            sams=[Path("f1.sam"), Path("f2.sam")],
            params=runner.PolishParams(depth=10.0, careful=True),
        )
        assert "--careful" not in plain
        assert "--careful" in careful

    def test_draft_precedes_the_sams(self):
        """`polypolish polish <ASSEMBLY> <SAM>...` -- order is positional,
        and swapping them is not a syntax error to the tool."""
        argv = runner.build_polish_command(
            polypolish_path="polypolish",
            draft=Path("d.fasta"),
            sams=[Path("f1.sam"), Path("f2.sam")],
            params=runner.PolishParams(),
        )
        assert argv.index("d.fasta") < argv.index("f1.sam")


class TestCarefulThreshold:
    @pytest.mark.parametrize("depth,expected", [(10.0, True), (25.0, True), (26.0, False), (60.0, False)])
    def test_threshold_in_both_directions(self, depth, expected):
        assert runner.params_for_depth(depth).careful is expected

    def test_unknown_depth_does_not_engage_careful(self):
        """Unknown depth takes the *normal* path deliberately.

        `--careful` reads as the safe default but it silently stops
        correcting repeats -- the capability the tool was chosen for. An
        unmeasurable depth should not quietly change what the tool does.
        """
        assert runner.params_for_depth(None).careful is False

    def test_depth_estimate_needs_both_inputs(self):
        assert runner.estimate_depth(read_bases=None, assembly_length=5_000_000) is None
        assert runner.estimate_depth(read_bases=300_000_000, assembly_length=None) is None
        assert runner.estimate_depth(read_bases=300_000_000, assembly_length=5_000_000) == 60.0


class TestParseStderr:
    def test_single_contig(self):
        facts = runner.parse_polish_stderr(SINGLE_CONTIG_STDERR)
        assert facts["polish_changed_positions"] == 20
        assert facts["polish_contigs"] == 1
        assert facts["polish_assembly_length"] == 20000
        assert facts["polish_measured_depth"] == 59.1
        assert facts["polish_zero_depth_bp"] == 27

    def test_multi_contig_sums_rather_than_taking_the_first(self):
        """The bug this test exists for: 11 would look entirely plausible."""
        facts = runner.parse_polish_stderr(TWO_CONTIG_STDERR)
        assert facts["polish_changed_positions"] == 20
        assert facts["polish_contigs"] == 2
        assert facts["polish_zero_depth_bp"] == 31

    def test_depth_is_length_weighted(self):
        """Two equal-length contigs, so the weighted mean is the plain one --
        but the code path exercised is the weighted one."""
        facts = runner.parse_polish_stderr(TWO_CONTIG_STDERR)
        assert facts["polish_measured_depth"] == pytest.approx(58.7, abs=0.05)

    def test_thousands_separators_are_handled(self):
        facts = runner.parse_polish_stderr(
            "Polishing chr1 (5,234,567 bp):\n"
            "  mean read depth: 41.2x\n"
            "  1,234 positions changed (0.0236% of total positions)\n"
        )
        assert facts["polish_changed_positions"] == 1234
        assert facts["polish_assembly_length"] == 5234567

    def test_ansi_colour_codes_do_not_break_parsing(self):
        facts = runner.parse_polish_stderr(
            "\x1b[1;4;93mPolishing assembly sequences\x1b[0m\n" + SINGLE_CONTIG_STDERR
        )
        assert facts["polish_changed_positions"] == 20

    def test_unparseable_output_returns_empty_rather_than_raising(self):
        """A summary that fails to parse must not fail a job that already
        produced a polished FASTA."""
        assert runner.parse_polish_stderr("something else entirely") == {}


class TestRedirect:
    def test_wraps_argv_with_a_stdout_redirect(self):
        cmd = runner.redirect_stdout(["polypolish", "polish", "d.fasta"], Path("out.fa"))
        assert cmd[0] == "/bin/sh"
        assert cmd[1] == "-c"
        assert cmd[2].endswith("> out.fa")

    def test_quotes_names_with_spaces(self):
        cmd = runner.redirect_stdout(["tool", "my draft.fasta"], Path("out file.fa"))
        assert "'my draft.fasta'" in cmd[2]
        assert "'out file.fa'" in cmd[2]
