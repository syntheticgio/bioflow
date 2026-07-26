"""Object endpoints."""

from pathlib import Path

from beanie import PydanticObjectId
from fastapi import APIRouter, status

from app.api.v1.schemas import BlobOut, ObjectDetail, ObjectOut, ObjectUpdate
from app.errors import ValidationError
from app.models import BlobStorage, JobClass
from app.services import object_service

router = APIRouter(prefix="/objects", tags=["objects"])


@router.get("/{object_id}", response_model=ObjectDetail)
async def get_object(object_id: PydanticObjectId) -> ObjectDetail:
    obj, blob = await object_service.object_with_blob(object_id)
    return ObjectDetail(
        **ObjectOut.of(obj).model_dump(),
        blob=BlobOut.of(blob) if blob else None,
    )


@router.patch("/{object_id}", response_model=ObjectOut)
async def update_object(object_id: PydanticObjectId, body: ObjectUpdate) -> ObjectOut:
    obj = await object_service.update_object(object_id, body.model_dump(exclude_unset=True))
    return ObjectOut.of(obj)


@router.post("/{object_id}/reingest", status_code=status.HTTP_202_ACCEPTED)
async def reingest_object(object_id: PydanticObjectId) -> dict:
    """Re-run format detection and header parsing.

    Useful after a parser improves, or when a file was ingested while its
    format was not yet recognized. Runs at user-interactive priority because
    someone clicked a button and is waiting.
    """
    obj, blob = await object_service.object_with_blob(object_id)
    if blob is None or not obj.blob_sha256:
        raise ValidationError("Object has no stored content to re-ingest yet")

    job_id = await object_service.enqueue_ingest(
        obj,
        digest=obj.blob_sha256 if blob.storage is BlobStorage.MANAGED else None,
        path=Path(blob.external_path) if blob.external_path else None,
        job_class=JobClass.USER_INTERACTIVE,
    )
    return {"object_id": str(object_id), "job_id": job_id}


@router.delete("/{object_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_object(object_id: PydanticObjectId) -> None:
    """Detach the object. The blob's bytes are unlinked later by GC, once its
    refcount has been zero past the grace window."""
    await object_service.delete_object(object_id)
