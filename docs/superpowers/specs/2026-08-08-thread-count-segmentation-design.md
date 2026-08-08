# Segmenting timing models by thread count

Design for [#8](https://github.com/syntheticgio/bioflow/issues/8).
Written 2026-08-08.

## What this covers, and what it deliberately doesn't

The issue's acceptance criteria split into two groups. This spec covers only
the first:

- A documented minimum-sample rule for when a thread-count segment is fit.
- The bytes-only fallback for sparse or unknown-thread segments.
- All predictive readers going through the outcome-filtered accessor
  (already true today; this spec adds a regression test rather than new
  behavior).
- Automated tests covering segmentation and fallback logic.

It does **not** claim the other two:

- "Thread-segmented duration and memory fits use real computation rows."
- "Real-row verification... cover segmentation and fallback."

Checked the database behind this stack directly: 9 rows total carry a thread
count, split `align_reads @ 4 threads` (5 rows) and `quantify @ 4 threads` (4
rows) -- one thread value per job type, no variation to segment against.
`docs/TODO.md`'s existing entry on this
(["Neither model segments by thread
count"](../../TODO.md#neither-model-segments-by-thread-count)) deferred for
exactly this reason and warned against testing segmentation "only against
synthetic data" as if that proved it works on real rows. This spec builds the
mechanism and proves it correct against fixtures; the two real-row criteria
stay open in the issue and the TODO until enough varied-thread runs
accumulate to check against, per `CLAUDE.md`'s standing rule to verify a rule
against the real database before trusting it.

## Where segmentation lives

**New function, existing fits underneath.** `_fit` and `_fit_memory` are
untouched. A new function, `_fit_segmented`, groups records by `.threads` and
calls the existing per-segment `_fit` (or `_fit_memory`, which is already the
same function) on each group, plus once more over every record regardless of
thread count for the fallback.

```python
def _fit_segmented(
    records: list[JobRunTiming],
    sample_fn: Callable[[list[JobRunTiming]], list[tuple[int, int]]],
) -> dict[int | None, dict]:
    """One fit per thread count with enough samples, plus a bytes-only
    fallback fit over every record regardless of thread count, keyed `None`.

    `sample_fn` is `_duration_samples_from` or `_memory_samples_from`, the
    same functions `_samples`/`estimate_memory` already use -- segmentation
    reuses them rather than re-deriving what counts as a sample.
    """
```

Why a wrapper instead of teaching `_fit` about threads directly: `_fit`'s
contract (least-squares over `(bytes, duration)` pairs, `None` below
`MIN_SAMPLES`) is exactly right for a single segment and for the fallback --
segmentation is "call that contract N+1 times and pick one," not a change to
what a fit means. Keeping `_fit` as-is also means the fallback path is
provably identical to today's un-segmented behavior: it's a call to the same
function with the same inputs it already received.

Records with `threads is None` are excluded from every per-thread group (an
unknown thread count can't be assigned to a segment) but are included in the
`None`-keyed fallback fit, matching today's behavior where thread count plays
no role at all.

## Threshold

Reuses `MIN_SAMPLES` (5), the same constant `_fit` already enforces, rather
than a new segment-specific constant. `MIN_SAMPLES`'s existing docstring
comment ("five points is the minimum at which a slope means anything") is a
statement about least-squares fits in general, not about the pooled case
specifically, so it applies unchanged to a per-thread group. A per-segment
constant could plausibly be stricter (fewer degrees of freedom feeding each
segment than the pooled fit gets), but that would be a number with no data
behind it yet, and the constant is trivial to split out later if segmented
fits turn out to need a different bar once real rows exist to check against.
`_fit_segmented`'s docstring says which constant it uses and points at this
paragraph.

## API surface

**`estimate()` and `estimate_memory()` gain `threads: int | None = None`.**

`threads=None` (the default, and what every existing caller currently passes
implicitly by not passing anything) produces byte-only output identical to
today's -- this is the regression the tests pin down.

`threads=<int>` builds the segmented dict via `_fit_segmented` and looks up
that thread count; if present, that segment's model answers, otherwise the
`None` fallback entry does. Either way the response gains one new key:

```python
"segment": {"threads": int | None, "samples": int}
```

`segment.threads` is the thread count whose model actually answered --
`None` when it fell back, not necessarily equal to the `threads` argument
passed in. This distinguishes "asked for 8 threads, 8-thread segment
answered" from "asked for 8 threads, fell back to the bytes-only pool" for
any caller or diagnostics view that wants to say so.

**`memory_estimate.resolve()` gains the same `threads: int | None = None`**,
threaded straight through to its internal `timing_service.estimate_memory`
call. Its `MemoryEstimate` return shape is unchanged -- `segment` info stays
inside the raw `measured` dict this function already discards details of
after extracting `mb`/`source`/`samples`/`r_squared`.

## Wiring the three real callers

Each already has a job's payload in scope and can read `threads` the same
way it already reads `size`:

- `queue/worker.py:_eta_model_ms` -- add `job.payload.get("threads")` to the
  existing `timing_service.estimate(job.type, size)` call.
- `api/v1/jobs.py:get_job` -- same pattern for both the `timing_estimate` and
  `memory_estimate` calls, and pass it into the `memory_estimate.resolve(...)`
  call a few lines below.

No other caller changes: passing `threads=None` implicitly (by omission) is
correct everywhere else and preserves current behavior exactly.

## Diagnostics (`stats()`)

`stats()` gains a `segments` list per job type entry:

```python
"segments": [
    {"threads": int, "samples": int, "model": {...} | None}
    for threads in sorted(thread counts seen)
]
```

Each entry's `model` mirrors the existing top-level `model` shape
(`slope_ms_per_byte`/`intercept_ms`/`r_squared`) or `None` if that thread
count has fewer than `MIN_SAMPLES` rows. This is additive and empty today (no
thread variation exists yet), so it's inert until real segmented data shows
up -- but it's the thing that lets a future look at `/timing-model` answer
"is this actually segmenting, and on what" without a database query.

## Testing

All fixture-based, per the acceptance criterion for automated tests
specifically (as distinct from the real-row criterion this spec leaves
open). Cases:

1. **Regression pin:** `estimate(job_type, size)` with no `threads` arg
   produces byte-for-byte the same output as before this change, given the
   same fixture rows.
2. **Segment fit chosen:** feed `>= MIN_SAMPLES` rows at one thread count and
   `>= MIN_SAMPLES` at another with a different slope; assert
   `estimate(..., threads=X)` returns the X-segment's fit (check
   `segment.threads == X`), not the pooled one.
3. **Sparse segment falls back:** a thread count with `< MIN_SAMPLES` rows
   returns the `None`-fallback fit (`segment.threads is None`), and that
   fallback point still counts toward the pooled samples.
4. **Unknown thread count excluded from segments, included in fallback:**
   rows with `threads=None` never form or join a per-thread group but do
   appear in the `None` fallback's sample count.
5. **Outcome filtering still holds under segmentation:** a `FAILED` row at a
   thread count that would otherwise have enough samples to segment is
   excluded from both that segment's fit and the fallback -- exercises
   `_fit_segmented` against `_modelled()`'s output the same way `_samples`
   and `estimate_memory` already are, closing the "all predictive readers
   use the outcome-filtered accessor" criterion with a test rather than
   leaving it as an unverified claim about existing code.
6. **`estimate_memory` mirrors 1-5** using `_memory_samples_from` as
   `sample_fn`, confirming the shared `_fit_segmented` behaves the same for
   both consumers.

## Out of scope, and what would close it

Leave the "thread-segmented fits use real rows" and "real-row verification"
checkboxes unticked on #8 after this lands. Once several job types
accumulate runs at genuinely differing thread counts, closing them means:
querying the real `job_timings` collection the way the
[timing-model diagnostics endpoint check in
CLAUDE.md](../../../CLAUDE.md#querying-computation-records) describes,
confirming a real segment actually gets picked over the fallback for at
least one job type, and recording what was found (sample counts, whether the
segmented slope differs meaningfully from pooled) in the TODO entry's
`-- FIXED` note when it moves to `docs/TODO-done.md`.
