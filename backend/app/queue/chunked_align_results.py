"""Applier for chunked alignment: create merged BAM, chain post-alignment."""

from pathlib import Path

from beanie import PydanticObjectId

from app.logging import get_logger
from app.models.object import DataObject, ObjectRole

log = get_logger(__name__)


async def apply_chunked_alignment(result: dict, *, owner: str) -> None:
    """Take the merged BAM, create a DataObject, chain index/stats/headers."""
    from app.queue import queue
    from app.services import object_service

    output_path = Path(result["output_path"])
    project_id = PydanticObjectId(result["project_id"])
    reference_id = PydanticObjectId(result["reference_id"])
    reads_id = PydanticObjectId(result["reads_object_id"])
    name = result.get("output_name", output_path.name)
    aligner_name = result.get("aligner", "unknown")
    params = result.get("params", {})

    facts = {
        "aligned_by": aligner_name,
        "align_params": params,
        "chunked": True,
        "chunk_bucket_count": result.get("bucket_count", 0),
    }

    obj = await object_service.ingest_local_file(
        owner=owner,
        project_id=project_id,
        path=output_path,
        name=name,
        role=ObjectRole.ALIGNMENT,
        derived_from=[reads_id, reference_id],
        facts=facts,
    )

    # Chain post-alignment pipeline (same as _apply_align_reads)
    queue.enqueue(
        "index_bam",
        payload={"object_id": str(obj.id), "project_id": str(project_id)},
        owner=owner,
    )
    queue.enqueue(
        "ingest_headers",
        payload={"object_id": str(obj.id), "project_id": str(project_id)},
        owner=owner,
    )
    queue.enqueue(
        "run_bam_stats",
        payload={"object_id": str(obj.id), "project_id": str(project_id)},
        owner=owner,
    )

    log.info(
        "chunked_alignment_applied",
        object_id=str(obj.id),
        bucket_count=result.get("bucket_count"),
    )
