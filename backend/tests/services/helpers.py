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

    obj = DataObject(
        project_id=project.id,
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
    )
    await job.insert()
    return job
