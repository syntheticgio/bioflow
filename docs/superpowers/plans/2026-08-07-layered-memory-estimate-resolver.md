# Layered Memory Estimate Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One resolver that arbitrates between the measured memory model and the heuristic estimator, reports which source it used, and feeds all four call sites that consume a memory number.

**Architecture:** A new pure-ish service module `app/services/memory_estimate.py` exposes `resolve(job_type, input_bytes, heuristic_mb) -> MemoryEstimate`. Callers compute their own heuristic (only they know whether it's an alignment or an assembly) and pass it in, so the resolver never learns about aligners. Measured wins when it is confident — enough samples, not extrapolating too far, decent fit — otherwise the heuristic, otherwise `UNKNOWN`. Four call sites in `pipeline_service.py` and `api/v1/jobs.py` are rewired to it.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (MongoDB), pytest with `asyncio_mode = "auto"`.

**Spec:** [`docs/superpowers/specs/2026-08-07-layered-memory-estimate-resolver-design.md`](../specs/2026-08-07-layered-memory-estimate-resolver-design.md)

**Issue:** [#69](https://github.com/syntheticgio/bioflow/issues/69)

---

## Critical context for the implementer

Read this before Task 1. Four things about this codebase will otherwise cost you a debugging session each.

**Tests run through a script, not bare pytest.** You are in a git worktree. Running `docker compose exec api python -m pytest` here silently tests *main's* code, because the `api` container bind-mounts the main checkout. Always use:

```bash
./backend/run-worktree-tests.sh tests/path -q
```

It starts a throwaway container mounting *this* worktree plus a private Mongo replica set. The private Mongo matters: `conftest.py` drops every collection in `biopipe_test` at session start, so sharing Mongo with the running stack makes unrelated tests fail at random.

**`estimate_memory()` nests its extrapolation data.** `extrapolating` and `factor_beyond` are under `result["range"]`, not at the top level. `range` is present only when `known` is `True`. Getting this wrong produces a resolver that never falls back and no test failure that explains why.

**`factor_beyond` can be `None` while `extrapolating` is `True`.** That happens when every observed sample had size zero — there is no ratio to report, but the input is still outside the observed range. Treat that case as "do not trust the measurement."

**`_fit_memory()` returns `None` below `MIN_SAMPLES` (5).** So `known: False` covers both "no rows at all" and "too few rows," and the resolver does not need to count samples itself.

---

## File structure

**Create:**
- `backend/app/services/memory_estimate.py` — the resolver, `EstimateSource`, `MemoryEstimate`. One responsibility: pick a layer and say which. No pipeline domain knowledge.
- `backend/tests/services/test_memory_estimate.py` — resolver unit tests. Pure where possible; the async entry point goes through real Mongo like `TestEstimateMemory` does.

**Modify:**
- `backend/app/services/timing_service.py:33` — add two guard constants beside `MIN_SAMPLES`.
- `backend/app/pipelines/resource_estimator.py:128` — `explain()` gains an optional `provenance` parameter.
- `backend/app/services/pipeline_service.py:1239` — `declared_align_mem_mb` becomes async, consults the resolver.
- `backend/app/services/pipeline_service.py:1437` — align BLOCK check classifies the resolved number.
- `backend/app/services/pipeline_service.py:3216` — assembly BLOCK check gates on `UNKNOWN`.
- `backend/app/services/pipeline_service.py:3277` — magic `16384` becomes a named constant.
- `backend/app/api/v1/jobs.py:256` — returns the resolved estimate with provenance.

---

## Task 1: Guard constants

**Files:**
- Modify: `backend/app/services/timing_service.py:33`

- [ ] **Step 1: Add the constants**

In `backend/app/services/timing_service.py`, directly after the `MIN_SAMPLES` block (line 33) and before `MAX_SAMPLES`, insert:

```python
# Guards on trusting the measured model over the heuristic. Both are judgment
# calls rather than derived values -- the same honesty resource_estimator's
# module docstring practices about its own coefficients.
#
# A pure extrapolation check is not enough on its own. `_fit_memory` falls back
# to a flat model, and memory genuinely is flat in input size for many tools
# (the reference or index dominates). A flat model extrapolates fine: 14 GB
# regardless of input is still 14 GB at 10x the input. A model with a real
# slope, fit on five scattered test rows, does not -- and both report the same
# factor_beyond. r_squared is what separates them.
MAX_EXTRAPOLATION_FACTOR = 2.0
MIN_R_SQUARED = 0.5
```

- [ ] **Step 2: Verify the module still imports**

Run: `./backend/run-worktree-tests.sh tests/storage/test_memory_model.py -q`
Expected: PASS, same count as before your change (the constants are not yet used).

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/timing_service.py
git commit -m "feat: add measured-model trust guards for the estimate resolver"
```

---

## Task 2: The resolver's types and the UNKNOWN case

TDD from the simplest end: a resolver with nothing to go on.

**Files:**
- Create: `backend/app/services/memory_estimate.py`
- Create: `backend/tests/services/test_memory_estimate.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_memory_estimate.py`:

```python
"""The layered memory estimate resolver.

Tests assert the *falling-back* direction wherever a guard is involved. Per
CLAUDE.md, the passing direction proves nothing here: the resolver returns a
number in almost every arrangement, so a test that only checks "we got an
estimate" passes whether or not the guard it claims to test is wired up.
"""

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.timing import JobRunTiming, RunOutcome, RunResources
from app.services import memory_estimate
from app.services.memory_estimate import EstimateSource


@pytest.fixture(autouse=True)
async def _init_beanie_models():
    """Function-scoped Beanie init, and drops `job_timings` on entry.

    Same reasoning as `tests/storage/test_memory_model.py`: pytest-asyncio
    hands each async test its own event loop, so a wider-scoped Motor client
    ends up bound to the wrong loop; and these tests assert on exact
    resolution outcomes, which leftover rows from an earlier test would
    corrupt.
    """
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    await init_beanie(database=client[settings.mongo_db], document_models=ALL_MODELS)
    await JobRunTiming.get_motor_collection().drop()
    yield
    client.close()


class TestUnknown:
    async def test_no_history_and_no_heuristic_is_unknown(self):
        """The assembly-without-genome-size case. `estimate_assembly_mb`
        returns None on purpose there, and the caller must let the run
        proceed rather than refuse -- so the resolver must not invent a
        number."""
        result = await memory_estimate.resolve(
            job_type="never_seen_job",
            input_bytes=1_000_000,
            heuristic_mb=None,
        )
        assert result.source is EstimateSource.UNKNOWN
        assert result.mb is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_memory_estimate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.memory_estimate'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/memory_estimate.py`:

```python
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
    if heuristic_mb is not None:
        return MemoryEstimate(
            mb=heuristic_mb,
            source=EstimateSource.HEURISTIC,
            detail="from published tool coefficients",
        )

    return MemoryEstimate(mb=None, source=EstimateSource.UNKNOWN, detail="")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_memory_estimate.py -q`
Expected: PASS, 1 test.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/memory_estimate.py backend/tests/services/test_memory_estimate.py
git commit -m "feat: add MemoryEstimate types and the UNKNOWN resolution case"
```

---

## Task 3: Heuristic beats nothing; measured beats heuristic

**Files:**
- Modify: `backend/app/services/memory_estimate.py`
- Modify: `backend/tests/services/test_memory_estimate.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/services/test_memory_estimate.py`:

```python
async def _insert_runs(job_type: str, count: int = 8, *, peak_base: int = 10_000_000):
    """A well-behaved history: a clean linear relationship, all succeeded.

    Returns the largest input size inserted, so callers can ask about an input
    inside or beyond the observed range without recomputing it.
    """
    for i in range(1, count + 1):
        await JobRunTiming(
            job_type=job_type,
            input_bytes=1_000_000 * i,
            duration_ms=120_000,
            outcome=RunOutcome.SUCCEEDED,
            resources=RunResources(peak_rss_bytes=peak_base + 1_000_000 * i),
        ).insert()
    return 1_000_000 * count


class TestHeuristic:
    async def test_heuristic_is_used_when_there_is_no_history(self):
        result = await memory_estimate.resolve(
            job_type="never_seen_job",
            input_bytes=1_000_000,
            heuristic_mb=4096,
        )
        assert result.source is EstimateSource.HEURISTIC
        assert result.mb == 4096
        assert result.fell_back_from_measured is False


class TestMeasured:
    async def test_measured_wins_over_the_heuristic_in_range(self):
        """The graduation the whole feature exists for: once a job type has
        real history on this machine, coefficients stop being the answer."""
        await _insert_runs("measured_win_job")

        result = await memory_estimate.resolve(
            job_type="measured_win_job",
            input_bytes=5_000_000,
            heuristic_mb=99_999,
        )

        assert result.source is EstimateSource.MEASURED
        assert result.mb != 99_999
        assert result.mb > 0
        assert result.samples == 8
        assert "previous runs" in result.detail

    async def test_measured_reports_megabytes_not_bytes(self):
        """`estimate_memory` returns bytes; every caller of this resolver
        works in MB. A unit mismatch here would be a 1,048,576x error that
        still looks like a plausible integer."""
        await _insert_runs("measured_units_job", peak_base=2 * 1024**3)

        result = await memory_estimate.resolve(
            job_type="measured_units_job",
            input_bytes=5_000_000,
            heuristic_mb=None,
        )

        assert result.source is EstimateSource.MEASURED
        # ~2 GB of peak RSS is ~2048 MB, not ~2.1 billion.
        assert 1500 < result.mb < 3000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_memory_estimate.py -q`
Expected: FAIL — `TestHeuristic` passes, both `TestMeasured` tests fail with `AssertionError` on `result.source is EstimateSource.MEASURED` (the resolver still returns HEURISTIC).

- [ ] **Step 3: Write the implementation**

In `backend/app/services/memory_estimate.py`, add the import at the top of the import block:

```python
from app.services import timing_service
```

Then replace the body of `resolve()` (everything after its docstring) with:

```python
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
```

And add this module-level helper above `resolve()`:

```python
def _is_trustworthy(measured: dict) -> bool:
    """Whether a known measured estimate should outrank the heuristic.

    Placeholder until Task 4 wires the guards -- a known estimate is trusted.
    """
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_memory_estimate.py -q`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/memory_estimate.py backend/tests/services/test_memory_estimate.py
git commit -m "feat: prefer the measured model over the heuristic when history exists"
```

---

## Task 4: The trust guards

This is the task that matters most. Both tests assert the *falling-back* direction, which is the direction that fails if the guard is not wired up.

**Files:**
- Modify: `backend/app/services/memory_estimate.py`
- Modify: `backend/tests/services/test_memory_estimate.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/services/test_memory_estimate.py`:

```python
class TestGuards:
    """The falling-back direction. Per CLAUDE.md's note on tool-availability
    tests, asserting that a well-behaved case resolves to MEASURED passes
    whether or not these guards exist -- only the rejections prove them."""

    async def test_far_extrapolation_falls_back_to_the_heuristic(self):
        """The admission design's core warning: every row in this database
        today is test data, so without this guard five small runs would
        confidently refuse the first real one."""
        largest = await _insert_runs("extrapolation_job")

        result = await memory_estimate.resolve(
            job_type="extrapolation_job",
            input_bytes=largest * 10,
            heuristic_mb=4096,
        )

        assert result.source is EstimateSource.HEURISTIC
        assert result.mb == 4096
        assert result.fell_back_from_measured is True

    async def test_a_poor_fit_falls_back_to_the_heuristic(self):
        """Scattered peaks with no relationship to input size. factor_beyond
        alone would not catch this -- the input is inside the observed range,
        so only r_squared can reject it."""
        for i, peak in enumerate(
            [5_000_000, 900_000_000, 12_000_000, 700_000_000,
             30_000_000, 850_000_000, 8_000_000, 640_000_000],
            start=1,
        ):
            await JobRunTiming(
                job_type="noisy_fit_job",
                input_bytes=1_000_000 * i,
                duration_ms=120_000,
                outcome=RunOutcome.SUCCEEDED,
                resources=RunResources(peak_rss_bytes=peak),
            ).insert()

        result = await memory_estimate.resolve(
            job_type="noisy_fit_job",
            input_bytes=4_000_000,
            heuristic_mb=4096,
        )

        assert result.source is EstimateSource.HEURISTIC
        assert result.fell_back_from_measured is True

    async def test_a_rejected_measurement_with_no_heuristic_is_unknown(self):
        """Falling back needs somewhere to fall. An assembly with no genome
        size and untrustworthy history is UNKNOWN, not a guess."""
        largest = await _insert_runs("rejected_no_heuristic_job")

        result = await memory_estimate.resolve(
            job_type="rejected_no_heuristic_job",
            input_bytes=largest * 10,
            heuristic_mb=None,
        )

        assert result.source is EstimateSource.UNKNOWN
        assert result.mb is None
        assert result.fell_back_from_measured is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_memory_estimate.py -q`
Expected: FAIL, 3 failures — each asserting `HEURISTIC`/`UNKNOWN` but getting `MEASURED`, because `_is_trustworthy` still returns `True` unconditionally.

- [ ] **Step 3: Write the implementation**

In `backend/app/services/memory_estimate.py`, replace `_is_trustworthy()` entirely with:

```python
def _is_trustworthy(measured: dict) -> bool:
    """Whether a known measured estimate should outrank the heuristic.

    Two independent ways a fit can be real and still not worth believing, and
    neither subsumes the other:

      * **Extrapolated too far.** The slope compounds past the largest input
        actually observed.
      * **A poor fit.** Scattered points still produce a line. This is the one
        an extrapolation check cannot see, because a bad fit inside the
        observed range is not extrapolating at all.
    """
    quality = measured.get("r_squared")
    if quality is not None and quality < timing_service.MIN_R_SQUARED:
        return False

    # `range` is present only when `known` is True, which the caller checked.
    observed = measured.get("range") or {}
    if observed.get("extrapolating"):
        factor = observed.get("factor_beyond")
        # None with extrapolating=True means every observed sample was
        # zero-sized: there is no ratio to report, but the input is still
        # outside what was measured. Not trustworthy.
        if factor is None or factor > timing_service.MAX_EXTRAPOLATION_FACTOR:
            return False

    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_memory_estimate.py -q`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/memory_estimate.py backend/tests/services/test_memory_estimate.py
git commit -m "feat: guard the measured model on extrapolation distance and fit quality"
```

---

## Task 5: `explain()` accepts a provenance clause

**Files:**
- Modify: `backend/app/pipelines/resource_estimator.py:128`
- Test: `backend/tests/pipelines/test_resource_estimator.py`

- [ ] **Step 1: Write the failing test**

Find the existing test file for the estimator:

```bash
ls backend/tests/pipelines/ | grep -i resource
```

Append to `backend/tests/pipelines/test_resource_estimator.py` (create the file with the imports below if it does not exist):

```python
class TestExplainProvenance:
    def test_provenance_clause_is_appended_when_given(self):
        """The user is told which model produced the number they are about to
        override. Without it, 'Estimated 14 GB' from coefficients and from 23
        measured runs are indistinguishable, and they justify very different
        confidence."""
        text = resource_estimator.explain(
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
        assert resource_estimator.explain(**kwargs) == resource_estimator.explain(
            **kwargs, provenance=""
        )
```

If you had to create the file, prepend:

```python
from app.pipelines import resource_estimator
from app.pipelines.aligners import Aligner
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_resource_estimator.py -q`
Expected: FAIL — `TypeError: explain() got an unexpected keyword argument 'provenance'`

- [ ] **Step 3: Write the implementation**

In `backend/app/pipelines/resource_estimator.py`, change the `explain()` signature (line 128) to add the parameter after `mem_budget_mb`:

```python
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
```

Extend its docstring with a paragraph before the closing `"""`:

```
    `provenance` names the model the number came from. This stays the
    *heuristic's* explainer: when the measured model wins there is no
    sort-buffer breakdown to give, because that model does not have one. So
    the "which slider to move" half below appears only when the heuristic is
    what is being reported -- which is exactly when it is true.
```

Then, immediately before the final `return " ".join(parts)`, insert:

```python
    if provenance:
        parts.append(f"Estimate {provenance}.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_resource_estimator.py -q`
Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/resource_estimator.py backend/tests/pipelines/test_resource_estimator.py
git commit -m "feat: let explain() name the model its number came from"
```

---

## Task 6: The alignment reservation consults the resolver

This is the site that changes admission behavior — its return value becomes `JobResources.mem_mb`, which `claim.lua` gates on.

**Files:**
- Modify: `backend/app/services/pipeline_service.py:1239` (`declared_align_mem_mb`)
- Modify: `backend/app/services/pipeline_service.py:1347`, `:1605` (its two callers)
- Test: `backend/tests/services/test_declared_align_mem.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_declared_align_mem.py`:

```python
"""The alignment reservation, which is what claim.lua gates on.

Distinct from the advisory sites: a wrong number here is silently costly in
both directions -- too low over-admits, too high starves the queue.
"""

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.timing import JobRunTiming, RunOutcome, RunResources
from app.pipelines.aligners import Aligner
from app.services import pipeline_service
from app.services.pipeline_service import MIN_DECLARED_MEM_MB


@pytest.fixture(autouse=True)
async def _init_beanie_models():
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    await init_beanie(database=client[settings.mongo_db], document_models=ALL_MODELS)
    await JobRunTiming.get_motor_collection().drop()
    yield
    client.close()


class TestDeclaredAlignMem:
    async def test_floor_holds_when_the_measured_model_predicts_almost_nothing(self):
        """A reference whose size is missing, or a measured model fit on tiny
        runs, would otherwise reserve almost nothing and let the governor admit
        the job alongside everything else -- the exact reason the floor exists."""
        for i in range(1, 9):
            await JobRunTiming(
                job_type="align_reads",
                input_bytes=1_000_000 * i,
                duration_ms=120_000,
                outcome=RunOutcome.SUCCEEDED,
                resources=RunResources(peak_rss_bytes=1_000_000 + 1000 * i),
            ).insert()

        mem_mb = await pipeline_service.declared_align_mem_mb(
            aligner=Aligner.BWA_MEM2,
            reference_bases=0,
            threads=4,
            sort_memory_mb=256,
            building_index=False,
            input_bytes=4_000_000,
        )

        assert mem_mb >= MIN_DECLARED_MEM_MB

    async def test_no_history_still_reserves_the_heuristic_number(self):
        """Behaviour before this change, preserved: with no rows, the
        coefficients are still the reservation."""
        mem_mb = await pipeline_service.declared_align_mem_mb(
            aligner=Aligner.BWA_MEM2,
            reference_bases=3_000_000_000,
            threads=8,
            sort_memory_mb=1024,
            building_index=False,
            input_bytes=None,
        )

        assert mem_mb > MIN_DECLARED_MEM_MB
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_declared_align_mem.py -q`
Expected: FAIL — `TypeError: declared_align_mem_mb() got an unexpected keyword argument 'input_bytes'`, and it is not awaitable.

- [ ] **Step 3: Write the implementation**

In `backend/app/services/pipeline_service.py`, change `declared_align_mem_mb` (line 1239) to:

```python
async def declared_align_mem_mb(
    *,
    aligner: Aligner,
    reference_bases: int,
    threads: int,
    sort_memory_mb: int,
    building_index: bool,
    input_bytes: int | None = None,
) -> int:
```

Keep the entire existing docstring, and add this paragraph before its closing `"""`:

```
    The number is resolved through `memory_estimate.resolve`, so once a job
    type has enough trustworthy history on this machine the reservation stops
    being a published coefficient and becomes a measurement. `input_bytes` is
    what makes that possible; without it only the heuristic can answer.

    The floor applies whatever the source, including UNKNOWN.
```

Replace the function body (currently the `estimate = ...` call and the `return max(...)`) with:

```python
    heuristic_mb = resource_estimator.estimate_mb(
        aligner=aligner,
        reference_bases=reference_bases,
        threads=threads,
        sort_memory_mb=sort_memory_mb,
        building_index=building_index,
    )
    resolved = await memory_estimate.resolve(
        job_type=JOB_TYPE_ALIGN_READS,
        input_bytes=input_bytes,
        heuristic_mb=heuristic_mb,
    )
    return max(resolved.mb or 0, MIN_DECLARED_MEM_MB)
```

Add to the imports at the top of `pipeline_service.py`, alongside the other `app.services` imports:

```python
from app.services import memory_estimate
```

The job types are bare string literals in the `@handler` decorators
(`align_handlers.py:307` registers `"align_reads"`,
`assembly_handlers.py:45` registers `"assemble_reads"`), and no `JOB_TYPE`
constants exist anywhere in the codebase yet. Define both near
`MIN_DECLARED_MEM_MB` (line 1231):

```python
# The job types whose memory the resolver arbitrates. These must match the
# strings the handlers register under (align_handlers.py:307,
# assembly_handlers.py:45) -- a typo here resolves against an empty history
# and silently falls back to the heuristic forever, with nothing failing.
JOB_TYPE_ALIGN_READS = "align_reads"
JOB_TYPE_ASSEMBLE = "assemble_reads"
```

- [ ] **Step 4: Update the two callers**

At `pipeline_service.py:1347` (index build) the call is inside `JobResources(...)`. Index builds have no read input, so pass nothing new — but the call must now be awaited. Extract it to a local above the `queue.enqueue(...)` call:

```python
    mem_mb = await declared_align_mem_mb(
        aligner=aligner,
        reference_bases=reference.size or 0,
        threads=INDEX_BUILD_THREADS,
        sort_memory_mb=0,
        building_index=True,
    )
```

then use `mem_mb=mem_mb` inside `JobResources(...)`. Check the existing arguments at that site first with `sed -n '1330,1352p' backend/app/services/pipeline_service.py` and preserve them exactly — only the `await` and the extraction change.

At `:1605` the call is nested inside `JobResources(...)` within an `enqueue` call. Extract it the same way, to a local immediately above, and pass the read object's size as `input_bytes`:

```python
    align_mem_mb = await declared_align_mem_mb(
        aligner=aligner,
        reference_bases=reference.size or 0,
        threads=align_params.threads,
        sort_memory_mb=align_params.sort_memory_mb,
        building_index=False,
        input_bytes=obj.size or None,
    )
```

**Preserve `building_index=False` at this site.** The comment above it explains why: passing `True` would reserve bowtie2's 3x and HISAT2's 4x build multiplier for every alignment against a not-yet-indexed reference.

Confirm the read object's variable name at that site before using `obj`:

```bash
sed -n '1590,1620p' backend/app/services/pipeline_service.py
```

- [ ] **Step 5: Run the tests**

Run: `./backend/run-worktree-tests.sh tests/services/test_declared_align_mem.py -q`
Expected: PASS, 2 tests.

Then the alignment suite, to catch any caller you missed:

Run: `./backend/run-worktree-tests.sh tests/services -q -k align`
Expected: PASS. A `RuntimeWarning: coroutine ... was never awaited` or a `TypeError` about `int` and `coroutine` means a third call site exists — find it with `grep -rn "declared_align_mem_mb" backend/`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_declared_align_mem.py
git commit -m "feat: resolve the alignment reservation through the layered estimate"
```

---

## Task 7: The alignment BLOCK check reports provenance

**Files:**
- Modify: `backend/app/services/pipeline_service.py:1437`

- [ ] **Step 1: Write the implementation**

At `pipeline_service.py:1437`, the existing block computes `estimate`, calls `classify`, and raises with `explain`. Replace from the `estimate = resource_estimator.estimate_mb(` line through the end of the `raise ValidationError(...)` block with:

```python
    heuristic_mb = resource_estimator.estimate_mb(
        aligner=aligner,
        reference_bases=reference.size or 0,
        threads=align_params.threads,
        sort_memory_mb=align_params.sort_memory_mb,
        building_index=building,
    )
    resolved = await memory_estimate.resolve(
        job_type=JOB_TYPE_ALIGN_READS,
        input_bytes=obj.size or None,
        heuristic_mb=heuristic_mb,
    )
    estimate = resolved.mb
    # UNKNOWN cannot arise here (the heuristic always answers for an
    # alignment), but classifying None would be a crash rather than a refusal.
    if estimate is not None:
        band = resource_estimator.classify(
            estimated_mb=estimate,
            mem_budget_mb=mem_budget_mb,
            threads=align_params.threads,
            cpu_budget=governor.cpu_budget(),
        )
        if band is resource_estimator.Band.BLOCK:
            raise ValidationError(
                resource_estimator.explain(
                    aligner=aligner,
                    reference_bases=reference.size or 0,
                    threads=align_params.threads,
                    sort_memory_mb=align_params.sort_memory_mb,
                    building_index=building,
                    mem_budget_mb=mem_budget_mb,
                    provenance=resolved.detail,
                ),
                details={
                    "estimate_mb": estimate,
                    "budget_mb": mem_budget_mb,
                    "estimate_source": resolved.source.value,
                },
            )
```

Confirm the read object's variable name at this site before using `obj`:

```bash
sed -n '1400,1440p' backend/app/services/pipeline_service.py
```

- [ ] **Step 2: Run the alignment tests**

Run: `./backend/run-worktree-tests.sh tests/services -q -k align`
Expected: PASS. Any test asserting on the exact BLOCK message text may need its expected string extended with the provenance clause — update the expectation, since the added sentence is the feature.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/pipeline_service.py
git commit -m "feat: name the estimate source in the alignment refusal message"
```

---

## Task 8: The assembly path

Two changes at once because they are the same decision: `UNKNOWN` replaces `estimate is None` as the "say nothing" signal, and the magic `16384` gets a name.

**Files:**
- Modify: `backend/app/services/pipeline_service.py:3216` (BLOCK check), `:3277` (reservation)
- Test: `backend/tests/services/test_memory_estimate.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_memory_estimate.py`:

```python
class TestAssemblyShape:
    async def test_history_answers_even_when_the_heuristic_declines(self):
        """New capability, and correct: `estimate_assembly_mb` returns None
        when there is no genome size, but history about this job type is real
        evidence where a missing genome size was merely absent evidence. So an
        assembly with no genome size becomes refusable once it has runs."""
        await _insert_runs("assemble_reads", peak_base=8 * 1024**3)

        result = await memory_estimate.resolve(
            job_type="assemble_reads",
            input_bytes=5_000_000,
            heuristic_mb=None,
        )

        assert result.source is EstimateSource.MEASURED
        assert result.mb > 0
```

- [ ] **Step 2: Run test to verify it passes already**

Run: `./backend/run-worktree-tests.sh tests/services/test_memory_estimate.py -q`
Expected: PASS, 8 tests. This one documents behaviour Task 4 already produced; it is here because it is the assembly path's contract and the next step depends on it.

- [ ] **Step 3: Write the implementation**

Add the named constant near `MIN_DECLARED_MEM_MB` (line 1231) in `pipeline_service.py`:

```python
# What to reserve for an assembly nothing can estimate. Assemblies are the
# heaviest thing this tool runs, and de novo assembly with no genome size is
# the normal case rather than a misconfigured one -- so this is deliberately
# generous. Reserving too little would let the governor admit an assembly
# alongside other work and drive the machine into swap.
UNKNOWN_ASSEMBLY_MEM_MB = 16384
```

At `:3216`, replace the `estimate = resource_estimator.estimate_assembly_mb(...)` call and the `if estimate is not None:` block that follows it with:

```python
    heuristic_mb = resource_estimator.estimate_assembly_mb(
        assembler=parsed.assembler,
        genome_bases=parsed.genome_size,
        threads=parsed.threads,
    )
    resolved = await memory_estimate.resolve(
        job_type=JOB_TYPE_ASSEMBLE,
        input_bytes=reads.size or None,
        heuristic_mb=heuristic_mb,
    )
    estimate = resolved.mb
    if estimate is not None:
        mem_budget_mb = int(LoadGovernor().mem_budget_bytes() / (1024 * 1024))
        band = resource_estimator.classify(
            estimated_mb=estimate,
            mem_budget_mb=mem_budget_mb,
            threads=parsed.threads,
            cpu_budget=None,
        )
        if band is resource_estimator.Band.BLOCK:
            raise ValidationError(
                f"This assembly needs about {estimate:,} MB "
                f"({resolved.detail}), more than the "
                f"{mem_budget_mb:,} MB available. Assembling a genome this "
                "size needs a bigger machine.",
                details={
                    "estimate_mb": estimate,
                    "budget_mb": mem_budget_mb,
                    "estimate_source": resolved.source.value,
                },
            )
```

At `:3277`, replace `mem_mb=estimate or 16384,` with:

```python
            mem_mb=estimate or UNKNOWN_ASSEMBLY_MEM_MB,
```

`JOB_TYPE_ASSEMBLE` was already defined in Task 6 alongside
`JOB_TYPE_ALIGN_READS`; no new constant is needed here.

- [ ] **Step 4: Run the assembly tests**

Run: `./backend/run-worktree-tests.sh tests/services -q -k assembl`
Expected: PASS. A test asserting the old refusal message text needs its expectation updated for the added `({resolved.detail})` clause.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_memory_estimate.py
git commit -m "feat: resolve assembly memory through the layered estimate"
```

---

## Task 9: The read-only API surface

**Files:**
- Modify: `backend/app/api/v1/jobs.py:256`

- [ ] **Step 1: Read the surrounding block**

Run: `sed -n '240,265p' backend/app/api/v1/jobs.py`

Note how `size` is derived and what `out` is — you need the same `size` value for `input_bytes`.

- [ ] **Step 2: Write the implementation**

Replace line 256:

```python
            out["memory_estimate"] = await timing_service.estimate_memory(job.type, size)
```

with:

```python
            # The raw model output stays, for the diagnostics view that shows
            # sample counts and fit quality. `resolution` is what the launch
            # dialog reads: the number actually used, and which layer produced
            # it. `heuristic_mb=None` because this endpoint has no run
            # structure to derive one from -- so an untrustworthy measurement
            # resolves to UNKNOWN here rather than silently to coefficients.
            out["memory_estimate"] = await timing_service.estimate_memory(job.type, size)
            resolved = await memory_estimate.resolve(
                job_type=job.type,
                input_bytes=size,
                heuristic_mb=None,
            )
            out["memory_estimate_resolution"] = {
                "mb": resolved.mb,
                "source": resolved.source.value,
                "detail": resolved.detail,
                "samples": resolved.samples,
                "r_squared": resolved.r_squared,
                "fell_back_from_measured": resolved.fell_back_from_measured,
            }
```

Add to the imports at the top of `jobs.py`:

```python
from app.services import memory_estimate
```

- [ ] **Step 3: Run the API tests**

Run: `./backend/run-worktree-tests.sh tests/api -q -k job`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/v1/jobs.py
git commit -m "feat: report estimate provenance on the job detail endpoint"
```

---

## Task 10: Full suite and the real-data check

CLAUDE.md requires checking a rule against the real database, not only its unit tests — the Actions-tab suggestion rules passed a full green suite while being wrong in two ways one look at real data exposed.

- [ ] **Step 1: Run the full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. **Read the count, not the exit code.** Note the number; compare it against a run on `main` if anything looks short.

- [ ] **Step 2: Check the resolver against real rows**

The main stack's database has the actual `job_timings` history. From the **main checkout root** (not this worktree):

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models.timing import JobRunTiming
from app.services import memory_estimate

async def main():
    await connect_to_mongo()
    types = await JobRunTiming.distinct('job_type')
    print(f'{len(types)} job types with history')
    for t in sorted(types):
        r = await memory_estimate.resolve(job_type=t, input_bytes=50_000_000_000, heuristic_mb=4096)
        print(f'  {t:35s} {r.source.value:10s} mb={r.mb} fellback={r.fell_back_from_measured} n={r.samples} r2={r.r_squared}')

asyncio.run(main())
"
```

- [ ] **Step 3: Interpret the result**

Every row in this database today is test data, and the input asked about above (50 GB) is far beyond anything measured.

**Expected and correct:** nearly every job type reports `heuristic` with `fellback=True`.

**A problem:** any job type reporting `measured` at that input size. That means the guards are too loose, and it is exactly the "five small test runs confidently refuse the first real one" failure the design exists to prevent. If you see it, check `r2` and the `factor_beyond` for that type before adjusting `MAX_EXTRAPOLATION_FACTOR` — and record what you found in the commit message.

- [ ] **Step 4: Commit any adjustment**

Only if Step 3 turned something up:

```bash
git add backend/app/services/timing_service.py
git commit -m "fix: tighten the measured-model guard after checking real job_timings rows"
```

---

## Task 11: Close out the issue

- [ ] **Step 1: Merge to main**

Per CLAUDE.md, once the suite is green and `main` is clean, merge without asking. From the main checkout root:

```bash
git checkout main && git pull && git merge --no-ff claude/epic-7-remaining-work-0b3552
```

If `main` moved, re-run the suite after merging rather than assuming the earlier green still holds.

- [ ] **Step 2: Push**

```bash
git push origin main
```

- [ ] **Step 3: Update the issue**

```bash
gh issue close 69 -R syntheticgio/bioflow -c "Implemented and merged. Resolver lives at \`backend/app/services/memory_estimate.py\`; all four call sites now consult it. Real-data check against \`job_timings\` recorded in the plan's Task 10."
```

Also flip the label off `status: implementation plan`:

```bash
gh issue edit 69 -R syntheticgio/bioflow --remove-label "status: implementation plan"
```

- [ ] **Step 4: Check whether a TODO entry closes**

```bash
grep -n -i "memory estimate\|resource limit" docs/TODO.md
```

If an entry covers this work, append ` — FIXED` to its heading, write a short note saying what shipped and where the code lives, say what the implementation did differently from its plan, and **move the whole entry to `docs/TODO-done.md`**. If the entry is only partially resolved, it stays in `docs/TODO.md`.

---

## Self-review notes

**Spec coverage:** Layer arbitration → Tasks 3–4. Provenance reporting → Tasks 2, 5, 7, 8, 9. `UNKNOWN` as a named source → Tasks 2, 8. `DECLARED` unreachable from `resolve()` → Task 2 (enum member present, no branch). All four call sites → Tasks 6 (reservation), 7 (align BLOCK), 8 (assembly BLOCK + reservation), 9 (API). Both guards → Task 4. Real-data check → Task 10.

**Deliberately deferred to the implementer, with the command to resolve each:** the read-object variable names at two `pipeline_service.py` sites (Tasks 6, 7), and the existing argument list at the index-build enqueue (Task 6, Step 4). These are single `sed` lookups whose answers this plan cannot state without guessing at code it has not read line-by-line — guessing would be worse than pointing at the lookup. The job-type strings *were* verified and are stated outright in Task 6.

**One task asserts behaviour rather than adding it:** Task 8 Step 2 expects its new test to pass immediately, because Task 4 already produced that behaviour. It is in the plan because it pins the assembly path's contract — that history can answer where the heuristic declines — which Step 3 then depends on. The step says so explicitly rather than letting a passing "failing test" look like a mistake.

**Out of scope, per the spec:** re-plan search (#71), refusal card (#70), thread segmentation (#8), cgroup enforcement (#72).
