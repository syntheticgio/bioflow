"""Which memory estimate to believe, and where it came from.

Three sources can answer "how much memory will this job need," and they know
different things:

  * **Measured** -- `timing_service.estimate_memory()`, a fit against this
    machine's own run history. The best evidence when there is enough of it,
    and keyed only by job type and input size.
  * **Heuristic** -- `resource_estimator`, coefficients from published tool
    documentation, keyed by the *structure* of the run (aligner, threads, sort
    buffer). Always available for alignments; may honestly decline for
    assemblies.
  * **Declared** -- the per-handler constants in `@handler(resources=...)`.

**The caller computes its own heuristic and passes it in.** Only the caller
knows whether this is an alignment or an assembly, and those take different
inputs and have different contracts about `None`. Keeping that out here is what
lets this module stay ignorant of the pipeline domain.

**The source is part of the answer, not a debugging detail.** "Estimated 14 GB
from 23 previous runs" and "Estimated 14 GB from published tool coefficients"
justify different confidence, and the second is what a user overrides when they
choose to launch anyway.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.services import timing_service


class EstimateSource(StrEnum):
    MEASURED = "measured"
    HEURISTIC = "heuristic"
    # Not reachable from `resolve()` -- see the module note below. Present so
    # the API surface can report what a job was actually reserved with.
    DECLARED = "declared"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MemoryEstimate:
    """An estimate and its provenance.

    `mb` is None if and only if `source` is UNKNOWN. That is a real answer
    rather than a failure -- see `resource_estimator.estimate_assembly_mb`,
    whose docstring makes the same point: de novo assembly is what you do when
    there is no reference, so a project that cannot supply a genome size is the
    normal case. A caller seeing UNKNOWN must let the run proceed.
    """

    mb: int | None
    source: EstimateSource
    detail: str
    samples: int | None = None
    r_squared: float | None = None
    # True when a measured estimate existed and was rejected by the guards.
    # Without this the graduation path is invisible from outside: a job type
    # with plenty of history that still reports HEURISTIC would look identical
    # to one with no history at all.
    fell_back_from_measured: bool = False


def _is_trustworthy(measured: dict) -> bool:
    """Whether a known measured estimate should outrank the heuristic.

    Placeholder until Task 4 wires the guards -- a known estimate is trusted.
    """
    return True


async def resolve(
    *,
    job_type: str,
    input_bytes: int | None,
    heuristic_mb: int | None,
) -> MemoryEstimate:
    """Pick the most trustworthy available estimate and say which it is.

    `DECLARED` is deliberately unreachable here. The declared numbers are
    per-handler constants, not per-job facts, and they are already what a job
    gets when nobody calls this at all. Worse, `JobResources.mem_mb` defaults
    to 256: treating that as an answer would let an assembly with no genome
    size resolve to "256 MB, source: declared" and sail through a BLOCK check
    that should have stayed silent -- strictly worse than no estimate.
    """
    measured = None
    if input_bytes is not None:
        measured = await timing_service.estimate_memory(job_type, input_bytes)

    if measured is not None and measured.get("known"):
        if _is_trustworthy(measured):
            samples = measured["samples"]
            return MemoryEstimate(
                mb=int(measured["estimate_bytes"] / (1024 * 1024)),
                source=EstimateSource.MEASURED,
                detail=f"from {samples} previous runs on this machine",
                samples=samples,
                r_squared=measured.get("r_squared"),
            )
        rejected = True
    else:
        rejected = False

    if heuristic_mb is not None:
        return MemoryEstimate(
            mb=heuristic_mb,
            source=EstimateSource.HEURISTIC,
            detail="from published tool coefficients",
            fell_back_from_measured=rejected,
        )

    return MemoryEstimate(
        mb=None,
        source=EstimateSource.UNKNOWN,
        detail="",
        fell_back_from_measured=rejected,
    )
