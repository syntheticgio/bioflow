"""Expression job handlers: quantify, differential_expression.

Split from the other handler modules because these two share a problem none of
the others have. Every pipeline before this one is a function of one file, or
of one file and a reference; `differential_expression` is the first that takes
N inputs and a design, and the first whose correctness depends on those N
inputs agreeing with each other. The checks that enforce that agreement are
what most of this file is.

`quantify` sits here rather than in `align_handlers.py` despite consuming a
BAM: it is the other half of the same user-facing feature, and the merge in
`differential_expression` depends closely on what it writes.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import counts_runner, de_runner, tools
from app.queue.align_handlers import _resolve_blob
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


@handler(
    "quantify",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # featureCounts holds the annotation's feature index in memory and streams
    # the BAM past it, so memory scales with the annotation rather than the
    # alignment. 4 GB covers a vertebrate GTF with room to spare.
    # LIGHT for the same reason run_bam_stats is: featureCounts makes one
    # sequential pass over the BAM and never seeks, which is a lighter load
    # than the random access an alignment does.
    resources=JobResources(cpu=4, mem_mb=4096, io=IoClass.LIGHT),
    # Deterministic: the same BAM and annotation produce the same counts, so a
    # failure is a real problem rather than a transient one.
    max_attempts=2,
)
def quantify(ctx: JobContext) -> dict:
    """Count reads per gene for one aligned sample.

    One BAM per job rather than featureCounts' native multi-BAM mode. That
    costs the guarantee of a shared gene order across samples -- which
    `differential_expression` then has to verify rather than assume -- and buys
    the property that adding a thirteenth sample costs one job instead of
    redoing twelve, and that a per-sample count is a first-class object with
    its own provenance.

    Runs off the event loop in a worker thread and so cannot touch the
    database: it returns a plain dict for `results._apply_quantify`.
    """
    featurecounts = tools.require(tools.featurecounts())

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("quantify requires an 'object_id'")

    work = _prepare_workdir(ctx, "quantify")

    bam_name = Path(ctx.payload.get("bam_name") or "aligned.bam").name
    bam = work / bam_name
    bam.unlink(missing_ok=True)
    bam.symlink_to(_resolve_blob(ctx.payload, "bam"))

    annotation_name = Path(
        ctx.payload.get("annotation_name") or "annotation.gtf"
    ).name
    annotation = work / annotation_name
    annotation.unlink(missing_ok=True)
    annotation.symlink_to(_resolve_blob(ctx.payload, "annotation"))

    # `paired` is decided at launch, where the BAM's facts and its alignment
    # run are both readable -- see pipeline_service.paired_for_bam. It is not
    # re-derived here: the only way to answer it from the file is to read the
    # records, and `run_subprocess` streams to completion, so a probe that
    # wanted the first thousand records would decompress the entire BAM to get
    # them. Paying an extra full pass over the alignment to re-answer a
    # question the launch path already answered is a bad trade.
    params = counts_runner.CountsParams.from_dict(ctx.payload.get("params"))

    output = work / counts_runner.output_name(bam_name)
    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(
        phase="counting",
        pct=0.2,
        message=f"counting {counts_runner.describe(params)}",
    )
    cmd = counts_runner.build_command(
        bam=bam,
        annotation=annotation,
        output=output,
        params=params,
        featurecounts_path=featurecounts.path,
    )
    log.info(
        "featurecounts_started",
        job_id=ctx.job_id,
        strandedness=params.strandedness,
        paired=params.paired,
        attribute=params.attribute,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "featureCounts")

    if not output.exists():
        raise PermanentError(
            "featureCounts reported success but wrote no counts file",
            details={"expected": str(output)},
        )

    ctx.progress(phase="summarizing", pct=0.9, message="reading assignment summary")

    facts: dict = {}
    summary = counts_runner.summary_path(output)
    if summary.exists():
        facts.update(counts_runner.parse_summary(summary.read_text(errors="replace")))
    _, table_facts = counts_runner.parse_counts(output.read_text(errors="replace"))
    facts.update(table_facts)

    # The two silent failures this pipeline has -- wrong strandedness and a
    # mismatched annotation -- both surface here and nowhere else. Logged at
    # warning so they appear without a user having to open the object, and
    # left as a warning rather than an error because a genuinely empty sample
    # is a real thing that should still produce a file.
    assigned_pct = facts.get("assigned_pct")
    if assigned_pct is not None and assigned_pct < 5:
        log.warning(
            "low_assignment_rate",
            job_id=ctx.job_id,
            assigned_pct=assigned_pct,
            strandedness=params.strandedness,
            attribute=params.attribute,
            hint=(
                "near-zero assignment usually means the strandedness does not "
                "match the library, or the annotation does not match the "
                "reference the BAM was aligned to"
            ),
        )
        facts["low_assignment_warning"] = True

    log.info(
        "featurecounts_done",
        job_id=ctx.job_id,
        assigned_pct=assigned_pct,
        genes_detected=facts.get("genes_detected"),
    )

    return {
        "object_id": object_id,
        "annotation_object_id": ctx.payload.get("annotation_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "output": {"tmp_path": str(output), "name": output.name},
        "tool_version": featurecounts.version,
        "params": params.as_dict(),
        # Carried so the merge in differential_expression can refuse inputs
        # counted against different annotations -- see de_runner.merge_counts.
        "annotation_name": annotation_name,
        "annotation_sha256": ctx.payload.get("annotation_sha256"),
        "facts": facts,
        "workdir": str(work),
    }


@handler(
    "differential_expression",
    # THREAD, not SUBPROCESS: PyDESeq2 is a library and runs in this process.
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    # Scales with genes x samples, both modest. The dispersion fit is the
    # expensive part and is CPU-bound.
    resources=JobResources(cpu=4, mem_mb=4096, io=IoClass.LIGHT),
    max_attempts=2,
)
def differential_expression(ctx: JobContext) -> dict:
    """Test per-gene counts between two conditions.

    The first handler here that fans in. Its inputs are N counts objects that
    were produced by N independent jobs, possibly days apart and possibly
    against different annotations, so it verifies they agree before it merges
    them -- `de_runner.merge_counts` raises rather than inner-joining, because
    an inner join across two gene universes produces a result that looks
    entirely normal and is computed over the wrong denominator.

    Runs in a worker thread and so cannot touch the database: it returns a
    plain dict for `results._apply_differential_expression`.
    """
    tools.require(tools.pydeseq2())

    samples = ctx.payload.get("samples") or []
    if not samples:
        raise PermanentError("differential_expression requires 'samples'")

    contrast = ctx.payload.get("contrast") or {}
    if not contrast.get("test") or not contrast.get("reference"):
        raise PermanentError(
            "differential_expression requires a contrast with 'test' and "
            "'reference' condition names"
        )

    work = _prepare_workdir(ctx, "differential_expression")

    ctx.progress(phase="reading", pct=0.05, message="reading count files")

    loaded: list[de_runner.SampleCounts] = []
    for entry in samples:
        path = _resolve_blob(entry, "counts")
        text = Path(path).read_text(errors="replace")
        counts, _ = counts_runner.parse_counts(text)
        loaded.append(
            de_runner.SampleCounts(
                sample=entry.get("sample") or entry.get("name") or str(path),
                condition=entry["condition"],
                counts=counts,
                annotation_sha256=entry.get("annotation_sha256"),
                object_id=entry.get("counts_object_id"),
            )
        )

    ctx.progress(phase="merging", pct=0.15, message="merging counts into a matrix")
    matrix = de_runner.merge_counts(loaded)

    ctx.progress(phase="fitting", pct=0.3, message="fitting dispersions")
    result = de_runner.run_deseq2(
        matrix,
        test=contrast["test"],
        reference=contrast["reference"],
        threads=int(ctx.payload.get("threads", 4)),
        on_phase=lambda phase, pct, msg: ctx.progress(
            phase=phase, pct=pct, message=msg
        ),
    )

    output = work / de_runner.output_name(contrast["test"], contrast["reference"])
    output.write_text(result.to_tsv())

    log.info(
        "deseq2_done",
        job_id=ctx.job_id,
        samples=len(loaded),
        genes_tested=result.facts.get("genes_tested"),
        significant=result.facts.get("significant_genes"),
    )

    return {
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "output": {"tmp_path": str(output), "name": output.name},
        "counts_object_ids": [
            s.object_id for s in loaded if s.object_id is not None
        ],
        "tool_version": tools.pydeseq2().version,
        "params": {
            "contrast": contrast,
            "design": {s.sample: s.condition for s in loaded},
        },
        "facts": result.facts,
        "workdir": str(work),
    }
