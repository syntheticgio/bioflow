# Thread-count timing segmentation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Segment the duration and memory timing models by thread count when
enough same-thread-count samples exist, falling back to today's bytes-only
model otherwise, per [#8](https://github.com/syntheticgio/bioflow/issues/8).

**Architecture:** A new pure function `_fit_segmented` groups `JobRunTiming`
records by `.threads` and calls the existing `_fit`/`_fit_memory` per group,
plus once over every record for a `None`-keyed fallback. `estimate()` and
`estimate_memory()` gain an optional `threads` parameter that selects a
segment when present. Three real call sites (`worker.py`, `jobs.py`,
`memory_estimate.resolve()`) start passing the job's already-known thread
count through. `stats()` gains a `segments` breakdown for diagnostics.

**Tech Stack:** Python 3.12, Beanie/Motor (MongoDB ODM), pytest
(`asyncio_mode = "auto"`).

Full design: [`docs/superpowers/specs/2026-08-08-thread-count-segmentation-design.md`](../specs/2026-08-08-thread-count-segmentation-design.md).

---

## File map

- Modify: `backend/app/services/timing_service.py` -- add `_fit_segmented`,
  extend `estimate`/`estimate_memory`/`stats`.
- Modify: `backend/app/services/memory_estimate.py` -- thread `threads`
  through `resolve()`.
- Modify: `backend/app/queue/worker.py` -- pass `job.payload.get("threads")`
  in `_eta_model_ms`.
- Modify: `backend/app/api/v1/jobs.py` -- pass `job.payload.get("threads")`
  in `get_job`.
- Test: `backend/tests/storage/test_timing_model.py` -- pure-function tests
  for `_fit_segmented` (duration side).
- Test: `backend/tests/storage/test_memory_model.py` -- pure-function tests
  for `_fit_segmented` (memory side), mirroring the duration ones.
- Test: `backend/tests/queue/test_record_outcomes.py` -- Mongo-backed tests:
  `estimate()`/`estimate_memory()` segment selection, fallback, and outcome
  filtering under segmentation.
- Test: `backend/tests/services/test_memory_estimate.py` -- `resolve()`
  passes `threads` through and it affects which segment answers.

---

### Task 1: `_fit_segmented` grouping function (duration side)

**Files:**
- Modify: `backend/app/services/timing_service.py`
- Test: `backend/tests/storage/test_timing_model.py`

The function groups `(threads, sample)` pairs by thread count, fits each
group with `>= MIN_SAMPLES` samples via the existing `_fit`, and always
includes a `None`-keyed fallback fit over every sample regardless of thread
count. It takes raw `JobRunTiming` records plus a `sample_fn` so the same
function serves both duration (`_duration_samples_from`) and memory
(`_memory_samples_from`) call sites -- Task 1 exercises it with duration
data; Task 2 reuses it unchanged for memory.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/storage/test_timing_model.py`, after the existing
imports (extend the import line to include `_fit_segmented`,
`_duration_samples_from`, and `JobRunTiming`):

```python
from app.services.timing_service import (
    MIN_SAMPLES,
    _duration_samples_from,
    _fit,
    _fit_segmented,
    _r_squared,
)
from app.models.timing import JobRunTiming
```

Append a new test class at the end of the file:

```python
def _timing(*, threads, input_bytes, duration_ms):
    """An unsaved JobRunTiming for feeding straight into `_fit_segmented`
    without touching Mongo -- the grouping logic is pure over the list."""
    return JobRunTiming(
        job_type="align_reads",
        input_bytes=input_bytes,
        duration_ms=duration_ms,
        threads=threads,
    )


class TestSegmentedFit:
    def test_segments_with_enough_samples_get_their_own_fit(self):
        """Two thread counts, each with MIN_SAMPLES rows and a different
        slope: each must be fit independently, not pooled."""
        records = [
            _timing(threads=4, input_bytes=1000 * i, duration_ms=1 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ] + [
            _timing(threads=8, input_bytes=1000 * i, duration_ms=3 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ]
        segments = _fit_segmented(records, _duration_samples_from)
        assert segments[4]["slope"] == pytest.approx(0.001, rel=1e-6)
        assert segments[8]["slope"] == pytest.approx(0.003, rel=1e-6)

    def test_fallback_key_is_none_and_pools_every_record(self):
        """The None entry is the bytes-only fit over ALL records, matching
        today's un-segmented behavior exactly."""
        records = [
            _timing(threads=4, input_bytes=1000 * i, duration_ms=1 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ]
        pooled = _fit(_duration_samples_from(records))
        segments = _fit_segmented(records, _duration_samples_from)
        assert segments[None]["slope"] == pytest.approx(pooled["slope"])
        assert segments[None]["n"] == pooled["n"]

    def test_sparse_thread_count_gets_no_segment_entry(self):
        """Fewer than MIN_SAMPLES rows at a thread count: no key for it at
        all, so callers fall through to the None fallback."""
        records = [
            _timing(threads=4, input_bytes=1000 * i, duration_ms=1 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ] + [
            _timing(threads=16, input_bytes=1000, duration_ms=1)
            for _ in range(MIN_SAMPLES - 1)
        ]
        segments = _fit_segmented(records, _duration_samples_from)
        assert 4 in segments
        assert 16 not in segments
        assert None in segments

    def test_unknown_thread_count_excluded_from_segments_included_in_fallback(self):
        """threads=None records never form or join a per-thread group, but
        do count toward the pooled fallback -- unknown thread count means
        'can't be assigned a segment', not 'assign it the None segment'."""
        records = [
            _timing(threads=None, input_bytes=1000 * i, duration_ms=1 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ]
        segments = _fit_segmented(records, _duration_samples_from)
        assert list(segments.keys()) == [None]
        assert segments[None]["n"] == MIN_SAMPLES
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/storage/test_timing_model.py -v`
Expected: FAIL with `ImportError: cannot import name '_fit_segmented'`

- [ ] **Step 3: Implement `_fit_segmented`**

In `backend/app/services/timing_service.py`, add the function directly
after `_fit_memory` (around line 381), before `estimate_memory`:

```python
def _fit_segmented(
    records: list[JobRunTiming],
    sample_fn,
) -> dict[int | None, dict]:
    """One fit per thread count with `>= MIN_SAMPLES` samples, plus a
    bytes-only fallback fit over every record regardless of thread count,
    keyed `None`.

    `sample_fn` is `_duration_samples_from` or `_memory_samples_from` --
    whichever `(input_bytes, y)` extraction the caller wants segmented, so
    duration and memory share this grouping logic rather than each
    reimplementing it. Records with `threads is None` never form or join a
    per-thread group (an unknown thread count can't be assigned one) but do
    count toward the `None` fallback, matching today's un-segmented
    behavior exactly when nothing has a thread count yet.

    Reuses `MIN_SAMPLES`, the same threshold `_fit` already enforces --
    see the design doc's "Threshold" section for why a separate,
    segment-specific constant was not introduced.
    """
    by_threads: dict[int, list[JobRunTiming]] = {}
    for record in records:
        if record.threads is not None:
            by_threads.setdefault(record.threads, []).append(record)

    out: dict[int | None, dict] = {}
    for threads, group in by_threads.items():
        samples = sample_fn(group)
        if len(samples) >= MIN_SAMPLES:
            model = _fit(samples)
            if model is not None:
                out[threads] = model

    fallback = _fit(sample_fn(records))
    if fallback is not None:
        out[None] = fallback

    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/storage/test_timing_model.py -v`
Expected: PASS, all tests including the four new `TestSegmentedFit` cases

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/timing_service.py backend/tests/storage/test_timing_model.py
git commit -m "feat(timing): add _fit_segmented grouping function"
```

---

### Task 2: Mirror segmentation tests for the memory side

**Files:**
- Test: `backend/tests/storage/test_memory_model.py`

`_fit_segmented` already works for memory since it takes `sample_fn` as a
parameter -- this task only adds tests proving it behaves the same way with
`_memory_samples_from`, per the design's "estimate_memory mirrors 1-5"
testing item.

- [ ] **Step 1: Read the existing memory test file's fixtures**

Run: `grep -n "^from\|^import\|def _" backend/tests/storage/test_memory_model.py`

Confirm `RunResources`, `JobRunTiming`, and `_memory_samples_from` are
either already imported or need adding — match whatever helper pattern the
file already uses for building a `JobRunTiming` with a peak RSS.

- [ ] **Step 2: Write the failing tests**

Add to `backend/tests/storage/test_memory_model.py` (adjust the import line
at the top of the file to add `_fit_segmented, _memory_samples_from` to the
existing `from app.services.timing_service import ...` line, and add
`from app.models.timing import JobRunTiming` if not already present):

```python
def _mem_timing(*, threads, input_bytes, peak_rss_bytes):
    return JobRunTiming(
        job_type="align_reads",
        input_bytes=input_bytes,
        duration_ms=1,
        threads=threads,
        resources=RunResources(peak_rss_bytes=peak_rss_bytes),
    )


class TestSegmentedMemoryFit:
    def test_segments_with_enough_samples_get_their_own_fit(self):
        records = [
            _mem_timing(threads=4, input_bytes=1000 * i, peak_rss_bytes=1_000_000 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ] + [
            _mem_timing(threads=8, input_bytes=1000 * i, peak_rss_bytes=3_000_000 * i)
            for i in range(1, MIN_SAMPLES + 1)
        ]
        segments = _fit_segmented(records, _memory_samples_from)
        assert segments[4]["slope"] == pytest.approx(1000.0, rel=1e-6)
        assert segments[8]["slope"] == pytest.approx(3000.0, rel=1e-6)

    def test_records_without_a_measured_peak_do_not_count_as_samples(self):
        """RunResources().peak_rss_bytes defaults to None -- a run under the
        sampling floor must not be treated as a zero-memory sample."""
        records = [
            _mem_timing(threads=4, input_bytes=1000 * i, peak_rss_bytes=1_000_000 * i)
            for i in range(1, MIN_SAMPLES)
        ] + [
            JobRunTiming(
                job_type="align_reads",
                input_bytes=1000,
                duration_ms=1,
                threads=4,
            )
            for _ in range(5)
        ]
        segments = _fit_segmented(records, _memory_samples_from)
        assert 4 not in segments
```

`RunResources` needs importing too if the file doesn't already: add
`from app.models.timing import RunResources` alongside the `JobRunTiming`
import.

- [ ] **Step 3: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/storage/test_memory_model.py -v`
Expected: FAIL with `ImportError: cannot import name '_fit_segmented'` (or
`_memory_samples_from`, whichever wasn't already imported)

- [ ] **Step 4: Fix imports only -- no production code changes needed**

`_fit_segmented` already handles this from Task 1. Add the missing imports
identified in Step 3's failure to the top of
`backend/tests/storage/test_memory_model.py`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/storage/test_memory_model.py -v`
Expected: PASS, all tests including the two new `TestSegmentedMemoryFit`
cases

- [ ] **Step 6: Commit**

```bash
git add backend/tests/storage/test_memory_model.py
git commit -m "test(timing): cover _fit_segmented against memory samples"
```

---

### Task 3: `estimate()` and `estimate_memory()` gain `threads`

**Files:**
- Modify: `backend/app/services/timing_service.py`
- Test: `backend/tests/queue/test_record_outcomes.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/queue/test_record_outcomes.py`. First extend the
`_record` helper (around line 45) to accept `threads`:

```python
async def _record(outcome, duration_ms=120_000, input_bytes=1_000_000, threads=None):
    await JobRunTiming(
        job_type="align_reads",
        input_bytes=input_bytes,
        duration_ms=duration_ms,
        outcome=outcome,
        threads=threads,
    ).insert()
```

This is a signature-compatible change (new kwarg with a default) so every
existing call to `_record` in the file keeps working unchanged.

Then append a new test class at the end of the file:

```python
class TestThreadSegmentation:
    async def test_no_threads_argument_matches_todays_bytes_only_behavior(self):
        """Regression pin: estimate() with no threads arg must be identical
        to before this feature existed."""
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED, duration_ms=1000 * i, input_bytes=1000 * i
            )
        result = await timing_service.estimate("align_reads", 5000)
        assert result["known"] is True
        assert result["segment"] == {"threads": None, "samples": result["samples"]}

    async def test_segment_with_enough_samples_answers_over_the_pool(self):
        """A thread count with its own MIN_SAMPLES rows and a distinct slope
        must be the one that answers, not the pooled fit."""
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=1 * i,
                input_bytes=1000 * i,
                threads=4,
            )
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=3 * i,
                input_bytes=1000 * i,
                threads=8,
            )
        result = await timing_service.estimate("align_reads", 5000, threads=8)
        assert result["segment"]["threads"] == 8

    async def test_sparse_thread_count_falls_back(self):
        """Fewer than MIN_SAMPLES rows at the requested thread count: the
        answer comes from the None fallback, not a half-formed segment."""
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=1000 * i,
                input_bytes=1000 * i,
                threads=4,
            )
        for _ in range(2):
            await _record(RunOutcome.SUCCEEDED, duration_ms=1, threads=16)
        result = await timing_service.estimate("align_reads", 5000, threads=16)
        assert result["segment"]["threads"] is None

    async def test_a_failed_run_at_the_segment_thread_count_is_excluded(self):
        """Outcome filtering must hold under segmentation too -- a FAILED row
        at a thread count that would otherwise qualify for its own segment
        must not enter that segment's fit or the fallback."""
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=120_000,
                input_bytes=1_000_000,
                threads=4,
            )
        clean = await timing_service.estimate("align_reads", 1_000_000, threads=4)
        for _ in range(MIN_SAMPLES):
            await _record(
                RunOutcome.FAILED, duration_ms=200, input_bytes=1_000_000, threads=4
            )
        after = await timing_service.estimate("align_reads", 1_000_000, threads=4)
        assert after["estimate_ms"] == pytest.approx(clean["estimate_ms"], rel=0.01)

    async def test_memory_estimate_segment_selection_mirrors_duration(self):
        for i in range(1, MIN_SAMPLES + 1):
            await JobRunTiming(
                job_type="align_reads",
                input_bytes=1000 * i,
                duration_ms=1,
                outcome=RunOutcome.SUCCEEDED,
                threads=4,
                resources={"peak_rss_bytes": 1_000_000 * i},
            ).insert()
        result = await timing_service.estimate_memory(
            "align_reads", 5000, threads=4
        )
        assert result["known"] is True
        assert result["segment"]["threads"] == 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/queue/test_record_outcomes.py -v`
Expected: FAIL with `TypeError: estimate() got an unexpected keyword
argument 'threads'`

- [ ] **Step 3: Implement the `threads` parameter**

In `backend/app/services/timing_service.py`, replace `estimate` (currently
lines 328-355):

```python
async def estimate(
    job_type: str, input_bytes: int, *, threads: int | None = None
) -> dict | None:
    """Predicted duration in ms for a run of this type and size.

    None means "not enough history" -- callers should show no estimate rather
    than guessing.

    `threads=None` (the default) is byte-only, identical to this function's
    behavior before segmentation existed. `threads=<int>` prefers a
    same-thread-count segment's fit when one has enough samples, falling back
    to the same pooled bytes-only fit `threads=None` would have used.
    """
    records = await _modelled(job_type)
    samples = _duration_samples_from(records)

    if threads is None:
        model = _fit(samples)
        answered_by = None
    else:
        segments = _fit_segmented(records, _duration_samples_from)
        if threads in segments:
            model, answered_by = segments[threads], threads
        elif None in segments:
            model, answered_by = segments[None], None
        else:
            model, answered_by = None, None

    if model is None:
        return {
            "known": False,
            "samples": len(samples),
            "needed": max(0, MIN_SAMPLES - len(samples)),
        }

    predicted = model["intercept"] + model["slope"] * max(0, input_bytes)
    return {
        "known": True,
        "estimate_ms": int(max(100, predicted)),
        "samples": model["n"],
        "r_squared": round(_r_squared(samples, model), 3),
        "throughput_mb_s": (
            round(1000 / (model["slope"] * 1024 * 1024), 1)
            if model["slope"] > 0
            else None
        ),
        "range": _observed_range(samples, input_bytes),
        "segment": {"threads": answered_by, "samples": model["n"]},
    }
```

Note this changes `estimate`'s internals to call `_modelled`/
`_duration_samples_from` directly instead of `_samples` (which was just a
thin wrapper doing exactly that) -- `_samples` stays as-is since
`records_for_object` callers and any external reference to it are
unaffected; it is simply no longer called from inside `estimate` itself.
Leave `_samples` in the file unchanged; do not delete it.

Then replace `estimate_memory` (currently lines 384-414) the same way:

```python
async def estimate_memory(
    job_type: str, input_bytes: int, *, threads: int | None = None
) -> dict | None:
    """Predicted peak RSS in bytes for a run of this type and size.

    **Modelled outcomes only** -- reads via `_modelled()`, the same
    outcome-filtered accessor `_samples()` uses, so a failed/OOM-killed run's
    peak RSS never enters the fit (that peak is the ceiling the run hit, not
    what it needed, and folding it in would bias predictions toward the exact
    number that caused the OOM).

    Returns `known: False` rather than a guess when there is not enough
    history. Only runs above the sampling floor carry a measured peak, so this
    can stay silent long after the duration model has become confident.

    `threads` behaves exactly as it does in `estimate()` -- see that
    docstring.
    """
    records = await _modelled(job_type)
    samples = _memory_samples_from(records)

    if threads is None:
        model = _fit_memory(samples)
        answered_by = None
    else:
        segments = _fit_segmented(records, _memory_samples_from)
        if threads in segments:
            model, answered_by = segments[threads], threads
        elif None in segments:
            model, answered_by = segments[None], None
        else:
            model, answered_by = None, None

    if model is None:
        return {
            "known": False,
            "samples": len(samples),
            "needed": max(0, MIN_SAMPLES - len(samples)),
        }

    predicted = model["intercept"] + model["slope"] * max(0, input_bytes)
    return {
        "known": True,
        "estimate_bytes": int(max(0, predicted)),
        "samples": model["n"],
        "r_squared": round(_r_squared(samples, model), 3),
        "range": _observed_range(samples, input_bytes),
        "segment": {"threads": answered_by, "samples": model["n"]},
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/queue/test_record_outcomes.py -v`
Expected: PASS, all tests including the five new `TestThreadSegmentation`
cases

- [ ] **Step 5: Run the full backend suite to check for regressions**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: all tests pass; pay particular attention to
`tests/storage/test_timing_model.py`, `tests/storage/test_memory_model.py`,
`tests/storage/test_eta.py`, and `tests/api/test_jobs_progress.py` since
they exercise `estimate`/`estimate_memory` output shape.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/timing_service.py backend/tests/queue/test_record_outcomes.py
git commit -m "feat(timing): thread-count segmentation for estimate() and estimate_memory()"
```

---

### Task 4: Thread `threads` through `memory_estimate.resolve()`

**Files:**
- Modify: `backend/app/services/memory_estimate.py`
- Test: `backend/tests/services/test_memory_estimate.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/services/test_memory_estimate.py`. First check the
existing `_insert_runs` helper's signature (near the bottom of the file,
around line 55) -- it likely doesn't set `threads`. Add a new class:

```python
class TestThreadSegmentation:
    async def test_threads_argument_selects_a_segment(self):
        """resolve() with a threads argument must reach the same segment
        estimate() would pick for that thread count -- proves the plumbing,
        not the fitting logic (already covered in test_record_outcomes.py)."""
        for i in range(1, MIN_SAMPLES + 1):
            await JobRunTiming(
                job_type="align_reads",
                input_bytes=1000 * i,
                duration_ms=1,
                outcome=RunOutcome.SUCCEEDED,
                threads=4,
                resources=RunResources(peak_rss_bytes=10_000_000 * i),
            ).insert()
        result = await memory_estimate.resolve(
            job_type="align_reads",
            input_bytes=5000,
            heuristic_mb=None,
            threads=4,
        )
        assert result.source is EstimateSource.MEASURED
```

Add `MIN_SAMPLES` to the existing `from app.services.timing_service import
...`-style import if `timing_service` constants aren't already imported —
check the top of the file first; if only `memory_estimate` is imported,
add `from app.services.timing_service import MIN_SAMPLES`.

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_memory_estimate.py -v`
Expected: FAIL with `TypeError: resolve() got an unexpected keyword argument
'threads'`

- [ ] **Step 3: Implement the parameter**

In `backend/app/services/memory_estimate.py`, modify the `resolve` function
signature (currently starting around line 93):

```python
async def resolve(
    *,
    job_type: str,
    input_bytes: int | None,
    heuristic_mb: int | None,
    threads: int | None = None,
) -> MemoryEstimate:
```

And update the body's call to `timing_service.estimate_memory` (currently):

```python
    measured = None
    if input_bytes is not None:
        measured = await timing_service.estimate_memory(job_type, input_bytes)
```

to:

```python
    measured = None
    if input_bytes is not None:
        measured = await timing_service.estimate_memory(
            job_type, input_bytes, threads=threads
        )
```

Also update the function's docstring to note the new parameter, appending
after the existing docstring's last paragraph:

```python
    `threads` passes straight through to `timing_service.estimate_memory` --
    see that function's docstring for segment-selection behavior. `None`
    (the default) preserves this function's byte-only behavior from before
    segmentation existed.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_memory_estimate.py -v`
Expected: PASS, including the new `TestThreadSegmentation` case

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/memory_estimate.py backend/tests/services/test_memory_estimate.py
git commit -m "feat(timing): thread threads through memory_estimate.resolve()"
```

---

### Task 5: Wire the three real callers

**Files:**
- Modify: `backend/app/queue/worker.py`
- Modify: `backend/app/api/v1/jobs.py`

No new automated test for this task: these are one-line call-site changes
passing an already-validated value (`job.payload.get("threads")`) into
parameters Task 3/4 already covered end-to-end. Verified manually in Step 3
below instead.

- [ ] **Step 1: Update `worker.py:_eta_model_ms`**

In `backend/app/queue/worker.py`, find (around line 437):

```python
        estimate = await timing_service.estimate(job.type, size)
```

Replace with:

```python
        estimate = await timing_service.estimate(
            job.type, size, threads=job.payload.get("threads")
        )
```

- [ ] **Step 2: Update `jobs.py:get_job`**

In `backend/app/api/v1/jobs.py`, find (around lines 255-262):

```python
        if size:
            out["timing_estimate"] = await timing_service.estimate(job.type, size)
            # The raw model output stays, for the diagnostics view that shows
            # sample counts and fit quality. `resolution` is what the launch
            # dialog reads: the number actually used, and which layer produced
            # it. `heuristic_mb=None` because this endpoint has no run
            # structure to derive one from -- so an untrustworthy measurement
            # resolves to UNKNOWN here rather than silently to coefficients.
            out["memory_estimate"] = await timing_service.estimate_memory(job.type, size)
            from app.services import memory_estimate

            resolved = await memory_estimate.resolve(
                job_type=job.type,
                input_bytes=size,
                heuristic_mb=None,
            )
```

Replace with:

```python
        if size:
            threads = job.payload.get("threads")
            out["timing_estimate"] = await timing_service.estimate(
                job.type, size, threads=threads
            )
            # The raw model output stays, for the diagnostics view that shows
            # sample counts and fit quality. `resolution` is what the launch
            # dialog reads: the number actually used, and which layer produced
            # it. `heuristic_mb=None` because this endpoint has no run
            # structure to derive one from -- so an untrustworthy measurement
            # resolves to UNKNOWN here rather than silently to coefficients.
            out["memory_estimate"] = await timing_service.estimate_memory(
                job.type, size, threads=threads
            )
            from app.services import memory_estimate

            resolved = await memory_estimate.resolve(
                job_type=job.type,
                input_bytes=size,
                heuristic_mb=None,
                threads=threads,
            )
```

- [ ] **Step 3: Run the full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: all tests pass, same count as Task 3 Step 5 (this task changes no
behavior for any thread-less job, since `job.payload.get("threads")`
returns `None` for job types that don't set it, matching the default).

- [ ] **Step 4: Commit**

```bash
git add backend/app/queue/worker.py backend/app/api/v1/jobs.py
git commit -m "feat(timing): pass known thread counts into duration/memory estimates"
```

---

### Task 6: `stats()` diagnostics breakdown

**Files:**
- Modify: `backend/app/services/timing_service.py`
- Test: `backend/tests/queue/test_record_outcomes.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/queue/test_record_outcomes.py`:

```python
class TestStatsSegments:
    async def test_segments_list_is_empty_with_no_thread_data(self):
        for _ in range(MIN_SAMPLES):
            await _record(RunOutcome.SUCCEEDED)
        rows = await timing_service.stats()
        row = next(r for r in rows if r["job_type"] == "align_reads")
        assert row["segments"] == []

    async def test_segments_list_reports_a_qualifying_thread_count(self):
        for i in range(1, MIN_SAMPLES + 1):
            await _record(
                RunOutcome.SUCCEEDED,
                duration_ms=1000 * i,
                input_bytes=1000 * i,
                threads=4,
            )
        rows = await timing_service.stats()
        row = next(r for r in rows if r["job_type"] == "align_reads")
        assert len(row["segments"]) == 1
        assert row["segments"][0]["threads"] == 4
        assert row["segments"][0]["samples"] == MIN_SAMPLES
        assert row["segments"][0]["model"] is not None

    async def test_sparse_thread_count_is_omitted_from_segments(self):
        for _ in range(MIN_SAMPLES - 1):
            await _record(RunOutcome.SUCCEEDED, threads=4)
        rows = await timing_service.stats()
        row = next(r for r in rows if r["job_type"] == "align_reads")
        assert row["segments"] == []
```

`MIN_SAMPLES` must be imported in this test file -- check the top of
`backend/tests/queue/test_record_outcomes.py`; if it only imports
`timing_service`, add `from app.services.timing_service import MIN_SAMPLES`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/queue/test_record_outcomes.py -v`
Expected: FAIL with `KeyError: 'segments'`

- [ ] **Step 3: Implement the `segments` field in `stats()`**

In `backend/app/services/timing_service.py`, replace `stats()` (currently
lines 417-450):

```python
async def stats() -> list[dict]:
    """Per-job-type model summary, for a diagnostics view."""
    types = await JobRunTiming.distinct("job_type")
    out = []
    for t in types:
        records = await _modelled(t)
        samples = _duration_samples_from(records)
        model = _fit(samples)
        memory_samples = _memory_samples_from(records)
        memory_model = _fit_memory(memory_samples)

        thread_counts = sorted(
            {r.threads for r in records if r.threads is not None}
        )
        duration_segments = _fit_segmented(records, _duration_samples_from)
        segments = [
            {
                "threads": threads,
                "samples": duration_segments[threads]["n"],
                "model": {
                    "slope_ms_per_byte": duration_segments[threads]["slope"],
                    "intercept_ms": round(duration_segments[threads]["intercept"]),
                    "r_squared": round(
                        _r_squared(
                            _duration_samples_from(
                                [r for r in records if r.threads == threads]
                            ),
                            duration_segments[threads],
                        ),
                        3,
                    ),
                },
            }
            for threads in thread_counts
            if threads in duration_segments
        ]

        out.append(
            {
                "job_type": t,
                "samples": len(samples),
                "model": None
                if model is None
                else {
                    "slope_ms_per_byte": model["slope"],
                    "intercept_ms": round(model["intercept"]),
                    "r_squared": round(_r_squared(samples, model), 3),
                },
                # Separate sample count: only runs above the floor carry a
                # peak, so this is legitimately smaller than `samples`.
                "memory_samples": len(memory_samples),
                "memory_model": None
                if memory_model is None
                else {
                    "slope_bytes_per_byte": memory_model["slope"],
                    "intercept_bytes": round(memory_model["intercept"]),
                    "r_squared": round(_r_squared(memory_samples, memory_model), 3),
                },
                # Per-thread-count duration fits that qualified (>=
                # MIN_SAMPLES same-thread rows), for the diagnostics view to
                # show what's actually segmenting versus falling back. Empty
                # until real runs at varying thread counts accumulate -- see
                # docs/superpowers/specs/2026-08-08-thread-count-segmentation-design.md.
                "segments": segments,
            }
        )
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/queue/test_record_outcomes.py -v`
Expected: PASS, all tests including the three new `TestStatsSegments` cases

- [ ] **Step 5: Run the full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: all tests pass. Check specifically for any test asserting the
exact shape of `/timing-model`'s response (search:
`grep -rn "timing-model\|timing_service.stats" backend/tests/`) since
`stats()`'s dict now has one more key per job-type entry.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/timing_service.py backend/tests/queue/test_record_outcomes.py
git commit -m "feat(timing): add per-thread-count segments to stats() diagnostics"
```

---

### Task 7: Update the TODO entry to reflect partial completion

**Files:**
- Modify: `docs/TODO.md`

Per `CLAUDE.md`'s "Closing out a TODO entry" section: this entry is only
*partially* resolved (segmentation machinery shipped; the two real-row
criteria have not, since there still isn't varied thread-count data). It
stays in `docs/TODO.md` rather than moving to `docs/TODO-done.md`.

- [ ] **Step 1: Add a note to the existing entry**

In `docs/TODO.md`, find the `## Neither model segments by thread count`
heading (around line 366) and insert a note immediately after the heading,
before the existing "Raised:" line:

```markdown
## Neither model segments by thread count

**Partially addressed 2026-08-08:** the segmentation machinery landed --
`_fit_segmented` in `backend/app/services/timing_service.py` groups
`JobRunTiming` records by `threads` and fits each group with
`>= MIN_SAMPLES` (5) same-thread-count rows, falling back to the existing
pooled bytes-only fit otherwise. `estimate()`, `estimate_memory()`, and
`memory_estimate.resolve()` all take an optional `threads` argument now, and
the three real callers (`worker.py:_eta_model_ms`, `jobs.py:get_job`) pass
`job.payload.get("threads")` through. `stats()`'s `/timing-model` output
gained a `segments` list per job type.

**Still open:** at the time this landed, the real `job_timings` collection
held only 9 rows with a thread count at all (`align_reads @ 4` x5,
`quantify @ 4` x4) -- one thread value per job type, nothing to segment
against. The two real-row acceptance criteria on
[#8](https://github.com/syntheticgio/bioflow/issues/8) --
"thread-segmented duration and memory fits use real computation rows" and
"real-row verification... cover segmentation and fallback" -- stay open
until enough varied-thread runs accumulate. See
`docs/superpowers/specs/2026-08-08-thread-count-segmentation-design.md`
for the full design and why those two criteria were deliberately deferred
rather than faked against fixtures.

---
```

Leave the rest of the existing entry body (the "Raised:", "Deferred
because...", "Revisit once...", "Touches:" paragraphs) exactly as-is below
the new note and the `---` separator -- it's still the accurate original
diagnosis of why segmentation was deferred, and per CLAUDE.md the original
body should stay intact for the next reader.

- [ ] **Step 2: Commit**

```bash
git add docs/TODO.md
git commit -m "docs: note partial completion of thread-count segmentation (#8)"
```

---

## Final verification

- [ ] **Run the complete backend suite one more time**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: full pass count with no failures, no flaky DB-isolation symptoms
(a rotating handful of unrelated failures would mean something is sharing
Mongo with another run -- re-run once to confirm before investigating
further, per `CLAUDE.md`'s note on this exact failure mode).

- [ ] **Manually check `/timing-model` against the running worktree stack**

```bash
./ops/worktree-up.sh
curl -s http://localhost:8100/api/v1/jobs/timing-model | python3 -m json.tool
```

Confirm the response includes a `segments` key (empty list is correct,
matching the real data's current lack of thread variation) for at least one
job type with existing samples, and that `min_samples` and `types` are
still present and unchanged in shape otherwise.
