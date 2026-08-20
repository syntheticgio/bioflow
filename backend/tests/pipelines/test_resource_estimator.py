"""Memory estimation and the three bands.

Band edges carry the weight here. The logic is pure arithmetic over
coefficients, and an off-by-one comparison at a boundary is invisible in
review but decides whether a run is blocked -- so every boundary is tested
from both sides.
"""

import math

from app.pipelines import resource_estimator as est
from app.pipelines.aligner_registry import spec_for
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

    def test_a_fractional_raw_total_rounds_up_not_down(self):
        """`estimate_mb` must round up, never truncate: classify()'s BLOCK
        check is a strict `>`, so truncating a raw total like 10000.9 MB down
        to 10000 against a 10000 MB budget would turn a genuine BLOCK into a
        WARN -- the opposite of this module's stated bias toward spurious
        warnings over missed blocks.

        These inputs are chosen so the raw (pre-rounding) total can be
        computed by hand: bowtie2's index_bytes_per_ref_base is 1.0, so a
        reference of 1,048,577 bases (one mebibyte plus one base) yields an
        index_mb with a nonzero fractional part, on top of round
        fixed_overhead_mb=256 and one thread's worker_mb=200 and no sort
        contribution.
        """
        model = spec_for(Aligner.BOWTIE2).memory_model
        reference_bases = 1_048_577
        threads = 1
        sort_memory_mb = 0

        index_mb = (reference_bases * model.index_bytes_per_ref_base) / (1024 * 1024)
        worker_mb = threads * model.bytes_per_thread_mb
        sort_mb = threads * sort_memory_mb
        raw = model.fixed_overhead_mb + index_mb + worker_mb + sort_mb

        # Sanity check on the hand-computed value: it must actually have a
        # fractional part, or this test would not distinguish ceil from int.
        assert raw != int(raw)

        result = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=reference_bases,
            threads=threads, sort_memory_mb=sort_memory_mb,
            building_index=False,
        )

        assert result == math.ceil(raw)
        assert result != int(raw)


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

    def test_memory_warn_and_thread_overrun_together_is_still_warn(self):
        """Memory is checked before threads in classify()'s early-return
        structure, so a configuration that is simultaneously in the memory
        WARN band and over the CPU budget must still come out WARN, not
        something else -- this pins the precedence for future refactors."""
        assert est.classify(estimated_mb=8000, mem_budget_mb=10000,
                            threads=32, cpu_budget=8) is est.Band.WARN


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

    def test_fixed_and_worker_overhead_can_dominate_over_index_and_sort(self):
        """A high-thread minimap2 run with a small reference and small sort
        memory: fixed_overhead_mb (512) plus worker_mb (16 threads x 512 MB
        = 8192 MB) together dwarf both the raw index (~0.14 MB for a
        100,000-base reference at 1.5 bytes/base) and the sort term (16
        threads x 10 MB = 160 MB).

        The old comparison (`sort_mb >= index_mb`) ignored fixed/worker
        overhead entirely and would have called this "sort buffer is
        dominant" (160 >= 0.14), which is wrong -- sort is a small fraction
        of the total. It also should not be described as purely "the index"
        once worker/fixed overhead make up most of that non-sort share.
        """
        msg = est.explain(
            aligner=Aligner.MINIMAP2, reference_bases=100_000, threads=16,
            sort_memory_mb=10, building_index=False, mem_budget_mb=None,
        )
        lower = msg.lower()
        assert "sort" not in lower
        assert "aligner itself" in lower


class TestExplainProvenance:
    def test_provenance_clause_is_appended_when_given(self):
        """The user is told which model produced the number they are about to
        override. Without it, 'Estimated 14 GB' from coefficients and from 23
        measured runs are indistinguishable, and they justify very different
        confidence."""
        text = est.explain(
            aligner=Aligner.BWA_MEM2,
            reference_bases=3_000_000_000,
            threads=8,
            sort_memory_mb=1024,
            building_index=False,
            mem_budget_mb=16384,
            provenance="from 23 previous runs on this machine",
        )
        assert "from 23 previous runs on this machine" in text

    def test_omitting_provenance_leaves_the_message_unchanged(self):
        """Existing callers pass nothing and must be unaffected."""
        kwargs = dict(
            aligner=Aligner.BWA_MEM2,
            reference_bases=3_000_000_000,
            threads=8,
            sort_memory_mb=1024,
            building_index=False,
            mem_budget_mb=16384,
        )
        assert est.explain(**kwargs) == est.explain(**kwargs, provenance="")


class TestAssemblyEstimate:
    def test_flye_estimate_ignores_read_bases(self):
        """Flye's model is genome-dominated; adding reads must not move it."""
        from app.pipelines import resource_estimator
        from app.pipelines.assemblers import Assembler

        without = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.FLYE, genome_bases=5_000_000, threads=8
        )
        with_reads = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.FLYE,
            genome_bases=5_000_000,
            threads=8,
            read_bases=2_000_000_000,
        )
        assert without == with_reads

    def test_abyss_estimate_grows_with_read_bases(self):
        """A de Bruijn graph's peak tracks distinct k-mers, so coverage counts."""
        from app.pipelines import resource_estimator
        from app.pipelines.assemblers import Assembler

        low = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.ABYSS,
            genome_bases=5_000_000,
            threads=8,
            read_bases=500_000_000,
        )
        high = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.ABYSS,
            genome_bases=5_000_000,
            threads=8,
            read_bases=5_000_000_000,
        )
        assert high > low

    def test_assembly_estimate_still_none_without_genome_size(self):
        """None stays a real answer -- de novo is what you do with no reference."""
        from app.pipelines import resource_estimator
        from app.pipelines.assemblers import Assembler

        assert (
            resource_estimator.estimate_assembly_mb(
                assembler=Assembler.ABYSS, genome_bases=None, threads=8
            )
            is None
        )

    def test_flye_meta_estimate_ignores_genome_bases(self):
        """Meta mode switches to the read-volume model; a genome size, if one
        is present anyway, must not move the estimate."""
        from app.pipelines import resource_estimator
        from app.pipelines.assemblers import Assembler

        without_genome = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.FLYE,
            genome_bases=None,
            threads=8,
            read_bases=10_000_000_000,
            meta=True,
        )
        with_genome = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.FLYE,
            genome_bases=5_000_000_000,
            threads=8,
            read_bases=10_000_000_000,
            meta=True,
        )
        assert without_genome == with_genome

    def test_spades_meta_estimate_uses_read_volume_not_genome_size(self):
        """The whole point of the second model: a community assembly normally
        has no genome size, and without a meta model the estimate would be
        None -- an unguarded run rather than a conservative one."""
        from app.pipelines import resource_estimator
        from app.pipelines.assemblers import Assembler

        est = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.SPADES,
            genome_bases=None,
            threads=4,
            read_bases=52_799_700,
            meta=True,
        )
        assert est is not None
        assert est > 0

    def test_spades_meta_estimate_covers_the_measured_peaks(self):
        """Both bake-off runs from #731, whose peaks the model must not
        under-call: under-provisioning is an OOM, and an OOM-killed run also
        poisons the timing models.

        Measured with metaSPAdes 4.3.0 at 4 threads over a 5-organism
        synthetic community with a 30x abundance spread.
        """
        from app.pipelines import resource_estimator
        from app.pipelines.assemblers import Assembler

        for read_bases, observed_peak_mb in ((52_799_700, 776), (158_398_800, 2154)):
            est = resource_estimator.estimate_assembly_mb(
                assembler=Assembler.SPADES,
                genome_bases=None,
                threads=4,
                read_bases=read_bases,
                meta=True,
            )
            assert est >= observed_peak_mb, (read_bases, est, observed_peak_mb)

    def test_non_meta_spades_estimate_still_uses_the_genome_model(self):
        """The three single-genome modes must be arithmetically untouched by
        the meta model existing."""
        from app.pipelines import resource_estimator
        from app.pipelines.assemblers import Assembler

        est = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.SPADES,
            genome_bases=5_000_000,
            threads=4,
            read_bases=500_000_000,
            meta=False,
        )
        spec_model = __import__(
            "app.pipelines.assembler_registry", fromlist=["spec_for"]
        ).spec_for(Assembler.SPADES).memory_model
        expected_genome_mb = (5_000_000 * spec_model.bytes_per_genome_base) / 1024**2
        assert est > expected_genome_mb

    def test_flye_meta_estimate_grows_with_read_bases(self):
        from app.pipelines import resource_estimator
        from app.pipelines.assemblers import Assembler

        low = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.FLYE, genome_bases=None, threads=8,
            read_bases=1_000_000_000, meta=True,
        )
        high = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.FLYE, genome_bases=None, threads=8,
            read_bases=100_000_000_000, meta=True,
        )
        assert high > low

    def test_flye_meta_estimate_is_none_without_read_bases(self):
        """No genome size and no read volume leaves nothing to estimate off."""
        from app.pipelines import resource_estimator
        from app.pipelines.assemblers import Assembler

        assert (
            resource_estimator.estimate_assembly_mb(
                assembler=Assembler.FLYE, genome_bases=None, threads=8,
                read_bases=None, meta=True,
            )
            is None
        )

    def test_flye_meta_estimate_matches_the_benchmark_order_of_magnitude(self):
        """Sanity check on the coefficient, not a precise claim: Flye's own
        HMP mock benchmark (7 Gb PacBio meta input) peaked at 72 GB. A 7 Gbp
        input here should land in the tens-of-GB range, not hundreds of MB
        or multiple terabytes."""
        from app.pipelines import resource_estimator
        from app.pipelines.assemblers import Assembler

        mb = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.FLYE, genome_bases=None, threads=8,
            read_bases=7_000_000_000, meta=True,
        )
        assert 10_000 < mb < 200_000

    def test_non_meta_flye_estimate_is_unaffected_by_the_meta_flag_default(self):
        """meta defaults to False, so every existing single-genome caller
        (which never passes it) is unchanged."""
        from app.pipelines import resource_estimator
        from app.pipelines.assemblers import Assembler

        default = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.FLYE, genome_bases=5_000_000, threads=8
        )
        explicit_false = resource_estimator.estimate_assembly_mb(
            assembler=Assembler.FLYE, genome_bases=5_000_000, threads=8,
            meta=False,
        )
        assert default == explicit_false


def test_exceeds_declared_budget_is_a_strict_comparison():
    # Equal fits: claim.lua admits on `mem <= mem_free`.
    assert not est.exceeds_declared_budget(
        declared_mb=8000, budget_mb=8000
    )
    assert est.exceeds_declared_budget(
        declared_mb=8001, budget_mb=8000
    )


def test_declared_refusal_names_both_numbers():
    msg = est.explain_declared_refusal(
        declared_mb=16384, budget_mb=5600
    )
    assert "16,384" in msg
    assert "5,600" in msg


def test_declared_refusal_says_requires_not_estimated():
    """R7: distinguishable from the heuristic refusal, which says 'Estimated'.

    The declared number is a fixed reservation, not a prediction. Calling it an
    estimate would send the user looking for a slider to move, when the fix is
    the memory budget setting.
    """
    msg = est.explain_declared_refusal(
        declared_mb=16384, budget_mb=5600
    )
    assert "Estimated" not in msg
    assert "requires" in msg.lower()


def test_declared_refusal_points_at_the_setting():
    msg = est.explain_declared_refusal(
        declared_mb=16384, budget_mb=5600
    )
    assert "memory budget" in msg.lower()
