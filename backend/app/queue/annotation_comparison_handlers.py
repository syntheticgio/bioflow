"""annotation_comparison: compare feature overlap and unique features between
two annotations of the same assembly using bedtools jaccard and subtract.

Imported by `handlers.py` for `@handler` registration.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import annotation_comparison_runner, tools
from app.queue.align_handlers import _resolve_blob
from app.queue.executor import run_subprocess
from app.queue.feature_coverage_handlers import _run_sort_with_clean_stdout
from app.queue.pipeline_handlers import _failure, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


@handler(
    "annotation_comparison",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.HEAVY),
    max_attempts=2,
)
def run_annotation_comparison(ctx: JobContext) -> dict:
    """Annotation comparison between two annotations."""
    bedtools = tools.require(tools.bedtools())

    anno_a_id = ctx.payload.get("annotation_a_id") or ctx.payload.get("annotation_id")
    if not anno_a_id:
        raise PermanentError("annotation_comparison requires 'annotation_a_id'")

    anno_b_id = ctx.payload.get("annotation_b_id") or ctx.payload.get("other_annotation_id")
    if not anno_b_id:
        raise PermanentError("annotation_comparison requires 'annotation_b_id'")

    format_a = ctx.payload.get("format_a", "gff")
    format_b = ctx.payload.get("format_b", "gff")

    work = _prepare_workdir(ctx, "annotation_comparison")

    name_a = Path(ctx.payload.get("name_a") or f"anno_a.{format_a}").name
    anno_a = work / name_a
    anno_a.unlink(missing_ok=True)
    anno_a.symlink_to(_resolve_blob(ctx.payload, "annotation_a"))

    name_b = Path(ctx.payload.get("name_b") or f"anno_b.{format_b}").name
    anno_b = work / name_b
    anno_b.unlink(missing_ok=True)
    anno_b.symlink_to(_resolve_blob(ctx.payload, "annotation_b"))

    ctx.progress(phase="sorting", pct=0.1, message="sorting annotations")
    sorted_a = work / f"sorted_a.{format_a}"
    sort_log_a = work / "sort_a.log"
    sort_cmd_a = ["bedtools", "sort", "-i", str(anno_a)]
    code = _run_sort_with_clean_stdout(
        ctx, sort_cmd_a, stdout_path=str(sorted_a), log_path=str(sort_log_a)
    )
    if code != 0:
        raise _failure(code, sort_log_a, "bedtools sort A")

    sorted_b = work / f"sorted_b.{format_b}"
    sort_log_b = work / "sort_b.log"
    sort_cmd_b = ["bedtools", "sort", "-i", str(anno_b)]
    code = _run_sort_with_clean_stdout(
        ctx, sort_cmd_b, stdout_path=str(sorted_b), log_path=str(sort_log_b)
    )
    if code != 0:
        raise _failure(code, sort_log_b, "bedtools sort B")

    ctx.progress(phase="jaccard", pct=0.4, message="computing jaccard overlap")
    jaccard_out = work / "jaccard.tsv"
    jaccard_cmd = annotation_comparison_runner.build_jaccard_command(sorted_a, sorted_b)
    code = run_subprocess(ctx, jaccard_cmd, log_path=str(jaccard_out))
    if code != 0:
        raise _failure(code, jaccard_out, "bedtools jaccard")
    jaccard_res = annotation_comparison_runner.parse_jaccard_output(jaccard_out)

    ctx.progress(phase="subtract", pct=0.6, message="finding features unique to A")
    sub_a_out = work / "unique_a.tsv"
    sub_a_cmd = annotation_comparison_runner.build_subtract_command(sorted_a, sorted_b)
    code = run_subprocess(ctx, sub_a_cmd, log_path=str(sub_a_out))
    if code != 0:
        raise _failure(code, sub_a_out, "bedtools intersect -v A")
    unique_a = annotation_comparison_runner.parse_subtract_output(
        sub_a_out, annotation_format=format_a
    )

    ctx.progress(phase="subtract", pct=0.8, message="finding features unique to B")
    sub_b_out = work / "unique_b.tsv"
    sub_b_cmd = annotation_comparison_runner.build_subtract_command(sorted_b, sorted_a)
    code = run_subprocess(ctx, sub_b_cmd, log_path=str(sub_b_out))
    if code != 0:
        raise _failure(code, sub_b_out, "bedtools intersect -v B")
    unique_b = annotation_comparison_runner.parse_subtract_output(
        sub_b_out, annotation_format=format_b
    )

    max_feat = annotation_comparison_runner.MAX_UNIQUE_FEATURES_IN_REPORT
    report = {
        "jaccard": jaccard_res,
        "unique_to_a_count": len(unique_a),
        "unique_to_a_truncated": len(unique_a) > max_feat,
        "unique_to_a": unique_a[:max_feat],
        "unique_to_b_count": len(unique_b),
        "unique_to_b_truncated": len(unique_b) > max_feat,
        "unique_to_b": unique_b[:max_feat],
        "annotation_a_id": anno_a_id,
        "annotation_b_id": anno_b_id,
    }

    report_dir = settings.annotation_comparison_dir / str(anno_a_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_name = "annotation_comparison.json"
    (report_dir / report_name).write_text(json.dumps(report))

    facts = {
        "annotation_comparison_status": "ok",
        "annotation_comparison_tool_version": bedtools.version,
        "annotation_comparison_computed_at": datetime.now(UTC).isoformat(),
        "annotation_comparison_jaccard": jaccard_res["jaccard"],
        "annotation_comparison_intersection_bp": jaccard_res["intersection_bp"],
        "annotation_comparison_union_bp": jaccard_res["union_bp"],
        "annotation_comparison_other_id": anno_b_id,
        "annotation_comparison_report": report_name,
    }

    ctx.progress(phase="done", pct=1.0, message="results complete")
    log.info(
        "annotation_comparison_finished",
        job_id=ctx.job_id,
        anno_a_id=anno_a_id,
        anno_b_id=anno_b_id,
        jaccard=jaccard_res["jaccard"],
    )

    return {
        "object_id": anno_a_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }
