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

from app.queue.transcript_qc_handlers import _sampling_plan


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
