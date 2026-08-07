# Four-Choice Refusal Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the launch-time BLOCK dead end with a card offering four exits — cancel, edit parameters, launch anyway, and auto re-plan.

**Architecture:** One shared React card component with two triggers: `AlignDialog` renders it pre-flight from its existing client-side band computation, `AssembleDialog` renders it reactively from the 422 response body. "Launch anyway" sets a persisted `Job.resource_override` flag that reaches `claim.lua` through the Redis job hash, where it relaxes the memory gate only when the job would be the sole occupant. "Auto re-plan" calls a new endpoint wrapping the already-merged `replan_service`.

**Tech Stack:** Python 3.12 / FastAPI / Beanie (MongoDB) / Redis + Lua / React + TypeScript / TanStack Query / pytest

**Spec:** [`docs/superpowers/specs/2026-08-07-refusal-card-design.md`](../specs/2026-08-07-refusal-card-design.md)

---

## File Structure

**Backend — create:**
- `backend/app/api/v1/replan.py` — the `POST /pipelines/replan` endpoint. A separate module rather than another block in the 1400-line `pipelines.py`, because it has one responsibility and no shared state with the launch routes.
- `backend/tests/api/test_replan_endpoint.py`
- `backend/tests/queue/test_resource_override.py` — the claim-time override tests, including the `ignore_reservations` trap.

**Backend — modify:**
- `backend/app/models/job.py:195` — add `resource_override: bool = False` beside `parent_job_id`.
- `backend/app/queue/queue.py:55-68` — `enqueue()` gains a `resource_override` parameter.
- `backend/app/queue/queue.py:320-331` — `_push_to_redis` writes `override` into the hash mapping.
- `backend/app/queue/queue.py:821-832` — `reconcile()` writes the same field. **This is the one that makes the flag survive a requeue.**
- `backend/app/queue/scripts/claim.lua` — read `override`, compute `sole`, relax the memory gate.
- `backend/app/services/replan_service.py` — gains `as_payload()`, the tagged-union serializer both the 422 and the endpoint use. Placed here rather than at either call site so the two cannot drift.
- `backend/app/services/pipeline_service.py:1443-1502` — `launch_alignment` gains `resource_override`, skips the raise, enriches the `details` dict.
- `backend/app/services/pipeline_service.py:3212-3290` — same for `launch_assembly`, plus the inlined replan result.
- `backend/app/api/v1/pipelines.py:869-876, 956-960` — request models gain `resource_override`.
- `backend/app/api/v1/pipelines.py:1346-1358, 963-969` — routes pass it through.

**Frontend — create:**
- `frontend/src/components/ResourceRefusalCard.tsx` — the shared card.

**Frontend — modify:**
- `frontend/src/api/types.ts` — `ReplanResult` union and `ResourceRefusalDetails`.
- `frontend/src/api/client.ts:753-762` — `replan()` call.
- `frontend/src/components/AlignDialog.tsx:140-200, 340-395` — pre-flight trigger.
- `frontend/src/components/AssembleDialog.tsx:86-93, 229` — reactive trigger.

**Ordering rationale:** the backend override lands first (Tasks 1–5) because the frontend's "Launch anyway" button is meaningless without it. The endpoint (Task 6) precedes the card (Tasks 7–9) for the same reason.

**Test commands.** All backend tests in this plan run from the worktree root via:

```bash
./backend/run-worktree-tests.sh tests/path/to/test.py -v
```

Never `docker compose exec api python -m pytest` — from a worktree that silently tests `main`'s code (see CLAUDE.md).

---

### Task 1: Persist the override flag on the job document

**Files:**
- Modify: `backend/app/models/job.py:195`
- Test: `backend/tests/queue/test_resource_override.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_resource_override.py`:

```python
"""The launch-anyway override, from the job document through to claim.lua.

The assertions here are chosen for the direction that fails when the seam
breaks. Asserting that an overridden job IS claimed proves little -- most
things are claimable in a quiet test environment. Asserting it is REFUSED
under contention is what fails if `sole` is computed wrongly.
"""

import pytest

from app.models.job import Job, JobState


@pytest.mark.asyncio
async def test_resource_override_defaults_to_false():
    job = Job(type="align_reads", owner="p1", state=JobState.PENDING)
    assert job.resource_override is False


@pytest.mark.asyncio
async def test_resource_override_persists_across_a_reload():
    job = Job(
        type="align_reads",
        owner="p1",
        state=JobState.PENDING,
        resource_override=True,
    )
    await job.insert()

    reloaded = await Job.get(job.id)
    assert reloaded is not None
    assert reloaded.resource_override is True
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_resource_override.py -v
```

Expected: FAIL. Pydantic raises on the unknown field `resource_override`, or the attribute does not exist.

- [ ] **Step 3: Add the field**

In `backend/app/models/job.py`, directly below line 195:

```python
    parent_job_id: PydanticObjectId | None = None  # the job that enqueued this one
    # Set when the user chose "Launch anyway" on the refusal card. Like
    # `last_attempt_progress`, this must specifically NOT be cleared on retry:
    # its whole purpose is to survive a requeue after lease expiry. It reaches
    # claim.lua through the Redis job hash, written by both `_push_to_redis`
    # and `reconcile`.
    resource_override: bool = False
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_resource_override.py -v
```

Expected: PASS, 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/job.py backend/tests/queue/test_resource_override.py
git commit -m "feat(queue): persist a per-job resource override flag"
```

---

### Task 2: Carry the flag through enqueue into the Redis hash

**Files:**
- Modify: `backend/app/queue/queue.py:55-68` (signature), `:127-136` (Job construction), `:320-331` (`_push_to_redis`)
- Test: `backend/tests/queue/test_resource_override.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/queue/test_resource_override.py`:

```python
from app.queue import keys, queue
from app.redis_client import get_redis


@pytest.mark.asyncio
async def test_enqueue_writes_override_into_the_redis_hash():
    job = await queue.enqueue(
        "align_reads", owner="p1", resource_override=True
    )
    assert job is not None

    r = get_redis()
    value = await r.hget(keys.job_key(str(job.id)), "override")
    assert value == "1"


@pytest.mark.asyncio
async def test_enqueue_writes_zero_when_not_overridden():
    job = await queue.enqueue("align_reads", owner="p1")
    assert job is not None

    r = get_redis()
    value = await r.hget(keys.job_key(str(job.id)), "override")
    # Written explicitly rather than omitted: claim.lua reads a fixed HMGET
    # position, and a missing field there is nil, not "0".
    assert value == "0"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_resource_override.py -v
```

Expected: FAIL — `enqueue() got an unexpected keyword argument 'resource_override'`.

- [ ] **Step 3: Thread the parameter through**

In `backend/app/queue/queue.py`, add to the `enqueue` signature after `parent_job_id` (line 68):

```python
    parent_job_id: PydanticObjectId | None = None,
    resource_override: bool = False,
```

In the `Job(...)` construction (around line 134), after `parent_job_id=parent_job_id,`:

```python
        parent_job_id=parent_job_id,
        resource_override=resource_override,
```

In `_push_to_redis` (line 320), add to the `mapping` dict after `"epoch"`:

```python
            "epoch": job.lease.epoch if job.lease else 0,
            # Always written, never omitted: claim.lua reads this at a fixed
            # HMGET position, where an absent field is nil rather than "0".
            "override": "1" if job.resource_override else "0",
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_resource_override.py -v
```

Expected: PASS, 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/queue.py backend/tests/queue/test_resource_override.py
git commit -m "feat(queue): carry the override flag into the Redis job hash"
```

---

### Task 3: Rewrite the hash on reconcile, so the flag survives a requeue

**Files:**
- Modify: `backend/app/queue/queue.py:821-832`
- Test: `backend/tests/queue/test_resource_override.py`

This is the task that satisfies the acceptance criterion "launch-anyway survives a lease-expiry requeue without being re-refused." `reconcile()` rebuilds the Redis hash from MongoDB, which is the record of truth — without this, the flag survives in Mongo and vanishes from the hash `claim.lua` actually reads.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/queue/test_resource_override.py`:

```python
@pytest.mark.asyncio
async def test_override_survives_a_hash_rebuild_by_reconcile():
    """The flag must come back on the hash, not merely on the document.

    Asserting the Mongo document still holds it would pass without this
    change -- Mongo is not what gets wiped by a Redis restart. The hash is
    what claim.lua reads, so the hash is what this asserts.
    """
    job = await queue.enqueue(
        "align_reads", owner="p1", resource_override=True
    )
    assert job is not None
    job_id = str(job.id)

    # Simulate the Redis-side loss a restart produces: the queue entry and
    # the hash both go, while Mongo keeps the job.
    r = get_redis()
    await r.delete(keys.job_key(job_id))
    await r.zrem(keys.READY, job_id)

    await queue.reconcile()

    value = await r.hget(keys.job_key(job_id), "override")
    assert value == "1"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_resource_override.py::test_override_survives_a_hash_rebuild_by_reconcile -v
```

Expected: FAIL — `assert None == "1"`. The reconcile mapping has no `override` key.

- [ ] **Step 3: Add the field to the reconcile mapping**

In `backend/app/queue/queue.py`, in `reconcile()`'s `pipe.hset` mapping (around line 830), after `"epoch"`:

```python
                "epoch": job.lease.epoch if job.lease else 0,
                # Kept in step with _push_to_redis. This is the half that makes
                # the override survive a requeue: Mongo is the record of truth
                # and this is where the hash is rebuilt from it.
                "override": "1" if job.resource_override else "0",
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_resource_override.py -v
```

Expected: PASS, 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/queue.py backend/tests/queue/test_resource_override.py
git commit -m "feat(queue): rebuild the override flag on reconcile"
```

---

### Task 4: Relax the memory gate in claim.lua for a sole-occupant override

**Files:**
- Modify: `backend/app/queue/scripts/claim.lua`
- Test: `backend/tests/queue/test_resource_override.py`

The whole trap of this change is `ignore_reservations`: when it is set the counters are **not read**, so `reserved_*` are all zero because nothing was looked at, not because nothing is running. Computing `sole` from them there would make the override fire whenever the claiming worker happens to be idle.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/queue/test_resource_override.py`:

```python
async def _claim_with_budget(mem_mb_budget: int, **kwargs):
    """Claim against a named memory budget, everything else generous."""
    return await queue.claim(
        "w1",
        allowed_classes=["user_background"],
        cpu_budget=64,
        mem_mb_budget=mem_mb_budget,
        io_heavy_budget=4,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_overridden_job_is_claimed_when_it_is_the_sole_occupant():
    from app.models.job import JobResources

    job = await queue.enqueue(
        "align_reads",
        owner="p1",
        resources=JobResources(mem_mb=8000),
        resource_override=True,
    )
    assert job is not None

    # Budget far below the job's declared need, and nothing else reserved.
    claimed = await _claim_with_budget(1000)
    assert claimed is not None
    assert claimed.job_id == str(job.id)


@pytest.mark.asyncio
async def test_overridden_job_is_refused_while_anything_else_holds_a_reservation():
    """The direction that fails if `sole` is computed wrongly.

    The complementary "is claimed" assertion above would pass against a
    naive unconditional exemption too. This one would not.
    """
    from app.models.job import JobResources

    r = get_redis()
    # Something else is running and holding memory.
    await r.set("bp:conc:mem_mb", 500)

    job = await queue.enqueue(
        "align_reads",
        owner="p1",
        resources=JobResources(mem_mb=8000),
        resource_override=True,
    )
    assert job is not None

    claimed = await _claim_with_budget(1000)
    assert claimed is None


@pytest.mark.asyncio
async def test_an_idle_worker_does_not_count_as_sole_occupancy():
    """The `ignore_reservations` trap, tested on its own.

    With ignore_reservations set the counters are never read, so the
    reserved_* locals are zero because nothing was looked at. Treating that
    as "nothing is running" makes the override MORE permissive than an
    unconditional exemption, while reading in the source as if it were more
    conservative.
    """
    from app.models.job import JobResources

    r = get_redis()
    await r.set("bp:conc:mem_mb", 500)

    job = await queue.enqueue(
        "align_reads",
        owner="p1",
        resources=JobResources(mem_mb=8000),
        resource_override=True,
    )
    assert job is not None

    claimed = await _claim_with_budget(1000, ignore_reservations=True)
    assert claimed is None


@pytest.mark.asyncio
async def test_a_non_overridden_job_is_still_refused_when_alone():
    """The gate must still work for everyone else."""
    from app.models.job import JobResources

    job = await queue.enqueue(
        "align_reads", owner="p1", resources=JobResources(mem_mb=8000)
    )
    assert job is not None

    claimed = await _claim_with_budget(1000)
    assert claimed is None


@pytest.mark.asyncio
async def test_override_does_not_relax_the_cpu_gate():
    """Scoped to memory. A CPU overcommit bands to WARN and never produces
    a card, so the override has no business touching it."""
    from app.models.job import JobResources

    job = await queue.enqueue(
        "align_reads",
        owner="p1",
        resources=JobResources(cpu=32, mem_mb=100),
        resource_override=True,
    )
    assert job is not None

    claimed = await queue.claim(
        "w1",
        allowed_classes=["user_background"],
        cpu_budget=4,
        mem_mb_budget=64000,
        io_heavy_budget=4,
    )
    assert claimed is None
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/queue/test_resource_override.py -v
```

Expected: `test_overridden_job_is_claimed_when_it_is_the_sole_occupant` FAILS (`assert None is not None`) — the gate refuses it today. The other four pass already, and must keep passing.

- [ ] **Step 3: Modify claim.lua**

In `backend/app/queue/scripts/claim.lua`, extend the header comment block (after line 30):

```lua
-- An `override` job (the user's "Launch anyway") relaxes ONLY the memory gate,
-- and ONLY when it would be the sole occupant. The budget is a user
-- preference; the physical RAM ceiling is not, and this relaxes the first
-- while respecting the second -- an overridden job can never be the cause of a
-- multi-job overcommit. CPU and io_heavy are untouched: classify() bands a CPU
-- overcommit to WARN, so it never produces a refusal card to override.
```

Replace the reservation-read block (lines 51-59) with:

```lua
local reserved_cpu = 0
local reserved_mem = 0
local reserved_io  = 0
-- Read separately from the gating values above, and unconditionally: when
-- `ignore_reservations` is set the gating locals stay at zero because nothing
-- was read, which is NOT the same fact as "nothing is running". Deriving
-- sole-occupancy from them there would fire the override whenever the claiming
-- worker happened to be idle -- strictly more permissive than an unconditional
-- exemption, while reading as if it were more conservative.
local live = redis.call('MGET', 'bp:conc:cpu', 'bp:conc:mem_mb', 'bp:conc:io_heavy')
local sole = math.max(tonumber(live[1]) or 0, 0) == 0
             and math.max(tonumber(live[2]) or 0, 0) == 0
             and math.max(tonumber(live[3]) or 0, 0) == 0

if not ignore_reservations then
  reserved_cpu = math.max(tonumber(live[1]) or 0, 0)
  reserved_mem = math.max(tonumber(live[2]) or 0, 0)
  reserved_io  = math.max(tonumber(live[3]) or 0, 0)
end
```

Change the `HMGET` (line 76) to fetch the new field:

```lua
  local h = redis.call('HMGET', jkey, 'class', 'cpu', 'mem_mb', 'io', 'epoch', 'override')
```

Replace the `fits` computation (lines 80-88) with:

```lua
    local cpu   = tonumber(h[2]) or 1
    local mem   = tonumber(h[3]) or 0
    local io    = h[4] or 'none'
    local epoch = tonumber(h[5]) or 0
    -- Appended field, so h[1]..h[5] keep their positions above.
    local override = h[6] == '1'

    local mem_ok = mem <= mem_free or (override and sole)

    local fits = allowed[class]
                 and cpu <= cpu_free
                 and mem_ok
                 and (io ~= 'heavy' or io_free > 0)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/test_resource_override.py -v
```

Expected: PASS, 10 passed.

- [ ] **Step 5: Run the whole queue suite for regressions**

```bash
./backend/run-worktree-tests.sh tests/queue/ -q
```

Expected: PASS. The extra unconditional `MGET` is the only behavioural change for non-overridden jobs, and it does not alter their gating.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/scripts/claim.lua backend/tests/queue/test_resource_override.py
git commit -m "feat(queue): admit an overridden job when it is the sole occupant"
```

---

### Task 5: Skip the BLOCK raise on override, and enrich the refusal details

**Files:**
- Modify: `backend/app/services/replan_service.py` — add `as_payload()`, the serializer Task 6's endpoint also uses
- Modify: `backend/app/services/pipeline_service.py:1443-1502` and `:3212-3290`
- Modify: `backend/app/api/v1/pipelines.py:869-876, 956-960, 1346-1358, 963-969`
- Test: `backend/tests/services/test_launch_resource_refusal.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_launch_resource_refusal.py`:

```python
"""The enqueue-time refusal, its payload, and the override that skips it."""

import pytest

from app.errors import ValidationError


@pytest.mark.asyncio
async def test_refusal_details_name_the_estimate_source(monkeypatch, align_fixture):
    """The card's source line is an acceptance criterion, so the payload
    that feeds it is asserted here rather than left to the UI."""
    from app.services import pipeline_service

    with pytest.raises(ValidationError) as exc:
        await pipeline_service.launch_alignment(
            object_id=align_fixture.reads.id,
            reference_id=align_fixture.reference.id,
            owner=align_fixture.owner,
            params={"threads": 64, "sort_memory_mb": 4096},
        )

    details = exc.value.details
    assert details["estimate_source"] in {"measured", "heuristic"}
    assert isinstance(details["detail"], str) and details["detail"]
    assert details["estimate_mb"] > details["budget_mb"]
    assert "replan" in details


@pytest.mark.asyncio
async def test_override_skips_the_refusal(align_fixture):
    """The same call that raised above must succeed with the override set."""
    from app.services import pipeline_service

    job = await pipeline_service.launch_alignment(
        object_id=align_fixture.reads.id,
        reference_id=align_fixture.reference.id,
        owner=align_fixture.owner,
        params={"threads": 64, "sort_memory_mb": 4096},
        resource_override=True,
    )
    assert job is not None


@pytest.mark.asyncio
async def test_a_child_job_never_reaches_the_block_check(align_fixture):
    """Acceptance criterion: jobs with a parent_job_id never render a card.

    Already true, and NOT implemented by a guard -- parent_job_id is set
    inside queue.enqueue() by callers in queue/results.py, while the BLOCK
    checks live in the launchers, reached only from the API where no
    parent_job_id is ever passed. Pinned here so a future refactor that
    routes child jobs through a launcher fails loudly.
    """
    import inspect

    from app.services import pipeline_service

    for name in ("launch_alignment", "launch_assembly"):
        sig = inspect.signature(getattr(pipeline_service, name))
        assert "parent_job_id" not in sig.parameters, (
            f"{name} now accepts parent_job_id -- child jobs can reach the "
            "BLOCK check, and the refusal card would be addressed to an "
            "empty room. Add an explicit skip."
        )
```

> **Note on `align_fixture`:** check `backend/tests/conftest.py` and `backend/tests/services/` for an existing fixture building a reads object plus an over-large reference. If none exists, build one in this test module following the nearest existing pattern in `backend/tests/services/`. It must produce a reference large enough that the heuristic estimate exceeds the governor's budget — otherwise the first test asserts nothing.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_launch_resource_refusal.py -v
```

Expected: the first two FAIL — `details` has no `detail` or `replan` key, and `launch_alignment()` takes no `resource_override`. The third passes already and must keep passing.

- [ ] **Step 3: Modify the alignment launcher**

In `backend/app/services/pipeline_service.py`, add to `launch_alignment`'s signature (after `paired: bool = True,`):

```python
    paired: bool = True,
    resource_override: bool = False,
```

Replace the `if band is resource_estimator.Band.BLOCK:` block (line 1486) with:

```python
        # `resource_override` is the user's "Launch anyway" from the refusal
        # card. It skips the refusal here and rides on the job document to
        # claim.lua, which admits the job only when it is the sole occupant.
        if band is resource_estimator.Band.BLOCK and not resource_override:
            from app.services import replan_service

            proposal = replan_service.replan(
                job_type=JOB_TYPE_ALIGN_READS,
                params=align_params.as_dict(),
                budget_mb=mem_budget_mb,
                cpu_budget=governor.cpu_budget() or 1,
            )
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
                    # The card names the source in prose; `detail` is the
                    # phrase resolve() already wrote for exactly that.
                    "detail": resolved.detail,
                    "replan": replan_service.as_payload(proposal),
                },
            )
```

Add the serializer to `backend/app/services/replan_service.py` instead of
`pipeline_service.py` — Task 6's endpoint needs the identical function, and two
copies of a tagged-union serializer drift the moment a field is added. It
belongs beside the dataclasses it serializes:

```python
def as_payload(result: ReplanResult) -> dict:
    """Serialize a result for the refusal card.

    A tagged union rather than a nullable proposal: the card must be able to
    tell "nothing fits" from "there is nothing here to tune", which call for
    different next steps and different prose.

    Lives here rather than at either call site because both the enqueue-time
    refusal (inlined into the 422 for assembly) and the replan endpoint (for
    alignment) need exactly this shape.
    """
    if isinstance(result, Proposal):
        return {
            "kind": "proposal",
            "params": result.params,
            "estimate_mb": result.estimate_mb,
            "changes": [
                {"name": c.name, "before": c.before, "after": c.after}
                for c in result.changes
            ],
            "note": result.note,
        }
    if isinstance(result, Infeasible):
        return {"kind": "infeasible", "reason": result.reason}
    return {"kind": "no_knobs"}
```

Call it as `replan_service.as_payload(proposal)` in the refusal above.

Finally, pass the flag to the alignment's own `enqueue` call. Find the `queue.enqueue(` call inside `launch_alignment` that creates the alignment job (search for `JOB_TYPE_ALIGN_READS` or `"align_reads"` within the function) and add:

```python
        resource_override=resource_override,
```

**Do not** add it to the `_enqueue_build_index` call — an index build is a child job with its own resource profile, and the user overrode the alignment, not the index.

- [ ] **Step 4: Modify the assembly launcher**

Add to `launch_assembly`'s signature (line 3212-3217):

```python
async def launch_assembly(
    *,
    object_id: PydanticObjectId,
    owner: str,
    params: dict | None = None,
    resource_override: bool = False,
) -> Job:
```

Replace its `if band is resource_estimator.Band.BLOCK:` block (line 3279) with:

```python
        if band is resource_estimator.Band.BLOCK and not resource_override:
            from app.services import replan_service

            proposal = replan_service.replan(
                job_type=JOB_TYPE_ASSEMBLE,
                params=parsed.as_dict(),
                budget_mb=mem_budget_mb,
                cpu_budget=LoadGovernor().cpu_budget() or 1,
            )
            raise ValidationError(
                f"This assembly needs about {estimate:,} MB "
                f"({resolved.detail}), more than the "
                f"{mem_budget_mb:,} MB available. Assembling a genome this "
                "size needs a bigger machine.",
                details={
                    "estimate_mb": estimate,
                    "budget_mb": mem_budget_mb,
                    "estimate_source": resolved.source.value,
                    "detail": resolved.detail,
                    # Inlined rather than fetched by a follow-up request: this
                    # path is reactive, so the card renders from this response
                    # and a second round trip would show it half-populated.
                    "replan": replan_service.as_payload(proposal),
                },
            )
```

Pass the flag to the assembly's `queue.enqueue` call in the same function:

```python
        resource_override=resource_override,
```

- [ ] **Step 5: Thread it through the API**

In `backend/app/api/v1/pipelines.py`, add to `AlignRequest` (line 869-875):

```python
    params: dict = Field(default_factory=dict)
    # "Launch anyway" from the refusal card. Skips the enqueue-time BLOCK and
    # persists on the job, where claim.lua admits it only as sole occupant.
    resource_override: bool = False
```

Add to `AssembleRequest` (line 956-960):

```python
    params: dict | None = None
    resource_override: bool = False
```

In the `launch_alignment` route (line 1349-1357), add:

```python
        paired=body.paired,
        resource_override=body.resource_override,
```

In the `launch_assemble` route (line 966-968):

```python
    job = await pipeline_service.launch_assembly(
        object_id=body.object_id,
        owner=owner,
        params=body.params,
        resource_override=body.resource_override,
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_launch_resource_refusal.py -v
```

Expected: PASS, 3 passed.

- [ ] **Step 7: Run the pipeline and API suites for regressions**

```bash
./backend/run-worktree-tests.sh tests/services/ tests/api/ -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/replan_service.py backend/app/services/pipeline_service.py backend/app/api/v1/pipelines.py backend/tests/services/test_launch_resource_refusal.py
git commit -m "feat(pipelines): honour the resource override and enrich refusal details"
```

---

### Task 6: The replan endpoint

**Files:**
- Create: `backend/app/api/v1/replan.py`
- Modify: wherever routers are registered (search: `include_router` in `backend/app/main.py`)
- Test: `backend/tests/api/test_replan_endpoint.py` (create)

Alignment calls this when the card opens. Assembly does not — its result is inlined in the 422 from Task 5.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_replan_endpoint.py`:

```python
"""POST /pipelines/replan -- the Auto re-plan button's data source."""

import pytest


@pytest.mark.asyncio
async def test_returns_a_proposal_for_a_tunable_over_budget_alignment(client):
    resp = await client.post(
        "/api/v1/pipelines/replan",
        json={
            "job_type": "align_reads",
            "params": {"aligner": "minimap2", "threads": 64, "sort_memory_mb": 4096},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] in {"proposal", "infeasible", "no_knobs"}


@pytest.mark.asyncio
async def test_unregistered_job_type_reports_no_knobs(client):
    resp = await client.post(
        "/api/v1/pipelines/replan",
        json={"job_type": "run_qc", "params": {}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"kind": "no_knobs"}


@pytest.mark.asyncio
async def test_the_client_cannot_state_its_own_budget(client):
    """A client that names its budget can name a larger one, which turns the
    feasibility test into a formality. Extra keys must be ignored, not honoured."""
    resp = await client.post(
        "/api/v1/pipelines/replan",
        json={
            "job_type": "align_reads",
            "params": {"aligner": "minimap2", "threads": 64, "sort_memory_mb": 4096},
            "budget_mb": 10_000_000,
        },
    )
    assert resp.status_code == 200
    # Ignored: the response must match the no-budget call above.
    baseline = await client.post(
        "/api/v1/pipelines/replan",
        json={
            "job_type": "align_reads",
            "params": {"aligner": "minimap2", "threads": 64, "sort_memory_mb": 4096},
        },
    )
    assert resp.json() == baseline.json()
```

> **Note on `client`:** use whatever async HTTP client fixture `backend/tests/api/` already uses — check a neighbouring file such as `backend/tests/api/test_jobs.py` for the exact name and import.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/api/test_replan_endpoint.py -v
```

Expected: FAIL with 404 — the route does not exist.

- [ ] **Step 3: Write the endpoint**

Create `backend/app/api/v1/replan.py`:

```python
"""The Auto re-plan button's endpoint.

A module of its own rather than another block in `pipelines.py`: one
responsibility, no shared state with the launch routes, and `pipelines.py` is
already 1400+ lines.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.queue.governor import LoadGovernor
from app.services import replan_service

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


class ReplanRequest(BaseModel):
    job_type: str
    params: dict = Field(default_factory=dict)
    # Deliberately no budget fields. A client that states its own budget can
    # state a larger one, and the feasibility test becomes a formality.
    # Pydantic ignores unknown keys by default, so one sent anyway is dropped.


@router.post("/replan")
async def replan(body: ReplanRequest) -> dict:
    """Propose a fitting configuration, or say why there is none.

    Returns a tagged union so the card can tell "nothing fits" from "there is
    nothing here to tune" -- different next steps, different prose. The button
    renders only for `proposal`, which is the design's guarantee that it is
    never offered and then refused.

    `replan_service` verifies every proposal against the same estimator that
    produced the refusal, so nothing is re-verified here.
    """
    governor = LoadGovernor()
    result = replan_service.replan(
        job_type=body.job_type,
        params=body.params,
        budget_mb=int(governor.mem_budget_bytes() / (1024 * 1024)),
        cpu_budget=governor.cpu_budget() or 1,
    )
    # The same serializer the enqueue-time refusal uses, so the two paths
    # cannot drift into describing a proposal differently.
    return replan_service.as_payload(result)
```

- [ ] **Step 4: Register the router**

Find the registration block:

```bash
grep -n "include_router" backend/app/main.py
```

Add the import and registration alongside the existing `pipelines` router, matching the surrounding style exactly:

```python
from app.api.v1 import replan as replan_router
...
app.include_router(replan_router.router, prefix="/api/v1")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/api/test_replan_endpoint.py -v
```

Expected: PASS, 3 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/replan.py backend/app/main.py backend/tests/api/test_replan_endpoint.py
git commit -m "feat(api): add POST /pipelines/replan for the refusal card"
```

---

### Task 7: Frontend types and the replan client call

**Files:**
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts:753-762`

There is no headless component-testing setup in this repo and none is expected, so Tasks 7–9 are verified by TypeScript plus manual browser testing in Task 10.

- [ ] **Step 1: Add the types**

Append to `frontend/src/api/types.ts`:

```typescript
/** One knob the re-planner moved. Mirrors replan_service.Change. */
export interface ReplanChange {
  name: string;
  before: number;
  after: number;
}

/**
 * Mirrors replan_service.ReplanResult.
 *
 * A tagged union rather than a nullable proposal: "nothing fits" and "there is
 * nothing here to tune" call for different prose and different next steps, and
 * collapsing both into null loses exactly the distinction the user needs.
 */
export type ReplanResult =
  | {
      kind: "proposal";
      params: Record<string, unknown>;
      estimate_mb: number;
      changes: ReplanChange[];
      note: string;
    }
  | { kind: "infeasible"; reason: string }
  | { kind: "no_knobs" };

/**
 * The `details` payload of a 422 resource refusal.
 *
 * Assembly renders the card straight from this; alignment builds the same
 * shape client-side from its envelope, so both dialogs feed one component.
 */
export interface ResourceRefusalDetails {
  estimate_mb: number;
  budget_mb: number;
  estimate_source: "measured" | "heuristic" | "declared" | "unknown";
  detail: string;
  replan: ReplanResult;
}
```

- [ ] **Step 2: Add the client call**

In `frontend/src/api/client.ts`, alongside `alignEnvelope` (line 760):

```typescript
  replan: (jobType: string, params: Record<string, unknown>) =>
    request<ReplanResult>("/pipelines/replan", {
      method: "POST",
      body: JSON.stringify({ job_type: jobType, params }),
    }),
```

Add `ReplanResult` to the type import block at the top of the file (line 8, beside `AlignEnvelope`).

- [ ] **Step 3: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(frontend): add replan types and client call"
```

---

### Task 8: The ResourceRefusalCard component

**Files:**
- Create: `frontend/src/components/ResourceRefusalCard.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/ResourceRefusalCard.tsx`:

```typescript
import type { ReplanResult } from "../api/types";

/**
 * The four exits from a resource refusal.
 *
 * One component, two triggers. AlignDialog renders it pre-flight from its own
 * client-side band computation; AssembleDialog renders it reactively from a
 * 422 body. Both produce this same props shape.
 */
export interface ResourceRefusalCardProps {
  estimateMb: number;
  budgetMb: number;
  /** The prose phrase from memory_estimate.resolve() -- "from 23 previous
   *  runs on this machine" or "from published tool coefficients". */
  detail: string;
  /** The full explanation sentence naming the dominant term. */
  explanation: string;
  /** null while the replan request is still in flight. */
  replan: ReplanResult | null;
  onCancel: () => void;
  onEdit: () => void;
  onLaunchAnyway: () => void;
  onAcceptReplan: (params: Record<string, unknown>) => void;
}

export function ResourceRefusalCard({
  estimateMb,
  budgetMb,
  detail,
  explanation,
  replan,
  onCancel,
  onEdit,
  onLaunchAnyway,
  onAcceptReplan,
}: ResourceRefusalCardProps) {
  const proposal = replan?.kind === "proposal" ? replan : null;

  return (
    <div className="error-box" style={{ marginBottom: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        This will not fit in the memory budget
      </div>

      <div>{explanation}</div>

      {/* An acceptance criterion, and decision-relevant rather than
          diagnostic: a published coefficient deserves less deference than a
          measurement, and this is what the user is overriding below.
          r_squared is deliberately absent -- resolve() already falls back to
          the heuristic when a measured estimate extrapolates too far, so any
          measured number reaching here is inside its own guard rails. */}
      <div style={{ marginTop: 4, opacity: 0.85 }}>
        Estimated {estimateMb.toLocaleString()} MB {detail}, against a{" "}
        {budgetMb.toLocaleString()} MB budget.
      </div>

      {proposal && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontWeight: 600 }}>A smaller configuration fits:</div>
          <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
            {proposal.changes.map((c) => (
              <li key={c.name}>
                {c.name}: {c.before.toLocaleString()} →{" "}
                {c.after.toLocaleString()}
              </li>
            ))}
          </ul>
          {/* Reported separately from the knob diff on purpose: a capacity
              clamp is a fact about the hardware, while the diff is a fact
              about the budget. Collapsing them loses the explanation a user
              who over-requested threads most needs. */}
          {proposal.note && (
            <div style={{ marginTop: 4 }}>{proposal.note}</div>
          )}
          <div style={{ marginTop: 4, opacity: 0.85 }}>
            Estimated {proposal.estimate_mb.toLocaleString()} MB. Fewer threads
            means a longer run.
          </div>
        </div>
      )}

      {replan?.kind === "infeasible" && (
        <div style={{ marginTop: 8 }}>{replan.reason}</div>
      )}

      {replan?.kind === "no_knobs" && (
        <div style={{ marginTop: 8 }}>
          There is nothing to adjust automatically for this job.
        </div>
      )}

      <div
        style={{
          marginTop: 12,
          display: "flex",
          gap: 8,
          flexWrap: "wrap",
          alignItems: "center",
        }}
      >
        <button type="button" className="btn" onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className="btn" onClick={onEdit}>
          Edit parameters
        </button>
        {/* Renders only for a verified proposal -- the design's guarantee that
            the button is never offered and then refused. */}
        {proposal && (
          <button
            type="button"
            className="btn primary"
            onClick={() => onAcceptReplan(proposal.params)}
          >
            Use the smaller configuration
          </button>
        )}
        <button type="button" className="btn" onClick={onLaunchAnyway}>
          Launch anyway
        </button>
      </div>

      {/* The consequence is stated where the button is, not behind a second
          click: a confirmation step would put the most friction on the
          least-used exit, and the card is already the confirmation. Worded so
          it promises no safety net -- the budget is the user's configured
          limit, so a limit set above physical RAM can still exhaust it. */}
      <div style={{ marginTop: 6, opacity: 0.85, fontSize: "0.9em" }}>
        Launching anyway runs this job only when nothing else is running, and
        it may use more than your configured limit.
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ResourceRefusalCard.tsx
git commit -m "feat(frontend): add the four-choice resource refusal card"
```

---

### Task 9: Wire the card into both dialogs

**Files:**
- Modify: `frontend/src/components/AlignDialog.tsx:106-200, 340-395`
- Modify: `frontend/src/components/AssembleDialog.tsx:86-93, 229`

- [ ] **Step 1: Wire the pre-flight trigger in AlignDialog**

Add imports at the top:

```typescript
import { ResourceRefusalCard } from "./ResourceRefusalCard";
import type { ReplanResult } from "../api/types";
```

Add state beside the other `useState` calls:

```typescript
  // Dismissed by "Edit parameters": the band is still "block", but the user
  // has asked to go back to the fields rather than be shown the card again.
  // Reset whenever the band leaves "block" so a fresh refusal re-renders it.
  const [cardDismissed, setCardDismissed] = useState(false);
```

Add the replan query after the existing `envelope` query (line 106):

```typescript
  // Fetched when the card is about to show, so the button's presence is
  // decided before the user sees it rather than on click.
  const { data: replan } = useQuery<ReplanResult>({
    queryKey: ["pipelines", "replan", "align_reads", params],
    queryFn: () => api.replan("align_reads", params as Record<string, unknown>),
    enabled: band === "block" && params != null,
  });
```

Add the launch-anyway mutation beside the existing `launch` mutation:

```typescript
  const launchAnyway = useMutation({
    mutationFn: () =>
      api.launchAlignment({
        object_id: object.id,
        reference_id: chosenId!,
        mate_object_id: usePair ? mate!.object_id : null,
        paired: usePair,
        read_group: readGroup,
        params,
        resource_override: true,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Launching without the memory check");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });
```

Reset the dismissal when the band recovers — add beside the other derived values:

```typescript
  // A new refusal must re-show the card even if a previous one was dismissed.
  useEffect(() => {
    if (band !== "block") setCardDismissed(false);
  }, [band]);
```

Replace the `bandMessage` block (lines 351-361) with:

```typescript
        {band === "block" && !cardDismissed ? (
          <ResourceRefusalCard
            estimateMb={estimate ?? 0}
            budgetMb={envelope?.mem_budget_mb ?? 0}
            detail={
              // The align path computes its estimate client-side from the
              // envelope's coefficients, so it knows the source without
              // asking: these are the published coefficients by construction.
              "from published tool coefficients"
            }
            explanation={bandMessage ?? ""}
            replan={replan ?? null}
            onCancel={onClose}
            onEdit={() => setCardDismissed(true)}
            onLaunchAnyway={() => launchAnyway.mutate()}
            onAcceptReplan={(p) => {
              setOverrides((o) => ({ ...o, ...p }));
              setCardDismissed(true);
            }}
          />
        ) : (
          bandMessage && (
            <div className={band === "block" ? "error-box" : "warn-box"}>
              {bandMessage}
            </div>
          )
        )}
```

Note the old "Reduce threads or sort memory…" hint is deliberately dropped for the `block` case — the card replaces it with actionable buttons. It remains for `warn`, which still shows the plain banner.

Add `useEffect` to the React import if it is not already there.

- [ ] **Step 2: Verify AlignDialog compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Wire the reactive trigger in AssembleDialog**

Add imports:

```typescript
import { ResourceRefusalCard } from "./ResourceRefusalCard";
import type { ResourceRefusalDetails } from "../api/types";
import { ApiRequestError } from "../api/client";
```

Add state:

```typescript
  // Populated from a 422's `details`. This path is reactive rather than
  // pre-flight: assembly has no envelope endpoint and no client-side mirror
  // of estimate_assembly_mb, so the server's refusal is what produces the card.
  const [refusal, setRefusal] = useState<ResourceRefusalDetails | null>(null);
```

Replace the launch mutation's `onError` (line 93):

```typescript
    onError: (e: Error) => {
      if (
        e instanceof ApiRequestError &&
        e.details &&
        "estimate_mb" in e.details
      ) {
        setRefusal(e.details as unknown as ResourceRefusalDetails);
        return;
      }
      notify.error(e.message);
    },
```

Add a launch-anyway mutation:

```typescript
  const launchAnyway = useMutation({
    mutationFn: () =>
      api.launchAssembly({
        object_id: object.id,
        params,
        resource_override: true,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Launching without the memory check");
      onClose();
    },
    onError: (e: Error) => notify.error(e.message),
  });
```

Render the card above the modal actions (before line 229's button block):

```typescript
        {refusal && (
          <ResourceRefusalCard
            estimateMb={refusal.estimate_mb}
            budgetMb={refusal.budget_mb}
            detail={refusal.detail}
            explanation={
              `This assembly needs about ${refusal.estimate_mb.toLocaleString()} MB, ` +
              `more than the ${refusal.budget_mb.toLocaleString()} MB available.`
            }
            replan={refusal.replan}
            onCancel={onClose}
            onEdit={() => setRefusal(null)}
            onLaunchAnyway={() => launchAnyway.mutate()}
            onAcceptReplan={(p) => {
              setParams((prev) => ({ ...prev, ...p }));
              setRefusal(null);
            }}
          />
        )}
```

> **Note:** check `AssembleDialog`'s actual params state setter name — if it is not `setParams`, use whatever it calls its override setter, matching how the dialog already writes user edits.

Ensure `ApiRequestError` is exported from `frontend/src/api/client.ts`; if it is not, add `export` to its class declaration (around line 122).

- [ ] **Step 4: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AlignDialog.tsx frontend/src/components/AssembleDialog.tsx frontend/src/api/client.ts
git commit -m "feat(frontend): render the refusal card from both launch dialogs"
```

---

### Task 10: Verify against real data, then merge

CLAUDE.md is explicit that a green suite is not enough for rules like these: the Actions tab's suggestion rules passed a full suite while being wrong about real projects, because the fixtures already looked the way the rules expected.

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

UI on 5273, API on 8100. This does not disturb the main instance on 5173.

- [ ] **Step 2: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count, not just the exit code.

- [ ] **Step 3: Exercise the alignment card in the browser**

At `localhost:5273`, open a project with a real reference, open the align dialog, and raise threads and sort memory until the band hits block. Confirm:

- The card replaces the disabled Launch button and the old banner.
- The estimate source line names something real.
- "Edit parameters" returns to the fields with the performance section open.
- "Use the smaller configuration" fills the fields and the card disappears with the band back at ok.
- "Launch anyway" starts a job.

- [ ] **Step 4: Confirm the override reached Redis**

```bash
docker compose -p biopipe-worktree exec redis redis-cli --scan --pattern 'bp:job:*'
```

Then `HGET` the launched job's key and confirm `override` is `1`:

```bash
docker compose -p biopipe-worktree exec redis redis-cli HGET bp:job:<id> override
```

Expected: `1`.

- [ ] **Step 5: Exercise the assembly card**

Open the assemble dialog on a long-read FASTQ whose genome size makes the estimate exceed the budget. Press Launch and confirm the card appears from the 422 rather than an error toast.

- [ ] **Step 6: Check the estimate source against real objects**

```bash
docker compose -p biopipe-worktree exec api python -c "
import asyncio
from app.services import memory_estimate
async def main():
    r = await memory_estimate.resolve(job_type='align_reads', input_bytes=10**9, heuristic_mb=4000)
    print(r.source, '|', r.detail)
asyncio.run(main())
"
```

Confirm the printed detail is the phrase the card shows.

- [ ] **Step 7: Merge and push**

```bash
git checkout main && git pull && git merge claude/issue-70-brainstorm-714db7
```

Re-run the suite after merging if `main` moved, then:

```bash
git push origin main
```

- [ ] **Step 8: Update the issue**

```bash
gh issue close 70 --comment "Shipped. All four exits work from the card; the override persists on the job document and reaches claim.lua through the Redis hash, where it relaxes the memory gate only for a sole occupant. Child jobs never reach the check -- pinned by a regression test rather than a guard, since the property already held."
```

Remove `status:specification document` and apply whatever status label the repo uses for shipped work.

- [ ] **Step 9: Point the main stack back at main**

Only needed if anything repointed the 5173 stack during this work. From the main checkout root:

```bash
docker compose up -d --build api web worker
```

Confirm with:

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

The source path must be the main checkout, not a path under `.claude/worktrees/`.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
| --- | --- |
| Two triggers, one card | 8 (card), 9 (both triggers) |
| Naming the estimate source | 5 (`detail` in payload), 8 (rendering) |
| The four exits | 8, 9 |
| Auto re-plan only for `Proposal` | 6 (endpoint), 8 (conditional render) |
| Accepting fills the form, does not launch | 9 (`onAcceptReplan` writes overrides) |
| Stating the consequence inline | 8 (line beneath the buttons) |
| Re-plan endpoint, budget resolved server-side | 6 |
| Override: persisted, hash, `claim.lua`, sole-occupant | 1, 2, 3, 4 |
| The `ignore_reservations` trap | 4 (`test_an_idle_worker_does_not_count_as_sole_occupancy`) |
| CPU/`io_heavy` unchanged | 4 (`test_override_does_not_relax_the_cpu_gate`) |
| Child jobs: test, not guard | 5 (`test_a_child_job_never_reaches_the_block_check`) |
| Manual verification against real data | 10 |

**Known soft spots**, flagged rather than hidden:

1. **`align_fixture` in Task 5 is not fully specified.** The plan says to find or build it and states the one property that matters (a reference large enough to exceed the budget), because writing a fixture against unseen `conftest.py` contents would be a guess presented as fact.
2. **The `client` fixture in Task 6** is likewise named by reference to neighbouring tests rather than invented.
3. **`AssembleDialog`'s params setter name** is unverified — flagged inline in Task 9.
4. **Task 9's alignment `detail` string is hardcoded** to "from published tool coefficients". That is correct by construction today (the client computes from envelope coefficients, so it is never a measured number), but it will become wrong if the envelope ever starts serving a measured estimate. Worth a comment in the code, which Task 9 includes.
