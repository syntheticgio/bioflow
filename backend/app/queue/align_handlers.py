"""Alignment job handlers: build_index, align_reads, index_bam.

Split from `pipeline_handlers.py` because these share a distinct problem the
trimming handlers do not have: every one of them needs a reference laid out on
disk under the names its tool expects, which content-addressed storage does not
provide. `aligners.materialize` is the shared answer, and these are its callers.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import align_runner, aligners, bam_stats_runner, tools
from app.pipelines.aligners import Aligner
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler
from app.storage.paths import blob_path

log = get_logger(__name__)


def _aligner_tool(aligner: Aligner):
    return tools.bwa_mem2() if aligner is Aligner.BWA_MEM2 else tools.minimap2()


def _resolve_blob(payload: dict, key: str) -> Path:
    """Locate an input by digest or explicit path.

    Registered-in-place files have no managed blob to address by hash, so the
    external path is the only way to reach them.
    """
    digest = payload.get(f"{key}_sha256")
    path_str = payload.get(f"{key}_path")

    if path_str:
        path = Path(path_str)
    elif digest:
        path = blob_path(digest)
    else:
        raise PermanentError(f"Job requires '{key}_sha256' or '{key}_path'")

    if not path.exists():
        # Permanent rather than retryable: a blob missing now will still be
        # missing in thirty seconds, and file verification is what reports
        # storage problems.
        raise PermanentError(f"Input not found: {path}")
    return path


def _materialize(ctx: JobContext, work: Path, payload: dict) -> aligners.MaterializedRef:
    """Lay out the reference and its sidecars under the names tools expect."""
    return aligners.materialize(
        workdir=work / "ref",
        reference_name=payload.get("reference_name") or "reference.fa",
        reference_blob=_resolve_blob(payload, "reference"),
        sidecars=payload.get("sidecars") or {},
    )


@handler(
    "build_index",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    # As for trimming: an index failure is almost always deterministic, and
    # retrying a long build delays the error without making it less likely.
    max_attempts=2,
)
def build_index(ctx: JobContext) -> dict:
    """Build an aligner index for one reference, plus its `.fai`.

    Produces sidecar objects rather than files beside the reference, because
    the reference is a content-addressed blob with no siblings. Keyed by
    content at launch, so the same genome registered in two projects shares one
    index with no cross-project bookkeeping.

    Runs off the event loop in a worker thread and so cannot touch the
    database: it returns a plain dict for `results._apply_build_index`.
    """
    aligner = Aligner(ctx.payload.get("aligner", Aligner.MINIMAP2))
    tool = tools.require(_aligner_tool(aligner))
    samtools = tools.require(tools.samtools())

    work = _prepare_workdir(ctx, "align")
    ref = _materialize(ctx, work, ctx.payload)
    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    produced: list[dict] = []

    # samtools faidx first: it is seconds of work against minutes or hours for
    # the aligner index, so a reference that cannot be indexed at all fails
    # fast rather than after a long build.
    ctx.progress(phase="faidx", pct=0.02, message="indexing reference sequence")
    code = run_subprocess(
        ctx,
        align_runner.build_faidx_command(
            samtools_path=samtools.path, reference=ref.reference
        ),
        log_path=str(log_path),
    )
    if code != 0:
        raise _failure(code, log_path, "samtools faidx")

    fai = ref.reference.parent / f"{ref.reference.name}{aligners.FAI_SUFFIX}"
    if fai.exists():
        produced.append({"tmp_path": str(fai), "name": fai.name, "role": "fai"})

    ctx.progress(phase="indexing", pct=0.1, message=f"building {aligner.value} index")

    if aligner is Aligner.BWA_MEM2:
        cmd = align_runner.build_index_command(
            aligner=aligner, tool_path=tool.path, reference=ref.reference
        )
    else:
        cmd = align_runner.build_index_command(
            aligner=aligner,
            tool_path=tool.path,
            reference=ref.reference,
            output=ref.reference.parent
            / f"{ref.reference.name}{aligners.MINIMAP2_SUFFIX}",
        )

    log.info(
        "index_build_started", job_id=ctx.job_id, aligner=aligner.value, cmd=" ".join(cmd)
    )
    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, aligner.value)

    index_role = aligners.INDEX_ROLE[aligner].value
    for name in aligners.index_filenames(ref.reference.name, aligner):
        path = ref.reference.parent / name
        if not path.exists():
            # A missing member is not a degraded index: the tool refuses to
            # load the set. Better to fail here than at the first alignment.
            raise RetryableError(
                f"{aligner.value} exited 0 but did not produce {name}"
            )
        produced.append({"tmp_path": str(path), "name": name, "role": index_role})

    ctx.progress(phase="done", pct=1.0, message="index built")
    log.info("index_build_finished", job_id=ctx.job_id, files=len(produced))

    return {
        "reference_object_id": ctx.payload.get("reference_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "aligner": aligner.value,
        "tool_version": tool.version,
        "outputs": produced,
        "workdir": str(work),
    }


@handler(
    "align_reads",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # cpu is overridden per job with the user's thread count, exactly as
    # trim_reads does -- see pipeline_service.launch_alignment.
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    max_attempts=2,
)
def align_reads(ctx: JobContext) -> dict:
    """Align reads against a reference, producing a coordinate-sorted BAM.

    Align and sort are one job because piping into `samtools sort` never
    materializes the intermediate SAM, which is several times the size of the
    resulting BAM and pure waste to write. Indexing is a separate follow-on
    job: it is fast, independently useful, and separable.
    """
    aligner = Aligner(ctx.payload.get("aligner", Aligner.MINIMAP2))
    tool = tools.require(_aligner_tool(aligner))
    samtools = tools.require(tools.samtools())

    params = align_runner.AlignParams.from_dict(ctx.payload.get("params"))
    read_group = align_runner.ReadGroup.from_dict(ctx.payload.get("read_group"))

    work = _prepare_workdir(ctx, "align")
    ref = _materialize(ctx, work, ctx.payload)
    if ref.missing_index:
        # The dependency gate should have made this impossible; if it happens,
        # the index job's sidecars did not reach the payload. Permanent because
        # a retry would materialize the same empty set.
        raise PermanentError(
            f"Reference has no {aligner.value} index available to this job"
        )

    r1 = _resolve_blob(ctx.payload, "r1")
    r2 = _resolve_blob(ctx.payload, "r2") if ctx.payload.get("r2_sha256") else None

    # Named links for the reads too. Aligners infer gzip from the filename in
    # the same way fastp does, which is the Phase 6a failure exactly.
    r1 = _named_read_link(work, r1, ctx.payload.get("r1_name"))
    if r2 is not None:
        r2 = _named_read_link(work, r2, ctx.payload.get("r2_name"))

    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)
    bam_name = ctx.payload.get("output_name") or "aligned.bam"
    bam_out = out_dir / bam_name

    cmd = align_runner.build_align_command(
        aligner=aligner,
        aligner_path=tool.path,
        samtools_path=samtools.path,
        reference=ref.reference,
        r1=r1,
        r2=r2,
        output=bam_out,
        read_group=read_group,
        params=params,
        tmp_prefix=work / "sort",
    )

    progress = align_runner.AlignProgress(expected_reads=ctx.payload.get("expected_reads"))
    ctx.progress(phase="starting", pct=0.0, message=f"starting {aligner.value}")

    def on_line(line: str) -> None:
        if progress.feed(line):
            ctx.progress(pct=progress.pct, phase=progress.phase, message=progress.message())

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log.info(
        "align_started",
        job_id=ctx.job_id,
        aligner=aligner.value,
        paired=r2 is not None,
        threads=params.threads,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path), on_line=on_line)
    if code != 0:
        # pipefail is what makes this reachable when the *aligner* fails: the
        # exit status of a pipe is otherwise samtools', which would report
        # success over a truncated BAM.
        raise _failure(code, log_path, f"{aligner.value} | samtools sort")

    if not bam_out.exists() or bam_out.stat().st_size == 0:
        raise RetryableError("alignment exited 0 but produced no BAM")

    if params.mark_duplicates:
        ctx.progress(phase="markdup", pct=0.97, message="marking duplicates")
        marked = out_dir / f"markdup.{bam_name}"
        code = run_subprocess(
            ctx,
            align_runner.build_markdup_command(
                samtools_path=samtools.path,
                source=bam_out,
                output=marked,
                threads=params.threads,
                paired=r2 is not None,
                tmp_prefix=work / "markdup-sort",
            ),
            log_path=str(log_path),
        )
        if code != 0:
            raise _failure(code, log_path, "samtools markdup")
        marked.replace(bam_out)

    ctx.progress(phase="done", pct=1.0, message="alignment complete")
    log.info("align_finished", job_id=ctx.job_id, size=bam_out.stat().st_size)

    return {
        "object_id": ctx.payload.get("object_id"),
        "mate_object_id": ctx.payload.get("mate_object_id"),
        "reference_object_id": ctx.payload.get("reference_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "output": {"tmp_path": str(bam_out), "name": bam_name},
        "params": params.as_dict(),
        "read_group": read_group.as_dict(),
        "aligner": aligner.value,
        "tool_version": tool.version,
        "samtools_version": samtools.version,
        "workdir": str(work),
    }


def _named_read_link(work: Path, target: Path, name: str | None) -> Path:
    """A symlink to a read file under its user-facing name.

    Same reasoning as the trimming handler: tools infer compression from the
    filename, and a managed blob has no extension.
    """
    if not name:
        return target
    link = work / f"in_{Path(name).name}"
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(target)
    except OSError as e:
        log.warning("read_link_failed", target=str(target), name=name, error=str(e))
        return target
    return link


@handler(
    "index_bam",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.LIGHT),
    max_attempts=2,
)
def index_bam(ctx: JobContext) -> dict:
    """Index a BAM and read its alignment statistics.

    flagstat runs here rather than as its own job because the file is already
    being traversed, and its four numbers -- alignment rate, properly-paired
    percentage, duplicate rate, total -- are what a person checks before
    trusting an alignment.
    """
    samtools = tools.require(tools.samtools())

    work = _prepare_workdir(ctx, "align")
    bam_source = _resolve_blob(ctx.payload, "bam")

    # samtools writes `<name>.bai` beside its input and infers the format from
    # the filename, so the blob needs its real name here too.
    bam_name = ctx.payload.get("bam_name") or "aligned.bam"
    bam = work / Path(bam_name).name
    bam.unlink(missing_ok=True)
    bam.symlink_to(bam_source)

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="indexing", pct=0.1, message="indexing alignments")
    code = run_subprocess(
        ctx,
        align_runner.build_index_bam_command(samtools_path=samtools.path, bam=bam),
        log_path=str(log_path),
    )
    if code != 0:
        raise _failure(code, log_path, "samtools index")

    bai = bam.parent / f"{bam.name}{aligners.BAI_SUFFIX}"
    if not bai.exists():
        raise RetryableError("samtools index exited 0 but produced no .bai")

    ctx.progress(phase="flagstat", pct=0.6, message="reading alignment statistics")
    flagstat_path = work / "flagstat.txt"
    code = run_subprocess(
        ctx,
        align_runner.build_flagstat_command(samtools_path=samtools.path, bam=bam),
        log_path=str(flagstat_path),
    )
    facts: dict = {}
    if code == 0:
        try:
            facts = align_runner.parse_flagstat(flagstat_path.read_text(errors="replace"))
        except OSError as e:  # noqa: BLE001
            log.warning("flagstat_read_failed", job_id=ctx.job_id, error=str(e))
    else:
        # Not fatal: the index is the deliverable and it succeeded. Statistics
        # are worth having but not worth discarding a good index over.
        log.warning("flagstat_failed", job_id=ctx.job_id, code=code)

    ctx.progress(phase="done", pct=1.0, message="indexing complete")
    log.info("index_bam_finished", job_id=ctx.job_id, mapped_pct=facts.get("mapped_pct"))

    return {
        "bam_object_id": ctx.payload.get("bam_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "output": {"tmp_path": str(bai), "name": bai.name, "role": "bai"},
        "facts": facts,
        "tool_version": samtools.version,
        "workdir": str(work),
        # Carried through so `_apply_index_bam` knows to chain the Results
        # computation that requested this index -- see
        # pipeline_service.launch_bam_stats.
        "then_bam_stats": ctx.payload.get("then_bam_stats", False),
    }


@handler(
    "run_bam_stats",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # LIGHT io: idxstats reads only the .bai, and coverage/depth are each one
    # sequential pass -- lighter than the random access an alignment does.
    resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.LIGHT),
    max_attempts=2,
)
def run_bam_stats(ctx: JobContext) -> dict:
    """Coverage, per-contig, and binned-depth statistics for the Results tab.

    Read-only, like run_qc: derives no files except the regenerable per-contig
    TSV report. The bounded summary (binned depth, top-N contigs) returns as
    facts for `_apply_run_bam_stats` to merge onto the object; the complete
    per-contig table is written straight to settings.bam_stats_dir and
    referenced by filename.

    Prerequisites (coordinate sort, presence of a .bai) are checked before
    this job is even enqueued -- see pipeline_service.launch_bam_stats -- so a
    failure here is an actual tool problem, not a missing precondition.
    """
    samtools = tools.require(tools.samtools())

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("run_bam_stats requires an 'object_id'")

    work = _prepare_workdir(ctx, "bam_stats")

    bam_name = Path(ctx.payload.get("bam_name") or "aligned.bam").name
    bam = work / bam_name
    bam.unlink(missing_ok=True)
    bam.symlink_to(_resolve_blob(ctx.payload, "bam"))

    # Same convention call_variants uses: the .bai as a sibling of the BAM,
    # under the name samtools itself expects (<name>.bam.bai).
    bai = work / f"{bam_name}{aligners.BAI_SUFFIX}"
    bai.unlink(missing_ok=True)
    bai.symlink_to(_resolve_blob(ctx.payload, "bai"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="idxstats", pct=0.1, message="reading index statistics")
    idxstats_path = work / "idxstats.txt"
    code = run_subprocess(
        ctx,
        bam_stats_runner.build_idxstats_command(samtools_path=samtools.path, bam=bam),
        log_path=str(idxstats_path),
    )
    if code != 0:
        raise _failure(code, idxstats_path, "samtools idxstats")
    idxstats_rows = bam_stats_runner.parse_idxstats(idxstats_path.read_text(errors="replace"))

    ctx.progress(phase="coverage", pct=0.3, message="computing per-contig coverage")
    coverage_path = work / "coverage.txt"
    code = run_subprocess(
        ctx,
        bam_stats_runner.build_coverage_command(samtools_path=samtools.path, bam=bam),
        log_path=str(coverage_path),
    )
    if code != 0:
        raise _failure(code, coverage_path, "samtools coverage")
    coverage_rows = bam_stats_runner.parse_coverage(coverage_path.read_text(errors="replace"))

    contigs = bam_stats_runner.contigs_from_coverage(
        idxstats_rows=idxstats_rows, coverage_rows=coverage_rows
    )

    ctx.progress(phase="depth", pct=0.5, message="binning coverage across the reference")
    depth_path = work / "depth.txt"
    code = run_subprocess(
        ctx,
        bam_stats_runner.build_depth_command(samtools_path=samtools.path, bam=bam),
        log_path=str(depth_path),
    )
    if code != 0:
        raise _failure(code, depth_path, "samtools depth")

    contig_lengths = [(c["contig"], c["length"]) for c in contigs]
    with open(depth_path, errors="replace") as fh:
        bins, boundaries = bam_stats_runner.bin_depth(
            contig_lengths=contig_lengths, depth_lines=fh
        )

    cumulative = bam_stats_runner.cumulative_coverage(
        bins=bins, thresholds=list(bam_stats_runner.COVERAGE_THRESHOLDS)
    )
    summary = bam_stats_runner.genome_summary(contigs=contigs, bins=bins)

    ctx.progress(phase="report", pct=0.9, message="writing the per-contig report")
    report_dir = settings.bam_stats_dir / str(object_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_name = "contigs.tsv"
    (report_dir / report_name).write_text(bam_stats_runner.contigs_tsv(contigs))

    # Capped for facts storage; the full table is the TSV written above.
    top_n = contigs[:50]

    facts = {
        "bam_stats_status": "ok",
        "bam_stats_tool_version": samtools.version,
        "bam_stats_computed_at": datetime.now(UTC).isoformat(),
        "bam_stats_summary": summary,
        "bam_stats_coverage_bins": bins,
        "bam_stats_coverage_boundaries": boundaries,
        "bam_stats_cumulative": cumulative,
        "bam_stats_contigs_top": top_n,
        "bam_stats_report": report_name,
    }

    ctx.progress(phase="done", pct=1.0, message="results complete")
    log.info(
        "bam_stats_finished",
        job_id=ctx.job_id,
        object_id=object_id,
        contigs=len(contigs),
        mean_depth=summary.get("mean_depth"),
    )

    return {
        "object_id": object_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }
