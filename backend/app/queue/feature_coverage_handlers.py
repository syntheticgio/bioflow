"""feature_coverage: per-feature read coverage for one BAM against one
annotation (GFF or BED), computed with `bedtools coverage`.

Split into its own module rather than folded into `align_handlers.py` or
`expression_handlers.py` because it belongs to neither: it is read-only like
`run_bam_stats` (no derived object, just a report plus summary facts merged
onto the BAM), but it consumes an annotation the way `quantify` does. The
runner underneath (`feature_coverage_runner`, Task 5) is pure functions only;
this module owns the subprocess calls and the workdir/blob-resolution seam,
mirroring the `bam_stats_runner` / `run_bam_stats` split.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import feature_coverage_runner, tools
from app.queue.align_handlers import _resolve_blob
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


@handler(
    "feature_coverage",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.HEAVY),
    # Deterministic failures (bad GFF, unsorted BAM) don't improve with
    # retries; one retry covers transient disk/exec noise.
    max_attempts=2,
)
def run_feature_coverage(ctx: JobContext) -> dict:
    """Per-feature read coverage for one BAM against one annotation.

    Read-only like bam_stats: no derived objects, one JSON report on disk
    plus summary facts merged onto the BAM by `_apply_feature_coverage`.
    """
    bedtools = tools.require(tools.bedtools())

    bam_id = ctx.payload.get("bam_id")
    if not bam_id:
        raise PermanentError("feature_coverage requires a 'bam_id'")

    annotation_id = ctx.payload.get("annotation_id")
    if not annotation_id:
        raise PermanentError("feature_coverage requires an 'annotation_id'")

    annotation_format = ctx.payload.get("annotation_format")
    if annotation_format not in ("gff", "bed"):
        raise PermanentError(
            "feature_coverage requires 'annotation_format' to be 'gff' or 'bed'"
        )

    work = _prepare_workdir(ctx, "feature_coverage")

    bam_name = Path(ctx.payload.get("bam_name") or "aligned.bam").name
    bam = work / bam_name
    bam.unlink(missing_ok=True)
    bam.symlink_to(_resolve_blob(ctx.payload, "bam"))

    annotation_name = Path(
        ctx.payload.get("annotation_name") or f"annotation.{annotation_format}"
    ).name
    annotation = work / annotation_name
    annotation.unlink(missing_ok=True)
    annotation.symlink_to(_resolve_blob(ctx.payload, "annotation"))

    fai = work / "reference.fa.fai"
    fai.unlink(missing_ok=True)
    fai.symlink_to(_resolve_blob(ctx.payload, "fai"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    genome_file = feature_coverage_runner.build_genome_file(
        fai_path=fai, out_path=work / "ref.genome"
    )

    ctx.progress(phase="sorting", pct=0.2, message="sorting the annotation to the reference order")
    sorted_annotation = work / f"{annotation.stem}.sorted{annotation.suffix}"
    sort_cmd = ["bedtools", "sort", "-faidx", str(fai), "-i", str(annotation)]
    code = run_subprocess(ctx, sort_cmd, log_path=str(sorted_annotation))
    if code != 0:
        raise _failure(code, sorted_annotation, "bedtools sort")

    ctx.progress(phase="computing coverage", pct=0.5, message="computing per-feature coverage")
    coverage_path = work / "coverage.tsv"
    coverage_cmd = feature_coverage_runner.build_command(
        annotation=sorted_annotation, bam=bam, genome_file=genome_file
    )
    code = run_subprocess(ctx, coverage_cmd, log_path=str(coverage_path))
    if code != 0:
        raise _failure(code, coverage_path, "bedtools coverage")

    ctx.progress(phase="report", pct=0.9, message="writing the coverage report")
    report = feature_coverage_runner.parse_coverage(
        stdout_path=coverage_path, annotation_format=annotation_format
    )

    report_dir = settings.feature_coverage_dir / str(bam_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_name = "coverage.json"
    (report_dir / report_name).write_text(json.dumps(report))

    facts = {
        "feature_coverage_status": "ok",
        "feature_coverage_tool_version": bedtools.version,
        "feature_coverage_computed_at": datetime.now(UTC).isoformat(),
        "feature_coverage_feature_count": report["feature_count"],
        "feature_coverage_zero_features": report["features_zero_coverage"],
        "feature_coverage_median_breadth": report["median_breadth"],
        "feature_coverage_annotation_id": annotation_id,
        "feature_coverage_report": report_name,
    }

    ctx.progress(phase="done", pct=1.0, message="results complete")
    log.info(
        "feature_coverage_finished",
        job_id=ctx.job_id,
        bam_id=bam_id,
        annotation_id=annotation_id,
        feature_count=report["feature_count"],
        median_breadth=report["median_breadth"],
    )

    return {
        "object_id": bam_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }
