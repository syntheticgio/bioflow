# Resource Limits Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a user-set memory limit genuinely govern job admission, by first fixing the discarded memory reservation that would otherwise make that limit lie under concurrency.

**Architecture:** Three layers, bottom-up. (1) `compute_free_resources()` learns to subtract the `bp:conc:mem_mb` reservation that `claim.lua` and `release.lua` already maintain and nobody reads. (2) A singleton `ResourceLimits` document stores a user-set memory/CPU/thread budget, following the `AiRouting` upsert-on-read precedent exactly. (3) `worker._free_resources()` uses that stored limit as the ceiling instead of physical RAM. No new enforcement code is written: `claim.lua` already refuses any job where `mem > mem_free`, so the setting is one number flowing into an existing gate.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (MongoDB), Redis + Lua, pytest, React + TypeScript.

**Scope:** Issues [#68](https://github.com/syntheticgio/bioflow/issues/68) and [#22](https://github.com/syntheticgio/bioflow/issues/22) only. The refusal card (#70), estimate resolver (#69), auto re-plan (#71), and cgroup enforcement (#72) are explicitly out of scope.

**Spec:** `docs/superpowers/specs/2026-08-07-resource-limits-admission-design.md`

---

## Background an engineer needs before starting

**The bug is one of omission, and its own test file documents the fix that skipped it.**
`backend/tests/queue/test_free_resources.py` opens by describing this exact class of bug — the worker computing headroom from a job *count* while `claim.lua` reserved real weights into `bp:conc:*`. That was fixed for `cpu` and `io_heavy`. Memory was left out. `claim.lua` still does `INCRBY bp:conc:mem_mb`, `release.lua:32` still does `DECRBY bp:conc:mem_mb`, and nothing reads the result.

**Why nothing catches it.** Over-admission only shows up under concurrent claims of memory-heavy jobs, and the symptom is a busy machine, not an error. Every existing test passes.

**The `in_flight == 0` clamp is load-bearing and must be preserved.** `compute_free_resources()` zeroes reservations when the worker is running nothing, because a missed release can only leak the counter *upward*, and a permanently-high counter would shrink capacity until someone restarted a worker. Memory must join that clamp, not bypass it.

**Do not "fix" `mem_mb` to be a hard guarantee.** Per the spec, this is an admission budget: it governs what we *plan* to start, never what a running job may use. Nothing here kills or caps a process.

**Running tests from this worktree** (per CLAUDE.md — `docker compose exec api` would silently test main's code instead):

```bash
./backend/run-worktree-tests.sh tests/ -q
```

---

## File Structure

**Task 1 — the reservation fix (#68)**
- Modify: `backend/app/queue/worker.py` — `compute_free_resources()` gains `reserved_mem`; `_read_reservations()` reads the third counter; `_free_resources()` passes it through.
- Modify: `backend/tests/queue/test_free_resources.py` — add memory cases alongside the existing cpu/io_heavy ones.

**Task 2-4 — persisted settings (#22)**
- Create: `backend/app/models/resource_limits.py` — the `ResourceLimits` singleton document.
- Modify: `backend/app/models/__init__.py` — register it in `ALL_MODELS` or Beanie never initializes it.
- Create: `backend/app/services/resource_limit_service.py` — load/save, and the resolution of a stored limit against the machine's real budget.
- Create: `backend/tests/services/test_resource_limits.py`
- Modify: `backend/app/api/v1/settings.py` — GET/PUT routes.
- Create: `backend/tests/api/test_settings_resources.py`

**Task 5 — wiring**
- Modify: `backend/app/queue/worker.py` — `_free_resources()` uses the stored limit.
- Create: `backend/tests/queue/test_resource_limit_admission.py`

**Task 6 — UI**
- Create: `frontend/src/components/SettingsResources.tsx`
- Modify: `frontend/src/components/SettingsNav.tsx`, and the settings router.

---

### Task 1: Subtract the memory reservation

**Files:**
- Modify: `backend/app/queue/worker.py:50-88` (`compute_free_resources`), `:258-277` (`_read_reservations`), `:225-257` (`_free_resources`)
- Test: `backend/tests/queue/test_free_resources.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/queue/test_free_resources.py`. Note the `free()` helper at the top of that file needs a `reserved_mem` default — update it first:

```python
def free(**kwargs):
    base = {
        "cpu_budget": 16,
        "mem_mb": 8192,
        "reserved_cpu": 0,
        "reserved_mem": 0,
        "reserved_io_heavy": 0,
        "in_flight": 0,
    }
    return compute_free_resources(**{**base, **kwargs})
```

Then add this class:

```python
class TestMemoryHeadroom:
    """The counterpart of TestCpuHeadroom, missing until now.

    `claim.lua` INCRBYs `bp:conc:mem_mb` and `release.lua` DECRBYs it, but the
    worker never read the counter and `compute_free_resources` had no parameter
    for it -- so a correctly-maintained ledger was written and thrown away.
    """

    def test_an_idle_worker_offers_the_whole_budget(self):
        assert free()["mem_mb"] == 8192

    def test_reservations_are_subtracted(self):
        """The bug. One 6 GB job must leave 2 GB of headroom, not 8."""
        assert free(reserved_mem=6144, in_flight=1)["mem_mb"] == 2048

    def test_two_heavy_jobs_cannot_both_be_offered_the_same_memory(self):
        """The over-admission case that matters, expressed as headroom.

        Two 6 GB alignments against an 8 GB budget: after the first is
        reserved, the remaining headroom must be too small for the second.
        `claim.lua` refuses any candidate where `mem > mem_free`, so this is
        the number that decides it.
        """
        after_first = free(reserved_mem=6144, in_flight=1)["mem_mb"]
        assert after_first < 6144

    def test_never_offers_less_than_zero(self):
        """An over-reserved worker offers nothing rather than a negative,
        which would read as extra capacity and over-admit."""
        assert free(reserved_mem=99999, in_flight=3)["mem_mb"] == 0

    def test_an_idle_worker_ignores_leaked_memory_counters(self):
        """Memory joins the self-healing clamp. A worker running nothing
        cannot still owe a reservation, so a counter left high by a crashed
        worker must not shrink its capacity forever."""
        assert free(reserved_mem=8192, in_flight=0)["mem_mb"] == 8192

    def test_a_busy_worker_still_respects_memory_counters(self):
        """The clamp must not become a way to ignore real reservations."""
        assert free(reserved_mem=4096, in_flight=2)["mem_mb"] == 4096
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/queue/test_free_resources.py -q
```

Expected: every test in the file errors with `TypeError: compute_free_resources() got an unexpected keyword argument 'reserved_mem'`. That error — rather than an assertion failure — is the proof the parameter does not exist yet.

- [ ] **Step 3: Add the parameter and subtract it**

In `backend/app/queue/worker.py`, replace `compute_free_resources` (currently lines 50-88):

```python
def compute_free_resources(
    *,
    cpu_budget: int,
    mem_mb: int,
    reserved_cpu: int,
    reserved_mem: int,
    reserved_io_heavy: int,
    in_flight: int,
) -> dict:
    """Headroom to admit against, from budgets and current reservations.

    Pure, because the failure this guards is not observable in a normal test
    run: reservation counters can only leak *upward* if a release is missed --
    a crashed worker, a lost lease -- and a leak permanently shrinks capacity
    until someone notices the queue has stopped moving.

    The defence is the `in_flight` clamp. Reservations are cluster-wide, but a
    worker also knows how many jobs it is actually running, and a single worker
    cannot be responsible for more reserved capacity than the jobs it holds.
    Taking the smaller of the two means a leaked counter costs at most the
    capacity of the jobs genuinely in flight, and an idle worker always
    recovers full headroom no matter what the counters claim.

    At least 1 CPU is always offered so a fully-reserved queue still drains
    rather than deadlocking against its own bookkeeping. Memory has no such
    floor: offering a phantom megabyte would admit a job that does not fit,
    which is the failure this exists to prevent, and `claim.lua` compares
    `mem <= mem_free` so zero simply admits nothing until something releases.
    """
    if in_flight == 0:
        # Nothing running here, so nothing this worker reserved can still be
        # outstanding. This is the line that makes a leak self-healing.
        effective_cpu_reserved = 0
        effective_mem_reserved = 0
        effective_io_reserved = 0
    else:
        effective_cpu_reserved = reserved_cpu
        effective_mem_reserved = reserved_mem
        effective_io_reserved = reserved_io_heavy

    return {
        "cpu": max(cpu_budget - effective_cpu_reserved, 1),
        "mem_mb": max(mem_mb - effective_mem_reserved, 0),
        "io_heavy": max(IO_HEAVY_LIMIT - effective_io_reserved, 0),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/test_free_resources.py -q
```

Expected: PASS, all tests in the file (the pre-existing cpu/io_heavy ones must still pass — the `free()` helper change affects them too).

- [ ] **Step 5: Read the third counter**

In `backend/app/queue/worker.py`, replace `_read_reservations` (currently lines 258-277):

```python
    async def _read_reservations(self) -> dict:
        """Current cluster-wide reservations, or zeroes if Redis cannot say.

        A failed read must not stall dispatch: falling back to zero reserved
        lets this worker admit against its own in-flight clamp, which is the
        pre-existing behaviour and never over-admits by more than one job.
        """
        try:
            values = await get_redis().mget(
                keys.conc_key("cpu"),
                keys.conc_key("mem_mb"),
                keys.conc_key("io_heavy"),
            )
        except Exception as e:  # noqa: BLE001 - dispatch must survive a Redis blip
            log.warning("reservation_read_failed", error=str(e))
            return {"cpu": 0, "mem_mb": 0, "io_heavy": 0}
        return {
            "cpu": _as_int(values[0]),
            "mem_mb": _as_int(values[1]),
            "io_heavy": _as_int(values[2]),
        }
```

- [ ] **Step 6: Pass it through**

In `_free_resources` (currently lines 225-257), change only the `compute_free_resources(...)` call at the end to add the new argument:

```python
        reserved = await self._read_reservations()
        return compute_free_resources(
            cpu_budget=int(cpu_budget),
            mem_mb=max(min(available_mb, budget_mb), 128),
            reserved_cpu=reserved["cpu"],
            reserved_mem=reserved["mem_mb"],
            reserved_io_heavy=reserved["io_heavy"],
            in_flight=len(self._running),
        )
```

- [ ] **Step 7: Run the full queue suite**

```bash
./backend/run-worktree-tests.sh tests/queue/ -q
```

Expected: PASS. Read the count, not just the exit code (per CLAUDE.md).

- [ ] **Step 8: Commit**

```bash
git add backend/app/queue/worker.py backend/tests/queue/test_free_resources.py
git commit -m "fix(queue): subtract the memory reservation from admission headroom (#68)

claim.lua INCRBYs bp:conc:mem_mb and release.lua DECRBYs it, but
_read_reservations never read the counter and compute_free_resources had no
parameter for it. The ledger was maintained correctly by both scripts and
discarded, so mem_mb_free was a snapshot of currently-free memory that could
not account for jobs already admitted but not yet allocated -- two 6 GB jobs
claimed in the same second both saw full headroom and both were admitted.

Memory joins the in_flight clamp rather than bypassing it, so a missed
release stays self-healing instead of permanently shrinking capacity.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The ResourceLimits singleton document

**Files:**
- Create: `backend/app/models/resource_limits.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/services/test_resource_limits.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_resource_limits.py`:

```python
"""The stored resource budget.

An admission budget, not an enforced ceiling: it governs what BioFlow plans
to start, never what a running job may use. See
docs/superpowers/specs/2026-08-07-resource-limits-admission-design.md.
"""

import pytest

from app.models.resource_limits import ResourceLimits


@pytest.mark.asyncio
class TestLoad:
    async def test_load_creates_the_document_on_first_read(self):
        """Upsert-on-read, matching AiRouting: there is exactly one, and a
        missing one is indistinguishable from a fresh install."""
        loaded = await ResourceLimits.load()
        assert loaded.id == ResourceLimits.SINGLETON_ID

    async def test_a_fresh_install_sets_no_limits(self):
        """None means "use the machine's own budget", which is a real state
        rather than a null needing cleanup -- the same reasoning AiRouting
        uses for an absent slot."""
        loaded = await ResourceLimits.load()
        assert loaded.max_mem_mb is None
        assert loaded.max_cpu is None
        assert loaded.max_threads is None

    async def test_load_returns_the_stored_document_once_saved(self):
        first = await ResourceLimits.load()
        first.max_mem_mb = 16384
        await first.save()

        second = await ResourceLimits.load()
        assert second.max_mem_mb == 16384

    async def test_load_is_idempotent(self):
        """Two loads must not create two documents."""
        await ResourceLimits.load()
        await ResourceLimits.load()
        assert await ResourceLimits.count() == 1
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_resource_limits.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.resource_limits'`.

- [ ] **Step 3: Create the model**

Create `backend/app/models/resource_limits.py`:

```python
"""The user's resource budget for admission decisions.

**An admission budget, not an enforced ceiling.** These numbers govern what
BioFlow plans to start; they do not cap or kill a running process. A job that
overruns its prediction goes over the limit, and that is an accepted outcome
-- see docs/superpowers/specs/2026-08-07-resource-limits-admission-design.md
for why enforcement-by-OOM-kill was rejected as the default.

The wording matters wherever these are surfaced: "will not plan to exceed",
never "will never exceed".

Exactly one document, and not a TimestampedDocument: it carries no `owner`,
deliberately. There is one machine here, matching the reasoning that leaves
AI provider settings unscoped -- a profile header should not change how much
memory the host has.
"""

from datetime import datetime
from typing import ClassVar

from beanie import Document
from pydantic import Field

from app.models.base import utcnow


class ResourceLimits(Document):
    """The stored budget. `None` on any field means "use the machine's own".

    None is a real state rather than a null needing cleanup: a fresh install
    has no opinion, and the machine's actual budget is the right default. The
    UI's "No limit" option writes None rather than a sentinel number.
    """

    SINGLETON_ID: ClassVar[str] = "resource_limits"

    id: str = Field(default=SINGLETON_ID)

    # Admission budget for memory. The one that actually binds today: it
    # replaces physical RAM as the ceiling `worker._free_resources` computes
    # headroom against, and `claim.lua` already refuses any job whose declared
    # mem_mb exceeds that headroom.
    max_mem_mb: int | None = None

    # Cores the governor may admit against.
    max_cpu: float | None = None

    # A default thread count ceiling for pipeline parameters. Advisory: it
    # bounds what the launch dialog offers, and does not stop a directly-called
    # API from asking for more.
    max_threads: int | None = None

    updated_at: datetime = Field(default_factory=utcnow)

    @classmethod
    async def load(cls) -> "ResourceLimits":
        """The limits document, creating it on first read.

        Upsert-on-read rather than a migration, for the same reasons AiRouting
        does it: there is exactly one, its empty state is meaningful, and a
        missing one is indistinguishable from a fresh install.
        """
        found = await cls.get(cls.SINGLETON_ID)
        if found is not None:
            return found
        created = cls()
        await created.insert()
        return created

    class Settings:
        name = "resource_limits"
```

- [ ] **Step 4: Register the model**

In `backend/app/models/__init__.py`, add the import alongside the others:

```python
from app.models.resource_limits import ResourceLimits
```

Add `ResourceLimits,` to the `ALL_MODELS` list, and `"ResourceLimits",` to `__all__`.

Beanie only initializes documents listed in `ALL_MODELS`. A model omitted from it raises `CollectionWasNotInitialized` on first use — not at import — so the failure surfaces at runtime rather than at startup.

- [ ] **Step 5: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_resource_limits.py -q
```

Expected: PASS, 4 tests.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/resource_limits.py backend/app/models/__init__.py backend/tests/services/test_resource_limits.py
git commit -m "feat(models): ResourceLimits singleton for the admission budget (#22)

Upsert-on-read singleton following the AiRouting precedent. None on any field
means 'use the machine's own budget' -- a real state rather than a null, so
the UI's 'No limit' writes None instead of a sentinel.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Resolve a stored limit against the machine's budget

**Files:**
- Create: `backend/app/services/resource_limit_service.py`
- Test: `backend/tests/services/test_resource_limits.py` (append)

The worker needs one number: the memory ceiling to compute headroom against. That is not simply the stored value — a user who types 64 GB on a 16 GB machine must not get 64 GB of admission headroom, because the limit is a *budget*, not a wish. This resolution is its own function so it can be tested without a worker.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_resource_limits.py`:

```python
from app.services import resource_limit_service


class TestResolveMemBudget:
    """A stored limit resolved against what the machine actually has.

    Pure, so the clamping rules are testable without a worker or a host probe.
    """

    def test_no_stored_limit_uses_the_machine_budget(self):
        assert resource_limit_service.resolve_mem_budget_mb(
            stored_mb=None, machine_mb=16384
        ) == 16384

    def test_a_stored_limit_below_the_machine_budget_wins(self):
        """The whole point: the user asked for less than the host has."""
        assert resource_limit_service.resolve_mem_budget_mb(
            stored_mb=8192, machine_mb=16384
        ) == 8192

    def test_a_stored_limit_above_the_machine_budget_is_clamped(self):
        """Typing 64 GB on a 16 GB machine cannot conjure headroom. The limit
        is a budget to stay under, not a claim about the hardware."""
        assert resource_limit_service.resolve_mem_budget_mb(
            stored_mb=65536, machine_mb=16384
        ) == 16384

    def test_a_zero_or_negative_stored_limit_is_ignored(self):
        """Zero would admit nothing at all and stall the queue silently.
        Treated as 'no opinion' rather than as a real ceiling."""
        assert resource_limit_service.resolve_mem_budget_mb(
            stored_mb=0, machine_mb=16384
        ) == 16384
        assert resource_limit_service.resolve_mem_budget_mb(
            stored_mb=-5, machine_mb=16384
        ) == 16384
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_resource_limits.py -q
```

Expected: FAIL — `ImportError: cannot import name 'resource_limit_service'`.

- [ ] **Step 3: Write the service**

Create `backend/app/services/resource_limit_service.py`:

```python
"""Reading the user's resource budget, and resolving it against the host.

Split from the model so the arithmetic is pure and testable without a worker
or a host probe -- the same reason `worker.compute_free_resources` is pure.
"""

from app.models.resource_limits import ResourceLimits


def resolve_mem_budget_mb(*, stored_mb: int | None, machine_mb: int) -> int:
    """The memory ceiling admission should compute headroom against.

    A stored limit only ever *lowers* the budget. Typing 64 GB on a 16 GB
    machine cannot conjure headroom, and letting it try would over-admit
    exactly as badly as having no limit at all -- the number is a budget to
    stay under, not a claim about the hardware.

    Zero and negatives are treated as "no opinion" rather than as a real
    ceiling of nothing. A literal zero budget would admit no job ever and
    stall the queue with no error anywhere, which is the silent-failure shape
    this codebase already goes out of its way to avoid.
    """
    if stored_mb is None or stored_mb <= 0:
        return machine_mb
    return min(stored_mb, machine_mb)


async def load() -> ResourceLimits:
    """The stored limits, created on first read."""
    return await ResourceLimits.load()


async def save(
    *,
    max_mem_mb: int | None,
    max_cpu: float | None,
    max_threads: int | None,
) -> ResourceLimits:
    """Replace the stored limits.

    Every field is written on every save, including None. The UI's "No limit"
    must be able to *clear* a previously-set ceiling, so an absent value here
    means "no limit" rather than "leave unchanged" -- the opposite of
    ProviderUpdate's three-way api_key semantics, and deliberately simpler
    because there is no secret to preserve.
    """
    from app.models.base import utcnow

    limits = await ResourceLimits.load()
    limits.max_mem_mb = max_mem_mb
    limits.max_cpu = max_cpu
    limits.max_threads = max_threads
    limits.updated_at = utcnow()
    await limits.save()
    return limits
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_resource_limits.py -q
```

Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/resource_limit_service.py backend/tests/services/test_resource_limits.py
git commit -m "feat(services): resolve a stored memory limit against the host budget (#22)

A stored limit only ever lowers the budget -- typing 64 GB on a 16 GB machine
cannot conjure headroom. Zero and negatives are 'no opinion' rather than a
literal ceiling of nothing, which would admit no job and stall the queue with
no error anywhere.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The settings API

**Files:**
- Modify: `backend/app/api/v1/settings.py`
- Test: `backend/tests/api/test_settings_resources.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_settings_resources.py`:

```python
"""The resource-limit settings surface.

Unscoped by profile, matching the AI settings in the same module: there is one
machine here, so a profile header cannot change how much memory it has.
"""

import pytest


@pytest.mark.asyncio
class TestGetLimits:
    async def test_a_fresh_install_reports_no_limits(self, client):
        resp = await client.get("/api/v1/settings/resources")
        assert resp.status_code == 200
        body = resp.json()
        assert body["max_mem_mb"] is None
        assert body["max_cpu"] is None
        assert body["max_threads"] is None

    async def test_it_reports_the_machine_budget_alongside(self, client):
        """The UI needs the host's actual capacity to render a sensible
        slider range and to say what "no limit" currently resolves to."""
        resp = await client.get("/api/v1/settings/resources")
        body = resp.json()
        assert body["machine_mem_mb"] > 0
        assert body["machine_cpu"] > 0


@pytest.mark.asyncio
class TestPutLimits:
    async def test_it_stores_a_limit(self, client):
        resp = await client.put(
            "/api/v1/settings/resources",
            json={"max_mem_mb": 8192, "max_cpu": 4, "max_threads": 8},
        )
        assert resp.status_code == 200
        assert resp.json()["max_mem_mb"] == 8192

        again = await client.get("/api/v1/settings/resources")
        assert again.json()["max_mem_mb"] == 8192

    async def test_null_clears_a_previously_set_limit(self, client):
        """"No limit" must be able to undo a limit. An absent value means no
        limit rather than 'leave unchanged' -- there is no secret to preserve
        here, unlike the AI provider key."""
        await client.put(
            "/api/v1/settings/resources",
            json={"max_mem_mb": 8192, "max_cpu": None, "max_threads": None},
        )
        await client.put(
            "/api/v1/settings/resources",
            json={"max_mem_mb": None, "max_cpu": None, "max_threads": None},
        )
        resp = await client.get("/api/v1/settings/resources")
        assert resp.json()["max_mem_mb"] is None

    async def test_it_rejects_a_zero_or_negative_memory_limit(self, client):
        """A literal zero budget would admit no job ever and stall the queue
        with no error anywhere. Refused at the edge rather than silently
        reinterpreted, so the user learns their input was meaningless."""
        resp = await client.put(
            "/api/v1/settings/resources",
            json={"max_mem_mb": 0, "max_cpu": None, "max_threads": None},
        )
        assert resp.status_code == 422
```

The `client` fixture is defined at `backend/tests/api/conftest.py:21`.

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/api/test_settings_resources.py -q
```

Expected: FAIL with 404s on every request — the routes do not exist.

- [ ] **Step 3: Add the routes**

In `backend/app/api/v1/settings.py`, add these imports at the top:

```python
import psutil

from app.services import resource_limit_service
```

Add the schemas alongside the existing ones:

```python
class ResourceLimitsOut(BaseModel):
    """The stored budget, plus what the machine actually has.

    The machine numbers are reported alongside so the UI can render a range
    and say what "no limit" resolves to right now, without a second request.
    """

    max_mem_mb: int | None
    max_cpu: float | None
    max_threads: int | None
    machine_mem_mb: int
    machine_cpu: float


class ResourceLimitsIn(BaseModel):
    """Every field is written on every save, including None.

    Absent means "no limit", not "leave unchanged": the UI's "No limit" option
    has to be able to clear a ceiling that was set earlier. Deliberately
    simpler than ProviderUpdate's three-way api_key semantics -- there is no
    secret here to accidentally erase.
    """

    max_mem_mb: int | None = Field(default=None, gt=0)
    max_cpu: float | None = Field(default=None, gt=0)
    max_threads: int | None = Field(default=None, gt=0)
```

Add the routes at the end of the file:

```python
def _machine_budget() -> tuple[int, float]:
    """What this host actually has, via the governor's cgroup-aware readers.

    Uses the governor rather than psutil directly: inside Docker the cgroup
    limit is the number that binds, and psutil reports the Linux VM's
    resources rather than the container's.
    """
    from app.queue.governor import LoadGovernor

    governor = LoadGovernor()
    return int(governor.mem_budget_bytes() / (1024 * 1024)), governor.cpu_budget()


def _limits_out(limits) -> ResourceLimitsOut:
    machine_mem_mb, machine_cpu = _machine_budget()
    return ResourceLimitsOut(
        max_mem_mb=limits.max_mem_mb,
        max_cpu=limits.max_cpu,
        max_threads=limits.max_threads,
        machine_mem_mb=machine_mem_mb,
        machine_cpu=machine_cpu,
    )


@router.get("/resources", response_model=ResourceLimitsOut)
async def get_resource_limits() -> ResourceLimitsOut:
    return _limits_out(await resource_limit_service.load())


@router.put("/resources", response_model=ResourceLimitsOut)
async def set_resource_limits(body: ResourceLimitsIn) -> ResourceLimitsOut:
    limits = await resource_limit_service.save(
        max_mem_mb=body.max_mem_mb,
        max_cpu=body.max_cpu,
        max_threads=body.max_threads,
    )
    return _limits_out(limits)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/api/test_settings_resources.py -q
```

Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/settings.py backend/tests/api/test_settings_resources.py
git commit -m "feat(api): GET/PUT settings/resources for the admission budget (#22)

Reports the machine's own budget alongside the stored one so the UI can render
a range and say what 'no limit' resolves to. A zero limit is refused at the
edge rather than silently reinterpreted -- it would otherwise admit no job and
stall the queue with nothing in the logs.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Wire the limit into admission

**Files:**
- Modify: `backend/app/queue/worker.py:225-257` (`_free_resources`)
- Test: `backend/tests/queue/test_resource_limit_admission.py`

This is where the setting starts to matter. No new enforcement code: `claim.lua` already refuses any candidate where `mem > mem_free`, so lowering `mem_mb_free` *is* the enforcement.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_resource_limit_admission.py`:

```python
"""The stored limit reaching admission.

No new enforcement exists for this: `claim.lua` already refuses any candidate
whose declared `mem_mb` exceeds `mem_mb_free`, so the setting is one number
flowing into a gate that was already there.
"""

import pytest

from app.models.resource_limits import ResourceLimits
from app.queue.worker import Worker


@pytest.mark.asyncio
class TestStoredLimitLowersHeadroom:
    async def test_a_stored_limit_reduces_offered_memory(self, monkeypatch):
        """The user set 2 GB; admission must offer no more than that however
        much RAM the host reports."""
        limits = await ResourceLimits.load()
        limits.max_mem_mb = 2048
        await limits.save()

        worker = Worker(worker_id="test-worker")
        monkeypatch.setattr(
            worker, "_read_reservations", _fake_reservations(cpu=0, mem_mb=0, io=0)
        )

        free = await worker._free_resources()
        assert free["mem_mb"] <= 2048

    async def test_no_stored_limit_leaves_behaviour_unchanged(self, monkeypatch):
        """A fresh install must admit exactly as it did before this feature."""
        limits = await ResourceLimits.load()
        limits.max_mem_mb = None
        await limits.save()

        worker = Worker(worker_id="test-worker")
        monkeypatch.setattr(
            worker, "_read_reservations", _fake_reservations(cpu=0, mem_mb=0, io=0)
        )

        free = await worker._free_resources()
        assert free["mem_mb"] > 0

    async def test_a_reservation_subtracts_from_the_stored_limit(self, monkeypatch):
        """The two halves of this slice composed: with a 4 GB limit and 3 GB
        already reserved, only 1 GB may be offered. Without Task 1's fix this
        would report the full 4 GB and over-admit."""
        limits = await ResourceLimits.load()
        limits.max_mem_mb = 4096
        await limits.save()

        worker = Worker(worker_id="test-worker")
        worker._running = {"job-1": (None, None, 0)}
        monkeypatch.setattr(
            worker, "_read_reservations", _fake_reservations(cpu=0, mem_mb=3072, io=0)
        )

        free = await worker._free_resources()
        assert free["mem_mb"] <= 1024


def _fake_reservations(*, cpu: int, mem_mb: int, io: int):
    async def _read():
        return {"cpu": cpu, "mem_mb": mem_mb, "io_heavy": io}

    return _read
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_resource_limit_admission.py -q
```

Expected: FAIL on `test_a_stored_limit_reduces_offered_memory` — the stored limit is not read, so headroom comes from physical RAM and exceeds 2048.

- [ ] **Step 3: Read the stored limit in `_free_resources`**

In `backend/app/queue/worker.py`, replace the budget-resolution block at the top of `_free_resources` (currently lines 236-247, from `if self._local_governor is not None:` through the `budget_mb = ...` line):

```python
        if self._local_governor is not None:
            cpu_budget = self._local_governor.cpu_budget()
            mem_budget = self._local_governor.mem_budget_bytes()
        else:
            cpu_budget = float(psutil.cpu_count() or 4)
            mem_budget = psutil.virtual_memory().total

        # The user's admission budget, if they set one. It only ever lowers
        # the ceiling -- see resource_limit_service.resolve_mem_budget_mb.
        #
        # This is the entire enforcement path for the setting: `claim.lua`
        # already refuses any candidate whose declared mem_mb exceeds
        # mem_mb_free, so a smaller ceiling here *is* the limit taking effect.
        # A read failure falls back to the machine budget rather than stalling
        # dispatch, matching _read_reservations' policy for the same reason.
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

Add the import at the top of `backend/app/queue/worker.py`, alongside `from app.services import run_service`:

```python
from app.services import resource_limit_service, run_service
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_resource_limit_admission.py -q
```

Expected: PASS, 3 tests.

- [ ] **Step 5: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count — CLAUDE.md is explicit that "green" means reading the number, not the exit code of the last thing in the pipeline.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/worker.py backend/tests/queue/test_resource_limit_admission.py
git commit -m "feat(queue): admission respects the stored resource limit (#22)

_free_resources resolves the stored budget against the machine's own and
computes headroom from the lower of the two. No new enforcement: claim.lua
already refuses any candidate whose declared mem_mb exceeds mem_mb_free, so
a smaller ceiling is the limit taking effect.

A read failure falls back to the machine budget rather than stalling dispatch.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: The settings UI

**Files:**
- Create: `frontend/src/components/SettingsResources.tsx`
- Modify: `frontend/src/components/SettingsNav.tsx:16-18`
- Modify: `frontend/src/App.tsx:30` (import) and `:111` (route table, beside `/settings/tools`)

There is no headless component-testing setup in this repo and none is expected (CLAUDE.md). Verification is manual, in the browser.

- [ ] **Step 1: Read the neighbouring page for house style**

```bash
sed -n 1,80p frontend/src/components/SettingsTools.tsx
```

Match its data-fetching approach, loading/error states, and styling conventions rather than inventing new ones.

- [ ] **Step 2: Build the page**

Create `frontend/src/components/SettingsResources.tsx` with:

- A GET of `/api/v1/settings/resources` on mount.
- A memory input, with a "No limit" affordance that PUTs `null`.
- The machine's own capacity shown as context — e.g. "This machine has 16384 MB" — so the number the user types has a reference point.
- A PUT on save, with the response used to update local state.

**The wording is a requirement, not a detail.** The page must say the limit governs what BioFlow *plans to start* — for example: "BioFlow will not start work it expects to exceed this. A job that uses more than predicted is not stopped." It must not say "never exceed". Per the spec, a mispredicted job will go over, and a UI promising otherwise makes the first overrun read as a bug rather than as designed behaviour.

- [ ] **Step 3: Add the nav entry**

In `frontend/src/components/SettingsNav.tsx`, add to the `items` array (currently lines 16-18):

```tsx
    { to: "/settings/resources", label: "Resources" },
```

Add the matching route wherever `/settings/tools` is registered.

- [ ] **Step 4: Rebuild and verify in the browser**

From this worktree (per CLAUDE.md — plain `docker compose` from a worktree silently repoints the main stack):

```bash
./ops/worktree-up.sh
```

Open http://localhost:5273/settings/resources and confirm:
- The page loads and shows the machine's capacity.
- Setting a limit persists across a reload.
- "No limit" clears a previously-set value.
- The wording says "will not start work it expects to exceed", not "will never exceed".

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/SettingsResources.tsx frontend/src/components/SettingsNav.tsx
git commit -m "feat(frontend): resource limits settings page (#22)

Says the limit governs what BioFlow plans to start, not what it may use --
per the spec, a mispredicted job will go over, and promising otherwise would
make the first overrun read as a bug.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Close out the backlog entry and the issues

Per CLAUDE.md, finishing the work is not finishing the entry — this has gone wrong three times before.

- [ ] **Step 1: Check the rule against real data**

CLAUDE.md is explicit that a green suite is not enough for a rule like this; the suggestion rules passed green while being wrong because fixtures already looked the way the code expected. From the main repo root:

```bash
docker compose exec api python -c "
import asyncio
from app.db.mongo import connect_to_mongo
from app.queue.worker import Worker
async def main():
    await connect_to_mongo()
    w = Worker(worker_id='probe')
    print(await w._free_resources())
asyncio.run(main())
"
```

Confirm the reported `mem_mb` reflects the stored limit rather than physical RAM.

- [ ] **Step 2: Update `docs/TODO.md`**

The entry is "Resource limits and intelligent enforcement" at `docs/TODO.md:224`. It is **partially** resolved — the settings and reservation halves shipped; the refusal card, resolver, re-plan, and cgroups did not. Per CLAUDE.md, a partially-resolved entry **stays in `docs/TODO.md`** rather than moving to `TODO-done.md`, because moving it would bury the still-open part.

Add a note under the heading recording: what shipped (the `bp:conc:mem_mb` fix and the persisted budget), what the implementation did differently from the entry's own plan (the entry proposed cgroups as option 1 and treated the load governor as the enforcement mechanism; the design chose admission-time budgeting instead, and found the enforcement gate already existed in `claim.lua`), and links to #69/#70/#71/#72 for the remainder. Keep the original body.

- [ ] **Step 3: Close the issues**

```bash
gh issue close 68 --comment "Fixed: _read_reservations now reads bp:conc:mem_mb and compute_free_resources subtracts it, inside the existing in_flight clamp. Tests in backend/tests/queue/test_free_resources.py::TestMemoryHeadroom."
gh issue close 22 --comment "Shipped: ResourceLimits singleton, GET/PUT /api/v1/settings/resources, and _free_resources resolving the stored budget against the machine's own. No new enforcement was needed -- claim.lua already gates on mem <= mem_free."
```

Comment on #7 noting the first slice is complete and #69/#70/#71/#72 remain.

- [ ] **Step 4: Merge and push**

Per CLAUDE.md: once the suite is green and `main` is clean, merge and push without asking. Re-run the suite after merging if `main` has moved.

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Then merge to `main` and push to `origin`.

---

## Self-Review

**Spec coverage.** The spec's in-scope list has three items: the reservation fix (Task 1), the persisted limit (Tasks 2-4), and wiring it into `_free_resources` (Task 5). All three have tasks. The spec's testing section names three required tests — the pure `compute_free_resources` unit test, the over-admission case asserting the *second* job is refused, and the in-flight clamp — all three are in Task 1 Step 1. Out-of-scope items (#69-#72) have no tasks here, correctly.

**Placeholders.** None. Every code step contains the code; the one step that cannot (Task 6's page body, since it must match a house style the engineer reads in Step 1) names its required content and its wording constraint explicitly.

**Type consistency.** `compute_free_resources(reserved_mem=...)` is defined in Task 1 Step 3 and used with that exact keyword in Task 1 Step 6 and the Task 1 tests. `_read_reservations` returns `{"cpu", "mem_mb", "io_heavy"}` in Task 1 Step 5 and is faked with those three keys in Task 5. `resolve_mem_budget_mb(stored_mb=, machine_mb=)` is defined in Task 3 and called with both keywords in Task 5. `ResourceLimits.max_mem_mb / max_cpu / max_threads` are consistent across Tasks 2, 3, 4, and 5.

**One known gap, deliberate.** `max_threads` is stored and exposed but nothing consumes it in this slice — it bounds what the launch dialog offers, which lives in the dialog work under #70. #22's acceptance criteria require the stored shape to cover threads, so it is stored; wiring it is out of scope. The model docstring says so.
