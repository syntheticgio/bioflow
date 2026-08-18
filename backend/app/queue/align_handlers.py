"""Alignment job handlers: build_index, align_reads, index_bam.

Split from `pipeline_handlers.py` because these share a distinct problem the
trimming handlers do not have: every one of them needs a reference laid out on
disk under the names its tool expects, which content-addressed storage does not
provide. `aligners.materialize` is the shared answer, and these are its callers.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import gzip
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import (
    align_params,
    align_runner,
    aligner_registry,
    aligners,
    bam_stats_runner,
    tools,
    winnowmap_runner,
)
from app.pipelines.aligners import Aligner
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler
from app.storage.paths import blob_path

log = get_logger(__name__)


def _aligner_tool(aligner: Aligner):
    """The probe for one aligner.

    A registry lookup rather than an if/else: the old form returned minimap2
    for anything that was not bwa-mem2, so a new aligner would silently run
    the wrong binary against the right index.
    """
    return aligner_registry.spec_for(aligner).tool()


def _index_tool(aligner: Aligner, aligner_tool: tools.Tool) -> tools.Tool:
    """Which `Tool` builds this aligner's index.

    bowtie2 and HISAT2 index through a separate binary from the one that
    aligns (`aligner_registry.spec_for(...).builder_tool`, the callable
    counterpart of the bare name in `aligners.layout_for(...).builder`), so
    the tool whose version was probed as `aligner_tool` is not always the one
    that builds the index. Resolved the same way every other tool path in
    this codebase is -- through a probe over `settings.<name>_path` -- rather
    than `shutil.which` on the builder's bare name, which would bypass the
    user-overridable setting Task 5 already added for exactly this binary.

    Kept separate from command construction so the tool-selection branch --
    the one place a copy-paste could silently swap in the wrong binary's path
    -- is unit-testable without building a whole JobContext.
    """
    builder_tool = aligner_registry.spec_for(aligner).builder_tool
    if builder_tool is not None:
        return tools.require(builder_tool())
    return aligner_tool


def _resolve_digest_or_path(
    digest: str | None, path_str: str | None, *, missing_message: str
) -> Path:
    """Locate a file by content digest or explicit path, and confirm it exists.

    Shared by `_resolve_blob` (payload keys named `{key}_sha256`/`{key}_path`)
    and the `extra_reads` loops -- each entry's own file under `sha256`/`path`
    and, in a paired run, its mate under `mate_sha256`/`mate_path` -- so the
    naming conventions can each supply their own already-known keys without
    duplicating the resolve-then-verify logic itself.
    """
    if path_str:
        path = Path(path_str)
    elif digest:
        path = blob_path(digest)
    else:
        raise PermanentError(missing_message)

    if not path.exists():
        # Permanent rather than retryable: a blob missing now will still be
        # missing in thirty seconds, and file verification is what reports
        # storage problems.
        raise PermanentError(f"Input not found: {path}")
    return path


def _resolve_blob(payload: dict, key: str) -> Path:
    """Locate an input by digest or explicit path.

    Registered-in-place files have no managed blob to address by hash, so the
    external path is the only way to reach them.
    """
    return _resolve_digest_or_path(
        payload.get(f"{key}_sha256"),
        payload.get(f"{key}_path"),
        missing_message=f"Job requires '{key}_sha256' or '{key}_path'",
    )


def _materialize(
    ctx: JobContext, work: Path, payload: dict, aligner: Aligner | None = None
) -> aligners.MaterializedRef:
    """Lay out the reference and its sidecars under the names tools expect.

    `aligner` selects the index layout, which only matters for the directory
    shape: STAR's members are stored flat and have to be reassembled into a
    `--genomeDir` before STAR can load them.
    """
    layout = aligners.layout_for(aligner) if aligner is not None else None
    return aligners.materialize(
        workdir=work / "ref",
        reference_name=payload.get("reference_name") or "reference.fa",
        reference_blob=_resolve_blob(payload, "reference"),
        sidecars=payload.get("sidecars") or {},
        layout=layout,
    )


def _star_scratch(work: Path, name: str) -> Path:
    """An empty directory for STAR's `--outFileNamePrefix`, recreated.

    Emptied rather than merely created, because of how a retry interacts with
    STAR's own temp directory. STAR refuses to start when the directory it
    wants for scratch (`<prefix>_STARtmp` by default) already exists, and it
    leaves that directory behind when it is killed rather than exiting. A job
    reuses its workdir across attempts, so attempt 2 after an OOM kill would
    fail instantly with "exiting because of fatal ERROR: could not make
    temporary directory" -- an error about the previous failure, reported as
    though it were this one's.
    """
    scratch = work / name
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _is_gzip(path: Path) -> bool:
    """Sniff the gzip magic bytes rather than trusting the `.gz` suffix.

    NCBI assemblies are downloaded and stored compressed, but the object's
    stored name is a user-facing label, not a format guarantee -- registered
    or renamed files can carry a mismatched extension.
    """
    with open(path, "rb") as handle:
        return handle.read(2) == b"\x1f\x8b"


def _ensure_uncompressed(path: Path, dest_dir: Path) -> Path:
    """Decompress a gzipped input for a builder that cannot read one.

    `materialize` symlinks blobs under their stored name with no format
    conversion, so a gzip-compressed reference or annotation -- the normal
    case for anything downloaded from NCBI -- reaches the builder as-is.
    Which builders can cope is declared per aligner by
    `aligner_registry.AlignerSpec.builder_accepts_gzip`, not decided here.

    Two builders cannot: STAR's `genomeGenerate` reads FASTA/GTF as plain
    text and fails with an "is not fasta" or GTF-parsing error, and
    `hisat2-build` exits 1 partway through, deleting the `.ht2` files it had
    already written. Both surface minutes into what looks like a routine
    index build.
    """
    if not _is_gzip(path):
        return path
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / path.with_suffix("").name
    with gzip.open(path, "rb") as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


def _fai_geometry(fai: Path) -> tuple[int, int]:
    """Total genome length and contig count, from a `.fai`.

    STAR's index parameters are derived from these two numbers, and the `.fai`
    is exact where the FASTA's file size is not -- it excludes headers and
    line breaks, which on a 60-column FASTA are about 1.7% of the bytes.
    Column 2 of each line is that sequence's length.
    """
    total = 0
    contigs = 0
    with open(fai) as handle:
        for line in handle:
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            try:
                total += int(fields[1])
            except ValueError:
                continue
            contigs += 1
    return total, contigs


def _run_winnowmap_meryl_index(
    *, ctx: JobContext, meryl_path: str, reference: Path, work: Path, log_path: Path
) -> None:
    """Build winnowmap's repetitive-k-mer file with two meryl commands.

    `k` and `distinct` come from the payload's `params` dict -- the same
    `WinnowmapParams` an align_reads job for this aligner would carry --
    with GCI's own README defaults (k=15, distinct=0.9998) when absent,
    since `build_index` can run before an alignment's params exist.

    Raises via `_failure` on either step, exactly like a single-command
    branch would via the shared `run_subprocess` call in the caller -- kept
    as two explicit calls here rather than forced into that shared call
    because there is no single winnowmap_runner command covering both meryl
    invocations (see that module's docstring).
    """
    params_payload = ctx.payload.get("params") or {}
    k = int(params_payload.get("k") or 15)
    distinct = float(params_payload.get("distinct") or 0.9998)

    database = work / "winnowmap.meryl"
    count_cmd = winnowmap_runner.build_meryl_count_command(
        meryl_path=meryl_path,
        k=k,
        reference=reference,
        output=database,
        threads=max(1, int(ctx.payload.get("threads") or 4)),
    )
    log.info("winnowmap_meryl_count_started", job_id=ctx.job_id, cmd=" ".join(count_cmd))
    code = run_subprocess(ctx, count_cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "meryl count")

    output = reference.parent / (
        f"{reference.name}{aligners.WINNOWMAP_REPETITIVE_KMER_SUFFIX}"
    )
    print_cmd = winnowmap_runner.build_meryl_print_repetitive_shell_command(
        meryl_path=meryl_path, distinct=distinct, database=database, output=output
    )
    log.info("winnowmap_meryl_print_started", job_id=ctx.job_id, cmd=" ".join(print_cmd))
    code = run_subprocess(ctx, print_cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "meryl print greater-than")

    if not output.exists():
        raise RetryableError(
            f"meryl exited 0 but did not produce {output.name}"
        )


@handler(
    "build_index",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # A fallback, not the number this job normally runs with. Every real
    # launch goes through `pipeline_service._enqueue_build_index`, which sizes
    # the reservation from the aligner's memory model and the reference --
    # a STAR human index is ~36 GB, not 8. `default_resources` reaches the
    # queue only via the development enqueue route in `api/v1/jobs.py`, so
    # this stays a plausible generic value rather than being raised to the
    # worst case and starving everything else on that path.
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

    # An annotation is only ever meaningful for STAR -- passing one for any
    # other aligner is a caller bug, not a request to ignore silently, since
    # the caller believed it was doing something the build will not do.
    gtf_payload_present = bool(
        ctx.payload.get("gtf_sha256") or ctx.payload.get("gtf_path")
    )
    if gtf_payload_present and aligner is not Aligner.STAR:
        raise PermanentError(f"{aligner.value} has no annotation-aware index")
    annotated = gtf_payload_present and aligner is Aligner.STAR

    work = _prepare_workdir(ctx, "align")
    ref = _materialize(ctx, work, ctx.payload)
    gtf: Path | None = None
    if annotated:
        gtf = _resolve_blob(ctx.payload, "gtf")
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

    index_tool = _index_tool(aligner, tool)
    # Done once here rather than per branch: a builder that cannot read gzip
    # is a property of the aligner, and deciding it at each call site is what
    # let hisat2-build reach a gzipped reference and fail (#560).
    build_reference = ref.reference
    if not aligner_registry.spec_for(aligner).builder_accepts_gzip:
        build_reference = _ensure_uncompressed(ref.reference, work / "build-input")

    if aligner is Aligner.STAR:
        if not fai.exists():
            # Unreachable in practice -- faidx above either produced it or the
            # job already failed -- but STAR's sizing is derived from it, and
            # falling back to STAR's defaults here would build a mammalian-
            # sized index for a virus and map almost nothing, succeeding.
            raise RetryableError("STAR index needs the .fai that faidx produces")

        genome_dir = ref.reference.parent / aligners.layout_for(
            Aligner.STAR, annotated=annotated
        ).directory_name(ref.reference.name)
        genome_length, contigs = _fai_geometry(fai)
        star_scratch = _star_scratch(work, "index")
        star_gtf = (
            _ensure_uncompressed(gtf, work / "build-input") if gtf is not None else None
        )
        cmd = align_runner.build_star_index_command(
            tool_path=index_tool.path,
            reference=build_reference,
            genome_dir=genome_dir,
            threads=ctx.payload.get("threads") or 4,
            genome_length=genome_length,
            contigs=contigs,
            scratch=star_scratch,
            gtf=star_gtf,
        )
        # STAR requires the directory to exist before it will write into it,
        # unlike every other builder here, which creates its own output.
        genome_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "star_index_sizing",
            job_id=ctx.job_id,
            genome_length=genome_length,
            contigs=contigs,
            annotated=annotated,
        )
    elif aligner is Aligner.MINIMAP2:
        cmd = align_runner.build_index_command(
            aligner=aligner,
            tool_path=index_tool.path,
            reference=ref.reference,
            output=ref.reference.parent
            / f"{ref.reference.name}{aligners.MINIMAP2_SUFFIX}",
        )
    elif aligner is Aligner.WINNOWMAP:
        # Two meryl commands, not one `align_runner.build_index_command`
        # call -- see winnowmap_runner's module docstring for why this is
        # not folded into the shared four-aligner dispatch. `index_tool`
        # here is meryl (`_index_tool` resolves it via `builder_tool`), not
        # winnowmap itself. Both must succeed in order, so this branch runs
        # them directly rather than handing a single `cmd` to the shared
        # run_subprocess call below -- there is no single command to hand.
        _run_winnowmap_meryl_index(
            ctx=ctx,
            meryl_path=index_tool.path,
            reference=ref.reference,
            work=work,
            log_path=log_path,
        )
        cmd = None
    else:
        cmd = align_runner.build_index_command(
            aligner=aligner,
            tool_path=index_tool.path,
            reference=build_reference,
            # The basename stays the stored path even when the builder reads a
            # decompressed copy, so the index files land where the layout below
            # looks for them rather than in the scratch directory.
            output=ref.reference,
        )

    if cmd is not None:
        log.info(
            "index_build_started",
            job_id=ctx.job_id,
            aligner=aligner.value,
            cmd=" ".join(cmd),
        )
        code = run_subprocess(ctx, cmd, log_path=str(log_path))
        if code != 0:
            raise _failure(code, log_path, aligner.value)

    index_role = aligners.index_role(aligner, annotated=annotated).value
    layout = aligners.layout_for(aligner, annotated=annotated)
    for name in aligners.index_filenames(ref.reference.name, aligner, annotated=annotated):
        # `name` is the *stored* name and is what the sidecar record carries;
        # for the directory shape the file is one level down under a different
        # name, which is what `workdir_path` undoes. Identity for the other
        # four aligners, so there is one loop rather than two.
        path = ref.reference.parent / layout.workdir_path(ref.reference.name, name)
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
    # cpu *and* mem_mb are overridden per job -- the thread count from the
    # user, the memory from `resource_estimator` against this aligner and this
    # reference. See pipeline_service.launch_alignment; as with build_index
    # above, these values are only a fallback for the development enqueue
    # route.
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

    params = align_params.from_dict(ctx.payload.get("params"))
    read_group = align_runner.ReadGroup.from_dict(ctx.payload.get("read_group"))

    work = _prepare_workdir(ctx, "align")
    ref = _materialize(ctx, work, ctx.payload, aligner)
    if ref.missing_index_for(aligners.layout_for(aligner), ref.reference.name):
        # The dependency gate should have made this impossible; if it happens,
        # the index job's sidecars did not reach the payload. Permanent because
        # a retry would materialize the same empty set.
        #
        # Asks for *this aligner's* files rather than "was anything linked".
        # A reference carrying only a `.fai` satisfied the weaker check, so a
        # reference whose index was never stored reached the aligner and
        # failed there instead -- STAR reporting a missing genome directory,
        # which reads as a corrupt index rather than an absent one.
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

    extra_reads_payload = ctx.payload.get("extra_reads") or []
    if extra_reads_payload:
        # Every set's R1 concatenates into the primary R1 stream and, in a
        # paired run, every set's mate into the R2 stream, before the aligner
        # ever sees them. See _concatenate_reads for why concatenation is the
        # only approach that works across all six aligners this handler
        # drives; the R1s and R2s are concatenated separately so the mate
        # streams stay symmetric with the reads streams.
        extra_r1_paths, extra_r2_paths = _extra_reads_paths(
            extra_reads_payload, paired=r2 is not None
        )
        r1_name = ctx.payload.get("r1_name") or "reads.fastq"
        combined = work / f"combined_{Path(r1_name).name}"
        r1 = _concatenate_reads(r1, extra_r1_paths, combined)

        if r2 is not None and extra_r2_paths:
            r2_name = ctx.payload.get("r2_name") or "mate.fastq"
            combined_r2 = work / f"combined_{Path(r2_name).name}"
            r2 = _concatenate_reads(r2, extra_r2_paths, combined_r2)

    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)
    bam_name = ctx.payload.get("output_name") or "aligned.bam"
    bam_out = out_dir / bam_name

    star_scratch = _star_scratch(work, "star") if aligner is Aligner.STAR else None

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
        scratch=star_scratch,
    )

    progress = align_runner.AlignProgress(
        name=aligner.value, expected_reads=ctx.payload.get("expected_reads")
    )
    ctx.progress(phase="starting", pct=0.0, message=f"starting {aligner.value}")

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log.info(
        "align_started",
        job_id=ctx.job_id,
        aligner=aligner.value,
        paired=r2 is not None,
        threads=params.threads,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path), parser=progress)
    if code != 0:
        # pipefail is what makes this reachable when the *aligner* fails: the
        # exit status of a pipe is otherwise samtools', which would report
        # success over a truncated BAM.
        raise _failure(code, log_path, f"{aligner.value} | samtools sort")

    if star_scratch is not None:
        _append_star_summary(star_scratch, log_path)

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


def _append_star_summary(scratch: Path, log_path: Path) -> None:
    """Copy STAR's `Log.final.out` into the job log.

    Worth rescuing because it is strictly better than the `samtools flagstat`
    numbers this application shows for every alignment: it separates uniquely
    mapped from multi-mapping reads, splits the unmapped into too-short versus
    too-many-mismatches, and counts splice junctions by motif. None of that is
    recoverable from a BAM afterwards.

    Appended to the job log rather than parsed into facts because the
    alignment report has no place to put per-aligner statistics yet. That is
    the obvious follow-on; until then the numbers are at least visible instead
    of deleted with the workdir.

    Best-effort: a missing or unreadable log is not a reason to fail a run
    whose BAM is already written.
    """
    summary = scratch / "Log.final.out"
    try:
        text = summary.read_text()
    except OSError as e:
        log.warning("star_summary_unavailable", path=str(summary), error=str(e))
        return

    try:
        with open(log_path, "a") as handle:
            handle.write("\n--- STAR Log.final.out ---\n")
            handle.write(text)
    except OSError as e:
        log.warning("star_summary_not_logged", error=str(e))


def _concatenate_reads(primary: Path, extras: list[Path], destination: Path) -> Path:
    """Combine several FASTQ files into the one file the aligner sees.

    None of the six aligners `align_runner` drives take several read files
    positionally, and only three of them (bowtie2, HISAT2, STAR) support a
    comma-separated list at all -- each with its own flag, and bwa-mem2,
    minimap2, and winnowmap have no multi-file convention whatsoever.
    Concatenating here, once, is the one approach that works uniformly across
    every aligner rather than special-casing half of them.

    Compression follows the primary: `_is_gzip` sniffs magic bytes rather than
    trusting a name, since a resolved blob has no extension until
    `_named_read_link` gives it one, and every read is decompressed on read
    and (if the primary was gzipped) recompressed on write so the aligner
    downstream sees one consistently-encoded file. FASTQ concatenates cleanly
    this way: each record is self-contained, so files placed end to end are a
    valid FASTQ of every read from every input, primary first.
    """
    primary_gzipped = _is_gzip(primary)
    sources = [primary, *extras]

    opener = gzip.open if primary_gzipped else open
    with opener(destination, "wb") as dst:
        for src in sources:
            if _is_gzip(src):
                with gzip.open(src, "rb") as fh:
                    shutil.copyfileobj(fh, dst)
            else:
                with open(src, "rb") as fh:
                    shutil.copyfileobj(fh, dst)
    return destination


def _extra_reads_paths(entries: list[dict], *, paired: bool) -> tuple[list[Path], list[Path]]:
    """Resolve an additional read sets payload to the R1 and R2 path lists.

    Every set contributes its own file to the R1 stream. In a paired run
    every set must also carry a mate, which contributes to the R2 stream -- a
    set without one is a launch that slipped past validation, and refusing it
    beats concatenating a shorter R2 stream that would misalign every read in
    the set.
    """
    extra_r1_paths = [
        _resolve_digest_or_path(
            entry.get("sha256"),
            entry.get("path"),
            missing_message="extra_reads entry requires 'sha256' or 'path'",
        )
        for entry in entries
    ]
    if not paired:
        return extra_r1_paths, []

    extra_r2_paths = []
    for entry in entries:
        mate_sha256 = entry.get("mate_sha256")
        mate_path = entry.get("mate_path")
        if not (mate_sha256 or mate_path):
            raise PermanentError(
                "extra_reads entry "
                f"{entry.get('name') or '(unnamed)'} has no mate in a "
                "paired run"
            )
        extra_r2_paths.append(
            _resolve_digest_or_path(
                mate_sha256,
                mate_path,
                missing_message="extra_reads mate requires 'mate_sha256' or 'mate_path'",
            )
        )
    return extra_r1_paths, extra_r2_paths


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

    # Mean depth is already known from `samtools coverage`, two phases back,
    # so the histogram's axis can be sized before the depth pass rather than
    # needing a second one.
    provisional = bam_stats_runner.genome_summary(contigs=contigs, bins=[])
    bucket_width = bam_stats_runner.histogram_bucket_width(
        mean_depth=provisional["mean_depth"]
    )
    histogram = (
        bam_stats_runner.DepthHistogram(bucket_width=bucket_width)
        if bucket_width is not None
        else None
    )

    with open(depth_path, errors="replace") as fh:
        bins, boundaries = bam_stats_runner.bin_depth(
            contig_lengths=contig_lengths, depth_lines=fh, histogram=histogram
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
        # Absent rather than empty when there is no usable mean depth, so the
        # frontend can tell "not computed" from "measured as flat".
        **(
            {
                "bam_stats_depth_histogram": histogram.to_facts(),
                "bam_stats_depth_bucket_width": bucket_width,
            }
            if histogram is not None
            else {}
        ),
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
