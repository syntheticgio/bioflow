"""Structural variant calling job handler: call_structural_variants.

Split out for the same reason `variant_handlers.py` was split from
`align_handlers.py`, which was itself split from `pipeline_handlers.py`:
this shares a problem the other handlers do not. Sniffles2 needs both an
indexed BAM and an indexed reference laid out as siblings on disk, the same
constraint `call_variants` has, so this handler mirrors that one closely
rather than the more generic ones in `pipeline_handlers.py`.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import aligners, sniffles_runner, sv_db, tools, variant_runner
from app.pipelines.align_runner import ReadChemistry
from app.queue.align_handlers import _resolve_blob
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


def _validate_payload(payload: dict) -> None:
    if not payload.get("bam_object_id"):
        raise PermanentError(
            "call_structural_variants requires a 'bam_object_id'"
        )
    if not payload.get("reference_object_id"):
        raise PermanentError(
            "call_structural_variants requires a 'reference_object_id'"
        )


def _chemistry_from_payload(payload: dict) -> ReadChemistry | None:
    raw = payload.get("chemistry")
    if not raw:
        return None
    try:
        return ReadChemistry(raw)
    except ValueError:
        # Facts are tool-written strings, not a validated enum. An
        # unrecognized value means "we do not know", which is not a reason
        # to fail the job.
        log.warning("unrecognized_chemistry", chemistry=raw)
        return None


def _check_chemistry(chemistry: ReadChemistry | None) -> None:
    """Re-check the chemistry the launch path already validated.

    Not redundant, for the same reason `variant_handlers._check_chemistry`
    isn't: a payload outlives the check that built it. A job queued before a
    file was reclassified, or replayed by hand, arrives here without having
    passed through `launch_structural_variant_calling` in its current state.
    """
    if chemistry is None:
        return
    if not sniffles_runner.sv_calling_allowed_for(chemistry):
        raise PermanentError(
            f"Chemistry {chemistry.value!r} is not long-read; structural "
            f"variant calling requires long reads."
        )


@handler(
    "call_structural_variants",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # The enqueue-time resources from launch_structural_variant_calling
    # override these; the governor reads job.resources, not this default.
    # Kept in step with that call site so the /jobs display is not
    # misleading.
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    # As for call_variants: an SV caller failure is almost always
    # deterministic, and retrying a multi-hour run delays the error without
    # making it less likely.
    max_attempts=2,
)
def call_structural_variants(ctx: JobContext) -> dict:
    """Call structural variants from an aligned, indexed long-read BAM.

    The BAM's `.bai` and the reference's `.fai` are both required and both
    checked at launch. They are materialized here as siblings of the files
    they index, because that is where Sniffles2 looks for them.
    """
    _validate_payload(ctx.payload)

    chemistry = _chemistry_from_payload(ctx.payload)
    _check_chemistry(chemistry)

    tool = tools.require(tools.sniffles())
    params = sniffles_runner.SnifflesParams.from_dict(ctx.payload.get("params"))

    work = _prepare_workdir(ctx, "sv")

    # The BAM and its .bai, laid out as siblings -- samtools' own
    # convention, matching call_variants.
    bam_name = Path(ctx.payload.get("bam_name") or "aligned.bam").name
    bam = work / bam_name
    bam.unlink(missing_ok=True)
    bam.symlink_to(_resolve_blob(ctx.payload, "bam"))

    bai = work / f"{bam_name}{aligners.BAI_SUFFIX}"
    bai.unlink(missing_ok=True)
    bai.symlink_to(_resolve_blob(ctx.payload, "bai"))

    # The reference and its .fai, through the same helper alignment and
    # call_variants use.
    ref_name = Path(ctx.payload.get("reference_name") or "reference.fa").name
    materialized = aligners.materialize(
        workdir=work / "ref",
        reference_name=ref_name,
        reference_blob=_resolve_blob(ctx.payload, "reference"),
        sidecars={
            f"{ref_name}{aligners.FAI_SUFFIX}": str(_resolve_blob(ctx.payload, "fai"))
        },
    )
    if materialized.missing_index:
        raise PermanentError(
            f"The reference index for {ref_name!r} could not be laid out. Its "
            f".fai may be recorded against a different reference."
        )

    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    output_name = ctx.payload.get("output_name") or f"{Path(bam_name).stem}.sniffles.vcf.gz"
    vcf = out_dir / output_name

    ctx.progress(phase="starting", pct=None, message="starting Sniffles2")
    cmd = sniffles_runner.build_sniffles_command(
        sniffles_path=tool.path,
        bam=bam,
        reference=materialized.reference,
        output=vcf,
        params=params,
    )
    log.info("sniffles_started", job_id=ctx.job_id)

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "sniffles2")

    if not vcf.exists() or vcf.stat().st_size == 0:
        raise RetryableError("Sniffles2 exited 0 but produced no VCF")

    tbi = _index_vcf(ctx, vcf, log_path)

    ctx.progress(phase="db", pct=0.9, message="building the SV index")
    # Keyed by the BAM's object id, not the VCF's -- the VCF does not have an
    # object id yet at this point in a SUBPROCESS handler; ingest happens
    # later, in `_apply_call_structural_variants`, which is what assigns one.
    # That applier moves this file to its permanent, VCF-keyed home (matching
    # every sibling report directory's convention: vcf_stats_dir, bam_stats_dir,
    # etc. are all keyed by the object the report is *about*) once the VCF's
    # real id is known. This path is therefore transient, not the database's
    # final location.
    db_path = _build_sv_index(ctx, vcf, out_dir)

    ctx.progress(phase="done", pct=1.0, message="structural variant calling complete")
    log.info(
        "call_structural_variants_finished",
        job_id=ctx.job_id,
        output=vcf.name,
    )

    return {
        "bam_object_id": ctx.payload.get("bam_object_id"),
        "reference_object_id": ctx.payload.get("reference_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "output": {"tmp_path": str(vcf), "name": vcf.name},
        "index": {"tmp_path": str(tbi), "name": tbi.name, "role": "tbi"},
        "tool_version": tool.version,
        "params": ctx.payload.get("params") or {},
        # Transient, BAM-keyed path. `_apply_call_structural_variants` moves
        # it to `sv_stats_dir/<vcf_object_id>/sv.db` once the VCF is ingested.
        "sv_db_path": str(db_path),
        "workdir": str(work),
    }


def _index_vcf(ctx: JobContext, vcf: Path, log_path: Path) -> Path:
    """Index the VCF, returning the .tbi path.

    bcftools, same as call_variants: it is installed unconditionally and
    knows the compression of what it reads. No SV-specific indexing exists,
    and none is needed.
    """
    tool = tools.require(tools.bcftools())

    ctx.progress(phase="indexing", pct=None, message="indexing structural variants")
    code = run_subprocess(
        ctx,
        variant_runner.build_index_command(bcftools_path=tool.path, vcf=vcf),
        log_path=str(log_path),
    )
    if code != 0:
        raise _failure(code, log_path, "bcftools index")

    tbi = Path(f"{vcf}.tbi")
    if not tbi.exists():
        raise RetryableError("bcftools index exited 0 but produced no .tbi")
    return tbi


def _build_sv_index(ctx: JobContext, vcf: Path, out_dir: Path) -> Path:
    """Build the SQLite table the SV Results view queries, from the VCF's own
    data lines.

    `bcftools view -H` writes the data lines (no header) to `log_path` via
    `run_subprocess`, the same way `variant_handlers.run_vcf_stats` writes
    `bcftools query` to a file rather than piping it: the kernel copies the
    bytes and the job stays cancellable mid-stream, which a hand-rolled
    `subprocess.Popen` pipe would not be. The file is then read back as an
    iterator of lines, the shape `sv_db.build_sv_db` expects -- one VCF data
    line per row -- without materializing them in memory.
    """
    bcftools = tools.require(tools.bcftools())

    rows_path = out_dir / "sv_rows.txt"
    code = run_subprocess(
        ctx,
        [bcftools.path, "view", "-H", str(vcf)],
        log_path=str(rows_path),
    )
    if code != 0:
        raise RetryableError("bcftools view exited nonzero while building the SV index")

    db_path = settings.sv_stats_dir / str(ctx.payload.get("bam_object_id")) / "sv.db"
    with open(rows_path, errors="replace") as fh:
        sv_db.build_sv_db(rows=fh, db_path=db_path)

    return db_path
