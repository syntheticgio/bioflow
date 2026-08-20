"""methylation: per-site base-modification (methylation) calling for one BAM,
via modkit.

Its own module for the same reason mosdepth_handlers.py is: read-only over
its input in the sense that the BAM is not modified, but unlike coverage this
job DOES produce a derived object -- the bedMethyl file itself, which a
person opens directly (IGV, R, a spreadsheet) -- plus summary facts merged
onto the BAM. See docs/superpowers/specs/2026-08-20-modkit-methylation-design.md,
decision K4.

The runner underneath (`modkit_runner`) is pure functions only; this module
owns the subprocess call and the workdir/blob-resolution seam, mirroring the
`mosdepth_runner` / `run_coverage` split.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import modkit_runner, tools
from app.queue.align_handlers import _resolve_blob
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


@handler(
    "methylation",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=2, mem_mb=4096, io=IoClass.HEAVY),
    # Deterministic failures (no modification tags, a corrupt BAM) do not
    # improve with retries; one retry covers transient disk/exec noise.
    max_attempts=2,
)
def run_methylation(ctx: JobContext) -> dict:
    """Per-site base-modification calling for one BAM.

    Produces a bedMethyl DataObject derived from the BAM, plus summary facts
    (`methylation_*`) merged onto the BAM. K3: a pileup that produces zero
    rows fails the job rather than reporting a silent, empty success.
    """
    modkit = tools.require(tools.modkit())

    bam_id = ctx.payload.get("bam_id")
    if not bam_id:
        raise PermanentError("methylation requires a 'bam_id'")

    work = _prepare_workdir(ctx, "methylation")

    bam_name = Path(ctx.payload.get("bam_name") or "aligned.bam").name
    bam = work / bam_name
    bam.unlink(missing_ok=True)
    bam.symlink_to(_resolve_blob(ctx.payload, "bam"))

    # modkit seeks through the BAM index the same way mosdepth does; without
    # it beside the BAM it exits with an index-not-found error.
    bai = work / f"{bam_name}.bai"
    bai.unlink(missing_ok=True)
    bai.symlink_to(_resolve_blob(ctx.payload, "bai"))

    ctx.progress(phase="pileup", pct=0.2, message="calling base modifications")
    output_name = "pileup.bed"
    output_bed = work / output_name
    log_path = work / "modkit.log"
    cmd = modkit_runner.build_pileup_command(bam, output_bed, threads=2)
    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "modkit")

    ctx.progress(phase="parse", pct=0.7, message="reading the bedMethyl output")
    records = modkit_runner.parse_bedmethyl(output_bed)
    facts = modkit_runner.summarize(records)
    if not facts:
        # K3: a pileup that ran cleanly but called nothing is not a success
        # to report quietly -- see the design spec's decision K3. A permanent
        # error rather than retryable: re-running modkit against the same
        # BAM will not discover modification tags that are not there.
        raise PermanentError(
            "modkit produced no base-modification calls for this alignment"
        )

    report_dir = settings.methylation_dir / str(bam_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_name = "methylation.json"
    (report_dir / report_name).write_text(json.dumps(facts))

    facts = {
        **facts,
        "methylation_status": "ok",
        "methylation_tool_version": modkit.version,
        "methylation_computed_at": datetime.now(UTC).isoformat(),
        "methylation_report": report_name,
    }

    ctx.progress(phase="done", pct=1.0, message="results complete")
    log.info(
        "methylation_finished",
        job_id=ctx.job_id,
        bam_id=bam_id,
        site_count=facts.get("methylation_site_count"),
        mean_pct=facts.get("methylation_mean_pct"),
    )

    return {
        "object_id": bam_id,
        "bam_object_id": bam_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "output": {"tmp_path": str(output_bed), "name": f"{bam_name}.methylation.bed"},
        "workdir": str(work),
    }
