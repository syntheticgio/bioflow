"""Factories for deletion tests.

Objects are created directly rather than through object_service.register_*,
which would require real bytes on disk and a running CAS. Deletion only cares
about the document graph and the blob refcount, both of which are set here
exactly as the ingest path sets them.
"""

from datetime import UTC, datetime

from beanie import PydanticObjectId

from app.models import (
    Blob,
    BlobStorage,
    DataObject,
    Job,
    JobState,
    ObjectStatus,
    Project,
    SidecarRole,
)
from app.services import project_service

# These factories exist for deletion-cascade tests, which care about the
# document graph and refcounts rather than the owner boundary -- that boundary
# has its own negative tests in test_project_service_owner.py. A default keeps
# those call sites reading about what they actually assert.
TEST_OWNER = "test-owner"


async def make_project(
    name: str, parent: Project | None = None, *, owner: str = TEST_OWNER
) -> Project:
    return await project_service.create_project(
        name=name, owner=owner, parent_id=parent.id if parent else None
    )


async def make_blob(digest: str, *, ref_count: int = 1) -> Blob:
    blob = Blob(
        id=digest,
        size=100,
        ref_count=ref_count,
        storage=BlobStorage.MANAGED,
        created_at=datetime.now(UTC),
    )
    await blob.insert()
    return blob


async def make_object(
    project: Project,
    name: str,
    *,
    size: int = 100,
    digest: str | None = None,
    sidecar_of: PydanticObjectId | None = None,
    sidecar_role: SidecarRole | None = None,
) -> DataObject:
    """An object plus the blob it references, with refcount 1.

    `digest` defaults to a unique per-object value; pass an explicit one to
    model two objects sharing content.
    """
    if digest is None:
        digest = f"{abs(hash(name)):064x}"[:64]
    if await Blob.get(digest) is None:
        await make_blob(digest)

    # Inherit the project's owner rather than the model default: the deletion
    # queries are owner-scoped now, so an object left at "local" under a
    # "test-owner" project is invisible to the cascade under test.
    obj = DataObject(
        project_id=project.id,
        owner=project.owner,
        name=name,
        size=size,
        blob_sha256=digest,
        status=ObjectStatus.READY,
        sidecar_of=sidecar_of,
        sidecar_role=sidecar_role,
    )
    await obj.insert()
    return obj


async def make_job(project: Project, job_type: str, state: str) -> Job:
    job = Job(
        type=job_type,
        state=JobState(state),
        project_id=project.id,
        owner=project.owner,
    )
    await job.insert()
    return job
