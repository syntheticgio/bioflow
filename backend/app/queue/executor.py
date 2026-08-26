"""Job execution: dispatch by mode, progress throttling, cancellation, results."""

import asyncio
import codecs
import contextlib
import os
import signal
import subprocess
import threading
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from typing import IO, Protocol, runtime_checkable

import psutil
from beanie import PydanticObjectId

from app.config import settings
from app.db.client import get_db
from app.errors import JobCancelled, PermanentError, RetryableError
from app.logging import get_logger
from app.models import Job, JobState
from app.models.timing import RunOutcome
from app.queue import queue
from app.queue.registry import HandlerMode, HandlerSpec, JobContext
from app.queue.resource_sampler import ResourceSampler

log = get_logger(__name__)

# Which payload key names the binary a job ran, checked in this order. Not a
# {job_type: key} mapping: that shape skips a job type nobody added an entry
# for, silently -- the "hand-maintained registries keyed by an enum" trap from
# CLAUDE.md. Reading whichever key is present instead degrades to None for a
# job type that names no tool (ingest_headers, assemble_upload), which is the
# honest answer rather than a missing case.
_TOOL_KEYS = ("tool", "aligner", "assembler")

# Progress writes are throttled: a job reporting at 5 Hz would otherwise cause a
# Mongo write and an SSE fan-out per tick, swamping the UI with refetches.
#
# The throttle *drops* updates rather than deferring them, which is correct for
# a percentage -- the next tick supersedes whatever was skipped -- and wrong for
# a phase, which changes rarely and then stands for minutes. A phase change is
# therefore exempt; see `_schedule_progress`.
PROGRESS_INTERVAL_SECONDS = 0.5

# A run past this floor with a parser attached that never produced a single
# update has almost certainly stopped matching the tool's actual output --
# see ProgressParser and _run_streaming's silence check below. 120s is long
# enough that a real tool has certainly printed something by then, short
# enough to catch a broken parser on an ordinary test run rather than only on
# a six-hour production job.
PARSER_SILENCE_FLOOR_S = 120.0


def _tool_from_payload(payload: dict) -> str | None:
    """Whichever `_TOOL_KEYS` entry names the binary this job ran."""
    for key in _TOOL_KEYS:
        value = payload.get(key)
        if value:
            return value
    return None


def _resolve_input_blobs(payload: dict) -> None:
    """Fetch input blobs from the primary before a job runs on a compute node.

    Scans the payload for SHA-256 digest fields and ensures every referenced
    blob is present locally.  Raises an OSError if the primary is unreachable
    (retryable — the job will be retried).  Raises FileNotFoundError if a
    blob is genuinely missing on the primary (permanent).
    """
    from app.storage.blob_transfer import resolve_payload_digests

    fetched = resolve_payload_digests(payload)
    if fetched:
        log.info("blobs_fetched", count=len(fetched), digests=fetched)


@runtime_checkable
class ProgressParser(Protocol):
    """Pure line-to-progress translation for a tool's stderr/stdout.

    No ctx, no I/O: a parser is a dataclass that consumes lines and can be
    asked what it currently knows. That is what makes golden-fixture tests
    possible -- replay a captured log through `feed()` with nothing to mock.

    `name` identifies the parser in the `progress_parser_silent` log line, so
    it should name the tool ("fastp", "bwa-mem2"), not the class.
    """

    name: str

    def feed(self, line: str) -> bool:
        """Consume one line. True if the caller should publish an update."""
        ...

    def snapshot(self) -> dict:
        """Current progress as kwargs for JobContext.progress().

        Only includes keys this parser actually knows -- a parser
        constructed without a declared stage order (assembly's
        `AssemblyProgress` when built with no `stage_order`) simply omits
        phase_total, rather than passing None and overwriting a value
        ctx.progress() would otherwise leave unchanged.
        """
        ...

# Grace period between SIGTERM and SIGKILL for subprocess handlers.
SUBPROCESS_GRACE_SECONDS = 15

# Resource sampling interval. Fine enough that a minute-long job yields ~60
# readings, coarse enough that the poll costs nothing next to the work.
SAMPLE_INTERVAL_SECONDS = 1.0

# Below this, resource fields are left null rather than filled with a peak
# derived from a handful of samples. Short jobs are not what this data is for
# -- the question "will this fit on my machine" is only asked about work
# measured in minutes -- so they are excluded rather than recorded unreliably.
RESOURCE_FLOOR_MS = 60_000


class JobExecutor:
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self._last_progress: dict[str, float] = {}
        # Last phase written per job, so a change can bypass the throttle.
        # Cleaned up alongside _last_progress; a leak here would be a slow
        # one, but it is the same lifetime and belongs in the same place.
        self._last_phase: dict[str, str] = {}

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
                owner=job.owner,
                started_at=job.timing.started_at,
            )
            ctx._progress_cb = lambda upd: self._schedule_progress(
                job_id,
                epoch,
                upd,
                owner=job.owner,
                run_ids=ctx.run_ids,
                started_at=ctx.started_at,
                eta_model_ms=ctx.eta_model_ms,
            )
            ctx._extend_cb = lambda seconds: self._schedule_lease_extension(
                job_id, epoch, seconds
            )

        log.info("job_started", job_id=job_id, type=job.type, mode=spec.mode.value)

        sampler, sampler_task = self._start_sampler(
            job_id,
            epoch,
            owner=job.owner,
            run_ids=ctx.run_ids,
            started_at=ctx.started_at,
            eta_model_ms=ctx.eta_model_ms,
        )
        outcome = RunOutcome.SUCCEEDED
        try:
            # Compute nodes: fetch input blobs from the primary before running
            # the handler.  No-op on the primary.
            #
            # to_thread: _resolve_input_blobs does a synchronous urllib download
            # of arbitrarily large blobs. Running it directly would freeze the
            # event loop for the whole transfer -- heartbeats, the cancel
            # watcher, and the lease renewals that feed the reaper all stop,
            # so every other running job's lease expires and those jobs get
            # reaped and re-run while still executing. Off the loop, the fetch
            # proceeds while those keep ticking.
            if settings.is_compute_node:
                await asyncio.to_thread(_resolve_input_blobs, job.payload)

            result = await self._dispatch(spec, ctx)
            # Thread-mode handlers cannot touch the database (Beanie is async),
            # so results that need persisting are applied here on the loop.
            await self._apply_result(job, result)
            await queue.complete(
                job_id, epoch, state=JobState.SUCCEEDED, result=result or {}
            )
            log.info("job_succeeded", job_id=job_id, type=job.type)

        except JobCancelled:
            outcome = RunOutcome.CANCELLED
            await queue.complete(job_id, epoch, state=JobState.CANCELLED)
            log.info("job_cancelled", job_id=job_id, type=job.type)

        except asyncio.CancelledError:
            # BaseException, not Exception -- raised when the worker's own
            # asyncio Task running this job is cancelled externally (e.g.
            # worker shutdown). Not caught by any of the except clauses
            # below, so without this branch it propagates straight through
            # to `finally` with `outcome` still SUCCEEDED, recording a job
            # that was killed mid-run as a fast success. Must re-raise: the
            # worker's own shutdown/cancellation machinery expects
            # CancelledError to keep propagating.
            outcome = RunOutcome.CANCELLED
            raise

        except PermanentError as e:
            outcome = RunOutcome.FAILED
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
                outcome = RunOutcome.DEAD
                await queue.complete(job_id, epoch, state=JobState.DEAD, error=error)
                log.error("job_dead", job_id=job_id, attempts=attempts, error=str(e))
            else:
                outcome = RunOutcome.FAILED
                await get_db().jobs.update_one(
                    {"_id": PydanticObjectId(job_id)}, {"$set": {"attempts": attempts}}
                )
                await queue.retry_later(job_id, epoch, attempts, error)
                log.warning("job_failed_retrying", job_id=job_id, attempts=attempts)
        finally:
            sampler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler_task
            await self._record_timing(job, outcome=outcome, sampler=sampler)
            self._last_progress.pop(job_id, None)
            self._last_phase.pop(job_id, None)

    async def _sample_resources(
        self,
        sampler: ResourceSampler,
        proc: psutil.Process,
        *,
        job_id: str,
        epoch: int,
        owner: str,
        run_ids: list[str],
        started_at: datetime | None,
        eta_model_ms: int | None,
    ) -> None:
        """Poll until cancelled. Never raises -- telemetry cannot fail a job.

        Each observation also drives a progress tick carrying the current and
        peak readings. This is deliberately the sampler's own tick, not a
        merge into whatever a handler happens to report: a phase-only job
        (Flye, Clair3) calls `ctx.progress()` a handful of times across a
        run that lasts minutes, so merging into handler-driven ticks would
        leave resource observations blank for exactly the jobs a user most
        wants to watch. `_schedule_progress`'s existing 0.5s throttle still
        applies, so a 1Hz sampler produces at most 1Hz of writes regardless.
        """
        try:
            while True:
                sampler.observe(proc)
                self._schedule_progress(
                    job_id,
                    epoch,
                    {
                        "rss_bytes": sampler.last_rss_bytes,
                        "cpu_percent": sampler.last_cpu_percent,
                        "peak_rss_bytes": sampler.peak_rss_bytes,
                        "peak_cpu_percent": sampler.peak_cpu_percent,
                    },
                    owner=owner,
                    run_ids=run_ids,
                    started_at=started_at,
                    eta_model_ms=eta_model_ms,
                )
                await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("resource_sampling_failed", error=str(e))

    def _start_sampler(
        self,
        job_id: str,
        epoch: int,
        *,
        owner: str,
        run_ids: list[str],
        started_at: datetime | None = None,
        eta_model_ms: int | None = None,
    ) -> tuple[ResourceSampler, asyncio.Task]:
        """Sample this worker's own process subtree.

        The worker's baseline is included, which slightly overstates a job's
        own footprint -- but subprocess tools are spawned as children of this
        process, so the subtree is what captures them, and two concurrent jobs
        remain separable because each tool tree is walked from its own root.

        `psutil.Process(pid)` is constructed exactly once, here, and reused
        for every poll. `cpu_percent(interval=None)` reports the percentage
        *since the previous call on that same Process instance* -- it returns
        0.0 unconditionally on an instance's first call. `ResourceSampler.observe()`
        accepts a `proc` argument for exactly this reason: a caller that
        constructed a fresh `psutil.Process(self.pid)` on every poll (as
        `observe()`'s own default does when called with no argument) would
        report 0.0 CPU forever, silently. This was caught in Task 1's code
        review before it could reach a running job.
        """
        proc = psutil.Process(os.getpid())
        sampler = ResourceSampler(pid=os.getpid())
        task = asyncio.create_task(
            self._sample_resources(
                sampler,
                proc,
                job_id=job_id,
                epoch=epoch,
                owner=owner,
                run_ids=run_ids,
                started_at=started_at,
                eta_model_ms=eta_model_ms,
            )
        )
        return sampler, task

    async def _record_timing(
        self, job: Job, *, outcome: str, sampler: ResourceSampler
    ) -> None:
        """Feed this run into the models and into provenance.

        Failed runs are recorded now, tagged by outcome: a failure is the most
        informative record a user can read, and an OOM kill is the best memory
        signal available. `timing_service._samples` keeps them out of the fits.
        """
        try:
            started = job.timing.started_at
            if started is None:
                return
            from datetime import UTC, datetime

            from app.pipelines import tools
            from app.services import machine_profile, timing_service
            from app.services.params_sanitizer import sanitize

            now = datetime.now(UTC)
            duration_ms = int((now - started).total_seconds() * 1000)
            queued_ms = None
            if job.timing.enqueued_at is not None:
                queued_ms = int((started - job.timing.enqueued_at).total_seconds() * 1000)

            # Payload size is what the models predict against; a job without
            # one (a schedule tick) has nothing to correlate.
            size = job.payload.get("size") or 0
            if not size and job.object_id:
                from app.models import DataObject

                obj = await DataObject.get(job.object_id)
                size = obj.size if obj else 0
            if not size:
                return

            # Nested first: every real launcher writes threads into the
            # sanitized params sub-dict (`payload["params"]["threads"]`), not
            # at the top level -- the flat key is a fallback for a launcher
            # that might one day write it there directly, not a shape
            # anything produces today.
            params_payload = job.payload.get("params") or {}
            threads = params_payload.get("threads") or job.payload.get("threads")

            tool = _tool_from_payload(job.payload)
            tool_version = tools.cached_version(tool) if tool else None

            # Under the floor the peak comes from too few samples to mean
            # anything, so the resource block stays empty rather than carrying
            # a number nothing should fit against.
            resources = {}
            if duration_ms >= RESOURCE_FLOOR_MS:
                resources = {
                    "peak_rss_bytes": sampler.peak_rss_bytes,
                    "peak_cpu_percent": sampler.peak_cpu_percent,
                    "mean_cpu_percent": sampler.mean_cpu_percent,
                    "sample_count": sampler.sample_count,
                }

            await timing_service.record(
                job_type=job.type,
                input_bytes=size,
                duration_ms=duration_ms,
                outcome=outcome,
                queued_ms=queued_ms,
                threads=threads,
                tool=tool,
                tool_version=tool_version,
                resources=resources,
                machine=machine_profile.capture(),
                params=sanitize(job.payload),
                job_id=str(job.id),
                object_id=str(job.object_id) if job.object_id else None,
                project_id=str(job.project_id) if job.project_id else None,
                worker_id=self.worker_id,
            )
        except Exception as e:  # noqa: BLE001 - telemetry never fails a job
            log.debug("timing_capture_failed", job_id=str(job.id), error=str(e))

    async def _apply_result(self, job: Job, result: dict | None) -> None:
        """Persist side effects a sync handler could not perform itself.

        `PermanentError` propagates; everything else is swallowed. The two
        halves answer different questions, which is why they are not treated
        alike:

        An unexpected exception means the write-back broke -- a Mongo blip, a
        bad document -- while the work itself succeeded and its output is on
        disk. Failing the job there would send hours of alignment back through
        the queue to fix a transient error, so it stays a loud log line.

        `PermanentError` means the applier *decided* this job produced nothing
        usable, and no rerun of the write-back changes that. Swallowing it
        leaves the job reporting succeeded with no output and only a worker log
        line to explain -- the silent partial success of #595, where a chunked
        alignment refuses to merge an incomplete set of buckets and the user
        sees a finished alignment job and no alignment. Raising reaches the
        `except PermanentError` branch in `run`, which fails the job with the
        applier's own message and without burning retries.
        """
        if not result:
            return
        try:
            from app.queue import results

            # The job document has carried its owner since `enqueue` started
            # setting it; the appliers that create objects from nothing but a
            # project_id have no other source for one.
            await results.apply(job.type, result, owner=job.owner)
        except PermanentError:
            raise
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

    def _schedule_lease_extension(self, job_id: str, epoch: int, seconds: int) -> None:
        """Renew one job's lease now, from any thread.

        Mirrors `_schedule_progress`'s thread handling: handlers run via
        `asyncio.to_thread`, so this is usually called off the loop and has to
        be handed back to it. Unthrottled, unlike progress -- a handler calls
        this once per long phase, not several times a second.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = getattr(self, "_loop", None)
            if loop is None:
                return
            asyncio.run_coroutine_threadsafe(
                self._extend_lease(job_id, epoch, seconds), loop
            )
            return

        loop.create_task(self._extend_lease(job_id, epoch, seconds))

    async def _extend_lease(self, job_id: str, epoch: int, seconds: int) -> None:
        try:
            await queue.heartbeat([job_id], {job_id: epoch}, {job_id: seconds})
            log.info("lease_extended", job_id=job_id, seconds=seconds)
        except Exception as e:  # noqa: BLE001 - the periodic heartbeat still covers us
            log.warning("lease_extension_failed", job_id=job_id, error=str(e))

    def _schedule_progress(
        self,
        job_id: str,
        epoch: int,
        update: dict,
        *,
        owner: str,
        run_ids: list[str] | None = None,
        started_at: datetime | None = None,
        eta_model_ms: int | None = None,
    ) -> None:
        """Throttle and persist a progress update from any thread.

        A *phase change* bypasses the throttle. Found by running a real
        assembly: the handler reported "starting", and Flye's `configure` and
        `assembly` banners both arrived inside the same second, so both were
        dropped -- then the tool ran for six minutes with no further stage
        line, and the job sat at "starting" for its entire life.

        Dropping is the right behaviour for a percentage, because the next tick
        carries the value the skipped one would have shown. A phase carries no
        such successor: it changes a handful of times and stands between
        changes, so a dropped one is not delayed, it is lost. Assembly makes
        this obvious because phases are all it has -- its stages differ in
        duration too much for an honest percentage -- but alignment's
        "sorting" transition had the same exposure.
        """
        now = datetime.now(UTC).timestamp()
        last = self._last_progress.get(job_id, 0.0)
        phase = update.get("phase")
        phase_changed = phase is not None and phase != self._last_phase.get(job_id)
        if not phase_changed and now - last < PROGRESS_INTERVAL_SECONDS:
            return
        self._last_progress[job_id] = now
        if phase is not None:
            self._last_phase[job_id] = phase

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Called from a worker thread: hand it back to the loop.
            loop = getattr(self, "_loop", None)
            if loop is None:
                return
            asyncio.run_coroutine_threadsafe(
                self._write_progress(
                    job_id,
                    epoch,
                    update,
                    owner=owner,
                    run_ids=run_ids,
                    started_at=started_at,
                    eta_model_ms=eta_model_ms,
                ),
                loop,
            )
            return

        loop.create_task(
            self._write_progress(
                job_id,
                epoch,
                update,
                owner=owner,
                run_ids=run_ids,
                started_at=started_at,
                eta_model_ms=eta_model_ms,
            )
        )

    async def _write_progress(
        self,
        job_id: str,
        epoch: int,
        update: dict,
        *,
        owner: str,
        run_ids: list[str] | None = None,
        started_at: datetime | None = None,
        eta_model_ms: int | None = None,
    ) -> None:
        try:
            await get_db().jobs.update_one(
                {"_id": PydanticObjectId(job_id), "lease.epoch": epoch},
                {"$set": {f"progress.{k}": v for k, v in update.items()}},
            )
            # The owner is passed down from the caller rather than read back off
            # the job document: this runs up to twice a second per running job,
            # and a lookup here would add a Mongo read to every progress tick.
            # run_ids and eta_model_ms likewise -- resolved once at claim time
            # and cached on the context, never re-queried here, for the same
            # reason.
            event = {"job_id": job_id, **update}
            if run_ids:
                event["run_ids"] = run_ids
            if started_at is not None:
                from app.services import timing_service

                pct = update.get("pct")
                elapsed_s = (datetime.now(UTC) - started_at).total_seconds()
                eta = timing_service.eta_seconds(
                    pct=pct, elapsed_s=elapsed_s, model_ms=eta_model_ms
                )
                if eta is not None:
                    event["eta_seconds"] = eta
                estimated = timing_service.pct_estimated(
                    pct=pct, elapsed_s=elapsed_s, model_ms=eta_model_ms
                )
                if estimated is not None:
                    event["pct_estimated"] = estimated
            await queue.publish_event("job.progress", event, owner=owner)
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
    parser: ProgressParser | None = None,
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

    `parser` is sugar over `on_line` for the common case: feed each line to a
    `ProgressParser` and forward its `snapshot()` to `ctx.progress()` whenever
    `feed()` says something changed. It also gets update counting for the
    silence check that a hand-written `on_line` closure would not. Passing
    both is a caller error -- there is exactly one thing to observe lines for.
    """
    if parser is not None:
        if on_line is not None:
            raise ValueError("run_subprocess: pass either on_line or parser, not both")
        return _run_streaming(
            ctx, cmd, cwd=cwd, env=env, log_path=log_path, on_line=None, parser=parser
        )
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


class _StreamPump:
    """Read subprocess stdout in a thread, splitting on `\r` or `\n`.

    Multiplexes five concerns over a running subprocess:
    - draining stdout
    - draining stderr (merged into stdout)
    - parsing progress out of the stream
    - calling an on_line callback
    - writing to the log file

    Each concern is a named method, and each collaborator is an injectable
    parameter a test can substitute.
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        log_file: IO[str] | None,
        parser: ProgressParser | None,
        on_line: Callable[[str], None] | None,
        ctx: JobContext,
    ) -> None:
        self.proc = proc
        self.log_file = log_file
        self.parser = parser
        self.on_line = on_line
        self.ctx = ctx
        self.line_count = 0
        self.update_count = 0

    def _observe(self, line: str) -> None:
        """Feed the parser or call the on_line callback."""
        if self.parser is not None:
            if self.parser.feed(line):
                self.update_count += 1
                self.ctx.progress(**self.parser.snapshot())
        elif self.on_line is not None:
            self.on_line(line)

    def _handle_line(self, line: str, delimiter: str = "\n") -> None:
        """Write to the log file and dispatch to the observer."""
        if not line and delimiter == "\r":
            # An empty split on a `\r` is an artifact of the delimiter, not a
            # line the tool printed: a progress bar redrawing itself emits
            # `\r` *before* the text it is about to draw. That happens at the
            # very start of a `\r`-first stream (fasterq-dump) and again after
            # every completed line the bar follows -- `start\r\ndone\rp: 10%`
            # would otherwise report a blank line between `start` and the bar.
            # An empty line delimited by `\n` is a bare `print()`: real output,
            # and still delivered.
            return
        self.line_count += 1
        if self.log_file is not None:
            with contextlib.suppress(Exception):
                self.log_file.write(line + "\n")
                self.log_file.flush()
        # An observer that raises must not kill the job: the work itself is
        # still valid, and progress is advisory everywhere else too.
        try:
            self._observe(line)
        except Exception as e:  # noqa: BLE001
            log.debug("on_line_failed", job_id=self.ctx.job_id, error=str(e))

    def run(self) -> None:
        """Read raw bytes and split on `\r` or `\n` ourselves.

        A `\r`-redrawn progress bar has no `\n` until the tool is done, so
        text-mode line iteration (`for line in proc.stdout`) would never yield
        it until the process exits.
        """
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = ""
        try:
            while True:
                chunk = (
                    self.proc.stdout.read1(4096)
                    if hasattr(self.proc.stdout, "read1")
                    else self.proc.stdout.read(4096)
                )
                if not chunk:
                    break
                buf += decoder.decode(chunk)
                # `\r\n` is one delimiter, not two blank-line-producing ones --
                # matches Python's own universal-newlines handling.
                buf = buf.replace("\r\n", "\n")
                while True:
                    nl, cr = buf.find("\n"), buf.find("\r")
                    if nl == -1 and cr == -1:
                        break
                    split = nl if cr == -1 else (cr if nl == -1 else min(nl, cr))
                    if split == len(buf) - 1 and buf[split] == "\r":
                        # A `\r` at the very end of the buffer may be the first
                        # half of a `\r\n` whose `\n` is in the next read. The
                        # replace() above only sees within one buffer, so
                        # splitting here would turn one delimiter into two and
                        # emit a phantom blank line. Wait for the next chunk.
                        break
                    delimiter = buf[split]
                    line, buf = buf[:split], buf[split + 1 :]
                    self._handle_line(line, delimiter)
            buf += decoder.decode(b"", final=True)
            if buf:
                self._handle_line(buf)
        except Exception as e:  # noqa: BLE001 - the pipe dies when the child is killed
            log.debug("output_pump_ended", job_id=self.ctx.job_id, error=str(e))


def _run_streaming(
    ctx: JobContext,
    cmd: list[str],
    *,
    cwd: str | None,
    env: dict | None,
    log_path: str | None,
    on_line: Callable[[str], None] | None,
    parser: ProgressParser | None = None,
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
        bufsize=0,
        # Binary, unbuffered: a `\r`-redrawn progress bar (fasterq-dump,
        # prefetch) never emits a `\n`, so Python's text-mode line iteration
        # (which only splits on `\n`) would sit on it until the process exits.
        # Reading raw bytes and splitting on either `\r` or `\n` ourselves is
        # what lets that kind of bar reach `on_line`/`parser` mid-transfer.
    )

    log_file = open(log_path, "a", encoding="utf-8", errors="replace") if log_path else None

    started = datetime.now(UTC)

    pump = _StreamPump(proc, log_file, parser, on_line, ctx)
    reader = threading.Thread(
        target=pump.run, name=f"subproc-out-{ctx.job_id}", daemon=True
    )
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
        elapsed = (datetime.now(UTC) - started).total_seconds()
        if parser is not None and pump.update_count == 0 and elapsed >= PARSER_SILENCE_FLOOR_S:
            log.warning(
                "progress_parser_silent",
                job_id=ctx.job_id,
                parser=parser.name,
                elapsed_s=round(elapsed, 1),
                line_count=pump.line_count,
            )


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
