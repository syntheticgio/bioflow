"""Coverage-vs-GC bias curve: join per-window depth with per-window GC content.

Read-only like ``run_bam_stats``: no derived objects, just facts merged onto
the BAM by ``_apply_gc_bias``.  Requires the BAM to have coverage depth
(mosdepth) and the reference to have GC tracks (``gc_tracks``), both of which
use the same window grid so the join is a direct (contig, window_index) lookup.
"""

import json
from pathlib import Path

from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import gc_bias_runner
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


@handler(
    "gc_bias",
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    # Pure Python join of two in-memory data structures, no subprocess, no
    # heavy I/O beyond reading one JSON report from the coverage_dir.
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.LIGHT),
    max_attempts=1,
)
def compute_gc_bias(ctx: JobContext) -> dict:
    """Join per-window depth with per-window GC to produce the bias curve.

    The payload must carry::

        bam_id                — the BAM's object id (for the applier)
        gc_tracks             — the reference's GC tracks dict
        coverage_report_path  — path to the BAM's mosdepth coverage JSON report
    """
    bam_id = ctx.payload.get("bam_id")
    if not bam_id:
        raise PermanentError("gc_bias requires a 'bam_id'")

    gc_tracks = ctx.payload.get("gc_tracks")
    if not gc_tracks:
        raise PermanentError("gc_bias requires 'gc_tracks' in payload")

    coverage_report_path = ctx.payload.get("coverage_report_path")
    if not coverage_report_path:
        raise PermanentError("gc_bias requires 'coverage_report_path' in payload")

    ctx.progress(phase="reading", pct=0.2, message="reading coverage report")

    report_path = Path(coverage_report_path)
    if not report_path.exists():
        raise PermanentError(f"Coverage report not found: {coverage_report_path}")

    report = json.loads(report_path.read_text())
    regions = report.get("regions", {})
    if not regions:
        raise PermanentError("Coverage report has no regions data")

    ctx.progress(phase="computing", pct=0.6, message="computing GC bias curve")
    result = gc_bias_runner.compute_gc_bias(gc_tracks, regions)

    ctx.progress(phase="done", pct=1.0, message="GC bias computed")
    log.info(
        "gc_bias_finished",
        job_id=ctx.job_id,
        bam_id=bam_id,
        bins=len(result.get("gc_bias_bins", [])),
        genome_avg_depth=result.get("genome_avg_depth"),
    )

    return {
        "object_id": bam_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": {
            "gc_bias": result,
            "gc_bias_status": "ok",
        },
    }
