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
