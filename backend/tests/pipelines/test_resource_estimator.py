"""Memory estimation and the three bands.

Band edges carry the weight here. The logic is pure arithmetic over
coefficients, and an off-by-one comparison at a boundary is invisible in
review but decides whether a run is blocked -- so every boundary is tested
from both sides.
"""

import pytest

from app.pipelines import resource_estimator as est
from app.pipelines.aligners import Aligner


class TestEstimate:
    def test_sort_memory_is_multiplied_by_threads(self):
        """The term users actually trip over: sort memory is per thread, so
        8 threads at 1024 MB is 8 GB, not 1 GB."""
        one = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=0, threads=1,
            sort_memory_mb=1024, building_index=False,
        )
        eight = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=0, threads=8,
            sort_memory_mb=1024, building_index=False,
        )
        assert eight - one >= 7 * 1024

    def test_a_larger_reference_costs_more(self):
        small = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=1_000_000, threads=4,
            sort_memory_mb=512, building_index=False,
        )
        human = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=3_100_000_000, threads=4,
            sort_memory_mb=512, building_index=False,
        )
        assert human > small

    def test_bowtie2_human_index_is_in_the_expected_range(self):
        """A sanity check on the coefficient, not a precise claim: bowtie2's
        published GRCh38 index is about 3.5 GB, so an estimate that came out
        at 300 MB or 30 GB would mean the units are wrong."""
        mb = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=3_100_000_000, threads=1,
            sort_memory_mb=64, building_index=False,
        )
        assert 2_000 < mb < 8_000

    def test_building_an_index_costs_more_than_loading_one(self):
        loading = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=3_100_000_000, threads=4,
            sort_memory_mb=512, building_index=False,
        )
        building = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=3_100_000_000, threads=4,
            sort_memory_mb=512, building_index=True,
        )
        assert building > loading


class TestBands:
    def test_well_under_budget_is_ok(self):
        assert est.classify(estimated_mb=1000, mem_budget_mb=16000,
                            threads=4, cpu_budget=8) is est.Band.OK

    def test_just_under_the_warn_edge_is_ok(self):
        assert est.classify(estimated_mb=6999, mem_budget_mb=10000,
                            threads=4, cpu_budget=8) is est.Band.OK

    def test_at_the_warn_edge_is_warn(self):
        assert est.classify(estimated_mb=7000, mem_budget_mb=10000,
                            threads=4, cpu_budget=8) is est.Band.WARN

    def test_just_under_budget_is_warn_not_block(self):
        assert est.classify(estimated_mb=9999, mem_budget_mb=10000,
                            threads=4, cpu_budget=8) is est.Band.WARN

    def test_over_budget_is_block(self):
        assert est.classify(estimated_mb=10001, mem_budget_mb=10000,
                            threads=4, cpu_budget=8) is est.Band.BLOCK

    def test_exactly_at_budget_is_warn(self):
        """The conservative edge the design calls for: block is reserved for
        genuinely impossible, and exactly-at-budget is merely doomed."""
        assert est.classify(estimated_mb=10000, mem_budget_mb=10000,
                            threads=4, cpu_budget=8) is est.Band.WARN

    def test_too_many_threads_warns_but_does_not_block(self):
        """Oversubscribing CPUs is slow and recoverable; it is not the
        twenty-minutes-then-OOM failure that blocking exists for."""
        assert est.classify(estimated_mb=100, mem_budget_mb=16000,
                            threads=32, cpu_budget=8) is est.Band.WARN

    def test_an_unknown_budget_never_blocks(self):
        """A host whose budget could not be read is not evidence that a run
        will fail, and blocking on missing information would be wrong."""
        assert est.classify(estimated_mb=99_999, mem_budget_mb=None,
                            threads=4, cpu_budget=None) is est.Band.OK


class TestExplain:
    def test_the_message_names_the_dominant_term(self):
        """A warning that does not say what to change is not actionable."""
        msg = est.explain(
            aligner=Aligner.BOWTIE2, reference_bases=100_000, threads=16,
            sort_memory_mb=2048, building_index=False, mem_budget_mb=8000,
        )
        assert "sort" in msg.lower()

    def test_the_message_reports_both_numbers(self):
        msg = est.explain(
            aligner=Aligner.BOWTIE2, reference_bases=3_100_000_000, threads=4,
            sort_memory_mb=1024, building_index=False, mem_budget_mb=8000,
        )
        assert "8000" in msg or "8,000" in msg or "7.8" in msg
