"""Compute node status: which machines are connected and what they are doing."""

import asyncio
import ipaddress
import os
import platform
import re
import secrets
import socket
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncssh
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator, model_validator

from app.config import settings
from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging import get_logger
from app.models.node import Node
from app.models.node_provision import NodeProvisionTask
from app.models.node_update import NodeUpdateTask
from app.queue import node_stats as node_stats_mod
from app.queue import worker_registry
from app.queue.worker import _own_image_digest
from app.services import node_ssh, node_update_service
from app.services.ai import crypto
from app.version import __version__

log = get_logger(__name__)

router = APIRouter(prefix="/nodes", tags=["nodes"])

# A worker whose last heartbeat is older than this is considered offline.
_OFFLINE_THRESHOLD_SECONDS = 60


# --- Provisioning request/response models ---

# `node_name` and `storage_location` are interpolated into commands that run on
# the remote node -- most consequentially into the body of a quoted heredoc
# (`_render_node_env`, written at the write_env step). The quoted delimiter
# stops `$`-expansion but not *delimiter* injection: a value carrying a newline
# followed by the delimiter ends the heredoc early and everything after it runs
# as shell on the remote machine. `node_name` also becomes the comment on a
# generated SSH key that is appended to the node's authorized_keys, where a
# newline forges an extra key line.
#
# The endpoint is unauthenticated, so with a rebound hostname (#871) a web page
# could drive this against a host the victim has credentials for. Validating
# here, in the model, is what makes that unreachable rather than escaped-so-far:
# every path out of this request is covered at once, and a new call site cannot
# forget to quote.
_NODE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# An absolute path of ordinary path segments. Deliberately no shell
# metacharacters, no whitespace, no "..", and no trailing slash -- this is a
# storage directory being named, not an arbitrary string.
_STORAGE_LOCATION_RE = re.compile(r"^(/[A-Za-z0-9._-]+)+$")


def _sanitize_node_name(raw: str) -> str:
    """Coerce a hostname-derived string into something `_NODE_NAME_RE` accepts.

    Only for values *this* code suggests, never for user input -- a request
    that fails validation must be refused, not quietly rewritten into a
    different node than the caller asked for.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", raw).strip("-")[:64]
    return cleaned or "compute-node"


class ProvisionRequest(BaseModel):
    host: str
    port: int = 22
    username: str
    password: str | None = None
    private_key: str | None = None
    node_name: str
    storage_location: str = "/data/scratch"
    worker_replicas: int = 2

    @field_validator("node_name")
    @classmethod
    def _check_node_name(cls, v: str) -> str:
        if not _NODE_NAME_RE.match(v):
            raise ValueError(
                "node_name must be 1-64 characters of letters, digits, "
                "underscore or hyphen"
            )
        return v

    @field_validator("storage_location")
    @classmethod
    def _check_storage_location(cls, v: str) -> str:
        if not _STORAGE_LOCATION_RE.match(v) or ".." in v.split("/"):
            raise ValueError(
                "storage_location must be an absolute path made of letters, "
                "digits, dot, underscore or hyphen -- no whitespace, shell "
                "metacharacters, or '..' segments"
            )
        return v

    @model_validator(mode="after")
    def _check_credential(self) -> "ProvisionRequest":
        if not self.password and not self.private_key:
            raise ValueError("Either password or private_key must be provided")
        if self.password and self.private_key:
            raise ValueError("Provide exactly one of password or private_key, not both")
        return self


class UpdateRequest(BaseModel):
    """Whether to let running jobs finish before swapping the image."""

    drain: bool = True


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
                "image_digest": doc.image_digest,
                "version": doc.version,
                "updatable": doc.ssh_key_enc is not None,
            }
    except Exception:
        # Any error here -- including an AttributeError from a Node field this
        # loop reads but a caller's mock/fixture doesn't have -- discards all
        # mongo_nodes accumulated so far for this call, not just the doc that
        # triggered it, and logs no doc id or traceback. This already happened
        # once: a stale test fixture silently emptied mongo_nodes in two
        # pre-existing tests. Don't widen this catch or add a field read here
        # without checking every caller's mocks/fixtures first.
        log.warning("node_mongo_read_failed")

    workers = await worker_registry.live_workers()

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

    for worker_id, data in workers:
        # A payload with no node_id is malformed, not a node called "unknown".
        # Synthesizing one used to invent a phantom row in the settings table
        # (#451) and collided with the real "unknown" enrollment status below.
        node_id = data.get("node_id")
        if not node_id:
            log.warning("worker_missing_node_id", worker_id=worker_id)
            continue
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
        entry["image_digest"] = mongo_info.get("image_digest")
        entry["version"] = mongo_info.get("version")
        entry["updatable"] = mongo_info.get("updatable", False)

    return by_node


@router.get("")
async def list_nodes() -> list[dict]:
    """All known compute nodes, with status and resource usage.

    Merges live Redis data (heartbeat, running jobs, slots) with MongoDB
    enrollment records (status, hostname, registered_at).  A node that has
    enrolled but has no online workers shows as ``"online": false``; a node
    whose enrollment was revoked shows ``"enrollment": "revoked"`` even if
    its workers are still heartbeating.
    """
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
            "image_digest": None,
            "version": None,
            "updatable": False,
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

    # Suggest a node name from the primary's hostname, sanitized to what
    # ProvisionRequest will actually accept. platform.node() routinely returns
    # a dotted mDNS name ("Johns-MacBook-Pro.local"), and offering the user a
    # default their own form then rejects is a worse bug than the one the
    # validation closes.
    try:
        primary_hostname = platform.node() or "primary"
    except Exception:
        primary_hostname = "primary"
    suggested = _sanitize_node_name(f"{primary_hostname}-node")

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


# --- Provisioning helpers ---

class UnroutablePrimaryHost(Exception):
    """Discovery produced an address a remote node could not reach us at."""


# Hostnames that are never the primary as seen from another machine. These are
# what `socket.gethostname()` returns inside a container often enough to matter.
_UNROUTABLE_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


def _is_routable_from_other_hosts(host: str) -> bool:
    """Whether `host` could plausibly identify this machine to a remote node.

    Only addresses are judged. A non-address name is accepted: we cannot
    resolve it the way the node's resolver would, and rejecting a name that
    happens to work would break provisioning that currently succeeds.

    Docker bridge addresses are the case #803 was filed for. They are ordinary
    RFC-1918 addresses -- a bridge on 172.19/16 is indistinguishable from a
    LAN on 172.19/16 -- so private ranges cannot simply be rejected: on this
    single-LAN tool the primary's real address is almost always private. What
    *is* safe to reject is the set of addresses that are meaningless off-box
    by definition, regardless of network layout.
    """
    if host.strip().lower() in _UNROUTABLE_NAMES:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_multicast
        or addr.is_reserved
    )


# Present in every Docker container, absent on the host. Verified against this
# project's own api container and the host it runs on.
_CONTAINER_MARKER = "/.dockerenv"


def _is_container_internal(host: str) -> bool:
    """Whether `host` is this *container's* address rather than the machine's.

    The UDP-connect discovery returns whichever address the kernel would use
    to reach the internet. On the host that is the LAN address we want; inside
    a container it is the container's Docker-network address, which is what
    #803 shipped into node .env files.

    Both are private addresses, so the address alone cannot distinguish them.
    What does is *where the code is running*: if we are in a container, a
    private discovered address is a bridge address, because a container does
    not have the host's LAN address on any of its interfaces.

    A public discovered address is left alone in either case -- that is a
    machine with a routable address, and it is reachable as-is.
    """
    if not os.path.exists(_CONTAINER_MARKER):
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private


def _primary_hostname() -> str:
    """The primary's externally-routable hostname.

    Uses PRIMARY_HOSTNAME config if set -- an explicit setting is an
    instruction and is never second-guessed, since the user may be pointing
    nodes at an address whose routing we cannot see from in here.

    Otherwise the LAN IP is discovered via a UDP socket connect, and *checked*
    before it is handed to a node. Running inside the API container, that
    trick returns the container's Docker bridge address; written into a node's
    .env as MONGO_URL it resolves to the node's own bridge network, so the
    worker crash-loops against a Mongo that is not ours (#803). Provisioning
    reported success throughout, because every step it verified had genuinely
    worked.

    Raises `UnroutablePrimaryHost` rather than returning a value the caller
    would have to remember to check: the whole failure was a bad address being
    used as though it were good.
    """
    if settings.primary_hostname:
        return settings.primary_hostname
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("1.1.1.1", 1))
        ip = s.getsockname()[0]
        s.close()
        candidate = ip
    except (TimeoutError, OSError):
        candidate = socket.gethostname()

    if not _is_routable_from_other_hosts(candidate):
        raise UnroutablePrimaryHost(
            f"Discovered the primary's address as {candidate!r}, which no "
            "other machine can reach. Set PRIMARY_HOSTNAME to this machine's "
            "LAN address (or DNS name) and provision the node again."
        )
    if _is_container_internal(candidate):
        raise UnroutablePrimaryHost(
            f"Discovered the primary's address as {candidate!r}, which is this "
            "container's own Docker network address and means nothing on "
            "another machine. Set PRIMARY_HOSTNAME to this machine's LAN "
            "address (or DNS name) and provision the node again."
        )
    return candidate


def _build_connection_urls(host: str) -> dict[str, str]:
    """Build externally-routable Mongo, Redis, and API URLs for node .env."""
    mongo = _rewrite_host(settings.mongo_url, host)
    redis = _rewrite_host(settings.redis_url, host)
    return {
        "mongo_url": mongo,
        "redis_url": redis,
        "api_url": f"http://{host}:8000",
    }


def _render_node_compose() -> str:
    """Render the worker-only compose file for a compute node.

    Generated here rather than bundled into the image: the API image builds
    with `context: ./backend` (docker-compose.override.yml and
    release.yml both), and the repo-root docker-compose.yml sits outside
    that context, so no `COPY` in backend/Dockerfile can reach it. Reading
    it from /srv/ was the original approach and could never have worked --
    nothing ever put a file there, and provisioning failed on every run with
    "Compose file not found in API container."

    The node runs the worker and nothing else: Mongo, Redis, and the API
    live on the primary, and this file deliberately omits them so a stray
    `docker compose up` on the node cannot start a second database. Every
    value it needs comes from the .env written alongside it by
    `_render_node_env`, so the two must be changed together -- the keys
    referenced here are exactly the keys rendered there.
    """
    return (
        "name: bioflow-node\n"
        "\n"
        "services:\n"
        "  worker:\n"
        "    image: ghcr.io/syntheticgio/bioflow-backend:${BIOFLOW_TAG:-latest}\n"
        '    command: ["python", "-m", "app.worker_main"]\n'
        "    environment:\n"
        "      MONGO_URL: ${MONGO_URL}\n"
        "      REDIS_URL: ${REDIS_URL}\n"
        "      BIOINFO_HOME: /data\n"
        "      BIOINFO_REGISTER_ROOTS: /data\n"
        "      BIOINFO_HOME_HOST: ${BIOINFO_HOME}\n"
        "      WORKER_NODE_ID: ${WORKER_NODE_ID:?WORKER_NODE_ID must be set}\n"
        "      PRIMARY_API_URL: ${PRIMARY_API_URL}\n"
        "      WORKER_MAX_CONCURRENT: ${WORKER_MAX_CONCURRENT:-4}\n"
        "      NCBI_SETTINGS: /data/tmp/ncbi/user-settings.mkfg\n"
        "      LOG_LEVEL: ${LOG_LEVEL:-INFO}\n"
        "    volumes:\n"
        "      - ${BIOINFO_HOME}:/data\n"
        "      # The host's Docker socket, so the worker can start sibling\n"
        "      # containers for tools too large to vendor into the image\n"
        "      # (DeepVariant is 8.83GB). Same reason as the primary stack.\n"
        "      - /var/run/docker.sock:/var/run/docker.sock\n"
        "    extra_hosts:\n"
        '      - "host.docker.internal:host-gateway"\n'
        "    mem_limit: ${BIOFLOW_HARD_MEM_LIMIT:-0}\n"
        "    deploy:\n"
        "      replicas: ${WORKER_REPLICAS:-2}\n"
        "    # The default 10s would SIGKILL mid-drain, stranding leases.\n"
        "    stop_grace_period: 90s\n"
        "    restart: unless-stopped\n"
    )


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


# --- Remote command execution ---

class RemoteStep(BaseModel):
    """One remote command, with the phase it reports and how it fails.

    `describe` turns the command's own output into the message the user sees.
    It takes the completed result rather than a preformatted string so a step
    can say something specific about *why* the command failed -- the exit
    status alone is rarely enough to act on, and the stderr/stdout fallback
    ordering was previously repeated at every call site.
    """

    model_config = {"arbitrary_types_allowed": True}

    phase: str
    message: str
    command: str
    timeout: float
    describe_failure: object


class RemoteCommandError(Exception):
    """A command in a sequence exited non-zero.

    Carries the step that failed so the caller can report both the phase the
    task was in and a message describing the failure, without re-deriving
    either from the exception text.
    """

    def __init__(self, step: RemoteStep, reason: str) -> None:
        super().__init__(reason)
        self.step = step
        self.reason = reason


def _command_output(result) -> str:
    """A command's output for display: stderr, else stdout, else a placeholder.

    stderr first because a command that failed usually explains itself there;
    stdout is the fallback for tools that report errors on it anyway.
    """
    return result.stderr or result.stdout or "no output"


async def _execute_remote_commands(
    conn,
    steps: list[RemoteStep],
    *,
    on_progress,
) -> None:
    """Run `steps` in order over `conn`, stopping at the first failure.

    Raises `RemoteCommandError` naming the step that failed rather than
    returning a status, so a caller cannot continue past a failed command by
    forgetting to check a return value -- which is the failure mode that
    matters here, since every later step assumes the earlier ones ran.

    `on_progress` is awaited once per *phase*, not once per command: several
    commands can share a phase (writing the compose file and creating the
    directory it goes in are both `setup_install`), and re-reporting the same
    phase would replace the message the user is reading with an identical one.
    The phases are what the provisioning UI renders, so a step's phase string
    is a user-visible contract, not an internal label.
    """
    reported: str | None = None
    for step in steps:
        if step.phase != reported:
            await on_progress(step.phase, step.message)
            reported = step.phase
        result = await asyncio.wait_for(
            conn.run(step.command, check=False), timeout=step.timeout
        )
        if result.exit_status != 0:
            raise RemoteCommandError(step, step.describe_failure(result))


# --- Post-install verification ---

# How long a freshly started worker gets to still be running. Short: this
# checks that the container did not immediately exit, not that it has
# finished starting up. A crash-loop shows up within seconds -- the failures
# this catches (a bad image, an unreadable .env, a storage path the worker
# cannot write) all kill the process on startup.
_VERIFY_SETTLE_SECONDS = 5
_VERIFY_TIMEOUT_SECONDS = 30
# Any restart within the settle window means the worker is not staying up.
# A healthy worker starts once and stays; this is not a tolerance budget.
_VERIFY_MAX_RESTARTS = 1


async def _verify_node_operational(conn, install_dir: str, host: str) -> str | None:
    """Check that the worker `up -d` started is actually still running.

    Returns None when the node is operational, or a message describing the
    failure. `docker compose up -d` exits 0 as soon as the container is
    *created*, which is before it can crash -- so a node whose worker dies on
    startup (bad image, unwritable storage path, unreadable .env) provisioned
    "successfully" and then never appeared, with the failure visible only in
    `docker compose logs` on the node itself. node_update_service already
    treats this as the failure worth guarding against; provisioning did not.

    Asks Docker for the container's state rather than waiting for the worker
    to enroll: a worker that starts fine but cannot reach the primary's Mongo
    is a *different* failure with a different fix, and reporting it as "the
    worker did not start" would send the user to the wrong machine.

    The state alone is not enough, though. `restart: unless-stopped` returns a
    container that dies on startup to `running` immediately, so a crash-loop
    reads as healthy at every sampling instant -- the node behind #803 sat at
    55 restarts, state `running`, and provisioning called it enrolled. The
    restart count is what tells the two apart.
    """
    await asyncio.sleep(_VERIFY_SETTLE_SECONDS)

    result = await asyncio.wait_for(
        conn.run(
            f"docker compose -f {install_dir}/docker-compose.yml "
            "ps --format '{{.State}}' worker",
            check=False,
        ),
        timeout=_VERIFY_TIMEOUT_SECONDS,
    )
    if result.exit_status != 0:
        return (
            f"Could not check the worker's state on {host}: "
            f"{_command_output(result)}"
        )

    states = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if not states:
        return (
            f"The worker container is not present on {host} despite starting "
            f"without error. Check `docker compose logs worker` on {host}."
        )
    if not all(state == "running" for state in states):
        return (
            f"The worker on {host} started and then stopped "
            f"(state: {', '.join(states)}). This usually means it crashed on "
            f"startup -- check `docker compose logs worker` on {host}."
        )

    return await _check_worker_not_restarting(conn, install_dir, host)


async def _check_worker_not_restarting(conn, install_dir: str, host: str) -> str | None:
    """Fail a worker that is `running` only because Docker keeps restarting it.

    Returns None when the restart counts look healthy *or cannot be read*: the
    readability of `docker inspect` is a property of the node's Docker, not of
    the worker, so treating unreadable output as a crash-loop would fail nodes
    that are fine. The state check above stays the load-bearing signal; this
    only adds a failure it cannot see.
    """
    result = await asyncio.wait_for(
        conn.run(
            f"docker compose -f {install_dir}/docker-compose.yml ps -q worker "
            "| xargs -r docker inspect --format '{{.RestartCount}}'",
            check=False,
        ),
        timeout=_VERIFY_TIMEOUT_SECONDS,
    )
    if result.exit_status != 0:
        return None

    counts = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            counts.append(int(line))
        except ValueError:
            return None
    if not counts:
        return None

    worst = max(counts)
    if worst < _VERIFY_MAX_RESTARTS:
        return None
    return (
        f"The worker on {host} is restarting repeatedly ({worst} restarts) "
        "rather than staying up. It starts, fails, and is restarted by "
        "Docker, so it reports as running while doing no work. Check "
        f"`docker compose logs worker` on {host} -- a worker that cannot "
        "reach the primary's Mongo or Redis fails this way."
    )


# --- Provisioning executor ---

async def _provision_node(task_id: str, req: ProvisionRequest) -> None:
    """Run the full node provisioning flow in a background task."""
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
        # Phase 1: validate_ssh with TOFU host key verification
        await _update("validate_ssh", f"Connecting to {req.host}…")
        try:
            conn, host_key = await node_ssh.connect_with_tofu(
                req.host,
                port=req.port,
                username=req.username,
                private_key=req.private_key,
                password=req.password,
                stored_host_key=None,  # first connection — capture the key
                timeout_seconds=15,
            )
        except TimeoutError:
            return await _fail(
                f"Connection to {req.host}:{req.port} timed out."
            )
        except asyncssh.Error as e:
            return await _fail(str(e))

        try:
            # INSTALL_DIR stays unexpanded on purpose: `~` must be resolved by
            # the *remote* user's shell, not this container's. Expanding it
            # here with os.path.expanduser() read the API container's own HOME
            # -- it runs as root -- and sent `mkdir -p /root/.bioflow` to the
            # node, which fails for any non-root SSH user. It is shared with
            # node_update_service so provisioning and updates cannot drift
            # onto different directories.
            install_dir = node_update_service.INSTALL_DIR

            # The quoted heredoc delimiter is load-bearing: the compose file
            # is almost entirely `${...}` references that the *node's* Compose
            # must expand against the .env written alongside it. An unquoted
            # delimiter would let the remote shell expand them first, and
            # since none of them are set in that shell they would all land as
            # empty strings -- yielding a syntactically valid compose file
            # with no image and no volumes.
            compose_contents = _render_node_compose()
            # Fails here, before anything is written to the node: an address
            # the node cannot reach produces a worker that starts, crash-loops
            # against the wrong Mongo, and never enrolls -- while every step
            # provisioning checks reports success (#803). Caught explicitly so
            # the user gets the remedy, not a stack trace from the catch-all.
            try:
                primary_host = _primary_hostname()
            except UnroutablePrimaryHost as e:
                task.phase = "write_env"
                return await _fail(str(e))
            urls = _build_connection_urls(primary_host)
            env_contents = _render_node_env(
                mongo_url=urls["mongo_url"],
                redis_url=urls["redis_url"],
                api_url=urls["api_url"],
                node_name=req.node_name,
                storage_location=req.storage_location,
                worker_replicas=req.worker_replicas,
            )

            # Phases 2-4: verify_docker, setup_install, write_env.
            try:
                await _execute_remote_commands(
                    conn,
                    [
                        RemoteStep(
                            phase="verify_docker",
                            message=f"Checking Docker on {req.host}…",
                            command="docker version --format '{{.Server.Version}}'",
                            timeout=15,
                            describe_failure=lambda _r: (
                                f"Docker is not available on {req.host}. "
                                "Install Docker first: "
                                "https://docs.docker.com/engine/install/"
                            ),
                        ),
                        RemoteStep(
                            phase="setup_install",
                            message="Preparing install directory…",
                            command=f"mkdir -p {install_dir}",
                            timeout=15,
                            describe_failure=lambda r: (
                                f"Could not create {install_dir} on {req.host}: "
                                f"{_command_output(r)}"
                            ),
                        ),
                        RemoteStep(
                            phase="setup_install",
                            message="Writing the node compose file…",
                            command=(
                                f"cat > {install_dir}/docker-compose.yml "
                                f"<< 'HERMESEOF'\n{compose_contents}\nHERMESEOF"
                            ),
                            timeout=15,
                            describe_failure=lambda r: (
                                f"Could not write {install_dir}/docker-compose.yml "
                                f"on {req.host}: {_command_output(r)}"
                            ),
                        ),
                        RemoteStep(
                            phase="write_env",
                            message="Writing node configuration…",
                            command=(
                                f"cat > {install_dir}/.env << 'HERMESEOF'\n"
                                f"{env_contents}\nHERMESEOF"
                            ),
                            timeout=15,
                            describe_failure=lambda r: (
                                f"Could not write {install_dir}/.env on "
                                f"{req.host}: {_command_output(r)}"
                            ),
                        ),
                    ],
                    on_progress=_update,
                )
            except RemoteCommandError as e:
                task.phase = e.step.phase
                return await _fail(e.reason)

            # Phase 5: install_key
            #
            # Before the image is pulled, so a node that cannot take the key
            # costs nothing. Failing here leaves the node unprovisioned rather
            # than provisioned-but-not-updatable: a fallback to storing the
            # user's own credential would make the security property depend on
            # a condition nobody observed.
            await _update("install_key", "Installing the BioFlow update key…")
            private_pem, public_line = node_ssh.generate_keypair(req.node_name)
            await node_ssh.install_public_key(conn, public_line)
            # verify_key returns the host key for TOFU pinning
            verify_conn, host_key = await node_ssh.verify_key(
                req.host, req.port, req.username, private_pem
            )
            verify_conn.close()

            node_doc = await Node.find_one(Node.node_id == req.node_name)
            if node_doc is None:
                node_doc = Node(node_id=req.node_name, hostname=req.host)
            node_doc.ssh_host = req.host
            node_doc.ssh_port = req.port
            node_doc.ssh_username = req.username
            node_doc.ssh_key_enc = crypto.encrypt(private_pem)
            node_doc.ssh_key_installed_at = datetime.now(UTC)
            node_doc.host_key = host_key
            await node_doc.save()

            # Phases 6-7: pull_image, start_worker.
            #
            # `--no-deps worker`, matching the launcher's own up_node (see
            # launcher/src-tauri/src/docker/shell.rs). The generated compose
            # file names only the worker, so this is belt-and-braces there --
            # but it is what keeps this command correct if the file ever
            # regains a service, and it mirrors what the launcher does on a
            # node provisioned the other way.
            try:
                await _execute_remote_commands(
                    conn,
                    [
                        RemoteStep(
                            phase="pull_image",
                            message="Pulling backend image…",
                            command=(
                                "docker pull "
                                "ghcr.io/syntheticgio/bioflow-backend:latest"
                            ),
                            timeout=600,
                            describe_failure=lambda r: (
                                f"Image pull failed: {r.stderr or r.stdout}"
                            ),
                        ),
                        RemoteStep(
                            phase="start_worker",
                            message="Starting worker…",
                            command=(
                                f"docker compose -f {install_dir}/docker-compose.yml "
                                "up -d --no-deps worker"
                            ),
                            timeout=60,
                            describe_failure=lambda r: (
                                f"Worker failed to start: {r.stderr or r.stdout}"
                            ),
                        ),
                    ],
                    on_progress=_update,
                )
            except RemoteCommandError as e:
                task.phase = e.step.phase
                return await _fail(e.reason)

            # Phase 8: verify
            await _update("verify", "Checking the worker stayed up…")
            problem = await _verify_node_operational(conn, install_dir, req.host)
            if problem is not None:
                task.phase = "verify"
                return await _fail(problem)

            # Phase 9: enrolled
            await _update("enrolled", "Node enrolled ✓")
            task.status = "success"
            task.finished_at = datetime.now(UTC)
            await task.save()

        finally:
            conn.close()

    except node_ssh.KeyInstallError as e:
        await _fail(str(e))

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
    image_digest = str(payload.get("image_digest") or "").strip() or None
    version = str(payload.get("version") or "").strip() or None

    if not node_id:
        raise ValidationError("node_id is required")

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
        # Only overwrite when reported: a node that cannot read its digest
        # must not erase the version it last reported.
        if image_digest:
            existing.image_digest = image_digest
        if version:
            existing.version = version
        await existing.save()
    else:
        node = Node(
            node_id=node_id,
            hostname=hostname,
            last_seen=now,
            status="active",
            image_digest=image_digest,
            version=version,
        )
        await node.insert()

    return {
        "node_id": node_id,
        "status": "active",
        "message": "enrolled",
    }


@router.get("/current-version")
async def current_version() -> dict:
    """The image the primary is running, as the reference for staleness.

    Nodes are compared against this rather than against a registry tag: the
    digest is what actually differs when an image is republished under the
    same tag. Run off the event loop -- _own_image_digest shells out to
    docker twice, up to ~20s worst case, which would otherwise stall every
    other request this process is serving (node heartbeats, job claims, UI
    polling) for the duration.
    """
    digest = await asyncio.to_thread(_own_image_digest)
    return {"image_digest": digest, "version": __version__}


_active_updates: dict[str, asyncio.Task] = {}


@router.post("/{node_id}/update", status_code=201)
async def update_node(node_id: str, req: UpdateRequest) -> dict:
    """Pull the current backend image on a node and restart its worker."""
    node = await Node.find_one(Node.node_id == node_id)
    if node is None:
        raise NotFoundError(f"Node {node_id!r} not found")
    if node.ssh_key_enc is None:
        raise ConflictError(
            f"Node {node_id!r} was not provisioned from BioFlow, so there is no "
            "stored key to reach it with. Re-provision it to enable updates."
        )

    running = await NodeUpdateTask.find_one(
        NodeUpdateTask.node_id == node_id,
        NodeUpdateTask.status == "updating",
    )
    if running is not None:
        raise ConflictError(f"Node {node_id!r} is already being updated.")

    task_doc = NodeUpdateTask(
        node_id=node_id,
        host=node.ssh_host or "",
        from_digest=node.image_digest,
        drain=req.drain,
        message="Queued…",
    )
    await task_doc.insert()

    bg = asyncio.create_task(
        node_update_service.run_update(task_doc.task_id, node, drain=req.drain)
    )
    _active_updates[task_doc.task_id] = bg
    bg.add_done_callback(lambda _: _active_updates.pop(task_doc.task_id, None))

    return {"task_id": task_doc.task_id, "status": "updating"}


@router.get("/update/{task_id}")
async def update_status(task_id: str) -> dict:
    """Poll the status of an update task."""
    task = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == task_id)
    if task is None:
        raise NotFoundError(f"Update task {task_id!r} not found")
    return {
        "task_id": task.task_id,
        "status": task.status,
        "phase": task.phase,
        "message": task.message,
        "pct": task.pct,
        "node_id": task.node_id,
        "host": task.host,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "error": task.error,
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
        raise NotFoundError(f"Node {node_id!r} not found")
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
        raise NotFoundError(f"Node {node_id!r} not found")
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
        raise NotFoundError(f"Provisioning task {task_id!r} not found")
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

        orphaned_updates = await NodeUpdateTask.find(
            NodeUpdateTask.status == "updating",
        ).to_list()
        for t in orphaned_updates:
            if t.task_id not in _active_updates:
                t.status = "failed"
                t.error = "API restart interrupted the update"
                t.finished_at = datetime.now(UTC)
                await t.save()
                log.info("orphaned_update_cleaned", task_id=t.task_id)
    except Exception:
        log.warning("provision_cleanup_failed")
