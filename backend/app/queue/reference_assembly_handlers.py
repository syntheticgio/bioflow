"""iVar amplicon/viral consensus calling.

First tool riding on the reference-based assembly foundation (#21):
`app.services.reference_assembly` provides the provenance rule this
handler's launch path enforces before ever reaching here, but installs no
tool and dispatches to nothing on its own. This module is what makes it
real -- the first slice for epic #14.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import ivar_runner, tools
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _named_link, _prepare_workdir, _resolve_input
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# Viral/amplicon consensus is minutes even at high coverage -- iVar does not
# parallelize and the reference is small. A one-hour lease is generous;
# there is no case here resembling the multi-hour runs
# assembly_qc_handlers.COMPLETENESS_LEASE_SECONDS sizes for.
CONSENSUS_LEASE_SECONDS = 3600


@handler(
    "consensus_from_alignment",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # 8192, not the 4096 the design first estimated -- corrected 2026-08-05
    # after a real run against a 26Mb T. brucei reference OOM-killed at
    # 4096. `mpileup -d 0` uncaps the pileup depth deliberately (the design's
    # own reasoning: a depth cap silently downsamples amplicon data), which
    # is exactly what makes an unexpectedly high-coverage or repetitive
    # region on a *non-viral* reference expensive. The original "30kb viral
    # reference, small memory" estimate was true for the tool's intended
    # case but this handler is generic over any BAM+reference pair, so the
    # budget has to cover the worst case it can actually be asked to run,
    # not the common one. Matches assess_completeness's budget for a
    # comparable whole-genome operation.
    resources=JobResources(cpu=2, mem_mb=8192, io=IoClass.HEAVY),
    # One attempt, same reasoning assess_completeness gives: the input and
    # the tool are both deterministic, so a retry fails the same way twice.
    max_attempts=1,
)
def consensus_from_alignment(ctx: JobContext) -> dict:
    """Trim primers (if supplied) and call a consensus sequence from a BAM.

    Three stages, one job: `ivar trim` (skipped without a primer BED),
    `samtools sort` on the trimmed BAM (iVar's own output is unsorted, and
    `ivar consensus` needs a position-sorted pileup), then
    `samtools mpileup | ivar consensus` as a single pipefail-wrapped
    invocation. See ivar_runner for why each stage is shaped the way it is.
    """
    tool = tools.require(tools.ivar())
    samtools = tools.require(tools.samtools())

    work = _prepare_workdir(ctx, "consensus")

    bam = _resolve_input(ctx.payload, "bam")
    bam = _named_link(work, bam, ctx.payload.get("bam_name"))

    reference = _resolve_input(ctx.payload, "reference")
    reference = _named_link(work, reference, ctx.payload.get("reference_name"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.extend_lease(CONSENSUS_LEASE_SECONDS)

    primer_bed_name = ctx.payload.get("primer_bed_name")
    primers_trimmed = bool(primer_bed_name)

    consensus_input = bam
    if primers_trimmed:
        ctx.progress(phase="trimming", pct=0.2, message="trimming primers")
        primer_bed = _resolve_input(ctx.payload, "primer_bed")
        primer_bed = _named_link(work, primer_bed, primer_bed_name)

        trim_prefix = work / "trimmed"
        trim_cmd = ivar_runner.build_trim_command(
            ivar_path=tool.path,
            bam=bam,
            primer_bed=primer_bed,
            out_prefix=trim_prefix,
        )
        code = run_subprocess(ctx, trim_cmd, log_path=str(log_path))
        if code != 0:
            raise _failure(code, log_path, "ivar trim")

        trimmed_bam = trim_prefix.with_suffix(".bam")
        if not trimmed_bam.exists():
            raise RetryableError("ivar trim exited successfully but wrote no BAM")

        ctx.progress(phase="sorting", pct=0.4, message="sorting trimmed reads")
        sorted_bam = work / "trimmed.sorted.bam"
        sort_cmd = ivar_runner.build_sort_command(
            samtools_path=samtools.path, bam=trimmed_bam, out=sorted_bam
        )
        code = run_subprocess(ctx, sort_cmd, log_path=str(log_path))
        if code != 0:
            raise _failure(code, log_path, "samtools sort")
        consensus_input = sorted_bam

    ctx.progress(phase="consensus", pct=0.7, message="calling consensus")
    out_prefix = work / "consensus"
    params = ivar_runner.ConsensusParams(
        min_quality=ctx.payload.get("min_quality") or 20,
        min_freq=ctx.payload.get("min_freq") if ctx.payload.get("min_freq") is not None else 0.0,
        min_depth=ctx.payload.get("min_depth") or 10,
    )
    consensus_cmd = ivar_runner.build_consensus_command(
        samtools_path=samtools.path,
        ivar_path=tool.path,
        bam=consensus_input,
        reference=reference,
        out_prefix=out_prefix,
        params=params,
    )

    # iVar's own consensus summary goes to stderr, not a file -- verified
    # against a real run, see ivar_runner's module docstring. run_subprocess
    # merges stderr into the same stream as stdout, and passing on_line is
    # the only way to also see the lines rather than only writing them to
    # log_path -- so this collector is what recovers the summary text
    # without duplicating run_subprocess's own cancellation handling.
    output_lines: list[str] = []
    code = run_subprocess(
        ctx, consensus_cmd, log_path=str(log_path), on_line=output_lines.append
    )
    stderr_text = "\n".join(output_lines)
    if code != 0:
        raise _failure(code, log_path, "samtools mpileup | ivar consensus")

    consensus_fasta = out_prefix.with_suffix(".fa")
    if not consensus_fasta.exists() or consensus_fasta.stat().st_size == 0:
        # iVar's exit codes are unreliable -- see the design's risks
        # section -- so an empty or missing FASTA is checked explicitly
        # rather than trusted to a non-zero return code.
        raise RetryableError(
            "ivar consensus exited successfully but produced no sequence"
        )

    facts = ivar_runner.parse_consensus_stderr(stderr_text)
    facts["consensus_tool_version"] = tool.version
    facts["consensus_min_quality"] = params.min_quality
    facts["consensus_min_freq"] = params.min_freq
    facts["consensus_min_depth"] = params.min_depth
    facts["consensus_primers_trimmed"] = primers_trimmed
    ref_len = facts.get("consensus_reference_length")
    zero_depth = facts.get("consensus_zero_depth_positions", 0)
    low_depth = facts.get("consensus_low_depth_positions", 0)
    if ref_len:
        facts["consensus_n_count"] = zero_depth + low_depth
        facts["consensus_ambiguous_pct"] = round(
            100 * (zero_depth + low_depth) / ref_len, 2
        )

    ctx.progress(phase="done", pct=1.0, message="consensus complete")
    log.info(
        "consensus_finished",
        job_id=ctx.job_id,
        primers_trimmed=primers_trimmed,
        n_count=facts.get("consensus_n_count"),
    )

    return {
        "job_id": ctx.job_id,
        "bam_object_id": ctx.payload.get("bam_object_id"),
        "reference_object_id": ctx.payload.get("reference_object_id"),
        "primer_bed_object_id": ctx.payload.get("primer_bed_object_id"),
        "output": {"tmp_path": str(consensus_fasta), "name": "consensus.fasta"},
        "facts": facts,
    }
