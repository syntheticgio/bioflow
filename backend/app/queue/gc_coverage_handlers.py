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

from datetime import UTC, datetime

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

    facts = {
        "gc_bias_status": "ok",
        "gc_bias_curve": curve,
        "gc_bias_computed_at": datetime.now(UTC).isoformat(),
    }

    log.info(
        "gc_bias_finished",
        job_id=ctx.job_id,
        bam_id=bam_id,
        bin_count=len(curve),
    )

    return {
        "object_id": bam_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
    }
