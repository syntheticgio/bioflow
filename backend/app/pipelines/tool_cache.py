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

import asyncio
import contextlib
import json
from typing import Any

from app.logging import get_logger
from app.pipelines import tools
from app.pipelines.tools import Delivery, Tool

log = get_logger(__name__)

CACHE_KEY = "bp:tools:probes"

# A backstop, not the primary invalidation -- that is the fingerprint. This
# only bounds how long a fingerprint collision could persist.
CACHE_TTL_SECONDS = 24 * 60 * 60

# Tools whose availability is not a property of a binary, so persisting their
# probe result by binary fingerprint would be wrong rather than merely
# unnecessary. Generalized from a DeepVariant-only set: every ON_DEMAND_IMAGE
# tool has the same shape of problem DeepVariant does. Its `Tool.path` is the
# *docker client's* path -- there is no DeepVariant binary -- and that path
# fingerprints successfully, so without this exclusion its availability would
# get cached against the identity of the docker client and would not change
# when the image is pulled or removed. Excluded here instead of made
# unfingerprintable, since the fingerprint is otherwise a correct, reusable
# idea for every BUNDLED tool.
#
# Derived from TOOL_META rather than hand-listed, so a tool that moves to
# ON_DEMAND_IMAGE later (Clair3, per the delivery plan's task 8) is excluded
# the moment its `ToolMeta.delivery` changes, with no second edit here to
# forget.
NOT_FINGERPRINTABLE = {
    name for name, meta in tools.TOOL_META.items() if meta.delivery is Delivery.ON_DEMAND_IMAGE
}

# Cross-process invalidation for exactly the tools above.
#
# The fingerprint-based invalidation this module otherwise relies on cannot
# reach an ON_DEMAND_IMAGE tool -- there is nothing to fingerprint -- so its
# probe result lives only in each process's `lru_cache`, keyed by nothing that
# changes when the image is pulled or removed. `api` and every `worker`
# replica are separate processes with separate caches: an install performed by
# a worker (task 4) does not clear the API's view, so a completed pull can
# leave the API still reporting "not installed" until it happens to restart.
# That reads as a broken Install button, and no single-process test can show
# it -- verify this one against the running stack, not only in a test file.
#
# A pub/sub channel rather than another poll loop: installs are rare and
# should invalidate promptly, not on the next tick of a timer nobody has a
# reason to tune.
INVALIDATE_CHANNEL = "bp:tools:invalidate"


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
        if tool.name in NOT_FINGERPRINTABLE:
            continue
        fingerprint = tools._fingerprint(tool.path)
        if fingerprint is not None:
            entries[tool.name] = (fingerprint, tool)

    await write(client, entries)
    log.info("tool_cache_warmed", tools=len(entries), seeded=len(stored))


async def publish_invalidation(client: Any, tool_name: str) -> None:
    """Tell every process's `lru_cache` to forget `tool_name`.

    Called at the end of a successful install or uninstall (task 4). Same
    failure discipline as everything else here: a missed publish means a
    stale badge until the process next restarts, which is a worse UI moment
    than this function, not a worse outcome than raising and failing the
    install that already succeeded.
    """
    try:
        await client.publish(INVALIDATE_CHANNEL, tool_name)
    except Exception as e:  # noqa: BLE001 - a missed invalidation is not a failed install
        log.warning("tool_cache_invalidation_publish_failed", tool=tool_name, error=str(e))


async def listen_for_invalidations(client: Any) -> None:
    """Run forever, clearing the process's probe cache whenever another
    process announces a tool's install state changed.

    Meant to run as a background task for the lifetime of the process --
    `app.main`'s `lifespan` and `app.worker_main`'s `main` both start one
    alongside their other startup work, and both cancel it the same way they
    cancel their other background loops. A subscriber that exits on the first
    Redis hiccup would silently stop watching for the rest of the process's
    life, so a connection failure is logged and retried rather than left to
    end the loop -- the outer `while True` is the retry, not a bug.

    Every tool's cache is cleared on any message, not only the named tool's:
    `tools.reset_cache()` already exists, clearing all twenty-six probes is
    cheap (they are re-probed lazily, one at a time, on next use), and there
    is no name -> probe-function registry to look up a single tool's
    `.cache_clear()` by string -- building one to shave a rare, cheap
    operation down to one tool is not worth the second place a probe
    function's name could drift from its `TOOL_META` key.
    """
    while True:
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(INVALIDATE_CHANNEL)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                tool_name = (
                    message["data"].decode()
                    if isinstance(message["data"], bytes)
                    else message["data"]
                )
                tools.reset_cache()
                log.info("tool_cache_invalidated", tool=tool_name)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - keep listening across a Redis blip
            log.warning("tool_cache_invalidation_listen_failed", error=str(e))
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(5)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(INVALIDATE_CHANNEL)
                await pubsub.aclose()
