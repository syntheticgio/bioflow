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
import json
from typing import Any

from app.logging import get_logger
from app.pipelines import tools
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
