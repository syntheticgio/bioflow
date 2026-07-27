"""Pipeline job handlers.

Separate from `handlers.py` because these shell out to external tools and carry
a different failure model: an exit code rather than an exception, output that
must be captured to disk, and a child process that has to die with the job.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import shutil
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

    r1_name = fastp_runner.output_name(ctx.payload.get("r1_name") or r1_in.name)
    r1_out = work / r1_name
    r2_out = None
    r2_name = None
    if paired:
        r2_name = fastp_runner.output_name(ctx.payload.get("r2_name") or r2_in.name)
        r2_out = work / r2_name

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
        "outputs": outputs,
        "report": report,
        "params": params.as_dict(),
        "tool": "fastp",
        "tool_version": fastp.version,
        "html_path": str(html_out) if html_out.exists() else None,
        "workdir": str(work),
    }


def _resolve_input(payload: dict, side: str) -> Path:
    """Locate an input read file from its digest or explicit path."""
    digest = payload.get(f"{side}_sha256")
    path_str = payload.get(f"{side}_path")

    if path_str:
        path = Path(path_str)
    elif digest:
        path = blob_path(digest)
    else:
        raise PermanentError(f"trim_reads requires '{side}_sha256' or '{side}_path'")

    if not path.exists():
        # Permanent rather than retryable: a blob that is missing now will
        # still be missing in thirty seconds, and the file-verification job is
        # what notices and reports storage problems.
        raise PermanentError(f"Input reads not found: {path}")
    return path


def _prepare_workdir(ctx: JobContext) -> Path:
    """A clean scratch directory for this job, under tmp/.

    tmp/ shares a filesystem with objects/ (asserted at startup in
    storage/home.py), so placing a finished output into the store is an atomic
    rename rather than a copy of a file that may be tens of gigabytes.

    Removed and recreated on entry, so a retry after a crashed attempt starts
    from nothing rather than inheriting a half-written file.
    """
    work = settings.tmp_dir / "trim" / ctx.job_id
    if work.exists():
        log.info("trim_workdir_reset", job_id=ctx.job_id, path=str(work))
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    return work


def _failure(code: int, log_path: Path) -> Exception:
    """Classify a non-zero fastp exit.

    The tail of the log goes into the message because the job record is where
    the user looks first, and "fastp exited 1" on its own tells them nothing.
    """
    tail = _log_tail(log_path)
    detail = f"fastp exited {code}"
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
