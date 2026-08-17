"""Index-build memory estimates, anchored to each tool's published figure.

The build multiplier is the term these pin. It exists because building an
index costs more than loading one, and for bwa-mem2 the gap is far wider than
the original 2.0 allowed: bwa-mem2's README states the build "Requires 28N GB
memory where N is the size of the reference sequence", against ~10 GB resident
to align human. Modelled at 4 bytes/base effective, a 897 Mbp reference
reserved 7.8 GB for a build that needs ~23 GB -- so the queue governor admitted
it believing there was room and the OOM killer took it (issue #100, the cause
of the failure reported in #96).

These assert bands rather than exact numbers. The coefficients are heuristics
and are expected to be tuned; what must not drift is the order of magnitude,
which is what decides whether a build is admitted or blocked.
"""

import pytest
from app.pipelines import resource_estimator as est
from app.pipelines.aligner_registry import spec_for
from app.pipelines.aligners import Aligner

HUMAN_BASES = 3_100_000_000

# The reference from issue #96, whose index build was OOM-killed.
ISSUE_96_BASES = 897_131_848


def _index_only_mb(aligner: Aligner, bases: int) -> int:
    """The build estimate with the thread and sort terms driven to ~zero.

    Isolates the index term so these assertions are about the coefficient
    under test rather than about samtools' per-thread sort buffers.
    """
    return est.estimate_mb(
        aligner=aligner,
        reference_bases=bases,
        threads=1,
        sort_memory_mb=0,
        building_index=True,
    )


def test_bwa_mem2_index_build_matches_the_published_28n_figure():
    """bwa-mem2's README: "Requires 28N GB memory where N is the size of the
    reference sequence". For human that is ~87 GB, and the estimate must land
    in that neighbourhood rather than an order of magnitude under it."""
    mb = _index_only_mb(Aligner.BWA_MEM2, HUMAN_BASES)
    gb = mb / 1024
    assert 75 < gb < 100, f"expected ~87 GB for a human bwa-mem2 build, got {gb:.1f} GB"


def test_bwa_mem2_build_covers_the_reference_that_was_oom_killed():
    """The regression this issue is actually about.

    ~897 Mbp needs ~23.4 GB by the 28N figure. The old model said 7.8 GB, so
    the governor admitted the job and it was killed. Anything below the real
    requirement here means the same silent admission.
    """
    mb = _index_only_mb(Aligner.BWA_MEM2, ISSUE_96_BASES)
    required_gb = ISSUE_96_BASES * 28 / 1024**3
    assert mb / 1024 >= required_gb * 0.9, (
        f"estimate {mb / 1024:.1f} GB is under the ~{required_gb:.1f} GB "
        f"bwa-mem2 documents for this build"
    )


def test_bwa_mem2_alignment_still_matches_its_resident_index_size():
    """The build coefficient must not be paid at alignment time.

    bwa-mem2's README puts the resident index at ~10 GB for human after the
    2020 compression change. Raising the *build* estimate by moving the shared
    per-base term instead of the multiplier would inflate this too, and every
    alignment would then over-reserve by ~8x.
    """
    mb = est.estimate_mb(
        aligner=Aligner.BWA_MEM2,
        reference_bases=HUMAN_BASES,
        threads=1,
        sort_memory_mb=0,
        building_index=False,
    )
    gb = mb / 1024
    assert 8 < gb < 14, f"expected ~10 GB resident for human, got {gb:.1f} GB"


@pytest.mark.parametrize("aligner", list(Aligner))
def test_every_aligner_builds_at_least_as_dearly_as_it_loads(aligner):
    """No aligner may model its build as cheaper than loading the result.

    A multiplier below 1.0 would mean exactly that, and would be a typo rather
    than a considered value.
    """
    assert spec_for(aligner).memory_model.index_build_multiplier >= 1.0


@pytest.mark.parametrize("aligner", list(Aligner))
def test_no_aligner_index_estimate_is_implausibly_small(aligner):
    """A units error or a dropped multiplier shows up here for any aligner.

    Every aligner supported here needs more than a gigabyte to build a human
    index; an estimate under that means the coefficient is wrong in kind, not
    merely imprecise. Deliberately loose -- this catches the STAR-shaped
    failure where a new entry is added with a plausible-looking but wrong
    value, not a coefficient that is off by 30%.
    """
    mb = _index_only_mb(aligner, HUMAN_BASES)
    assert mb > 1024, f"{aligner.value} human index build estimated at only {mb} MB"
