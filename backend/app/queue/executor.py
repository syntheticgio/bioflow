"""Job execution: dispatch by mode, progress throttling, cancellation, results."""

import asyncio
import contextlib
import os
import signal
import subprocess
import threading
import traceback
from collections.abc import Callable
from datetime import UTC, datetime

from beanie import PydanticObjectId

from app.db.client import get_db
from app.errors import JobCancelled, PermanentError, RetryableError
from app.logging import get_logger
from app.models import Job, JobState
from app.queue import queue
from app.queue.registry import HandlerMode, HandlerSpec, JobContext

log = get_logger(__name__)

# Progress writes are throttled: a job reporting at 5 Hz would otherwise cause a
# Mongo write and an SSE fan-out per tick, swamping the UI with refetches.
PROGRESS_INTERVAL_SECONDS = 0.5

# Grace period between SIGTERM and SIGKILL for subprocess handlers.
SUBPROCESS_GRACE_SECONDS = 15


class JobExecutor:
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self._last_progress: dict[str, float] = {}

    async def run(
        self, job: Job, spec: HandlerSpec, epoch: int, *, ctx: JobContext | None = None
    ) -> None:
        """Execute one job to a terminal state. Never raises.

        The caller may supply the context so it retains the handle needed to
        signal cancellation; otherwise one is created here.
        """
        job_id = str(job.id)
        if ctx is None:
            ctx = JobContext(
                job_id=job_id,
                payload=job.payload,
                epoch=epoch,
                attempts=job.attempts,
            )
            ctx._progress_cb = lambda upd: self._schedule_progress(job_id, epoch, upd)

        log.info("job_started", job_id=job_id, type=job.type, mode=spec.mode.value)

        try:
            result = await self._dispatch(spec, ctx)
            # Thread-mode handlers cannot touch the database (Beanie is async),
            # so results that need persisting are applied here on the loop.
            await self._apply_result(job, result)
            await queue.complete(
                job_id, epoch, state=JobState.SUCCEEDED, result=result or {}
            )
            await self._record_timing(job)
            log.info("job_succeeded", job_id=job_id, type=job.type)

        except JobCancelled:
            await queue.complete(job_id, epoch, state=JobState.CANCELLED)
            log.info("job_cancelled", job_id=job_id, type=job.type)

        except PermanentError as e:
            # Cannot succeed however many times we try, so do not burn retries.
            await queue.complete(
                job_id,
                epoch,
                state=JobState.FAILED,
                error={
                    "code": e.code,
                    "message": e.message,
                    "retryable": False,
                    "traceback_tail": "",
                },
            )
            log.warning("job_failed_permanent", job_id=job_id, error=e.message)

        except (RetryableError, Exception) as e:  # noqa: B014
            attempts = job.attempts + 1
            error = {
                # Coerced to str: some libraries (pymongo) put an int in .code,
                # which would fail JobError validation and make the job
                # unreadable through the API.
                "code": str(getattr(e, "code", None) or type(e).__name__),
                "message": str(e),
                "retryable": True,
                "traceback_tail": "".join(traceback.format_exc().splitlines(True)[-12:]),
            }
            if attempts >= job.max_attempts:
                await queue.complete(job_id, epoch, state=JobState.DEAD, error=error)
                log.error("job_dead", job_id=job_id, attempts=attempts, error=str(e))
            else:
                await get_db().jobs.update_one(
                    {"_id": PydanticObjectId(job_id)}, {"$set": {"attempts": attempts}}
                )
                await queue.retry_later(job_id, epoch, attempts, error)
                log.warning("job_failed_retrying", job_id=job_id, attempts=attempts)
        finally:
            self._last_progress.pop(job_id, None)

    async def _record_timing(self, job: Job) -> None:
        """Feed this run into the duration model.

        Only successful runs: a job that failed after 200ms would otherwise
        drag every future estimate down.
        """
        try:
            started = job.timing.started_at
            if started is None:
                return
            from datetime import UTC, datetime

            from app.services import timing_service

            duration_ms = int(
                (datetime.now(UTC) - started).total_seconds() * 1000
            )
            # Payload size is what the model predicts against; a job without
            # one (a schedule tick) has nothing to correlate.
            size = job.payload.get("size") or 0
            if not size and job.object_id:
                from app.models import DataObject

                obj = await DataObject.get(job.object_id)
                size = obj.size if obj else 0
            if not size:
                return

            await timing_service.record(
                job_type=job.type,
                input_bytes=size,
                duration_ms=duration_ms,
                worker_id=self.worker_id,
            )
        except Exception as e:  # noqa: BLE001 - telemetry never fails a job
            log.debug("timing_capture_failed", job_id=str(job.id), error=str(e))

    async def _apply_result(self, job: Job, result: dict | None) -> None:
        """Persist side effects a sync handler could not perform itself."""
        if not result:
            return
        try:
            from app.queue import results

            await results.apply(job.type, result)
        except Exception as e:  # noqa: BLE001
            # The work succeeded; only the write-back failed. Log loudly rather
            # than failing the job and re-running expensive work.
            log.error(
                "result_apply_failed", job_id=str(job.id), type=job.type, error=str(e)
            )

    async def _dispatch(self, spec: HandlerSpec, ctx: JobContext):
        """Route to the handler, keeping blocking modes off the event loop."""
        if spec.mode is HandlerMode.ASYNC:
            return await spec.fn(ctx)

        if spec.mode in (HandlerMode.THREAD, HandlerMode.SUBPROCESS):
            # to_thread is correct rather than a process pool: these handlers
            # are IO-bound or spawn their own processes, and both hashlib and
            # file reads release the GIL.
            return await asyncio.to_thread(spec.fn, ctx)

        raise PermanentError(f"Unknown handler mode: {spec.mode}")

    def _schedule_progress(self, job_id: str, epoch: int, update: dict) -> None:
        """Throttle and persist a progress update from any thread."""
        now = datetime.now(UTC).timestamp()
        last = self._last_progress.get(job_id, 0.0)
        if now - last < PROGRESS_INTERVAL_SECONDS:
            return
        self._last_progress[job_id] = now

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Called from a worker thread: hand it back to the loop.
            loop = getattr(self, "_loop", None)
            if loop is None:
                return
            asyncio.run_coroutine_threadsafe(
                self._write_progress(job_id, epoch, update), loop
            )
            return

        loop.create_task(self._write_progress(job_id, epoch, update))

    async def _write_progress(self, job_id: str, epoch: int, update: dict) -> None:
        try:
            await get_db().jobs.update_one(
                {"_id": PydanticObjectId(job_id), "lease.epoch": epoch},
                {"$set": {f"progress.{k}": v for k, v in update.items()}},
            )
            await queue.publish_event(
                "job.progress", {"job_id": job_id, **update}
            )
        except Exception as e:  # noqa: BLE001 - progress is advisory
            log.debug("progress_write_failed", job_id=job_id, error=str(e))

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop


def run_subprocess(
    ctx: JobContext,
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    log_path: str | None = None,
    on_line: Callable[[str], None] | None = None,
) -> int:
    """Run a subprocess that dies with the job.

    start_new_session puts the child in its own process group, so cancellation
    can signal the whole group. That matters for pipelines: killing only the
    direct child of `bwa | samtools sort` orphans the rest.

    Without `on_line` the child's output is redirected straight to the log file
    descriptor: the kernel does the copying and this process never sees the
    bytes. Pass `on_line` to additionally observe output as it streams -- the
    only way to turn a tool's own progress reporting into `ctx.progress()`.
    Lines are still written to `log_path`, so enabling it costs a pipe and a
    reader thread but loses nothing.
    """
    if on_line is not None:
        return _run_streaming(ctx, cmd, cwd=cwd, env=env, log_path=log_path, on_line=on_line)

    log_file = open(log_path, "ab") if log_path else subprocess.DEVNULL

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        return _wait_cancellable(ctx, proc)
    finally:
        if log_path and log_file is not subprocess.DEVNULL:
            with contextlib.suppress(Exception):
                log_file.close()


def _run_streaming(
    ctx: JobContext,
    cmd: list[str],
    *,
    cwd: str | None,
    env: dict | None,
    log_path: str | None,
    on_line: Callable[[str], None],
) -> int:
    """run_subprocess with the output piped through a reader thread.

    The reader runs on its own thread rather than in the wait loop because a
    tool that goes quiet for minutes (fastp reads a long stretch before it says
    anything) would otherwise block cancellation polling behind a read that
    never returns.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        bufsize=1,
        text=True,
        errors="replace",  # tool output is not guaranteed to be valid UTF-8
    )

    log_file = open(log_path, "a", encoding="utf-8", errors="replace") if log_path else None

    def pump() -> None:
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if log_file is not None:
                    with contextlib.suppress(Exception):
                        log_file.write(line + "\n")
                        log_file.flush()
                # A parser that raises must not kill the job: the work itself
                # is still valid, and progress is advisory everywhere else too.
                try:
                    on_line(line)
                except Exception as e:  # noqa: BLE001
                    log.debug("on_line_failed", job_id=ctx.job_id, error=str(e))
        except Exception as e:  # noqa: BLE001 - the pipe dies when the child is killed
            log.debug("output_pump_ended", job_id=ctx.job_id, error=str(e))

    reader = threading.Thread(target=pump, name=f"subproc-out-{ctx.job_id}", daemon=True)
    reader.start()

    try:
        return _wait_cancellable(ctx, proc)
    finally:
        # The child is gone by now, so the pipe is at EOF and the reader is
        # finishing. Bounded join: a wedged reader must not hold the worker.
        reader.join(timeout=5)
        with contextlib.suppress(Exception):
            if proc.stdout is not None:
                proc.stdout.close()
        if log_file is not None:
            with contextlib.suppress(Exception):
                log_file.close()


def _wait_cancellable(ctx: JobContext, proc: subprocess.Popen) -> int:
    """Wait for the child, checking for cancellation once a second."""
    while True:
        try:
            return proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired as e:
            if ctx.is_cancelled():
                _terminate_group(proc)
                raise JobCancelled(f"Job {ctx.job_id} cancelled") from e


def _terminate_group(proc: subprocess.Popen) -> None:
    """SIGTERM the process group, then SIGKILL anything that survives."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return

    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)

    try:
        proc.wait(timeout=SUBPROCESS_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
