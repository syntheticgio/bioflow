"""Compute node status: which machines are connected and what they are doing."""

import platform
import re
import secrets

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.db.redis_client import get_redis
from app.logging import get_logger
from app.models.node import Node
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

    Merges live Redis data (heartbeat, running jobs, slots) with MongoDB
    enrollment records (status, hostname, registered_at).  A node that has
    enrolled but has no online workers shows as ``"online": false``; a node
    whose enrollment was revoked shows ``"enrollment": "revoked"`` even if
    its workers are still heartbeating.
    """
    import json

    now = datetime.now(UTC)
    threshold = now - timedelta(seconds=_OFFLINE_THRESHOLD_SECONDS)

    # --- MongoDB: enrollment records ---
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

    # --- Redis: live worker data ---
    try:
        raw = await get_redis().hgetall(keys.WORKERS)
    except Exception:
        log.warning("nodes_read_failed")
        raw = {}

    # Group workers by node_id.
    by_node: dict[str, dict] = {}
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

    # Fold in any enrolled nodes that have no workers (yet).
    for node_id, _mongo_info in mongo_nodes.items():
        if node_id not in by_node:
            by_node[node_id] = {
                "node_id": node_id,
                "workers": 0,
                "online_workers": 0,
                "running_jobs": 0,
                "slots": 0,
                "online": False,
            }

    # Build result — merge Redis + MongoDB.
    result = []
    for node_id, info in sorted(by_node.items()):
        # Attach per-node concurrency readings for online nodes.
        if info["online"]:
            conc = await _node_conc(node_id)
            info["reserved"] = conc
        else:
            info["reserved"] = {"cpu": 0, "mem_mb": 0, "io_heavy": 0}

        # Merge MongoDB enrollment info.
        mongo_info = mongo_nodes.get(node_id, {})
        info["hostname"] = mongo_info.get("hostname", "")
        info["registered_at"] = mongo_info.get("registered_at")
        info["enrollment"] = mongo_info.get("enrollment", "unknown")
        info["last_seen_mongo"] = mongo_info.get("last_seen")

        result.append(info)

    return result


# Canonical Docker service names that are not routable from outside the
# compose network. Replaced with the request's host when building the
# connection-details response.
_REDACTABLE_HOSTS = {"mongo", "redis", "api", "web", "worker"}


@router.get("/connection-details")
async def connection_details(request: Request) -> dict:
    """Return the URLs a compute node needs to connect to this primary.

    Auto-discovery: the launcher hits this endpoint when the user enters the
    primary's hostname.  Returns externally-routable Mongo and Redis URLs
    (internal Docker hostnames rewritten to the request's host), plus a
    suggested node name derived from the primary's hostname.
    """
    host = request.client.host if request.client else "127.0.0.1"
    # IPv6 localhost often shows up as ::1 -- normalize for hostname use.
    if host == "::1":
        host = "127.0.0.1"

    mongo = _rewrite_host(settings.mongo_url, host)
    redis = _rewrite_host(settings.redis_url, host)

    # Suggest a node name from the primary's hostname.
    try:
        primary_hostname = platform.node() or "primary"
    except Exception:
        primary_hostname = "primary"
    suggested = f"{primary_hostname}-node"

    return {
        "mongo_url": mongo,
        "redis_url": redis,
        "api_url": f"http://{host}:8000",
        "suggested_node_name": suggested,
    }


def _rewrite_host(url: str, host: str) -> str:
    """Replace Docker service hostnames in `url` with `host`.

    e.g. ``mongodb://mongo:27017/db`` → ``mongodb://192.168.1.50:27017/db``.
    Hostnames that are already real IPs or FQDNs pass through unchanged.
    """
    if not url:
        return url
    result = url
    for name in _REDACTABLE_HOSTS:
        # Match the service name as a hostname — preceded by :// or @
        # and followed by :port or / or end-of-string.
        result = re.sub(
            rf"(?P<before>(://|@)){re.escape(name)}(?P<after>(:\d+|/|$))",
            rf"\g<before>{host}\g<after>",
            result,
            count=1,
        )
    return result


# --- Enrollment ---

_ENROLL_KEY_EMPTY_SENTINEL = ""


@router.post("/enroll")
async def enroll_node(payload: dict) -> dict:
    """Register or re-register a compute node.

    A child worker calls this on startup.  If the primary has an enrollment
    key configured, the child must present it.  Revoked nodes are rejected
    outright.

    Idempotent: calling it again with the same ``node_id`` updates the
    hostname and ``last_seen`` without changing the enrollment status.
    """
    node_id = str(payload.get("node_id", "")).strip()
    hostname = str(payload.get("hostname", "")).strip()
    key = str(payload.get("enrollment_key", "")).strip()

    if not node_id:
        raise HTTPException(422, "node_id is required")

    # ---- verify enrollment key ----
    if settings.enrollment_key != _ENROLL_KEY_EMPTY_SENTINEL:
        if not key:
            raise HTTPException(403, "Missing enrollment_key")
        if not secrets.compare_digest(key, settings.enrollment_key):
            raise HTTPException(403, "Invalid enrollment_key")

    # ---- upsert node ----
    existing = await Node.find_one(Node.node_id == node_id)
    if existing and existing.status == "revoked":
        raise HTTPException(403, f"Node {node_id!r} has been revoked")

    now = datetime.now(UTC)
    if existing:
        existing.hostname = hostname
        existing.last_seen = now
        await existing.save()
    else:
        node = Node(
            node_id=node_id,
            hostname=hostname,
            last_seen=now,
            status="active",
        )
        await node.insert()

    return {
        "node_id": node_id,
        "status": "active",
        "message": "enrolled",
    }


@router.get("/{node_id}/status")
async def node_status(node_id: str) -> dict:
    """Check whether a node is still active.

    Child workers poll this to detect revocation without restarting.
    Returns 404 for unknown nodes so the worker can distinguish "not yet
    enrolled" from "enrolled but revoked."
    """
    node = await Node.find_one(Node.node_id == node_id)
    if not node:
        raise HTTPException(404, f"Node {node_id!r} not found")
    return {
        "node_id": node.node_id,
        "status": node.status,
    }


@router.delete("/{node_id}")
async def revoke_node(node_id: str) -> dict:
    """Revoke a node so it can no longer claim jobs.

    The node's workers will discover the revocation on their next status
    poll (or at next enrollment attempt) and stop claiming.
    """
    node = await Node.find_one(Node.node_id == node_id)
    if not node:
        raise HTTPException(404, f"Node {node_id!r} not found")
    node.status = "revoked"
    await node.save()
    log.info("node_revoked", node_id=node_id)
    return {"node_id": node_id, "status": "revoked"}
