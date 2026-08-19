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


# Medaka announces its model choice on stderr before inference. The two
# shapes below are what distinguish a run that read basecaller metadata
# from one that fell back -- a distinction invisible in the consensus.
AUTO_RESOLVED_STDERR = """
[13:22:04 - MdlStore] Model r1041_e82_400bps_sup_v5.0.0 resolved from input file.
[13:22:04 - Predict] Setting tensorflow threads to 8.
"""

FALLBACK_STDERR = """
[13:22:04 - MdlStore] Could not resolve model from input data.
[13:22:04 - MdlStore] Using default consensus model r1041_e82_400bps_sup_v4.2.0.
[13:22:04 - Predict] Setting tensorflow threads to 8.
"""


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


class TestModelLine:
    def test_auto_resolved_model_is_named(self):
        facts = runner.parse_model_line(AUTO_RESOLVED_STDERR)
        assert facts["polish_model"] == "r1041_e82_400bps_sup_v5.0.0"
        assert facts["polish_model_auto_resolved"] is True

    def test_fallback_model_is_flagged(self):
        """A fallback succeeds with worse output and no error.

        The consensus alone cannot show it happened, so if this flag is
        wrong the run is undiagnosable after the fact.
        """
        facts = runner.parse_model_line(FALLBACK_STDERR)
        assert facts["polish_model"] == "r1041_e82_400bps_sup_v4.2.0"
        assert facts["polish_model_auto_resolved"] is False

    def test_unparseable_returns_empty(self):
        """A missed fact is a blank field; raising would discard a
        consensus that already exists on disk."""
        assert runner.parse_model_line("no model information here") == {}
        assert runner.parse_model_line("") == {}


def _write_fasta(path: Path, records: list[tuple[str, str]]) -> Path:
    """Write records as FASTA, wrapping at 60 columns.

    Line wrapping is deliberate: a comparison implemented per-line rather
    than per-sequence passes on single-line fixtures and fails on real
    tool output, which always wraps.
    """
    with open(path, "w") as fh:
        for name, seq in records:
            fh.write(f">{name}\n")
            for i in range(0, len(seq), 60):
                fh.write(seq[i : i + 60] + "\n")
    return path


class TestChangedPositions:
    def test_identical_sequences_report_zero(self, tmp_path):
        seq = "ACGT" * 50
        draft = _write_fasta(tmp_path / "d.fasta", [("ctg1", seq)])
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1", seq)])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_changed_positions"] == 0
        assert facts["polish_contigs_compared"] == 1

    def test_known_substitutions_are_recovered_exactly(self, tmp_path):
        """The count is the evidence that polishing did anything.

        Medaka prints no tally of its own, so if this number is wrong there
        is nothing else on the object to contradict it.
        """
        seq = list("ACGT" * 50)
        draft = _write_fasta(tmp_path / "d.fasta", [("ctg1", "".join(seq))])
        for pos in (10, 42, 99):
            seq[pos] = "A" if seq[pos] != "A" else "C"
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1", "".join(seq))])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_changed_positions"] == 3

    def test_length_change_is_reported_separately(self, tmp_path):
        """An indel is not a substitution count.

        Folding a length change into `polish_changed_positions` would make
        a one-base insertion look like every downstream base changed.
        """
        draft = _write_fasta(tmp_path / "d.fasta", [("ctg1", "ACGT" * 50)])
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1", "ACGT" * 50 + "AAA")])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_length_delta"] == 3

    def test_contig_missing_from_consensus_does_not_raise(self, tmp_path):
        """Degrade to a visible count, never to an exception.

        The facts exist to make failures visible; a parser that raises
        would discard a consensus that is already on disk.
        """
        draft = _write_fasta(
            tmp_path / "d.fasta", [("ctg1", "ACGT" * 20), ("ctg2", "TTTT" * 20)]
        )
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1", "ACGT" * 20)])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_contigs_unmatched"] == 1
        assert facts["polish_contigs_compared"] == 1

    def test_multiline_wrapping_does_not_affect_the_count(self, tmp_path):
        seq = "ACGTACGTGG" * 30
        draft = _write_fasta(tmp_path / "d.fasta", [("ctg1", seq)])
        changed = seq[:5] + ("A" if seq[5] != "A" else "C") + seq[6:]
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1", changed)])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_changed_positions"] == 1

    def test_header_description_is_ignored_when_matching(self, tmp_path):
        """Medaka appends its own description to contig headers.

        Matching on the full header line would find zero shared contigs and
        silently report a polish that changed nothing.
        """
        seq = "ACGT" * 40
        draft = _write_fasta(tmp_path / "d.fasta", [("ctg1", seq)])
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1 medaka consensus", seq)])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_contigs_compared"] == 1
        assert facts["polish_contigs_unmatched"] == 0
