"""coverage: per-window (or per-region) read depth for one BAM, via mosdepth.

Its own module for the same reason feature_coverage_handlers.py is: this is
read-only like `run_bam_stats` -- no derived object, one JSON report on disk
plus summary facts merged onto the BAM -- but it belongs to neither the
alignment nor the expression handler family.

Distinct from `feature_coverage` despite the neighbouring name. That one
answers "how well is each annotated feature covered" with bedtools against an
annotation; this one answers "how deep is coverage across the reference"
with mosdepth over fixed windows, and needs no annotation at all.

The runner underneath (`mosdepth_runner`) is pure functions only; this module
owns the subprocess call and the workdir/blob-resolution seam, mirroring the
`bam_stats_runner` / `run_bam_stats` split.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import mosdepth_runner, tools
from app.queue.align_handlers import _resolve_blob
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


@handler(
    "coverage",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.HEAVY),
    # Deterministic failures (an unindexed BAM, a mismatched reference) do
    # not improve with retries; one retry covers transient disk/exec noise.
    max_attempts=2,
)
def run_coverage(ctx: JobContext) -> dict:
    """Per-window read depth for one BAM.

    Read-only like bam_stats: no derived objects, one JSON report on disk
    plus summary facts merged onto the BAM by `_apply_coverage`.
    """
    mosdepth = tools.require(tools.mosdepth())

    bam_id = ctx.payload.get("bam_id")
    if not bam_id:
        raise PermanentError("coverage requires a 'bam_id'")

    work = _prepare_workdir(ctx, "coverage")

    bam_name = Path(ctx.payload.get("bam_name") or "aligned.bam").name
    bam = work / bam_name
    bam.unlink(missing_ok=True)
    bam.symlink_to(_resolve_blob(ctx.payload, "bam"))

    # mosdepth reads the BAM index to seek per region; without it beside the
    # BAM it exits with "index not found". The index is a sidecar of the BAM
    # object, so it is resolved the same way and linked under the name
    # mosdepth expects rather than the blob's own.
    bai = work / f"{bam_name}.bai"
    bai.unlink(missing_ok=True)
    bai.symlink_to(_resolve_blob(ctx.payload, "bai"))

    fai = work / "reference.fa.fai"
    fai.unlink(missing_ok=True)
    fai.symlink_to(_resolve_blob(ctx.payload, "fai"))

    # Region mode when the launch carried a target BED, windowed otherwise.
    # The two differ only in what `--by` points at; everything downstream --
    # parsing, the report, the facts -- is shared, because mosdepth emits the
    # same `.regions.bed.gz` either way.
    regions_id = ctx.payload.get("regions_id")
    mode = "regions" if regions_id else "windows"

    if mode == "regions":
        ctx.progress(phase="regions", pct=0.1, message="reading the target regions")
        regions_name = Path(ctx.payload.get("regions_name") or "regions.bed").name
        regions_bed = work / regions_name
        regions_bed.unlink(missing_ok=True)
        regions_bed.symlink_to(_resolve_blob(ctx.payload, "regions"))
        by_kwargs = {"regions_bed": regions_bed}
    else:
        ctx.progress(
            phase="windows", pct=0.1, message="tiling the reference into windows"
        )
        contig_lengths = mosdepth_runner.contig_lengths_from_fai(fai)
        if not contig_lengths:
            raise PermanentError(
                "coverage could not read any contig lengths from the reference index"
            )
        windows = mosdepth_runner.build_windows_bed(contig_lengths)
        if not windows:
            # Every contig shorter than MIN_WINDOW_BASES. Permanent rather
            # than retryable: the reference will not grow, and a mosdepth run
            # against an empty --by BED produces an empty report that reads
            # as a bug.
            raise PermanentError(
                "coverage found no contig long enough to window "
                f"(all shorter than {mosdepth_runner.MIN_WINDOW_BASES}bp)"
            )
        windows_bed = work / "windows.bed"
        windows_bed.write_text(mosdepth_runner.render_windows_bed(windows))
        by_kwargs = {"windows_bed": windows_bed}

    ctx.progress(phase="depth", pct=0.4, message="computing depth")
    prefix = work / "cov"
    log_path = work / "mosdepth.log"
    cmd = mosdepth_runner.build_command(bam=bam, prefix=prefix, **by_kwargs)
    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "mosdepth")

    ctx.progress(phase="report", pct=0.9, message="writing the coverage report")
    report = mosdepth_runner.build_report(prefix=prefix, mode=mode)
    facts = mosdepth_runner.summarize(report)
    if not facts:
        raise PermanentError("mosdepth produced no depth summary for this alignment")

    report_dir = settings.coverage_dir / str(bam_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_name = "coverage.json"
    (report_dir / report_name).write_text(json.dumps(report))

    facts = {
        **facts,
        "coverage_status": "ok",
        "coverage_tool_version": mosdepth.version,
        "coverage_computed_at": datetime.now(UTC).isoformat(),
        "coverage_report": report_name,
    }
    if mode == "windows":
        facts["coverage_window_count"] = mosdepth_runner.WINDOW_COUNT
    else:
        # Which target set produced these numbers -- without it a region run's
        # facts are unattributable once a second BED exists.
        facts["coverage_regions_id"] = regions_id

    ctx.progress(phase="done", pct=1.0, message="results complete")
    log.info(
        "coverage_finished",
        job_id=ctx.job_id,
        bam_id=bam_id,
        mean_depth=facts.get("coverage_mean_depth"),
        contig_count=facts.get("coverage_contig_count"),
    )

    return {
        "object_id": bam_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }
