"""Request/response models for the v1 API.

Response models are explicit rather than returning documents directly, so the
wire contract does not drift silently when a storage field is added.
"""

from datetime import datetime

from beanie import PydanticObjectId
from pydantic import BaseModel, Field

from app.models import Blob, DataObject, ObjectRole, Profile, Project, Share
from app.models.profile import ProfileDisplay

DELETED_PROFILE_PLACEHOLDER = "(deleted profile)"


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


class PairRequest(BaseModel):
    """Pairing two reads files by hand.

    Separate from ObjectUpdate because pairing writes *two* documents and its
    central validation question -- is this candidate already attached to a
    third file -- cannot be answered from the single object a PATCH fetches.
    """

    mate_object_id: PydanticObjectId
    # Which half the *subject* is. The mate is always given the other one, so
    # two R1s cannot be produced by a well-formed request.
    read_number: int = Field(ge=1, le=2)


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
    derived_from: list[str]
    produced_by_job: str | None
    mate_object_id: str | None
    read_number: int | None
    sidecar_of: str | None
    sidecar_role: str | None
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
            derived_from=[str(p) for p in o.derived_from],
            produced_by_job=str(o.produced_by_job) if o.produced_by_job else None,
            mate_object_id=str(o.mate_object_id) if o.mate_object_id else None,
            read_number=o.read_number,
            sidecar_of=str(o.sidecar_of) if o.sidecar_of else None,
            sidecar_role=o.sidecar_role.value if o.sidecar_role else None,
            created_at=o.created_at,
            updated_at=o.updated_at,
        )


class ObjectDetail(ObjectOut):
    blob: BlobOut | None = None
    # Digest of the object's current facts and metadata. Compared client-side
    # against the digest stored with a narrative summary to tell one that still
    # describes the file from one written before the last QC or trim run.
    # Detail-only: the listing has no use for it and it costs a hash per row.
    summary_fingerprint: str | None = None


# --- System ---
class HealthOut(BaseModel):
    status: str
    checks: dict


class ActiveJobOut(BaseModel):
    id: str
    job_type: str
    state: str


class DeletionPreviewOut(BaseModel):
    """What deleting a project would destroy, and whether it may proceed."""

    project_ids: list[str]
    child_project_count: int
    object_count: int
    total_bytes: int
    run_count: int
    job_count: int
    upload_session_count: int
    active_jobs: list[ActiveJobOut]
    blocked: bool


# --- Profiles ---
class ProfileCreate(BaseModel):
    username: str
    password: str | None = None
    email: str | None = None
    # A claim the caller makes, not a fact -- `create_profile` checks it,
    # because the setup screen is reachable again from a stale tab and a second
    # adopter would hand an existing library to whoever asked last.
    is_first_boot: bool = False


class ProfileSelect(BaseModel):
    password: str | None = None


class ProfileOut(BaseModel):
    """A profile as the picker sees it.

    `has_password` rather than the hash: the picker needs to know whether to
    show the password prompt, and nothing on the client has any use for the
    stored value. Hand-enumerated like every other response model here, which
    is what keeps `password_hash` from arriving the day someone reaches for a
    generic serializer -- returning the document directly would publish it.
    """

    id: str
    username: str
    email: str | None
    display: ProfileDisplay
    details: dict
    has_password: bool
    adopted_legacy_owner: bool
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, p: Profile) -> "ProfileOut":
        return cls(
            id=str(p.id),
            username=p.username,
            email=p.email,
            display=p.display,
            details=p.details,
            has_password=p.password_hash is not None,
            adopted_legacy_owner=p.adopted_legacy_owner,
            last_used_at=p.last_used_at,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )


# --- Shares ---
class ShareCreate(BaseModel):
    object_id: str
    to_profile_id: str
    message: str | None = None


class ShareAccept(BaseModel):
    # Omitted: land in the lazily created "Shared with me" project.
    project_id: str | None = None


class SharePartyOut(BaseModel):
    """Just enough to name one side of a share in a list row.

    Deliberately not `ProfileOut`: an inbox row does not need `email`,
    `details`, or `has_password`, and sharing is not a reason to publish them.
    """

    owner: str
    username: str
    emoji: str
    colour: str

    @classmethod
    def of(cls, owner: str, profile: Profile | None) -> "SharePartyOut":
        # A missing profile (deleted between the offer and this read -- see
        # #51) renders as a placeholder rather than raising: an inbox that
        # 500s because one sender was deleted is worse than one odd-looking
        # row.
        if profile is None:
            return cls(owner=owner, username=DELETED_PROFILE_PLACEHOLDER, emoji="", colour="")
        return cls(
            owner=owner,
            username=profile.username,
            emoji=profile.display.emoji,
            colour=profile.display.colour,
        )


class ShareOut(BaseModel):
    """A share as either side of it sees it.

    Hand-enumerated like every other response model here. `name`/`size` come
    from the denormalized snapshot on `Share` itself, not from resolving
    `source_object_id` -- that is what lets an offer whose source the sender
    has since deleted still render in the inbox.

    `from_profile`/`to_profile` are resolved server-side rather than left for
    the client to join against `/profiles`: the adopted profile's owner string
    is the literal `"local"`, which matches no profile id, so a client-side
    join silently renders a blank sender for exactly the profile holding the
    pre-existing library. See `share_service.resolve_owner_profiles`.
    """

    id: str
    from_owner: str
    to_owner: str
    from_profile: SharePartyOut
    to_profile: SharePartyOut
    source_object_id: str
    name: str
    size: int
    state: str
    accepted_object_id: str | None
    message: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, s: Share, parties: dict[str, Profile] | None = None) -> "ShareOut":
        parties = parties or {}
        return cls(
            id=str(s.id),
            from_owner=s.from_owner,
            to_owner=s.to_owner,
            from_profile=SharePartyOut.of(s.from_owner, parties.get(s.from_owner)),
            to_profile=SharePartyOut.of(s.to_owner, parties.get(s.to_owner)),
            source_object_id=str(s.source_object_id),
            name=s.name,
            size=s.size,
            state=s.state.value,
            accepted_object_id=str(s.accepted_object_id) if s.accepted_object_id else None,
            message=s.message,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
