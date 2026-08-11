"""The worker: claim loop, heartbeat, cancellation watch, leader duties, drain."""

import asyncio
import contextlib
import json
import os
import socket
from datetime import UTC, datetime, timedelta

import httpx
import psutil
from beanie import PydanticObjectId

from app.config import settings
from app.db.redis_client import get_redis
from app.logging import get_logger
from app.models import Job, JobClass, JobState
from app.pipelines import tool_cache
from app.queue import governor, keys, queue
from app.queue.executor import JobExecutor
from app.queue.registry import JobContext, get_handler, load_handlers
from app.services import resource_limit_service, run_service

log = get_logger(__name__)

CLAIM_BACKOFF_MIN = 0.05
CLAIM_BACKOFF_MAX = 2.0
LEADER_LOCK_TTL_MS = 15000
# The loop is expected to be responsive; a stall beyond this means blocking work
# leaked onto it, which is what silently causes leases to expire.
LOOP_STALL_WARN_SECONDS = 0.25

# More than two concurrent heavy readers on a FUSE mount is slower in aggregate
# than two, so this is a throughput cap as much as a safety valve.
IO_HEAVY_LIMIT = 2


def _as_int(value) -> int:
    """A counter value as a non-negative int.

    Counters are clamped at zero because a missed release makes them drift
    *down* past zero, and a negative reservation would otherwise read as extra
    free capacity -- turning a leak into over-admission.
    """
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def compute_free_resources(
    *,
    cpu_budget: int,
    mem_mb: int,
    reserved_cpu: int,
    reserved_mem: int,
    reserved_io_heavy: int,
    in_flight: int,
) -> dict:
    """Headroom to admit against, from budgets and current reservations.

    Pure, because the failure this guards is not observable in a normal test
    run: reservation counters can only leak *upward* if a release is missed --
    a crashed worker, a lost lease -- and a leak permanently shrinks capacity
    until someone notices the queue has stopped moving.

    The defence is the `in_flight` clamp. Reservations are cluster-wide, but a
    worker also knows how many jobs it is actually running, and a single worker
    cannot be responsible for more reserved capacity than the jobs it holds.
    Taking the smaller of the two means a leaked counter costs at most the
    capacity of the jobs genuinely in flight, and an idle worker always
    recovers full headroom no matter what the counters claim.

    At least 1 CPU is always offered so a fully-reserved queue still drains
    rather than deadlocking against its own bookkeeping. Memory has no such
    floor: offering a phantom megabyte would admit a job that does not fit,
    which is the failure this exists to prevent, and `claim.lua` compares
    `mem <= mem_free` so zero simply admits nothing until something releases.
    """
    if in_flight == 0:
        # Nothing running here, so nothing this worker reserved can still be
        # outstanding. This is the line that makes a leak self-healing.
        effective_cpu_reserved = 0
        effective_mem_reserved = 0
        effective_io_reserved = 0
    else:
        effective_cpu_reserved = reserved_cpu
        effective_mem_reserved = reserved_mem
        effective_io_reserved = reserved_io_heavy

    return {
        "cpu": max(cpu_budget - effective_cpu_reserved, 1),
        "mem_mb": max(mem_mb - effective_mem_reserved, 0),
        "io_heavy": max(IO_HEAVY_LIMIT - effective_io_reserved, 0),
    }


class Worker:
    def __init__(self, worker_id: str | None = None):
        self.worker_id = worker_id or settings.worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.node_id: str = settings.worker_node_id
        self.max_concurrent = settings.worker_max_concurrent
        self.shutdown = asyncio.Event()
        self.executor = JobExecutor(self.worker_id)

        # job_id -> (task, context, epoch)
        self._running: dict[str, tuple[asyncio.Task, JobContext, int]] = {}
        self._tasks: list[asyncio.Task] = []

        # Only the leader samples and publishes; followers read the decision.
        # Independent decisions would disagree at the margins, with half the
        # workers admitting while the other half refused.
        self._local_governor: governor.LoadGovernor | None = None
        self._starvation_checked_at = 0.0
        self._starvation_cached = False

        # Node enrollment (compute nodes only).
        self._revoked: bool = False
        self._enrollment_task: asyncio.Task | None = None

    # ---------- lifecycle ----------

    async def start(self) -> None:
        load_handlers()
        self.executor.bind_loop(asyncio.get_running_loop())

        restored = await queue.reconcile()

        # The workflow equivalent, and needed for the same reason: a process
        # that died between a node finishing and its successor launching leaves
        # a run that nothing will ever revive -- there is no timer on a workflow
        # and no dependency to release. See the design's §10.
        from app.services import workflow_orchestrator

        try:
            recovered = await workflow_orchestrator.reconcile_workflows()
        except Exception as e:  # noqa: BLE001 - a stuck workflow must not stop the worker
            log.warning("workflow_reconcile_failed", error=str(e))
            recovered = 0

        log.info(
            "worker_starting",
            worker_id=self.worker_id,
            node_id=self.node_id,
            max_concurrent=self.max_concurrent,
            restored_jobs=restored,
            recovered_workflow_nodes=recovered,
        )

        # Enroll with the primary if this is a compute node.
        if settings.primary_api_url:
            await self._enroll()
            self._enrollment_task = asyncio.create_task(
                self._enrollment_watch_loop(), name="enrollment-watch"
            )

        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._cancel_watch_loop(), name="cancel-watch"),
            asyncio.create_task(self._leader_loop(), name="leader"),
            asyncio.create_task(self._load_sampler_loop(), name="load-sampler"),
            asyncio.create_task(self._loop_watchdog(), name="watchdog"),
            asyncio.create_task(self._tool_invalidation_loop(), name="tool-invalidation"),
        ]

        try:
            await self._claim_loop()
        finally:
            await self._drain()

    def request_shutdown(self) -> None:
        log.info("shutdown_requested", worker_id=self.worker_id)
        self.shutdown.set()

    # ---------- claim loop ----------

    async def _claim_loop(self) -> None:
        backoff = CLAIM_BACKOFF_MIN

        while not self.shutdown.is_set():
            if self._revoked:
                # Node was revoked by the primary — stop claiming.
                await self._sleep_interruptible(5.0)
                continue

            if len(self._running) >= self.max_concurrent:
                await self._sleep_interruptible(0.1)
                continue

            try:
                claimed = await self._try_claim()
            except Exception as e:  # noqa: BLE001 - a claim failure must not kill the worker
                log.error("claim_failed", error=str(e))
                await self._sleep_interruptible(1.0)
                continue

            if claimed is None:
                await self._sleep_interruptible(backoff)
                backoff = min(backoff * 2, CLAIM_BACKOFF_MAX)
                continue

            backoff = CLAIM_BACKOFF_MIN
            await self._start_job(claimed)

    async def _try_claim(self):
        state = await governor.read_state(get_redis())
        allowed = set(governor.allowed_classes(state))

        # Escape hatch: sustained *external* load (an aligner running in a
        # terminal) would otherwise hold the governor closed indefinitely, and
        # maintenance would never run. A verify_files that never runs is a
        # silent failure, so long-starved maintenance gets admitted anyway.
        if state is not governor.AdmissionState.OPEN and await self._maintenance_starving():
            allowed.add(JobClass.MAINTENANCE.value)

        # During ramp-up after a reopen, admit gradually rather than launching
        # everything at once and immediately re-saturating the machine.
        if self._local_governor is not None and not self._local_governor.may_admit_now():
            return None

        budgets = await self._resource_budgets()
        # Try the node-specific queue first, then the global pool.
        claimed = await self._try_claim_queue(
            keys.ready_key(self.node_id),
            allowed,
            budgets,
        )
        if claimed is None:
            claimed = await self._try_claim_queue(
                keys.ready_key(),
                allowed,
                budgets,
            )
        if claimed is not None and self._local_governor is not None:
            self._local_governor.record_admission()
        return claimed

    async def _try_claim_queue(
        self,
        ready_key: str,
        allowed: set[str],
        budgets: dict,
    ):
        """Claim one job from a specific queue, or None."""
        return await queue.claim(
            self.worker_id,
            allowed_classes=sorted(allowed),
            cpu_budget=budgets["cpu"],
            mem_mb_budget=budgets["mem_mb"],
            io_heavy_budget=budgets["io_heavy"],
            ignore_reservations=len(self._running) == 0,
            node_id=self.node_id,
            ready_key=ready_key,
        )

    async def _maintenance_starving(self) -> bool:
        """True when the oldest queued maintenance job has waited too long.

        Checked at most once every 30s: it is a Mongo query on a path that runs
        several times a second otherwise.
        """
        now = datetime.now(UTC).timestamp()
        if now - self._starvation_checked_at < 30:
            return self._starvation_cached
        self._starvation_checked_at = now

        cutoff = datetime.now(UTC) - timedelta(seconds=governor.STARVATION_ESCAPE_SECONDS)
        oldest = await Job.find(
            Job.job_class == JobClass.MAINTENANCE,
            Job.state == JobState.QUEUED,
            Job.created_at < cutoff,
        ).first_or_none()

        self._starvation_cached = oldest is not None
        if self._starvation_cached:
            log.warning("starvation_override", job_id=str(oldest.id), type=oldest.type)
            # System-owned, not the starving job's owner: there is one queue for
            # the installation, and the fact that it is admitting maintenance
            # ahead of interactive work is a property of the machine rather
            # than news about anybody's library.
            await queue.publish_event(
                "system.starvation_override",
                {"job_id": str(oldest.id), "type": oldest.type},
                owner=keys.SYSTEM_OWNER,
            )
        return self._starvation_cached

    async def _resource_budgets(self) -> dict:
        """The ceiling to admit against, before any reservation is subtracted.

        Budgets come from the governor, which reads cgroup limits where Docker
        sets them and falls back to the VM's own resources otherwise. This is
        the pre-reservation number: claim.lua subtracts the live `bp:conc:*`
        counters from it atomically as part of claiming, so no reservation
        arithmetic belongs here -- see _free_resources for the analogous
        Python-side computation used for logging and non-claim callers.
        """
        if self._local_governor is not None:
            cpu_budget = self._local_governor.cpu_budget()
            mem_budget = self._local_governor.mem_budget_bytes()
        else:
            cpu_budget = float(psutil.cpu_count() or 4)
            mem_budget = psutil.virtual_memory().total

        # The user's admission budget, if they set one. It only ever lowers
        # the ceiling -- see resource_limit_service.resolve_mem_budget_mb.
        #
        # This is the entire enforcement path for the setting: `claim.lua`
        # already refuses any candidate whose declared mem_mb exceeds the
        # live-computed free amount, so a smaller ceiling here *is* the limit
        # taking effect. A read failure falls back to the machine budget
        # rather than stalling dispatch, matching _read_reservations' policy
        # for the same reason.
        machine_mb = int(mem_budget / (1024 * 1024))
        try:
            stored = await resource_limit_service.load()
            budget_source_mb = resource_limit_service.resolve_mem_budget_mb(
                stored_mb=stored.max_mem_mb, machine_mb=machine_mb
            )
            if stored.max_cpu:
                cpu_budget = min(cpu_budget, stored.max_cpu)
        except Exception as e:  # noqa: BLE001 - dispatch must survive a DB blip
            log.warning("resource_limits_read_failed", error=str(e))
            budget_source_mb = machine_mb

        available_mb = int(psutil.virtual_memory().available / (1024 * 1024))
        # Never hand out the last of memory: leave headroom so a job that
        # slightly overshoots its declared demand does not push into swap.
        budget_mb = int(budget_source_mb * 0.7)

        return {
            "cpu": int(cpu_budget),
            "mem_mb": max(min(available_mb, budget_mb), 128),
            "io_heavy": IO_HEAVY_LIMIT,
        }

    async def _free_resources(self) -> dict:
        """Capacity headroom this worker will admit against.

        Reserved amounts come from the `bp:conc:*` counters that `claim.lua`
        maintains, not from a count of running jobs. The counters are what the
        reservation actually is: with every handler declaring cpu=1 the two
        agreed, but `trim_reads` and `align_reads` declare the user's thread
        count, so a 16-thread alignment and a single-CPU job are the same
        number to a count and very different to the machine.

        This is a Python-side estimate for logging and callers other than
        claiming: it reads reservations one round trip before use, which is
        exactly the staleness window claim.lua closes for the actual claim by
        reading the same counters live inside its own atomic execution
        instead. See _try_claim, which calls _resource_budgets directly.
        """
        budgets = await self._resource_budgets()
        reserved = await self._read_reservations()
        return compute_free_resources(
            cpu_budget=budgets["cpu"],
            mem_mb=budgets["mem_mb"],
            reserved_cpu=reserved["cpu"],
            reserved_mem=reserved["mem_mb"],
            reserved_io_heavy=reserved["io_heavy"],
            in_flight=len(self._running),
        )

    async def _read_reservations(self) -> dict:
        """Current cluster-wide reservations, or zeroes if Redis cannot say.

        A failed read must not stall dispatch: falling back to zero reserved
        lets this worker admit against its own in-flight clamp, which is the
        pre-existing behaviour and never over-admits by more than one job.
        """
        try:
            values = await get_redis().mget(
                keys.conc_key("cpu", self.node_id),
                keys.conc_key("mem_mb", self.node_id),
                keys.conc_key("io_heavy", self.node_id),
            )
        except Exception as e:  # noqa: BLE001 - dispatch must survive a Redis blip
            log.warning("reservation_read_failed", error=str(e))
            return {"cpu": 0, "mem_mb": 0, "io_heavy": 0}
        return {
            "cpu": _as_int(values[0]),
            "mem_mb": _as_int(values[1]),
            "io_heavy": _as_int(values[2]),
        }

    async def _start_job(self, claimed) -> None:
        job = await queue.mark_running(claimed.job_id, self.worker_id, claimed.epoch)
        if job is None:
            # Deleted or cancelled between claim and start.
            await queue.release(claimed.job_id, requeue=False)
            return

        spec = get_handler(job.type)
        if spec is None:
            await queue.complete(
                claimed.job_id,
                claimed.epoch,
                state=JobState.FAILED,
                error={
                    "code": "unknown_handler",
                    "message": f"No handler registered for job type {job.type!r}",
                    "retryable": False,
                },
            )
            return

        # Resolved once here, at claim time, and cached on the context for the
        # rest of the run -- never re-queried from the throttled progress
        # writer, which runs up to twice a second. A list, not the first
        # match: a deduplicated job (build_index reused by a second
        # alignment) genuinely belongs to more than one run.
        run_ids = [str(rid) for rid in await run_service.runs_for_job(job.id)]

        # Same reasoning as run_ids: the model's prediction is a function of
        # job type and input size, both fixed at claim time, so caching it
        # here costs nothing in accuracy and takes a Mongo read off a path
        # that runs twice a second per job.
        eta_model_ms = await self._eta_model_ms(job)

        ctx = JobContext(
            job_id=claimed.job_id,
            payload=job.payload,
            epoch=claimed.epoch,
            attempts=job.attempts,
            owner=job.owner,
            run_ids=run_ids,
            eta_model_ms=eta_model_ms,
            started_at=job.timing.started_at,
        )
        ctx._progress_cb = lambda upd: self.executor._schedule_progress(
            claimed.job_id,
            claimed.epoch,
            upd,
            owner=job.owner,
            run_ids=run_ids,
            started_at=job.timing.started_at,
            eta_model_ms=eta_model_ms,
        )
        # Renew immediately rather than waiting up to a full heartbeat interval:
        # a handler calls extend_lease *because* it is about to go quiet, and the
        # gap between the call and the next tick is exactly when it is exposed.
        ctx._extend_cb = lambda seconds: self.executor._schedule_lease_extension(
            claimed.job_id, claimed.epoch, seconds
        )

        task = asyncio.create_task(self._run_and_cleanup(job, spec, claimed.epoch, ctx))
        self._running[claimed.job_id] = (task, ctx, claimed.epoch)

    async def _run_and_cleanup(self, job: Job, spec, epoch: int, ctx: JobContext) -> None:
        try:
            # The worker owns the context so the cancel watcher can signal this
            # exact job; the executor runs against it rather than making its own.
            await self.executor.run(job, spec, epoch, ctx=ctx)
        finally:
            self._running.pop(str(job.id), None)

    async def _eta_model_ms(self, job: Job) -> int | None:
        """The prior-runs duration model's prediction for this job, or None.

        Mirrors the size resolution `executor._record_timing` uses: a job's
        own payload size first, falling back to its object's size. A job with
        neither (a schedule tick) or too little history has nothing to
        correlate, so this is None -- eta_seconds then falls back to pure
        extrapolation once there is enough progress to trust, or reports no
        ETA at all.
        """
        from app.services import timing_service

        size = job.payload.get("size") or 0
        if not size and job.object_id:
            from app.models import DataObject

            obj = await DataObject.get(job.object_id)
            size = obj.size if obj else 0
        if not size:
            return None

        estimate = await timing_service.estimate(
            job.type, size, threads=job.payload.get("threads")
        )
        if estimate is None or not estimate.get("known"):
            return None
        return estimate["estimate_ms"]

    # ---------- heartbeat ----------

    async def _heartbeat_loop(self) -> None:
        interval = max(settings.lease_ttl_seconds / 3, 2)
        while not self.shutdown.is_set() or self._running:
            try:
                if self._running:
                    ids = list(self._running)
                    epochs = {jid: e for jid, (_, _, e) in self._running.items()}
                    # Handlers that declared a long quiet phase renew to what
                    # they asked for; everything else takes the global default.
                    ttls = {
                        jid: ctx.lease_override_seconds
                        for jid, (_, ctx, _) in self._running.items()
                        if ctx.lease_override_seconds is not None
                    }
                    await queue.heartbeat(ids, epochs, ttls)
                await self._register_worker()
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat_failed", error=str(e))
            await asyncio.sleep(interval)

    async def _register_worker(self) -> None:
        await get_redis().hset(
            keys.WORKERS,
            self.worker_id,
            json.dumps(
                {
                    "last_seen": datetime.now(UTC).isoformat(),
                    "slots": self.max_concurrent,
                    "running": list(self._running),
                    "draining": self.shutdown.is_set(),
                    "node_id": self.node_id,
                }
            ),
        )

    # ---------- enrollment ----------

    async def _enroll(self) -> None:
        """Call POST /nodes/enroll on the primary at startup.

        Only called when ``primary_api_url`` is set (compute nodes).
        Does not crash the worker on failure — enrollment is advisory, and
        the primary may not be reachable yet at startup.
        """
        url = f"{settings.primary_api_url.rstrip('/')}/api/v1/nodes/enroll"
        payload = {
            "node_id": self.node_id,
            "hostname": socket.gethostname(),
            "enrollment_key": settings.enrollment_key,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 403:
                    log.error(
                        "enrollment_rejected",
                        node_id=self.node_id,
                        status=resp.status_code,
                        detail=resp.text[:200],
                    )
                    self._revoked = True
                elif resp.status_code >= 400:
                    log.warning(
                        "enrollment_failed",
                        node_id=self.node_id,
                        status=resp.status_code,
                        detail=resp.text[:200],
                    )
                else:
                    log.info("enrolled", node_id=self.node_id)
        except Exception as e:  # noqa: BLE001 — enrollment is advisory
            log.warning("enrollment_error", node_id=self.node_id, error=str(e))

    async def _enrollment_watch_loop(self) -> None:
        """Periodically check whether this node is still active.

        If the primary revokes this node, we stop claiming jobs until the
        next enrollment attempt (which will also be rejected).
        """
        await asyncio.sleep(30)  # Let the enrollment request settle first.
        while not self.shutdown.is_set():
            try:
                url = (
                    f"{settings.primary_api_url.rstrip('/')}"
                    f"/api/v1/nodes/{self.node_id}/status"
                )
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url)
                    if resp.status_code == 403:
                        self._revoked = True
                        log.warning(
                            "node_revoked",
                            node_id=self.node_id,
                            detail=resp.text[:200],
                        )
                    elif resp.status_code == 404:
                        # Not yet enrolled — try again.
                        log.info("node_not_found_re_enrolling", node_id=self.node_id)
                        await self._enroll()
                    elif resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") == "revoked":
                            self._revoked = True
                            log.warning("node_revoked", node_id=self.node_id)
                        else:
                            self._revoked = False
            except Exception as e:  # noqa: BLE001
                log.debug("enrollment_watch_error", node_id=self.node_id, error=str(e))
            await asyncio.sleep(30)

    # ---------- cancellation ----------

    async def _cancel_watch_loop(self) -> None:
        """Poll for cancellation requests affecting this worker's jobs.

        Polling rather than pub/sub: the set is authoritative and survives a
        dropped message, and at one-second granularity the cost is trivial.
        """
        while not self.shutdown.is_set():
            try:
                if self._running:
                    cancelled = await get_redis().smembers(keys.CANCEL)
                    for job_id in cancelled:
                        entry = self._running.get(job_id)
                        if entry is not None and not entry[1].cancel_event.is_set():
                            log.info("cancelling_job", job_id=job_id)
                            entry[1].cancel_event.set()
            except Exception as e:  # noqa: BLE001
                log.warning("cancel_watch_failed", error=str(e))
            await asyncio.sleep(1.0)

    # ---------- load sampling ----------

    async def _load_sampler_loop(self) -> None:
        """Sample system load and publish one shared decision.

        Sampling happens on every worker (it is cheap and keeps each EWMA warm
        for the local ramp limiter), but only the leader publishes the state
        that everyone acts on.
        """
        self._local_governor = governor.LoadGovernor()

        while not self.shutdown.is_set():
            try:
                self._local_governor.sample()
                self._local_governor.evaluate()
                if await self._acquire_leadership():
                    await governor.publish(get_redis(), self._local_governor)
            except Exception as e:  # noqa: BLE001 - sampling must never kill the worker
                log.warning("load_sample_failed", error=str(e))
            await asyncio.sleep(governor.SAMPLE_INTERVAL)

    # ---------- leader duties ----------

    async def _leader_loop(self) -> None:
        """Reaper, promotion, and the schedule beat run on exactly one worker.

        Running them everywhere would multiply the work and let two reapers
        requeue the same job twice.
        """
        while not self.shutdown.is_set():
            try:
                if await self._acquire_leadership():
                    await queue.promote_delayed()
                    await queue.reap_expired()
                    await queue.rescue_orphans()
                    await self._maybe_promote_aged()
                    await self._tick_schedules()
                    await self._reconcile_workflows()
            except Exception as e:  # noqa: BLE001
                log.warning("leader_duties_failed", error=str(e))
            await asyncio.sleep(settings.reaper_interval_seconds)

    async def _reconcile_workflows(self) -> None:
        """Relaunch workflow nodes that became runnable but were not launched.

        Periodic rather than startup-only, and the difference is not academic:
        a real trim -> qc run stalled permanently in testing because one
        `_advance` failed on a transient read of an output object that was
        present a second later. The node stayed PENDING with its upstream
        SUCCEEDED, and with recovery only at startup nothing ever retried --
        the run looked healthy and was simply finished forever.

        The startup call stays: it covers the crash-between-writes window that
        the design's §10 names, which a running leader cannot observe.

        Isolated in its own try, so a stuck workflow cannot take the reaper and
        the schedule beat down with it.
        """
        from app.services import workflow_orchestrator

        try:
            await workflow_orchestrator.reconcile_workflows()
        except Exception as e:  # noqa: BLE001
            log.warning("workflow_reconcile_failed", error=str(e))

    async def _tick_schedules(self) -> None:
        from app.queue import scheduler

        try:
            await scheduler.tick()
        except Exception as e:  # noqa: BLE001
            log.warning("schedule_tick_failed", error=str(e))

    async def _acquire_leadership(self) -> bool:
        return bool(
            await get_redis().set(
                keys.LEADER, self.worker_id, nx=True, px=LEADER_LOCK_TTL_MS
            )
            or await get_redis().get(keys.LEADER) == self.worker_id
        )

    _last_promote: float = 0.0

    async def _maybe_promote_aged(self) -> None:
        now = datetime.now(UTC).timestamp()
        if now - self._last_promote < settings.promote_interval_seconds:
            return
        self._last_promote = now
        await queue.promote_aged()

    # ---------- watchdog ----------

    async def _loop_watchdog(self) -> None:
        """Detect event-loop stalls.

        A blocked loop stops heartbeats, which expires leases, which double-runs
        jobs. Surfacing the stall makes that chain diagnosable instead of
        mysterious.
        """
        while not self.shutdown.is_set():
            before = datetime.now(UTC).timestamp()
            await asyncio.sleep(1.0)
            drift = datetime.now(UTC).timestamp() - before - 1.0
            if drift > LOOP_STALL_WARN_SECONDS:
                log.warning("event_loop_stalled", drift_seconds=round(drift, 3))

    # ---------- tool cache invalidation ----------

    async def _tool_invalidation_loop(self) -> None:
        """Keep this worker's tool probe cache in sync with installs and
        uninstalls performed by any process, including itself.

        Every worker replica and the api process each hold their own
        `lru_cache` of probe results. Without this, an install completed here
        (or by a sibling worker, or by the api) would leave the others serving
        a stale "not installed" or "installed" verdict until they happened to
        restart -- see `tool_cache.listen_for_invalidations` for the full
        reasoning. Runs for the life of the process; cancelled alongside the
        other background loops in `_drain`.
        """
        await tool_cache.listen_for_invalidations(get_redis())

    # ---------- shutdown ----------

    async def _drain(self) -> None:
        """Finish in-flight work, then requeue whatever did not finish.

        Jobs still running at the timeout are requeued *without* incrementing
        attempts: a deliberate shutdown is not the job's fault, and counting it
        would eventually mark a perfectly good job dead.
        """
        log.info("draining", running=len(self._running))

        # Wait on the tasks themselves rather than polling, so shutdown finishes
        # the instant the last job does instead of up to half a second later.
        if self._running:
            tasks = [entry[0] for entry in self._running.values()]
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=settings.drain_timeout_seconds,
                )

        if self._running:
            log.warning("drain_timeout_requeueing", count=len(self._running))
            for job_id, (task, _, epoch) in list(self._running.items()):
                task.cancel()
                job = await Job.get(PydanticObjectId(job_id))
                score = None
                if job is not None:
                    from app.queue.priority import compute_score

                    score = compute_score(job.job_class, job.timing.enqueued_at)
                    await job.set({Job.state: JobState.QUEUED, Job.lease: None})
                await queue.release(job_id, requeue=True, score=score)
                log.info("requeued_on_shutdown", job_id=job_id, epoch=epoch)

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with contextlib.suppress(Exception):
            await get_redis().hdel(keys.WORKERS, self.worker_id)

        log.info("worker_stopped", worker_id=self.worker_id)

    async def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep, but wake immediately on shutdown."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.shutdown.wait(), timeout=seconds)
