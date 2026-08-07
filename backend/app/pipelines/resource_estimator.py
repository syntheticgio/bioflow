"""Whether a proposed alignment fits on this machine.

Two failure modes, and they differ in kind. Too many threads is slow and
recoverable. An index that does not fit in RAM is an OOM kill twenty minutes
in, with a log that says nothing useful -- the job simply stops. Only the
second is worth blocking, which is why the bands below are asymmetric: thread
oversubscription warns, memory overrun blocks.

The coefficients are heuristics from published tool documentation, not
measurements on this hardware. That is the reason BLOCK is set at
strictly-over-budget rather than at some safety margin below it: a wrong
coefficient should cost a spurious warning, never a blocked run that would
have worked.
"""

import math
from enum import StrEnum

from app.pipelines.aligner_registry import spec_for
from app.pipelines.aligners import Aligner

# Below this fraction of the budget, say nothing.
# A heuristic like the MemoryModel coefficients above -- not a precisely
# derived number -- chosen to leave headroom before the BLOCK edge without
# nagging on runs that are comfortably within budget.
WARN_FRACTION = 0.70


class Band(StrEnum):
    OK = "ok"
    WARN = "warn"
    BLOCK = "block"


def estimate_mb(
    *,
    aligner: Aligner,
    reference_bases: int,
    threads: int,
    sort_memory_mb: int,
    building_index: bool,
) -> int:
    """Peak resident memory for a run, in MB.

    The samtools sort term is the one that surprises people: `-m` is per
    thread, so it multiplies. Everything else is the aligner's own index plus
    per-worker buffers.
    """
    model = spec_for(aligner).memory_model

    index_mb = (reference_bases * model.index_bytes_per_ref_base) / (1024 * 1024)
    if building_index:
        index_mb *= model.index_build_multiplier

    worker_mb = threads * model.bytes_per_thread_mb
    sort_mb = threads * sort_memory_mb

    # Round up, not toward zero: classify()'s BLOCK check is a strict `>`,
    # so truncating a raw total like 10000.9 down to 10000 against a 10000 MB
    # budget would turn a genuine BLOCK into a WARN -- the opposite of the
    # module's stated bias (see docstring above).
    return math.ceil(model.fixed_overhead_mb + index_mb + worker_mb + sort_mb)


def estimate_assembly_mb(
    *,
    assembler,
    genome_bases: int | None,
    threads: int,
) -> int | None:
    """Peak resident memory for a de novo assembly, in MB. None if unknowable.

    The genome dominates rather than the reads: a repeat graph is built over
    the assembly, and coverage drives runtime far more than peak residency.

    **None is a real answer, not a failure.** De novo assembly is what you do
    when there is no reference, so a project that cannot supply a genome size
    is the normal case rather than a misconfigured one. Callers must treat
    None as "no opinion" and let the run proceed -- refusing to start because
    we could not guess would be worse than starting and failing, which at
    least produces a log.
    """
    if genome_bases is None or genome_bases <= 0:
        return None

    from app.pipelines.assembler_registry import spec_for as assembler_spec_for

    model = assembler_spec_for(assembler).memory_model
    graph_mb = (genome_bases * model.bytes_per_genome_base) / (1024 * 1024)
    return math.ceil(model.fixed_overhead_mb + graph_mb + threads * model.mb_per_thread)


def classify(
    *,
    # Named `estimated_mb` rather than `estimate_mb` so it does not shadow the
    # function above within this scope -- the two would be indistinguishable
    # at a glance in a file where both are in play.
    estimated_mb: int,
    mem_budget_mb: int | None,
    threads: int,
    cpu_budget: float | None,
) -> Band:
    """Which band a configuration falls in.

    A missing budget yields OK rather than a guess: not being able to read the
    host's limits is not evidence that a run will fail, and blocking on absent
    information would stop work for no reason.
    """
    if mem_budget_mb is None:
        return Band.OK

    if estimated_mb > mem_budget_mb:
        return Band.BLOCK

    if estimated_mb >= mem_budget_mb * WARN_FRACTION:
        return Band.WARN

    # Same "missing data, no opinion" policy as the mem_budget_mb check
    # above: a cpu_budget of None means we could not read the host's CPU
    # limit, not that thread count is fine -- so no warning fires regardless
    # of how many threads are requested.
    if cpu_budget is not None and threads > cpu_budget:
        return Band.WARN

    return Band.OK


def explain(
    *,
    aligner: Aligner,
    reference_bases: int,
    threads: int,
    sort_memory_mb: int,
    building_index: bool,
    mem_budget_mb: int | None,
    provenance: str = "",
) -> str:
    """A sentence naming the dominant term and both numbers.

    "Estimated 14 GB of 16 GB" is a fact; "Sort buffer is 8 GB of that (8
    threads x 1024 MB)" is what tells someone which slider to move. A warning
    without the second half is not actionable.

    `provenance` names the model the number came from. This stays the
    *heuristic's* explainer: when the measured model wins there is no
    sort-buffer breakdown to give, because that model does not have one. So
    the "which slider to move" half below appears only when the heuristic is
    what is being reported -- which is exactly when it is true.
    """
    total = estimate_mb(
        aligner=aligner,
        reference_bases=reference_bases,
        threads=threads,
        sort_memory_mb=sort_memory_mb,
        building_index=building_index,
    )
    model = spec_for(aligner).memory_model

    sort_mb = threads * sort_memory_mb
    worker_mb = threads * model.bytes_per_thread_mb
    index_mb = int((reference_bases * model.index_bytes_per_ref_base) / (1024 * 1024))
    if building_index:
        index_mb = int(index_mb * model.index_build_multiplier)

    budget_text = f" of {mem_budget_mb:,} MB available" if mem_budget_mb else ""
    parts = [f"Estimated {total:,} MB{budget_text}."]

    # worker_mb (per-thread buffers) is folded into the non-sort side rather
    # than compared on its own: it is part of the aligner's own footprint,
    # conceptually distinct from the sort step, and for a high-thread run it
    # can rival or exceed the index itself. Comparing sort_mb only against
    # index_mb (ignoring fixed_overhead_mb and worker_mb entirely) let the
    # message misattribute the dominant cost -- e.g. calling sort dominant
    # when the aligner's own overhead was actually the bigger share.
    aligner_side_mb = index_mb + worker_mb + model.fixed_overhead_mb
    if sort_mb >= aligner_side_mb:
        parts.append(
            f"The sort buffer is {sort_mb:,} MB of that "
            f"({threads} threads x {sort_memory_mb} MB each)."
        )
    else:
        # This number includes worker/fixed overhead as well as the raw
        # index, so it is described as "the aligner itself" rather than
        # specifically "the index" -- that would overclaim precision the
        # heuristic doesn't have.
        what = "building the index" if building_index else "the aligner itself"
        parts.append(f"Most of it is {what}: about {aligner_side_mb:,.0f} MB.")

    if provenance:
        parts.append(f"Estimate {provenance}.")

    return " ".join(parts)
