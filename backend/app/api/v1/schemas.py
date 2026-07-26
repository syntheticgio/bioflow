"""Request/response models for the v1 API.

Response models are explicit rather than returning documents directly, so the
wire contract does not drift silently when a storage field is added.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models import Blob, DataObject, ObjectRole, Project


# --- Projects ---
class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    parent_id: str | None = None
    metadata: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    metadata: dict | None = None
    tags: list[str] | None = None
    archived: bool | None = None


class ProjectOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str
    parent_id: str | None
    metadata: dict
    tags: list[str]
    object_count: int
    total_bytes: int
    archived: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, p: Project) -> "ProjectOut":
        return cls(
            id=str(p.id),
            name=p.name,
            slug=p.slug,
            description=p.description,
            parent_id=str(p.parent_id) if p.parent_id else None,
            metadata=p.metadata,
            tags=p.tags,
            object_count=p.counters.object_count,
            total_bytes=p.counters.total_bytes,
            archived=p.archived,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )


class ProjectDetail(ProjectOut):
    breadcrumbs: list[dict] = Field(default_factory=list)


# --- Objects ---
class ObjectUpdate(BaseModel):
    name: str | None = None
    metadata: dict | None = None
    tags: list[str] | None = None
    # An explicit null clears the role ("convert back to reads"); omitting the
    # key leaves it untouched. exclude_unset=True in the route preserves the
    # difference.
    role: ObjectRole | None = None


class BlobOut(BaseModel):
    sha256: str
    size: int
    state: str
    storage: str
    rel_path: str | None
    external_path: str | None
    ref_count: int
    last_verified_at: datetime | None

    @classmethod
    def of(cls, b: Blob) -> "BlobOut":
        return cls(
            sha256=b.id,
            size=b.size,
            state=b.state.value,
            storage=b.storage.value,
            rel_path=b.rel_path,
            external_path=b.external_path,
            ref_count=b.ref_count,
            last_verified_at=b.last_verified_at,
        )


class ObjectOut(BaseModel):
    id: str
    project_id: str
    name: str
    size: int
    status: str
    blob_sha256: str | None
    format: dict
    facts: dict
    metadata: dict
    tags: list[str]
    source: dict
    error: dict | None
    role: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, o: DataObject) -> "ObjectOut":
        return cls(
            id=str(o.id),
            project_id=str(o.project_id),
            name=o.name,
            size=o.size,
            status=o.status.value,
            blob_sha256=o.blob_sha256,
            format=o.format.model_dump(mode="json"),
            facts=o.facts,
            metadata=o.metadata,
            tags=o.tags,
            source=o.source.model_dump(mode="json"),
            error=o.error.model_dump(mode="json") if o.error else None,
            role=o.role.value if o.role else None,
            created_at=o.created_at,
            updated_at=o.updated_at,
        )


class ObjectDetail(ObjectOut):
    blob: BlobOut | None = None


# --- System ---
class HealthOut(BaseModel):
    status: str
    checks: dict
