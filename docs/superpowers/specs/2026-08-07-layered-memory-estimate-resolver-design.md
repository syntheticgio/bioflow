# Layered memory estimate resolver with source provenance

Design for issue [#69](https://github.com/syntheticgio/bioflow/issues/69),
child of epic [#7](https://github.com/syntheticgio/bioflow/issues/7).
Written 2026-08-07.

Follows [`2026-08-07-resource-limits-admission-design.md`](2026-08-07-resource-limits-admission-design.md),
which set the layer order and the requirement that the estimate report its
source. This design settles the four questions that one left open: how the
measured and heuristic layers reconcile when they disagree about *what they are
even keyed by*, which call sites the resolver feeds, where extrapolation stops
being trustworthy, and what "no opinion" looks like as a return value.

## The two models do not speak the same language

The measured layer is `timing_service.estimate_memory(job_type, input_bytes)`.
It knows a job type and an input size.

The heuristic layer is `resource_estimator.estimate_mb(aligner, reference_bases,
threads, sort_memory_mb, building_index)`. It knows the structure of the run.

That gap is not cosmetic. **Threads and sort buffer are exactly the levers the
auto re-plan algorithm ([#71](https://github.com/syntheticgio/bioflow/issues/71))
tunes** — `sort_memory_mb` is per thread, so it multiplies, and 8 threads at
1 GB versus 4 threads at 512 MB is a multi-gigabyte swing in the heuristic and
*identical* in the measured model. A measured estimate that simply wins is
structurally blind to the thing the next feature needs to predict.

**Decision: measured wins outright on the total, and the heuristic remains the
only re-plan model.** #71 keeps calling `resource_estimator` directly to search
thread counts and reports its proposal in heuristic terms.

The alternative considered was calibrating the heuristic with a per-job-type
correction factor fit from observed runs, keeping the structured terms live so
re-plan operates on calibrated numbers. It is the better end state and it is
not affordable yet: a correction factor is only meaningful if thread counts in
the history actually vary, and every row in this database today is test data.
Segmenting the measured model by thread count first
([#8](https://github.com/syntheticgio/bioflow/issues/8)) is the other route,
and it needs far more rows per job type because it partitions the sample set.

The cost of this decision is that a future refusal card can show two numbers
from two models — "estimated 14 GB from 23 runs" beside "4 threads → 7 GB" from
coefficients. That is #71's to resolve, and it can resolve it by sourcing both
sides of any delta it displays from the heuristic.

## What the resolver is

New module `backend/app/services/memory_estimate.py`. One public entry point:

```python
async def resolve(
    *,
    job_type: str,
    input_bytes: int | None,
    heuristic_mb: int | None,
) -> MemoryEstimate
```

**The caller computes its own heuristic and passes it in.** Only the caller
knows whether this is an alignment (`estimate_mb`, which needs the aligner and
the run's structure) or an assembly (`estimate_assembly_mb`, which may honestly
return `None`). The resolver never learns about aligners; it arbitrates between
a number the caller derived structurally and a number fit from this machine's
history, and reports which won.

This is what keeps the resolver out of the pipeline domain and leaves
`resource_estimator`'s two genuinely different signatures where they already
work.

## The return type

```python
class EstimateSource(StrEnum):
    MEASURED = "measured"
    HEURISTIC = "heuristic"
    DECLARED = "declared"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MemoryEstimate:
    mb: int | None                        # None if and only if source is UNKNOWN
    source: EstimateSource
    detail: str                           # user-facing provenance clause
    samples: int | None = None
    r_squared: float | None = None
    fell_back_from_measured: bool = False
```

`detail` is a clause, not a sentence — `"from 23 previous runs on this machine"`,
`"from published tool coefficients"` — because call sites compose it into prose
they already own.

**`UNKNOWN` is a real answer, not a failure.** This follows
`estimate_assembly_mb`'s existing contract, whose docstring is emphatic on the
point: de novo assembly is what you do when there is no reference, so a project
that cannot supply a genome size is the normal case rather than a misconfigured
one. A caller seeing `UNKNOWN` must let the run proceed.

Making it a distinct source rather than returning `None` keeps "we have no idea"
as a named state inside the resolver instead of something each of four call
sites reconstructs for itself.

**`fell_back_from_measured` records that a measured estimate existed and was
rejected.** Without it the graduation path is invisible from outside: a job type
with plenty of history that still reports `HEURISTIC` would look identical to
one with no history at all, and the guard below would be untestable through the
public interface.

## Resolution order

Measured wins if **all** of these hold:

- `estimate_memory()` returned `known: True`
- not extrapolating, or `factor_beyond <= MAX_EXTRAPOLATION_FACTOR`
- `r_squared >= MIN_R_SQUARED`

Otherwise the heuristic, when it is not `None`. Otherwise `UNKNOWN`.

Two constants, beside `MIN_SAMPLES` in `timing_service.py`:

```python
MAX_EXTRAPOLATION_FACTOR = 2.0
MIN_R_SQUARED = 0.5
```

Both are judgment calls rather than derived values, and are commented as such —
the same honesty `resource_estimator`'s module docstring already practices about
its own coefficients.

**Why both conditions and not just the extrapolation factor.** `_fit_memory()`
is a least-squares line with a flat-model fallback, and memory is often
genuinely flat in input size because the reference or index dominates. A flat
model extrapolates fine: if peak RSS is 14 GB regardless of input, asking about
a 10x larger input still yields 14 GB, which is right. A model with a real slope
fit on five small test runs, extrapolated 10x, produces a confidently wrong
number. Both cases carry the same `factor_beyond`. The `r_squared` gate is what
separates them, it is already computed and returned, and a scattered five-point
fit is precisely the failure the admission design warned about when it noted
that every row in this database today is test data.

## `DECLARED` is not reachable from `resolve()`

The declared numbers are per-handler constants in `@handler(resources=...)`
decorators — `align_handlers.py:163` declares `mem_mb=8192`, and so on. They are
not per-job facts, and they are already what a job gets when nobody calls the
resolver at all.

A `DECLARED` branch inside `resolve()` would mean inventing a `job_type` →
handler-constant lookup purely to return a number the enqueue path already
holds. Worse, `JobResources.mem_mb` defaults to 256, which is a default and not
an estimate of anything: treating it as an answer would let an assembly with no
genome size resolve to "256 MB, source: declared" and sail through a BLOCK check
that should have stayed silent — strictly worse than today, where no estimate
means no refusal.

The enum member stays so the API surface can report what a job was actually
reserved with. `resolve()` returns `MEASURED`, `HEURISTIC`, or `UNKNOWN`.

## The four call sites

The admission design named two ("the dialog and the enqueue check"). There are
four consumers of a memory number, and one of them is not advisory.

### 1. `declared_align_mem_mb` (`pipeline_service.py:1239`) — the reservation

This is the one that changes admission behavior rather than displayed text: its
return value becomes `JobResources.mem_mb`, which is what `claim.lua` gates on.

It becomes `async` and consults the resolver, passing in the heuristic it
already computes. The `MIN_DECLARED_MEM_MB` floor stays and now also catches
`UNKNOWN`. Both callers (`:1347` for index builds, `:1605` for alignment) are
already in async functions, so this is an `await` rather than a refactor.

**The `building_index=True/False` difference between this site and the BLOCK
check is deliberate and must survive.** The reservation at `:1605` passes
`building_index=False` on purpose — the comment there explains it would
otherwise reserve bowtie2's 3x and HISAT2's 4x build multiplier for every
alignment against a not-yet-indexed reference. Taking `heuristic_mb` as a
parameter is what lets each caller keep its own structural inputs.

Including this site is the point of the feature. `declared_align_mem_mb`'s own
docstring records that the flat 8 GB it replaced was "wrong in both directions";
a measurement from this machine is strictly better evidence than a published
coefficient, and leaving reservations on the heuristic would mean the system
never actually improves at the thing epic #7 exists for.

The tempting wrong answer here is reserving `max(measured, heuristic)`. It
sounds safe and it permanently discards the measured layer's main benefit, since
the heuristic's known failure mode is over-estimating on hardware it was not
tuned for. That builds the graduation path and pins it shut. The extrapolation
and `r_squared` guards plus `MIN_DECLARED_MEM_MB` already cover the dangerous
direction.

### 2. Align BLOCK check (`pipeline_service.py:1437`)

Resolves, then classifies the resolved number instead of the raw heuristic.

`explain()` gains an optional `provenance: str = ""` parameter rather than being
rewritten. It stays the heuristic's own explainer, because when the measured
layer wins there is no sort-buffer breakdown to give — the measured model does
not have one. So a `MEASURED` refusal reports the number and "from N previous
runs"; the "which slider to move" half appears only when the heuristic is what
is being reported, which is exactly when it is true.

### 3. Assembly BLOCK check (`:3216`) and reservation (`:3277`)

Today `if estimate is not None` gates the check, and the reservation is
`mem_mb=estimate or 16384` — an unexplained magic number sitting exactly where
`UNKNOWN` belongs. After: `UNKNOWN` gates the check (behaviour identical to
today when there is no genome size), and the bare `16384` becomes a named
constant with the comment it never had.

One genuinely new capability falls out: a measured estimate can make an assembly
refusable *even when genome size is missing*. That is correct. History about
this job type is real evidence, where a missing genome size was merely absent
evidence.

### 4. `api/v1/jobs.py:256` — the read-only surface

Returns the full `MemoryEstimate` rather than the raw `estimate_memory()` dict,
so the surface that already exists reports provenance too. This is the only site
with a shape change visible to the frontend, and nothing consumes it today, so
it is free.

## Testing

Per CLAUDE.md's warning about fixtures that already look the way the code
expects, the tests that carry weight are the ones asserting the *falling-back*
direction — the passing direction proves nothing here, the same trap recorded
for tool-availability tests.

- 5+ rows, good `r_squared`, in-range input → `MEASURED`. (The passing
  direction, included for completeness.)
- Same history, input 3x beyond the observed range → `HEURISTIC` with
  `fell_back_from_measured=True`. **This is the test that fails if the guard is
  wrong**, and it is the admission design's "five small test runs would
  confidently refuse the first real one" scenario stated as an assertion.
- Same history, `r_squared` 0.2 → `HEURISTIC` with `fell_back_from_measured=True`.
- Assembly, no genome size, no history → `UNKNOWN`, and the BLOCK check does not
  refuse.
- Assembly, no genome size, usable history → `MEASURED`, and refusal is now
  possible.
- `declared_align_mem_mb` never returns below `MIN_DECLARED_MEM_MB` whatever the
  source.

Plus the check CLAUDE.md requires against real rows rather than fixtures:

```bash
docker compose exec api python -c "..."
```

resolving against actual `job_timings` rows to see what today's data produces.
Because every current row is test data, the expected and correct outcome is that
most job types resolve to `HEURISTIC`. **If any job type resolves to `MEASURED`
off five tiny rows, the guard is too loose** — and that is found before it ships
rather than after.

Backend tests run from a worktree via:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

## Out of scope

The re-plan search (#71), the four-choice refusal card
([#70](https://github.com/syntheticgio/bioflow/issues/70)), thread segmentation
of the timing model (#8), and cgroup enforcement
([#72](https://github.com/syntheticgio/bioflow/issues/72)).

Per the decision at the top, `resource_estimator` remains the only thread-aware
model and the resolver is not in #71's search loop.
