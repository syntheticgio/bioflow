"""`_sampling_plan`'s floor allocation for small contigs.

`_sampling_plan` splits a read budget proportionally across a BAM's
contigs by length. `share = int(budget * length / total)` truncates to
zero for any contig whose proportional share rounds under 1 read, and the
old code dropped that contig from the plan entirely via `if share > 0`.
On a scaffold-heavy assembly (hundreds of small/unplaced contigs) that
silently drops many contigs and undershoots the nominal budget with no
signal anywhere. See the code-review finding fixed alongside this test:
every contig with nonzero length now gets at least 1 read of budget.

`_sampling_plan` only touches `af.references` and
`af.get_reference_length()`, so a tiny stub stands in for
`pysam.AlignmentFile` -- no BAM file needed.
"""

import gzip
from unittest.mock import patch

from app.queue.transcript_qc_handlers import _opener_for, _sampling_plan


class TestOpenerFor:
    """`_opener_for` picks gzip.open for gzip/bgzf-compressed GTFs and plain
    `open` otherwise -- see gc_tracks.compute_gc_tracks for the pattern this
    mirrors. Opening a BGZF-compressed GTF with plain `open()` doesn't raise;
    it silently reads garbled bytes, which is the bug this guards against
    (NCBI Datasets ships GTFs bgzf-compressed by default)."""

    def test_bgzf_uses_gzip_open(self):
        assert _opener_for("bgzf") is gzip.open

    def test_gzip_uses_gzip_open(self):
        assert _opener_for("gzip") is gzip.open

    def test_none_uses_plain_open(self):
        assert _opener_for("none") is open

    def test_missing_compression_uses_plain_open(self):
        assert _opener_for(None) is open

    def test_unknown_compression_value_falls_back_to_plain_open(self):
        """A payload value that isn't a valid Compression member (e.g. a
        typo, or a future enum member this code doesn't know about yet)
        should not raise -- fall back to plain open rather than crash the
        job on an unrelated string."""
        assert _opener_for("not-a-real-compression") is open

    def test_zstd_and_bzip2_are_not_treated_as_gzip_compatible(self):
        """gzip.open cannot read zstd or bzip2 -- only gzip/bgzf go through
        it. These should fall back to plain open rather than silently
        garble (matches current behavior: no zstd/bzip2 GTF support yet)."""
        assert _opener_for("zstd") is open
        assert _opener_for("bzip2") is open


class _FakeAlignmentFile:
    def __init__(self, lengths: dict[str, int]):
        self._lengths = lengths

    @property
    def references(self):
        return list(self._lengths)

    def get_reference_length(self, contig):
        return self._lengths[contig]


class TestSamplingPlanFloor:
    def test_small_contig_gets_one_read_instead_of_vanishing(self):
        """A contig whose proportional share truncates to zero must still
        appear in the plan with 1 read, not be dropped."""
        # chr1 is huge; scaffold_1 is small enough that its proportional
        # share of a 200k budget truncates to 0 under the old code.
        af = _FakeAlignmentFile({"chr1": 200_000_000, "scaffold_1": 500})
        plan = _sampling_plan(af, budget=200_000)

        contigs = dict(plan)
        assert "scaffold_1" in contigs
        assert contigs["scaffold_1"] == 1
        assert contigs["chr1"] > 0

    def test_many_small_contigs_all_survive(self):
        """A scaffold-heavy assembly: hundreds of small unplaced contigs
        alongside a few large chromosomes. None should disappear."""
        lengths = {f"chr{i}": 50_000_000 for i in range(1, 4)}
        lengths.update({f"scaffold_{i}": 1_000 for i in range(500)})
        af = _FakeAlignmentFile(lengths)

        plan = _sampling_plan(af, budget=200_000)
        contigs = dict(plan)

        assert len(contigs) == len(lengths)
        for name in lengths:
            assert contigs[name] >= 1

    def test_planned_total_can_exceed_budget_by_at_most_contig_count(self):
        """The floor allocation is bounded: overshoot beyond `budget` is at
        most 1 extra read per contig that would otherwise have gotten 0."""
        lengths = {f"chr{i}": 10_000_000 for i in range(1, 3)}
        lengths.update({f"scaffold_{i}": 100 for i in range(300)})
        af = _FakeAlignmentFile(lengths)
        budget = 200_000

        plan = _sampling_plan(af, budget=budget)
        planned_total = sum(share for _, share in plan)

        assert planned_total >= budget
        assert planned_total <= budget + len(lengths)

    def test_zero_length_contig_is_excluded_not_floored(self):
        """A contig with no length (or missing length info) shouldn't get
        a phantom floor read -- there's nothing to sample."""
        af = _FakeAlignmentFile({"chr1": 1_000_000, "chrUn_random": 0})
        plan = _sampling_plan(af, budget=200_000)
        contigs = dict(plan)

        assert "chrUn_random" not in contigs
        assert "chr1" in contigs

    def test_normal_case_unaffected_by_floor(self):
        """When every contig's proportional share is already >= 1, the
        floor should not change anything."""
        af = _FakeAlignmentFile({"chr1": 100_000, "chr2": 100_000})
        plan = _sampling_plan(af, budget=200_000)
        contigs = dict(plan)

        assert contigs["chr1"] == 100_000
        assert contigs["chr2"] == 100_000

    def test_all_lengths_unavailable_falls_back_to_first_contig(self):
        af = _FakeAlignmentFile({"chr1": 0, "chr2": 0})
        plan = _sampling_plan(af, budget=200_000)

        assert plan == [("chr1", 200_000)]

    def test_no_log_when_no_contig_needed_the_floor(self):
        """Ordinary rounding always makes planned_total != budget, but that
        alone is not floor activity -- the log must stay silent unless a
        contig's share actually truncated to zero and got bumped to 1."""
        af = _FakeAlignmentFile({"chr1": 100_000, "chr2": 100_000, "chr3": 100_000})
        with patch("app.queue.transcript_qc_handlers.log") as mock_log:
            _sampling_plan(af, budget=200_000)
            mock_log.info.assert_not_called()

    def test_logs_floored_contig_count_when_floor_fires(self):
        af = _FakeAlignmentFile({"chr1": 200_000_000, "scaffold_1": 500})
        with patch("app.queue.transcript_qc_handlers.log") as mock_log:
            _sampling_plan(af, budget=200_000)
            mock_log.info.assert_called_once()
            args, kwargs = mock_log.info.call_args
            assert args[0] == "transcript_qc_sampling_plan_floored"
            assert kwargs["floored_contigs"] == 1
