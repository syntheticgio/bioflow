"""Chunked, resumable upload endpoints."""

from datetime import datetime

from beanie import PydanticObjectId
from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import OwnerDep
from app.api.v1.schemas import ObjectOut
from app.errors import ValidationError
from app.models import UploadSession
from app.services import upload_service

router = APIRouter(prefix="/uploads", tags=["uploads"])


class UploadCreate(BaseModel):
    project_id: str
    filename: str
    total_size: int
    # Optional. Supplying it enables pre-flight deduplication: if the content is
    # already stored, the upload completes without transferring any bytes.
    client_sha256: str | None = None


class UploadSessionOut(BaseModel):
    id: str
    project_id: str
    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int
    state: str
    received_chunks: int
    received_bytes: int
    missing_chunks: list[int] = Field(default_factory=list)
    resulting_object_id: str | None = None
    resulting_sha256: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, s: UploadSession, *, include_missing: bool = True) -> "UploadSessionOut":
        missing = upload_service.missing_chunks(s) if include_missing else []
        return cls(
            id=str(s.id),
            project_id=str(s.project_id),
            filename=s.filename,
            total_size=s.total_size,
            chunk_size=s.chunk_size,
            total_chunks=s.total_chunks,
            state=s.state.value,
            received_chunks=len(s.received_chunks),
            received_bytes=s.received_bytes,
            # Capped: a client resuming a 10,000-chunk upload does not need the
            # whole list in one response to make progress.
            missing_chunks=missing[:500],
            resulting_object_id=str(s.resulting_object_id) if s.resulting_object_id else None,
            resulting_sha256=s.resulting_sha256,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )


class UploadCreated(BaseModel):
    """Either a new session, or a dedup hit that needs no transfer at all."""

    dedup_hit: bool = False
    session: UploadSessionOut | None = None
    object: ObjectOut | None = None


class ChunkAccepted(BaseModel):
    index: int
    received_chunks: int
    total_chunks: int
    received_bytes: int
    missing_count: int


class CompleteAccepted(BaseModel):
    session_id: str
    object_id: str
    job_id: str


@router.post("", response_model=UploadCreated, status_code=status.HTTP_201_CREATED)
async def create_upload(body: UploadCreate, owner: OwnerDep) -> UploadCreated:
    session, obj = await upload_service.create_session(
        project_id=PydanticObjectId(body.project_id),
        owner=owner,
        filename=body.filename,
        total_size=body.total_size,
        client_sha256=body.client_sha256,
    )
    if obj is not None:
        return UploadCreated(dedup_hit=True, object=ObjectOut.of(obj))
    return UploadCreated(dedup_hit=False, session=UploadSessionOut.of(session))


@router.get("", response_model=list[UploadSessionOut])
async def list_uploads(
    owner: OwnerDep,
    project_id: str | None = None,
    limit: int = Query(50, le=200),
) -> list[UploadSessionOut]:
    """Active sessions, so a client that reloaded can offer to resume."""
    sessions = await upload_service.list_active_sessions(
        PydanticObjectId(project_id) if project_id else None, owner=owner, limit=limit
    )
    return [UploadSessionOut.of(s) for s in sessions]


@router.get("/{session_id}", response_model=UploadSessionOut)
async def get_upload(session_id: PydanticObjectId, owner: OwnerDep) -> UploadSessionOut:
    """Resume source of truth: reports exactly which chunks are still missing."""
    return UploadSessionOut.of(await upload_service.get_session(session_id, owner=owner))


@router.put("/{session_id}/chunks/{index}", response_model=ChunkAccepted)
async def put_chunk(
    session_id: PydanticObjectId, index: int, request: Request, owner: OwnerDep
) -> ChunkAccepted:
    """Upload one chunk. The body is raw bytes.

    Deliberately not multipart: python-multipart spools the payload to a temp
    file and buffers it, which wastes an entire extra copy of every chunk.
    """
    body = await request.body()
    if not body:
        raise ValidationError("Chunk body is empty")

    # Scoped like the rest, and this is the sharpest case in the file: an
    # unscoped chunk write does not read another profile's upload, it writes
    # bytes *into* the file they are assembling, and the corruption only
    # surfaces later as a digest mismatch on their completed object.
    session = await upload_service.write_chunk(
        session_id,
        index,
        body,
        owner=owner,
        expected_sha256=request.headers.get("X-Chunk-SHA256"),
    )
    return ChunkAccepted(
        index=index,
        received_chunks=len(session.received_chunks),
        total_chunks=session.total_chunks,
        received_bytes=session.received_bytes,
        missing_count=len(upload_service.missing_chunks(session)),
    )


@router.post(
    "/{session_id}/complete",
    response_model=CompleteAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def complete_upload(session_id: PydanticObjectId, owner: OwnerDep) -> CompleteAccepted:
    """Finalize the session and enqueue assembly.

    Returns 202: assembling and hashing a large file takes minutes, so the work
    happens on the queue and the client follows the job.
    """
    session, obj, job_id = await upload_service.complete_session(session_id, owner=owner)
    return CompleteAccepted(
        session_id=str(session.id), object_id=str(obj.id), job_id=job_id
    )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def abort_upload(session_id: PydanticObjectId, owner: OwnerDep) -> Response:
    """Abort and purge the staging directory."""
    await upload_service.abort_session(session_id, owner=owner)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
