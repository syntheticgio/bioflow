"""sequence_extraction: extract specific sequence names or region coordinates
from an assembly FASTA using `seqkit subseq`.

Imported by `handlers.py` for `@handler` registration.
"""

from datetime import UTC, datetime
from pathlib import Path

from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import sequence_extraction_runner, tools
from app.queue.align_handlers import _resolve_blob
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler
from app.services import blob_service

log = get_logger(__name__)


@handler(
    "sequence_extraction",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.HEAVY),
    max_attempts=2,
)
def run_sequence_extraction(ctx: JobContext) -> dict:
    """Sequence extraction using seqkit subseq."""
    seqkit = tools.require(tools.seqkit())

    assembly_id = ctx.payload.get("assembly_id")
    if not assembly_id:
        raise PermanentError("sequence_extraction requires 'assembly_id'")

    regions_raw = ctx.payload.get("regions")
    if not regions_raw or not isinstance(regions_raw, list):
        raise PermanentError("sequence_extraction requires a non-empty 'regions' list")

    regions: list[tuple[str, int, int]] = [
        (r[0], int(r[1]), int(r[2])) for r in regions_raw
    ]

    work = _prepare_workdir(ctx, "sequence_extraction")

    assembly_name = Path(ctx.payload.get("assembly_name") or "assembly.fasta").name
    assembly = work / assembly_name
    assembly.unlink(missing_ok=True)
    assembly.symlink_to(_resolve_blob(ctx.payload, "assembly"))

    bed_file = work / "regions.bed"
    sequence_extraction_runner.write_bed_file(regions, bed_file)

    out_fasta = work / "extracted.fasta"

    ctx.progress(phase="extracting", pct=0.3, message="extracting sequences with seqkit")
    cmd = sequence_extraction_runner.build_command(fasta=assembly, bed_file=bed_file)
    log_path = work / "seqkit.log"
    code = run_subprocess(ctx, cmd, stdout_path=str(out_fasta), log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "seqkit subseq")

    if not out_fasta.is_file() or out_fasta.stat().st_size == 0:
        raise PermanentError("seqkit subseq produced an empty output file")

    ctx.progress(phase="ingesting", pct=0.8, message="registering extracted FASTA blob")
    sha256 = blob_service.ingest_blob_sync(out_fasta)

    output_name = ctx.payload.get("output_name") or f"extracted_{assembly_name}"

    facts = {
        "sequence_extraction_status": "ok",
        "sequence_extraction_tool_version": seqkit.version,
        "sequence_extraction_computed_at": datetime.now(UTC).isoformat(),
        "sequence_extraction_region_count": len(regions),
    }

    ctx.progress(phase="done", pct=1.0, message="extraction complete")
    log.info(
        "sequence_extraction_finished",
        job_id=ctx.job_id,
        assembly_id=assembly_id,
        sha256=sha256,
        size_bytes=out_fasta.stat().st_size,
    )

    return {
        "object_id": assembly_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "blob_sha256": sha256,
        "size_bytes": out_fasta.stat().st_size,
        "output_name": output_name,
        "source_annotation_id": ctx.payload.get("annotation_id"),
        "query_summary": ctx.payload.get("query_summary"),
        "workdir": str(work),
    }
