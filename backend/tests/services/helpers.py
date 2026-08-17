"""Factories for deletion tests.

Objects are created directly rather than through object_service.register_*,
which would require real bytes on disk and a running CAS. Deletion only cares
about the document graph and the blob refcount, both of which are set here
exactly as the ingest path sets them.
"""

from datetime import UTC, datetime

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
from beanie import PydanticObjectId

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

    # Setting `owner` here now matches production: Task 4 made
    # object_service's writers stamp it, so a real object carries its
    # project's owner rather than the "local" default from
    # TimestampedDocument. Runs are still the gap -- run_service does not set
    # it yet (Task 5) -- so a cascade over runs remains coverage of the
    # intended design rather than proof the partition holds end to end.
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
    # Unlike make_object above, this one is still forward-looking:
    # queue/queue.py does not set `owner` on real jobs yet (Task 8), so a real
    # job takes the "local" default whatever its project's owner is.
    job = Job(
        type=job_type,
        state=JobState(state),
        project_id=project.id,
        owner=project.owner,
    )
    await job.insert()
    return job
