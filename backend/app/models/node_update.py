"""Node update task tracking.

Mirrors NodeProvisionTask rather than sharing its collection: the phase
vocabularies differ, and a shared collection would make "when was this node
last updated" filter on a discriminator forever.
"""

from datetime import UTC, datetime
from uuid import uuid4

from beanie import Document
from beanie.odm.fields import Indexed
from pydantic import Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class NodeUpdateTask(Document):
    """One attempt to update a compute node's backend image."""

    task_id: Indexed(str, unique=True) = Field(  # type: ignore[valid-type]
        default_factory=lambda: uuid4().hex[:12]
    )
    status: str = "updating"  # "updating" | "success" | "failed"
    phase: str = ""  # connect, pull_image, drain, restart, verify, done
    message: str = ""
    pct: float | None = None
    node_id: Indexed(str) = ""  # type: ignore[valid-type]
    host: str = ""
    from_digest: str | None = None
    to_digest: str | None = None
    drain: bool = True
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    error: str | None = None

    class Settings:
        name = "node_updates"
        # No `indexes` entry: `task_id`'s `Indexed(str, unique=True)` and
        # `node_id`'s `Indexed(str)` annotations already declare their
        # indexes. A bare field-name string in Settings.indexes reaches
        # db.index_reconcile as a str (not an IndexModel) and raises
        # AttributeError on startup -- this is the exact bug NodeProvisionTask
        # had before it was fixed to rely on annotation-only indexes; see
        # Node.node_id for the established precedent.
