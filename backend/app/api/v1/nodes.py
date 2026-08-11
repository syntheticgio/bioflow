"""Compute node status: which machines are connected and what they are doing."""

import asyncio
import os
import pathlib
import platform
import re
import secrets
import socket
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, model_validator

from app.config import settings
from app.db.redis_client import get_redis
from app.logging import get_logger
from app.models.node import Node
from app.models.node_provision import NodeProvisionTask
from app.queue import keys

log = get_logger(__name__)

router = APIRouter(prefix="/nodes", tags=["nodes"])

# A worker whose last heartbeat is older than this is considered offline.
_OFFLINE_THRESHOLD_SECONDS = 60


# --- Provisioning request/response models ---

class ProvisionRequest(BaseModel):
    host: str
    port: int = 22
    username: str
    password: str | None = None
    private_key: str | None = None
    node_name: str
    storage_location: str = "/data/scratch"
    worker_replicas: int = 2

    @model_validator(mode="after")
    def _check_credential(self) -> "ProvisionRequest":
        if not self.password and not self.private_key:
            raise ValueError("Either password or private_key must be provided")
        if self.password and self.private_key:
            raise ValueError("Provide exactly one of password or private_key, not both")
        return self


class ProvisionStatusOut(BaseModel):
    task_id: str
    status: str
    phase: str
    message: str
    pct: float | None = None
    node_name: str
    host: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


# --- Existing node listing ---


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
            rf"(?P<before>(://|@)){re.escape(name)}(?P<after>(:\\d+|/|$))",
            rf"\g<before>{host}\g<after>",
            result,
            count=1,
        )
    return result


# --- Provisioning helpers ---

def _primary_hostname() -> str:
    """The primary's externally-routable hostname.

    Uses PRIMARY_HOSTNAME config if set; otherwise discovers the LAN IP
    via a UDP socket connect. Falls back to socket.gethostname().
    """
    if settings.primary_hostname:
        return settings.primary_hostname
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("1.1.1.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except (TimeoutError, OSError):
        return socket.gethostname()


def _build_connection_urls(host: str) -> dict[str, str]:
    """Build externally-routable Mongo, Redis, and API URLs for node .env."""
    mongo = _rewrite_host(settings.mongo_url, host)
    redis = _rewrite_host(settings.redis_url, host)
    return {
        "mongo_url": mongo,
        "redis_url": redis,
        "api_url": f"http://{host}:8000",
    }


def _render_node_env(
    mongo_url: str,
    redis_url: str,
    api_url: str,
    node_name: str,
    storage_location: str,
    worker_replicas: int,
) -> str:
    """Render the .env file for a compute node, mirroring the launcher's output."""
    return (
        f"NODE_TYPE=compute\n"
        f"MONGO_URL={mongo_url}\n"
        f"REDIS_URL={redis_url}\n"
        f"WORKER_NODE_ID={node_name}\n"
        f"PRIMARY_API_URL={api_url}\n"
        f"BIOINFO_HOME={storage_location}\n"
        f"BIOINFO_REGISTER_ROOTS={storage_location}\n"
        f"BIOFLOW_TAG=latest\n"
        f"WORKER_REPLICAS={worker_replicas}\n"
    )


# --- Provisioning executor ---

async def _provision_node(task_id: str, req: ProvisionRequest) -> None:
    """Run the full node provisioning flow in a background task."""
    import asyncssh

    task = await NodeProvisionTask.find_one(
        NodeProvisionTask.task_id == task_id
    )
    if not task:
        task = NodeProvisionTask(
            task_id=task_id,
            node_name=req.node_name,
            host=req.host,
        )
        await task.insert()

    async def _update(phase: str, message: str, pct: float | None = None) -> None:
        task.phase = phase
        task.message = message
        task.pct = pct
        await task.save()

    async def _fail(reason: str) -> None:
        task.status = "failed"
        task.error = reason
        task.message = reason
        task.finished_at = datetime.now(UTC)
        await task.save()

    try:
        if req.private_key:
            import io
            key = asyncssh.import_private_key(io.StringIO(req.private_key))
            connect_kw = {"client_keys": [key]}
        else:
            connect_kw = {"password": req.password}

        # Phase 1: validate_ssh
        await _update("validate_ssh", f"Connecting to {req.host}…")
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    req.host,
                    port=req.port,
                    username=req.username,
                    known_hosts=None,
                    **connect_kw,
                ),
                timeout=15,
            )
        except TimeoutError:
            return await _fail(
                f"Connection to {req.host}:{req.port} timed out."
            )
        except asyncssh.Error as e:
            return await _fail(str(e))

        try:
            # Phase 2: verify_docker
            await _update("verify_docker", f"Checking Docker on {req.host}…")
            result = await asyncio.wait_for(
                conn.run("docker version --format '{{.Server.Version}}'", check=False),
                timeout=15,
            )
            if result.exit_status != 0:
                return await _fail(
                    f"Docker is not available on {req.host}. "
                    "Install Docker first: https://docs.docker.com/engine/install/"
                )

            # Phase 3: setup_install
            await _update("setup_install", "Preparing install directory…")
            install_dir = os.path.expanduser("~/.bioflow")  # noqa: ASYNC240
            await asyncio.wait_for(
                conn.run(f"mkdir -p {install_dir}", check=True),
                timeout=15,
            )

            compose_src = pathlib.Path("/srv/docker-compose.yml")  # noqa: ASYNC240
            if compose_src.exists():  # noqa: ASYNC240
                await asyncssh.scp(
                    str(compose_src),
                    (conn, f"{install_dir}/docker-compose.yml"),
                )
            else:
                return await _fail(
                    "Compose file not found in API container. "
                    "The API image must bundle docker-compose.yml at /srv/."
                )

            # Phase 4: write_env
            await _update("write_env", "Writing node configuration…")
            primary_host = _primary_hostname()
            urls = _build_connection_urls(primary_host)
            env_contents = _render_node_env(
                mongo_url=urls["mongo_url"],
                redis_url=urls["redis_url"],
                api_url=urls["api_url"],
                node_name=req.node_name,
                storage_location=req.storage_location,
                worker_replicas=req.worker_replicas,
            )
            await asyncio.wait_for(
                conn.run(
                    f"cat > {install_dir}/.env << 'HERMESEOF'\n{env_contents}\nHERMESEOF",
                    check=True,
                ),
                timeout=15,
            )

            # Phase 5: pull_image
            await _update("pull_image", "Pulling backend image…")
            pull_result = await asyncio.wait_for(
                conn.run(
                    "docker pull ghcr.io/syntheticgio/bioflow-backend:latest",
                    check=False,
                ),
                timeout=600,
            )
            if pull_result.exit_status != 0:
                return await _fail(
                    f"Image pull failed: {pull_result.stderr or pull_result.stdout}"
                )

            # Phase 6: start_worker
            await _update("start_worker", "Starting worker…")
            up_result = await asyncio.wait_for(
                conn.run(
                    f"docker compose -f {install_dir}/docker-compose.yml up -d",
                    check=False,
                ),
                timeout=60,
            )
            if up_result.exit_status != 0:
                return await _fail(
                    f"Worker failed to start: {up_result.stderr or up_result.stdout}"
                )

            # Phase 7: enrolled
            await _update("enrolled", "Node enrolled ✓")
            task.status = "success"
            task.finished_at = datetime.now(UTC)
            await task.save()

        finally:
            conn.close()

    except Exception as e:
        log.exception("node_provision_failed", task_id=task_id)
        await _fail(str(e))


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


# --- Provisioning endpoints ---

_active_provisions: dict[str, asyncio.Task] = {}


@router.post("/provision", status_code=201)
async def provision_node(req: ProvisionRequest) -> dict:
    """Start provisioning a compute node on a remote machine via SSH."""
    task_id = uuid4().hex[:12]
    task = asyncio.create_task(_provision_node(task_id, req))
    _active_provisions[task_id] = task
    task.add_done_callback(lambda _: _active_provisions.pop(task_id, None))
    return {"task_id": task_id, "status": "provisioning"}


@router.get("/provision/{task_id}")
async def provision_status(task_id: str):
    """Poll the status of a provisioning task."""
    task_doc = await NodeProvisionTask.find_one(
        NodeProvisionTask.task_id == task_id
    )
    if not task_doc:
        raise HTTPException(404, f"Provisioning task {task_id!r} not found")
    return {
        "task_id": task_doc.task_id,
        "status": task_doc.status,
        "phase": task_doc.phase,
        "message": task_doc.message,
        "pct": task_doc.pct,
        "node_name": task_doc.node_name,
        "host": task_doc.host,
        "started_at": task_doc.started_at.isoformat() if task_doc.started_at else None,
        "finished_at": task_doc.finished_at.isoformat() if task_doc.finished_at else None,
        "error": task_doc.error,
    }


async def _clean_orphaned_provisions() -> None:
    """On startup, mark orphaned provisioning tasks as failed.

    Tasks left in 'provisioning' status when no active asyncio.Task exists
    were abandoned by an API restart.
    """
    try:
        orphaned = await NodeProvisionTask.find(
            NodeProvisionTask.status == "provisioning",
        ).to_list()
        for t in orphaned:
            if t.task_id not in _active_provisions:
                t.status = "failed"
                t.error = "API restart interrupted provisioning"
                t.finished_at = datetime.now(UTC)
                await t.save()
                log.info("orphaned_provision_cleaned", task_id=t.task_id)
    except Exception:
        log.warning("provision_cleanup_failed")
