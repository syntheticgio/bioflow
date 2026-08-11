"""Node provisioning task tracking."""

from datetime import datetime
from uuid import uuid4

from beanie import Document
from beanie.odm.fields import Indexed
from pydantic import Field


class NodeProvisionTask(Document):
    """One node provisioning attempt, tracked so the frontend can poll progress."""

    task_id: Indexed(str, unique=True) = Field(default_factory=lambda: uuid4().hex[:12])
    status: str = "provisioning"  # "provisioning" | "success" | "failed"
    phase: str = ""  # current phase: validate_ssh, verify_docker, ...
    message: str = ""
    pct: float | None = None  # percentage during pull_image
    node_name: str = ""
    host: str = ""
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    error: str | None = None

    class Settings:
        name = "node_provisions"
        indexes = [
            "task_id",
        ]
