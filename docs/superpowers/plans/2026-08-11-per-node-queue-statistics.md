# Per-node queue statistics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose per-node queue depth and resource reservations on
`GET /api/v1/system/load` and `GET /api/v1/nodes`, and show queue depth in the
nodes settings table.

**Architecture:** One new module, `app/queue/node_stats.py`, reads the
per-node Redis keys that `enqueue()` and `claim.lua` already write
(`bp:q:ready:{node_id}`, `bp:conc:{resource}:{node_id}`) in a single pipeline.
Both endpoints call it at request time -- not from the governor's published
snapshot -- so the numbers stay correct when the leader lock lapses. A `SCAN`
for ready queues belonging to no enrolled node surfaces jobs targeted at a
misspelled node ID, which are otherwise invisible.

**Tech Stack:** Python 3.12, FastAPI, redis.asyncio, Beanie/Motor, pytest +
pytest-asyncio + fakeredis; React 18 + TypeScript on the frontend.

**Spec:** `docs/superpowers/specs/2026-08-11-per-node-queue-statistics-design.md`

---

## Facts verified before writing this plan

Do not re-derive these; they are checked and correct as of 2026-08-11.

- **Tests use `fakeredis`, not a live Redis.** `backend/tests/api/test_nodes.py`
  defines its own `fake_redis` fixture and patches `get_redis` **at the module
  that imported it** (`app.api.v1.nodes.get_redis`). A new module needs its own
  patch target: `app.queue.node_stats.get_redis`.
- **`fakeredis` supports `SCAN` with `match`.** Verified: with
  `bp:q:ready:gpu` and `bp:q:ready` both set, `match="bp:q:ready:*"` returns
  only `['bp:q:ready:gpu']`. The bare global queue key does **not** match, so
  it cannot appear as a phantom node.
- **A pipelined `ZCARD` + `MGET` on absent keys returns `[0, [None, None,
  None]]`-shaped output.** Verified `[1, [None, None, None]]` with one member
  present. The helper must coalesce `None` to `0`.
- **`test_nodes.py` runs without a Mongo fixture.** `list_nodes()` calls
  `Node.find_all()`, which raises, and the existing `except Exception:
  log.warning("node_mongo_read_failed")` swallows it, leaving `mongo_nodes`
  empty. New tests in that file inherit this: assertions about Mongo-sourced
  fields would need the `mongo_db` fixture, so this plan's tests avoid them.
- **`/system/load` returns `current_load()` verbatim** from
  `app/queue/governor.py`, which has two return paths: the cached Redis
  snapshot, and a psutil fallback with `governor_active: False`. Both need the
  new key.

## Running the tests

Always from the worktree root, never `docker compose exec api` (that silently
tests `main`'s code):

```bash
./backend/run-worktree-tests.sh tests/api/test_nodes.py -q
```

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/queue/node_stats.py` (create) | Read per-node Redis counters; discover orphaned queues. The only place that knows the per-node key layout is read. |
| `backend/tests/queue/test_node_stats.py` (create) | Unit tests for the above. |
| `backend/app/api/v1/nodes.py` (modify) | Use the helper; add `queued_jobs`; union in orphans. |
| `backend/tests/api/test_nodes.py` (modify) | Endpoint tests for the new fields. |
| `backend/app/queue/governor.py` (modify) | `current_load()` attaches `nodes` on both return paths. |
| `backend/tests/queue/test_governor_nodes.py` (create) | Tests for the `nodes` key on `/system/load`'s backing function. |
| `frontend/src/api/types.ts` (modify) | `NodeInfo.queued_jobs`, `SystemLoadNode`, `SystemLoad.nodes`. |
| `frontend/src/components/SettingsNodes.tsx` (modify) | Queued column; unknown-node rendering. |

Node enumeration (workers hash + Mongo records) stays in `nodes.py` and is
extracted into a helper that `governor.py` imports, so the two endpoints agree
on what a node is without duplicating the merge logic.

---

## Task 1: `node_stats()` — per-node counters in one pipeline

**Files:**
- Create: `backend/app/queue/node_stats.py`
- Test: `backend/tests/queue/test_node_stats.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/queue/test_node_stats.py`:

```python
"""Tests for per-node queue and reservation counters."""

from unittest.mock import patch

import fakeredis.aioredis
import pytest

from app.queue.node_stats import node_stats


@pytest.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


class TestNodeStats:
    async def test_absent_keys_read_as_zero(self, fake_redis):
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["ghost"])
        assert stats == {
            "ghost": {"queued": 0, "cpu": 0, "mem_mb": 0, "io_heavy": 0}
        }

    async def test_counts_ready_queue_depth(self, fake_redis):
        await fake_redis.zadd("bp:q:ready:gpu", {"j1": 1, "j2": 2, "j3": 3})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["gpu"])
        assert stats["gpu"]["queued"] == 3

    async def test_reads_reservation_counters(self, fake_redis):
        await fake_redis.mset(
            {
                "bp:conc:cpu:gpu": "8",
                "bp:conc:mem_mb:gpu": "16384",
                "bp:conc:io_heavy:gpu": "1",
            }
        )
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["gpu"])
        assert stats["gpu"]["cpu"] == 8
        assert stats["gpu"]["mem_mb"] == 16384
        assert stats["gpu"]["io_heavy"] == 1

    async def test_negative_counters_clamp_to_zero(self, fake_redis):
        # A counter can go negative if a release double-decrements; the UI
        # should read zero rather than a nonsense negative reservation.
        await fake_redis.mset({"bp:conc:cpu:gpu": "-3"})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["gpu"])
        assert stats["gpu"]["cpu"] == 0

    async def test_global_ready_queue_is_not_a_node(self, fake_redis):
        # bp:q:ready (no node suffix) is the global pool. Asking for stats on
        # a node must never read it.
        await fake_redis.zadd("bp:q:ready", {"global1": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["gpu"])
        assert stats["gpu"]["queued"] == 0

    async def test_multiple_nodes_in_one_call(self, fake_redis):
        await fake_redis.zadd("bp:q:ready:gpu", {"j1": 1})
        await fake_redis.zadd("bp:q:ready:cpu-node", {"j2": 1, "j3": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["gpu", "cpu-node"])
        assert stats["gpu"]["queued"] == 1
        assert stats["cpu-node"]["queued"] == 2

    async def test_empty_input_makes_no_redis_call(self, fake_redis):
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            assert await node_stats([]) == {}

    async def test_redis_failure_returns_zeroes(self):
        # The endpoints must stay up when Redis is down. This asserts the
        # direction that fails when the error handling breaks -- a happy-path
        # test would pass either way.
        class Boom:
            def pipeline(self):
                raise ConnectionError("redis is down")

        with patch("app.queue.node_stats.get_redis", return_value=Boom()):
            stats = await node_stats(["gpu"])
        assert stats == {
            "gpu": {"queued": 0, "cpu": 0, "mem_mb": 0, "io_heavy": 0}
        }
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/queue/test_node_stats.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named
'app.queue.node_stats'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/queue/node_stats.py`:

```python
"""Per-node queue depth and resource reservations.

`enqueue()` and `claim.lua` already write per-node keys -- `bp:q:ready:{node}`
and `bp:conc:{resource}:{node}`. This is the only module that reads them in
aggregate, so both `/api/v1/nodes` and `/api/v1/system/load` report the same
numbers without duplicating the key layout.

Everything here degrades to zeroes rather than raising: a node table that
renders with zeros while Redis is down is worth more than a 500 on the page
someone opened *because* something looked wrong.
"""

from collections.abc import Iterable

from app.db.redis_client import get_redis
from app.logging import get_logger
from app.queue import keys

log = get_logger(__name__)

_ZERO = {"queued": 0, "cpu": 0, "mem_mb": 0, "io_heavy": 0}


def _int(value) -> int:
    """A counter as a non-negative int; absent, junk, and negative all read 0.

    Negative is possible if a release double-decrements. Reporting a negative
    reservation would be worse than reporting none.
    """
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


async def node_stats(node_ids: Iterable[str]) -> dict[str, dict]:
    """Queue depth and reservations for each node, in one round trip.

    `queued` is ready-queue depth only. Delayed jobs live in one global sorted
    set with no node scoping and blocked jobs are not in Redis at all, so
    neither is countable per node without new bookkeeping on the write path.
    """
    ids = list(node_ids)
    if not ids:
        return {}

    try:
        pipe = get_redis().pipeline()
        for node_id in ids:
            pipe.zcard(keys.ready_key(node_id))
            pipe.mget(keys.node_conc_keys(node_id))
        results = await pipe.execute()
    except Exception as e:  # noqa: BLE001
        log.warning("node_stats_read_failed", error=str(e))
        return {node_id: dict(_ZERO) for node_id in ids}

    stats: dict[str, dict] = {}
    for index, node_id in enumerate(ids):
        queued = results[index * 2]
        cpu, mem_mb, io_heavy = results[index * 2 + 1]
        stats[node_id] = {
            "queued": _int(queued),
            "cpu": _int(cpu),
            "mem_mb": _int(mem_mb),
            "io_heavy": _int(io_heavy),
        }
    return stats
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/test_node_stats.py -q
```

Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/node_stats.py backend/tests/queue/test_node_stats.py
git commit -m "feat(queue): read per-node queue depth and reservations in one pipeline"
```

---

## Task 2: `orphaned_queue_nodes()` — find queues nobody drains

**Files:**
- Modify: `backend/app/queue/node_stats.py`
- Test: `backend/tests/queue/test_node_stats.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/queue/test_node_stats.py`. Add
`orphaned_queue_nodes` to the existing import at the top of the file so it
reads:

```python
from app.queue.node_stats import node_stats, orphaned_queue_nodes
```

Then append this class:

```python
class TestOrphanedQueueNodes:
    async def test_none_when_every_queue_is_known(self, fake_redis):
        await fake_redis.zadd("bp:q:ready:gpu", {"j1": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            assert await orphaned_queue_nodes({"gpu"}) == []

    async def test_finds_queue_for_unenrolled_node(self, fake_redis):
        # The typo case: someone launched with ?target_node=gpu-nodee and the
        # jobs will sit here forever, drained by nobody.
        await fake_redis.zadd("bp:q:ready:gpu-nodee", {"j1": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            assert await orphaned_queue_nodes({"gpu"}) == ["gpu-nodee"]

    async def test_global_queue_is_never_orphaned(self, fake_redis):
        # bp:q:ready has no node suffix and must not be reported as a node.
        await fake_redis.zadd("bp:q:ready", {"j1": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            assert await orphaned_queue_nodes(set()) == []

    async def test_result_is_sorted(self, fake_redis):
        await fake_redis.zadd("bp:q:ready:zeta", {"j1": 1})
        await fake_redis.zadd("bp:q:ready:alpha", {"j2": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            assert await orphaned_queue_nodes(set()) == ["alpha", "zeta"]

    async def test_redis_failure_returns_empty(self):
        class Boom:
            def scan_iter(self, *a, **kw):
                raise ConnectionError("redis is down")

        with patch("app.queue.node_stats.get_redis", return_value=Boom()):
            assert await orphaned_queue_nodes(set()) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/queue/test_node_stats.py -q
```

Expected: `ImportError: cannot import name 'orphaned_queue_nodes'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/app/queue/node_stats.py`:

```python
async def orphaned_queue_nodes(known: set[str]) -> list[str]:
    """Node IDs that have a ready queue but are not enrolled and have no workers.

    This is how a typo in `?target_node=` becomes visible. Those jobs land in
    `bp:q:ready:{typo}`, which no worker claims from, and they sit there
    forever -- the symptom is "my job never ran" with nothing anywhere to
    explain it.

    `SCAN` rather than `KEYS`: `KEYS` blocks the server for the whole sweep.
    The prefix match excludes the bare `bp:q:ready` global pool, which has no
    node suffix and is not a node.
    """
    prefix = f"{keys.READY}:"
    found: set[str] = set()
    try:
        async for key in get_redis().scan_iter(match=f"{prefix}*", count=100):
            node_id = key[len(prefix):]
            if node_id:
                found.add(node_id)
    except Exception as e:  # noqa: BLE001
        log.warning("orphan_queue_scan_failed", error=str(e))
        return []
    return sorted(found - known)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/test_node_stats.py -q
```

Expected: `13 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/node_stats.py backend/tests/queue/test_node_stats.py
git commit -m "feat(queue): surface ready queues belonging to no enrolled node"
```

---

## Task 3: Extract node enumeration from `list_nodes()`

Both endpoints need the same answer to "what nodes exist." Extract it before
adding a second caller, so the merge logic has one home.

**Files:**
- Modify: `backend/app/api/v1/nodes.py`
- Test: `backend/tests/api/test_nodes.py` (existing tests are the safety net)

- [ ] **Step 1: Run the existing tests to establish a green baseline**

```bash
./backend/run-worktree-tests.sh tests/api/test_nodes.py -q
```

Expected: all pass. Record the count; it must not drop.

- [ ] **Step 2: Add the enumeration helper**

In `backend/app/api/v1/nodes.py`, add this above `list_nodes` (after the
`_node_conc` definition). It is the body currently inlined in `list_nodes`,
lifted verbatim apart from the `import json` moving to the top of the module:

```python
async def enumerate_nodes() -> dict[str, dict]:
    """Every known node, keyed by node_id, merged from Redis and MongoDB.

    Redis knows which workers are heartbeating; MongoDB knows which nodes
    enrolled. A node can be in either without the other: enrolled but not yet
    started (no workers), or heartbeating after its enrollment was revoked.
    Both belong in the result.

    Shared by `/nodes` and `/system/load` so the two cannot disagree about
    what a node is.
    """
    now = datetime.now(UTC)
    threshold = now - timedelta(seconds=_OFFLINE_THRESHOLD_SECONDS)

    mongo_nodes: dict[str, dict] = {}
    try:
        async for doc in Node.find_all():
            mongo_nodes[doc.node_id] = {
                "hostname": doc.hostname,
                "registered_at": doc.registered_at.isoformat() if doc.registered_at else None,
                "enrollment": doc.status,
                "last_seen": doc.last_seen.isoformat() if doc.last_seen else None,
            }
    except Exception:
        log.warning("node_mongo_read_failed")

    try:
        raw = await get_redis().hgetall(keys.WORKERS)
    except Exception:
        log.warning("nodes_read_failed")
        raw = {}

    by_node: dict[str, dict] = {}

    def _blank(node_id: str) -> dict:
        return {
            "node_id": node_id,
            "workers": 0,
            "online_workers": 0,
            "running_jobs": 0,
            "slots": 0,
            "online": False,
        }

    for _worker_id, blob in raw.items():
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        node_id = data.get("node_id", "unknown")
        last_seen_str = data.get("last_seen", "")
        try:
            last_seen = datetime.fromisoformat(last_seen_str)
        except (ValueError, TypeError):
            last_seen = None
        online = last_seen is not None and last_seen > threshold

        entry = by_node.setdefault(node_id, _blank(node_id))
        entry["workers"] += 1
        if online:
            entry["online_workers"] += 1
            entry["online"] = True
        entry["running_jobs"] += len(data.get("running", []))
        entry["slots"] += data.get("slots", 0)

    for node_id in mongo_nodes:
        by_node.setdefault(node_id, _blank(node_id))

    for node_id, entry in by_node.items():
        mongo_info = mongo_nodes.get(node_id, {})
        entry["hostname"] = mongo_info.get("hostname", "")
        entry["registered_at"] = mongo_info.get("registered_at")
        entry["enrollment"] = mongo_info.get("enrollment", "unknown")
        entry["last_seen_mongo"] = mongo_info.get("last_seen")

    return by_node
```

- [ ] **Step 3: Move `import json` to the module top**

`list_nodes` currently has a function-local `import json`. Add `json` to the
stdlib import block at the top of `backend/app/api/v1/nodes.py` (alphabetically
before `os`) so both functions can use it:

```python
import asyncio
import json
import os
```

- [ ] **Step 4: Rewrite `list_nodes` to use the helper**

Replace the entire body of `list_nodes` (everything after its docstring) with:

```python
    by_node = await enumerate_nodes()

    result = []
    for node_id, info in sorted(by_node.items()):
        if info["online"]:
            info["reserved"] = await _node_conc(node_id)
        else:
            info["reserved"] = {"cpu": 0, "mem_mb": 0, "io_heavy": 0}
        result.append(info)

    return result
```

This is behaviour-preserving: same fields, same order, same
online-only-`reserved` rule. Task 4 changes the behaviour.

- [ ] **Step 5: Run the tests to verify nothing regressed**

```bash
./backend/run-worktree-tests.sh tests/api/test_nodes.py -q
```

Expected: the same count as Step 1, all passing.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/nodes.py
git commit -m "refactor(api): extract node enumeration from the nodes endpoint"
```

---

## Task 4: `/api/v1/nodes` gains `queued_jobs` and orphaned nodes

**Files:**
- Modify: `backend/app/api/v1/nodes.py`
- Test: `backend/tests/api/test_nodes.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/api/test_nodes.py`. Note the patch targets: this
endpoint reads Redis through **two** modules now, so both need the fake.

```python
class TestNodeQueueStats:
    async def test_queued_jobs_reports_ready_queue_depth(self, fake_redis):
        await _seed_workers(
            fake_redis,
            **{"host1:1234": _worker_blob("primary", slots=4)},
        )
        await fake_redis.zadd("bp:q:ready:primary", {"j1": 1, "j2": 2})
        with (
            patch("app.api.v1.nodes.get_redis", return_value=fake_redis),
            patch("app.queue.node_stats.get_redis", return_value=fake_redis),
        ):
            nodes = await list_nodes()
        assert nodes[0]["queued_jobs"] == 2

    async def test_queued_jobs_is_zero_with_no_queue(self, fake_redis):
        await _seed_workers(
            fake_redis,
            **{"host1:1234": _worker_blob("primary")},
        )
        with (
            patch("app.api.v1.nodes.get_redis", return_value=fake_redis),
            patch("app.queue.node_stats.get_redis", return_value=fake_redis),
        ):
            nodes = await list_nodes()
        assert nodes[0]["queued_jobs"] == 0

    async def test_offline_node_still_reports_reservations(self, fake_redis):
        # Workers died mid-job and the counters have not been reaped. That is
        # a real condition and hiding it behind zeros makes it undiagnosable.
        await _seed_workers(
            fake_redis,
            **{"dead:host": _worker_blob("primary", online=False)},
        )
        await fake_redis.mset({"bp:conc:cpu:primary": "4"})
        with (
            patch("app.api.v1.nodes.get_redis", return_value=fake_redis),
            patch("app.queue.node_stats.get_redis", return_value=fake_redis),
        ):
            nodes = await list_nodes()
        assert nodes[0]["online"] is False
        assert nodes[0]["reserved"]["cpu"] == 4

    async def test_orphaned_queue_appears_as_unknown_node(self, fake_redis):
        await _seed_workers(
            fake_redis,
            **{"host1:1234": _worker_blob("primary")},
        )
        await fake_redis.zadd("bp:q:ready:gpu-nodee", {"j1": 1})
        with (
            patch("app.api.v1.nodes.get_redis", return_value=fake_redis),
            patch("app.queue.node_stats.get_redis", return_value=fake_redis),
        ):
            nodes = await list_nodes()
        orphan = next(n for n in nodes if n["node_id"] == "gpu-nodee")
        assert orphan["queued_jobs"] == 1
        assert orphan["online"] is False
        assert orphan["workers"] == 0
        assert orphan["enrollment"] == "unknown"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/api/test_nodes.py::TestNodeQueueStats -q
```

Expected: FAIL with `KeyError: 'queued_jobs'` on the first three, and
`StopIteration` on the fourth.

- [ ] **Step 3: Rewrite `list_nodes` to use `node_stats`**

In `backend/app/api/v1/nodes.py`, add the import at the top with the other
`app.queue` import:

```python
from app.queue import keys, node_stats as node_stats_mod
```

Replace the body of `list_nodes` (from Task 3 Step 4) with:

```python
    by_node = await enumerate_nodes()

    # Ready queues for node ids nobody has enrolled: jobs targeted at a
    # misspelled node, which no worker will ever claim.
    for node_id in await node_stats_mod.orphaned_queue_nodes(set(by_node)):
        by_node[node_id] = {
            "node_id": node_id,
            "workers": 0,
            "online_workers": 0,
            "running_jobs": 0,
            "slots": 0,
            "online": False,
            "hostname": "",
            "registered_at": None,
            "enrollment": "unknown",
            "last_seen_mongo": None,
        }

    stats = await node_stats_mod.node_stats(by_node)

    result = []
    for node_id, info in sorted(by_node.items()):
        s = stats.get(node_id, {"queued": 0, "cpu": 0, "mem_mb": 0, "io_heavy": 0})
        info["queued_jobs"] = s["queued"]
        # Reported for offline nodes too: a stale reservation on a node whose
        # workers died is exactly what someone reading this table needs to see.
        info["reserved"] = {
            "cpu": s["cpu"],
            "mem_mb": s["mem_mb"],
            "io_heavy": s["io_heavy"],
        }
        result.append(info)

    return result
```

- [ ] **Step 4: Delete the now-unused `_node_conc`**

`node_stats()` replaces it. Remove the whole `_node_conc` function from
`backend/app/api/v1/nodes.py`. Verify nothing else calls it:

```bash
grep -rn "_node_conc" backend/
```

Expected: no output.

- [ ] **Step 5: Run the full nodes test file**

```bash
./backend/run-worktree-tests.sh tests/api/test_nodes.py -q
```

Expected: all pass, including the four new ones.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/nodes.py backend/tests/api/test_nodes.py
git commit -m "feat(api): report per-node queue depth and stale reservations on /nodes"
```

---

## Task 5: `/api/v1/system/load` gains a `nodes` key

**Files:**
- Modify: `backend/app/queue/governor.py` (the `current_load` function)
- Create: `backend/tests/queue/test_governor_nodes.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/queue/test_governor_nodes.py`:

```python
"""The per-node breakdown attached to /system/load."""

import json
from unittest.mock import patch

import fakeredis.aioredis
import pytest

from app.queue.governor import current_load


@pytest.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _worker_blob(node_id: str, running: list[str], slots: int = 4) -> str:
    from datetime import UTC, datetime

    return json.dumps(
        {
            "last_seen": datetime.now(UTC).isoformat(),
            "slots": slots,
            "running": running,
            "draining": False,
            "node_id": node_id,
        }
    )


def _patches(fake_redis):
    """Every module that reaches Redis on this path."""
    return (
        patch("app.db.redis_client.get_redis", return_value=fake_redis),
        patch("app.queue.node_stats.get_redis", return_value=fake_redis),
        patch("app.api.v1.nodes.get_redis", return_value=fake_redis),
    )


class TestSystemLoadNodes:
    async def test_nodes_key_present_without_a_governor_snapshot(self, fake_redis):
        # No leader has published. The per-node data must still be there --
        # that is the moment someone is most likely looking at the page.
        p1, p2, p3 = _patches(fake_redis)
        with p1, p2, p3:
            load = await current_load()
        assert load["governor_active"] is False
        assert load["nodes"] == []

    async def test_nodes_key_present_with_a_governor_snapshot(self, fake_redis):
        await fake_redis.set(
            "bp:load:snapshot",
            json.dumps({"state": "OPEN", "governor_active": True}),
        )
        await fake_redis.hset("bp:workers", "host1:1", _worker_blob("gpu", ["j1"]))
        p1, p2, p3 = _patches(fake_redis)
        with p1, p2, p3:
            load = await current_load()
        assert load["governor_active"] is True
        assert [n["node_id"] for n in load["nodes"]] == ["gpu"]

    async def test_node_entry_shape(self, fake_redis):
        await fake_redis.hset(
            "bp:workers", "host1:1", _worker_blob("gpu", ["j1", "j2"], slots=8)
        )
        await fake_redis.zadd("bp:q:ready:gpu", {"j3": 1, "j4": 2, "j5": 3})
        await fake_redis.mset(
            {"bp:conc:cpu:gpu": "6", "bp:conc:mem_mb:gpu": "8192"}
        )
        p1, p2, p3 = _patches(fake_redis)
        with p1, p2, p3:
            load = await current_load()
        assert load["nodes"] == [
            {
                "node_id": "gpu",
                "running": 2,
                "queued": 3,
                "cpu": 6,
                "mem_mb": 8192,
                "workers": 1,
                "known": True,
            }
        ]

    async def test_orphaned_queue_is_flagged_unknown(self, fake_redis):
        await fake_redis.zadd("bp:q:ready:gpu-nodee", {"j1": 1})
        p1, p2, p3 = _patches(fake_redis)
        with p1, p2, p3:
            load = await current_load()
        orphan = next(n for n in load["nodes"] if n["node_id"] == "gpu-nodee")
        assert orphan["known"] is False
        assert orphan["queued"] == 1
        assert orphan["workers"] == 0

    async def test_enumeration_failure_reports_the_error(self, fake_redis):
        # An empty list would read as "no nodes" rather than "the read broke",
        # which is the same reasoning as `queue_error` on /system/stats.
        p1, p2, p3 = _patches(fake_redis)
        with (
            p1,
            p2,
            p3,
            patch(
                "app.api.v1.nodes.enumerate_nodes",
                side_effect=RuntimeError("boom"),
            ),
        ):
            load = await current_load()
        assert load["nodes"] == []
        assert "boom" in load["nodes_error"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/queue/test_governor_nodes.py -q
```

Expected: FAIL with `KeyError: 'nodes'`.

- [ ] **Step 3: Implement the attachment**

In `backend/app/queue/governor.py`, add this function immediately above
`current_load`:

```python
async def _node_breakdown() -> tuple[list[dict], str | None]:
    """Per-node running/queued/reserved, and an error string if it failed.

    Imported inside the function: `app.api.v1.nodes` imports from
    `app.queue`, so a module-level import here would be circular.
    """
    try:
        from app.api.v1.nodes import enumerate_nodes
        from app.queue import node_stats as node_stats_mod

        by_node = await enumerate_nodes()
        known = set(by_node)
        orphans = await node_stats_mod.orphaned_queue_nodes(known)
        stats = await node_stats_mod.node_stats(list(known) + orphans)

        nodes = []
        for node_id in sorted(known | set(orphans)):
            info = by_node.get(node_id, {})
            s = stats.get(node_id, {"queued": 0, "cpu": 0, "mem_mb": 0})
            nodes.append(
                {
                    "node_id": node_id,
                    "running": info.get("running_jobs", 0),
                    "queued": s["queued"],
                    "cpu": s["cpu"],
                    "mem_mb": s["mem_mb"],
                    "workers": info.get("workers", 0),
                    "known": node_id in known,
                }
            )
        return nodes, None
    except Exception as e:  # noqa: BLE001
        log.warning("node_breakdown_failed", error=str(e))
        return [], str(e)
```

`governor.py` already defines `log = get_logger(__name__)` at module level
(verified), so `_node_breakdown` can use it as written — no import to add.

The inner import is not defensive guesswork: `app/api/v1/nodes.py` has
`from app.queue import keys` at module level (verified), so a module-level
`from app.api.v1.nodes import ...` here would be a genuine import cycle.

Then rewrite `current_load` so both return paths carry the key:

```python
async def current_load() -> dict:
    """Backing data for /system/load and the header indicator.

    The per-node breakdown is computed here rather than published in the
    leader's snapshot: baking it in would make it stale up to the snapshot TTL
    and absent entirely on the fallback path below, so the whole feature would
    vanish exactly when the leader lock lapses.
    """
    from app.db.redis_client import get_redis

    nodes, nodes_error = await _node_breakdown()

    snap = await read_snapshot(get_redis())
    if snap is not None:
        snap["nodes"] = nodes
        if nodes_error:
            snap["nodes_error"] = nodes_error
        return snap

    # No leader has published yet (or Redis is empty): report raw metrics so the
    # endpoint stays useful, and say the governor is not driving anything.
    vm = psutil.virtual_memory()
    load = {
        "state": AdmissionState.OPEN.value,
        "admitted_classes": allowed_classes(),
        "ramping": False,
        "cpu": {"percent": psutil.cpu_percent(interval=None), "budget": psutil.cpu_count()},
        "memory": {"percent": vm.percent, "available_bytes": vm.available},
        "disk": None,
        "governor_active": False,
        "nodes": nodes,
    }
    if nodes_error:
        load["nodes_error"] = nodes_error
    return load
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/test_governor_nodes.py -q
```

Expected: `5 passed`.

- [ ] **Step 5: Run the existing governor tests for regressions**

```bash
./backend/run-worktree-tests.sh tests/queue/test_governor.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/governor.py backend/tests/queue/test_governor_nodes.py
git commit -m "feat(api): break system load down by node on /system/load"
```

---

## Task 6: Backend full-suite check

**Files:** none — verification only.

- [ ] **Step 1: Run the whole backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: green. **Read the pass/fail count, not the exit code** — CLAUDE.md
is explicit that "green" means the count.

If DB-touching tests fail in a rotating, different-each-run pattern, that is
two test runs sharing one Mongo, not a bug in this change; re-run with no other
suite in flight. A run that dies with `EXIT=137` is host memory, not a
failure.

- [ ] **Step 2: Commit nothing if green**

No commit for this task. If something failed, fix it in a task-appropriate
commit before continuing.

---

## Task 7: Frontend types

**Files:**
- Modify: `frontend/src/api/types.ts`

- [ ] **Step 1: Add `queued_jobs` to `NodeInfo`**

In `frontend/src/api/types.ts`, in the `NodeInfo` interface, add the field
after `running_jobs`:

```typescript
export interface NodeInfo {
  node_id: string;
  workers: number;
  online_workers: number;
  running_jobs: number;
  queued_jobs: number;
  slots: number;
  online: boolean;
  reserved: {
    cpu: number;
    mem_mb: number;
    io_heavy: number;
  };
  hostname?: string;
  registered_at?: string | null;
  enrollment?: string;
}
```

`hostname`, `registered_at`, and `enrollment` are genuinely absent from the
current interface (verified) even though the endpoint has always returned
them. They are added here because Task 8's unknown-node rendering reads
`node.enrollment`, which would not typecheck otherwise. They are optional so
no existing construction site breaks.

- [ ] **Step 2: Add the `SystemLoad` node types**

Add above the `SystemLoad` interface:

```typescript
export interface SystemLoadNode {
  node_id: string;
  running: number;
  queued: number;
  cpu: number;
  mem_mb: number;
  workers: number;
  /** False when a ready queue exists for a node id that never enrolled. */
  known: boolean;
}
```

And inside `SystemLoad`, after `governor_active`:

```typescript
  nodes: SystemLoadNode[];
  nodes_error?: string;
```

- [ ] **Step 3: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors. If `tsc` reports that `queued_jobs` is missing on an
object literal somewhere, that is a test fixture or mock needing the field —
add it.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts
git commit -m "feat(frontend): type per-node queue statistics"
```

---

## Task 8: Queued column in the nodes settings table

**Files:**
- Modify: `frontend/src/components/SettingsNodes.tsx`

- [ ] **Step 1: Add the column header**

In `frontend/src/components/SettingsNodes.tsx`, the header row currently reads
`Node / Status / Workers / Running / Reserved CPU / Reserved RAM`. Insert
`Queued` between `Running` and `Reserved CPU`:

```tsx
                <th>Node</th>
                <th>Status</th>
                <th>Workers</th>
                <th>Running</th>
                <th>Queued</th>
                <th>Reserved CPU</th>
                <th>Reserved RAM</th>
```

- [ ] **Step 2: Add the cell**

In the row component, after the `running_jobs` cell, insert:

```tsx
      <td>{node.queued_jobs}</td>
```

- [ ] **Step 3: Render unknown nodes distinctly**

An orphaned node arrives with `enrollment === "unknown"` and zero workers. In
the same row component, replace the status cell with:

```tsx
      <td>
        {node.enrollment === "unknown" && node.workers === 0 ? (
          <span
            className="nodes-status unknown"
            title={`Jobs are queued for "${node.node_id}", but no node with that name has ever enrolled. Check the target node name used at launch.`}
          >
            Unknown
          </span>
        ) : (
          <span className={`nodes-status ${node.online ? "online" : ""}`}>
            {node.online ? "Online" : "Offline"}
          </span>
        )}
      </td>
```

- [ ] **Step 4: Style the unknown badge**

Find the stylesheet carrying `.nodes-status` (grep for it under
`frontend/src`) and add a rule alongside the existing `.nodes-status.online`,
matching whatever warning color the file already defines rather than
introducing a new hex value:

```css
.nodes-status.unknown {
  /* Reuse the file's existing warning token; do not invent a new color. */
  color: var(--warning, #b8860b);
}
```

- [ ] **Step 5: Typecheck and lint**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/SettingsNodes.tsx frontend/src
git commit -m "feat(ui): show per-node queue depth in the nodes table"
```

---

## Task 9: Verify in the browser

The frontend has no headless component tests and none is expected; this is the
verification step for anything UI-facing.

**Files:** none — verification only.

- [ ] **Step 1: Start the worktree stack**

From the worktree root:

```bash
./ops/worktree-up.sh
```

Serves the UI on **5273** and the API on **8100**, with its own database. Do
not use plain `docker compose` here — a hook blocks it, because it would
repoint the main 5173 stack at this worktree.

- [ ] **Step 2: Check the endpoints directly**

```bash
curl -s localhost:8100/api/v1/nodes | head -40
```

Expected: each entry has `queued_jobs`.

```bash
curl -s localhost:8100/api/v1/system/load | head -40
```

Expected: a `nodes` array, each entry with `node_id`, `running`, `queued`,
`cpu`, `mem_mb`, `workers`, `known`.

- [ ] **Step 3: Check the settings table**

Open `http://localhost:5273`, go to Settings → Nodes. Confirm the Queued
column renders and the existing columns are unchanged.

- [ ] **Step 4: Exercise the orphan path against real data**

CLAUDE.md is explicit that a rule checked only against hand-built fixtures is
undertested. Push a job id into a queue for a node that does not exist:

```bash
docker exec -i biopipe-issue-217-brainstorm-27a8c6-redis-1 redis-cli ZADD bp:q:ready:gpu-nodee 1 fake-job-id
```

If that container name is wrong, find it with `docker ps --format '{{.Names}}'
| grep redis`.

Reload Settings → Nodes. Expected: a `gpu-nodee` row with status **Unknown**,
Queued **1**, and the explanatory tooltip on hover.

- [ ] **Step 5: Clean up the test key**

```bash
docker exec -i biopipe-issue-217-brainstorm-27a8c6-redis-1 redis-cli DEL bp:q:ready:gpu-nodee
```

- [ ] **Step 6: Stop the stack**

```bash
./ops/worktree-up.sh --down
```

---

## Task 10: Open the PR

- [ ] **Step 1: Confirm the suite is green**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Read the count. Green is the precondition for pushing.

- [ ] **Step 2: Push**

```bash
git push -u origin HEAD
```

- [ ] **Step 3: Open the PR**

The title lands in the release notes verbatim:

```bash
gh pr create --base main --title "feat(api): report per-node queue depth and resource reservations" --body "$(cat <<'EOF'
`GET /system/load` reported only whole-machine totals and `GET /nodes` only
what workers were running, so "is gpu-node backed up?" was unanswerable from
the UI even though the per-node counters had been in Redis all along.

Both endpoints now report per-node queue depth and reservations, computed at
request time from a single Redis pipeline rather than from the governor's
published snapshot -- so the numbers survive the leader lock lapsing, which is
when someone is most likely to be looking.

Ready queues belonging to no enrolled node are surfaced and flagged rather
than filtered out: a job launched at a misspelled `target_node` used to sit in
a queue nobody drained, with nothing anywhere to explain why it never ran.

Closes #217

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Label the PR**

`.github/release.yml` categorizes by label, not by the title prefix; an
unlabelled PR lands under "Other changes".

```bash
gh pr edit --add-label "type:feature" --add-label "area:backend" --add-label "area:frontend"
```

- [ ] **Step 5: Report the PR URL and stop**

Do not merge. The user reviews and merges.

---

## Self-review notes

Checked against the spec:

- Shared helper, single pipeline, error-swallowing → Task 1
- Orphan discovery via `SCAN` → Task 2
- `/nodes` gains `queued_jobs`, reservations for offline nodes, orphans → Task 4
- `/system/load` gains `nodes` on **both** return paths, plus `nodes_error` → Task 5
- Frontend types and Queued column → Tasks 7, 8
- Redis-failure tests assert the failing direction → Tasks 1, 2, 5
- Browser verification including a real orphaned queue → Task 9

Two things this plan adds that the spec did not spell out:

- **Task 3**, extracting `enumerate_nodes()` before adding a second caller.
  The spec said the two endpoints share the enumeration; it did not say the
  extraction is its own behaviour-preserving commit. It is, so the refactor
  and the behaviour change stay separately reviewable.
- **The circular-import note** in Task 5: `app.api.v1.nodes` imports from
  `app.queue`, so `governor.py` must import it inside the function.
