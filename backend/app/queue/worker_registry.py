"""Reading the worker heartbeat hash, minus the entries nobody cleaned up.

`bp:workers` is written by `Worker._register_worker` on every heartbeat and
deleted by `Worker.stop` on a *graceful* shutdown. Nothing deletes it
otherwise, and the hash has no TTL -- so a worker killed by `docker compose
down`, a container restart, an image rebuild, or an OOM leaves its last
heartbeat in Redis permanently.

Those corpses are not harmless. `/api/v1/nodes` counts them as workers, so a
node with two live workers reports eight; and an entry old enough to predate
the `node_id` field in the payload used to be bucketed under a synthesized
node literally named "unknown", which is what #451 was looking at. Six of the
eight `primary` workers and all eighteen `unknown` ones on the reporting
stack were entries last seen eleven days earlier.

So every reader goes through `live_workers()`, which drops what has rotted
past `_STALE_AFTER_SECONDS` and deletes it from the hash on the way past.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from app.db.redis_client import get_redis
from app.logging import get_logger
from app.queue import keys

log = get_logger(__name__)

# A heartbeat older than this is a dead worker's leftovers, not an offline
# worker. Deliberately far above the 60s the nodes API uses to decide
# "offline": a node that is genuinely down but real should keep showing as
# offline for a good long while, and only entries with no plausible owner
# left should disappear. A worker that comes back re-registers under the same
# worker_id on its next heartbeat, so reaping early costs nothing but a row
# blinking out for one poll.
_STALE_AFTER_SECONDS = 24 * 60 * 60


def _parse_last_seen(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    # Pre-#451 payloads were always written with a tz-aware `datetime.now(UTC)`,
    # but a naive string here would make every comparison below raise.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def live_workers() -> Iterator[tuple[str, dict]]:
    """Every worker entry that is not a dead worker's leftovers.

    Yields `(worker_id, payload)`. Entries that fail to parse, that carry no
    usable `last_seen`, or whose `last_seen` is older than
    `_STALE_AFTER_SECONDS` are dropped from the result *and* deleted from the
    hash, so the reaping is a side effect of the read rather than a
    background job that has to be scheduled and can fall over on its own.

    Degrades to an empty result rather than raising: this feeds a status page
    someone opened because something already looked wrong.
    """
    try:
        raw = await get_redis().hgetall(keys.WORKERS)
    except Exception as e:  # noqa: BLE001
        log.warning("worker_registry_read_failed", error=str(e))
        return iter([])

    cutoff = datetime.now(UTC) - timedelta(seconds=_STALE_AFTER_SECONDS)
    live: list[tuple[str, dict]] = []
    stale: list[str] = []

    for worker_id, blob in (raw or {}).items():
        try:
            payload = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            stale.append(worker_id)
            continue
        if not isinstance(payload, dict):
            stale.append(worker_id)
            continue
        last_seen = _parse_last_seen(payload.get("last_seen"))
        if last_seen is None or last_seen < cutoff:
            stale.append(worker_id)
            continue
        live.append((worker_id, payload))

    if stale:
        try:
            await get_redis().hdel(keys.WORKERS, *stale)
            log.info("reaped_stale_workers", count=len(stale))
        except Exception as e:  # noqa: BLE001
            # Reporting the live set correctly matters more than the cleanup
            # landing; the next read tries again.
            log.warning("worker_registry_reap_failed", error=str(e))

    return iter(live)
