"""gc_bias: the GC-vs-coverage bias curve for one BAM, joining its own
per-window depth (mosdepth) against its alignment target's per-window GC
(gc_tracks).

THREAD mode, not SUBPROCESS: no external tool runs here, only arithmetic
over two already-computed stored artifacts. THREAD mode also means this
handler cannot reach the database (see de_summary_handlers.py) -- the async
launcher (pipeline_service.launch_gc_bias) does every DB/file read and
passes fully-loaded gc_tracks contigs and mosdepth region rows in the
payload.

Read-only like coverage and bam_stats: no derived object, just facts merged
onto the BAM by _apply_gc_bias (results.py).
"""

import json
from datetime import UTC, datetime

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import gc_coverage
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


@handler(
    "gc_bias",
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=256, io=IoClass.LIGHT),
    max_attempts=2,
)
def compute_gc_bias(ctx: JobContext) -> dict:
    bam_id = ctx.payload.get("bam_id")
    if not bam_id:
        raise PermanentError("gc_bias requires a 'bam_id'")

    gc_contigs = ctx.payload.get("gc_contigs") or []
    depth_regions = ctx.payload.get("depth_regions") or {}

    joined = gc_coverage.join_windows(gc_contigs, depth_regions)
    curve = gc_coverage.bias_curve(joined)

    # The per-contig blobplot array is bounded by cap_by_cumulative_length
    # (V4) but can still run to thousands of entries -- too large for facts
    # (V5), so it goes to a JSON report on disk instead, mirroring
    # coverage_dir/coverage.json.
    contig_rows = gc_coverage.per_contig(joined)
    kept, dropped = gc_coverage.cap_by_cumulative_length(contig_rows)

    report_dir = settings.gc_bias_dir / str(bam_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_name = "gc_blob.json"
    (report_dir / report_name).write_text(
        json.dumps(
            {
                "contigs": kept,
                "dropped_count": dropped,
                "kept_count": len(kept),
            }
        )
    )

    # "empty", not "ok", when every window's GC was None (a legitimate
    # all-N reference case): "ok" with an empty curve is indistinguishable
    # from "never ran" in BamResults.tsx, which gates the chart on
    # `gc_bias_status === "ok" && gc_bias_curve` -- an empty array is
    # truthy, so the render would be attempted and GcBiasChart's own
    # `if (!curve?.length) return null` would silently produce nothing.
    facts = {
        "gc_bias_status": "ok" if curve else "empty",
        "gc_bias_curve": curve,
        "gc_bias_partial": bool(ctx.payload.get("gc_tracks_partial")),
        "gc_bias_computed_at": datetime.now(UTC).isoformat(),
        "gc_blob_status": "ok",
        "gc_blob_report": report_name,
        "gc_blob_contig_count": len(kept),
        "gc_blob_dropped_count": dropped,
    }

    log.info(
        "gc_bias_finished",
        job_id=ctx.job_id,
        bam_id=bam_id,
        bin_count=len(curve),
        contig_count=len(kept),
        dropped_count=dropped,
    )

    return {
        "object_id": bam_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
    }
