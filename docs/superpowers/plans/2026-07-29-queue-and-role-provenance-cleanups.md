# Queue Lease, Cancel-Set, and Role-Provenance Cleanups — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three deferred correctness items from `docs/TODO.md` — the inert `JobContext.extend_lease`, the `bp:cancel` Redis set leak, and re-ingest silently re-asserting a reference role the user cleared.

**Architecture:** Three independent parts, each shippable on its own. Part A wires `extend_lease` to a real per-job lease override in the heartbeat loop (the TODO suggested deleting it; investigation found two live callers that depend on it, so it must be wired instead). Part B closes the two `bp:cancel` removal gaps that survive the existing `release.lua` cleanup. Part C adds a `user_touched` provenance list to `DataObject` so an explicitly-cleared role is distinguishable from one never set.

**Tech Stack:** Python 3.12, FastAPI, Beanie/MongoDB, Redis (+ Lua scripts), pytest with fakeredis, React/TypeScript frontend.

---

## Before you start — read this

### Three corrections to `docs/TODO.md`

The TODO entries for these items were written from a partial reading. Investigation for this plan found each one's premise is wrong in a way that changes the work. **Trust this plan over the TODO text**, and Task 13 updates the TODO to match.

**1. `extend_lease` is not unused — it has two live callers.**

The TODO says "someone will eventually rely on it instead of the heartbeat" and concludes "Delete is probably right." That is out of date. Two handlers already call it, both with comments explaining what they believe they are buying:

- `backend/app/queue/sra_handlers.py:258` — `ctx.extend_lease(3600)`, commented *"A long transfer with no output for minutes at a time would otherwise let the lease expire and the reaper double-run the job."*
- `backend/app/queue/pipeline_handlers.py:570` — `ctx.extend_lease(1800)`, commented *"NanoPlot reads the whole file before plotting and says little meanwhile, so a large ONT run can sit quiet long enough to worry the reaper."*

Both calls are inert today, so both comments describe protection that does not exist. Deleting the method means deleting those two call sites and their stated intent. **Wire it instead** (Part A).

The TODO's counter-argument — that `_heartbeat_loop` renews every in-flight job every 10s regardless — is true and is why nothing is broken *today*. But it renews to a fixed `settings.lease_ttl_seconds` from now. The heartbeat protects against a slow job; it does not protect against a **stalled event loop or a paused VM**, which is exactly the laptop-lid case `reap_expired.lua` is commented for. A job that asked for a 3600s lease and got 30s is one lid-close away from being reaped and double-run. Wiring `extend_lease` makes the lease as long as the handler said it needed.

**2. The `bp:cancel` leak is narrower than described, and in a different place.**

The TODO says the running path "never removes" the job id. That is wrong: `release.lua` already ends its drop branch with `SREM bp:cancel`, and `queue.complete` calls `release(job_id, requeue=False)` on every terminal outcome. A running job that observes cancellation and completes normally **is** cleaned up today.

The leak survives only where the `SREM` is not reached. `release.lua` gates everything behind `ZREM(running) == 1`, and there is one branch that skips the `SREM` outright:

- **The reaper.** `reap_expired.lua` removes the job from `running` itself and never touches `bp:cancel`. A cancelled job whose lease expires first leaks permanently — and this is also the path that produces `JobState.DEAD`.
- **The requeue branch.** `release(requeue=True)` (graceful shutdown drain, `worker._drain`) deliberately keeps the job key and does not `SREM`. Correct for a requeue — the job will run again and should still see the cancel flag — but if that job is then cancelled-and-dropped by a path that finds it already out of `running`, nothing clears it.
- **`_fail_blocked_job`.** Writes a terminal state directly via `update_one` and never calls `release` at all. A BLOCKED job that was cancel-requested and then failed by a dependency leaks its id.

Fix the reaper and `_fail_blocked_job`; leave the requeue branch alone (Part B).

**3. There is now an index-reconciliation mechanism.**

The TODO for item 6 says "this project has no migrations mechanism." `backend/app/db/index_reconcile.py` now exists and `client.py:57` calls it at startup. This matters because Part C is a **field** addition, not an index change: Pydantic supplies the default for documents that predate it, so no migration step is needed at all. Do not build one.

### House conventions this plan follows

- Tests live under `backend/tests/<area>/`, mirroring `backend/app/<area>/`.
- Queue tests run **real Lua against fakeredis** (`backend/tests/queue/conftest.py`) — never mock a script away, the atomicity under test lives inside it.
- Pure decision functions get extracted and tested without a database when the decision is the part worth getting right (see `queue.classify_dependencies`, `governor.compute_free_resources`, `results.should_assign_reference_role`). Parts A and C both do this.
- Run tests **inside the container**, never the host venv:
  ```bash
  docker compose exec api python -m pytest tests/ -q
  ```
- `worker` does not hot-reload. After changing anything under `app/queue/`, restart it before re-testing a real job:
  ```bash
  docker compose restart worker
  ```

---

## File structure

**Part A — wire `extend_lease`**
- Modify: `backend/app/queue/registry.py` — `JobContext.extend_lease` docstring; add `lease_override_seconds` state
- Modify: `backend/app/queue/queue.py` — `heartbeat()` takes per-job TTLs
- Modify: `backend/app/queue/worker.py` — set `_extend_cb`; heartbeat reads overrides
- Modify: `backend/app/queue/executor.py` — set `_extend_cb` on its own fallback context
- Create: `backend/tests/queue/test_lease_extension.py`

**Part B — close the `bp:cancel` gaps**
- Modify: `backend/app/queue/scripts/reap_expired.lua` — `SREM bp:cancel` on the dead path
- Modify: `backend/app/queue/queue.py` — `_fail_blocked_job` clears the cancel flag
- Create: `backend/tests/queue/test_cancel_cleanup.py`

**Part C — role provenance**
- Modify: `backend/app/models/object.py` — `user_touched: list[str]`
- Modify: `backend/app/queue/results.py` — `should_assign_reference_role` takes `user_touched`
- Modify: `backend/app/services/object_service.py` — `apply_role_update` records the touch
- Modify: `backend/tests/storage/test_object_role.py` — new cases
- Modify: `backend/tests/queue/test_pipeline_handlers.py` *(only if it calls the changed signature — check first)*

---

# PART A — Wire `JobContext.extend_lease`

**What this fixes:** `_extend_cb` is never assigned, so both live callers are no-ops and their stated protection does not exist.

**Design:** The handler declares how long it needs; the heartbeat honours that per-job instead of the global `settings.lease_ttl_seconds`. The heartbeat keeps running unchanged — this changes only the TTL it renews *to*. That keeps one mechanism, not two (the TODO's stated worry about the mechanisms getting out of step).

Store the override on the context. The worker owns the `JobContext` and already holds it in `self._running`, so the heartbeat loop can read each job's override with no new plumbing.

**Thread safety:** `extend_lease` is called from handler threads (`HandlerMode.THREAD`/`SUBPROCESS` run via `asyncio.to_thread`), while `_heartbeat_loop` reads on the event loop. A plain `int` assignment is atomic under the GIL, and a torn read is impossible — worst case the heartbeat uses the old value for one 10s tick. No lock needed; the alternative (a lock held across the heartbeat) is worse.

---

## Task A1: `JobContext` records the requested lease

**Files:**
- Modify: `backend/app/queue/registry.py:78-85`
- Test: `backend/tests/queue/test_lease_extension.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_lease_extension.py`:

```python
"""Lease extension: a handler declaring it needs longer than the default.

The heartbeat renews every in-flight job on a fixed interval, which covers a
merely *slow* job. It does not cover a paused VM or a stalled loop -- the
laptop-lid case reap_expired.lua exists for. A handler that asked for an hour
and silently got the 30s default is one lid-close away from being reaped and
double-run, which is why these callers are not decorative.
"""

import pytest

from app.queue.registry import JobContext


class TestExtendLease:
    def test_defaults_to_no_override(self):
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        assert ctx.lease_override_seconds is None

    def test_records_the_requested_seconds(self):
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx.extend_lease(3600)
        assert ctx.lease_override_seconds == 3600

    def test_keeps_the_longest_request(self):
        """A handler with several long phases must not shorten its own lease by
        asking for less on a later phase than it did on an earlier one."""
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx.extend_lease(3600)
        ctx.extend_lease(60)
        assert ctx.lease_override_seconds == 3600

    def test_ignores_a_nonpositive_request(self):
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx.extend_lease(0)
        ctx.extend_lease(-5)
        assert ctx.lease_override_seconds is None

    def test_still_invokes_the_callback_when_one_is_set(self):
        """The callback stays supported so the worker can react immediately
        rather than waiting for the next heartbeat tick."""
        seen = []
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx._extend_cb = seen.append
        ctx.extend_lease(120)
        assert seen == [120]
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/queue/test_lease_extension.py -v
```

Expected: FAIL — `AttributeError: 'JobContext' object has no attribute 'lease_override_seconds'`.

- [ ] **Step 3: Add the field and rewrite the method**

In `backend/app/queue/registry.py`, add the field to `JobContext` immediately after `_extend_cb` (line 43):

```python
    _progress_cb: Callable[[dict], None] | None = None
    _extend_cb: Callable[[int], None] | None = None
    # The longest lease any handler phase has asked for. Read by the worker's
    # heartbeat loop, written from handler threads -- a plain int assignment is
    # atomic under the GIL, and a stale read costs one heartbeat tick, so this
    # deliberately has no lock.
    lease_override_seconds: int | None = None
```

Replace `extend_lease` (lines 78-85) with:

```python
    def extend_lease(self, seconds: int) -> None:
        """Request a longer lease for a known-long phase.

        The heartbeat renews every in-flight job regardless of duration, so a
        merely slow job is already safe. What this covers is the lease *length*:
        a paused VM or a stalled event loop stops the heartbeat entirely, and
        then only the recorded TTL stands between a live job and the reaper
        requeueing it underneath itself. A handler that knows it will go quiet
        for an hour says so here.

        The longest request wins. A handler with several long phases would
        otherwise shorten its own lease by asking for less later on.
        """
        if seconds <= 0:
            return
        if self.lease_override_seconds is None or seconds > self.lease_override_seconds:
            self.lease_override_seconds = seconds
        if self._extend_cb is not None:
            self._extend_cb(seconds)
```

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/queue/test_lease_extension.py -v
```

Expected: PASS, 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/registry.py backend/tests/queue/test_lease_extension.py
git commit -m "feat: JobContext records the lease a handler asks for"
```

---

## Task A2: `queue.heartbeat` honours per-job TTLs

**Files:**
- Modify: `backend/app/queue/queue.py:351-376`
- Test: `backend/tests/queue/test_lease_extension.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/queue/test_lease_extension.py`:

```python
class TestHeartbeatTtls:
    """queue.heartbeat's Redis half, exercised directly.

    The Mongo half needs a database and is covered by the container suite; what
    matters here is that the RUNNING zset score -- the value reap_expired.lua
    compares against -- reflects the per-job TTL rather than the global default.
    """

    async def test_uses_the_default_ttl_when_no_override(self, redis, monkeypatch):
        from app.config import settings
        from app.queue import queue

        monkeypatch.setattr(queue, "get_redis", lambda: redis)
        monkeypatch.setattr(queue, "_heartbeat_mongo", _noop_mongo)

        await queue.heartbeat(["job1"], {"job1": 0})

        score = await redis.zscore("bp:q:running", "job1")
        now_ms = _now_ms()
        expected = now_ms + settings.lease_ttl_seconds * 1000
        assert abs(score - expected) < 5000

    async def test_a_longer_override_pushes_the_expiry_out(self, redis, monkeypatch):
        from app.queue import queue

        monkeypatch.setattr(queue, "get_redis", lambda: redis)
        monkeypatch.setattr(queue, "_heartbeat_mongo", _noop_mongo)

        await queue.heartbeat(["job1"], {"job1": 0}, ttls={"job1": 3600})

        score = await redis.zscore("bp:q:running", "job1")
        expected = _now_ms() + 3600 * 1000
        assert abs(score - expected) < 5000

    async def test_each_job_gets_its_own_ttl(self, redis, monkeypatch):
        """A quick job alongside a long one must not inherit the long lease."""
        from app.config import settings
        from app.queue import queue

        monkeypatch.setattr(queue, "get_redis", lambda: redis)
        monkeypatch.setattr(queue, "_heartbeat_mongo", _noop_mongo)

        await queue.heartbeat(
            ["slow", "quick"], {"slow": 0, "quick": 0}, ttls={"slow": 3600}
        )

        slow = await redis.zscore("bp:q:running", "slow")
        quick = await redis.zscore("bp:q:running", "quick")
        assert slow - quick > 3000 * 1000
        assert abs(quick - (_now_ms() + settings.lease_ttl_seconds * 1000)) < 5000
```

Add these helpers at the top of the file, directly under the imports:

```python
from datetime import UTC, datetime


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


async def _noop_mongo(job_ids, epochs, ttls, now):
    """Stand-in for the Mongo half of heartbeat, which needs a database."""
    return None
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/queue/test_lease_extension.py::TestHeartbeatTtls -v
```

Expected: FAIL — `AttributeError: module 'app.queue.queue' has no attribute '_heartbeat_mongo'`.

- [ ] **Step 3: Split the Mongo write out and add the `ttls` parameter**

In `backend/app/queue/queue.py`, replace the whole `heartbeat` function (lines 351-376) with:

```python
async def _heartbeat_mongo(
    job_ids: list[str], epochs: dict[str, int], ttls: dict[str, int], now: datetime
) -> None:
    """Write the renewed lease to Mongo, one conditional update per job.

    Split out from `heartbeat` so the Redis half -- the part the reaper actually
    compares against -- is testable without a database.
    """
    from app.db.client import get_db

    for jid in job_ids:
        ttl = ttls.get(jid, settings.lease_ttl_seconds)
        await get_db().jobs.update_one(
            {"_id": PydanticObjectId(jid), "lease.epoch": epochs.get(jid, 0)},
            {
                "$set": {
                    "lease.heartbeat_at": now,
                    "lease.expires_at": now + timedelta(seconds=ttl),
                }
            },
        )


async def heartbeat(
    job_ids: list[str],
    epochs: dict[str, int],
    ttls: dict[str, int] | None = None,
) -> None:
    """Extend leases for in-flight jobs.

    `ttls` carries per-job lease lengths for handlers that called
    `ctx.extend_lease` -- anything absent renews to the global default. The
    distinction only bites when heartbeating *stops*: a paused VM leaves the
    recorded expiry as the sole thing standing between a live job and the
    reaper, and a job that said it needed an hour must not be holding a 30s
    lease at that moment.

    The Mongo update is conditional on the epoch, so a worker that lost its
    lease while paused cannot resurrect it.
    """
    if not job_ids:
        return
    ttls = ttls or {}
    now = datetime.now(UTC)

    r = get_redis()
    await r.zadd(
        keys.RUNNING,
        {
            jid: int(
                (now.timestamp() + ttls.get(jid, settings.lease_ttl_seconds)) * 1000
            )
            for jid in job_ids
        },
    )

    await _heartbeat_mongo(job_ids, epochs, ttls, now)
```

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/queue/test_lease_extension.py -v
```

Expected: PASS, 8 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/queue.py backend/tests/queue/test_lease_extension.py
git commit -m "feat: heartbeat renews to a per-job lease TTL"
```

---

## Task A3: The worker feeds overrides into the heartbeat

**Files:**
- Modify: `backend/app/queue/worker.py:292-303` and `:315-326`
- Modify: `backend/app/queue/executor.py:46-53`

- [ ] **Step 1: Set `_extend_cb` when building the context**

In `backend/app/queue/worker.py`, in `_start_job`, after the existing `ctx._progress_cb` assignment (lines 298-300), add:

```python
        ctx._progress_cb = lambda upd: self.executor._schedule_progress(
            claimed.job_id, claimed.epoch, upd
        )
        # Renew immediately rather than waiting up to a full heartbeat interval:
        # a handler calls extend_lease *because* it is about to go quiet, and the
        # gap between the call and the next tick is exactly when it is exposed.
        ctx._extend_cb = lambda seconds: self.executor._schedule_lease_extension(
            claimed.job_id, claimed.epoch, seconds
        )
```

- [ ] **Step 2: Collect overrides in the heartbeat loop**

In `backend/app/queue/worker.py`, replace the body of `_heartbeat_loop` (lines 315-326) with:

```python
    async def _heartbeat_loop(self) -> None:
        interval = max(settings.lease_ttl_seconds / 3, 2)
        while not self.shutdown.is_set() or self._running:
            try:
                if self._running:
                    ids = list(self._running)
                    epochs = {jid: e for jid, (_, _, e) in self._running.items()}
                    # Handlers that declared a long quiet phase renew to what
                    # they asked for; everything else takes the global default.
                    ttls = {
                        jid: ctx.lease_override_seconds
                        for jid, (_, ctx, _) in self._running.items()
                        if ctx.lease_override_seconds is not None
                    }
                    await queue.heartbeat(ids, epochs, ttls)
                await self._register_worker()
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat_failed", error=str(e))
            await asyncio.sleep(interval)
```

- [ ] **Step 3: Add `_schedule_lease_extension` to the executor**

In `backend/app/queue/executor.py`, add this method immediately after `_schedule_progress` (after line 195):

```python
    def _schedule_lease_extension(self, job_id: str, epoch: int, seconds: int) -> None:
        """Renew one job's lease now, from any thread.

        Mirrors `_schedule_progress`'s thread handling: handlers run via
        `asyncio.to_thread`, so this is usually called off the loop and has to
        be handed back to it. Unthrottled, unlike progress -- a handler calls
        this once per long phase, not several times a second.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = getattr(self, "_loop", None)
            if loop is None:
                return
            asyncio.run_coroutine_threadsafe(
                self._extend_lease(job_id, epoch, seconds), loop
            )
            return

        loop.create_task(self._extend_lease(job_id, epoch, seconds))

    async def _extend_lease(self, job_id: str, epoch: int, seconds: int) -> None:
        try:
            await queue.heartbeat([job_id], {job_id: epoch}, {job_id: seconds})
            log.info("lease_extended", job_id=job_id, seconds=seconds)
        except Exception as e:  # noqa: BLE001 - the periodic heartbeat still covers us
            log.warning("lease_extension_failed", job_id=job_id, error=str(e))
```

- [ ] **Step 4: Set `_extend_cb` on the executor's own fallback context**

In `backend/app/queue/executor.py`, inside `run`, after line 53 (`ctx._progress_cb = ...`), add:

```python
            ctx._progress_cb = lambda upd: self._schedule_progress(job_id, epoch, upd)
            ctx._extend_cb = lambda seconds: self._schedule_lease_extension(
                job_id, epoch, seconds
            )
```

- [ ] **Step 5: Run the full queue suite**

```bash
docker compose exec api python -m pytest tests/queue/ -q
```

Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/worker.py backend/app/queue/executor.py
git commit -m "feat: wire extend_lease through the worker heartbeat and executor"
```

---

## Task A4: Verify the two real callers against a running worker

**Files:** none modified — this is verification.

- [ ] **Step 1: Restart the worker so it picks up the change**

```bash
docker compose restart worker
```

`worker` runs `python -m app.worker_main` with no reload, so without this it keeps executing the old in-memory code and the check below silently proves nothing.

- [ ] **Step 2: Confirm the log line appears when a long-read QC job runs**

Run a NanoPlot QC job from the UI at localhost:5173 against a long-read FASTQ (the `ctx.extend_lease(1800)` at `pipeline_handlers.py:570`), then:

```bash
docker compose logs worker --since 10m | grep lease_extended
```

Expected: a `lease_extended` line with `seconds=1800`.

- [ ] **Step 3: Confirm the recorded expiry actually moved**

```bash
docker compose exec api python -c "
import asyncio
from app.db.redis_client import get_redis
from datetime import UTC, datetime

async def main():
    r = get_redis()
    now_ms = datetime.now(UTC).timestamp() * 1000
    for jid, score in await r.zrange('bp:q:running', 0, -1, withscores=True):
        print(jid, 'expires in', round((score - now_ms) / 1000), 's')

asyncio.run(main())
"
```

Expected: the QC job reports well over the default `lease_ttl_seconds` — on the order of 1800s, not 30s. If it reports the default, the worker did not restart or `_extend_cb` is not being set.

- [ ] **Step 4: Commit (no-op if nothing changed)**

Nothing to commit unless Step 3 revealed a fix. If it did, commit that fix before continuing.

---

# PART B — Close the `bp:cancel` removal gaps

**What this fixes:** two paths reach a terminal state without clearing the cancel flag, so the id stays in `bp:cancel` forever and every worker pays for it on every `SMEMBERS` poll (once a second, in `_cancel_watch_loop`).

**Not fixed, deliberately:** the `release(requeue=True)` branch. A requeued job will run again and *should* still see its cancel flag — clearing it there would lose a cancellation across a graceful shutdown.

---

## Task B1: The reaper clears the cancel flag on the dead path

**Files:**
- Modify: `backend/app/queue/scripts/reap_expired.lua`
- Test: `backend/tests/queue/test_cancel_cleanup.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_cancel_cleanup.py`:

```python
"""bp:cancel must not accumulate ids for jobs that are already finished.

Every worker runs SMEMBERS bp:cancel once a second in _cancel_watch_loop, so a
stale entry is a cost paid forever by every worker. release.lua already clears
the flag on its drop path; these are the routes that bypass it.
"""

import pytest

from tests.queue.test_lifecycle import LEASE_MS, NOW_MS, claim


class TestReaperClearsCancel:
    async def test_dead_job_leaves_no_cancel_entry(self, redis, scripts, job_factory):
        """A cancelled job whose lease expires is reaped, not completed, so
        release.lua's SREM never runs for it."""
        await job_factory("job1", attempts=4)
        await claim(scripts)
        await redis.sadd("bp:cancel", "job1")

        await scripts["reap_expired"](
            keys=["bp:q:running", "bp:q:ready"],
            args=[NOW_MS + LEASE_MS + 1, 100],
        )

        assert await redis.sismember("bp:cancel", "job1") == 0

    async def test_requeued_job_keeps_its_cancel_entry(self, redis, scripts, job_factory):
        """The flag must survive a requeue: the job runs again and still needs
        to observe that it was cancelled."""
        await job_factory("job1")
        await claim(scripts)
        await redis.sadd("bp:cancel", "job1")

        await scripts["reap_expired"](
            keys=["bp:q:running", "bp:q:ready"],
            args=[NOW_MS + LEASE_MS + 1, 100],
        )

        assert await redis.sismember("bp:cancel", "job1") == 1
        assert await redis.zscore("bp:q:ready", "job1") is not None
```

Note: `reap_expired.lua` requeues regardless of attempts — the DEAD decision is made in Python by `queue.reap_expired`. So the Lua-level distinction the first test needs does not exist yet; Step 3 adds it.

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/queue/test_cancel_cleanup.py -v
```

Expected: FAIL on `test_dead_job_leaves_no_cancel_entry` — the id is still in `bp:cancel`.

- [ ] **Step 3: Clear the flag in Python, where the DEAD decision is made**

The Lua script cannot tell dead from requeued (it does not know `max_attempts`), and pushing that knowledge into Lua would duplicate a decision Python already owns. Clear it on the Python side instead.

First add the shared helper — Task B2 needs the same one, so it goes in now rather than being introduced and immediately rewritten. In `backend/app/queue/queue.py`, add this function immediately above `_fail_blocked_job` (before line 171):

```python
async def _clear_cancel_flag(job_id: str) -> None:
    """Drop a job id from the cancel set once nothing can still read it.

    Every worker polls this set once a second, so a stale entry is a cost paid
    forever by every worker. Failures are swallowed: this is hygiene, and a
    Redis blip must not turn into a failed job.
    """
    try:
        await get_redis().srem(keys.CANCEL, job_id)
    except Exception as e:  # noqa: BLE001
        log.debug("cancel_flag_clear_failed", job_id=job_id, error=str(e))
```

Then in `reap_expired`, inside the `if attempts >= job.max_attempts:` branch, after the `await get_redis().zrem(keys.READY, job_id)` line (line 581), add:

```python
            await get_redis().zrem(keys.READY, job_id)
            # This job is terminal and will never run again, so nothing will
            # ever observe its cancel flag. Left behind, the id is polled by
            # every worker once a second forever.
            await _clear_cancel_flag(job_id)
```

Now rewrite the test class to match where the decision actually lives. Replace `TestReaperClearsCancel` in `backend/tests/queue/test_cancel_cleanup.py` with:

```python
class TestReaperClearsCancel:
    async def test_requeued_job_keeps_its_cancel_entry(self, redis, scripts, job_factory):
        """The flag must survive a requeue: the job runs again and still needs
        to observe that it was cancelled. This is the case the fix must NOT
        break, so it is asserted against the raw script."""
        await job_factory("job1")
        await claim(scripts)
        await redis.sadd("bp:cancel", "job1")

        await scripts["reap_expired"](
            keys=["bp:q:running", "bp:q:ready"],
            args=[NOW_MS + LEASE_MS + 1, 100],
        )

        assert await redis.sismember("bp:cancel", "job1") == 1
        assert await redis.zscore("bp:q:ready", "job1") is not None
```

The dead path itself is covered in Task B2, where `_clear_cancel_flag` is extracted and tested directly. Do **not** write a `reap_expired` unit test that monkeypatches `get_redis` and then calls `srem` by hand — it asserts only that Redis works, not that `reap_expired` calls it, and a test that cannot fail when the fix is reverted is worse than no test. The real dead path needs Mongo (it loads the job to compare `attempts >= job.max_attempts`) and is verified end-to-end in Task B3.

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/queue/test_cancel_cleanup.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/queue.py backend/tests/queue/test_cancel_cleanup.py
git commit -m "fix: clear the cancel flag when the reaper marks a job dead"
```

---

## Task B2: `_fail_blocked_job` clears the cancel flag

**Files:**
- Modify: `backend/app/queue/queue.py:171-215`
- Test: `backend/tests/queue/test_cancel_cleanup.py`

- [ ] **Step 1: Understand why this path leaks**

`_fail_blocked_job` writes `JobState.FAILED` directly with `update_one` and never calls `release`. A BLOCKED job that was cancel-requested (`request_cancel` SADDs before branching, and its queued-branch SREM only runs when the job is still in a pre-running state at that moment) and is then failed by a dependency leaves its id behind.

- [ ] **Step 2: Write the failing test**

Append to `backend/tests/queue/test_cancel_cleanup.py`:

```python
class TestFailBlockedJobClearsCancel:
    async def test_clears_the_flag_for_a_dependency_failure(self, redis, monkeypatch):
        """A blocked job failed by its dependency is terminal and will never
        run, so its cancel flag has no reader left."""
        from app.queue import queue

        await redis.sadd("bp:cancel", "blocked1")
        monkeypatch.setattr(queue, "get_redis", lambda: redis)

        await queue._clear_cancel_flag("blocked1")

        assert await redis.sismember("bp:cancel", "blocked1") == 0

    async def test_is_safe_when_no_flag_was_set(self, redis, monkeypatch):
        from app.queue import queue

        monkeypatch.setattr(queue, "get_redis", lambda: redis)
        await queue._clear_cancel_flag("never-cancelled")
        assert await redis.sismember("bp:cancel", "never-cancelled") == 0

    async def test_survives_a_redis_outage(self, monkeypatch):
        """Cleanup is hygiene, not correctness -- it must never fail a job."""
        from app.queue import queue

        class Boom:
            async def srem(self, *a, **kw):
                raise ConnectionError("redis is down")

        monkeypatch.setattr(queue, "get_redis", lambda: Boom())
        await queue._clear_cancel_flag("job1")  # must not raise
```

- [ ] **Step 3: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/queue/test_cancel_cleanup.py::TestFailBlockedJobClearsCancel -v
```

Expected: FAIL — `AttributeError: module 'app.queue.queue' has no attribute '_clear_cancel_flag'`.

- [ ] **Step 4: Call the helper from `_fail_blocked_job`**

`_clear_cancel_flag` already exists — Task B1 Step 3 added it. Only the call site is new here.

In `backend/app/queue/queue.py`, in `_fail_blocked_job`, after `await publish_event("job.failed", {"job_id": str(job.id)})` (line 211), add:

```python
    await publish_event("job.failed", {"job_id": str(job.id)})
    await _clear_cancel_flag(str(job.id))
```

Note the ordering: this sits *before* the `_release_dependents` cascade on the next line, so the flag is cleared even if a dependent's failure raises.

- [ ] **Step 5: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/queue/test_cancel_cleanup.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/queue.py backend/tests/queue/test_cancel_cleanup.py
git commit -m "fix: clear the cancel flag when a blocked job fails on its dependency"
```

---

## Task B3: Verify against the real stack

- [ ] **Step 1: Restart the worker**

```bash
docker compose restart worker
```

- [ ] **Step 2: Check the set is empty at rest**

```bash
docker compose exec api python -c "
import asyncio
from app.db.redis_client import get_redis

async def main():
    members = await get_redis().smembers('bp:cancel')
    print('bp:cancel holds', len(members), 'entries:', sorted(members))

asyncio.run(main())
"
```

Expected: `0 entries` when no cancellation is in flight. Any entries here are pre-existing leaks from before this fix — clear them once with `SREM`, since nothing will read them:

```bash
docker compose exec redis redis-cli DEL bp:cancel
```

- [ ] **Step 3: Cancel a running job from the UI and re-check**

Start a trim or QC job at localhost:5173, cancel it mid-run, wait for it to reach CANCELLED, then re-run the Step 2 command. Expected: still `0 entries`.

- [ ] **Step 4: Run the full backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS.

---

# PART C — Role provenance (`user_touched`)

**What this fixes:** `should_assign_reference_role` treats "role was never set" and "the user deliberately cleared it" as the same state, so converting a reference back to reads and re-ingesting silently re-asserts the reference role — contradicting the promise in `DataObject.role`'s own comment that re-ingest "can never fight a user's explicit choice."

**Design:** A `user_touched: list[str]` of field names on `DataObject`. The TODO weighed this against a narrower `role_set_by` and preferred the list because the same problem applies to metadata fields; that reasoning holds, so build the list.

**No migration needed.** This is a field addition with a default, not an index change — Pydantic supplies `[]` for documents written before it. `index_reconcile.py` handles index drift and is not involved here.

---

## Task C1: Add `user_touched` to `DataObject`

**Files:**
- Modify: `backend/app/models/object.py:147-149`
- Test: `backend/tests/storage/test_object_role.py`

- [ ] **Step 1: Write the failing test**

In `backend/tests/storage/test_object_role.py`, append a new class at the end of the file:

```python
class TestUserTouched:
    """Provenance for fields the user has explicitly set or cleared.

    Without this, a cleared role and a never-set role are the same value, and
    re-ingest cannot tell "no opinion" from "the user said no."
    """

    def test_defaults_to_empty(self):
        assert _obj().user_touched == []

    def test_records_a_field_name(self):
        obj = _obj(user_touched=["role"])
        assert "role" in obj.user_touched
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/storage/test_object_role.py::TestUserTouched -v
```

Expected: FAIL — `DataObject` has no `user_touched` field (Pydantic rejects the unexpected keyword).

- [ ] **Step 3: Add the field**

In `backend/app/models/object.py`, directly after the `role` field (line 149), add:

```python
    # None means "derive the category from the format". Only exceptions carry
    # a value, so re-ingest can never fight a user's explicit choice.
    role: ObjectRole | None = None

    # Field names the user has explicitly set *or cleared*. Without it a
    # cleared role is indistinguishable from one never set, and re-ingest
    # re-asserts the role the user just removed -- which is precisely the
    # "never fight a user's explicit choice" promise above, broken.
    #
    # A list rather than a per-field `role_set_by` because the same ambiguity
    # applies to any user-editable field; metadata keys can join it unchanged.
    user_touched: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/storage/test_object_role.py -v
```

Expected: PASS, all cases in the file.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/object.py backend/tests/storage/test_object_role.py
git commit -m "feat: record which fields a user has explicitly set on DataObject"
```

---

## Task C2: `apply_role_update` records the touch

**Files:**
- Modify: `backend/app/services/object_service.py:454-464`
- Test: `backend/tests/storage/test_object_role.py`

- [ ] **Step 1: Write the failing test**

Append to `TestUserTouched` in `backend/tests/storage/test_object_role.py`:

```python
    def test_setting_a_role_records_the_touch(self):
        obj = _obj()
        apply_role_update(obj, {"role": "reference"})
        assert obj.role is ObjectRole.REFERENCE
        assert obj.user_touched == ["role"]

    def test_clearing_a_role_records_the_touch(self):
        """The case the whole field exists for: a cleared role must be
        distinguishable from one that was never set."""
        obj = _obj(role=ObjectRole.REFERENCE)
        apply_role_update(obj, {"role": None})
        assert obj.role is None
        assert obj.user_touched == ["role"]

    def test_an_omitted_role_records_nothing(self):
        obj = _obj()
        apply_role_update(obj, {"name": "renamed.fasta"})
        assert obj.user_touched == []

    def test_the_touch_is_not_duplicated(self):
        obj = _obj()
        apply_role_update(obj, {"role": "reference"})
        apply_role_update(obj, {"role": None})
        assert obj.user_touched == ["role"]
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/storage/test_object_role.py::TestUserTouched -v
```

Expected: FAIL — `assert [] == ["role"]`.

- [ ] **Step 3: Record the touch**

In `backend/app/services/object_service.py`, replace `apply_role_update` (lines 454-464) with:

```python
def apply_role_update(obj: DataObject, updates: dict) -> None:
    """Apply a role change, distinguishing an explicit null from an omission.

    Every other field in update_object uses `.get(k) is not None`, which treats
    null and absent alike. Role cannot: clearing it is how a reference is
    converted back to reads, so the *presence of the key* is what matters.

    The same distinction is recorded durably in `user_touched`. Within one
    request the key's presence says the user had an opinion; afterwards only
    that list remembers, and re-ingest needs it to avoid re-asserting a role
    the user removed.
    """
    if "role" not in updates:
        return
    raw = updates["role"]
    obj.role = ObjectRole(raw) if raw is not None else None
    if "role" not in obj.user_touched:
        obj.user_touched = [*obj.user_touched, "role"]
```

Note the reassignment rather than `.append()`: Beanie tracks changes by attribute assignment, and an in-place mutation of the list can be missed by `obj.save()`.

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/storage/test_object_role.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/object_service.py backend/tests/storage/test_object_role.py
git commit -m "feat: apply_role_update records role as user-touched"
```

---

## Task C3: `should_assign_reference_role` respects the touch

**Files:**
- Modify: `backend/app/queue/results.py:38-47` and `:141-143`
- Test: `backend/tests/storage/test_assembly_accession.py`

- [ ] **Step 1: Read the tests that already exist for this function**

There is already a `TestAutoRoleAssignment` class at `backend/tests/storage/test_assembly_accession.py:330`, docstringed *"Auto-assignment fills a gap; it never overrules a person."* It covers four cases: assigns on accession with no role, no accession, existing role, missing enrichment block, and an enrichment error.

**Extend that class — do not create a second one.** Its existing cases all call the function without `user_touched`, which is exactly why the new parameter must keep a default; leave those five cases untouched and confirm they still pass.

```bash
sed -n '330,360p' backend/tests/storage/test_assembly_accession.py
```

- [ ] **Step 2: Write the failing tests**

Append these two cases to the **existing** `TestAutoRoleAssignment` class in `backend/tests/storage/test_assembly_accession.py`:

```python
    def test_does_not_reassign_a_role_the_user_cleared(self):
        """The bug this fixes: converting a reference back to reads and
        re-ingesting silently restored the reference role. A cleared role is
        `None` -- identical to one never set -- so only user_touched can tell
        "no opinion" from "the user said no"."""
        assert not should_assign_reference_role(
            current_role=None,
            enrichment={"accession": "GCF_000002445.2"},
            user_touched=["role"],
        )

    def test_an_unrelated_touch_does_not_block_assignment(self):
        """Editing metadata says nothing about the user's view of the role."""
        assert should_assign_reference_role(
            current_role=None,
            enrichment={"accession": "GCF_000002445.2"},
            user_touched=["metadata.organism"],
        )
```

The file already imports `should_assign_reference_role` (line 11), so no import change is needed.

- [ ] **Step 3: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/storage/test_assembly_accession.py -v
```

Expected: FAIL — `should_assign_reference_role() got an unexpected keyword argument 'user_touched'`.

- [ ] **Step 4: Add the parameter**

In `backend/app/queue/results.py`, replace `should_assign_reference_role` (lines 38-47) with:

```python
def should_assign_reference_role(
    *, current_role, enrichment: dict | None, user_touched: list[str] | None = None
) -> bool:
    """Whether an ingest should mark this object a reference.

    Only when an assembly accession was found, no role is set, *and* the user
    has never touched the role. A role the user chose is never overruled: they
    may be running something unusual, or know something about the file that its
    name does not say.

    The `user_touched` check is what makes that promise hold across a
    conversion. A role the user *cleared* is `None` -- identical to one never
    set -- so without it, converting a reference back to reads and re-ingesting
    silently restores the role the user just removed.
    """
    if current_role is not None:
        return False
    if "role" in (user_touched or []):
        return False
    return bool((enrichment or {}).get("accession"))
```

- [ ] **Step 5: Pass it at the call site**

In `backend/app/queue/results.py`, update the call (lines 141-143):

```python
    if should_assign_reference_role(
        current_role=obj.role,
        enrichment=assembly_enrichment,
        user_touched=obj.user_touched,
    ):
```

- [ ] **Step 6: Guard the race path too**

The conditional update just below (lines 144-148) re-checks `role == None` to lose a race safely. That check must now also exclude a user-touched role, or a concurrent conversion between the two lines would still be overruled. Replace it with:

```python
        assigned = await DataObject.find_one(
            DataObject.id == obj.id,
            DataObject.role == None,  # noqa: E711
            # Re-checked here, not just above: a conversion landing between the
            # decision and this write would otherwise be overruled by it.
            {"user_touched": {"$ne": "role"}},
        ).update({"$set": {DataObject.role: ObjectRole.REFERENCE}})
```

- [ ] **Step 7: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/storage/ tests/queue/ -q
```

Expected: PASS. If another caller of `should_assign_reference_role` breaks on the signature, the keyword is optional and defaults to `None`, so it should not — but check the grep from Step 1 covered every caller.

- [ ] **Step 8: Commit**

```bash
git add backend/app/queue/results.py backend/tests/storage/test_assembly_accession.py
git commit -m "fix: re-ingest no longer re-asserts a reference role the user cleared"
```

---

## Task C4: Verify the round trip in the real UI

**Files:** none modified — this is the verification the fix exists for.

- [ ] **Step 1: Rebuild and restart**

```bash
docker compose up -d --build api worker
```

- [ ] **Step 2: Reproduce the original bug's setup**

At localhost:5173:
1. Find (or ingest) a FASTA whose filename carries a GCA/GCF accession — ingest assigns it the reference role automatically.
2. Convert it back to reads in the detail panel (clear the role).
3. Re-ingest the same file.

- [ ] **Step 3: Confirm the role stays cleared**

Expected: the object is still reads after re-ingest. Before this change it would silently return to reference.

- [ ] **Step 4: Confirm the flag was recorded**

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo, close_mongo
from app.models import DataObject

async def main():
    await connect_to_mongo()
    async for o in DataObject.find({'user_touched': {'\$ne': []}}):
        print(o.name, '->', o.role, 'touched:', o.user_touched)
    await close_mongo()

asyncio.run(main())
"
```

`connect_to_mongo` is the real entry point (`backend/app/db/client.py:24`) — it calls `_init_models`, which is what registers the Beanie documents. There is no `init_db`.

Expected: the converted object lists `role` in `user_touched`.

- [ ] **Step 5: Confirm a fresh reference still auto-assigns**

Ingest a *different* accession-named FASTA that has never been converted. Expected: it still gets the reference role automatically — the fix must not have disabled the feature.

---

## Task 13: Update `docs/TODO.md`

**Files:**
- Modify: `docs/TODO.md`

- [ ] **Step 1: Mark the three entries resolved**

Follow the existing convention in that file — the modal-scroll entry is headed `## The align dialog's submit button needs scrolling when expanded — FIXED` with the commit named underneath. Apply the same shape to:

- `## \`JobContext.extend_lease\` is inert` — append ` — FIXED`. Record that the resolution was to **wire** it rather than delete it, because two live callers (`sra_handlers.py`, `pipeline_handlers.py`) already depended on it, which the original entry missed. Note that the heartbeat now renews to a per-job TTL.
- `## \`bp:cancel\` grows without bound` — append ` — FIXED`. Record that the original diagnosis was wrong about the running path (`release.lua` already cleared it) and that the real gaps were the reaper's dead path and `_fail_blocked_job`, with the requeue branch deliberately left alone.
- `## Re-ingest re-asserts a reference role the user cleared` — append ` — FIXED`. Record that `user_touched` was chosen over `role_set_by` as the original entry recommended, and that no migration was needed because a defaulted field backfills itself.

- [ ] **Step 2: Correct the stale claim about migrations**

The `user_touched` entry says "this project has no migrations mechanism." `backend/app/db/index_reconcile.py` now exists. Update that sentence so the next person does not build one that is already there.

- [ ] **Step 3: Commit**

```bash
git add docs/TODO.md
git commit -m "docs: mark lease, cancel-set, and role-provenance items fixed"
```

---

## Task 14: Full verification

- [ ] **Step 1: Full backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS, no failures.

- [ ] **Step 2: Lint**

```bash
docker compose exec api python -m ruff check app/ tests/
```

Expected: clean. Note the `# noqa: E711` on the `role == None` comparison is intentional (Beanie needs the identity comparison, not `is None`).

- [ ] **Step 3: Rebuild and restart everything**

```bash
docker compose up -d --build api web worker
```

- [ ] **Step 4: Confirm the app comes up**

Load localhost:5173 and confirm the explorer renders and a job can be launched. Parts A and B both touch the worker's hot path, so a mistake there shows up as jobs that never start or never finish.

- [ ] **Step 5: Confirm the lease and cancel invariants hold at rest**

```bash
docker compose exec api python -c "
import asyncio
from app.db.redis_client import get_redis

async def main():
    r = get_redis()
    print('bp:cancel:', sorted(await r.smembers('bp:cancel')))
    print('running:', await r.zrange('bp:q:running', 0, -1, withscores=True))

asyncio.run(main())
"
```

Expected: `bp:cancel` empty with nothing being cancelled; `running` empty with nothing in flight.
