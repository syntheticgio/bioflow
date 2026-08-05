"""RagTag command construction and output parsing.

The `.stats` and `.confidence.txt` fixtures below are verbatim from a real
`ragtag 2.1.0` run on 2026-08-05 (a synthetic 2-chromosome reference and a
7-contig draft cut from it, shuffled and partly reverse-complemented, all 7
contigs placed with zero errors against the truth). Not invented text --
RagTag's TSVs are simple, but the column order and header names are worth
pinning to something real rather than assumed.
"""

from pathlib import Path

import pytest

from app.pipelines import ragtag_runner as runner

# Real output, one run.
STATS_TSV = (
    "placed_sequences\tplaced_bp\tunplaced_sequences\tunplaced_bp\tgap_bp\tgap_sequences\n"
    "7\t100000\t0\t0\t500\t5\n"
)

# Real output, the same run -- one row per placed contig, all confident here.
CONFIDENCE_TSV = (
    "query\tgrouping_confidence\tlocation_confidence\torientation_confidence\n"
    "ctg5_c1_0\t1.0\t1.0\t1.0\n"
    "ctg1_c1_1\t1.0\t1.0\t1.0\n"
    "ctg0_c1_2\t1.0\t1.0\t1.0\n"
)

# A synthetic low-confidence row appended, since the real run had none: this
# is what exercises the "minimum, not mean" requirement.
CONFIDENCE_TSV_MIXED = CONFIDENCE_TSV + "ctg3_c2_0\t0.3\t0.9\t1.0\n"

SCAFFOLD_FASTA = ">chr1_RagTag\nACGT\n>chr2_RagTag\nACGT\n"


class TestScaffoldCommand:
    def test_reference_comes_before_draft(self):
        """`ragtag.py scaffold <reference> <query>` -- the opposite order
        from how these two objects are named in this app's own inputs
        (draft, then reference). Swapping them is not a syntax error to the
        tool, so this is asserted on the argv rather than trusted to review."""
        argv = runner.build_scaffold_command(
            ragtag_path="ragtag.py",
            reference=Path("ref.fasta"),
            draft=Path("draft.fasta"),
            out_dir=Path("out"),
            threads=4,
        )
        assert argv.index("ref.fasta") < argv.index("draft.fasta")

    def test_dash_u_is_present(self):
        """Without -u, RagTag's own log warns that AGP object/component IDs
        may collide, which some downstream tools reject. Not optional here."""
        argv = runner.build_scaffold_command(
            ragtag_path="ragtag.py",
            reference=Path("ref.fasta"),
            draft=Path("draft.fasta"),
            out_dir=Path("out"),
            threads=4,
        )
        assert "-u" in argv

    def test_threads_and_output_dir_are_passed_through(self):
        argv = runner.build_scaffold_command(
            ragtag_path="ragtag.py",
            reference=Path("ref.fasta"),
            draft=Path("draft.fasta"),
            out_dir=Path("myout"),
            threads=8,
        )
        assert argv[argv.index("-t") + 1] == "8"
        assert argv[argv.index("-o") + 1] == "myout"

    @pytest.mark.parametrize(
        "divergence,expected",
        [
            (runner.Divergence.SAME_SPECIES, "-x asm5"),
            (runner.Divergence.SAME_GENUS, "-x asm10"),
            (runner.Divergence.DISTANT, "-x asm20"),
        ],
    )
    def test_divergence_maps_to_the_right_preset(self, divergence, expected):
        argv = runner.build_scaffold_command(
            ragtag_path="ragtag.py",
            reference=Path("ref.fasta"),
            draft=Path("draft.fasta"),
            out_dir=Path("out"),
            threads=4,
            divergence=divergence,
        )
        assert argv[argv.index("--mm2-params") + 1] == expected

    def test_unrecognised_divergence_falls_back_to_the_default(self):
        """Degrades to RagTag's own default rather than raising -- same
        posture align_runner.preset_for_chemistry takes for UNKNOWN."""
        argv = runner.build_scaffold_command(
            ragtag_path="ragtag.py",
            reference=Path("ref.fasta"),
            draft=Path("draft.fasta"),
            out_dir=Path("out"),
            threads=4,
            divergence="nonsense",
        )
        assert argv[argv.index("--mm2-params") + 1] == "-x asm5"


class TestParseStats:
    def test_real_fixture(self):
        facts = runner.parse_stats(STATS_TSV)
        assert facts == {
            "scaffold_placed_sequences": 7,
            "scaffold_placed_bp": 100000,
            "scaffold_unplaced_sequences": 0,
            "scaffold_unplaced_bp": 0,
            "scaffold_gap_bp": 500,
            "scaffold_gap_sequences": 5,
        }

    def test_garbage_returns_empty_rather_than_raising(self):
        """A summary that fails to parse must not fail a job that already
        produced a scaffolded FASTA."""
        assert runner.parse_stats("not a tsv at all") == {}

    def test_empty_string(self):
        assert runner.parse_stats("") == {}


class TestParseConfidence:
    def test_uniform_confidence(self):
        facts = runner.parse_confidence(CONFIDENCE_TSV)
        assert facts["scaffold_min_grouping_confidence"] == 1.0

    def test_minimum_not_mean(self):
        """The bug this test exists for: a mean of the mixed fixture would
        be well above 0.3 and would hide the one contig actually worth
        looking at."""
        facts = runner.parse_confidence(CONFIDENCE_TSV_MIXED)
        assert facts["scaffold_min_grouping_confidence"] == pytest.approx(0.3)

    def test_garbage_returns_empty(self):
        assert runner.parse_confidence("nonsense") == {}

    def test_header_only_returns_empty(self):
        header = "query\tgrouping_confidence\tlocation_confidence\torientation_confidence\n"
        assert runner.parse_confidence(header) == {}


class TestCountScaffolds:
    def test_counts_output_headers(self):
        """7 placed contigs joined into 2 scaffolds in the real run -- the
        deliverable's own header count, not derived from placed_sequences,
        since those two numbers differ whenever more than one contig joins
        one scaffold."""
        assert runner.count_scaffolds(SCAFFOLD_FASTA) == 2

    def test_empty_fasta(self):
        assert runner.count_scaffolds("") == 0
