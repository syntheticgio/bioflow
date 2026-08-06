"""Progress parsers replayed against real captured tool output.

The hand-built-line tests in each tool's own test module (test_fastp_runner,
test_assembly_runner, test_variant_runner) cover individual regex behaviour
in isolation. These tests are different on purpose: they replay a real
stderr/stdout log captured from an actual run, because a parser that only
ever sees lines the author already expected cannot catch a regex that quietly
stopped matching. AssemblyProgress's original stage table -- built from prose
banners nobody had actually run -- is why: four of five patterns never fired,
and the tests were green the whole time because they fed it hand-built lines
that matched by construction.

Fixtures live in tests/fixtures/tool_logs/{tool}-{version}.log. Most are
captured from real jobs on this stack; minimap2-2.27.log is one exception --
captured by running the real minimap2/samtools binaries this image ships
against generated (not project) FASTA/FASTQ input, because no minimap2
alignment existed in this stack's job history to pull from. The binary and
its output are real; only the input sequences are synthetic. See
docs/superpowers/specs/2026-08-05-tool-progress-instrumentation-design.md,
"Golden log fixtures".
"""

from pathlib import Path

from app.pipelines.align_runner import AlignProgress
from app.pipelines.assembly_runner import AssemblyProgress
from app.pipelines.completeness_runner import CompletenessProgress
from app.pipelines.fastp_runner import TrimProgress
from app.pipelines.variant_runner import VariantProgress

FIXTURES = Path(__file__).parent.parent / "fixtures" / "tool_logs"


def _replay(parser, log_path: Path) -> list:
    """Feed every line to the parser; return the phase after each accepted
    update, in order -- the sequence a UI would actually have shown."""
    phases = []
    for line in log_path.read_text().splitlines():
        if parser.feed(line):
            phases.append(parser.phase)
    return phases


def _phase_changes(parser, log_path: Path) -> list:
    """Like _replay, but only the entries where the phase itself changed --
    for parsers (VariantProgress, post-#60) whose feed() also fires for
    non-phase updates like Clair3's chunk counts, where phases from _replay
    would repeat the current phase on every chunk-completion line."""
    phases = []
    last = parser.phase
    for line in log_path.read_text().splitlines():
        if parser.feed(line) and parser.phase != last:
            phases.append(parser.phase)
            last = parser.phase
    return phases


class TestFastpFixture:
    """fastp-0.24.0.log: a real single-end trim, ~6.2K reads."""

    def test_reaches_the_reporting_phase(self):
        parser = TrimProgress(expected_reads=6217)
        phases = _replay(parser, FIXTURES / "fastp-0.24.0.log")
        assert phases[-1] == "reporting"

    def test_counts_real_reads_loaded(self):
        """The fixture's 'Loading completed with 25 packs' line has no reads
        count -- fastp only reports load progress via a separate 'loadedNM
        reads' line during larger runs.  This run is too small to ever emit
        one, so reads_loaded correctly stays at zero and pct stays None: an
        indeterminate bar, not a fabricated one, which is the honest
        behaviour for a run this size."""
        parser = TrimProgress(expected_reads=6217)
        _replay(parser, FIXTURES / "fastp-0.24.0.log")
        assert parser.reads_loaded == 0
        assert parser.pct is None

    def test_phase_reaches_writing_before_reporting(self):
        parser = TrimProgress(expected_reads=6217)
        phases = _replay(parser, FIXTURES / "fastp-0.24.0.log")
        assert "writing" in phases
        assert phases.index("writing") < phases.index("reporting")


class TestFlyeFixture:
    """flye-2.9.5.log: a real assembly that aborted after the assembly stage
    (no disjointigs) -- still a legitimate replay target, since a parser must
    track phase correctly right up to the point a tool gives up."""

    def test_reaches_the_assembly_stage(self):
        parser = AssemblyProgress()
        phases = _replay(parser, FIXTURES / "flye-2.9.5.log")
        assert phases[0] == "configuring"
        assert "assembling draft" in phases

    def test_never_regresses_to_starting(self):
        parser = AssemblyProgress()
        phases = _replay(parser, FIXTURES / "flye-2.9.5.log")
        assert "starting" not in phases


class TestMinimap2Fixture:
    """minimap2-2.27.log: a real minimap2 | samtools sort pipeline over
    400K generated long reads against a 2Mb generated reference (see the
    module docstring for why the input is synthetic).

    This replay is what settled a claim the code carried without ever having
    checked it: a comment on _PROCESSED_RE used to say minimap2 "says nothing
    comparable per-batch" to bwa-mem2. Running it showed that is wrong --
    minimap2 does emit a per-batch `mapped N sequences` line via the same
    logging library bwa-mem2 uses -- just at a batch size minimap2 decides
    internally (166776, 166833, 66391 for this run) rather than a fixed
    count. The old comment was itself unverified, which is the same failure
    this fixture convention exists to prevent, just caught before it became a
    bug rather than after.
    """

    def test_reaches_the_aligning_phase_with_measured_progress(self):
        parser = AlignProgress(expected_reads=400_000)
        phases = _replay(parser, FIXTURES / "minimap2-2.27.log")
        assert "aligning" in phases
        assert parser.processed == 166776 + 166833 + 66391

    def test_batches_sum_rather_than_overwrite(self):
        """Each worker_pipeline line is a distinct batch, not a running
        total -- the same shape as bwa-mem2's Processed lines, and the same
        mistake (treating it as cumulative already) that summing, not
        assigning, exists to avoid."""
        parser = AlignProgress(expected_reads=1_000_000)
        parser.feed("[M::worker_pipeline::13.514*3.90] mapped 166776 sequences")
        parser.feed("[M::worker_pipeline::26.460*3.97] mapped 166833 sequences")
        assert parser.processed == 166776 + 166833

    def test_reaches_the_sorting_phase(self):
        """The fixture's pipeline is the real minimap2 | samtools sort shape
        this repo actually runs, so the merge-phase transition applies to
        minimap2 exactly as it does to bwa-mem2."""
        parser = AlignProgress(expected_reads=400_000)
        phases = _replay(parser, FIXTURES / "minimap2-2.27.log")
        assert phases[-1] == "sorting"


class TestClair3Fixture:
    """clair3-v2.0.2.log: a real full run (pileup -> full-alignment -> merge)
    against a small reference with several contigs, one chunk each.

    This replay originally exposed two live bugs in _PHASE_PATTERNS, found
    only because this is a real log rather than a hand-built one: the
    "merging" phase never fired (Clair3 logs "Merge", not "merging"), and the
    per-contig summary lines near the end caused the phase to flip repeatedly
    between pileup and full_alignment. _PHASE_PATTERNS now anchors on Clair3's
    actual numbered-stage banners instead of loose substrings, and these
    assertions pin the fixed sequence: pileup -> full_alignment -> merging,
    exactly once each, with no flicker at the end.

    Uses _phase_changes rather than _replay for the phase-sequence
    assertions, since #60 added chunk-count tracking that also makes feed()
    return True -- _replay would then repeat the current phase on every
    chunk-completion line, which is correct new behaviour, not the flicker
    these tests originally guarded against.
    """

    def test_reaches_full_alignment(self):
        parser = VariantProgress()
        phases = _phase_changes(parser, FIXTURES / "clair3-v2.0.2.log")
        assert "full_alignment" in phases

    def test_merging_phase_fires(self):
        """Clair3's real merge banner is 'Merge pileup VCF...', not
        'merging' -- the pattern must match the capitalized real banner."""
        parser = VariantProgress()
        phases = _phase_changes(parser, FIXTURES / "clair3-v2.0.2.log")
        assert "merging" in phases

    def test_phase_sequence_has_no_flicker(self):
        """Per-contig summary lines near the end ('Pileup variants processed
        in <contig>: N', 'Full-alignment variants processed in <contig>: N')
        must not re-trigger pileup/full_alignment once the run has moved on
        to merging."""
        parser = VariantProgress()
        phases = _phase_changes(parser, FIXTURES / "clair3-v2.0.2.log")
        assert phases == ["pileup", "full_alignment", "merging"]

    def test_config_echo_does_not_trigger_full_alignment_early(self):
        """The early config line 'ENABLE NO PHASING FOR FULL ALIGNMENT: False'
        must not be mistaken for the start of full-alignment work."""
        parser = VariantProgress()
        phases = _phase_changes(parser, FIXTURES / "clair3-v2.0.2.log")
        assert phases[0] == "pileup"

    def test_units_total_is_the_sum_of_the_declared_chunk_plan(self):
        """'Chunk number for each contig: 1 1 1 1 1' -- five contigs, one
        chunk each, on this fixture's small reference."""
        parser = VariantProgress()
        _replay(parser, FIXTURES / "clair3-v2.0.2.log")
        assert parser.units_total == 5

    def test_units_done_counts_distinct_chunk_completions(self):
        """Five 'Total processed positions in <contig> (chunk 1/1)' lines in
        the pileup phase; the sixth, later 'chunk 1/1' line for NC_001135.5
        during full-alignment reprocesses a contig already counted and must
        not double it."""
        parser = VariantProgress()
        _replay(parser, FIXTURES / "clair3-v2.0.2.log")
        assert parser.units_done == 5

    def test_units_appear_in_snapshot_once_the_plan_is_known(self):
        parser = VariantProgress()
        for line in (FIXTURES / "clair3-v2.0.2.log").read_text().splitlines():
            parser.feed(line)
            if parser.units_total is not None:
                break
        snap = parser.snapshot()
        assert snap["units_total"] == 5
        assert snap["unit_label"] == "chunks"


class TestCompleasmFixture:
    """compleasm-0.2.9.log: a real completeness run against a yeast genome
    and the saccharomycetaceae_odb12 lineage -- miniprot mapping, then
    hmmsearch scoring.

    compleasm's own stderr is almost entirely tool-discovery chatter before
    miniprot starts; the mapped-sequence lines are the real signal, and this
    is the fixture that settled whether they were parseable at all rather
    than assuming so from #57's minimap2 precedent.
    """

    def test_starts_in_the_mapping_phase(self):
        parser = CompletenessProgress()
        assert parser.phase == "mapping"

    def test_mapped_count_accumulates_across_batches(self):
        parser = CompletenessProgress()
        _replay(parser, FIXTURES / "compleasm-0.2.9.log")
        # Sum of every "mapped N sequences" line in the fixture.
        assert parser.mapped == 143990

    def test_reaches_the_scoring_phase(self):
        parser = CompletenessProgress()
        phases = _replay(parser, FIXTURES / "compleasm-0.2.9.log")
        assert phases[-1] == "scoring"

    def test_scoring_phase_does_not_republish(self):
        """hmmsearch prints its own command-echo line once; a second
        occurrence must not count as a second phase transition."""
        parser = CompletenessProgress()
        parser.feed("hmmsearch execute command:")
        assert parser.feed("hmmsearch execute command:") is False

    def test_pct_is_always_none(self):
        """No honest total to divide the mapped count by -- see the class
        docstring for why, same reasoning as Flye's phase-only progress."""
        parser = CompletenessProgress()
        _replay(parser, FIXTURES / "compleasm-0.2.9.log")
        assert parser.pct is None
