"""Medaka command construction and output parsing.

Medaka differs from Polypolish in three ways that this file pins down,
because each is a plausible "fix" that breaks the tool silently:

- It writes an output *directory*, not stdout, so there is no redirect
  wrapper here the way `polypolish_runner.redirect_stdout` exists there.
- It builds its own minimap2 call from model-dependent parameters
  (`medaka tools get_alignment_params`), so this module must never
  construct alignment arguments.
- Without `-f` it reuses whatever consensus is already in the output
  directory and exits zero, returning a previous run's assembly.
"""

from pathlib import Path

from app.pipelines import medaka_runner as runner


class TestConsensusCommand:
    def test_force_flag_is_always_present(self):
        """Without -f, medaka reuses stale outputs and exits zero.

        That returns a *previous* run's assembly while reporting success,
        which is why this is asserted on the argv rather than trusted to
        survive a future tidy-up.
        """
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("draft.fasta"),
            reads=Path("reads.fastq"),
            outdir=Path("/work/out"),
        )
        assert "-f" in argv

    def test_draft_reads_and_outdir_are_passed(self):
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("draft.fasta"),
            reads=Path("reads.fastq"),
            outdir=Path("/work/out"),
        )
        assert argv[0] == "medaka_consensus"
        assert argv[argv.index("-d") + 1] == "draft.fasta"
        assert argv[argv.index("-i") + 1] == "reads.fastq"
        assert argv[argv.index("-o") + 1] == "/work/out"

    def test_threads_are_passed(self):
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("d.fasta"),
            reads=Path("r.fastq"),
            outdir=Path("/out"),
            threads=8,
        )
        assert argv[argv.index("-t") + 1] == "8"

    def test_bacteria_absent_by_default(self):
        """--bacteria is a dialog opt-in, never a default.

        ONT labels the bacterial model a research release; defaulting it on
        would silently apply it to eukaryotic drafts.
        """
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("d.fasta"),
            reads=Path("r.fastq"),
            outdir=Path("/out"),
        )
        assert "--bacteria" not in argv

    def test_bacteria_present_when_requested(self):
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("d.fasta"),
            reads=Path("r.fastq"),
            outdir=Path("/out"),
            bacteria=True,
        )
        assert "--bacteria" in argv

    def test_no_alignment_arguments_are_constructed(self):
        """Medaka resolves its own minimap2 preset from the model.

        Constructing one here would override a model-dependent choice with
        a fixed guess -- the inverse of Polypolish's mandatory `-a`.
        """
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("d.fasta"),
            reads=Path("r.fastq"),
            outdir=Path("/out"),
        )
        joined = " ".join(argv)
        assert "-x" not in argv
        assert "map-ont" not in joined
        assert "minimap2" not in joined
