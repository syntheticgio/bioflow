"""Variant calling job handler: call_variants.

Split from `align_handlers.py` for the same reason that file was split from
`pipeline_handlers.py`: these share a problem the others do not. A variant
caller needs *both* an indexed BAM and an indexed reference laid out as
siblings on disk, because Clair3 and bcftools each infer their index paths from
the filename they were given.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError, ValidationError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import aligners, csq_runner, tools, variant_db, variant_runner, vcf_stats_runner
from app.pipelines.align_runner import ReadChemistry
from app.pipelines.variant_runner import VariantCaller
from app.queue.align_handlers import _resolve_blob
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _named_link, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


def _validate_payload(payload: dict) -> None:
    if not payload.get("bam_object_id"):
        raise PermanentError("call_variants requires a 'bam_object_id'")
    if not payload.get("reference_object_id"):
        raise PermanentError("call_variants requires a 'reference_object_id'")


def _resolve_caller(payload: dict) -> VariantCaller:
    """The caller this job should run, from the payload.

    PermanentError throughout: every failure here is a payload that will not
    improve on retry.
    """
    raw = payload.get("caller") or VariantCaller.CLAIR3.value
    try:
        caller = VariantCaller(raw)
    except ValueError:
        raise PermanentError(
            f"Unknown variant caller {raw!r}",
            details={"valid": [c.value for c in VariantCaller]},
        ) from None

    return caller


def _check_chemistry(
    *, chemistry: ReadChemistry | None, caller: VariantCaller
) -> None:
    """Re-check the chemistry the launch path already validated.

    Not redundant: a payload outlives the check that built it. A job queued
    before a file was reclassified, or replayed by hand, arrives here without
    having passed through `launch_variant_calling` in its current state.

    A caller that merely disagrees with the chemistry is logged and allowed --
    overriding the suggestion is the user's call. CLR is the exception, because
    there is no caller that produces trustworthy results from it.
    """
    if chemistry is None:
        return

    try:
        expected = variant_runner.caller_for_chemistry(chemistry)
    except ValidationError as e:
        # CLR. Permanent: the file's chemistry is not going to change.
        raise PermanentError(str(e)) from e

    if caller is not expected:
        log.warning(
            "caller_chemistry_mismatch",
            caller=caller.value,
            suggested=expected.value,
            chemistry=chemistry.value,
        )


def _model_path(platform: str, *, root: Path | None = None) -> Path:
    """The Clair3 model directory for a platform.

    Checked before the run rather than trusted, so a model missing from the
    image fails in the first second with a message naming the path, instead of
    surfacing as a Clair3 traceback once the job is already underway.
    """
    base = root if root is not None else Path(settings.clair3_models_dir)
    path = base / platform
    if not path.is_dir():
        raise PermanentError(
            f"Clair3 model not found at {path}. The image should carry it; "
            f"rebuild, or point CLAIR3_MODELS_DIR at a directory that has it.",
            details={"platform": platform, "path": str(path)},
        )
    return path


def _chemistry_from_payload(payload: dict) -> ReadChemistry | None:
    raw = payload.get("chemistry")
    if not raw:
        return None
    try:
        return ReadChemistry(raw)
    except ValueError:
        # Facts are tool-written strings, not a validated enum. An unrecognized
        # value means "we do not know", which is not a reason to fail the job.
        log.warning("unrecognized_chemistry", chemistry=raw)
        return None


@handler(
    "call_variants",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # The enqueue-time resources from launch_variant_calling override these;
    # the governor reads job.resources, not this default. Kept in step with
    # that call site so the /jobs display is not misleading.
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    # As for alignment: a caller failure is almost always deterministic, and
    # retrying a multi-hour run delays the error without making it less likely.
    max_attempts=2,
)
def call_variants(ctx: JobContext) -> dict:
    """Call variants from an aligned, indexed BAM.

    The BAM's `.bai` and the reference's `.fai` are both required and both
    checked at launch. They are materialized here as siblings of the files they
    index, because that is where the callers look for them -- neither takes an
    explicit index path.
    """
    _validate_payload(ctx.payload)

    caller = _resolve_caller(ctx.payload)
    chemistry = _chemistry_from_payload(ctx.payload)
    _check_chemistry(chemistry=chemistry, caller=caller)

    work = _prepare_workdir(ctx, "variants")

    # The BAM and its .bai, laid out as siblings. samtools' own convention:
    # `<name>.bam` alongside `<name>.bam.bai`.
    bam_name = Path(ctx.payload.get("bam_name") or "aligned.bam").name
    bam = work / bam_name
    bam.unlink(missing_ok=True)
    bam.symlink_to(_resolve_blob(ctx.payload, "bam"))

    bai = work / f"{bam_name}{aligners.BAI_SUFFIX}"
    bai.unlink(missing_ok=True)
    bai.symlink_to(_resolve_blob(ctx.payload, "bai"))

    # The reference and its .fai, through the same helper alignment uses.
    ref_name = Path(ctx.payload.get("reference_name") or "reference.fa").name
    materialized = aligners.materialize(
        workdir=work / "ref",
        reference_name=ref_name,
        reference_blob=_resolve_blob(ctx.payload, "reference"),
        sidecars={f"{ref_name}{aligners.FAI_SUFFIX}": str(_resolve_blob(ctx.payload, "fai"))},
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

    if caller is VariantCaller.CLAIR3:
        vcf = _run_clair3(ctx, bam, materialized.reference, out_dir, log_path)
    elif caller is VariantCaller.DEEPVARIANT:
        vcf = _run_deepvariant(ctx, bam, materialized.reference, out_dir, log_path)
    else:
        vcf = _run_bcftools(ctx, bam, materialized.reference, out_dir, log_path)

    tbi = _index_vcf(ctx, vcf, log_path)

    ctx.progress(phase="done", pct=1.0, message="variant calling complete")
    log.info(
        "call_variants_finished",
        job_id=ctx.job_id,
        caller=caller.value,
        output=vcf.name,
    )

    return {
        "bam_object_id": ctx.payload.get("bam_object_id"),
        "reference_object_id": ctx.payload.get("reference_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "output": {"tmp_path": str(vcf), "name": vcf.name},
        "index": {"tmp_path": str(tbi), "name": tbi.name, "role": "tbi"},
        "caller": caller.value,
        "tool_version": _caller_version(caller),
        "params": ctx.payload.get("params") or {},
        "workdir": str(work),
    }


def _caller_version(caller: VariantCaller) -> str | None:
    if caller is VariantCaller.CLAIR3:
        tool = tools.clair3()
    elif caller is VariantCaller.DEEPVARIANT:
        tool = tools.deepvariant()
    else:
        tool = tools.bcftools()
    return tool.version




def _run_clair3(
    ctx: JobContext, bam: Path, reference: Path, out_dir: Path, log_path: Path
) -> Path:
    tool = tools.require(tools.clair3())

    params = variant_runner.Clair3Params.from_dict(ctx.payload.get("clair3_params"))
    model_path = _model_path(params.platform)

    ctx.progress(phase="starting", pct=None, message="starting Clair3")
    cmd = variant_runner.build_clair3_command(
        clair3_path=tool.path,
        bam=bam,
        reference=reference,
        output_dir=out_dir,
        model_path=model_path,
        params=params,
    )
    log.info("clair3_started", job_id=ctx.job_id, platform=params.platform)

    code = run_subprocess(
        ctx, cmd, log_path=str(log_path), parser=variant_runner.VariantProgress()
    )
    if code != 0:
        raise _failure(code, log_path, "clair3")

    # Clair3 writes merge_output.vcf.gz: the pileup and full-alignment calls
    # reconciled. Anything else in this directory is an intermediate.
    produced = out_dir / "merge_output.vcf.gz"
    if not produced.exists():
        raise RetryableError("Clair3 exited 0 but produced no merged VCF")

    return _rename_output(ctx, produced, bam, "clair3")


def _run_bcftools(
    ctx: JobContext, bam: Path, reference: Path, out_dir: Path, log_path: Path
) -> Path:
    tool = tools.require(tools.bcftools())

    params = variant_runner.BcftoolsParams.from_dict(ctx.payload.get("bcftools_params"))
    vcf = out_dir / variant_runner.output_name(bam.name, "bcftools")

    ctx.progress(phase="starting", pct=None, message="starting bcftools")
    cmd = variant_runner.build_bcftools_command(
        bcftools_path=tool.path,
        reference=reference,
        bam=bam,
        output=vcf,
        params=params,
    )
    log.info("bcftools_started", job_id=ctx.job_id)

    code = run_subprocess(
        ctx, cmd, log_path=str(log_path), parser=variant_runner.VariantProgress()
    )
    if code != 0:
        raise _failure(code, log_path, "bcftools")

    if not vcf.exists() or vcf.stat().st_size == 0:
        raise RetryableError("bcftools exited 0 but produced no VCF")
    return vcf


def _run_deepvariant(
    ctx: JobContext, bam: Path, reference: Path, out_dir: Path, log_path: Path
) -> Path:
    tool = tools.require(tools.deepvariant())

    params = variant_runner.DeepVariantParams.from_dict(
        ctx.payload.get("deepvariant_params")
    )
    vcf = out_dir / variant_runner.output_name(bam.name, "deepvariant")

    # Checked before the run, in the spirit of _model_path: a missing image is
    # a 3GB download, and discovering that from a docker error mid-job is worse
    # than being told before anything starts.
    _require_image(ctx, tool.path, settings.deepvariant_image)

    ctx.progress(phase="starting", pct=None, message="starting DeepVariant")
    cmd = variant_runner.build_deepvariant_command(
        image=settings.deepvariant_image,
        bam=bam,
        reference=reference,
        output_vcf=vcf,
        params=params,
    )
    log.info("deepvariant_started", job_id=ctx.job_id, model=params.model_type)

    code = run_subprocess(
        ctx, cmd, log_path=str(log_path), parser=variant_runner.VariantProgress()
    )
    if code != 0:
        raise _failure(code, log_path, "deepvariant")

    if not vcf.exists():
        raise RetryableError("DeepVariant exited 0 but produced no VCF")

    return vcf


def _require_image(ctx: JobContext, docker_path: str, image: str) -> None:
    """A guard against a bug, not a first-run instruction.

    Before `install_tool` (queue/tool_handlers.py) and the confirm-then-chain
    consent flow (pipeline_service._require_or_offer_install), an absent
    image was the expected first-run state and this function's job was to
    turn that into an actionable "run `docker pull` yourself" message.

    It no longer is. `launch_variant_calling` now refuses to enqueue this job
    at all for a not-yet-installed DeepVariant unless the caller consented,
    in which case it chains `call_variants` behind an `install_tool` job via
    `depends_on` -- so by the time this handler runs, the image is either
    present or the dependency itself already failed and this job was never
    dispatched (see `queue._failed_dependencies`). Reaching this branch with
    the image still absent is therefore a bug in that chain, not a state a
    user is expected to hit or recover from by opening a terminal -- the
    message reflects that.
    """
    import subprocess as _sp

    probe = _sp.run(
        [docker_path, "image", "inspect", image],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        raise PermanentError(
            f"The DeepVariant image {image} is not present, even though this "
            f"job was launched as though it were. This points at a bug in "
            f"the install-then-launch chain, not something to fix by hand.",
            details={"image": image},
        )


def _rename_output(ctx: JobContext, produced: Path, bam: Path, caller: str) -> Path:
    """Give the caller's fixed output filename a name derived from the input.

    Clair3 always writes `merge_output.vcf.gz`; two runs would be
    indistinguishable in the object store.
    """
    name = ctx.payload.get("output_name") or variant_runner.output_name(bam.name, caller)
    final = produced.parent / name
    if final != produced:
        produced.rename(final)
    return final


def _index_vcf(ctx: JobContext, vcf: Path, log_path: Path) -> Path:
    """Index the VCF, returning the .tbi path.

    Always bcftools, whichever caller produced the file: it is installed
    unconditionally and knows the compression of what it reads.
    """
    tool = tools.require(tools.bcftools())

    ctx.progress(phase="indexing", pct=None, message="indexing variants")
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


@handler(
    "run_vcf_stats",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
)
def run_vcf_stats(ctx: JobContext) -> dict:
    """Summary statistics and the per-variant table for the Results tab.

    Read-only, like run_bam_stats: derives no objects except the regenerable
    TSV and SQLite database. The bounded summary returns as facts for
    `_apply_run_vcf_stats` to merge; the per-variant detail goes to
    settings.vcf_stats_dir and is referenced by filename.

    The query output is consumed as a stream and fed to the database builder
    and the density accumulator together, so a file with tens of millions of
    variants is read once and never held in memory.
    """
    bcftools = tools.require(tools.bcftools())

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("run_vcf_stats requires an 'object_id'")

    work = _prepare_workdir(ctx, "vcf_stats")

    vcf_name = Path(ctx.payload.get("vcf_name") or "variants.vcf.gz").name
    vcf = work / vcf_name
    vcf.unlink(missing_ok=True)
    vcf.symlink_to(_resolve_blob(ctx.payload, "vcf"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="stats", pct=0.1, message="summarising the call set")
    stats_path = work / "stats.txt"
    code = run_subprocess(
        ctx,
        vcf_stats_runner.build_stats_command(bcftools_path=bcftools.path, vcf=vcf),
        log_path=str(stats_path),
    )
    if code != 0:
        raise _failure(code, stats_path, "bcftools stats")
    stats = vcf_stats_runner.parse_stats(stats_path.read_text(errors="replace"))

    ctx.progress(phase="query", pct=0.4, message="extracting variants")
    query_path = work / "variants.tsv"
    code = run_subprocess(
        ctx,
        vcf_stats_runner.build_query_command(bcftools_path=bcftools.path, vcf=vcf),
        log_path=str(query_path),
    )
    if code != 0:
        raise _failure(code, query_path, "bcftools query")

    # Contig lengths come from the payload -- the ingest parser already read
    # them from the header, and the handler cannot query for them.
    contig_lengths = [
        (name, int(length))
        for name, length in (ctx.payload.get("contig_lengths") or [])
    ]
    density = vcf_stats_runner.DensityAccumulator(contig_lengths=contig_lengths)
    consequences = vcf_stats_runner.ConsequenceAccumulator()
    filter_counts: dict[str, int] = {}

    def _rows():
        """One pass: every line reaches the database, the density bins, the
        FILTER tally, and the consequence/severe-variant accumulator without
        the file being read more than once.

        Field 6 (index 6) is BCSQ -- ahead of the repeating sample block, see
        QUERY_FORMAT's docstring in vcf_stats_runner.py -- and present
        whether or not the VCF was annotated: an un-annotated file's rows
        carry "." there via bcftools query's -u flag, which parse_bcsq (and
        so ConsequenceAccumulator.add) already treats as "no consequence".
        """
        with open(query_path, errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 8:
                    continue
                try:
                    pos = int(parts[1])
                except ValueError:
                    continue
                filter_counts[parts[5]] = filter_counts.get(parts[5], 0) + 1
                density.add(parts[0], pos, ref=parts[2], alt=parts[3])
                consequences.add(chrom=parts[0], pos=pos, bcsq=parts[6])
                yield line

    ctx.progress(phase="index", pct=0.7, message="building the variant index")
    report_dir = settings.vcf_stats_dir / str(object_id)
    report_dir.mkdir(parents=True, exist_ok=True)

    # Built at a temporary path and renamed into place, so a failed recompute
    # leaves the previous working database rather than a half-built one the
    # table would query.
    tmp_db = report_dir / "variants.db.tmp"
    total = variant_db.build_variant_db(rows=_rows(), db_path=tmp_db)
    tmp_db.replace(report_dir / "variants.db")

    # The downloadable export, beside the database. Moved rather than copied:
    # bcftools already wrote it.
    query_path.replace(report_dir / "variants.tsv")

    summary = vcf_stats_runner.variant_summary(stats, filter_counts=filter_counts)

    facts = {
        "vcf_stats_status": "ok",
        "vcf_stats_tool_version": bcftools.version,
        "vcf_stats_summary": summary,
        "vcf_stats_qual_histogram": vcf_stats_runner.rebin_distribution(
            stats["qual"], value_key="qual"
        ),
        "vcf_stats_depth_histogram": vcf_stats_runner.rebin_distribution(
            stats["dp"], value_key="depth"
        ),
        "vcf_stats_substitutions": stats["st"],
        "vcf_stats_indel_lengths": stats["idd"],
        "vcf_stats_filters": [
            {"filter": k, "count": v}
            for k, v in sorted(filter_counts.items(), key=lambda kv: -kv[1])
        ],
        "vcf_stats_density_bins": density.bins(),
        "vcf_stats_density_bounds": density.boundaries(),
        "vcf_stats_contigs": density.contigs(),
        "vcf_stats_report": "variants.tsv",
        "vcf_stats_db": "variants.db",
        "consequence_counts": consequences.consequence_counts(),
        "severe_variants": consequences.severe_variants(),
    }

    log.info("vcf_stats_done", object_id=object_id, variants=total)
    return {"object_id": object_id, "facts": facts}


def _csq_line_logger(ctx: JobContext) -> "callable":
    """A line callback that classifies `csq`'s stderr as it streams.

    Real NCBI GFF3 files emit parse warnings on every successful run -- see
    `csq_runner.is_benign_gff_warning` -- so those are logged at debug and
    everything else at warning, rather than either failing the job on routine
    noise or hiding an unrecognised line that might matter.
    """

    def on_line(line: str) -> None:
        if not line.strip():
            return
        if csq_runner.is_benign_gff_warning(line):
            log.debug("csq_gff_warning", job_id=ctx.job_id, line=line)
        else:
            log.warning("csq_stderr", job_id=ctx.job_id, line=line)

    return on_line


@handler(
    "annotate_variants",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
)
def annotate_variants(ctx: JobContext) -> dict:
    """Add consequence annotations to a VCF with `bcftools csq`.

    Produces a new VCF object rather than mutating the input: the original is
    what the caller actually emitted, and an annotation run is a derivation of
    it like every other step here.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("annotate_variants requires an 'object_id'")
    if not (ctx.payload.get("reference_sha256") or ctx.payload.get("reference_path")):
        raise PermanentError("annotate_variants requires a reference")
    if not (
        ctx.payload.get("annotation_sha256") or ctx.payload.get("annotation_path")
    ):
        raise PermanentError("annotate_variants requires an annotation (GFF3)")

    bcftools = tools.require(tools.bcftools_csq())
    work = _prepare_workdir(ctx, "annotate")

    vcf = _named_link(
        work, _resolve_blob(ctx.payload, "vcf"), ctx.payload.get("vcf_name")
    )
    annotation = _named_link(
        work,
        _resolve_blob(ctx.payload, "annotation"),
        ctx.payload.get("annotation_name"),
    )

    # The reference and its .fai, laid out as real siblings the way
    # call_variants does -- not a symlink `samtools faidx` would resolve
    # through, writing a stray `.fai` into the content-addressed store beside
    # the blob instead of beside the name csq actually looks next to.
    ref_name = Path(ctx.payload.get("reference_name") or "reference.fa").name
    materialized = aligners.materialize(
        workdir=work / "ref",
        reference_name=ref_name,
        reference_blob=_resolve_blob(ctx.payload, "reference"),
        sidecars={f"{ref_name}{aligners.FAI_SUFFIX}": str(_resolve_blob(ctx.payload, "fai"))},
    )
    if materialized.missing_index:
        raise PermanentError(
            f"The reference index for {ref_name!r} could not be laid out. Its "
            f".fai may be recorded against a different reference."
        )
    reference = materialized.reference

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # csq's own log, separate from the shared job log: `_failure` only shows
    # the last 5 lines, and a real NCBI GFF3 buries a genuine failure under
    # routine "unknown biotype" parse warnings if the two share one file.
    csq_log_path = work / "csq.log"

    ctx.progress(phase="annotate", pct=0.3, message="calling consequences")
    out = work / "annotated.vcf.gz"
    code = run_subprocess(
        ctx,
        csq_runner.build_csq_command(
            bcftools_path=bcftools.path,
            vcf=vcf,
            reference=reference,
            annotation=annotation,
            out=out,
        ),
        log_path=str(csq_log_path),
        on_line=_csq_line_logger(ctx),
    )
    if code != 0:
        raise _failure(code, csq_log_path, "bcftools csq")

    # Not `_rename_output`: it calls `variant_runner.output_name`, which takes
    # `Path(name).stem` on the assumption its input is a BAM. Handed a
    # `.vcf.gz` that yields `foo.bcftools.vcf.csq.vcf.gz`.
    name = csq_runner.annotated_name(ctx.payload.get("vcf_name") or vcf.name)
    produced = out.parent / name
    if produced != out:
        out.rename(produced)
    index = _index_vcf(ctx, produced, log_path)

    ctx.progress(phase="done", pct=1.0, message="annotation complete")
    log.info("annotate_variants_finished", job_id=ctx.job_id, output=produced.name)

    return {
        "object_id": object_id,
        "reference_object_id": ctx.payload.get("reference_object_id"),
        "annotation_object_id": ctx.payload.get("annotation_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "output": {"tmp_path": str(produced), "name": produced.name},
        "index": {"tmp_path": str(index), "name": index.name, "role": "tbi"},
        "tool": "bcftools csq",
        "tool_version": bcftools.version,
    }
