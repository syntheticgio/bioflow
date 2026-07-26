"""Project endpoints, including the in-project upload path."""

from urllib.parse import unquote

from beanie import PydanticObjectId
from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel

from app.api.v1.schemas import (
    ObjectOut,
    ProjectCreate,
    ProjectDetail,
    ProjectOut,
    ProjectUpdate,
)
from app.errors import ValidationError
from app.models import ObjectStatus
from app.services import object_service, project_service

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    parent_id: str | None = None,
    include_archived: bool = False,
    limit: int = Query(200, le=1000),
) -> list[ProjectOut]:
    parent = PydanticObjectId(parent_id) if parent_id else None
    projects = await project_service.list_projects(
        parent_id=parent, include_archived=include_archived, limit=limit
    )
    return [ProjectOut.of(p) for p in projects]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate) -> ProjectOut:
    project = await project_service.create_project(
        name=body.name,
        description=body.description,
        parent_id=PydanticObjectId(body.parent_id) if body.parent_id else None,
        metadata=body.metadata,
        tags=body.tags,
    )
    return ProjectOut.of(project)


@router.get("/{project_id}", response_model=ProjectDetail)
async def get_project(project_id: PydanticObjectId) -> ProjectDetail:
    project = await project_service.get_project(project_id)
    trail = await project_service.breadcrumbs(project)
    return ProjectDetail(**ProjectOut.of(project).model_dump(), breadcrumbs=trail)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(project_id: PydanticObjectId, body: ProjectUpdate) -> ProjectOut:
    project = await project_service.update_project(
        project_id, body.model_dump(exclude_unset=True)
    )
    return ProjectOut.of(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: PydanticObjectId, cascade: bool = False) -> None:
    await project_service.delete_project(project_id, cascade=cascade)


@router.get("/{project_id}/objects", response_model=list[ObjectOut])
async def list_project_objects(
    project_id: PydanticObjectId,
    obj_status: ObjectStatus | None = Query(None, alias="status"),
    limit: int = Query(200, le=1000),
) -> list[ObjectOut]:
    await project_service.get_project(project_id)  # 404 if the project is gone
    objects = await object_service.list_objects(project_id, status=obj_status, limit=limit)
    return [ObjectOut.of(o) for o in objects]


class RegisterInPlace(BaseModel):
    path: str
    name: str | None = None


class RegisterAccepted(BaseModel):
    object: ObjectOut
    job_id: str


@router.post(
    "/{project_id}/objects/register",
    response_model=RegisterAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def register_object(
    project_id: PydanticObjectId, body: RegisterInPlace
) -> RegisterAccepted:
    """Register a file already on disk, without copying it.

    Returns 202: the file is recorded immediately, but hashing a large file runs
    on the queue. The object reaches `ready` once that finishes.
    """
    obj, job_id = await object_service.register_in_place(
        project_id=project_id, path_str=body.path, name=body.name
    )
    return RegisterAccepted(object=ObjectOut.of(obj), job_id=job_id)


@router.post(
    "/{project_id}/objects/upload",
    response_model=ObjectOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_object(
    project_id: PydanticObjectId,
    request: Request,
) -> ObjectOut:
    """Simple streamed upload (Phase 0; capped, see MAX_SIMPLE_UPLOAD_BYTES).

    The body is the raw file bytes and the name arrives in a header. Multipart
    parsing is avoided deliberately: python-multipart spools to a temp file and
    buffers, which wastes an entire extra copy of the payload.
    """
    raw_filename = request.headers.get("X-Filename")
    if not raw_filename:
        raise ValidationError("Missing required X-Filename header")
    # Percent-encoded by the client: HTTP headers are latin-1, and genomics
    # filenames routinely carry characters outside it.
    filename = unquote(raw_filename)

    # Bridge the async request stream into the sync iterator that the hashing
    # thread consumes, with a small buffer for backpressure.
    obj = await object_service.ingest_stream(
        project_id=project_id,
        filename=filename,
        stream=_SyncStreamBridge(request.stream()),
    )
    return ObjectOut.of(obj)


class _SyncStreamBridge:
    """Expose an async byte iterator to a worker thread as a sync iterator.

    The consumer runs via asyncio.to_thread, so it cannot await. Each chunk is
    pulled back onto the loop with run_coroutine_threadsafe.
    """

    def __init__(self, async_iter):
        import asyncio

        self._it = async_iter.__aiter__()
        self._loop = asyncio.get_running_loop()

    def __iter__(self):
        import asyncio

        while True:
            future = asyncio.run_coroutine_threadsafe(self._next(), self._loop)
            chunk = future.result()
            if chunk is None:
                return
            if chunk:
                yield chunk

    async def _next(self):
        try:
            return await self._it.__anext__()
        except StopAsyncIteration:
            return None
