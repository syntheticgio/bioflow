"""Compute node status: which machines are connected and what they are doing."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter

from app.db.redis_client import get_redis
from app.logging import get_logger
from app.queue import keys

log = get_logger(__name__)

router = APIRouter(prefix="/nodes", tags=["nodes"])

# A worker whose last heartbeat is older than this is considered offline.
_OFFLINE_THRESHOLD_SECONDS = 60


async def _node_conc(node_id: str) -> dict:
    """Read per-node concurrency counters, or zeroes if unavailable."""
    try:
        values = await get_redis().mget(*keys.node_conc_keys(node_id))
    except Exception:
        return {"cpu": 0, "mem_mb": 0, "io_heavy": 0}
    return {
        "cpu": max(int(values[0] or 0), 0),
        "mem_mb": max(int(values[1] or 0), 0),
        "io_heavy": max(int(values[2] or 0), 0),
    }


@router.get("")
async def list_nodes() -> list[dict]:
    """All known compute nodes, with status and resource usage.

    Built from the live `bp:workers` hash. Workers register every heartbeat
    interval; a worker unseen past the threshold is offline, and a node with
    no online workers is offline.
    """
    try:
        raw = await get_redis().hgetall(keys.WORKERS)
    except Exception:
        log.warning("nodes_read_failed")
        return []

    import json

    now = datetime.now(UTC)
    threshold = now - timedelta(seconds=_OFFLINE_THRESHOLD_SECONDS)

    # Group workers by node_id.
    by_node: dict[str, dict] = {}
    for worker_id, blob in raw.items():
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

        if node_id not in by_node:
            by_node[node_id] = {
                "node_id": node_id,
                "workers": 0,
                "online_workers": 0,
                "running_jobs": 0,
                "slots": 0,
                "online": False,
            }
        entry = by_node[node_id]
        entry["workers"] += 1
        if online:
            entry["online_workers"] += 1
            entry["online"] = True
        entry["running_jobs"] += len(data.get("running", []))
        entry["slots"] += data.get("slots", 0)

    # Attach per-node concurrency readings.
    result = []
    for node_id, info in sorted(by_node.items()):
        if info["online"]:
            conc = await _node_conc(node_id)
            info["reserved"] = conc
        else:
            info["reserved"] = {"cpu": 0, "mem_mb": 0, "io_heavy": 0}
        result.append(info)

    return result
