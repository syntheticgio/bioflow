"""Create, list, and download project export archives."""

import re

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.api.deps import OwnerDep
from app.config import settings
from app.services import pipeline_service

router = APIRouter(tags=["exports"])

# An export filename is "<owner>__<slug>-<timestamp>.tar.gz". The owner prefix
# is "local" or a 24-char hex ObjectId; the double-underscore delimiter is
# unambiguous since slugs use single underscores and ObjectIds are pure hex.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}\.tar\.gz$")


def _owner_prefix(owner: str) -> str:
    """Return the filename prefix for a given owner."""
    return f"{owner}__"


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
    if not settings.exports_dir.exists():
        return []
    prefix = _owner_prefix(owner)
    return sorted(
        (
            {
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "created_at": p.stat().st_mtime,
            }
            for p in settings.exports_dir.glob("*.tar.gz")
            if p.name.startswith(prefix)
        ),
        key=lambda e: e["created_at"],
        reverse=True,
    )


@router.get("/exports/{name}/download")
async def download_export(name: str, owner: OwnerDep) -> FileResponse:
    if not _SAFE_NAME.match(name):
        raise HTTPException(status_code=400, detail="Invalid export name")
    if not name.startswith(_owner_prefix(owner)):
        raise HTTPException(status_code=404, detail="Export not found")
    path = settings.exports_dir / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    return FileResponse(path, media_type="application/gzip", filename=name)
