"""Create, list, and download project export archives."""

import re

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.api.deps import OwnerDep
from app.config import settings
from app.services import pipeline_service

router = APIRouter(tags=["exports"])

# An export filename is "<slug>-<timestamp>.tar.gz" and nothing else. The
# download route joins this onto a directory, so anything else is a
# traversal attempt, not a typo.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}\.tar\.gz$")


@router.post("/projects/{project_id}/export", status_code=202)
async def create_export(
    project_id: PydanticObjectId,
    owner: OwnerDep,
    threshold_bytes: int | None = None,
) -> dict:
    job = await pipeline_service.launch_project_export(
        project_id=project_id, owner=owner, threshold_bytes=threshold_bytes
    )
    return {"job_id": str(job.id)}


@router.get("/exports")
async def list_exports(owner: OwnerDep) -> list[dict]:
    # KNOWN GAP: not owner-scoped. `owner` is accepted (every route under
    # this router takes it, and OwnerDep is what makes the create endpoint
    # 404 on another profile's project) but unused here -- archive filenames
    # carry no owner-derived component and there is no sidecar record
    # associating a file with the profile that created it, so every profile
    # can currently list every other profile's export archives. Fixing this
    # properly means changing the archive-naming convention or adding an
    # owner-tracking sidecar, both of which touch already-reviewed
    # archive-naming logic -- tracked as
    # https://github.com/syntheticgio/bioflow/issues/536 (I5 in the final
    # whole-branch review) rather than done here.
    if not settings.exports_dir.exists():
        return []
    return sorted(
        (
            {
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "created_at": p.stat().st_mtime,
            }
            for p in settings.exports_dir.glob("*.tar.gz")
        ),
        key=lambda e: e["created_at"],
        reverse=True,
    )


@router.get("/exports/{name}/download")
async def download_export(name: str, owner: OwnerDep) -> FileResponse:
    # KNOWN GAP: not owner-scoped, same as list_exports above -- `owner` is
    # accepted and unused. See the note there for why and the tracking
    # issue.
    if not _SAFE_NAME.match(name):
        raise HTTPException(status_code=400, detail="Invalid export name")
    path = settings.exports_dir / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    return FileResponse(path, media_type="application/gzip", filename=name)
