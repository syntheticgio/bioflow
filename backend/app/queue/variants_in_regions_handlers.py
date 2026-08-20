"""variants_in_regions: variant distribution across annotated features
for one VCF against one annotation (GFF or BED), computed with `bedtools intersect`.

Imported by `handlers.py` for `@handler` registration.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import feature_coverage_runner, tools, variants_in_regions_runner
from app.queue.align_handlers import _resolve_blob
from app.queue.executor import run_subprocess
from app.queue.feature_coverage_handlers import _run_sort_with_clean_stdout
from app.queue.pipeline_handlers import _failure, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


@handler(
    "variants_in_regions",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.HEAVY),
    max_attempts=2,
)
def run_variants_in_regions(ctx: JobContext) -> dict:
    """Variants in regions for one VCF against one annotation."""
    bedtools = tools.require(tools.bedtools())

    vcf_id = ctx.payload.get("vcf_id")
    if not vcf_id:
        raise PermanentError("variants_in_regions requires a 'vcf_id'")

    annotation_id = ctx.payload.get("annotation_id")
    if not annotation_id:
        raise PermanentError("variants_in_regions requires an 'annotation_id'")

    annotation_format = ctx.payload.get("annotation_format", "gff")

    work = _prepare_workdir(ctx, "variants_in_regions")

    vcf_name = Path(ctx.payload.get("vcf_name") or "variants.vcf").name
    vcf = work / vcf_name
    vcf.unlink(missing_ok=True)
    vcf.symlink_to(_resolve_blob(ctx.payload, "vcf"))

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

    ctx.progress(phase="sorting", pct=0.2, message="sorting annotation to reference order")
    sorted_annotation = work / f"{annotation.stem}.sorted{annotation.suffix}"
    sort_log_path = work / "sort.log"
    sort_cmd = ["bedtools", "sort", "-faidx", str(fai), "-i", str(annotation)]
    code = _run_sort_with_clean_stdout(
        ctx, sort_cmd, stdout_path=str(sorted_annotation), log_path=str(sort_log_path)
    )
    if code != 0:
        raise _failure(code, sort_log_path, "bedtools sort")

    ctx.progress(phase="intersecting", pct=0.5, message="intersecting variants with features")
    intersect_out = work / "intersect.tsv"
    cmd = variants_in_regions_runner.build_command(
        vcf=vcf, annotation=sorted_annotation, genome_file=genome_file
    )
    code = run_subprocess(ctx, cmd, log_path=str(intersect_out))
    if code != 0:
        raise _failure(code, intersect_out, "bedtools intersect")

    ctx.progress(phase="report", pct=0.9, message="parsing variants in regions results")
    report = variants_in_regions_runner.parse_output(
        stdout_path=intersect_out, annotation_format=annotation_format
    )

    report_dir = settings.variants_in_regions_dir / str(vcf_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_name = "variants_in_regions.json"
    (report_dir / report_name).write_text(json.dumps(report))

    facts = {
        "variants_in_regions_status": "ok",
        "variants_in_regions_tool_version": bedtools.version,
        "variants_in_regions_computed_at": datetime.now(UTC).isoformat(),
        "variants_in_regions_total_variants": report["total_variants"],
        "variants_in_regions_in_features": report["variants_in_features"],
        "variants_in_regions_annotation_id": annotation_id,
        "variants_in_regions_report": report_name,
    }

    ctx.progress(phase="done", pct=1.0, message="results complete")
    log.info(
        "variants_in_regions_finished",
        job_id=ctx.job_id,
        vcf_id=vcf_id,
        annotation_id=annotation_id,
        total_variants=report["total_variants"],
        in_features=report["variants_in_features"],
    )

    return {
        "object_id": vcf_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }
