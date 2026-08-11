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

    class Settings:
        name = "nodes"
