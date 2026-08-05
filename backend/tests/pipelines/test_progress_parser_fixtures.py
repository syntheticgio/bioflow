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

Fixtures live in tests/fixtures/tool_logs/{tool}-{version}.log, captured from
real jobs on this stack. See docs/superpowers/specs/2026-08-05-tool-progress-
instrumentation-design.md, "Golden log fixtures".
"""

from pathlib import Path

from app.pipelines.assembly_runner import AssemblyProgress
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


class TestClair3Fixture:
    """clair3-v2.0.2.log: a real full run (pileup -> full-alignment -> merge)
    against a small reference with several contigs.

    This replay exposes two live bugs in _PHASE_PATTERNS, found only because
    this is a real log rather than a hand-built one: the "merging" phase never
    fires (Clair3 logs "Merge", not "merging"), and the per-contig summary
    lines near the end cause the phase to flip repeatedly between pileup and
    full_alignment. These assertions pin the *current* (buggy) behaviour so a
    fix is visible as a test change rather than a silent regex edit -- see the
    follow-up filed against variant_runner.py's _PHASE_PATTERNS.
    """

    def test_reaches_full_alignment(self):
        parser = VariantProgress()
        phases = _replay(parser, FIXTURES / "clair3-v2.0.2.log")
        assert "full_alignment" in phases

    def test_merging_phase_does_not_fire(self):
        """Known bug: Clair3's real merge banner is 'Merge', not 'merging'."""
        parser = VariantProgress()
        phases = _replay(parser, FIXTURES / "clair3-v2.0.2.log")
        assert "merging" not in phases

    def test_phase_flickers_near_the_end(self):
        """Known bug: per-contig summary lines re-trigger pileup/
        full_alignment repeatedly in the final stretch of a run that is
        otherwise complete."""
        parser = VariantProgress()
        phases = _replay(parser, FIXTURES / "clair3-v2.0.2.log")
        tail = phases[-10:]
        assert len(set(tail)) > 1, (
            "expected the known end-of-run flicker; if this now holds a "
            "single phase, the _PHASE_PATTERNS fix landed -- update this test"
        )
