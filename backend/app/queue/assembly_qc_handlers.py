"""Assembly completeness, scored by compleasm.

Separate from contiguity by design, not by omission: contiguity
(sequence_n50 and friends) is computed inline at ingest by
`storage/parsers._parse_fasta` and needs no job at all. This module exists
because completeness needs an external tool and can run for hours on a large
genome, so it must be its own job -- one that a user launches, that can fail
without losing anything else, and whose progress is not conflated with
whatever else touched the object.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import assembly_qc_registry, completeness_runner, tools
from app.queue.executor import run_subprocess
from app.queue.lineage_handlers import lineage_present
from app.queue.pipeline_handlers import _failure, _named_link, _prepare_workdir, _resolve_input
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# A bacterial genome against a small lineage is minutes; a vertebrate genome
# against a large one -- more markers, more sequence for miniprot to search --
# can run hours. Matches the reasoning `ASSEMBLY_LEASE_SECONDS` gives in
# assembly_handlers, at a smaller multiple since this does less work than the
# assembly it is scoring.
COMPLETENESS_LEASE_SECONDS = 3 * 3600


@handler(
    "assess_completeness",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=8, mem_mb=8192, io=IoClass.HEAVY),
    # One attempt, same reasoning assemble_reads gives: the input and the
    # tool are both deterministic, so a retry fails the same way twice, and a
    # genuine transient is better surfaced to the user than silently re-run.
    max_attempts=1,
)
def assess_completeness(ctx: JobContext) -> dict:
    """Score one assembly's completeness against a lineage-specific ortholog
    set.

    Requires the lineage dataset to already be present under
    `settings.lineages_dir` -- `download_lineage` is a separate job and a
    dependency of this one, not something fetched inline, so a completeness
    run never silently blocks on the network partway through.
    """
    tool = tools.require(tools.compleasm())

    lineage = (ctx.payload.get("lineage") or "").strip()
    if not lineage:
        raise PermanentError("assess_completeness requires a 'lineage'")
    odb = (ctx.payload.get("odb") or assembly_qc_registry.COMPLEASM_SPEC.odb).strip()

    library_path = settings.lineages_dir
    if not lineage_present(library_path, lineage, odb):
        raise PermanentError(
            f"The {lineage}_{odb} lineage dataset is not downloaded. "
            "Run the download first, then try again."
        )

    work = _prepare_workdir(ctx, "completeness")
    assembly = _resolve_input(ctx.payload, "assembly")
    # miniprot infers nothing from the filename the way Flye infers gzip
    # compression from it, but a named link keeps the workdir's contents
    # legible for anyone debugging a failed run from the log alone.
    assembly = _named_link(work, assembly, ctx.payload.get("assembly_name"))

    out_dir = work / "out"
    params = completeness_runner.CompletenessParams(
        threads=ctx.payload.get("threads") or 8,
        lineage=lineage,
        odb=odb,
    )

    cmd = completeness_runner.build_completeness_command(
        compleasm_path=tool.path,
        assembly=assembly,
        out_dir=out_dir,
        library_path=library_path,
        params=params,
    )

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="starting", pct=None, message=f"starting compleasm ({lineage})")
    ctx.extend_lease(COMPLETENESS_LEASE_SECONDS)

    log.info(
        "completeness_started",
        job_id=ctx.job_id,
        lineage=lineage,
        odb=odb,
        threads=params.threads,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "compleasm")

    summary_path = out_dir / "summary.txt"
    if not summary_path.exists():
        # Retryable, the same reasoning assemble_reads gives for a missing
        # FASTA: an exit-0 run with no summary is most plausibly a disk that
        # filled during the final write. max_attempts=1 means this will not
        # actually retry -- the class is what tells the user whether
        # re-running is worth their time.
        raise RetryableError(
            "compleasm exited successfully but wrote no summary.txt"
        )

    facts = completeness_runner.parse_summary(
        summary_path.read_text(errors="replace")
    )
    if not facts:
        # Parsed to nothing rather than raising: parse_summary already logs
        # the reason, and a summary that failed to parse must not fail a job
        # that spent possibly hours running miniprot and hmmsearch.
        log.warning("completeness_summary_unparseable", job_id=ctx.job_id)
    facts["assembly_completeness_tool_version"] = tool.version

    ctx.progress(phase="done", pct=1.0, message="completeness scored")
    log.info(
        "completeness_finished",
        job_id=ctx.job_id,
        lineage=lineage,
        complete_pct=facts.get("assembly_completeness_complete_pct"),
    )

    return {
        "object_id": ctx.payload.get("object_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }
