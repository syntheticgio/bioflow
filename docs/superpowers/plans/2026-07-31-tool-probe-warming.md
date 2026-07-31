# Tool Probe Cache Warming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the 6-15s stall the first `/api/v1/pipelines/tools` request pays, and stop `uvicorn --reload` from re-paying it on every backend edit.

**Architecture:** Two layered pieces. A fire-and-forget `asyncio.create_task` in `lifespan` probes all fifteen tools in a thread before a user asks for them. Underneath that, a Redis-backed store lets a restart seed the probe caches from the previous process instead of re-probing. Persistence lives entirely in `lifespan`, where the code is already async -- the sync probe functions are untouched.

**Tech Stack:** Python 3.12, FastAPI, `redis.asyncio`, pytest / pytest-asyncio, `fakeredis.aioredis` for tests.

**Spec:** `docs/superpowers/specs/2026-07-31-tool-probe-warming-design.md`

---

## Background the engineer needs

**How probing works today.** `backend/app/pipelines/tools.py` has eighteen
`@lru_cache(maxsize=1)` functions (`fastp()`, `nanoplot()`, ...), each calling
`_probe(name, configured, version_args, timeout=None)`. `_probe` does
`shutil.which(configured)`, then `subprocess.run([resolved, *version_args])`,
and returns a frozen `Tool(name, path, version, error)` dataclass. `all_tools()`
calls fifteen of them in sequence. Nothing populates these caches at startup, so
the first request to hit the tool selector or `/help/software` pays the whole
cost -- 14.7s cold, of which NanoPlot alone is 12.0s because it imports pandas,
scipy and plotly before printing one line.

**Why not parallelise.** Concurrency caps the total at the slowest single probe
(NanoPlot's 12s), buying ~3s of the 15 and adding a thread pool. The spec
rejects it, and so does this plan.

**Why persistence is at the lifespan layer.** The Redis client is
`redis.asyncio`, but every probe function is sync and called from sync contexts.
A read-through cache underneath `lru_cache` would need
`run_coroutine_threadsafe` from a worker thread. Instead the warm task *is* the
cache load: it reads Redis, seeds what matches, probes the rest, writes back.

**How seeding works.** `lru_cache` has no public API for inserting a value, so
`_probe` gains a module-level override dict, `_seeded`, keyed by tool name and
holding `(fingerprint, Tool)`. `_probe` consults it after `shutil.which`
resolves the path and before it shells out. Because the check compares
fingerprints, a seeded entry can never be served for a changed binary.

**The fingerprint** is `f"{path}:{mtime_ns}:{size}:{sha256}"` of the resolved
binary. An upgraded tool produces a different fingerprint and is re-probed. This
is a correctness concern, not a performance one: versions end up in methods
sections.

Revised during Task 1, after the originally-planned `path:mtime_ns:size` proved
both flaky and insufficient. Two writes to the same path can land in one
`mtime_ns` tick, and four of the fifteen tools (`fastqc`, `bowtie2`, `hisat2`,
`cutadapt`) are interpreter wrapper scripts rather than binaries -- so each
component covers a failure the others miss: mtime catches a reinstall whose
bytes are unchanged, the hash catches an in-place same-mtime replacement, and
the path keeps two tools sharing a binary from colliding. Hashing costs a few ms
(4.15 MB across all tools, against a 15s probe).

Residual, documented limitation: for a wrapper script this fingerprints the
wrapper, not the program it dispatches to. A payload-only upgrade leaving the
wrapper byte- and mtime-identical goes undetected; the 24h TTL is the backstop.

**Test conventions in this repo.** `backend/tests/pipelines/test_tools.py` tests
probes by writing real `#!/bin/sh` scripts into `tmp_path` and prepending it to
`PATH` -- follow that, do not mock `subprocess`. Redis is tested with
`fakeredis.aioredis.FakeRedis(decode_responses=True)`, as in
`backend/tests/queue/conftest.py`. Run tests inside the container:
`docker compose exec api python -m pytest ... ` from the **main repo root**, per
CLAUDE.md, never from a worktree.

**A trap CLAUDE.md calls out explicitly.** The image ships most tools as
installed, so a test asserting a cache "works" passes whether or not its patch
took effect. Every test below asserts the direction that *fails* when the seam
breaks.

---

## File Structure

- **Modify** `backend/app/pipelines/tools.py` -- fingerprinting, the `_seeded`
  override dict, `_probe` consulting it, `reset_cache()` clearing it. This file
  is already ~930 lines, most of it the static `TOOL_META` descriptions; the
  additions here are small and belong beside `_probe`, so no split is proposed.
- **Create** `backend/app/pipelines/tool_cache.py` -- the async Redis layer:
  serialise/deserialise a `Tool`, read the stored set, write it back, invalidate.
  Separate file because it is the only async code in the pipelines package and
  its dependency (Redis) is one `tools.py` deliberately does not have.
- **Modify** `backend/app/main.py` -- the warm task in `lifespan`.
- **Create** `backend/tests/pipelines/test_tool_cache.py` -- the Redis layer and
  fingerprint behaviour.
- **Modify** `backend/tests/pipelines/test_tools.py` -- seeding and
  `reset_cache()` behaviour.
- **Create** `backend/tests/api/test_startup_warm.py` -- that the warm task does
  not block startup and that its failure is logged, not raised.

---

## Task 1: Fingerprint a resolved binary

**Files:**
- Modify: `backend/app/pipelines/tools.py`
- Test: `backend/tests/pipelines/test_tools.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_tools.py`, at the end of the file:

```python
class TestFingerprint:
    def test_fingerprint_changes_when_the_binary_changes(self, tmp_path):
        """The fingerprint is what keeps a stale version out of a methods
        section: an upgraded tool must not be served from cache."""
        binary = tmp_path / "sometool"
        binary.write_text("#!/bin/sh\necho 'sometool 1.0.0'\n")
        binary.chmod(0o755)

        before = tools._fingerprint(str(binary))

        binary.write_text("#!/bin/sh\necho 'sometool 2.0.0'\n")
        binary.chmod(0o755)

        assert tools._fingerprint(str(binary)) != before

    def test_fingerprint_is_stable_for_an_unchanged_binary(self, tmp_path):
        binary = tmp_path / "sometool"
        binary.write_text("#!/bin/sh\necho 'sometool 1.0.0'\n")
        binary.chmod(0o755)

        assert tools._fingerprint(str(binary)) == tools._fingerprint(str(binary))

    def test_fingerprint_of_a_missing_path_is_none(self):
        """A tool `which` cannot resolve has nothing to fingerprint, and must
        always be probed rather than served from a cache entry."""
        assert tools._fingerprint("/definitely/not/here/xyz") is None

    def test_fingerprint_of_none_is_none(self):
        assert tools._fingerprint(None) is None
```

- [ ] **Step 2: Run the test to verify it fails**

From the main repo root:

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools.py::TestFingerprint -v
```

Expected: FAIL with `AttributeError: module 'app.pipelines.tools' has no attribute '_fingerprint'`

- [ ] **Step 3: Write the implementation**

> **Superseded during execution.** The implementation below was the original
> plan; it proved flaky (two writes in one `mtime_ns` tick) and insufficient
> (wrapper scripts). The shipped version combines stat metadata with a SHA-256
> content hash -- see the fingerprint note in the background section above, and
> `_fingerprint` in `backend/app/pipelines/tools.py` for what actually landed.
> The test class below is unchanged and still passes, plus one case asserting
> that identical content at different paths fingerprints differently.

In `backend/app/pipelines/tools.py`, add `import os` and `import hashlib` to the
imports at the top (alongside the existing `import re`, `import shutil`,
`import subprocess`), then add a `_fingerprint` function directly below
`_clean_version` that returns `None` for a `None` or unstattable path, and
otherwise `f"{path}:{st.st_mtime_ns}:{st.st_size}:{sha256_of_contents}"`,
streaming the hash in 1MB chunks and catching `OSError`.

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools.py::TestFingerprint -v
```

Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat: fingerprint a resolved binary for probe caching"
```

---

## Task 2: Let a probe be seeded from outside

**Files:**
- Modify: `backend/app/pipelines/tools.py`
- Test: `backend/tests/pipelines/test_tools.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_tools.py`, at the end of the file:

```python
class TestSeeding:
    def test_a_seeded_probe_does_not_shell_out(self, tmp_path, monkeypatch):
        """The whole point: a seeded entry must skip the subprocess. Asserted
        by seeding a version the script does not print -- if the probe ran, it
        would return 1.0.0 instead."""
        script = tmp_path / "seededtool"
        script.write_text("#!/bin/sh\necho 'seededtool 1.0.0'\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        resolved = str(script)
        tools.seed(
            "seededtool",
            tools._fingerprint(resolved),
            tools.Tool(name="seededtool", path=resolved, version="9.9.9"),
        )

        tool = tools._probe("seededtool", "seededtool", ["--version"])
        assert tool.version == "9.9.9"

    def test_a_stale_fingerprint_forces_a_reprobe(self, tmp_path, monkeypatch):
        """The correctness case. An upgraded binary must be re-probed, not
        served from a seed describing the old one."""
        script = tmp_path / "staletool"
        script.write_text("#!/bin/sh\necho 'staletool 1.0.0'\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        tools.seed(
            "staletool",
            "a-fingerprint-that-does-not-match",
            tools.Tool(name="staletool", path=str(script), version="9.9.9"),
        )

        tool = tools._probe("staletool", "staletool", ["--version"])
        assert tool.version == "1.0.0"

    def test_a_seed_for_a_missing_binary_is_ignored(self):
        """`which` failing short-circuits before the seed is consulted: a tool
        that is no longer installed must report unavailable, not report the
        version it had when it was."""
        tools.seed(
            "gonetool",
            "some-fingerprint",
            tools.Tool(name="gonetool", path="/was/here", version="9.9.9"),
        )

        tool = tools._probe("gonetool", "definitely-not-a-real-binary-xyz", ["--version"])
        assert not tool.available
        assert "not found on PATH" in tool.error

    def test_reset_cache_clears_seeds(self, tmp_path, monkeypatch):
        """Otherwise a test or a runtime config change clears the lru_caches
        and immediately repopulates them from the values it meant to discard."""
        script = tmp_path / "clearedtool"
        script.write_text("#!/bin/sh\necho 'clearedtool 1.0.0'\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        resolved = str(script)
        tools.seed(
            "clearedtool",
            tools._fingerprint(resolved),
            tools.Tool(name="clearedtool", path=resolved, version="9.9.9"),
        )
        tools.reset_cache()

        tool = tools._probe("clearedtool", "clearedtool", ["--version"])
        assert tool.version == "1.0.0"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools.py::TestSeeding -v
```

Expected: FAIL with `AttributeError: module 'app.pipelines.tools' has no attribute 'seed'`

- [ ] **Step 3: Write the implementation**

In `backend/app/pipelines/tools.py`, add this above `_probe`:

```python
# Probe results supplied from outside, keyed by tool name and holding the
# fingerprint of the binary they describe. Populated at startup from Redis (see
# `tool_cache.py`) so a restart does not re-pay the probe cost -- NanoPlot alone
# is 12s of it.
#
# Keyed by fingerprint rather than trusted outright: an entry describing a
# binary that has since been upgraded must be ignored, not served.
_seeded: dict[str, tuple[str, Tool]] = {}


def seed(name: str, fingerprint: str | None, tool: Tool) -> None:
    """Offer a previously-probed result for `name`.

    Ignored at use time unless the binary still fingerprints identically, so a
    caller cannot force a stale version into the cache.
    """
    if fingerprint is None:
        return
    _seeded[name] = (fingerprint, tool)
```

Then in `_probe`, immediately after the `if resolved is None:` block returns and
before the `try:` that calls `subprocess.run`, insert:

```python
    seeded = _seeded.get(name)
    if seeded is not None and seeded[0] == _fingerprint(resolved):
        return seeded[1]
```

Finally, in `reset_cache()`, add this as the first line of the body (above
`fastp.cache_clear()`):

```python
    _seeded.clear()
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools.py -v
```

Expected: PASS. The whole file, not just the new class -- `_probe` changed, so
the existing probe tests are the regression check.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat: allow a probe result to be seeded from outside"
```

---

## Task 3: Serialise a Tool to and from Redis

**Files:**
- Create: `backend/app/pipelines/tool_cache.py`
- Create: `backend/tests/pipelines/test_tool_cache.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_tool_cache.py`:

```python
"""The Redis layer behind probe warming.

Uses fakeredis rather than a live server, matching tests/queue/conftest.py.
"""

import fakeredis.aioredis
import pytest

from app.pipelines import tool_cache, tools


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


class TestRoundTrip:
    async def test_a_written_entry_reads_back_identically(self, redis):
        entry = tools.Tool(name="fastp", path="/usr/bin/fastp", version="0.24.0")

        await tool_cache.write(redis, {"fastp": ("fp-1", entry)})
        loaded = await tool_cache.read(redis)

        assert loaded == {"fastp": ("fp-1", entry)}

    async def test_an_error_tool_round_trips(self, redis):
        """The unavailable case carries the message the launch dialog shows,
        so it must survive the round trip too."""
        entry = tools.Tool(
            name="clair3", path=None, version=None, error="not found on PATH"
        )

        await tool_cache.write(redis, {"clair3": ("c3-1", entry)})
        loaded = await tool_cache.read(redis)

        assert loaded["clair3"][1].error == "not found on PATH"
        assert not loaded["clair3"][1].available

    async def test_reading_an_empty_cache_returns_empty(self, redis):
        assert await tool_cache.read(redis) == {}


class TestFailurePosture:
    async def test_corrupt_json_is_ignored_not_raised(self, redis):
        """A cache that can fail the request is worse than no cache."""
        await redis.set(tool_cache.CACHE_KEY, "{not json at all")

        assert await tool_cache.read(redis) == {}

    async def test_a_malformed_entry_is_skipped_but_others_survive(self, redis):
        """One bad record must not discard the other fourteen."""
        await redis.set(
            tool_cache.CACHE_KEY,
            '{"fastp": {"fingerprint": "fp-1", "tool": {"name": "fastp", '
            '"path": "/usr/bin/fastp", "version": "0.24.0", "error": null}}, '
            '"broken": {"fingerprint": "b-1"}}',
        )

        loaded = await tool_cache.read(redis)

        assert "fastp" in loaded
        assert "broken" not in loaded

    async def test_an_unreachable_redis_reads_as_empty(self):
        """Redis being down must degrade to 'probe normally', never to an
        error -- the same rule the governor applies to the mount sentinel."""

        class Broken:
            async def get(self, key):
                raise ConnectionError("redis is down")

        assert await tool_cache.read(Broken()) == {}

    async def test_an_unreachable_redis_does_not_raise_on_write(self):
        class Broken:
            async def set(self, *a, **kw):
                raise ConnectionError("redis is down")

        await tool_cache.write(Broken(), {})  # must not raise
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tool_cache.py -v
```

Expected: FAIL with `ImportError: cannot import name 'tool_cache' from 'app.pipelines'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/tool_cache.py`:

```python
"""Redis-backed persistence for tool probe results.

Probing fifteen tools costs ~15s cold, and `lru_cache` lives in the process --
so `uvicorn --reload`, which is how this app runs, discards it on every backend
edit. Persisting the results means a restart seeds the caches instead of
re-probing.

Every function here degrades to "no cache" on any Redis failure. A probe cache
that can fail a request is worse than no probe cache: the result is only ever an
optimisation, and the caller can always fall back to shelling out.

Async because the Redis client is; `tools.py` stays sync and knows nothing about
Redis. The seam between them is `tools.seed`, called from the startup warm task.
"""

import json
from typing import Any

from app.logging import get_logger
from app.pipelines.tools import Tool

log = get_logger(__name__)

CACHE_KEY = "bp:tools:probes"

# A backstop, not the primary invalidation -- that is the fingerprint. This
# only bounds how long a fingerprint collision could persist.
CACHE_TTL_SECONDS = 24 * 60 * 60


async def read(client: Any) -> dict[str, tuple[str, Tool]]:
    """Stored probe results, by tool name. Empty on any failure."""
    try:
        raw = await client.get(CACHE_KEY)
    except Exception as e:  # noqa: BLE001 - a cache miss is always acceptable
        log.warning("tool_cache_read_failed", error=str(e))
        return {}

    if not raw:
        return {}

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as e:
        log.warning("tool_cache_corrupt", error=str(e))
        return {}

    out: dict[str, tuple[str, Tool]] = {}
    for name, record in (payload or {}).items():
        # Per-entry rather than all-or-nothing: one malformed record should not
        # discard the other fourteen.
        try:
            out[name] = (record["fingerprint"], Tool(**record["tool"]))
        except (KeyError, TypeError) as e:
            log.warning("tool_cache_entry_skipped", tool=name, error=str(e))
    return out


async def write(client: Any, entries: dict[str, tuple[str, Tool]]) -> None:
    """Store probe results. Silent on any failure."""
    payload = {
        name: {
            "fingerprint": fingerprint,
            "tool": {
                "name": tool.name,
                "path": tool.path,
                "version": tool.version,
                "error": tool.error,
            },
        }
        for name, (fingerprint, tool) in entries.items()
    }
    try:
        await client.set(CACHE_KEY, json.dumps(payload), ex=CACHE_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001 - failing to cache is not an error
        log.warning("tool_cache_write_failed", error=str(e))


async def invalidate(client: Any) -> None:
    """Drop the stored results. For a runtime config change."""
    try:
        await client.delete(CACHE_KEY)
    except Exception as e:  # noqa: BLE001
        log.warning("tool_cache_invalidate_failed", error=str(e))
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tool_cache.py -v
```

Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/tool_cache.py backend/tests/pipelines/test_tool_cache.py
git commit -m "feat: persist tool probe results in Redis"
```

---

## Task 4: Warm the caches, seeding from Redis

**Files:**
- Modify: `backend/app/pipelines/tool_cache.py`
- Modify: `backend/tests/pipelines/test_tool_cache.py`

This is the piece that ties Tasks 1-3 together: read Redis, seed what still
matches, probe the rest in a thread, write everything back.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_tool_cache.py`:

```python
class TestWarm:
    @pytest.fixture(autouse=True)
    def clear_cache(self):
        tools.reset_cache()
        yield
        tools.reset_cache()

    async def test_warm_populates_redis_from_a_cold_start(self, redis):
        await tool_cache.warm(redis)

        stored = await tool_cache.read(redis)
        assert stored, "warm should have written probe results"
        # Every tool all_tools() reports should be stored.
        assert {t.name for t in tools.all_tools()} <= set(stored)

    async def test_warm_seeds_probes_so_they_do_not_shell_out(self, redis, tmp_path, monkeypatch):
        """The direction that fails when the seam breaks: seed a version the
        binary does not print, then assert the probe returns it."""
        script = tmp_path / "warmtool"
        script.write_text("#!/bin/sh\necho 'warmtool 1.0.0'\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        resolved = str(script)
        await tool_cache.write(
            redis,
            {
                "warmtool": (
                    tools._fingerprint(resolved),
                    tools.Tool(name="warmtool", path=resolved, version="9.9.9"),
                )
            },
        )

        await tool_cache.warm(redis)

        assert tools._probe("warmtool", "warmtool", ["--version"]).version == "9.9.9"

    async def test_warm_survives_an_unreachable_redis(self):
        """A total Redis failure must leave behaviour exactly as it is today,
        not raise into the startup path."""

        class Broken:
            async def get(self, key):
                raise ConnectionError("redis is down")

            async def set(self, *a, **kw):
                raise ConnectionError("redis is down")

        await tool_cache.warm(Broken())  # must not raise
```

Add `import os` to that test file's imports.

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tool_cache.py::TestWarm -v
```

Expected: FAIL with `AttributeError: module 'app.pipelines.tool_cache' has no attribute 'warm'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/tool_cache.py`. Add `import asyncio` and `import
shutil` to its imports, and `from app.config import settings` plus `from
app.pipelines import tools` alongside the existing `Tool` import:

```python
async def warm(client: Any) -> None:
    """Populate the probe caches, using Redis to skip what has not changed.

    Seeds first, then probes. Probing runs in a thread: `all_tools()` is sync
    and spawns fifteen subprocesses, so calling it on the event loop would
    block every request for the ~15s it takes -- turning a latency problem into
    an outage.
    """
    stored = await read(client)
    for name, (fingerprint, tool) in stored.items():
        tools.seed(name, fingerprint, tool)

    probed = await asyncio.to_thread(tools.all_tools)

    entries: dict[str, tuple[str, Tool]] = {}
    for tool in probed:
        fingerprint = tools._fingerprint(tool.path)
        if fingerprint is not None:
            entries[tool.name] = (fingerprint, tool)

    await write(client, entries)
    log.info("tool_cache_warmed", tools=len(entries), seeded=len(stored))
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tool_cache.py -v
```

Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/tool_cache.py backend/tests/pipelines/test_tool_cache.py
git commit -m "feat: warm the probe caches, seeding from Redis"
```

---

## Task 5: Run the warm at startup without blocking it

**Files:**
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/test_startup_warm.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_startup_warm.py`:

```python
"""Startup must not wait on tool probing.

The probe costs ~15s cold. Moving it off the request path is the entire point
of the feature; moving it *into* startup instead would be a regression, so
these tests pin the fire-and-forget shape.
"""

import asyncio

import pytest

from app import main


class TestStartupWarm:
    async def test_lifespan_does_not_wait_for_the_warm(self, monkeypatch):
        """A slow warm must not delay the app becoming ready."""
        started = asyncio.Event()

        async def slow_warm(client):
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(main.tool_cache, "warm", slow_warm)
        monkeypatch.setattr(main, "initialize_home", lambda: None)
        monkeypatch.setattr(main, "connect_to_mongo", _noop)
        monkeypatch.setattr(main, "connect_to_redis", _noop)
        monkeypatch.setattr(main, "close_mongo", _noop)
        monkeypatch.setattr(main, "close_redis", _noop)
        monkeypatch.setattr(main, "load_handlers", lambda: None)
        monkeypatch.setattr(main, "get_redis", lambda: object())

        async with main.lifespan(None):
            # If lifespan awaited the warm, this line is unreachable for 30s.
            await asyncio.wait_for(started.wait(), timeout=5)

    async def test_a_failing_warm_does_not_break_startup(self, monkeypatch):
        """A probe failure must be logged, not propagated -- the app still
        serves, tools just report lazily as they do today."""
        failed = asyncio.Event()

        async def broken_warm(client):
            failed.set()
            raise RuntimeError("probing exploded")

        monkeypatch.setattr(main.tool_cache, "warm", broken_warm)
        monkeypatch.setattr(main, "initialize_home", lambda: None)
        monkeypatch.setattr(main, "connect_to_mongo", _noop)
        monkeypatch.setattr(main, "connect_to_redis", _noop)
        monkeypatch.setattr(main, "close_mongo", _noop)
        monkeypatch.setattr(main, "close_redis", _noop)
        monkeypatch.setattr(main, "load_handlers", lambda: None)
        monkeypatch.setattr(main, "get_redis", lambda: object())

        async with main.lifespan(None):
            await asyncio.wait_for(failed.wait(), timeout=5)
            # Let the task run its exception handler.
            await asyncio.sleep(0.1)


async def _noop(*args, **kwargs):
    return None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/api/test_startup_warm.py -v
```

Expected: FAIL with `AttributeError: module 'app.main' has no attribute 'tool_cache'`

- [ ] **Step 3: Write the implementation**

In `backend/app/main.py`, add to the imports:

```python
import asyncio

from app.db.redis_client import close_redis, connect_to_redis, get_redis
from app.pipelines import tool_cache
```

(the `redis_client` line replaces the existing import, adding `get_redis`).

Then add this function above `lifespan`:

```python
async def _warm_tools() -> None:
    """Probe every tool in the background, so a user does not pay for it.

    Never awaited by `lifespan` and deliberately not gating `/readyz`: a
    container that reports unready while probing is a worse experience than the
    stall this removes, and a probe that fails should not keep the app from
    serving. Exceptions are caught here rather than left to surface at
    garbage-collection time as "task exception was never retrieved".
    """
    try:
        await tool_cache.warm(get_redis())
    except Exception as e:  # noqa: BLE001 - a warm failure is never fatal
        log.warning("tool_warm_failed", error=str(e))
```

And in `lifespan`, directly after the `load_handlers()` call and its comment,
before `log.info("started")`:

```python
    # Fire-and-forget: fifteen `<tool> --version` spawns, ~15s cold, of which
    # NanoPlot is 12s. Held in a local so the task is not garbage-collected
    # mid-flight, which would cancel it silently.
    warm_task = asyncio.create_task(_warm_tools())
```

Then in the `finally:` block, before `await close_redis()`, add:

```python
        warm_task.cancel()
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/api/test_startup_warm.py -v
```

Expected: PASS, 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/tests/api/test_startup_warm.py
git commit -m "feat: warm the tool caches at startup, off the request path"
```

---

## Task 6: Verify against the running app

Per CLAUDE.md, a green suite is not the verification step -- the tests feed the
rules hand-built objects. This task measures the real thing.

**Files:** none modified.

- [ ] **Step 1: Run the full backend suite**

From the main repo root:

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass. Investigate any failure before continuing -- `_probe` and
`lifespan` both changed, so a break here is a real regression, not flakiness.

- [ ] **Step 2: Rebuild the stack against this branch's code**

```bash
docker compose up -d --build api web worker
```

- [ ] **Step 3: Confirm the stack is serving the right tree**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: no path contains `.claude/worktrees/`. If one does, the rebuild ran
from a worktree -- re-run Step 2 from `/Users/syntheticgio/Programming/local-bio-pipeliner`.

- [ ] **Step 4: Confirm the warm ran at startup**

```bash
docker compose logs api | grep -E "tool_cache_warmed|tool_warm_failed"
```

Expected: a `tool_cache_warmed` line reporting ~15 tools. On this first run
`seeded=0`, since Redis was empty.

- [ ] **Step 5: Measure the endpoint, which is the actual fix**

```bash
docker compose exec api python -c "
import time, urllib.request
t = time.monotonic()
urllib.request.urlopen('http://localhost:8000/api/v1/pipelines/tools').read()
print(f'{time.monotonic() - t:.2f}s')
"
```

Expected: well under 1s. Before this change it was 6-15s. If it is still
seconds, the warm task did not finish before the request -- check Step 4's log
line appeared first.

- [ ] **Step 6: Confirm the reload case, which is the second half of the fix**

```bash
docker compose restart api && sleep 15 && docker compose logs --tail=50 api | grep tool_cache_warmed
```

Expected: `seeded=15` (or however many tools resolved), proving the second
startup read Redis rather than re-probing.

- [ ] **Step 7: Confirm the tool list is still correct**

```bash
docker compose exec api python -c "
from app.pipelines.tools import all_tools
for t in all_tools():
    print(f'{t.name:16} {t.version or t.error}')
"
```

Expected: the same versions as before the change, NanoPlot included with a real
version rather than an error. A tool showing a version it did not have before,
or an error it did not have before, means the cache is serving something wrong.

- [ ] **Step 8: Commit nothing, or fix and commit**

If Steps 1-7 all pass, there is nothing to commit. If any failed, fix it, re-run
the affected step, and commit the fix.

---

## Task 7: Record the outcome in docs/TODO.md

**Files:**
- Modify: `docs/TODO.md`

- [ ] **Step 1: Replace the entry**

The first entry in `docs/TODO.md` is `## The first /pipelines/tools request
stalls 6-15s on NanoPlot`. Replace its heading and add a resolution note
immediately below, matching the style of the `— FIXED` entry further down the
file:

```markdown
## The first `/pipelines/tools` request stalls 6-15s on NanoPlot — FIXED

Fixed on 2026-07-31. `lifespan` now starts a fire-and-forget task
(`_warm_tools` in `backend/app/main.py`) that probes every tool in a thread
before a user asks, and `backend/app/pipelines/tool_cache.py` persists the
results in Redis keyed by each binary's path+mtime+size -- so `uvicorn
--reload`, which discarded all eighteen `lru_cache`s on every backend edit,
now re-seeds them instead of re-probing.

Measured after the change: endpoint under 1s cold, against 6-15s before.

Options 2 (skip NanoPlot's `--version`) and 3's file-based variant were not
taken. The reasoning for both, and the measurements behind the original
diagnosis, are kept below because they still describe the shape of the problem
if another heavy-import tool is added.
```

Leave the rest of the entry's body in place -- the measurement table and the
"parallelism is the wrong fix" reasoning stay useful.

- [ ] **Step 2: Commit**

```bash
git add docs/TODO.md
git commit -m "docs: mark the tool probe stall fixed"
```

---

## Self-review notes

Checked against the spec:

- Piece 1 (background warm, in a thread, not gating readyz, exceptions logged)
  -> Task 5, with all four constraints asserted in `test_startup_warm.py`.
- Piece 2 (Redis persistence, lifespan layer only, probe functions untouched)
  -> Tasks 3-4. `tools.py` gains only `_fingerprint`, `_seeded` and `seed`; no
  probe function signature changes.
- Fingerprint as path+mtime+size -> Task 1.
- Seeding mechanism via override dict consulted inside `_probe` -> Task 2.
- TTL backstop -> `CACHE_TTL_SECONDS` in Task 3.
- Failure posture (degrade to probing, never error) -> Task 3's
  `TestFailurePosture`, Task 4's `test_warm_survives_an_unreachable_redis`,
  Task 5's `test_a_failing_warm_does_not_break_startup`.
- `reset_cache()` stays sync, clears seeds; `invalidate` is separate and async
  -> Task 2 Step 3, Task 3's `invalidate`.
- Every test named in the spec's Testing section has a task.

Naming is consistent across tasks: `_fingerprint`, `seed`, `_seeded`,
`tool_cache.read/write/warm/invalidate`, `CACHE_KEY`, `CACHE_TTL_SECONDS`,
`_warm_tools`.

One deliberate gap: `tool_cache.invalidate` is implemented and tested for
failure tolerance but has no production caller, since nothing currently changes
tool config at runtime. It exists because `reset_cache()`'s sync/async split
would otherwise be undocumented in code. If that bothers a reviewer, deleting it
is safe.
