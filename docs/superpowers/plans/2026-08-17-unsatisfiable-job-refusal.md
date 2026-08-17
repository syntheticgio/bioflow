# Unsatisfiable Job Refusal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refuse at launch any pipeline job whose declared `mem_mb` exceeds the admission budget, so it can never queue forever unclaimed.

**Architecture:** A pure budget helper in `resource_limit_service` becomes the single definition of the stable admission ceiling, used by both the worker and a new exact declared-vs-budget check. The check runs in the two pipeline launch paths, outside the existing `estimate is not None` guard, and reuses the `resource_override` "Launch anyway" escape that already carries through to `claim.lua`.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (MongoDB), Redis + Lua, pytest.

**Spec:** `docs/superpowers/specs/2026-08-17-unsatisfiable-job-refusal-design.md`

## Global Constraints

- **Run tests from the worktree with `./backend/run-worktree-tests.sh`, never `docker compose exec api`** — the latter silently tests main's code. See CLAUDE.md "Verifying changes".
- **Conventional Commits**, imperative mood, lowercase after the colon, no trailing period, ~65 chars. Scope `queue` or `api`.
- **`MEM_HEADROOM_FRACTION = 0.7`** — the exact existing literal from `worker.py:405`. Do not change the value in this work.
- **`MIN_DECLARED_MEM_MB = 2048`** and **`UNKNOWN_ASSEMBLY_MEM_MB = 16384`** already exist in `pipeline_service.py` (`:1290`, `:1309`). Read them, do not redefine.
- **Do not modify `resource_estimator.classify()`** — its `mem_budget_mb is None → Band.OK` policy is correct for a heuristic and is out of scope (spec D5).
- **Do not modify `claim.lua`** — the `override and sole` clause already does what this work needs.
- **Keep commits separable** — a mechanical change and a behaviour change never share a commit.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `backend/app/services/resource_limit_service.py` | Owns the stable admission ceiling: `MEM_HEADROOM_FRACTION`, `admission_budget_mb()` | 1, 2 |
| `backend/app/queue/worker.py` | Consumes the helper; applies its own live clamp on top | 2 |
| `backend/app/pipelines/resource_estimator.py` | Owns the exact predicate and its message, beside the heuristic banding | 3 |
| `backend/app/services/pipeline_service.py` | Calls the check in both launch paths | 4, 5 |
| `backend/tests/services/test_resource_limit_service.py` | Budget helper unit tests | 1, 2 |
| `backend/tests/pipelines/test_resource_estimator.py` | Predicate and message tests | 3 |
| `backend/tests/services/test_declared_budget_refusal.py` | Launch-path refusal tests (new file) | 4, 5 |

Task 2 is a standalone bug fix (spec R4a) and lands before the feature so the budget figure is correct when the check starts reading it.

---

### Task 1: The shared admission budget helper

**Files:**
- Modify: `backend/app/services/resource_limit_service.py`
- Test: `backend/tests/services/test_resource_limit_service.py`

**Interfaces:**
- Consumes: existing `resolve_mem_budget_mb(*, stored_mb, machine_mb, hard_mem_mb=None) -> int`
- Produces: `MEM_HEADROOM_FRACTION: float` and
  `admission_budget_mb(*, stored_mb: int | None, machine_mb: int, hard_mem_mb: int | None = None) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/services/test_resource_limit_service.py`:

```python
def test_admission_budget_applies_the_headroom_fraction():
    # 10000 MB machine, no stored limit -> 70% of the machine.
    assert resource_limit_service.admission_budget_mb(
        stored_mb=None, machine_mb=10000
    ) == 7000


def test_admission_budget_applies_headroom_to_the_stored_limit():
    # A stored limit lowers the ceiling first, then headroom applies to it.
    assert resource_limit_service.admission_budget_mb(
        stored_mb=8000, machine_mb=10000
    ) == 5600


def test_admission_budget_respects_the_hard_ceiling():
    # hard_mem_mb binds unconditionally, below stored and machine alike.
    assert resource_limit_service.admission_budget_mb(
        stored_mb=8000, machine_mb=10000, hard_mem_mb=4000
    ) == 2800


def test_admission_budget_never_returns_negative():
    assert resource_limit_service.admission_budget_mb(
        stored_mb=None, machine_mb=0
    ) == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_resource_limit_service.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'admission_budget_mb'`

- [ ] **Step 3: Write the implementation**

In `backend/app/services/resource_limit_service.py`, after `resolve_mem_budget_mb`:

```python
# Headroom so a job that slightly overshoots its declared demand does not push
# the machine into swap. Extracted from worker._resource_budgets, which applied
# it as a bare literal: the launch-time refusal must use the same figure, and
# two copies of a constant that must agree is how they come to disagree.
MEM_HEADROOM_FRACTION = 0.7


def admission_budget_mb(
    *, stored_mb: int | None, machine_mb: int, hard_mem_mb: int | None = None
) -> int:
    """The stable ceiling admission plans against, before live headroom.

    This is the number that decides whether a job can *ever* be claimed.
    `worker._resource_budgets` clamps it further by a live `available_mb`
    reading, which moves with whatever else is running; the launch-time check
    deliberately does not, because a job under this ceiling is claimable once
    the machine is quiet, and refusing it would be a false refusal. See the
    spec's "The budget is not the configured limit".
    """
    resolved = resolve_mem_budget_mb(
        stored_mb=stored_mb, machine_mb=machine_mb, hard_mem_mb=hard_mem_mb
    )
    return max(int(resolved * MEM_HEADROOM_FRACTION), 0)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_resource_limit_service.py -v`
Expected: PASS (4 new tests, plus the existing file green)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/resource_limit_service.py backend/tests/services/test_resource_limit_service.py
git commit -m "refactor(queue): extract the admission budget ceiling into one helper"
```

---

### Task 2: Make the worker use the helper and honour `hard_mem_mb`

Spec R4a. This is a real bug on its own: `worker.py:392-394` never passes `hard_mem_mb`, so the admission budget can sit above the kernel cgroup ceiling and jobs get admitted that the kernel then OOM-kills.

**Files:**
- Modify: `backend/app/queue/worker.py:378-409`
- Test: `backend/tests/services/test_resource_limit_service.py`

**Interfaces:**
- Consumes: `admission_budget_mb`, `MEM_HEADROOM_FRACTION` from Task 1
- Produces: no new symbols; `_resource_budgets()` keeps its signature and return shape

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_resource_limit_service.py`:

```python
def test_worker_budget_never_exceeds_the_admission_budget():
    """The worker's live clamp may only lower the shared ceiling, never raise it.

    Guards the pair from drifting: if someone changes the worker's arithmetic
    so it can exceed admission_budget_mb, a job could pass the launch check and
    still be unclaimable, which is the bug this work exists to remove.
    """
    ceiling = resource_limit_service.admission_budget_mb(
        stored_mb=8000, machine_mb=10000
    )
    # The worker takes min(available_mb, ceiling) then floors at 128. For any
    # available reading, the result is <= ceiling except via the 128 floor.
    for available_mb in (50, 1000, 5600, 99999):
        worker_mb = max(min(available_mb, ceiling), 128)
        assert worker_mb <= max(ceiling, 128)


def test_hard_mem_mb_lowers_the_admission_budget():
    """R4a: the kernel-enforced ceiling binds, so admission stays under it."""
    without = resource_limit_service.admission_budget_mb(
        stored_mb=None, machine_mb=10000
    )
    with_hard = resource_limit_service.admission_budget_mb(
        stored_mb=None, machine_mb=10000, hard_mem_mb=4000
    )
    assert with_hard < without
    assert with_hard <= 4000
```

- [ ] **Step 2: Run the tests to verify the second fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_resource_limit_service.py -v`
Expected: both PASS at the helper level (Task 1 already threads `hard_mem_mb`). If `test_hard_mem_mb_lowers_the_admission_budget` fails, Task 1's helper is not forwarding `hard_mem_mb` — fix that before continuing.

These tests pin the contract; the worker change below is what makes the running system honour it.

- [ ] **Step 3: Change the worker to use the helper**

In `backend/app/queue/worker.py`, replace the block at `:381-405`. The comment at `:383-385` claiming `claim.lua` is "the entire enforcement path for the setting" is describing bug #478 as though it were the design — replace it, per the spec's "Correcting a stale comment".

Old:

```python
        # The user's admission budget, if they set one. It only ever lowers
        # the ceiling -- see resource_limit_service.resolve_mem_budget_mb.
        #
        # This is the entire enforcement path for the setting: `claim.lua`
        # already refuses any candidate whose declared mem_mb exceeds the
        # live-computed free amount, so a smaller ceiling here *is* the limit
        # taking effect. A read failure falls back to the machine budget
        # rather than stalling dispatch, matching _read_reservations' policy
        # for the same reason.
        machine_mb = int(mem_budget / (1024 * 1024))
        try:
            stored = await resource_limit_service.load()
            budget_source_mb = resource_limit_service.resolve_mem_budget_mb(
                stored_mb=stored.max_mem_mb, machine_mb=machine_mb
            )
            if stored.max_cpu:
                cpu_budget = min(cpu_budget, stored.max_cpu)
        except Exception as e:  # noqa: BLE001 - dispatch must survive a DB blip
            log.warning("resource_limits_read_failed", error=str(e))
            budget_source_mb = machine_mb

        available_mb = int(psutil.virtual_memory().available / (1024 * 1024))
        # Never hand out the last of memory: leave headroom so a job that
        # slightly overshoots its declared demand does not push into swap.
        budget_mb = int(budget_source_mb * 0.7)
```

New:

```python
        # The user's admission budget, if they set one. It only ever lowers
        # the ceiling -- see resource_limit_service.resolve_mem_budget_mb.
        #
        # Enforcement is primarily at launch: pipeline_service refuses a job
        # whose declared mem_mb exceeds this ceiling, because such a job could
        # never be claimed (#478). `claim.lua` remains the backstop for jobs
        # that reach the queue by another route. A read failure falls back to
        # the machine budget rather than stalling dispatch, matching
        # _read_reservations' policy for the same reason.
        machine_mb = int(mem_budget / (1024 * 1024))
        try:
            stored = await resource_limit_service.load()
            budget_mb = resource_limit_service.admission_budget_mb(
                stored_mb=stored.max_mem_mb,
                machine_mb=machine_mb,
                # Binds unconditionally: a soft budget above the kernel's own
                # ceiling admits jobs the kernel then OOM-kills.
                hard_mem_mb=resource_limit_service.hard_mem_mb(),
            )
            if stored.max_cpu:
                cpu_budget = min(cpu_budget, stored.max_cpu)
        except Exception as e:  # noqa: BLE001 - dispatch must survive a DB blip
            log.warning("resource_limits_read_failed", error=str(e))
            budget_mb = resource_limit_service.admission_budget_mb(
                stored_mb=None, machine_mb=machine_mb
            )

        available_mb = int(psutil.virtual_memory().available / (1024 * 1024))
```

The `return` at `:407-411` is unchanged: it still reads `max(min(available_mb, budget_mb), 128)`.

- [ ] **Step 4: Run the worker and queue tests**

Run: `./backend/run-worktree-tests.sh tests/queue/ tests/services/test_resource_limit_service.py -q`
Expected: PASS. If a worker test asserted the old inline `* 0.7`, update it to call `admission_budget_mb` rather than re-deriving the number.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/worker.py backend/tests/services/test_resource_limit_service.py
git commit -m "fix(queue): keep the admission budget under the kernel hard limit"
```

---

### Task 3: The exact declared-vs-budget predicate and message

**Files:**
- Modify: `backend/app/pipelines/resource_estimator.py`
- Test: `backend/tests/pipelines/test_resource_estimator.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `exceeds_declared_budget(*, declared_mb: int, budget_mb: int) -> bool`
  - `explain_declared_refusal(*, declared_mb: int, budget_mb: int) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_resource_estimator.py`:

```python
def test_exceeds_declared_budget_is_a_strict_comparison():
    # Equal fits: claim.lua admits on `mem <= mem_free`.
    assert not resource_estimator.exceeds_declared_budget(
        declared_mb=8000, budget_mb=8000
    )
    assert resource_estimator.exceeds_declared_budget(
        declared_mb=8001, budget_mb=8000
    )


def test_declared_refusal_names_both_numbers():
    msg = resource_estimator.explain_declared_refusal(
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
    msg = resource_estimator.explain_declared_refusal(
        declared_mb=16384, budget_mb=5600
    )
    assert "Estimated" not in msg
    assert "requires" in msg.lower()


def test_declared_refusal_points_at_the_setting():
    msg = resource_estimator.explain_declared_refusal(
        declared_mb=16384, budget_mb=5600
    )
    assert "memory budget" in msg.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_resource_estimator.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'exceeds_declared_budget'`

- [ ] **Step 3: Write the implementation**

Append to `backend/app/pipelines/resource_estimator.py`:

```python
def exceeds_declared_budget(*, declared_mb: int, budget_mb: int) -> bool:
    """Whether a job's declared reservation can never be claimed.

    Deliberately separate from `classify()`. That function bands a *heuristic*
    prediction and answers "is this likely to be tight?"; this one compares two
    exact numbers and answers "is this impossible?". Strict `>` mirrors
    claim.lua's `mem <= mem_free`, so a job declaring exactly the budget fits
    here and there alike.
    """
    return declared_mb > budget_mb


def explain_declared_refusal(*, declared_mb: int, budget_mb: int) -> str:
    """The refusal sentence for a job that could never be claimed.

    Worded to be distinguishable from `explain()`, which reports an estimate
    and points at the sliders that move it. Nothing the user can change about
    the run alters this number -- it is a fixed reservation -- so the sentence
    points at the memory budget setting instead.
    """
    return (
        f"This job requires {declared_mb:,} MB, which is more than the "
        f"{budget_mb:,} MB memory budget. It would wait forever without "
        f"running. Raise the memory budget in Settings, or launch it anyway "
        f"to run it on its own when the machine is idle."
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_resource_estimator.py -v`
Expected: PASS (4 new tests, existing file green)

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/resource_estimator.py backend/tests/pipelines/test_resource_estimator.py
git commit -m "feat(queue): add an exact declared-vs-budget refusal predicate"
```

---

### Task 4: Refuse an over-budget assembly at launch

Assembly first: spec case 1 (`UNKNOWN_ASSEMBLY_MEM_MB` with no estimate at all) is the cleanest instance of #478, and it is the case that proves the check must sit outside the `estimate is not None` guard.

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (assemble path, around `:4240-4335`)
- Create: `backend/tests/services/test_declared_budget_refusal.py`

**Interfaces:**
- Consumes: `resource_limit_service.admission_budget_mb` (Task 1); `resource_estimator.exceeds_declared_budget`, `explain_declared_refusal` (Task 3)
- Produces: a module-level helper in `pipeline_service`:
  `async def _refuse_if_over_budget(*, declared_mb: int, resource_override: bool) -> None`
  — raises `ValidationError`, returns `None` otherwise. Task 5 reuses it.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_declared_budget_refusal.py`:

```python
"""Launch-time refusal of a job that could never be claimed (#478).

The bug: a job whose declared mem_mb exceeds the admission budget can never
satisfy claim.lua's `mem <= mem_free`, has no starvation escape, and no
timeout -- so it waits forever. These tests pin the refusal that replaces
that wait.
"""

import pytest

from app.errors import ValidationError
from app.pipelines import resource_estimator
from app.services import pipeline_service, resource_limit_service


def test_over_budget_declaration_raises():
    """R1: a declaration above the budget is refused, not queued."""
    with pytest.raises(ValidationError) as excinfo:
        pipeline_service.refuse_if_over_budget(
            declared_mb=16384, budget_mb=5600, resource_override=False
        )
    assert "16,384" in str(excinfo.value)
    assert "5,600" in str(excinfo.value)


def test_override_skips_the_refusal():
    """R3: 'Launch anyway' proceeds; claim.lua admits it under sole occupancy."""
    pipeline_service.refuse_if_over_budget(
        declared_mb=16384, budget_mb=5600, resource_override=True
    )


def test_within_budget_declaration_is_unaffected():
    """R6: the regression guard -- normal jobs see no new refusal."""
    pipeline_service.refuse_if_over_budget(
        declared_mb=2048, budget_mb=5600, resource_override=False
    )


def test_equal_to_budget_is_allowed():
    """claim.lua admits on `mem <= mem_free`, so equality must fit here too."""
    pipeline_service.refuse_if_over_budget(
        declared_mb=5600, budget_mb=5600, resource_override=False
    )


def test_unknown_assembly_declaration_exceeds_a_modest_budget():
    """R6a: the case with no estimate at all.

    An assembly nothing can estimate declares UNKNOWN_ASSEMBLY_MEM_MB and is
    banded by nothing -- both launch sites guard their banding on
    `estimate is not None`. This asserts the value is genuinely over a modest
    budget, which is what makes placing the check outside that guard load-bearing.
    """
    budget = resource_limit_service.admission_budget_mb(
        stored_mb=8000, machine_mb=32000
    )
    assert resource_estimator.exceeds_declared_budget(
        declared_mb=pipeline_service.UNKNOWN_ASSEMBLY_MEM_MB, budget_mb=budget
    )
    with pytest.raises(ValidationError):
        pipeline_service.refuse_if_over_budget(
            declared_mb=pipeline_service.UNKNOWN_ASSEMBLY_MEM_MB,
            budget_mb=budget,
            resource_override=False,
        )
```

Note the helper is named `refuse_if_over_budget` (public, no underscore) and takes `budget_mb` explicitly, so it is pure and testable without a DB. The launch sites resolve the budget and pass it in.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_declared_budget_refusal.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'refuse_if_over_budget'`

- [ ] **Step 3: Add the helper and the budget resolver**

In `backend/app/services/pipeline_service.py`, near `UNKNOWN_ASSEMBLY_MEM_MB` (`:1309`):

```python
def refuse_if_over_budget(
    *, declared_mb: int, budget_mb: int, resource_override: bool
) -> None:
    """Refuse a job whose declared reservation could never be claimed (#478).

    Pure and budget-injected so it can be tested without a database, and so
    both launch paths share one definition of the refusal.

    `resource_override` is the user's "Launch anyway", the same flag the
    estimate-based refusal honours: it rides the job document to claim.lua,
    which admits the job when it is the sole occupant. That is a real escape
    here rather than a rubber stamp -- an over-budget job genuinely can run,
    just not alongside anything else.
    """
    if resource_override:
        return
    if not resource_estimator.exceeds_declared_budget(
        declared_mb=declared_mb, budget_mb=budget_mb
    ):
        return
    raise ValidationError(
        resource_estimator.explain_declared_refusal(
            declared_mb=declared_mb, budget_mb=budget_mb
        ),
        details={"declared_mb": declared_mb, "budget_mb": budget_mb},
    )


async def current_admission_budget_mb() -> int:
    """The ceiling a launch is checked against, matching the worker's.

    Reads the stored limits like `worker._resource_budgets` does, and falls
    back to the machine's own budget on a read failure for the same reason:
    a DB blip must not refuse every launch.
    """
    # Imported in-function, matching the five existing LoadGovernor call sites
    # in this module (:1139, :1726, :1857, :4189, ...). Hoisting it to module
    # scope is a separate change with cycle risk, not part of this fix.
    from app.queue.governor import LoadGovernor

    machine_mb = int(LoadGovernor().mem_budget_bytes() / (1024 * 1024))
    try:
        stored = await resource_limit_service.load()
        stored_mb = stored.max_mem_mb
    except Exception as e:  # noqa: BLE001 - a launch must survive a DB blip
        log.warning("resource_limits_read_failed", error=str(e))
        stored_mb = None
    return resource_limit_service.admission_budget_mb(
        stored_mb=stored_mb,
        machine_mb=machine_mb,
        hard_mem_mb=resource_limit_service.hard_mem_mb(),
    )
```

Imports, verified against the current file:

- **`LoadGovernor`** is imported *in-function* at five sites (`:1139`, `:1726`, `:1857`, `:4189`, and one more) — never at module scope. Follow that pattern, as the snippet above does. Do not hoist it.
- **`resource_limit_service`** is **not** currently imported here. Add it to the existing module-level line at `:64`, keeping the names alphabetical:

```python
from app.services import (
    blob_service,
    memory_estimate,
    object_service,
    resource_limit_service,
    run_service,
)
```

CI's `ruff check` enforces import order (`I001`); that rule is what failed on #217/#314, so run `ruff check backend/app` before pushing rather than discovering it in CI.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_declared_budget_refusal.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Wire it into the assemble launch path**

In the assemble path, the declared value is currently inline at `:4323` as `mem_mb=estimate or UNKNOWN_ASSEMBLY_MEM_MB`. Hoist it to a local **before** the `if estimate is not None:` block at `:4241`, then check it there — outside that guard, so an unestimatable assembly is still checked (R6a):

```python
    # Hoisted from the enqueue below so it can be checked before we get there.
    # Outside the `estimate is not None` guard on purpose: an assembly nothing
    # can estimate declares the flat fallback and is banded by nothing at all,
    # which is the cleanest instance of #478.
    declared_mem_mb = estimate or UNKNOWN_ASSEMBLY_MEM_MB
    refuse_if_over_budget(
        declared_mb=declared_mem_mb,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )
```

Then change the enqueue at `:4323` to use the local:

```python
        resources=JobResources(
            cpu=parsed.threads,
            mem_mb=declared_mem_mb,
            io=IoClass.HEAVY,
        ),
```

- [ ] **Step 6: Run the assembly launch tests**

Run: `./backend/run-worktree-tests.sh tests/services/ -q -k "assembl or launch"`
Expected: PASS. Existing assembly-launch tests that did not set a memory budget use the machine's own, so a 16384 MB declaration passes on any host with ~24 GB+. On a smaller CI host these may now refuse — if one fails, set an explicit generous `max_mem_mb` in that test's fixture rather than weakening the check.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_declared_budget_refusal.py
git commit -m "fix(queue): refuse an assembly that could never be claimed"
```

---

### Task 5: Refuse an over-budget alignment at launch

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (align path, around `:2058-2090`)
- Test: `backend/tests/services/test_declared_budget_refusal.py`

**Interfaces:**
- Consumes: `refuse_if_over_budget`, `current_admission_budget_mb` (Task 4)
- Produces: no new symbols

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_declared_budget_refusal.py`:

```python
def test_min_declared_floor_can_exceed_a_small_budget():
    """Spec case 2: the floor lifts a declaration past a banded-OK estimate.

    `declared_align_mem_mb` floors at MIN_DECLARED_MEM_MB, so a tiny alignment
    still declares 2048 MB. Under a very small budget that is unclaimable,
    while the estimate the banding saw was smaller and passed.
    """
    tiny_budget = resource_limit_service.admission_budget_mb(
        stored_mb=1024, machine_mb=32000
    )
    assert resource_estimator.exceeds_declared_budget(
        declared_mb=pipeline_service.MIN_DECLARED_MEM_MB, budget_mb=tiny_budget
    )
    with pytest.raises(ValidationError):
        pipeline_service.refuse_if_over_budget(
            declared_mb=pipeline_service.MIN_DECLARED_MEM_MB,
            budget_mb=tiny_budget,
            resource_override=False,
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_declared_budget_refusal.py::test_min_declared_floor_can_exceed_a_small_budget -v`
Expected: PASS already — the helper from Task 4 handles it. This test documents spec case 2 and guards the floor's interaction; if it fails, `MIN_DECLARED_MEM_MB` or the budget arithmetic has drifted from the spec.

- [ ] **Step 3: Wire the check into the align launch path**

`align_mem_mb` is computed at `:2058` via `declared_align_mem_mb(...)` and passed to `enqueue` at `:2085`. Insert the check between them, after `:2058`:

```python
    # The value actually enqueued, which is not the number the banding above
    # saw: it is recomputed with building_index=False and floored at
    # MIN_DECLARED_MEM_MB. Checking the enqueued value is the point (#478).
    refuse_if_over_budget(
        declared_mb=align_mem_mb,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )
```

Placed after `:2058` it is already outside the `if estimate is not None:` banding block at `:1888`, as required.

- [ ] **Step 4: Run the align launch tests**

Run: `./backend/run-worktree-tests.sh tests/services/ -q -k "align or launch"`
Expected: PASS. Same note as Task 4 Step 6 about tests that assume a generous budget.

- [ ] **Step 5: Run the full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the final count, not just the exit code — CLAUDE.md is explicit that "green" means reading the number.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_declared_budget_refusal.py
git commit -m "fix(queue): refuse an alignment that could never be claimed"
```

---

### Task 6: Verify against a real launch, then close out

Spec's testing section: the unit tests feed hand-built numbers that already look the way the check expects. CLAUDE.md's "Check a rule against the real database" applies — the Actions-tab rules passed a full green suite while being wrong about real objects.

**Files:**
- Modify: `docs/TODO.md` / `docs/TODO-done.md` only if an entry covers this
- No source changes expected

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

UI on 5273, API on 8100. Do not use plain `docker compose` here — a hook blocks it, and it would repoint the main stack at this worktree.

- [ ] **Step 2: Set a low memory budget**

In the UI at `localhost:5273`, open Settings and set the memory budget to 4096 MB. This puts `admission_budget_mb` at roughly 2867 MB, below both `UNKNOWN_ASSEMBLY_MEM_MB` (16384) and `MIN_DECLARED_MEM_MB` (2048 — note this one is *under* 2867, so alignment should still launch).

- [ ] **Step 3: Attempt an assembly**

Launch an assembly on any reads object. Expected: a refusal card naming both numbers — "requires 16,384 MB … 2,867 MB memory budget" — and **no job created**. Confirm no job appears in the activity view.

- [ ] **Step 4: Confirm the override still works**

Click "Launch anyway" on the same refusal. Expected: the job is created. Confirm it carries the flag:

The worktree stack's compose project is `biopipe-wt-<slug>`, where the slug is
the branch name lowercased with non-alphanumerics turned to `-`
(`ops/worktree-up.sh:104`). Derive it rather than typing it:

```bash
./ops/worktree-up.sh --list
```

Then, substituting the project name that command prints:

```bash
docker compose -p biopipe-wt-claude-issue-478-bug-a38cb6 exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models.job import Job
async def main():
    await connect_to_mongo()
    j = await Job.find(Job.type == 'assemble_reads').sort(-Job.timing.enqueued_at).first_or_none()
    print(j.type, j.resources.mem_mb, 'override=', j.resource_override)
asyncio.run(main())
"
```

Naming the project explicitly is what lets this pass the worktree hook — a bare
`docker compose` here is blocked, and correctly so.

Expected: `assemble_reads 16384 override= True`

- [ ] **Step 5: Confirm the regression guard on a real launch**

Raise the memory budget back to a generous value and launch the same assembly. Expected: it queues normally with no refusal — R6 against real objects rather than fixtures.

- [ ] **Step 6: Tear the stack down**

```bash
./ops/worktree-up.sh --down
```

CLAUDE.md: a stack you brought up for testing is yours to bring down. Leftover stacks corrupt other test runs by dropping `biopipe_test` collections mid-run.

- [ ] **Step 7: Check for a TODO entry**

```bash
grep -n "478\|unsatisfiable\|waits forever" docs/TODO.md
```

If an entry covers this, append ` — FIXED` to its heading with a note on what shipped and what differed from the plan (the diagnosis correction in `62d1eaa5` is the notable delta), then move the whole entry to `docs/TODO-done.md`. If there is no entry, skip — do not invent one.

- [ ] **Step 8: Commit any doc changes**

```bash
git add docs/
git commit -m "docs: close out the unsatisfiable-job backlog entry"
```

Skip this commit entirely if Step 7 found nothing.

---

## Finishing

Follow CLAUDE.md's merge workflow: rebase on `origin/main`, verify the diff survived, push, open the PR with `Closes #478`, label it `type:bug` + `area:backend`, poll `gh pr checks` until every check reports pass, then `gh pr merge <N> --rebase --delete-branch`. Remove the worktree once the merge lands.

One caveat worth stating in the PR body: this makes a conservative memory budget hard-refuse assembly at launch (spec D6). Those jobs previously queued and never ran, so nothing that worked stops working — but the failure moves from silent to loud, and a user who had been living with a permanently-stuck assembly will now see a refusal instead.

## Self-Review Notes

Spec coverage: R1 → Task 4 Step 1; R2 → Task 3 Step 1; R3 → Task 4 Step 1; R4 → Tasks 1–2; R4a → Task 2; R5 → Task 1 (`stored_mb=None` case); R6 → Task 4 Step 1; R6a → Task 4 Steps 1 and 5; R7 → Task 3 Step 1. Spec cases 1–3 map to Task 4 Step 5, Task 5 Step 1, and Task 5 Step 3 respectively.

Naming is consistent throughout: `admission_budget_mb`, `MEM_HEADROOM_FRACTION`, `exceeds_declared_budget`, `explain_declared_refusal`, `refuse_if_over_budget`, `current_admission_budget_mb`.

One deliberate departure from the spec: the spec sketched `refuse_if_over_budget` as private (`_refuse_if_over_budget`) and async. It is public and sync here, with the budget injected, so it can be unit-tested without a database — the async budget resolution lives in `current_admission_budget_mb` instead. Same behaviour, testable seam.
