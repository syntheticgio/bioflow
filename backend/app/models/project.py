"""Projects: the organizational unit shown in the left explorer panel."""

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models.base import TimestampedDocument


class ProjectCounters(BaseModel):
    """Denormalized rollups, maintained with $inc.

    Recomputing these from the objects collection on every list render would be
    a per-project aggregation; at explorer-refresh frequency that is wasteful.
    """

    object_count: int = 0
    total_bytes: int = 0


class Project(TimestampedDocument):
    name: str
    slug: str
    description: str = ""

    # Reserved for nested projects. `path` is a materialized ancestor list,
    # which makes breadcrumbs a single document read instead of a walk.
    parent_id: PydanticObjectId | None = None
    path: list[PydanticObjectId] = Field(default_factory=list)

    metadata: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    counters: ProjectCounters = Field(default_factory=ProjectCounters)
    archived: bool = False

    class Settings:
        name = "projects"
        indexes = [
            # No duplicate sibling names within one parent.
            IndexModel(
                [("owner", ASCENDING), ("parent_id", ASCENDING), ("name", ASCENDING)],
                unique=True,
                name="uniq_sibling_name",
            ),
            IndexModel([("owner", ASCENDING), ("updated_at", DESCENDING)], name="recent"),
            IndexModel([("path", ASCENDING)], name="ancestors"),
            IndexModel([("slug", ASCENDING)], name="slug"),
        ]
