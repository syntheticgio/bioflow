# GC vs coverage visualizations — task breakdown

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the GC-vs-coverage bias curve (#640) and the per-contig
GC-vs-coverage blobplot (#641) as two independently mergeable PRs that share
one join module.

**Architecture:** A pure module (`gc_coverage.py`) joins `gc_tracks`' stored
per-window GC against mosdepth's stored per-window depth on their shared
`(contig, window_index)` grid, width-weights the aggregation, then offers two
aggregations of that join: by GC bin (stage 1, the bias curve) and by contig
(stage 2, the blobplot). A THREAD-mode handler wraps the pure functions; the
async launcher does every database read the handler cannot do for itself.
Both ship as read-only facts/report on the BAM object, the `coverage`
posture (`_NO_NARRATIVE_STEP`), gated by a suggestion card with named
refusal reasons rather than auto-chaining prerequisite jobs.

**Tech Stack:** Python (FastAPI, Beanie/Motor, the queue's THREAD handler
mode), React + TypeScript, hand-rolled SVG (no charting library, matching
every sibling chart in this codebase).

**Spec:** `docs/superpowers/specs/2026-08-20-gc-coverage-visualizations-design.md`
(decisions V1–V5). Companion high-level plan:
`docs/superpowers/plans/2026-08-20-gc-coverage-visualizations.md`.

## Global Constraints

- **Reuse the shared window grid** (`gc_tracks.WINDOW_COUNT = 500`,
  `MIN_WINDOW_BASES = 100`); no new windowing constant (V1).
- **Bin/contig aggregation weights each window by its physical width**
  (`end - start`), never a naive `mean(depth)` — this must be proven by a
  test using contigs of *different* lengths (V1).
- **A window in the reference GC but absent from depth resolves to depth 0**,
  never a dropped window (spec "Testing" section).
- **The card refuses with a reason naming the missing step** for each of:
  no resolvable alignment target, target has no `gc_tracks`, BAM has no
  windowed `coverage` run. Never auto-chains the missing jobs (V2).
- **#641's per-contig GC is `Σ(gc_count) / Σ(window_bases)` over the same
  joined windows** — no new FASTA scan in `bam_stats_runner` (V3).
- **#641's cap is by cumulative length** (contigs covering 99% of bases),
  plus a hard ceiling; the omission count is part of the output, always
  rendered on the chart when non-zero (V4).
- **Point area, not radius, proportional to contig length** on the scatter
  (V4/issue #641).
- **Both are read-only facts + a report on the BAM**, `_NO_NARRATIVE_STEP`
  posture, no new DataObject (V5).
- **`mosdepth_runner.py`'s `coverage_mode` must be `"windows"`**, not
  `"regions"` — a regions-mode run is not on the shared grid at all, and the
  card must treat it exactly like "no coverage run" (new finding from
  re-reading `mosdepth_handlers.py:82`, not explicit in the spec — the join
  cannot recover contig windows from a region-mode `coverage.json`, whose
  `regions` key holds target-BED rows, not the uniform tiling).
- **A THREAD-mode handler cannot reach the database** (`de_summary_handlers.py`
  is the reference: `ctx.payload` carries everything). The launcher does the
  two object reads (BAM's `coverage_report`, reference's `gc_tracks` fact)
  and the JSON report read, and hands the handler already-loaded dicts.
- **Restart the worker after editing a handler** —
  `docker compose restart worker`, from the **main** repo root, never a
  worktree's compose project. It does not hot-reload.
- Run `./backend/run-worktree-tests.sh tests/ -q` from the worktree, never
  `docker compose exec api` (silently tests `main`'s code).
- Run `ruff check --config backend/pyproject.toml backend/app backend/tests
  ops e2e` before every commit; fix every finding, not only ones this branch
  introduced.

---

## Stage 1 — the join and the bias curve (#640)

### Task 1: `gc_coverage.py` — width-weighted bias curve, pure

**Files:**
- Create: `backend/app/pipelines/gc_coverage.py`
- Test: `backend/tests/pipelines/test_gc_coverage.py`

**Interfaces:**
- Consumes: nothing from other tasks (first task, pure stdlib only).
- Produces:
  - `JoinedWindow` — a `TypedDict` (or small dataclass) with fields
    `contig: str`, `index: int`, `start: int`, `end: int`, `width: int`,
    `gc: float | None`, `depth: float`.
  - `join_windows(gc_contigs: list[dict], depth_regions: dict[str,
    list[dict]]) -> list[JoinedWindow]` — `gc_contigs` is
    `gc_tracks`-shaped (`[{"name", "length", "window_bases", "gc": [...],
    "skew": [...]}]`), `depth_regions` is mosdepth's `parse_regions()`
    return shape (`{contig: [{"start", "end", "depth", "name"}]}`).
  - `bias_curve(joined: list[JoinedWindow], *, bins: int = 20) -> list[dict]`
    — each dict is `{"gc_min": float, "gc_max": float, "mean_depth": float,
    "window_count": int}`.

- [ ] **Step 1: Write the width-weighting test first — this is the one piece
  of real logic in the whole feature**

```python
# backend/tests/pipelines/test_gc_coverage.py
from app.pipelines.gc_coverage import JoinedWindow, bias_curve, join_windows


def _window(contig, index, start, end, gc, depth):
    return JoinedWindow(
        contig=contig, index=index, start=start, end=end,
        width=end - start, gc=gc, depth=depth,
    )


def test_bias_curve_weights_by_window_width_not_uniform_mean():
    """Two contigs land in the same GC bin (both GC=50.0) but one contig's
    windows are 10x wider than the other's. A naive `mean(depths)` gives
    equal say to a 10bp window and a 100bp window and returns (2+20)/2=11.
    The correct width-weighted mean is dominated by the wide window:
    (2*10 + 20*100) / (10 + 100) = 2020/110 = 18.363636...

    Contigs of DIFFERENT lengths are required here: a uniform-width fixture
    produces the same number under both implementations and would pass
    against the naive, wrong one.
    """
    joined = [
        _window("short", 0, 0, 10, gc=50.0, depth=2.0),
        _window("long", 0, 0, 100, gc=50.0, depth=20.0),
    ]
    curve = bias_curve(joined, bins=1)
    assert len(curve) == 1
    assert curve[0]["mean_depth"] == pytest.approx(2020 / 110)
    assert curve[0]["window_count"] == 2
```

Add `import pytest` at the top of the test file.

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_gc_coverage.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.gc_coverage'`

- [ ] **Step 3: Write `gc_coverage.py` with `JoinedWindow` and `bias_curve`**
  (leave `join_windows` as a stub for now — Step 3's test only exercises
  `bias_curve` directly against hand-built `JoinedWindow`s):

```python
"""Join reference GC (gc_tracks) against alignment depth (mosdepth) on their
shared per-window grid, and aggregate the join two ways.

A pure module: no queue, no filesystem, no subprocess. gc_tracks and
mosdepth already tile every contig identically (`window_count = min(
WINDOW_COUNT, length // MIN_WINDOW_BASES)` per contig, mosdepth_runner.py
imports the constants rather than redeclaring them), so the join is a
`(contig, window_index)` key lookup -- no resampling, no second reference
scan.

Windows vary in physical width across contigs (a contig with fewer windows
than WINDOW_COUNT has wider ones), so every aggregation here weights by
`width` rather than averaging window values directly. See `bias_curve`'s
docstring and its test for why the unweighted mean is a real bug, not a
simplification: it silently over-weights short-contig windows on exactly the
fragmented assemblies where these plots matter most.
"""

from __future__ import annotations

from typing import TypedDict


class JoinedWindow(TypedDict):
    contig: str
    index: int
    start: int
    end: int
    width: int
    gc: float | None
    depth: float


def join_windows(
    gc_contigs: list[dict], depth_regions: dict[str, list[dict]]
) -> list[JoinedWindow]:
    """Join gc_tracks' per-contig window arrays against mosdepth's per-contig
    region rows, keyed by (contig, window index).

    A window present in the reference GC but with no matching depth row
    (mosdepth found no windows for a contig -- e.g. it was too short for
    ANY window, or the BAM had zero reads on it) resolves to depth 0.0,
    never a dropped window: a real GC dropout must read as "no coverage
    here", not silently vanish and bias the curve upward by omission.

    Contigs present only in depth_regions (should not happen -- mosdepth
    windows are built from the same reference's .fai -- but a mismatched
    reference input is not this function's job to detect) are ignored:
    there is no GC to join them to.
    """
    raise NotImplementedError


def bias_curve(joined: list[JoinedWindow], *, bins: int = 20) -> list[dict]:
    """Aggregate joined windows into `bins` fixed-width GC bins, 0-100%.

    Each bin's value is the width-weighted mean depth of every window whose
    GC falls in that bin: `sum(depth * width) / sum(width)`, NOT
    `mean(depth)`. A window with gc=None (an all-N stretch gc_tracks could
    not score) is excluded from every bin -- it has no GC to bin by.

    Empty bins are omitted from the result, not zero-filled: a bias curve is
    read as a line through the GC values that were actually observed, and a
    zero-depth bin at an unobserved GC value would misrepresent a gap as
    "sequenced here at zero depth".
    """
    bin_width = 100.0 / bins
    sums: dict[int, float] = {}
    widths: dict[int, float] = {}
    counts: dict[int, int] = {}

    for w in joined:
        if w["gc"] is None:
            continue
        idx = min(int(w["gc"] / bin_width), bins - 1)
        sums[idx] = sums.get(idx, 0.0) + w["depth"] * w["width"]
        widths[idx] = widths.get(idx, 0.0) + w["width"]
        counts[idx] = counts.get(idx, 0) + 1

    result = []
    for idx in sorted(sums):
        result.append({
            "gc_min": round(idx * bin_width, 2),
            "gc_max": round((idx + 1) * bin_width, 2),
            "mean_depth": round(sums[idx] / widths[idx], 4),
            "window_count": counts[idx],
        })
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_gc_coverage.py -q`
Expected: PASS (`test_bias_curve_weights_by_window_width_not_uniform_mean`)

- [ ] **Step 5: Write the join test — the depth-0 case is the load-bearing
  one**

```python
def test_join_windows_missing_depth_row_resolves_to_zero_not_dropped():
    """A contig gc_tracks scored but mosdepth found no aligned reads on (or
    the contig was too short to window under mosdepth's own floor, though
    the two use the same floor so this specific case is about zero-read
    contigs) must still appear in the join, at depth 0 -- not be silently
    absent. Dropping it would make the bias curve blind to exactly the
    "this GC content has no coverage" signal the plot exists to show.
    """
    gc_contigs = [
        {
            "name": "covered",
            "length": 20,
            "window_bases": 10,
            "gc": [40.0, 60.0],
            "skew": [0.0, 0.0],
        },
        {
            "name": "uncovered",
            "length": 10,
            "window_bases": 10,
            "gc": [50.0],
            "skew": [0.0],
        },
    ]
    depth_regions = {
        "covered": [
            {"start": 0, "end": 10, "depth": 5.0, "name": None},
            {"start": 10, "end": 20, "depth": 8.0, "name": None},
        ],
        # "uncovered" has no key at all -- mosdepth produced no rows for it.
    }

    joined = join_windows(gc_contigs, depth_regions)

    by_contig = {}
    for w in joined:
        by_contig.setdefault(w["contig"], []).append(w)

    assert len(by_contig["covered"]) == 2
    assert len(by_contig["uncovered"]) == 1
    assert by_contig["uncovered"][0]["depth"] == 0.0
    assert by_contig["uncovered"][0]["gc"] == 50.0


def test_join_windows_reconstructs_window_bounds_from_gc_tracks_shape():
    """gc_tracks stores window_bases (a per-contig constant) and a flat gc
    list; start/end for each window index must be reconstructed the same
    way gc_tracks/mosdepth_runner both tile: index * window_bases, with the
    LAST window absorbing the remainder to length (mirrors
    mosdepth_runner.build_windows_bed's own comment about why
    range(0, length, width) is wrong here)."""
    gc_contigs = [
        {
            "name": "c1",
            "length": 25,
            "window_bases": 10,
            "gc": [50.0, 50.0],
            "skew": [0.0, 0.0],
        },
    ]
    depth_regions = {
        "c1": [
            {"start": 0, "end": 10, "depth": 1.0, "name": None},
            # mosdepth's own last window also absorbs the remainder to 25.
            {"start": 10, "end": 25, "depth": 2.0, "name": None},
        ],
    }
    joined = join_windows(gc_contigs, depth_regions)
    assert [(w["start"], w["end"]) for w in joined] == [(0, 10), (10, 25)]
    assert [w["width"] for w in joined] == [10, 15]
```

- [ ] **Step 6: Run both new tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_gc_coverage.py -q`
Expected: FAIL — `NotImplementedError`

- [ ] **Step 7: Implement `join_windows`**

Replace the `raise NotImplementedError` body:

```python
def join_windows(
    gc_contigs: list[dict], depth_regions: dict[str, list[dict]]
) -> list[JoinedWindow]:
    joined: list[JoinedWindow] = []
    for contig in gc_contigs:
        name = contig["name"]
        gc_list = contig["gc"]
        rows = depth_regions.get(name)
        if rows and len(rows) == len(gc_list):
            # Depth rows carry their own (start, end) -- mosdepth's own
            # source of truth, not re-derived from window_bases, so a
            # future divergence between the two tilings would show up as a
            # length mismatch (the `else` branch below) rather than being
            # silently masked by recomputing bounds independently.
            for i, (row, gc) in enumerate(zip(rows, gc_list, strict=True)):
                joined.append(JoinedWindow(
                    contig=name, index=i,
                    start=row["start"], end=row["end"],
                    width=row["end"] - row["start"],
                    gc=gc, depth=row["depth"],
                ))
        else:
            # No depth rows for this contig (zero aligned reads, or a
            # length mismatch that should not happen against the same
            # reference) -- reconstruct window bounds from gc_tracks' own
            # tiling rule and default depth to 0. Mirrors
            # mosdepth_runner.build_windows_bed's rule exactly: the last
            # window absorbs the remainder.
            window_bases = contig["window_bases"]
            length = contig["length"]
            window_count = len(gc_list)
            for i, gc in enumerate(gc_list):
                start = i * window_bases
                end = length if i == window_count - 1 else start + window_bases
                joined.append(JoinedWindow(
                    contig=name, index=i, start=start, end=end,
                    width=end - start, gc=gc, depth=0.0,
                ))
    return joined
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_gc_coverage.py -q`
Expected: PASS, all 3 tests.

- [ ] **Step 9: Add a test for `bias_curve` skipping `gc=None` windows and
  omitting empty bins**

```python
def test_bias_curve_skips_none_gc_and_omits_empty_bins():
    joined = [
        _window("c", 0, 0, 10, gc=None, depth=99.0),  # excluded entirely
        _window("c", 1, 10, 20, gc=5.0, depth=3.0),
    ]
    curve = bias_curve(joined, bins=20)
    assert len(curve) == 1
    assert curve[0]["mean_depth"] == 3.0
```

- [ ] **Step 10: Run, verify pass, then run ruff**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_gc_coverage.py -q`
Expected: PASS, 4 tests.

Run: `ruff check --config backend/pyproject.toml backend/app/pipelines/gc_coverage.py backend/tests/pipelines/test_gc_coverage.py --fix`
Expected: clean, or only auto-fixed formatting.

- [ ] **Step 11: Commit**

```bash
git add backend/app/pipelines/gc_coverage.py backend/tests/pipelines/test_gc_coverage.py
git commit -m "feat(pipelines): join reference GC against alignment depth by window"
```

---

### Task 2: THREAD-mode handler + applier for the bias curve

**Files:**
- Create: `backend/app/queue/gc_coverage_handlers.py`
- Modify: `backend/app/queue/handlers.py` (import for `@handler` registration
  side effect — find the existing `from app.queue import mosdepth_handlers`
  or equivalent import block and add a sibling line)
- Modify: `backend/app/queue/results.py` (new applier + `_APPLIERS` entry)
- Test: `backend/tests/queue/test_gc_coverage_handlers.py`

**Interfaces:**
- Consumes: `gc_coverage.join_windows`, `gc_coverage.bias_curve` (Task 1).
- Produces:
  - Handler name `"gc_bias"`, registered via `@handler("gc_bias",
    mode=HandlerMode.THREAD, ...)`, function `compute_gc_bias(ctx:
    JobContext) -> dict`. Payload contract: `{"bam_id": str, "project_id":
    str, "job_id": str (from ctx), "gc_contigs": list[dict], "depth_regions":
    dict[str, list[dict]]}`. Returns `{"object_id": bam_id, "project_id":
    ..., "job_id": ..., "facts": {...}}` (no `workdir` — nothing written to
    disk by this handler; the bias curve is small enough to live directly in
    facts per V5).
  - `_apply_gc_bias(result: dict, *, owner: str) -> None` in `results.py`,
    registered in `_APPLIERS["gc_bias"]`.

- [ ] **Step 1: Write the handler test against a real payload shape**

```python
# backend/tests/queue/test_gc_coverage_handlers.py
from app.queue.gc_coverage_handlers import compute_gc_bias
from app.queue.registry import JobContext


def _ctx(payload):
    return JobContext(
        job_id="job1", payload=payload, epoch=0, attempts=1, owner="test-owner",
    )


def test_compute_gc_bias_returns_curve_facts():
    payload = {
        "bam_id": "abc123",
        "project_id": "proj1",
        "gc_contigs": [
            {
                "name": "c1", "length": 20, "window_bases": 10,
                "gc": [30.0, 70.0], "skew": [0.0, 0.0],
            },
        ],
        "depth_regions": {
            "c1": [
                {"start": 0, "end": 10, "depth": 5.0, "name": None},
                {"start": 10, "end": 20, "depth": 15.0, "name": None},
            ],
        },
    }
    result = compute_gc_bias(_ctx(payload))
    assert result["object_id"] == "abc123"
    assert result["facts"]["gc_bias_status"] == "ok"
    assert result["facts"]["gc_bias_curve"] == [
        {"gc_min": 30.0, "gc_max": 35.0, "mean_depth": 5.0, "window_count": 1},
        {"gc_min": 70.0, "gc_max": 75.0, "mean_depth": 15.0, "window_count": 1},
    ]
    assert "gc_bias_computed_at" in result["facts"]


def test_compute_gc_bias_requires_bam_id():
    from app.errors import PermanentError
    import pytest

    with pytest.raises(PermanentError):
        compute_gc_bias(_ctx({}))
```

Check `JobContext`'s actual field names first — read
`backend/app/queue/registry.py`'s `JobContext` dataclass (already partly
shown in Task exploration: `job_id, payload, epoch, attempts`, plus more
fields like `owner`) and match the test's constructor call to them exactly;
adjust the fixture above if a field is missing or named differently.

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/queue/test_gc_coverage_handlers.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the handler**

```python
"""gc_bias: the GC-vs-coverage bias curve for one BAM, joining its own
per-window depth (mosdepth) against its alignment target's per-window GC
(gc_tracks).

THREAD mode, not SUBPROCESS: no external tool runs here, only arithmetic
over two already-computed stored artifacts. THREAD mode also means this
handler cannot reach the database (see de_summary_handlers.py) -- the async
launcher (pipeline_service.launch_gc_bias) does every DB/file read and
passes fully-loaded gc_tracks contigs and mosdepth region rows in the
payload.

Read-only like coverage and bam_stats: no derived object, just facts merged
onto the BAM by _apply_gc_bias (results.py).
"""

from datetime import UTC, datetime

from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import gc_coverage
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


@handler(
    "gc_bias",
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=256, io=IoClass.LIGHT),
    max_attempts=2,
)
def compute_gc_bias(ctx: JobContext) -> dict:
    bam_id = ctx.payload.get("bam_id")
    if not bam_id:
        raise PermanentError("gc_bias requires a 'bam_id'")

    gc_contigs = ctx.payload.get("gc_contigs") or []
    depth_regions = ctx.payload.get("depth_regions") or {}

    joined = gc_coverage.join_windows(gc_contigs, depth_regions)
    curve = gc_coverage.bias_curve(joined)

    facts = {
        "gc_bias_status": "ok",
        "gc_bias_curve": curve,
        "gc_bias_computed_at": datetime.now(UTC).isoformat(),
    }

    log.info(
        "gc_bias_finished",
        job_id=ctx.job_id,
        bam_id=bam_id,
        bin_count=len(curve),
    )

    return {
        "object_id": bam_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
    }
```

- [ ] **Step 4: Register the handler module for its `@handler` side effect**

Read `backend/app/queue/handlers.py`'s import block (find where
`mosdepth_handlers` or a similar sibling module is imported purely for
registration) and add:

```python
from app.queue import gc_coverage_handlers  # noqa: F401
```

in the same style/location as the neighboring import.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/queue/test_gc_coverage_handlers.py -q`
Expected: PASS, both tests.

- [ ] **Step 6: Write the applier, mirroring `_apply_coverage`**

Read `backend/app/queue/results.py:1770` (`_apply_coverage`) first for the
exact merge pattern, then add a sibling function near it:

```python
async def _apply_gc_bias(result: dict, *, owner: str) -> None:
    """Record a GC-vs-coverage bias curve on the BAM it was computed for.

    Read-only like _apply_coverage: no files to ingest, just facts merged
    onto the object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("gc_bias_object_missing", object_id=object_id)
        return

    merged = {**obj.facts, **facts}
    await obj.set(
        {
            DataObject.facts: merged,
            DataObject.updated_at: datetime.now(UTC),
        }
    )
```

Match `DataObject.set(...)`'s exact call shape to what `_apply_coverage`
actually does at `results.py:1805-1810` (the exploration report paraphrased
it; read the live file and copy verbatim, since a missing `updated_at` or
a different `.set()` vs `.update()` call would not be caught by this task's
own test).

- [ ] **Step 7: Register in `_APPLIERS`**

Find the `_APPLIERS` dict (per exploration, ends around `results.py:3480+`,
with `"coverage": _apply_coverage` nearby) and add:

```python
    "gc_bias": _apply_gc_bias,
```

- [ ] **Step 8: Write an applier test**

Follow whatever pattern `backend/tests/queue/` or `backend/tests/api/` uses
for testing an existing applier like `_apply_coverage` — search for
`test_apply_coverage` or similar in the test tree first
(`grep -rn "_apply_coverage" backend/tests/`) and mirror its fixture setup
(likely a test DB with a real `DataObject`, calling `_apply_gc_bias`
directly, then re-fetching the object and asserting the facts landed).

Write the test into `backend/tests/queue/test_gc_coverage_handlers.py`
(same file, applier tests colocated with handler tests is the existing
convention per `test_mosdepth_handlers.py` — verify this by opening that
file's structure) or a separate `backend/tests/queue/test_results_gc_bias.py`
if `test_apply_coverage` lives in a `results`-specific test file instead —
match whichever the grep reveals.

- [ ] **Step 9: Run the full queue test subset, then ruff**

Run: `./backend/run-worktree-tests.sh tests/queue/ -q`
Expected: PASS.

Run: `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e --fix`
Expected: clean.

- [ ] **Step 10: Restart the worker (main repo root only) and commit**

```bash
git add backend/app/queue/gc_coverage_handlers.py backend/app/queue/handlers.py backend/app/queue/results.py backend/tests/queue/
git commit -m "feat(pipelines): compute and store the GC-bias curve for a BAM"
```

(Worker restart is a manual verification step for the developer running this
plan against the shared 5173 stack — `docker compose restart worker` from
the main repo root — not part of the commit; note it here so it is not
skipped before Task 3's manual check.)

---

### Task 3: Launcher — `launch_gc_bias`, doing every DB read the handler can't

**Files:**
- Modify: `backend/app/services/pipeline_service.py`
- Test: `backend/tests/services/test_pipeline_service_gc_bias.py` (new file
  — or append to an existing coverage-adjacent test file if one groups
  `launch_coverage`'s tests; check
  `backend/tests/services/test_pipeline_service*.py` for where
  `launch_coverage` itself is tested and colocate there instead if so)

**Interfaces:**
- Consumes: `reference_assembly.resolve_alignment_target_for_bam(bam, *,
  owner)` (existing, raises `ValidationError` on failure),
  `object_service.get_object`, `settings.coverage_dir`, `queue.enqueue`
  (existing patterns from `launch_coverage`, `pipeline_service.py:4944`).
- Produces: `async def launch_gc_bias(*, bam_id: PydanticObjectId, owner:
  str, resource_override: bool = False) -> Job` in `pipeline_service.py`,
  raising `ValidationError` with `details={"needs": "<action>"}` for each of
  V2's three preconditions, `ConflictError` on a duplicate in-flight job
  (mirroring `launch_coverage`'s dedup pattern).

- [ ] **Step 1: Read `launch_coverage` in full (`pipeline_service.py:4944`
  through its end around line 5063)** to confirm the exact helper names,
  since this task's implementation copies its shape closely: `import
  queue`/`object_service` inside the function, `refuse_if_over_budget`,
  `_resolve_readable` for reading a stored file's bytes/path (NOT needed
  here — this launcher reads JSON facts and a JSON report, not a blob — skip
  that helper), `queue.enqueue(..., dedup_key=..., resource_override=...)`.

- [ ] **Step 2: Write the failing precondition tests first (V2, failing
  direction first)**

```python
# backend/tests/services/test_pipeline_service_gc_bias.py
import pytest

from app.errors import ValidationError
from app.services import pipeline_service


@pytest.mark.asyncio
async def test_launch_gc_bias_refuses_when_alignment_target_unresolved(
    monkeypatch, ready_bam_factory,  # use whichever fixtures the sibling
    # launch_coverage tests use for a READY BAM with no derived_from parent
    # -- check test_pipeline_service*.py's existing fixtures before writing
    # a new one.
):
    bam = await ready_bam_factory(derived_from=[])  # no parent -> no target
    with pytest.raises(ValidationError, match="no recorded alignment target"):
        await pipeline_service.launch_gc_bias(bam_id=bam.id, owner=bam.owner)


@pytest.mark.asyncio
async def test_launch_gc_bias_refuses_when_reference_has_no_gc_tracks(
    ready_bam_factory, ready_reference_factory,
):
    reference = await ready_reference_factory(facts={})  # no gc_tracks fact
    bam = await ready_bam_factory(derived_from=[reference.id])
    with pytest.raises(ValidationError, match="gc tracks"):
        await pipeline_service.launch_gc_bias(bam_id=bam.id, owner=bam.owner)


@pytest.mark.asyncio
async def test_launch_gc_bias_refuses_when_bam_has_no_windowed_coverage(
    ready_bam_factory, ready_reference_factory,
):
    reference = await ready_reference_factory(
        facts={"gc_tracks": {"window_count": 500, "contigs": []}}
    )
    bam = await ready_bam_factory(derived_from=[reference.id], facts={})
    with pytest.raises(ValidationError, match="coverage"):
        await pipeline_service.launch_gc_bias(bam_id=bam.id, owner=bam.owner)


@pytest.mark.asyncio
async def test_launch_gc_bias_refuses_when_coverage_is_region_mode(
    ready_bam_factory, ready_reference_factory,
):
    """A region-mode coverage run is not on the shared window grid; the
    join cannot use it, and the card must treat this exactly like 'no
    coverage run' rather than crash on a shape mismatch."""
    reference = await ready_reference_factory(
        facts={"gc_tracks": {"window_count": 500, "contigs": []}}
    )
    bam = await ready_bam_factory(
        derived_from=[reference.id],
        facts={"coverage_status": "ok", "coverage_mode": "regions"},
    )
    with pytest.raises(ValidationError, match="windowed"):
        await pipeline_service.launch_gc_bias(bam_id=bam.id, owner=bam.owner)
```

Before finalizing this step, open `backend/tests/services/` and grep for
`ready_bam_factory`/`ready_reference_factory` (or whatever the actual
fixture names are — these are placeholders guessing at the codebase's
convention) to get their real signatures; adjust the test bodies to match
exactly. If no such factories exist, follow whatever `launch_coverage`'s own
existing tests do to build a READY BAM/reference pair.

- [ ] **Step 3: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_pipeline_service_gc_bias.py -q`
Expected: FAIL — `AttributeError: module 'pipeline_service' has no attribute 'launch_gc_bias'`

- [ ] **Step 4: Implement `launch_gc_bias`**

Add near `launch_coverage` in `pipeline_service.py`:

```python
GC_BIAS_MEM_MB = 256


async def launch_gc_bias(
    *,
    bam_id: PydanticObjectId,
    owner: str,
    resource_override: bool = False,
) -> Job:
    """Queue the GC-vs-coverage bias curve for one BAM.

    Read-only, like coverage and gc_tracks: no derived object, just a curve
    of facts merged onto the BAM. Three preconditions, each refused by name
    rather than auto-chained (V2, docs/superpowers/specs/
    2026-08-20-gc-coverage-visualizations-design.md): the BAM's alignment
    target must resolve, that target must have run gc_tracks, and this BAM
    must have a *windowed* (not region-mode) coverage run. The handler runs
    in a thread and cannot reach the database (mirrors de_summary_handlers'
    reasoning), so every read below happens here and is handed to the
    handler pre-loaded in the payload.
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly

    refuse_if_over_budget(
        declared_mb=GC_BIAS_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    bam = await object_service.get_object(bam_id, owner=owner)

    try:
        reference = await reference_assembly.resolve_alignment_target_for_bam(
            bam, owner=owner
        )
    except ValidationError:
        raise ValidationError(
            f"{bam.name!r} has no recorded alignment target -- gc_bias "
            f"needs to know which reference this BAM was aligned to.",
            details={"bam_id": str(bam.id)},
        ) from None

    gc_tracks_fact = (reference.facts or {}).get("gc_tracks")
    if not gc_tracks_fact or not gc_tracks_fact.get("contigs"):
        raise ValidationError(
            f"Reference {reference.name!r} has no GC tracks computed. "
            f"Run the Circos GC tracks analysis on it first.",
            details={"reference_id": str(reference.id), "needs": "analyze_gc_tracks"},
        )

    if (bam.facts or {}).get("coverage_status") != "ok":
        raise ValidationError(
            f"{bam.name!r} has no coverage computed. Run coverage depth "
            f"analysis on it first.",
            details={"bam_id": str(bam.id), "needs": "coverage"},
        )
    if (bam.facts or {}).get("coverage_mode") != "windows":
        raise ValidationError(
            f"{bam.name!r}'s coverage was computed against a target region "
            f"set, not uniform windows. gc_bias needs a windowed coverage "
            f"run -- run coverage depth analysis without a target BED.",
            details={"bam_id": str(bam.id), "needs": "coverage"},
        )

    report_name = bam.facts.get("coverage_report")
    if not report_name:
        raise ValidationError(
            f"{bam.name!r}'s coverage report is missing on disk. Re-run "
            f"coverage depth analysis.",
            details={"bam_id": str(bam.id), "needs": "coverage"},
        )
    report_path = settings.coverage_dir / str(bam.id) / report_name
    try:
        depth_regions = json.loads(report_path.read_text())["regions"]
    except (OSError, KeyError, ValueError) as e:
        raise ValidationError(
            f"{bam.name!r}'s coverage report could not be read. Re-run "
            f"coverage depth analysis.",
            details={"bam_id": str(bam.id), "needs": "coverage"},
        ) from e

    payload = {
        "bam_id": str(bam.id),
        "project_id": str(bam.project_id),
        "gc_contigs": gc_tracks_fact["contigs"],
        "depth_regions": depth_regions,
    }

    job = await queue.enqueue(
        "gc_bias",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=GC_BIAS_MEM_MB, io=IoClass.LIGHT),
        max_attempts=2,
        dedup_key=f"gc_bias:{bam.blob_sha256}:{reference.blob_sha256}",
        project_id=bam.project_id,
        object_id=bam.id,
        resource_override=resource_override,
    )
    if job is None:
        raise ConflictError(
            "GC bias analysis is already queued or running for this BAM",
            details={"bam_id": str(bam.id)},
        )

    log.info("gc_bias_launched", job_id=str(job.id), bam_id=str(bam.id))
    return job
```

Check that `json` is already imported at module scope in `pipeline_service.py`
(likely yes given other handlers serialize JSON); if not, add the import at
the top with the other stdlib imports, not inline.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_pipeline_service_gc_bias.py -q`
Expected: PASS, all 4 precondition tests.

- [ ] **Step 6: Write one success-path test** (a full READY BAM + reference
  with real gc_tracks/coverage facts and a real report file on disk,
  asserting `launch_gc_bias` returns a `Job` and the payload it enqueued
  carries `gc_contigs`/`depth_regions` — check how `test_pipeline_service*`
  asserts on enqueued payload for `launch_coverage`'s own success test and
  mirror that inspection mechanism, likely a queue spy/mock fixture).

- [ ] **Step 7: Run full services test subset, then ruff**

Run: `./backend/run-worktree-tests.sh tests/services/ -q`
Expected: PASS.

Run: `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e --fix`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_pipeline_service_gc_bias.py
git commit -m "feat(pipelines): launch gc_bias with three named refusal reasons"
```

---

### Task 4: API route

**Files:**
- Modify: `backend/app/api/v1/pipelines.py`
- Test: `backend/tests/api/test_gc_bias_route.py`

**Interfaces:**
- Consumes: `pipeline_service.launch_gc_bias` (Task 3).
- Produces: `POST /pipelines/gc-bias` (body `{"bam_id": PydanticObjectId}`
  → `JobOut`). No report GET route in stage 1 — the curve lives in facts
  (V5: "small enough... can live in facts"), read directly off the BAM's
  `ObjectDetail.facts.gc_bias_curve` the same way `bam_stats_coverage_bins`
  is read today (no dedicated report endpoint needed, confirm by checking
  whether `ObjectDetail`/the object-get route already returns `facts`
  wholesale — if it does, as `BamResults.tsx`'s `obj.facts` usage strongly
  implies, no new route is needed here beyond the launch POST).

- [ ] **Step 1: Read `launch_coverage`'s route (`pipelines.py:809-825`) as
  the template.**

- [ ] **Step 2: Write the route test**

```python
# backend/tests/api/test_gc_bias_route.py
def test_launch_gc_bias_route_calls_service(client, monkeypatch, ...):
    # Mirror whatever backend/tests/api/test_bam_stats_reports.py or the
    # coverage route's own test does for mocking pipeline_service and
    # asserting the JobOut shape -- read that file first and copy its
    # fixture/mock pattern exactly, since API route tests in this repo have
    # an established convention this task should not deviate from.
    ...
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/api/test_gc_bias_route.py -q`
Expected: FAIL — 404, route does not exist yet.

- [ ] **Step 4: Add the route**

```python
class GcBiasRequest(BaseModel):
    bam_id: PydanticObjectId


@router.post("/gc-bias", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_gc_bias_route(body: GcBiasRequest, owner: OwnerDep) -> JobOut:
    """Queue the GC-vs-coverage bias curve for a BAM.

    Read-only, like /coverage and /gc-tracks: no derived object, just a
    curve of facts merged onto the BAM. See pipeline_service.launch_gc_bias
    for the three preconditions this can refuse on.
    """
    job = await pipeline_service.launch_gc_bias(bam_id=body.bam_id, owner=owner)
    return JobOut.of(job)
```

Place it near `launch_coverage`'s route for locality.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/api/test_gc_bias_route.py -q`
Expected: PASS.

- [ ] **Step 6: ruff, then commit**

```bash
git add backend/app/api/v1/pipelines.py backend/tests/api/test_gc_bias_route.py
git commit -m "feat(api): expose the gc_bias launch route"
```

---

### Task 5: Suggestion card, failing direction first

**Files:**
- Modify: `backend/app/services/suggestion_service.py`
- Test: `backend/tests/services/test_suggestion_service.py`

**Interfaces:**
- Consumes: `ctx.alignment_target` (already resolved once per BAM object at
  `suggestion_service.py:2834-2845` for `build_consensus_card` — reuse it,
  do not re-resolve).
- Produces: `build_gc_bias_card(obj, alignment_target) -> SuggestionCard |
  None`, registered in `CARD_BUILDERS` as `("gc_bias", lambda obj, ctx:
  build_gc_bias_card(obj, ctx.alignment_target))`.

- [ ] **Step 1: Read `build_consensus_card` (`suggestion_service.py:1254`)
  and `build_coverage_card` (`:2401`) as templates — the new card combines
  both: gated on `alignment_target` like consensus, `FormatKind.BAM`-only
  like coverage.**

- [ ] **Step 2: Write the four card tests, failing direction first, each
  asserting on the message**

```python
# in backend/tests/services/test_suggestion_service.py, near the existing
# coverage/consensus card tests -- read those first for the fixture
# conventions (how a bare `obj` with specific `.facts`/`.format.kind` is
# constructed in this file) and match them.

def test_build_gc_bias_card_unavailable_when_no_alignment_target():
    obj = _bam_object(facts={})
    card = build_gc_bias_card(obj, None)
    assert card.status == CardStatus.UNAVAILABLE
    assert "alignment target" in card.reason.lower()


def test_build_gc_bias_card_unavailable_when_reference_has_no_gc_tracks():
    reference = _fasta_object(facts={})
    obj = _bam_object(facts={"coverage_status": "ok", "coverage_mode": "windows"})
    card = build_gc_bias_card(obj, reference)
    assert card.status == CardStatus.UNAVAILABLE
    assert "gc tracks" in card.reason.lower()


def test_build_gc_bias_card_unavailable_when_no_windowed_coverage():
    reference = _fasta_object(facts={"gc_tracks": {"contigs": [{}]}})
    obj = _bam_object(facts={})
    card = build_gc_bias_card(obj, reference)
    assert card.status == CardStatus.UNAVAILABLE
    assert "coverage" in card.reason.lower()


def test_build_gc_bias_card_available_when_all_preconditions_met():
    reference = _fasta_object(facts={"gc_tracks": {"contigs": [{}]}})
    obj = _bam_object(facts={"coverage_status": "ok", "coverage_mode": "windows"})
    card = build_gc_bias_card(obj, reference)
    assert card.status == CardStatus.AVAILABLE
    assert card.launch == {"endpoint": "/pipelines/gc-bias", "body": {"bam_id": str(obj.id)}}
```

Replace `_bam_object`/`_fasta_object` with whatever helper functions this
test file already uses for building a fake `DataObject` (grep the file for
existing card tests near `build_coverage_card`'s tests first).

- [ ] **Step 3: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q -k gc_bias`
Expected: FAIL — `NameError: build_gc_bias_card`

- [ ] **Step 4: Implement the card**

```python
def build_gc_bias_card(obj, alignment_target) -> SuggestionCard | None:
    """GC-vs-coverage bias curve: does depth vary with GC content, and if
    so, in which direction.

    `alignment_target` is the BAM's resolved reference, pre-resolved the
    same way build_consensus_card's `reference` is (an async provenance
    walk kept out of this synchronous builder). None means the walk raised
    -- no recorded target or an ambiguous one.

    Three distinct refusal reasons rather than one generic "unavailable",
    per the design doc's V2: each names the specific missing step so the
    user knows what to run next, and none of them auto-launches that step --
    two multi-minute jobs from one click on a chart card is a surprise no
    other card in this file springs.
    """
    if obj.format.kind is not FormatKind.BAM:
        return None

    title = "Coverage vs GC bias"
    description = (
        "Plot mean read depth against GC content across the reference, to "
        "show whether this library's coverage is biased by GC -- a dome "
        "shape is PCR amplification bias, a flat line is not."
    )

    if alignment_target is None:
        return SuggestionCard(
            kind="gc_bias",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason="This alignment has no recorded alignment target.",
        )

    gc_tracks_fact = (alignment_target.facts or {}).get("gc_tracks")
    if not gc_tracks_fact or not gc_tracks_fact.get("contigs"):
        return SuggestionCard(
            kind="gc_bias",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=(
                f"{alignment_target.name!r} has no GC tracks computed. "
                f"Run the Circos GC tracks analysis on it first."
            ),
        )

    facts = obj.facts or {}
    if facts.get("coverage_status") != "ok" or facts.get("coverage_mode") != "windows":
        return SuggestionCard(
            kind="gc_bias",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=(
                "This BAM has no windowed coverage computed. Run coverage "
                "depth analysis on it first (without a target region set)."
            ),
        )

    return SuggestionCard(
        kind="gc_bias",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        why=(
            "A dome peaking at mid-GC is PCR amplification bias, fixable "
            "at the bench; a flat curve rules that out as the cause of any "
            "coverage unevenness this run shows."
        ),
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/gc-bias",
            "body": {"bam_id": str(obj.id)},
        },
    )
```

- [ ] **Step 5: Register in `CARD_BUILDERS`**

Add, near `("coverage", ...)` at `suggestion_service.py:2714`:

```python
    ("gc_bias", lambda obj, ctx: build_gc_bias_card(obj, ctx.alignment_target)),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q -k gc_bias`
Expected: PASS, all 4.

- [ ] **Step 7: Run the whole suggestion_service test file (registry
  changes can break unrelated card-count assertions)**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q`
Expected: PASS. If a test asserts a fixed total card count for a BAM object,
update its expected count by +1.

- [ ] **Step 8: ruff, commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat(pipelines): offer the gc_bias card with three named refusals"
```

---

### Task 6: Registries — `running_now`, `provenance_walker`, `node_types`

**Files:**
- Modify: `backend/app/services/running_now.py`
- Modify: `backend/app/services/provenance_walker.py`
- Modify: `backend/app/pipelines/node_types.py`
- Test: existing `TestExhaustiveness` classes (no new test files — run the
  whole classes)

**Interfaces:**
- Consumes: `"gc_bias"` handler name (Task 2), `pipeline_service.launch_gc_bias`
  (Task 3).
- Produces: three registry entries; no new functions.

- [ ] **Step 1: `running_now.ENDPOINT_JOB_TYPES`** — add, near
  `"/pipelines/coverage": frozenset({"coverage"})` at `running_now.py:68`:

```python
    "/pipelines/gc-bias": frozenset({"gc_bias"}),
```

- [ ] **Step 2: `provenance_walker._NO_NARRATIVE_STEP`** — add, in the same
  set as `"coverage"` at `provenance_walker.py:173` (the "written back onto
  an existing object" group):

```python
        "gc_bias",
```

- [ ] **Step 3: `node_types.py` — spec + adapter**

Read `node_types.py:570-594`'s `"coverage": NodeTypeSpec(...)` in full first
(the earlier read stopped at line 594; read a bit further to see the closing
`outputs=()` and the `_launch_coverage` adapter at line 171 in full) so the
new entry matches its shape exactly. Add an adapter near `_launch_coverage`:

```python
async def _launch_gc_bias(*, inputs: dict, params: dict, owner: str):
    bam = inputs["alignment"]
    return await pipeline_service.launch_gc_bias(bam_id=bam.id, owner=owner)
```

(Check `_launch_coverage`'s exact `inputs` dict key naming and return
convention first — the exploration only located its line number, not its
body — and match this adapter's `inputs[...]` key and return statement to
whatever that function actually does.)

Add the `NODE_TYPES` entry, near `"coverage"`:

```python
    "gc_bias": NodeTypeSpec(
        label="Coverage vs GC bias",
        launch_name="pipeline_service.launch_gc_bias",
        run_kind=None,  # Read-only: facts only, no PipelineRun.
        launch=_launch_gc_bias,
        inputs=(
            PortSpec(
                "alignment",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
            ),
        ),
        outputs=(),
    ),
```

- [ ] **Step 4: Run every registry exhaustiveness/partition test as whole
  classes (per CLAUDE.md's "run the whole `TestExhaustiveness` class, not
  the one test a bug report names")**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -q`
Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q -k Exhaustive`
Run: `./backend/run-worktree-tests.sh tests/pipelines/test_assembler_registry.py -q`
(the last one is the general partition-pattern reference per CLAUDE.md, run
it as a sanity check that nothing in that unrelated registry broke — it
should already pass unchanged, confirming this task didn't touch it by
accident)

Also check for a provenance-walker-specific partition test:
`grep -rln "_NO_NARRATIVE_STEP" backend/tests/` and run whatever test file
that turns up.

Expected: all PASS.

- [ ] **Step 5: ruff, commit**

```bash
git add backend/app/services/running_now.py backend/app/services/provenance_walker.py backend/app/pipelines/node_types.py
git commit -m "feat(pipelines): register gc_bias in the canvas, running-now, and provenance registries"
```

---

### Task 7: Frontend chart

**Files:**
- Create: `frontend/src/components/GcBiasChart.tsx`
- Modify: `frontend/src/components/BamResults.tsx`
- Modify: `frontend/src/lib/metricInfo.ts`
- Modify: `frontend/src/api/types/alignment.ts` (or wherever `BamStatsFacts`
  lives — check `object.ts`/`alignment.ts` for where `coverage_report` etc.
  are typed and add `gc_bias_curve`/`gc_bias_status` alongside them)

**Interfaces:**
- Consumes: `obj.facts.gc_bias_curve: GcBiasBin[] | undefined`,
  `obj.facts.gc_bias_status: "ok" | undefined` (read directly from
  `ObjectDetail.facts`, no new API client function — no report route was
  added in Task 4).
- Produces: `GcBiasChart({ curve }: { curve: GcBiasBin[] })` component;
  `interface GcBiasBin { gc_min: number; gc_max: number; mean_depth: number;
  window_count: number }` in the types file.

- [ ] **Step 1: Locate where `BamStatsFacts` (or the relevant facts
  interface `BamResults.tsx` imports as `f`) is defined** — grep
  `frontend/src/api/types` for `interface BamStatsFacts` and add:

```typescript
export interface GcBiasBin {
  gc_min: number;
  gc_max: number;
  mean_depth: number;
  window_count: number;
}
```

near `CoverageWindow`/`CoverageReport` in `alignment.ts`, and add to
`BamStatsFacts`:

```typescript
  gc_bias_status?: "ok";
  gc_bias_curve?: GcBiasBin[];
```

- [ ] **Step 2: Write `GcBiasChart.tsx`**, modeled on `DepthHistogramChart.tsx`
  (read it in full first for its exact SVG scaffolding/padding constants —
  it was not fully read during exploration, only referenced by name; open
  it before writing this component so the axis/padding conventions match):

```tsx
import type { GcBiasBin } from "../api/types";
import { InfoMarker } from "./InfoMarker";

const W = 480;
const H = 180;

/**
 * Mean read depth per GC-content bin, across the reference.
 *
 * A dome peaking at mid-GC with both tails dropping is PCR amplification
 * bias -- fixable at the bench (a PCR-free prep, a different polymerase),
 * not by re-aligning. A flat line rules that out. A monotonic rise or fall
 * points at a capture/enrichment artifact rather than PCR. None of that is
 * visible in the depth histogram, which shows the distribution's shape but
 * not what it correlates with.
 *
 * Bins with no observed windows are omitted by the backend (gc_coverage.
 * bias_curve), not zero-filled -- so gaps in the line are GC content this
 * reference simply does not contain, not zero-depth regions.
 */
export function GcBiasChart({ curve }: { curve: GcBiasBin[] }) {
  if (!curve?.length) return null;

  const pad = { top: 10, right: 16, bottom, ...  // finish padding to match
  // DepthHistogramChart's own constants once that file has been read.
  };

  // ... plot as a line/area across gc_min..gc_max on the x axis (0-100%),
  // mean_depth on the y axis, following DepthHistogramChart's exact
  // axis-drawing and tick-label pattern rather than reinventing one.
}
```

This step is intentionally left to be finished against the real
`DepthHistogramChart.tsx` source at implementation time — write the
component by copying that file's padding/axis/tick structure verbatim and
substituting the bar-chart body for a line/area path through
`curve.map(b => ({x: (b.gc_min+b.gc_max)/2, y: b.mean_depth}))`.

- [ ] **Step 3: Add the METRIC_INFO entry**

In `frontend/src/lib/metricInfo.ts`, near `"ui.chart_contig_depth"`:

```typescript
  "ui.chart_gc_bias": {
    term: "Coverage vs GC bias",
    description:
      "Mean read depth across the reference, binned by GC content. A dome shape peaking at mid-GC is PCR amplification bias — fixable at the bench, not by re-aligning. A flat line means depth does not depend on GC. A monotonic rise or fall points at a capture or enrichment artifact instead of PCR.",
    computed:
      "Reference GC (gc_tracks) joined against per-window depth (mosdepth coverage) on their shared window grid, width-weighted per bin so a contig with wider windows is not over-represented.",
  },
```

- [ ] **Step 4: Wire into `BamResults.tsx`**, following the
  `f.coverage_report && <CoverageDepth .../>` pattern at line 255 exactly
  (same comment style, placed after it):

```tsx
      {/* Same independent-job reasoning as CoverageDepth above. Needs both
          this BAM's windowed coverage and its reference's gc_tracks, so it
          is gated on gc_bias's own fact rather than either prerequisite's. */}
      {f.gc_bias_status === "ok" && f.gc_bias_curve && (
        <GcBiasChart curve={f.gc_bias_curve} />
      )}
```

Add the import at the top: `import { GcBiasChart } from "./GcBiasChart";`

- [ ] **Step 5: Run `metricInfo.test.ts`**

Run (from wherever frontend tests run in this repo — check `package.json`
for the vitest script, likely `npm run test` or `npx vitest run` inside
`frontend/`):

```bash
cd frontend && npx vitest run src/lib/metricInfo.test.ts
```

Expected: PASS (the new key is covered).

- [ ] **Step 6: Manual verification at the worktree stack**

```bash
./ops/worktree-up.sh
```

Then open `http://localhost:5273` (never 5173 from a worktree), navigate to
a project with a completed alignment against a reference that has GC tracks
computed and windowed coverage computed, launch "Coverage vs GC bias" from
the Actions tab, and confirm the chart renders after the job completes. If
no such project/data exists in the worktree stack yet, run gc_tracks and
coverage on a real BAM first, then gc_bias.

- [ ] **Step 7: `./ops/worktree-up.sh --down` when done verifying (only from
  inside this worktree)**

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/GcBiasChart.tsx frontend/src/components/BamResults.tsx frontend/src/lib/metricInfo.ts frontend/src/api/types/alignment.ts
git commit -m "feat(frontend): show the GC-vs-coverage bias curve on BAM results"
```

---

### Stage 1 close-out

- [ ] Run the complete backend suite: `./backend/run-worktree-tests.sh tests/ -q`
- [ ] Run `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e` clean
- [ ] Rebase onto `origin/main`, verify `git diff origin/main...HEAD --stat`
  matches intent
- [ ] Push, open PR titled `feat(pipelines): add the coverage-vs-GC bias curve`,
  body says why (GC bias is invisible in the existing depth histogram) and
  `Closes #640`
- [ ] Label `type: feature`, `area: pipelines`, `area: frontend`
- [ ] Poll `gh pr checks` to green, `gh pr merge --rebase --delete-branch`
- [ ] **Do not start Stage 2 until this is merged and the chart has been
  looked at against real data** — per the high-level plan's own instruction.

---

## Stage 2 — the blobplot (#641)

Only after Stage 1 is merged. Requires a fresh `git pull`/rebase onto `main`
first so `gc_coverage.py`, the `gc_bias` handler/applier, and the card
pattern from Stage 1 are present to build on.

### Task 8: `gc_coverage.per_contig` — GC and depth per contig, with the
cumulative-length cap

**Files:**
- Modify: `backend/app/pipelines/gc_coverage.py`
- Modify: `backend/tests/pipelines/test_gc_coverage.py`

**Interfaces:**
- Consumes: `JoinedWindow`, `join_windows` (Task 1/Stage 1, already merged).
- Produces:
  - `per_contig(joined: list[JoinedWindow]) -> list[dict]` — each dict is
    `{"contig": str, "gc": float | None, "mean_depth": float, "length":
    int, "window_count": int}`, one row per contig present in `joined`,
    unsorted (caller sorts).
  - `cap_by_cumulative_length(contigs: list[dict], *, target_fraction:
    float = 0.99, hard_ceiling: int = 5000) -> tuple[list[dict], int]` —
    returns `(kept, dropped_count)`. `contigs` must carry a `"length"` key.

- [ ] **Step 1: Write the per-contig GC aggregation test (V3: `Σ(gc_count) /
  Σ(window_bases)`, not a plain mean of per-window GC percentages, since
  each window's GC percentage was itself already a ratio over a possibly
  different-width window)**

```python
def test_per_contig_gc_is_base_weighted_not_unweighted_mean():
    """Two windows, one 10bp at GC=100% (10 G/C bases of 10) and one 90bp at
    GC=0% (0 of 90). The unweighted mean of percentages is 50%. The correct
    per-contig GC, weighted by each window's base count, is
    (10 + 0) / (10 + 90) = 10%.
    """
    joined = [
        _window("c1", 0, 0, 10, gc=100.0, depth=5.0),
        _window("c1", 1, 10, 100, gc=0.0, depth=5.0),
    ]
    rows = per_contig(joined)
    assert len(rows) == 1
    assert rows[0]["contig"] == "c1"
    assert rows[0]["gc"] == pytest.approx(10.0)
    assert rows[0]["length"] == 100
    assert rows[0]["window_count"] == 2


def test_per_contig_mean_depth_is_width_weighted():
    joined = [
        _window("c1", 0, 0, 10, gc=50.0, depth=100.0),
        _window("c1", 1, 10, 110, gc=50.0, depth=1.0),
    ]
    rows = per_contig(joined)
    # (100*10 + 1*100) / 110 = 1100/110 = 10.0
    assert rows[0]["mean_depth"] == pytest.approx(10.0)


def test_per_contig_skips_none_gc_windows_in_the_gc_average_but_not_depth():
    """An all-N window contributes no G/C or total bases to the GC ratio
    (it truly has none), but its depth still counts toward mean_depth --
    depth was measured regardless of base composition."""
    joined = [
        _window("c1", 0, 0, 10, gc=None, depth=8.0),
        _window("c1", 1, 10, 20, gc=60.0, depth=2.0),
    ]
    rows = per_contig(joined)
    assert rows[0]["gc"] == 60.0
    assert rows[0]["mean_depth"] == pytest.approx((8.0 * 10 + 2.0 * 10) / 20)
```

Note: `per_contig` needs each window's raw G/C base count, not just its
percentage, to correctly weight the GC average — but `JoinedWindow` only
carries `gc` as a percentage (matching gc_tracks' own stored shape, which is
already rounded to 2 decimals and has lost the raw count). **Decide and
document this precisely before implementing**: reconstruct an approximate
G/C count as `round(gc / 100 * width)` (introduces float rounding error
proportional to window count, acceptable since gc_tracks' own stored value
is already rounded) — write this as a comment in the implementation, and
adjust the test's `pytest.approx` tolerance if this reconstruction makes the
first test's expected `10.0` inexact (it should still be exact for the
round-number fixture above; if it is not when actually run, that is a sign
the reconstruction needs a different rounding rule, not a reason to loosen
the test's tolerance).

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_gc_coverage.py -q -k per_contig`
Expected: FAIL — `NameError: per_contig`

- [ ] **Step 3: Implement `per_contig`**

```python
def per_contig(joined: list[JoinedWindow]) -> list[dict]:
    """One row per contig: GC (base-weighted, per V3 -- Σgc_count/Σwidth,
    reconstructing each window's approximate G/C base count from its stored
    percentage since gc_tracks does not retain the raw count), mean depth
    (width-weighted, same reasoning as bias_curve), total length, and how
    many windows contributed.

    Feeds #641's blobplot: each contig becomes one scatter point, GC on one
    axis and mean_depth on the other, point area proportional to length.
    """
    by_contig: dict[str, list[JoinedWindow]] = {}
    for w in joined:
        by_contig.setdefault(w["contig"], []).append(w)

    rows = []
    for contig, windows in by_contig.items():
        gc_bases = 0.0
        gc_total_bases = 0.0
        depth_sum = 0.0
        width_sum = 0
        for w in windows:
            width_sum += w["width"]
            depth_sum += w["depth"] * w["width"]
            if w["gc"] is not None:
                gc_bases += (w["gc"] / 100.0) * w["width"]
                gc_total_bases += w["width"]
        rows.append({
            "contig": contig,
            "gc": round(gc_bases / gc_total_bases * 100.0, 2) if gc_total_bases else None,
            "mean_depth": round(depth_sum / width_sum, 4) if width_sum else 0.0,
            "length": width_sum,
            "window_count": len(windows),
        })
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_gc_coverage.py -q -k per_contig`
Expected: PASS, all 3.

- [ ] **Step 5: Write the cumulative-length cap tests — the omission count
  is the load-bearing assertion (V4)**

```python
def test_cap_by_cumulative_length_keeps_contigs_covering_target_fraction():
    contigs = [
        {"contig": "big", "length": 900, "gc": 50.0, "mean_depth": 10.0, "window_count": 9},
        {"contig": "small", "length": 90, "gc": 50.0, "mean_depth": 10.0, "window_count": 1},
        {"contig": "tiny", "length": 10, "gc": 50.0, "mean_depth": 10.0, "window_count": 1},
    ]
    kept, dropped = cap_by_cumulative_length(contigs, target_fraction=0.99, hard_ceiling=5000)
    # Sorted descending by length: big(900)=90%, +small(90)=99% exactly hits
    # target; tiny should be dropped.
    assert {c["contig"] for c in kept} == {"big", "small"}
    assert dropped == 1


def test_cap_by_cumulative_length_reports_true_dropped_count_not_a_log_line():
    """V4's whole point: a contaminant is often many small contigs, so this
    count must be exact and always available to the caller -- it becomes
    the chart's 'M shorter contigs omitted' line."""
    contigs = [{"contig": f"c{i}", "length": 1, "gc": 50.0, "mean_depth": 1.0, "window_count": 1}
               for i in range(100)]
    contigs[0]["length"] = 10_000  # one huge contig covers >99% alone
    kept, dropped = cap_by_cumulative_length(contigs, target_fraction=0.99, hard_ceiling=5000)
    assert dropped == 99
    assert len(kept) == 1


def test_cap_by_cumulative_length_hard_ceiling_binds_even_under_target_fraction():
    """A pathologically fragmented assembly where reaching 99% would need
    more than hard_ceiling contigs -- the ceiling must still bind, and the
    dropped count must still be exact."""
    contigs = [{"contig": f"c{i}", "length": 1, "gc": 50.0, "mean_depth": 1.0, "window_count": 1}
               for i in range(20)]
    kept, dropped = cap_by_cumulative_length(contigs, target_fraction=0.99, hard_ceiling=5)
    assert len(kept) == 5
    assert dropped == 15
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_gc_coverage.py -q -k cap_by_cumulative`
Expected: FAIL — `NameError`

- [ ] **Step 7: Implement `cap_by_cumulative_length`**

```python
def cap_by_cumulative_length(
    contigs: list[dict], *, target_fraction: float = 0.99, hard_ceiling: int = 5000
) -> tuple[list[dict], int]:
    """Keep the longest contigs covering `target_fraction` of total bases,
    capped at `hard_ceiling` regardless.

    Sorted descending by length, so the cut point is always the shortest
    contigs -- a contaminant that is many SMALL contigs is exactly what this
    can drop (V4), which is why the dropped count is returned rather than
    only logged: the caller must always be able to say what was omitted.
    """
    by_length = sorted(contigs, key=lambda c: c["length"], reverse=True)
    total = sum(c["length"] for c in by_length)
    if total <= 0:
        return [], 0

    target = total * target_fraction
    kept = []
    cumulative = 0
    for c in by_length:
        if len(kept) >= hard_ceiling:
            break
        kept.append(c)
        cumulative += c["length"]
        if cumulative >= target:
            break

    return kept, len(contigs) - len(kept)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_gc_coverage.py -q`
Expected: PASS, all tests in the file (both stage 1 and stage 2 so far).

- [ ] **Step 9: ruff, commit**

```bash
git add backend/app/pipelines/gc_coverage.py backend/tests/pipelines/test_gc_coverage.py
git commit -m "feat(pipelines): aggregate the GC-depth join per contig with a length cap"
```

---

### Task 9: Handler extension + report route (per-contig array is too big
for facts — V5)

**Files:**
- Modify: `backend/app/queue/gc_coverage_handlers.py`
- Modify: `backend/app/queue/results.py` (`_apply_gc_bias` — rename
  semantics or add a second applier; see Step 3 below for the decision)
- Modify: `backend/app/api/v1/pipelines.py` (report GET route, mirroring
  `/coverage/{object_id}/report`)
- Modify: `backend/app/config.py` (new `gc_bias_dir` property, or reuse an
  existing dir — see Step 1)
- Test: extend `backend/tests/queue/test_gc_coverage_handlers.py`, add
  `backend/tests/api/test_gc_bias_report_route.py`

**Interfaces:**
- Consumes: `gc_coverage.per_contig`, `gc_coverage.cap_by_cumulative_length`
  (Task 8).
- Produces: handler payload gains `"contigs"` in its `facts`-vs-`report`
  split — the bias curve stays in facts (small, stage 1), the per-contig
  array moves to a JSON report on disk (V5: "bounded by V4 but can still be
  thousands of entries"). New facts: `gc_blob_status`, `gc_blob_report`
  (filename), `gc_blob_contig_count` (kept count), `gc_blob_dropped_count`.
  `GET /pipelines/gc-bias/{object_id}/report` returns
  `{"contigs": [...], "dropped_count": int, "kept_count": int}`.

- [ ] **Step 1: Decide the directory.** Read `config.py`'s `coverage_dir`
  property (`config.py:459`) as the template and add a sibling:

```python
    @property
    def gc_bias_dir(self) -> Path:
        """Generated per-contig GC-vs-depth reports (the blobplot JSON),
        keyed by BAM object id.

        Outside objects/ deliberately, same rationale as coverage_dir: this
        is derivative and regenerable from the BAM's coverage report and its
        reference's gc_tracks, so content-addressing it would buy
        deduplication of something never shared and cost a blob record per
        run.
        """
        return self.bioinfo_home / "gc_bias"
```

- [ ] **Step 2: Write the extended handler test** — same `compute_gc_bias`
  function now also writes a report file. Update
  `test_compute_gc_bias_returns_curve_facts` (Task 2's test) to also assert
  on the new facts, and add:

```python
def test_compute_gc_bias_writes_capped_per_contig_report(tmp_path, monkeypatch):
    # Monkeypatch settings.gc_bias_dir to tmp_path (check how
    # test_mosdepth_handlers.py mocks settings.coverage_dir for its own
    # report-writing test and mirror that exact mechanism).
    payload = {
        "bam_id": "abc123",
        "project_id": "proj1",
        "gc_contigs": [
            {"name": "c1", "length": 20, "window_bases": 10,
             "gc": [30.0, 70.0], "skew": [0.0, 0.0]},
        ],
        "depth_regions": {
            "c1": [
                {"start": 0, "end": 10, "depth": 5.0, "name": None},
                {"start": 10, "end": 20, "depth": 15.0, "name": None},
            ],
        },
    }
    result = compute_gc_bias(_ctx(payload))
    assert result["facts"]["gc_blob_status"] == "ok"
    assert result["facts"]["gc_blob_contig_count"] == 1
    assert result["facts"]["gc_blob_dropped_count"] == 0
    assert "gc_blob_report" in result["facts"]
```

- [ ] **Step 3: Run to verify it fails**, then extend `compute_gc_bias`:

```python
    ctx.payload  # unchanged read of gc_contigs/depth_regions

    joined = gc_coverage.join_windows(gc_contigs, depth_regions)
    curve = gc_coverage.bias_curve(joined)
    contig_rows = gc_coverage.per_contig(joined)
    kept, dropped = gc_coverage.cap_by_cumulative_length(contig_rows)

    report_dir = settings.gc_bias_dir / str(bam_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_name = "gc_blob.json"
    (report_dir / report_name).write_text(json.dumps({
        "contigs": kept,
        "dropped_count": dropped,
        "kept_count": len(kept),
    }))

    facts = {
        "gc_bias_status": "ok",
        "gc_bias_curve": curve,
        "gc_bias_computed_at": datetime.now(UTC).isoformat(),
        "gc_blob_status": "ok",
        "gc_blob_report": report_name,
        "gc_blob_contig_count": len(kept),
        "gc_blob_dropped_count": dropped,
    }
```

Add `import json` and `from app.config import settings` to the handler
module's imports.

- [ ] **Step 4: Decide on the applier.** `_apply_gc_bias` (Task 2, Stage 1)
  already merges whatever's in `result["facts"]` onto the object — since the
  new facts are just more keys in the same dict, **no applier change is
  needed**; `_apply_gc_bias` already handles it generically. Confirm this by
  re-reading the Task 2 applier body: if it does `{**obj.facts, **facts}`
  with no per-key allowlist, it is already correct. If it turns out to
  allowlist specific keys (unlikely given `_apply_coverage`'s own pattern),
  extend it.

- [ ] **Step 5: Run handler tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/queue/test_gc_coverage_handlers.py -q`
Expected: PASS.

- [ ] **Step 6: Add the report GET route**, mirroring
  `get_coverage_report` (`pipelines.py:828-850`) exactly, substituting
  `gc_bias_dir` and `gc_blob.json`:

```python
@router.get("/gc-bias/{object_id}/report")
async def get_gc_bias_report(object_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """Serve the per-contig GC-vs-depth report for a BAM (the blobplot).

    Same discard-the-read-just-for-404 reasoning as get_coverage_report.
    """
    await object_service.get_object(object_id, owner=owner)

    root = (settings.gc_bias_dir / str(object_id)).resolve()
    target = (root / "gc_blob.json").resolve()

    if not target.is_relative_to(root) or not target.is_file():
        raise NotFoundError(f"No GC-bias report for object {object_id}")

    return json.loads(target.read_text())
```

- [ ] **Step 7: Write the route test**, mirroring whatever tests
  `get_coverage_report` already has in `backend/tests/api/`.

- [ ] **Step 8: Run tests, ruff, commit**

Run: `./backend/run-worktree-tests.sh tests/queue/ tests/api/ -q`
Run: `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e --fix`

```bash
git add backend/app/queue/gc_coverage_handlers.py backend/app/config.py backend/app/api/v1/pipelines.py backend/tests/queue/ backend/tests/api/
git commit -m "feat(pipelines): store the capped per-contig blobplot report"
```

Restart the worker manually before frontend verification later:
`docker compose restart worker` from the main repo root.

---

### Task 10: Frontend scatter (`ContigBlobChart.tsx`)

**Files:**
- Create: `frontend/src/components/ContigBlobChart.tsx`
- Modify: `frontend/src/components/BamResults.tsx`
- Modify: `frontend/src/lib/metricInfo.ts`
- Modify: `frontend/src/api/types/alignment.ts`
- Modify: `frontend/src/api/client.ts`

**Interfaces:**
- Consumes: `api.request<T>(path)` helper (existing, per
  `coverageReport`'s own one-liner at `client.ts:1232-1233`).
- Produces: `api.gcBlobReport(objectId: string): Promise<GcBlobReport>`;
  `interface GcBlobReport { contigs: GcBlobContig[]; dropped_count: number;
  kept_count: number }`; `interface GcBlobContig { contig: string; gc:
  number | null; mean_depth: number; length: number; window_count: number
  }`; `ContigBlobChart({ objectId }: { objectId: string })` — self-fetching,
  following the `CoverageDepth.tsx` pattern (Task 7 read that file in full
  already; reuse its `useQuery` scaffolding directly).

- [ ] **Step 1: Add the client function**, near `coverageReport` at
  `client.ts:1232`:

```typescript
  gcBlobReport: (objectId: string) =>
    request<GcBlobReport>(`/pipelines/gc-bias/${objectId}/report`),
```

Add `GcBlobReport` to the import list at the top of `client.ts` (near line
36-37 where `CoverageReport`/`FeatureCoverageReport` are imported).

- [ ] **Step 2: Add the types**, near `CoverageReport` in `alignment.ts`:

```typescript
/** One contig's aggregate GC and depth, for the blobplot. */
export interface GcBlobContig {
  contig: string;
  /** Base-weighted GC percentage across this contig's windows; null if
   *  every window was unscoreable (all-N). */
  gc: number | null;
  mean_depth: number;
  length: number;
  window_count: number;
}

/**
 * `GET /pipelines/gc-bias/{object_id}/report`'s full body.
 *
 * `contigs` is capped by cumulative length (the longest contigs covering
 * 99% of total bases, per V4) -- `dropped_count` is how many shorter
 * contigs were omitted, and MUST be shown whenever non-zero: a contaminant
 * is often many small contigs, so a naive cap can drop exactly the cluster
 * this chart exists to find, and a clean-looking plot must be
 * distinguishable from one whose contamination was truncated away.
 */
export interface GcBlobReport {
  contigs: GcBlobContig[];
  dropped_count: number;
  kept_count: number;
}
```

Add `gc_blob_status?: "ok";` and `gc_blob_dropped_count?: number;` to
`BamStatsFacts` alongside the Task 7 additions.

- [ ] **Step 3: Write `ContigBlobChart.tsx`**

```tsx
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { InfoMarker } from "./InfoMarker";

const W = 480;
const H = 360;
// Point area (not radius) proportional to contig length, per V4/#641: radius
// scaling exaggerates large contigs quadratically, and the whole reason to
// weight by length is to show whether an off-cluster group is a trivial or
// substantial fraction of the assembly -- that comparison only holds if
// area, the visually-integrated quantity, tracks length linearly.
const MIN_RADIUS = 1.5;
const MAX_RADIUS = 14;

/**
 * Per-contig GC vs coverage -- the unlabelled blobplot.
 *
 * Each point is a contig, GC on the x axis, mean depth (log scale) on the
 * y axis, point AREA proportional to contig length. A clean assembly forms
 * one cluster; a contaminant -- a different organism's DNA at a small
 * fraction of total bases but often many contigs -- sits at a different
 * GC/depth coordinate and separates visually, even when every summary
 * statistic looks fine.
 *
 * Unlike ContigDepthChart's 50-bar cap, there is no readability ceiling
 * here -- clustering gets clearer with more points, not noisier. The cap
 * that does apply (cumulative length, V4) exists only so a report with
 * hundreds of thousands of contigs stays a bounded document; it is NOT a
 * "top N by size" readability cap, and dropping it changes what a clean
 * plot means -- which is why the omission line below always renders when
 * anything was dropped.
 *
 * Clusters are NOT taxonomically labelled -- that is a true BlobTools plot
 * and needs classification against a database (#625). Reading a cluster as
 * identified rather than merely separated is the mistake this InfoMarker
 * exists to head off.
 */
export function ContigBlobChart({ objectId }: { objectId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["gc-blob", objectId],
    queryFn: () => api.gcBlobReport(objectId),
  });

  return (
    <div className="section">
      <div
        className="section-title"
        style={{ display: "flex", alignItems: "center", gap: 8 }}
      >
        <span>GC vs coverage (blobplot)</span>
        <InfoMarker metric="ui.chart_gc_blob" />
      </div>

      {isLoading ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          Loading the per-contig report…
        </div>
      ) : isError || !data || !data.contigs.length ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          Couldn't load the GC-vs-coverage report.
        </div>
      ) : (
        <BlobScatter contigs={data.contigs} droppedCount={data.dropped_count} />
      )}
    </div>
  );
}

function BlobScatter({
  contigs,
  droppedCount,
}: {
  contigs: import("../api/types").GcBlobContig[];
  droppedCount: number;
}) {
  const pad = { top: 10, right: 16, bottom: 28, left: 44 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;

  const scored = contigs.filter((c) => c.gc != null);
  const depths = scored.map((c) => Math.max(c.mean_depth, 0.1));
  const minDepth = Math.min(...depths);
  const maxDepth = Math.max(...depths);
  const lengths = scored.map((c) => c.length);
  const minLen = Math.min(...lengths);
  const maxLen = Math.max(...lengths);

  const x = (gc: number) => pad.left + (gc / 100) * plotW;
  // Log scale on depth, per the spec -- a linear axis compresses the low-
  // depth contamination cluster against the axis when one organism is much
  // deeper than the other, which is the common case this plot targets.
  const logMin = Math.log10(Math.max(minDepth, 0.1));
  const logMax = Math.log10(Math.max(maxDepth, minDepth * 10));
  const y = (depth: number) => {
    const t = (Math.log10(Math.max(depth, 0.1)) - logMin) / (logMax - logMin || 1);
    return pad.top + plotH - t * plotH;
  };

  // Area, not radius, proportional to length: radius = sqrt(area).
  const areaFor = (length: number) => {
    if (maxLen === minLen) return (MIN_RADIUS + MAX_RADIUS) / 2;
    const t = (length - minLen) / (maxLen - minLen);
    const minArea = Math.PI * MIN_RADIUS ** 2;
    const maxArea = Math.PI * MAX_RADIUS ** 2;
    return Math.sqrt((minArea + t * (maxArea - minArea)) / Math.PI);
  };

  return (
    <div style={{ marginTop: 8 }}>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ maxWidth: W, display: "block" }}>
        <line
          x1={pad.left} x2={pad.left + plotW} y1={pad.top + plotH} y2={pad.top + plotH}
          stroke="var(--border)"
        />
        <line
          x1={pad.left} x2={pad.left} y1={pad.top} y2={pad.top + plotH}
          stroke="var(--border)"
        />
        {[0, 25, 50, 75, 100].map((tick) => (
          <text key={tick} x={x(tick)} y={pad.top + plotH + 14} fontSize={9} textAnchor="middle" fill="var(--text-faint)">
            {tick}%
          </text>
        ))}
        <text x={pad.left - 6} y={pad.top + 4} fontSize={9} textAnchor="end" fill="var(--text-faint)">
          {maxDepth.toFixed(0)}×
        </text>
        <text x={pad.left - 6} y={pad.top + plotH} fontSize={9} textAnchor="end" fill="var(--text-faint)">
          {minDepth.toFixed(1)}×
        </text>

        {scored.map((c) => (
          <circle
            key={c.contig}
            cx={x(c.gc as number)}
            cy={y(c.mean_depth)}
            r={areaFor(c.length)}
            fill="var(--accent)"
            opacity={0.55}
          >
            <title>
              {c.contig}: {(c.gc as number).toFixed(1)}% GC, {c.mean_depth.toFixed(1)}× depth, {c.length.toLocaleString()} bp
            </title>
          </circle>
        ))}
      </svg>

      <div style={{ color: "var(--text-faint)", fontSize: 12, marginTop: 4 }}>
        showing {contigs.length.toLocaleString()} contigs covering 99% of bases
        {droppedCount > 0
          ? `; ${droppedCount.toLocaleString()} shorter contig${droppedCount === 1 ? "" : "s"} omitted`
          : ""}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Add the METRIC_INFO entry**, near `ui.chart_gc_bias`:

```typescript
  "ui.chart_gc_blob": {
    term: "GC vs coverage (blobplot)",
    description:
      "Each point is a contig, positioned by GC content and mean depth (log scale), sized by area proportional to length. A clean assembly forms one cluster; a contaminant -- a different organism present at a small fraction of total bases but often many contigs -- sits at a different coordinate and separates visually, even when every summary statistic looks acceptable. Clusters are NOT taxonomically labelled; this shows separation, not identity.",
    computed:
      "Aggregated from the same GC/depth window join as the bias curve above, capped to the longest contigs covering 99% of total bases plus a hard ceiling. The omission line always shows how many shorter contigs were dropped, since a contaminant is often many small contigs -- without it a clean-looking plot cannot be told from one whose contamination was truncated away.",
  },
```

- [ ] **Step 5: Wire into `BamResults.tsx`**, after the `GcBiasChart` block
  from Task 7:

```tsx
      {/* Same independent-job reasoning as GcBiasChart above -- gated on
          its own fact since it is a second, separately-launched aggregation
          of the same underlying join. */}
      {f.gc_blob_status === "ok" && <ContigBlobChart objectId={obj.id} />}
```

Add `import { ContigBlobChart } from "./ContigBlobChart";`

- [ ] **Step 6: Run `metricInfo.test.ts`**

```bash
cd frontend && npx vitest run src/lib/metricInfo.test.ts
```

Expected: PASS.

- [ ] **Step 7: Manual verification** — same worktree stack as Task 7,
  confirm the scatter renders after a `gc_bias` job completes (it produces
  both the curve and the per-contig report in one job, per Task 9), check
  the omission line appears when a fragmented reference is used and reads 0
  contigs dropped for a small/clean test fixture.

- [ ] **Step 8: `./ops/worktree-up.sh --down`, then commit**

```bash
git add frontend/src/components/ContigBlobChart.tsx frontend/src/components/BamResults.tsx frontend/src/lib/metricInfo.ts frontend/src/api/types/alignment.ts frontend/src/api/client.ts
git commit -m "feat(frontend): add the unlabelled per-contig GC-vs-coverage blobplot"
```

---

### Stage 2 close-out

- [ ] Run the complete backend suite: `./backend/run-worktree-tests.sh tests/ -q`
- [ ] Run `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e` clean
- [ ] Run `cd frontend && npx vitest run` (whole suite, not just metricInfo)
- [ ] Rebase onto `origin/main`, verify `git diff origin/main...HEAD --stat`
  matches intent
- [ ] Push, open PR titled
  `feat(pipelines): add the per-contig GC-vs-coverage blobplot`, body
  explains the contamination-detection use case and `Closes #641`
- [ ] Label `type: feature`, `area: pipelines`, `area: frontend`
- [ ] Poll `gh pr checks` to green, `gh pr merge --rebase --delete-branch`

---

## Self-review notes (from the plan author)

**Spec coverage check against `2026-08-20-gc-coverage-visualizations-design.md`:**
- V1 (shared grid, width weighting, tested with unequal contigs) → Task 1,
  Steps 1-8.
- V2 (three named refusals, no auto-chaining) → Task 3 (launcher) + Task 5
  (card), both failing-direction-first.
- V3 (per-contig GC from the same windows) → Task 8.
- V4 (cumulative-length cap, dropped count always reported, area not
  radius) → Task 8 (cap logic) + Task 10 (chart rendering + omission line).
- V5 (facts + report, `_NO_NARRATIVE_STEP`, curve in facts / per-contig in a
  report) → Task 2 (facts), Task 6 (`_NO_NARRATIVE_STEP`), Task 9 (report
  route for the larger per-contig array).
- The "depth-0, not dropped" join rule → Task 1, Steps 5-8.
- The regions-mode exclusion (my own addition during exploration, not
  explicitly in the spec text but required by it) → Task 3 Step 2's fourth
  test, and the launcher/card implementations' `coverage_mode` checks.

**Known gaps intentionally left for the implementer to resolve against live
code, flagged inline above rather than guessed:**
- Exact `JobContext` field names (Task 2, Step 1) — the exploration agent
  paraphrased this dataclass rather than quoting it verbatim.
- Exact `_apply_coverage` `.set()` call shape (Task 2, Step 6) — read and
  copy verbatim rather than trust the paraphrase.
- Existing test fixture names/conventions in
  `test_pipeline_service*.py`/`test_suggestion_service.py`
  (Tasks 3 and 5) — these test files were not read in full during
  exploration; grep for the sibling `launch_coverage`/`build_coverage_card`
  tests before writing new ones, and match their fixtures exactly rather
  than inventing new fixture names.
- `_launch_coverage`'s adapter body (Task 6, Step 3) — only its line number
  was found; read it before writing `_launch_gc_bias`.
- `DepthHistogramChart.tsx`'s exact padding constants (Task 7, Step 2) —
  read the file before writing `GcBiasChart.tsx`; the task deliberately
  stops short of inventing padding numbers that might not match the
  sibling chart's visual rhythm.
- Whether `frontend`'s test runner command is `npm run test` or a direct
  `vitest` invocation — check `frontend/package.json`'s scripts before Task
  7/10's verification steps.

None of these gaps block starting the plan — each is resolved by reading one
specific file immediately before the step that needs it, which is cheaper
and more reliable than guessing now and finding out wrong at review time.
