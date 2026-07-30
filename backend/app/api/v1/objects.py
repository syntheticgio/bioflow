"""Object endpoints."""

from pathlib import Path

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from fastapi.responses import FileResponse

from app.api.v1.schemas import BlobOut, ObjectDetail, ObjectOut, ObjectUpdate, PairRequest
from app.errors import NotFoundError, ValidationError
from app.models import BlobStorage, JobClass
from app.services import object_service
from app.storage.paths import blob_path

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


@router.post("/{object_id}/pair", response_model=ObjectOut)
async def pair_object(object_id: PydanticObjectId, body: PairRequest) -> ObjectOut:
    """Mark two reads files as paired-end mates.

    Its own endpoint rather than a field on PATCH because it writes both
    documents and validates across them -- a merge endpoint could leave one
    side pointing at a file that does not point back.
    """
    obj = await object_service.set_pair(object_id, body.mate_object_id, body.read_number)
    return ObjectOut.of(obj)


@router.delete("/{object_id}/pair", response_model=ObjectOut)
async def unpair_object(object_id: PydanticObjectId) -> ObjectOut:
    """Undo a pairing from either side. Idempotent."""
    obj = await object_service.clear_pair(object_id)
    return ObjectOut.of(obj)


@router.get("/{object_id}/download")
async def download_object(object_id: PydanticObjectId) -> FileResponse:
    """Serve the object's raw bytes as an attachment.

    The file is returned exactly as stored -- still gzipped if it was ingested
    gzipped -- because the point of this route is to get the original back out,
    not a re-encoded copy. `name` is what the user called the file, so it is
    what the download is named; the digest is an implementation detail of where
    we put it.

    Managed and external blobs both resolve here. Neither path is
    caller-controlled: a managed path is built from the validated digest, and
    an external path was checked against the register allowlist at ingest, so
    there is no user-supplied path segment to contain.
    """
    obj, blob = await object_service.object_with_blob(object_id)
    if blob is None or not obj.blob_sha256:
        raise NotFoundError("Object has no stored content to download yet")

    if blob.storage is BlobStorage.EXTERNAL:
        if not blob.external_path:
            raise NotFoundError("External blob has no recorded path")
        target = Path(blob.external_path)
    else:
        target = blob_path(obj.blob_sha256)

    # An external drive can be unmounted, and GC can win a race against a stale
    # page, so presence is checked rather than assumed. Reporting this as a 404
    # beats letting FileResponse raise once the response has already begun.
    if not target.is_file():
        raise NotFoundError(f"Stored content is not available: {obj.name}")

    return FileResponse(
        target,
        # Deliberately opaque: these are FASTQ/BAM/VCF payloads to hand to the
        # user's own tools, and guessing a renderable type for a file whose
        # bytes came from outside is how a download becomes a page.
        media_type="application/octet-stream",
        filename=obj.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


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
