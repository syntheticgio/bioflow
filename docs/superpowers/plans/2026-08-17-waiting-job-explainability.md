# Waiting-Job Explainability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Activity view say *why* a queued job is not running — which gate is blocking it, with the numbers — instead of a bare `WAITING`.

**Architecture:** Three layers. Layer 0 wires the existing `waitingReason()` into the run card that renders user-launched jobs (the literal fix for the screenshot in #457). Layer 1 makes `claim.lua` record the gate it failed on, since that script is the only place the answer is authoritative. Layer 2 carries the declared demand and the recorded reason to the frontend and adds the unsatisfiable case.

**Tech Stack:** Python 3.12 / FastAPI / Beanie+MongoDB / Redis (Lua scripts, tested against fakeredis) / React + TypeScript / TanStack Query

**Spec:** [`docs/superpowers/specs/2026-08-17-waiting-job-explainability-design.md`](../specs/2026-08-17-waiting-job-explainability-design.md)

## Global Constraints

- **Conventional Commits.** `<type>(<scope>): <subject>`, imperative, lowercase after the colon, no trailing period, ~65 chars. Scopes in use here: `queue`, `api`, `frontend`, `ui`.
- **Commits stay separable.** A mechanical change and a behaviour change are two commits.
- **Run tests from the worktree with `./backend/run-worktree-tests.sh`**, never `docker compose exec api` — the latter silently tests `main`'s code.
- **Gate order is fixed and identical everywhere:** `class`, `cpu`, `mem`, `io` (spec R2).
- **Reason TTL is 15 seconds**, matching `governor.SNAPSHOT_TTL`, so a stale reason expires rather than being shown as current (spec R5).
- **Recording must never change which job is selected** (spec R6).
- **No new dependencies.**

## Verified Facts

These were checked against the running code while writing this plan. Do not re-derive them; do not assume the opposite.

1. **`fakeredis` supports `cjson.encode` and `SET ... EX` inside Lua.** Probed directly: a script calling `cjson.encode({gate='mem', need=32768, free=12288})` then `SET ... 'EX', 15` returned `{"free":12288,"need":32768,"gate":"mem"}` with a positive TTL. Key ordering in the encoded JSON is **not** stable — parse it, never string-compare it.
2. **`queue.claim()` never forwards `node_id` or `ready_key` to the script.** It accepts both (`queue.py:489-500`) but sends only 9 ARGV and always keys on `keys.READY` (`queue.py:515-528`). So `claim.lua`'s `ARGV[10]` is always empty on the production path. **Do not build the reason key on `node_id`** — it would always be the global one. Task 8 files this as its own issue; do not fix it here.
3. **`backend/tests/queue/conftest.py` runs the real Lua against fakeredis**, with a `job_factory` fixture taking `job_id, job_class, cpu, mem_mb, io`. `ALL_CLASSES = "user_interactive,user_background,maintenance,bulk"`.
4. **`test_claim.py`'s local `claim()` helper passes 9 ARGV.** New tests reuse that helper.
5. **`RunMemberJob` (`frontend/src/api/types.ts:895`) is a different type from `JobSummary`** and carries no resources. `LeadStep` in `ActivityLead.tsx:216` renders it. This is why Layer 0 and Layer 2 touch different types.
6. **Baseline is green:** `tests/queue/test_claim.py` → 24 passed.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/queue/scripts/claim.lua` | Modify: record the failing gate for the head-of-queue candidate |
| `backend/app/queue/blocked_reason.py` | **Create:** the key, the TTL, and typed read access |
| `backend/app/queue/queue.py` | Modify: expose the recorded reason to callers |
| `backend/app/services/run_service.py:223-236` | Modify: add `resources` + `blocked_reason` to each member job |
| `backend/app/api/v1/system.py` | Modify: nothing new — reason rides `/system/load` |
| `frontend/src/api/types.ts` | Modify: `BlockedReason`, extend `RunMemberJob` |
| `frontend/src/lib/runFormat.ts:185` | Modify: prefer a recorded reason; add unsatisfiable |
| `frontend/src/components/activity/ActivityLead.tsx` | Modify: thread `load` to `LeadStep` |
| `frontend/src/components/ActivityView.tsx:158` | Modify: pass `load` into `ActivityLead` |

---

## Task 1: Show a reason on run-owned waiting jobs (Layer 0)

The literal fix for #457's screenshot, using only machinery that already exists. Ships alone.

**Files:**
- Modify: `frontend/src/components/activity/ActivityLead.tsx`
- Modify: `frontend/src/components/ActivityView.tsx:158-163`

**Interfaces:**
- Consumes: `waitingReason(job, load)` from `lib/runFormat.ts:185`, `SystemLoad` from `api/types.ts:454`
- Produces: `ActivityLead` and `LeadStory` accept an optional `load?: SystemLoad` prop; `LeadStep` accepts `load?: SystemLoad`

There is no headless component-test setup in this repo (no jsdom, zero `.test.tsx`), so this task is verified by reading the UI, per CLAUDE.md.

- [ ] **Step 1: Thread `load` through `ActivityLead` and `LeadStory`**

In `ActivityLead.tsx`, add `SystemLoad` to the type import from `../../api/types`, then add the prop to both components.

```tsx
export function ActivityLead({
  runs,
  workflows = [],
  details,
  load,
  onSelect,
}: {
  runs: RunSummary[];
  workflows?: WorkflowRunRow[];
  details: Map<string, RunDetail>;
  /** Drives each waiting step's reason. Optional: the card renders before
   *  the first /system/load response arrives. */
  load?: SystemLoad;
  onSelect: (objectId: string, projectId: string) => void;
}) {
```

Pass it down at the `LeadStory` call site:

```tsx
          <LeadStory
            key={lead.id}
            run={lead}
            detail={details.get(lead.id)}
            load={load}
            onSelect={onSelect}
          />
```

And accept it on `LeadStory`:

```tsx
function LeadStory({
  run,
  detail,
  load,
  onSelect,
}: {
  run: RunSummary;
  detail?: RunDetail;
  load?: SystemLoad;
  onSelect: (objectId: string, projectId: string) => void;
}) {
```

- [ ] **Step 2: Render the reason in `LeadStep`**

`waitingReason` takes a `JobSummary`, but `LeadStep` has a `RunMemberJob`. Build the minimal shape it actually reads (`cancel_requested`, `state`, `job_class`) rather than casting. Replace `LeadStep` in `ActivityLead.tsx:216-243`:

```tsx
function LeadStep({ job, load }: { job: RunMemberJob; load?: SystemLoad }) {
  // A pruned job has no state to show. Saying so beats inventing one.
  const state = job.state ?? "expired";
  const pct =
    job.state === "running" && job.progress?.pct
      ? ` ${Math.round(job.progress.pct * 100)}%`
      : "";

  // #457: a run-owned job showed a bare "queued" with nothing saying what it
  // was queued behind. waitingReason already answered this for loose jobs in
  // the "Other waiting" section; this is the same sentence on the card users
  // actually watch.
  const why =
    job.state !== null && WAITING.has(job.state)
      ? waitingReason(
          {
            state: job.state,
            job_class: job.job_class,
            cancel_requested: job.cancel_requested,
          },
          load,
        )
      : null;

  return (
    <div className="lead-step">
      <span className={`lead-step-state ${state}`}>{state}</span>
      <span className="lead-step-label">
        {ROLE_LABELS[job.role] ?? job.role}
        {pct}
        {job.shared && (
          <span
            className="lead-step-shared"
            title="Reused from an earlier run — this run did not do this work"
          >
            reused
          </span>
        )}
      </span>
      {why && <span className="lead-step-why">{why}</span>}
      {job.error && <span className="lead-step-error">{job.error.message}</span>}
      <span className="lead-step-time">{formatClock(job.created_at)}</span>
    </div>
  );
}
```

Update the import on line 13 and add `WAITING`:

```tsx
import {
  ROLE_LABELS,
  STATUS_LABELS,
  WAITING,
  kindAction,
  runFacts,
  waitingReason,
} from "../../lib/runFormat";
```

Pass `load` at the `LeadStep` call site inside `LeadStory`:

```tsx
        {steps.map((job) => (
          <LeadStep key={job.job_id} job={job} load={load} />
        ))}
```

- [ ] **Step 3: Widen `waitingReason`'s parameter**

`waitingReason` currently takes a full `JobSummary`, but reads only three fields. Narrow the parameter so both call sites type-check. In `frontend/src/lib/runFormat.ts`, replace the signature at line 185:

```ts
/** The fields `waitingReason` actually reads. Narrowed so a run member job
 *  — which is not a JobSummary and has no payload — can ask the same
 *  question and get the same words back (#457). */
export type WaitingJob = {
  state: string;
  job_class: string;
  cancel_requested?: boolean;
};

export function waitingReason(job: WaitingJob, load?: SystemLoad): string {
```

The body is unchanged. `JobSummary` structurally satisfies `WaitingJob`, so the existing `JobRow` call site keeps compiling untouched.

- [ ] **Step 4: Add `job_class` and `cancel_requested` to `RunMemberJob`**

The reason cannot name the governor gate without the job's class. In `frontend/src/api/types.ts`, extend the interface at line 895:

```ts
export interface RunMemberJob {
  job_id: string;
  role: RunJobRole;
  /** True when this run reused a job another run created. */
  shared: boolean;
  /** Null once the job has been pruned by the 30-day TTL. */
  type: string | null;
  state: JobState | null;
  /** Null for a pruned job. Drives the governor branch of waitingReason. */
  job_class: JobClass | null;
  cancel_requested: boolean;
  progress: JobSummary["progress"] | null;
  error: { code: string; message: string; retryable: boolean } | null;
  created_at: string | null;
}
```

`job_class` is `JobClass | null`, so pass `job.job_class ?? ""` where `WaitingJob` wants a string — an empty class matches no admitted class, which correctly falls through to the generic wording.

Adjust the Step 2 call accordingly:

```tsx
          job_class: job.job_class ?? "",
```

- [ ] **Step 5: Serve the two new fields**

In `backend/app/services/run_service.py`, extend the dict built at lines 223-236:

```python
        detail.append(
            {
                "job_id": str(link.job_id),
                "role": link.role.value,
                "shared": link.shared,
                # Absent when the job has been pruned. The UI says "expired"
                # rather than inventing a state.
                "type": job.type if job else None,
                "state": job.state.value if job else None,
                # Both drive the waiting reason on the run card (#457): the
                # class decides whether the governor is what is holding this
                # job, and a cancelling job must not read as "waiting".
                "job_class": job.job_class.value if job else None,
                "cancel_requested": bool(job.cancel_requested) if job else False,
                "progress": job.progress.model_dump(mode="json") if job else None,
                "error": job.error.model_dump(mode="json") if job and job.error else None,
                "created_at": job.created_at if job else None,
            }
        )
```

- [ ] **Step 6: Pass `load` from `ActivityView`**

`ActivityView.tsx` already holds `load`. Pass it at line 158:

```tsx
        <ActivityLead
          runs={activeRuns}
          workflows={workflows.active}
          details={details}
          load={load}
          onSelect={selectObject}
        />
```

- [ ] **Step 7: Style the reason line**

Find the `.lead-step-error` rule in the stylesheet (`grep -rn "lead-step-error" frontend/src`) and add a sibling beside it, matching that file's existing conventions:

```css
.lead-step-why {
  color: var(--text-faint);
  font-size: 12px;
}
```

Faint, not `--warn`: this is the normal state of a queued job, not a fault. The spec's accessibility constraint is satisfied because the reason is a sentence, not a colour.

- [ ] **Step 8: Verify the backend still passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_run_service.py tests/api -q`
Expected: PASS. If a test asserts the exact key set of a member-job dict, update it to include `job_class` and `cancel_requested`.

- [ ] **Step 9: Check types and lint**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

Run: `./backend/run-worktree-tests.sh --help >/dev/null; cd /workspace 2>/dev/null; ruff check backend/app`
Expected: no errors. (Run `ruff` however this checkout already runs it; CI enforces `I001` import ordering, which has bitten this repo before.)

- [ ] **Step 10: Look at it**

Run: `./ops/worktree-up.sh`

Open `http://localhost:5273`, launch any pipeline job, and confirm the run card's step line now reads e.g. `queued  Assemble — waiting for a free slot` instead of a bare `queued`. Tear the stack down when finished: `./ops/worktree-up.sh --down`.

- [ ] **Step 11: Commit**

```bash
git add frontend/src backend/app/services/run_service.py
git commit -m "fix(ui): say what a run's queued job is waiting on, not just that it waits

A job launched from the UI belongs to a run, so it renders through
ActivityLead, which never received the system load and so never called
waitingReason(). The reason existed and was shown only for loose jobs in
the Other waiting section, leaving the card users actually watch showing
a bare WAITING/QUEUED.

Refs #457"
```

---

## Task 2: Record the blocking gate in `claim.lua` (Layer 1)

**Files:**
- Modify: `backend/app/queue/scripts/claim.lua`
- Test: `backend/tests/queue/test_claim_blocked_reason.py` (create)

**Interfaces:**
- Produces: Redis key `bp:why:<ready_key>` holding `{"gate","need","free","class","admitted"}` JSON, TTL 15s. `gate` is one of `class` / `cpu` / `mem` / `io`.

Reason keying is derived from `KEYS[1]` (the ready key), **not** `node_id` — see Verified Fact 2.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/queue/test_claim_blocked_reason.py`:

```python
"""What claim.lua says about the job it could not start (#457).

The script already evaluates every gate; before this it discarded which one
failed. These tests pin the attribution, the fixed gate order, and the
guarantee that recording never changes what gets claimed.
"""

import json

import pytest

from tests.queue.conftest import ALL_CLASSES
from tests.queue.test_claim import LEASE_MS, NOW_MS, claim

REASON_KEY = "bp:why:bp:q:ready"


async def reason(redis):
    raw = await redis.get(REASON_KEY)
    return json.loads(raw) if raw else None


class TestGateAttribution:
    async def test_memory_gate_records_need_and_free(self, redis, scripts, job_factory):
        await job_factory("job1", mem_mb=32768)
        assert await claim(scripts, mem=8192) is None

        r = await reason(redis)
        assert r["gate"] == "mem"
        assert r["need"] == 32768
        assert r["free"] == 8192

    async def test_cpu_gate_records_need_and_free(self, redis, scripts, job_factory):
        await job_factory("job1", cpu=16)
        assert await claim(scripts, cpu=4) is None

        r = await reason(redis)
        assert r["gate"] == "cpu"
        assert r["need"] == 16
        assert r["free"] == 4

    async def test_io_gate_records_the_heavy_slot(self, redis, scripts, job_factory):
        await job_factory("job1", io="heavy")
        assert await claim(scripts, io=0) is None

        r = await reason(redis)
        assert r["gate"] == "io"

    async def test_class_gate_records_class_and_admitted(self, redis, scripts, job_factory):
        await job_factory("job1", job_class="bulk")
        assert await claim(scripts, classes="user_interactive") is None

        r = await reason(redis)
        assert r["gate"] == "class"
        assert r["class"] == "bulk"
        assert r["admitted"] == "user_interactive"

    async def test_free_is_headroom_after_reservations(self, redis, scripts, job_factory):
        """The recorded free must be what the gate compared against, not the
        raw budget: a half-reserved machine and an idle one give different
        answers to 'why is this waiting'."""
        await redis.set("bp:conc:mem_mb", 6144)
        await job_factory("job1", mem_mb=32768)
        assert await claim(scripts, mem=8192) is None

        r = await reason(redis)
        assert r["free"] == 2048


class TestGateOrder:
    async def test_class_wins_over_every_resource_gate(self, redis, scripts, job_factory):
        """All four gates fail at once. The fixed order makes the sentence
        deterministic; class first because governor closure explains every
        queued job at once rather than anything about this one."""
        await job_factory("job1", job_class="bulk", cpu=16, mem_mb=32768, io="heavy")
        assert await claim(scripts, classes="user_interactive", cpu=1, mem=128, io=0) is None

        assert (await reason(redis))["gate"] == "class"

    async def test_cpu_wins_over_mem_and_io(self, redis, scripts, job_factory):
        await job_factory("job1", cpu=16, mem_mb=32768, io="heavy")
        assert await claim(scripts, cpu=1, mem=128, io=0) is None

        assert (await reason(redis))["gate"] == "cpu"

    async def test_mem_wins_over_io(self, redis, scripts, job_factory):
        await job_factory("job1", mem_mb=32768, io="heavy")
        assert await claim(scripts, mem=128, io=0) is None

        assert (await reason(redis))["gate"] == "mem"


class TestRecordingIsInert:
    async def test_a_successful_claim_records_nothing(self, redis, scripts, job_factory):
        await job_factory("job1")
        assert (await claim(scripts))[0] == "job1"
        assert await reason(redis) is None

    async def test_recording_does_not_change_which_job_is_claimed(
        self, redis, scripts, job_factory
    ):
        """job1 sorts first and does not fit; job2 does. The scan must still
        reach job2 -- recording a reason must not short-circuit selection."""
        await job_factory("job1", mem_mb=32768, score=1)
        await job_factory("job2", mem_mb=128, score=2)

        result = await claim(scripts, mem=8192)
        assert result[0] == "job2"

    async def test_only_the_head_of_queue_is_described(self, redis, scripts, job_factory):
        """Two jobs, neither fits, blocked on different gates. The reason
        describes the one actually next in line."""
        await job_factory("job1", mem_mb=32768, score=1)
        await job_factory("job2", cpu=16, score=2)

        assert await claim(scripts, cpu=8, mem=8192) is None
        assert (await reason(redis))["gate"] == "mem"

    async def test_an_empty_queue_records_nothing(self, redis, scripts):
        assert await claim(scripts) is None
        assert await reason(redis) is None

    async def test_the_reason_expires(self, redis, scripts, job_factory):
        await job_factory("job1", mem_mb=32768)
        await claim(scripts, mem=8192)
        assert 0 < await redis.ttl(REASON_KEY) <= 15
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/queue/test_claim_blocked_reason.py -q`
Expected: FAIL — every assertion on `reason(redis)` returns `None` because nothing writes the key yet.

- [ ] **Step 3: Record the gate in `claim.lua`**

In `backend/app/queue/scripts/claim.lua`, replace the `fits` block inside the candidate loop:

```lua
    local mem_ok = mem <= mem_free or (override and sole)

    local fits = allowed[class]
                 and cpu <= cpu_free
                 and mem_ok
                 and (io ~= 'heavy' or io_free > 0)

    -- #457: the head-of-queue candidate is the job a user is watching, and
    -- until now the reason it did not start was computed here and discarded.
    -- Only i == 1 is described: it keeps this O(1) regardless of queue depth,
    -- and it is the job actually next in line. Gate order is fixed (class,
    -- cpu, mem, io) so the same queue state always yields the same sentence.
    if not fits and i == 1 then
      local why
      if not allowed[class] then
        why = {gate = 'class', class = class, admitted = ARGV[4]}
      elseif cpu > cpu_free then
        why = {gate = 'cpu', need = cpu, free = cpu_free}
      elseif not mem_ok then
        why = {gate = 'mem', need = mem, free = mem_free}
      else
        why = {gate = 'io', need = 1, free = io_free}
      end
      redis.call('SET', 'bp:why:' .. KEYS[1], cjson.encode(why), 'EX', 15)
    end
```

The `SET` is on the not-fits path only, so a successful claim never writes, and the write happens after `fits` is computed so it cannot influence selection.

- [ ] **Step 4: Clear a stale reason on a successful claim**

A reason left from a previous tick would outlive the condition it describes. Inside `if fits then`, immediately before `return`, add:

```lua
      -- This queue is dispatching again; a reason from an earlier tick now
      -- describes a condition that has passed.
      redis.call('DEL', 'bp:why:' .. KEYS[1])
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/queue/test_claim_blocked_reason.py -q`
Expected: PASS (14 tests).

- [ ] **Step 6: Run the whole queue suite for regressions**

Run: `./backend/run-worktree-tests.sh tests/queue -q`
Expected: PASS, with `test_claim.py`'s 24 still passing. Recording must be inert (spec R6); a failure here means it is not.

- [ ] **Step 7: Commit**

```bash
git add backend/app/queue/scripts/claim.lua backend/tests/queue/test_claim_blocked_reason.py
git commit -m "feat(queue): record which gate blocked the head-of-queue job

claim.lua evaluated four independently-failing gates and returned nil,
discarding which one failed -- the one moment the answer is knowable
atomically, since the live bp:conc:* counters are read inside this same
execution. It now writes the gate and its two numbers to a short-lived
key for the activity view to read.

Only the head-of-queue candidate is described, keeping the cost O(1)
whatever the queue depth, and the write happens after the fits test so
selection is unchanged.

Refs #457"
```

---

## Task 3: Read the reason from Python (Layer 1)

**Files:**
- Create: `backend/app/queue/blocked_reason.py`
- Test: `backend/tests/queue/test_blocked_reason.py` (create)

**Interfaces:**
- Produces: `BlockedReason` dataclass (`gate`, `need`, `free`, `job_class`, `admitted`); `async def read(redis, ready_key: str = keys.READY) -> BlockedReason | None`; `def reason_key(ready_key: str) -> str`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/queue/test_blocked_reason.py`:

```python
"""Typed access to what claim.lua recorded (#457)."""

import json

from app.queue import blocked_reason


class TestReadReason:
    async def test_returns_none_when_nothing_recorded(self, redis):
        assert await blocked_reason.read(redis) is None

    async def test_parses_a_resource_gate(self, redis):
        await redis.set(
            blocked_reason.reason_key("bp:q:ready"),
            json.dumps({"gate": "mem", "need": 32768, "free": 8192}),
        )
        r = await blocked_reason.read(redis)

        assert r.gate == "mem"
        assert r.need == 32768
        assert r.free == 8192
        assert r.job_class is None

    async def test_parses_the_class_gate(self, redis):
        await redis.set(
            blocked_reason.reason_key("bp:q:ready"),
            json.dumps(
                {"gate": "class", "class": "bulk", "admitted": "user_interactive"}
            ),
        )
        r = await blocked_reason.read(redis)

        assert r.gate == "class"
        assert r.job_class == "bulk"
        assert r.admitted == ["user_interactive"]
        assert r.need is None

    async def test_malformed_json_reads_as_no_reason(self, redis):
        """A reason is advisory. Never let it break the activity view."""
        await redis.set(blocked_reason.reason_key("bp:q:ready"), "{not json")
        assert await blocked_reason.read(redis) is None

    async def test_an_unknown_gate_reads_as_no_reason(self, redis):
        await redis.set(
            blocked_reason.reason_key("bp:q:ready"), json.dumps({"gate": "quantum"})
        )
        assert await blocked_reason.read(redis) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/queue/test_blocked_reason.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.queue.blocked_reason'`.

- [ ] **Step 3: Write the module**

Create `backend/app/queue/blocked_reason.py`:

```python
"""Why the queue did not start the job at the head of the line.

`claim.lua` evaluates four independently-failing gates and, before #457,
returned nil without saying which one failed. It now records that decision;
this module is the read side.

The reason is advisory and short-lived (15s, matching the governor snapshot).
Every failure to read one is treated as "no reason available" rather than an
error: the activity view falls back to its own inference, which is what it
showed before this existed.
"""

import json
from dataclasses import dataclass

from app.logging import get_logger
from app.queue import keys

log = get_logger(__name__)

# Fixed order, mirrored in claim.lua and in the frontend's wording.
GATES = ("class", "cpu", "mem", "io")


@dataclass(frozen=True)
class BlockedReason:
    """One gate, and the numbers it compared."""

    gate: str
    need: int | None = None
    free: int | None = None
    job_class: str | None = None
    admitted: list[str] | None = None


def reason_key(ready_key: str = keys.READY) -> str:
    """Keyed by the ready queue, not the node id.

    `queue.claim` accepts a `node_id` but does not forward it to the script,
    so keying on it would always produce the global key while reading as
    though it were per-node.
    """
    return f"bp:why:{ready_key}"


async def read(redis, ready_key: str = keys.READY) -> BlockedReason | None:
    """The current reason, or None when there isn't a usable one."""
    try:
        raw = await redis.get(reason_key(ready_key))
        if not raw:
            return None
        data = json.loads(raw)
        gate = data.get("gate")
        if gate not in GATES:
            return None
        admitted = data.get("admitted")
        return BlockedReason(
            gate=gate,
            need=data.get("need"),
            free=data.get("free"),
            job_class=data.get("class"),
            admitted=admitted.split(",") if admitted else None,
        )
    except Exception as e:  # noqa: BLE001 - advisory data must never break a read path
        log.warning("blocked_reason_unreadable", error=str(e))
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/queue/test_blocked_reason.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/blocked_reason.py backend/tests/queue/test_blocked_reason.py
git commit -m "feat(queue): read the recorded blocking gate as a typed value

Refs #457"
```

---

## Task 4: Serve the reason and the declared demand (Layer 2)

**Files:**
- Modify: `backend/app/queue/governor.py:474-508` (`current_load`)
- Modify: `backend/app/services/run_service.py:223-236`
- Test: `backend/tests/queue/test_blocked_reason_load.py` (create)

**Interfaces:**
- Consumes: `blocked_reason.read` from Task 3
- Produces: `/system/load` gains `blocked_reason`; each run member job gains `resources: {cpu, mem_mb, io}`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_blocked_reason_load.py`:

```python
"""The reason reaches /system/load (#457)."""

import json

from app.queue import blocked_reason, governor


class TestLoadCarriesTheReason:
    async def test_absent_when_nothing_is_blocked(self, redis, monkeypatch):
        monkeypatch.setattr(
            "app.db.redis_client.get_redis", lambda: redis, raising=False
        )
        monkeypatch.setattr(governor, "_node_breakdown", _no_nodes)

        load = await governor.current_load()
        assert load.get("blocked_reason") is None

    async def test_present_when_a_gate_blocked_the_head_job(self, redis, monkeypatch):
        monkeypatch.setattr(
            "app.db.redis_client.get_redis", lambda: redis, raising=False
        )
        monkeypatch.setattr(governor, "_node_breakdown", _no_nodes)
        await redis.set(
            blocked_reason.reason_key(),
            json.dumps({"gate": "mem", "need": 32768, "free": 8192}),
        )

        load = await governor.current_load()
        assert load["blocked_reason"] == {
            "gate": "mem",
            "need": 32768,
            "free": 8192,
            "class": None,
            "admitted": None,
        }


async def _no_nodes():
    return [], None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/queue/test_blocked_reason_load.py -q`
Expected: FAIL — `blocked_reason` is not a key in the returned dict.

- [ ] **Step 3: Attach the reason in `current_load`**

In `backend/app/queue/governor.py`, inside `current_load`, after `nodes, nodes_error = await _node_breakdown()`:

```python
    from app.queue import blocked_reason as blocked_reason_mod

    reason = await blocked_reason_mod.read(get_redis())
    reason_out = (
        {
            "gate": reason.gate,
            "need": reason.need,
            "free": reason.free,
            "class": reason.job_class,
            "admitted": reason.admitted,
        }
        if reason
        else None
    )
```

Then set `snap["blocked_reason"] = reason_out` on the snapshot path before returning it, and add `"blocked_reason": reason_out` to the fallback dict literal so the field exists on both paths.

- [ ] **Step 4: Add declared resources to run member jobs**

The unsatisfiable check needs the job's own demand. In `run_service.py`, add one line to the dict from Task 1 Step 5:

```python
                # Declared demand, for the unsatisfiable check on the card: a
                # job needing more memory than the whole budget can never be
                # claimed, which is a different thing from waiting a turn.
                "resources": job.resources.model_dump(mode="json") if job else None,
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/queue/test_blocked_reason_load.py tests/services/test_run_service.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/governor.py backend/app/services/run_service.py backend/tests/queue/test_blocked_reason_load.py
git commit -m "feat(api): report the blocking gate and each job's declared demand

Refs #457"
```

---

## Task 5: Turn the reason into a sentence (Layer 2)

**Files:**
- Modify: `frontend/src/lib/runFormat.ts`
- Modify: `frontend/src/api/types.ts`

**Interfaces:**
- Consumes: `/system/load`'s `blocked_reason` from Task 4
- Produces: `formatBlockedReason(reason, bytes)`, `isUnsatisfiable(resources, load)`; `waitingReason` gains a third parameter

There is no frontend test runner in this repo, so these pure functions are verified by `tsc` plus the manual check in Task 6.

- [ ] **Step 1: Add the types**

In `frontend/src/api/types.ts`, add above `SystemLoad`:

```ts
/** Which gate stopped the head-of-queue job, and the two numbers it
 *  compared. `need`/`free` are cores for cpu and MB for mem; the class gate
 *  carries `class`/`admitted` instead (#457). */
export interface BlockedReason {
  gate: "class" | "cpu" | "mem" | "io";
  need: number | null;
  free: number | null;
  class: string | null;
  admitted: string[] | null;
}
```

Add to the `SystemLoad` interface:

```ts
  blocked_reason?: BlockedReason | null;
```

And to `RunMemberJob`:

```ts
  /** Declared demand. Null for a pruned job. */
  resources: { cpu: number; mem_mb: number; io: string } | null;
```

- [ ] **Step 2: Write the formatter**

In `frontend/src/lib/runFormat.ts`, add beside `waitingReason`:

```ts
/** MB as the largest unit that keeps the number readable. */
function mb(value: number): string {
  return value >= 1024 ? `${(value / 1024).toFixed(1)} GB` : `${value} MB`;
}

/**
 * The recorded gate as a sentence.
 *
 * Fact rather than inference: these numbers are the ones claim.lua actually
 * compared, so they answer "is it waiting on resources" with the amounts
 * instead of a guess (#457).
 */
export function formatBlockedReason(reason: BlockedReason): string {
  switch (reason.gate) {
    case "cpu":
      return `waiting on CPU — needs ${reason.need}, ${reason.free} free`;
    case "mem":
      return reason.need != null && reason.free != null
        ? `waiting on memory — needs ${mb(reason.need)}, ${mb(reason.free)} free`
        : "waiting on memory";
    case "io":
      return "waiting on disk — another heavy job is reading";
    case "class":
      return "waiting: system loaded";
  }
}

/**
 * True when this job can never be claimed on this machine.
 *
 * Compared against the *total* budget, not free headroom: headroom recovers
 * as other jobs finish, a budget does not. The two need different words
 * because only one of them ends on its own.
 */
export function isUnsatisfiable(
  resources: { mem_mb: number } | null,
  load?: SystemLoad,
): boolean {
  const budget = load?.memory.budget_bytes;
  if (!resources || budget == null) return false;
  return resources.mem_mb * 1024 * 1024 > budget;
}

/** The unsatisfiable sentence, naming both numbers. */
export function unsatisfiableReason(
  resources: { mem_mb: number },
  budget: number,
): string {
  return `cannot start here — needs ${mb(resources.mem_mb)}, this machine's budget is ${mb(
    Math.round(budget / (1024 * 1024)),
  )}`;
}
```

Import `BlockedReason` in the type import at the top of the file.

- [ ] **Step 3: Prefer the recorded reason**

Replace the body of `waitingReason`:

```ts
export function waitingReason(
  job: WaitingJob,
  load?: SystemLoad,
  reason?: BlockedReason | null,
): string {
  if (job.cancel_requested) return "cancelling";
  if (job.state === "delayed") return "retrying after a failure";
  if (job.state === "blocked") return "waiting on an earlier step";
  // A recorded reason is what the queue actually decided; everything below is
  // inference from global state. Kept as the fallback so a cold or expired
  // key degrades to the previous behaviour rather than to a blank (#457).
  if (reason) return formatBlockedReason(reason);
  if (!load) return "waiting";
  if (!load.admitted_classes.includes(job.job_class)) {
    return load.state === "CLOSED"
      ? "waiting: system loaded"
      : "waiting: system busy";
  }
  return "waiting for a free slot";
}
```

The new parameter is optional and last, so the existing `JobRow` call site is unaffected.

- [ ] **Step 4: Check types**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/runFormat.ts frontend/src/api/types.ts
git commit -m "feat(frontend): word the blocking gate with its actual numbers

Refs #457"
```

---

## Task 6: Show the reason and the way out (Layer 2)

**Files:**
- Modify: `frontend/src/components/activity/ActivityLead.tsx`

**Interfaces:**
- Consumes: `formatBlockedReason`, `isUnsatisfiable`, `unsatisfiableReason`, `waitingReason` from Task 5

Only the *head-of-queue* reason is recorded, so it must only be shown on a job that could plausibly be that one. Showing it on every waiting step would attribute one job's gate to all of them.

- [ ] **Step 1: Show the recorded reason on the first waiting step**

In `ActivityLead.tsx`, inside `LeadStory`, compute which step owns the reason, just above the `return`:

```tsx
  // The recorded reason describes the head of the queue, so it is shown on
  // this run's first waiting step only -- pinning it to every waiting step
  // would attribute one job's gate to all of them.
  const firstWaitingId = steps.find(
    (j) => j.state !== null && WAITING.has(j.state),
  )?.job_id;
```

Pass it down:

```tsx
        {steps.map((job) => (
          <LeadStep
            key={job.job_id}
            job={job}
            load={load}
            reason={job.job_id === firstWaitingId ? load?.blocked_reason : null}
          />
        ))}
```

- [ ] **Step 2: Render it, with the unsatisfiable case taking priority**

Update `LeadStep` to accept `reason` and replace the `why` computation from Task 1 Step 2:

```tsx
function LeadStep({
  job,
  load,
  reason,
}: {
  job: RunMemberJob;
  load?: SystemLoad;
  reason?: BlockedReason | null;
}) {
  const state = job.state ?? "expired";
  const pct =
    job.state === "running" && job.progress?.pct
      ? ` ${Math.round(job.progress.pct * 100)}%`
      : "";

  const isWaiting = job.state !== null && WAITING.has(job.state);
  // A job demanding more than the whole budget is not waiting its turn --
  // nothing that finishes will free enough. It gets its own words and its
  // own colour, and it is checked first because the queue would otherwise
  // report it as an ordinary memory wait forever (#457).
  const stuck = isWaiting && isUnsatisfiable(job.resources, load);
  const why = !isWaiting
    ? null
    : stuck && job.resources && load?.memory.budget_bytes != null
      ? unsatisfiableReason(job.resources, load.memory.budget_bytes)
      : waitingReason(
          {
            state: job.state as string,
            job_class: job.job_class ?? "",
            cancel_requested: job.cancel_requested,
          },
          load,
          reason,
        );

  return (
    <div className="lead-step">
      <span className={`lead-step-state ${state}`}>{state}</span>
      <span className="lead-step-label">
        {ROLE_LABELS[job.role] ?? job.role}
        {pct}
        {job.shared && (
          <span
            className="lead-step-shared"
            title="Reused from an earlier run — this run did not do this work"
          >
            reused
          </span>
        )}
      </span>
      {why && (
        <span className={stuck ? "lead-step-stuck" : "lead-step-why"}>{why}</span>
      )}
      {job.error && <span className="lead-step-error">{job.error.message}</span>}
      <span className="lead-step-time">{formatClock(job.created_at)}</span>
    </div>
  );
}
```

Add `BlockedReason` to the type import and `isUnsatisfiable`, `unsatisfiableReason` to the `runFormat` import.

- [ ] **Step 3: Point the user at the way out**

An unsatisfiable job needs an action, not just a diagnosis. Below the progress row in `LeadStory`, after the `lead-progress` div:

```tsx
      {/* The run is already launched, so the Band.BLOCK refusal card that
          offers "Launch anyway" is behind us. Cancelling and relaunching
          with the override is the actual way out, so say that rather than
          leaving the user watching a job that cannot start. */}
      {steps.some(
        (j) =>
          j.state !== null &&
          WAITING.has(j.state) &&
          isUnsatisfiable(j.resources, load),
      ) && (
        <div className="lead-stuck-note">
          This needs more memory than this machine allows. Cancel the run and
          relaunch it — the launch dialog offers “Launch anyway”, or lower the
          thread count to reduce what it needs.
        </div>
      )}
```

- [ ] **Step 4: Style both**

Beside the `.lead-step-why` rule from Task 1:

```css
.lead-step-stuck {
  color: var(--warn);
  font-size: 12px;
}

.lead-stuck-note {
  color: var(--warn);
  font-size: 12px;
  margin-top: 6px;
}
```

Both remain full sentences with colour removed, per the spec's accessibility constraint.

- [ ] **Step 5: Check types**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src
git commit -m "feat(ui): name the blocking resource and flag a job that cannot start

Refs #457"
```

---

## Task 7: Verify against the real queue

Per CLAUDE.md: hand-built fixtures that already look the way the code expects are exactly what hid the last bug of this shape. This task runs the real thing.

**Files:** none — verification only.

- [ ] **Step 1: Bring the worktree stack up**

Run: `./ops/worktree-up.sh`
Expected: UI on `http://localhost:5273`.

- [ ] **Step 2: Confirm the ordinary waiting case**

Launch two assemblies so the second queues behind the first. Confirm the second's step line names a real gate with numbers — `waiting on memory — needs 12.0 GB, 3.2 GB free` — not `waiting for a free slot`.

- [ ] **Step 3: Confirm the unsatisfiable case**

Lower the memory budget below any assembly's demand (Settings → resource limits, or `BIOINFO_MEM_BUDGET_MB`), then launch one. Confirm the step reads `cannot start here — needs …, this machine's budget is …` and the note about relaunching appears.

- [ ] **Step 4: Confirm the fallback**

Run: `docker exec <worktree-redis> redis-cli DEL "bp:why:bp:q:ready"`

Confirm the line falls back to the old inference wording within one poll rather than going blank (spec R8).

- [ ] **Step 5: Confirm the reason clears**

Let the blocking job finish. Confirm the reason disappears from the now-running job rather than persisting.

- [ ] **Step 6: Full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the count, not just the exit code.

- [ ] **Step 7: Tear the stack down**

Run: `./ops/worktree-up.sh --down`

A stack left up corrupts other test runs — `conftest.py` drops every collection in `biopipe_test` at session start.

---

## Task 8: Close out the paperwork

**Files:**
- Modify: `backend/app/queue/governor.py:80-87`

- [ ] **Step 1: Correct the governor's comment**

Its premise ("a waiting pipeline run is *visible* as waiting in the activity view, so it fails loudly rather than silently") was false when written and is true only now. Replace the middle of that comment:

```python
# The escape is deliberately limited to maintenance (see worker._maintenance_
# starving). Compute does not qualify: a waiting pipeline run says what it is
# waiting on in the activity view -- the gate and the numbers, recorded by
# claim.lua (#457) -- so it fails loudly rather than silently, and forcing a
# multi-hour job onto an already-strained machine is the outcome the governor
# exists to prevent.
```

- [ ] **Step 2: File the two follow-ups**

```bash
gh issue create --title "Queue: an unsatisfiable job waits forever with no resolution" --body "$(cat <<'EOF'
A job whose declared `mem_mb` exceeds the machine's memory budget can never
satisfy `mem <= mem_free` in `claim.lua`. Compute jobs have no starvation
escape (`governor.py:80-87`) and there is no timeout, so it waits forever.

#457 made this *visible* — the activity view now says the job cannot start
and points at the override. It did not decide what the queue should do about
it. Options, none obviously right:

- Fail the job with a clear error instead of queueing it.
- Downscale its thread count until it fits.
- Grant compute a starvation escape like maintenance has.

Split out of #457, which was scoped to explaining the wait.
EOF
)"
```

```bash
gh issue create --title "queue.claim accepts node_id and ready_key but forwards neither" --body "$(cat <<'EOF'
`queue.claim()` (`backend/app/queue/queue.py:489-528`) takes `node_id` and
`ready_key` parameters, but its `get_script("claim")` call passes only 9 ARGV
and always keys on `keys.READY`. So `claim.lua`'s `ARGV[10]` (node_id) is
always empty on the production path, and the per-node concurrency counters
(`bp:conc:*:<node_id>`) are never used there — only the global ones.

`worker._try_claim_queue` does pass both, so the intent is clearly that
per-node queues work. Either the parameters should be forwarded, or they
should be removed as misleading.

Found while implementing #457, which had to key its reason on the ready key
rather than the node id because of this.
EOF
)"
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/queue/governor.py
git commit -m "docs(queue): correct the comment claiming waiting runs explain themselves

Refs #457"
```

- [ ] **Step 4: Open the PR**

```bash
git fetch origin main && git rebase origin/main
```

```bash
git diff origin/main...HEAD --stat
```

Confirm the file list matches the tasks above, then push and open the PR with `Closes #457`, labelled `type:` and `area:` so `.github/release.yml` categorises it.

---

## Self-Review

**Spec coverage.** R1 → T2S3. R2 → T2S3 + `TestGateOrder`. R3 → T2S3 + `TestGateAttribution`. R4 → T2S3 (`i == 1`) + `test_only_the_head_of_queue_is_described`. R5 → T2S3 (`EX 15`) + `test_the_reason_expires` + T2S4 (`DEL`). R6 → T2S3 ordering + `TestRecordingIsInert`. R7 → T6S2. R8 → T5S3 fallback + T7S4. R9 → T1S3 (one `waitingReason` for both paths). R10 → preserved `blocked` branch in T5S3. R11 → T5S2 `isUnsatisfiable` + T6S2. R12 → `unsatisfiableReason`. R13 → T6S3. R14 → untouched Cancel run button. Non-functional: performance (one conditional `SET`), capacity (one key, TTL), consistency (recorded in-script), accessibility (T1S7, T6S4).

**Placeholders.** None: every code step carries the actual code, every test step the actual assertions.

**Type consistency.** `BlockedReason` is `{gate, need, free, class, admitted}` in Lua (T2S3), Python (T3S3), the API (T4S3), and TypeScript (T5S1); the Python dataclass renames `class`→`job_class` at its boundary because `class` is reserved, and T4S3 maps it back. `WaitingJob` (T1S3) is used consistently in T5S3 and T6S2. `isUnsatisfiable` takes `{mem_mb}` and is called with `job.resources` (`RunMemberJob.resources`, added T5S1).

One ordering note for the executor: Task 1 Step 2 writes a `LeadStep` that Task 6 Step 2 rewrites. That is deliberate — Task 1 must stand alone as the shippable fix for the reported screenshot.
