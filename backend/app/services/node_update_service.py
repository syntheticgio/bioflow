"""Updating a compute node's backend image over SSH.

The update is phases 6-7 of provisioning (`docker pull`, `docker compose up
-d`) run again later, against a node whose managed key the primary already
holds.

Two orderings matter. The pull happens before anything is stopped, so a failed
download leaves the node running its current image. And success means a worker
re-enrolled reporting the new digest -- `docker compose up -d` exits 0 for a
container that immediately crash-loops, which is the failure this feature
exists to fix.

Pulls via `docker compose ... pull`, not `docker pull <hardcoded image:tag>`.
A node's deployment can pin BIOFLOW_TAG to a real version -- provisioning's
own `_render_node_env` writes BIOFLOW_TAG into the node's `.env` next to its
docker-compose.yml -- and a tag hardcoded here would silently update a pinned
node to the wrong image, the same bug already found and fixed for the
worker's own digest probe (see worker.py's _own_image_digest). `docker
compose pull` reads the compose file's `image: ...${BIOFLOW_TAG:-latest}`
directive and resolves BIOFLOW_TAG from the node's own .env, exactly as the
restart phase's own `docker compose up` does below -- so this needs no image
reference of its own, and the primary never needs to know what tag any given
node runs.
"""

import asyncio
from datetime import UTC, datetime

import asyncssh

from app.logging import get_logger
from app.models.node import Node
from app.models.node_update import NodeUpdateTask
from app.services.ai import crypto
from app.services.node_ssh import connect_with_tofu

log = get_logger(__name__)

INSTALL_DIR = "~/.bioflow"

_VERIFY_TIMEOUT_SECONDS = 120
_DRAIN_TIMEOUT_SECONDS = 900
_POLL_INTERVAL_SECONDS = 5


async def run_update(task_id: str, node: Node, drain: bool) -> None:
    """Execute one node update, recording progress on the task document."""
    try:
        task = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == task_id)
    except Exception:
        log.exception("update_task_load_failed", task_id=task_id)
        return
    if task is None:
        log.warning("update_task_missing", task_id=task_id)
        return

    async def _phase(phase: str, message: str, pct: float | None = None) -> None:
        task.phase = phase
        task.message = message
        task.pct = pct
        await task.save()

    async def _fail(phase: str, reason: str) -> None:
        task.status = "failed"
        task.phase = phase
        task.error = reason
        task.message = reason
        task.finished_at = datetime.now(UTC)
        try:
            await task.save()
        except Exception:
            # A failed update must not raise a second time while recording
            # the first failure -- this is the fire-and-forget background
            # task's own last line of defense, so log-and-swallow here
            # rather than let a Mongo hiccup during error handling become
            # an unretrieved asyncio.Task exception.
            log.exception(
                "update_task_save_failed", task_id=task_id, phase=phase
            )
            return
        log.warning("node_update_failed", task_id=task_id, phase=phase, reason=reason)

    conn = None
    try:
        # ---- connect ----
        await _phase("connect", f"Connecting to {node.ssh_host}…", 5)
        private_pem = crypto.decrypt(node.ssh_key_enc) if node.ssh_key_enc else None
        if not private_pem:
            return await _fail(
                "connect",
                "The stored update key could not be decrypted. Re-provision this node.",
            )
        if not node.ssh_host or not node.ssh_username:
            return await _fail(
                "connect",
                "Node has no SSH host or username configured. Re-provision this node.",
            )
        try:
            conn, _ = await connect_with_tofu(
                node.ssh_host,
                node.ssh_port,
                node.ssh_username,
                private_pem,
                stored_host_key=node.host_key,
                timeout_seconds=20,
            )
        except (TimeoutError, asyncssh.Error, ValueError) as e:
            return await _fail(
                "connect",
                f"Could not reach {node.ssh_host}: {e}. "
                "The machine may be off, or the update key may have been removed.",
            )

        # ---- pull (before stopping anything) ----
        await _phase("pull_image", "Pulling the new image…", 20)
        result = await asyncio.wait_for(
            conn.run(
                f"docker compose -f {INSTALL_DIR}/docker-compose.yml pull worker",
                check=False,
            ),
            timeout=1800,
        )
        if result.exit_status != 0:
            return await _fail(
                "pull_image",
                f"Image pull failed: {result.stderr or result.stdout or 'no output'}",
            )

        # ---- drain ----
        if drain:
            await _phase("drain", "Waiting for running jobs to finish…", 40)
            await asyncio.wait_for(
                conn.run(
                    f"docker compose -f {INSTALL_DIR}/docker-compose.yml stop -t "
                    f"{_DRAIN_TIMEOUT_SECONDS} worker",
                    check=False,
                ),
                timeout=_DRAIN_TIMEOUT_SECONDS + 60,
            )
            await _await_drained(node.node_id)

        # ---- restart ----
        #
        # `--no-deps worker` because this runs against whatever compose file
        # provisioning left behind, and a launcher-provisioned node has the
        # *full* stack file there (the launcher copies docker-compose.yml
        # verbatim and starts only the worker). A bare `up -d` would start
        # mongo, redis, api, and web on the compute node -- a second database
        # nobody asked for, with the worker still pointed at the primary's.
        await _phase("restart", "Starting the updated worker…", 70)
        result = await asyncio.wait_for(
            conn.run(
                f"docker compose -f {INSTALL_DIR}/docker-compose.yml "
                "up -d --no-deps worker",
                check=False,
            ),
            timeout=120,
        )
        if result.exit_status != 0:
            return await _fail(
                "restart",
                f"Worker failed to start: {result.stderr or result.stdout}",
            )

        # ---- verify ----
        await _phase("verify", "Waiting for the updated worker to report in…", 85)
        new_digest = await _await_digest(node.node_id, node.image_digest)
        if new_digest is None:
            return await _fail(
                "verify",
                f"The updated worker did not report in within "
                f"{_VERIFY_TIMEOUT_SECONDS}s. It may be failing to start -- "
                f"check `docker compose logs worker` on {node.ssh_host}.",
            )

        task.status = "success"
        task.phase = "done"
        task.to_digest = new_digest
        task.pct = 100
        task.message = "Node updated ✓"
        task.finished_at = datetime.now(UTC)
        await task.save()
        log.info("node_updated", node_id=node.node_id, digest=new_digest)

    except Exception as e:  # noqa: BLE001 - a failed update must not kill the API
        log.exception("node_update_error", task_id=task_id)
        await _fail(task.phase or "unknown", str(e))
    finally:
        if conn is not None:
            conn.close()


async def _await_drained(node_id: str) -> bool:
    """Wait for the node's workers to stop reporting running jobs."""
    # Deferred: nodes.py is expected to import run_update once an endpoint
    # wires it up, which would make a module-level import here circular.
    from app.api.v1.nodes import enumerate_nodes

    deadline = asyncio.get_running_loop().time() + _DRAIN_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        nodes = await enumerate_nodes()
        entry = nodes.get(node_id)
        if entry is None or entry.get("running_jobs", 0) == 0:
            return True
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return False


async def _await_digest(node_id: str, previous: str | None) -> str | None:
    """Wait for a worker on `node_id` to report a digest other than `previous`.

    Returns the new digest, or None if none arrived in time.
    """
    deadline = asyncio.get_running_loop().time() + _VERIFY_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        node = await Node.find_one(Node.node_id == node_id)
        if node and node.image_digest and node.image_digest != previous:
            return node.image_digest
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return None
