"""Project-level operation endpoints.

Operations are project-scoped actions that don't require selecting a specific
file first. They live under /projects/{project_id}/operations/.
"""

import asyncio
import shutil
from pathlib import Path

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import OwnerDep
from app.errors import ValidationError
from app.logging import get_logger
from app.services import object_service, project_service

log = get_logger(__name__)

router = APIRouter(prefix="/projects/{project_id}/operations", tags=["operations"])


class MergeFastqRequest(BaseModel):
    object_ids: list[str] = Field(..., min_length=2, max_length=100)
    output_name: str = Field(..., min_length=1, max_length=255)


class BatchRenameRequest(BaseModel):
    renames: list[dict] = Field(..., min_length=1, max_length=100)


class BatchTagsRequest(BaseModel):
    object_ids: list[str] = Field(..., min_length=1, max_length=100)
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class QcAllRequest(BaseModel):
    pass


def _read_file_chunks(path: Path):
    """Yield chunks from a file for use with ingest_stream's _drain_to_temp."""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            yield chunk


@router.post("/merge-fastq", status_code=status.HTTP_202_ACCEPTED)
async def merge_fastq(
    project_id: PydanticObjectId,
    body: MergeFastqRequest,
    owner: OwnerDep,
) -> dict:
    """Concatenate multiple FASTQ files into one new file."""
    # Verify all objects exist, are FASTQ, and belong to this project
    oids = [PydanticObjectId(oid) for oid in body.object_ids]
    obj_map = await object_service.get_objects_by_ids(project_id, oids, owner=owner)

    for oid in body.object_ids:
        obj = obj_map.get(oid)
        if not obj:
            raise ValidationError(f"Object {oid} not found in project")
        if obj.format.kind != "fastq":
            raise ValidationError(f"Object {obj.name} is not a FASTQ file")
        if obj.status != "ready":
            raise ValidationError(f"Object {obj.name} is not ready (status: {obj.status})")

    # Resolve blob paths
    from app.storage.paths import blob_path

    input_paths = []
    for oid in body.object_ids:
        obj = obj_map[oid]
        if not obj.blob_sha256:
            raise ValidationError(f"Object {obj.name} has no blob (not yet stored)")
        input_paths.append(blob_path(obj.blob_sha256))

    for p in input_paths:
        if not p.exists():
            raise ValidationError(f"Blob not found on disk: {p}")

    # Concatenate files in a thread (cat is fast, but multi-GB files need offloading)
    def _concatenate():
        tmp_dir = Path("/tmp/bioflow-merge-fastq")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        out = tmp_dir / body.output_name
        with open(out, "wb") as f_out:
            for p in input_paths:
                with open(p, "rb") as f_in:
                    shutil.copyfileobj(f_in, f_out)
        return out

    output_path = await asyncio.to_thread(_concatenate)

    try:
        # Register the merged file as a new object via ingest_stream
        obj = await object_service.ingest_stream(
            owner=owner,
            project_id=project_id,
            filename=body.output_name,
            stream=_read_file_chunks(output_path),
        )

        log.info(
            "Merged %d files into %s (id=%s, size=%d)",
            len(input_paths),
            obj.name,
            str(obj.id),
            obj.size,
        )

        return {
            "object_id": str(obj.id),
            "name": obj.name,
            "size": obj.size,
        }
    finally:
        # Clean up temp file
        if output_path.exists():
            output_path.unlink()


@router.post("/batch-rename", status_code=status.HTTP_200_OK)
async def batch_rename(
    project_id: PydanticObjectId,
    body: BatchRenameRequest,
    owner: OwnerDep,
) -> dict:
    """Rename multiple files at once."""
    updated = 0
    for rename in body.renames:
        obj_id = rename.get("id")
        name = rename.get("name")
        if not obj_id or not name:
            continue
        await object_service.update_object(
            PydanticObjectId(obj_id),
            {"name": name},
            owner=owner,
        )
        updated += 1

    return {"updated": updated}


@router.post("/batch-tags", status_code=status.HTTP_200_OK)
async def batch_tags(
    project_id: PydanticObjectId,
    body: BatchTagsRequest,
    owner: OwnerDep,
) -> dict:
    """Add/remove tags on multiple files. Reuses the existing bulk-tags service."""
    from app.services import search_service

    result = await search_service.bulk_update_tags(
        owner=owner,
        object_ids=[PydanticObjectId(oid) for oid in body.object_ids],
        add=body.add,
        remove=body.remove,
    )
    return {"updated": result}


@router.get("/export", status_code=status.HTTP_200_OK)
async def export_project(
    project_id: PydanticObjectId,
    owner: OwnerDep,
) -> dict:
    """Generate and return a project summary."""
    project = await project_service.get_project(project_id, owner=owner)
    objects = await object_service.list_objects(project_id, owner=owner)

    return {
        "project_name": project.name,
        "project_description": project.description,
        "created_at": project.created_at.isoformat(),
        "total_files": len(objects),
        "total_bytes": sum(o.size for o in objects),
        "files_by_format": _count_by(objects, lambda o: o.format.kind),
        "files_by_status": _count_by(objects, lambda o: o.status),
    }


@router.post("/qc-all", status_code=status.HTTP_202_ACCEPTED)
async def qc_all_reads(
    project_id: PydanticObjectId,
    body: QcAllRequest,
    owner: OwnerDep,
) -> dict:
    """Queue QC jobs for all read files in the project."""
    from app.queue import queue

    objects = await object_service.list_objects(project_id, owner=owner)
    reads = [
        o for o in objects
        if o.format.kind == "fastq" and o.status == "ready"
    ]

    job_ids = []
    for read in reads:
        job = await queue.enqueue(
            job_type="run_qc",
            payload={"object_id": str(read.id)},
            owner=owner,
        )
        job_ids.append(str(job.id))

    return {"job_ids": job_ids, "count": len(job_ids)}


def _count_by(items, key_fn):
    counts = {}
    for item in items:
        k = key_fn(item)
        counts[k] = counts.get(k, 0) + 1
    return counts
