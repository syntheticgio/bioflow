"""Compute node enrollment records.

Every physical machine that runs a BioFlow worker registers here when it
first enrolls.  The canonical status lives in MongoDB so revocations survive
restarts; live telemetry (heartbeat, running jobs, slots) stays in Redis where
it is cheap to publish and cheap to expire.

See docs/superpowers/specs/ for the full multi-node design.
"""

from datetime import UTC, datetime

from beanie import Document
from beanie.odm.fields import Indexed
from pydantic import Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Node(Document):
    """One physical machine running BioFlow worker(s).

    Keyed by ``node_id``, which MUST match ``settings.worker_node_id`` on
    every worker process that runs on this machine.  The launcher writes this
    value into ``.env`` at install time.
    """

    node_id: Indexed(str, unique=True)  # type: ignore[valid-type]
    hostname: str = ""
    last_seen: datetime | None = None
    registered_at: datetime = Field(default_factory=utcnow)
    status: str = "active"  # "active" | "revoked"

    # How to reach this node over SSH for updates. Null on nodes that
    # enrolled themselves rather than being provisioned from the UI --
    # those report a version but cannot be updated from here.
    ssh_host: str | None = None
    ssh_port: int = 22
    ssh_username: str | None = None
    ssh_key_enc: bytes | None = None  # Fernet; the managed key's private half
    ssh_key_installed_at: datetime | None = None
    # Pinned host key for TOFU (trust-on-first-use). Set during provisioning
    # and verified on every subsequent connection. None for nodes enrolled
    # before this field existed -- the next connection captures it.
    host_key: str | None = None

    # What this node is running, from the worker heartbeat. Persisted here
    # rather than only in Redis because Redis entries expire with the worker,
    # and an offline node's last-known version is exactly what someone
    # deciding whether to bring it up wants to see.
    image_digest: str | None = None
    version: str | None = None

    class Settings:
        name = "nodes"
