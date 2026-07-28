"""Pipeline job handlers.

Separate from `handlers.py` because these shell out to external tools and carry
a different failure model: an exit code rather than an exception, output that
must be captured to disk, and a child process that has to die with the job.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import fastp_runner, tools
from app.queue.executor import run_subprocess
from app.queue.registry import HandlerMode, JobContext, handler
from app.storage.paths import blob_path

log = get_logger(__name__)


@handler(
    "trim_reads",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=2048, io=IoClass.HEAVY),
    # Low on purpose. A fastp failure is almost always deterministic -- bad
    # input, a missing binary, a full disk -- and spending five attempts on a
    # multi-hour run delays the error without making it less likely.
    max_attempts=2,
)
def trim_reads(ctx: JobContext) -> dict:
    """Adapter-trim and quality-filter a FASTQ file or an R1/R2 pair.

    Runs off the event loop in a worker thread, so it cannot touch the
    database: it resolves its inputs from the payload and returns a plain dict
    for `results._apply_trim_reads` to persist. See queue/results.py.

    Idempotent by construction. Delivery is at-least-once, and a drain during
    shutdown requeues a running job, so a second attempt must converge rather
    than collide with the first. Each attempt gets its own scratch directory,
    which is removed on entry -- a partial run leaves nothing behind that a
    retry could mistake for its own output.
    """
    fastp = tools.require(tools.fastp())

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("trim_reads requires an 'object_id'")

    r1_in = _resolve_input(ctx.payload, "r1")
    r2_in = _resolve_input(ctx.payload, "r2") if ctx.payload.get("r2_sha256") else None
    paired = r2_in is not None

    params = fastp_runner.TrimParams.from_dict(ctx.payload.get("params"))
    work = _prepare_workdir(ctx)

    # fastp decides whether an input is gzipped from its *filename*, and offers
    # no flag to override that. Managed blobs are stored under their hash with
    # no extension, so handing fastp the blob path directly makes it read gzip
    # bytes as plain text and fail with a parse error. Symlinking the original
    # name into the scratch directory costs nothing and keeps the command
    # readable in the log besides.
    r1_in = _named_link(work, r1_in, ctx.payload.get("r1_name"))
    if paired:
        r2_in = _named_link(work, r2_in, ctx.payload.get("r2_name"))

    # Outputs go in an `out/` subdirectory so a trimmed file can never collide
    # with the input symlink it was derived from -- `output_name` preserves the
    # stem, and only the `.trimmed` marker separates them.
    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)

    r1_name = fastp_runner.output_name(ctx.payload.get("r1_name") or r1_in.name)
    r1_out = out_dir / r1_name
    r2_out = None
    r2_name = None
    if paired:
        r2_name = fastp_runner.output_name(ctx.payload.get("r2_name") or r2_in.name)
        r2_out = out_dir / r2_name

    json_out = work / "fastp.json"
    html_out = work / "fastp.html"

    cmd = fastp_runner.build_command(
        fastp_path=fastp.path,
        r1_in=r1_in,
        r1_out=r1_out,
        r2_in=r2_in,
        r2_out=r2_out,
        json_out=json_out,
        html_out=html_out,
        params=params,
    )

    progress = fastp_runner.TrimProgress(expected_reads=ctx.payload.get("expected_reads"))
    ctx.progress(phase="starting", pct=0.0, message="starting fastp")

    def on_line(line: str) -> None:
        if progress.feed(line):
            ctx.progress(pct=progress.pct, phase=progress.phase, message=progress.message())

    # logs/ is created at startup, but this is the first code to write into it
    # and a worker that somehow started without it must not lose a run over a
    # missing directory.
    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("trim_started", job_id=ctx.job_id, paired=paired, cmd=" ".join(cmd))

    code = run_subprocess(ctx, cmd, log_path=str(log_path), on_line=on_line)
    if code != 0:
        raise _failure(code, log_path)

    # fastp reports success by exit code, but a zero exit with no output means
    # something went wrong in a way it did not consider fatal. Catching it here
    # beats creating an empty object and discovering it downstream.
    for produced in filter(None, (r1_out, r2_out)):
        if not produced.exists() or produced.stat().st_size == 0:
            raise RetryableError(f"fastp exited 0 but produced no output at {produced.name}")

    ctx.progress(phase="reporting", pct=fastp_runner.MAX_MEASURED_PCT, message="reading report")
    report = fastp_runner.parse_report(json_out)

    outputs = [{"tmp_path": str(r1_out), "name": r1_name, "mate": "R1" if paired else None}]
    if paired:
        outputs.append({"tmp_path": str(r2_out), "name": r2_name, "mate": "R2"})

    ctx.progress(phase="done", pct=1.0, message="trimming complete")
    log.info(
        "trim_finished",
        job_id=ctx.job_id,
        outputs=len(outputs),
        reads_in=report.get("before", {}).get("total_reads"),
        reads_out=report.get("after", {}).get("total_reads"),
    )

    return {
        "object_id": object_id,
        "mate_object_id": ctx.payload.get("mate_object_id"),
        "project_id": ctx.payload.get("project_id"),
        # Rides along so the produced objects can record which run made them.
        "job_id": ctx.job_id,
        "outputs": outputs,
        "report": report,
        "params": params.as_dict(),
        "tool": "fastp",
        "tool_version": fastp.version,
        "html_path": str(html_out) if html_out.exists() else None,
        "workdir": str(work),
    }


@handler(
    "run_qc",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # HEAVY io, not light: QC streams the whole FASTQ exactly as a trim does,
    # and the governor's cap on concurrent heavy readers is what keeps a
    # handful of QC runs from being slower in aggregate than two. (The plan
    # said MEDIUM, which is not one of the three classes the model defines.)
    resources=JobResources(cpu=2, mem_mb=1024, io=IoClass.HEAVY),
    # Same reasoning as trim_reads: a QC failure is deterministic -- bad input
    # or a missing binary -- and retries only delay the error.
    max_attempts=2,
)
def run_qc(ctx: JobContext) -> dict:
    """Run fastp (report-only) and FastQC over one FASTQ.

    Read-only, unlike trim: it derives no files, only a description of one.
    The structured numbers come back as facts for `_apply_run_qc` to persist;
    the HTML lands under qc_reports/<object_id>/ and is referenced by path.

    Synchronous -- SUBPROCESS runs this off the event loop, so the body must
    not await. Database work happens in the applier.

    FastQC's failure is not the job's failure. fastp produces every number the
    UI charts, and FastQC is the optional extra that needs a JRE; letting a
    broken Java install fail a QC run would deny the user the facts that did
    parse.
    """
    fastp_tool = tools.require(tools.fastp())

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("run_qc requires an 'object_id'")

    reads_in = _resolve_input(ctx.payload, "r1")
    work = _prepare_workdir(ctx, kind="qc")

    # Same reason as trim: fastp infers gzip from the filename, and a managed
    # blob is stored under its hash with no extension.
    name = ctx.payload.get("name")
    reads_in = _named_link(work, reads_in, name)

    json_out = work / "fastp_qc.json"
    html_out = work / "fastp_qc.html"

    cmd = fastp_runner.build_qc_command(
        fastp_path=fastp_tool.path,
        r1_in=reads_in,
        json_out=json_out,
        html_out=html_out,
        threads=min(settings.pipeline_default_threads, 2),
    )

    progress = fastp_runner.TrimProgress(expected_reads=ctx.payload.get("expected_reads"))
    ctx.progress(phase="starting", pct=0.0, message="starting fastp")

    def on_line(line: str) -> None:
        if progress.feed(line):
            ctx.progress(pct=progress.pct, phase=progress.phase, message=progress.message())

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("qc_started", job_id=ctx.job_id, object_id=object_id, cmd=" ".join(cmd))

    code = run_subprocess(ctx, cmd, log_path=str(log_path), on_line=on_line)
    if code != 0:
        raise _failure(code, log_path)

    facts = fastp_runner.parse_qc_facts(json_out)

    # Reports are keyed by object rather than by job: a re-run replaces the
    # previous report rather than accumulating one directory per attempt, and
    # the path stored in facts stays valid without an update.
    report_dir = settings.qc_reports_dir / str(object_id)
    report_dir.mkdir(parents=True, exist_ok=True)

    if html_out.exists():
        shutil.copyfile(html_out, report_dir / "fastp.html")
        facts["qc_fastp_report"] = f"{object_id}/fastp.html"

    ctx.progress(phase="fastqc", pct=fastp_runner.MAX_MEASURED_PCT, message="running FastQC")
    fastqc_name = _run_fastqc(ctx, reads_in, report_dir, log_path)
    if fastqc_name:
        facts["qc_fastqc_report"] = f"{object_id}/{fastqc_name}"

    facts["qc_status"] = "ok"

    ctx.progress(phase="done", pct=1.0, message="QC complete")
    log.info(
        "qc_finished",
        job_id=ctx.job_id,
        object_id=object_id,
        reads=facts.get("qc_before_filtering", {}).get("total_reads"),
        fastqc=bool(fastqc_name),
    )

    return {
        "object_id": object_id,
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
    }


def _run_fastqc(
    ctx: JobContext, reads_in: Path, report_dir: Path, log_path: Path
) -> str | None:
    """Run FastQC into `report_dir`, returning the HTML's filename or None.

    Every failure here is swallowed to a warning. FastQC is the optional half
    of the QC pair -- it needs a JRE, and on a host missing one the fastp facts
    are still worth keeping.
    """
    fastqc_tool = tools.fastqc()
    if not fastqc_tool.available:
        log.info("qc_fastqc_unavailable", job_id=ctx.job_id, error=fastqc_tool.error)
        return None

    out_dir = report_dir / "fastqc"
    out_dir.mkdir(parents=True, exist_ok=True)

    # FastQC names its output after the input file, and the input here is the
    # `in_`-prefixed symlink that exists only to give fastp a filename it can
    # read the compression from. Linking the bare name beside it keeps that
    # implementation detail out of a report title the user reads.
    if reads_in.name.startswith("in_"):
        clean = reads_in.parent / reads_in.name[len("in_") :]
        if not clean.exists():
            try:
                clean.symlink_to(reads_in.resolve())
                reads_in = clean
            except OSError as e:
                # Cosmetic only -- a report named after the symlink is still a
                # correct report, so this must not fail the run.
                log.debug("qc_name_link_failed", error=str(e))

    code = run_subprocess(
        ctx,
        [fastqc_tool.path, "--outdir", str(out_dir), "--quiet", str(reads_in)],
        log_path=str(log_path),
    )
    if code != 0:
        log.warning("qc_fastqc_failed", job_id=ctx.job_id, code=code)
        return None

    html = next(iter(sorted(out_dir.glob("*_fastqc.html"))), None)
    if html is None:
        log.warning("qc_fastqc_no_report", job_id=ctx.job_id)
        return None
    return f"fastqc/{html.name}"


def _resolve_input(payload: dict, side: str) -> Path:
    """Locate an input read file from its digest or explicit path."""
    digest = payload.get(f"{side}_sha256")
    path_str = payload.get(f"{side}_path")

    if path_str:
        path = Path(path_str)
    elif digest:
        path = blob_path(digest)
    else:
        raise PermanentError(f"No input given: expected '{side}_sha256' or '{side}_path'")

    if not path.exists():
        # Permanent rather than retryable: a blob that is missing now will
        # still be missing in thirty seconds, and the file-verification job is
        # what notices and reports storage problems.
        raise PermanentError(f"Input reads not found: {path}")
    return path


def _named_link(work: Path, target: Path, name: str | None) -> Path:
    """A symlink to `target` under its user-facing name, inside the workdir.

    Falls back to the target itself when there is no name to use -- a
    register-in-place file already sits under its real name, so the link would
    add nothing.
    """
    if not name:
        return target

    safe = Path(name).name
    link = work / f"in_{safe}"
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(target)
    except OSError as e:
        # Not fatal on its own, but the run will almost certainly fail on a
        # compressed input, so say why here rather than leaving a parse error
        # as the only clue.
        log.warning("input_link_failed", target=str(target), name=safe, error=str(e))
        return target
    return link


def _prepare_workdir(ctx: JobContext, kind: str = "trim") -> Path:
    """A clean scratch directory for this job, under tmp/.

    tmp/ shares a filesystem with objects/ (asserted at startup in
    storage/home.py), so placing a finished output into the store is an atomic
    rename rather than a copy of a file that may be tens of gigabytes.

    Removed and recreated on entry, so a retry after a crashed attempt starts
    from nothing rather than inheriting a half-written file.
    """
    work = settings.tmp_dir / kind / ctx.job_id
    if work.exists():
        log.info("workdir_reset", job_id=ctx.job_id, kind=kind, path=str(work))
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    return work


def _failure(code: int, log_path: Path, tool: str = "fastp") -> Exception:
    """Classify a non-zero exit from an external tool.

    The tail of the log goes into the message because the job record is where
    the user looks first, and "fastp exited 1" on its own tells them nothing.
    """
    tail = _log_tail(log_path)
    detail = f"{tool} exited {code}"
    if tail:
        detail = f"{detail}: {tail}"

    # 137 is SIGKILL, which on this stack means the OOM killer -- a bigger
    # machine or fewer threads might succeed, so it is worth one retry.
    if code == 137:
        return RetryableError(f"{detail} (killed, most likely out of memory)")
    return PermanentError(detail)


def _log_tail(path: Path, *, lines: int = 5, max_chars: int = 600) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    tail = " / ".join(line.strip() for line in text.splitlines()[-lines:] if line.strip())
    return tail[:max_chars]


@handler(
    "reap_pipeline_scratch",
    mode=HandlerMode.ASYNC,
    job_class=JobClass.MAINTENANCE,
    resources=JobResources(cpu=1, mem_mb=64, io=IoClass.LIGHT),
)
async def reap_pipeline_scratch(ctx: JobContext) -> dict:
    """Remove trim working directories and expired job logs.

    Two failure modes make this necessary. A crashed or cancelled run leaves
    its outputs under tmp/trim/<job_id>, and those are whole FASTQ files --
    tens of gigabytes that nothing else would ever reclaim. Separately,
    `_apply_result` deliberately does not fail a job when the write-back
    throws, because the expensive work succeeded; that path also strands its
    scratch directory.

    Age is the only signal used. Checking whether the owning job is still
    running would be a race: a directory is created before the job is marked
    running, so a young one may belong to a job that has not started writing.
    The grace period sidesteps that entirely.
    """
    from datetime import timedelta

    from app.storage.home import check_home

    home = check_home()
    if not home.ok:
        return {"skipped": True, "reason": home.detail}

    scratch_grace_hours = float(ctx.payload.get("scratch_grace_hours", 6))
    log_retention_days = float(
        ctx.payload.get("log_retention_days", settings.pipeline_log_retention_days)
    )

    now = datetime.now(UTC)
    removed_dirs = 0
    # Every scratch root a pipeline handler writes into. An alignment workdir
    # holds a whole BAM plus samtools' sort spills, so one left behind by a
    # crashed run is tens of gigabytes that nothing else would reclaim.
    for kind in ("trim", "align", "qc"):
        removed_dirs += await _reap_dir(
            ctx,
            settings.tmp_dir / kind,
            cutoff=now - timedelta(hours=scratch_grace_hours),
            remove=lambda p: shutil.rmtree(p, ignore_errors=True),
            want_dirs=True,
        )
    removed_logs = await _reap_dir(
        ctx,
        settings.logs_dir,
        cutoff=now - timedelta(days=log_retention_days),
        remove=lambda p: p.unlink(missing_ok=True),
        want_dirs=False,
    )

    if removed_dirs or removed_logs:
        log.info("pipeline_scratch_reaped", dirs=removed_dirs, logs=removed_logs)
    return {"removed_scratch_dirs": removed_dirs, "removed_logs": removed_logs}


async def _reap_dir(ctx: JobContext, root: Path, *, cutoff, remove, want_dirs: bool) -> int:
    """Delete entries under `root` last modified before `cutoff`."""
    if not await asyncio.to_thread(root.exists):
        return 0

    removed = 0
    for entry in await asyncio.to_thread(lambda: sorted(root.iterdir())):
        ctx.check_cancel()
        try:
            if entry.is_dir() != want_dirs:
                continue
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, UTC)
        except OSError:
            continue  # vanished underneath us; nothing to do
        if mtime > cutoff:
            continue
        await asyncio.to_thread(remove, entry)
        removed += 1
    return removed
