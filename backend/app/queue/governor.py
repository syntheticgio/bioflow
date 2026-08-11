"""Load sampling and admission control.

The naive version of this -- "if CPU > 80%, don't start a job" -- oscillates
badly, because the job you start *causes* the spike that blocks the next one,
which finishes, which frees CPU, which admits three at once. Four mechanisms
prevent that:

  (a) EWMA smoothing       decide on a trend, never an instantaneous sample
  (b) hysteresis           close at 85%, reopen at 70% -- a wide dead band
  (c) dwell time           no state change within 20s of the last one
  (d) token-bucket ramp    after reopening, admit gradually, not all at once

Admission is three-state rather than binary, and `user_interactive` work is
never fully blocked: a user who clicks a button must always get a response,
even if it costs one more process. A UI that goes dead under load is a worse
outcome than a machine that is briefly oversubscribed.
"""

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import psutil

from app.config import settings
from app.logging import get_logger
from app.models import JobClass

log = get_logger(__name__)


class AdmissionState(StrEnum):
    OPEN = "OPEN"
    THROTTLED = "THROTTLED"
    CLOSED = "CLOSED"


# user_interactive appears in every row: see the module docstring.
ADMITTED_CLASSES: dict[AdmissionState, set[JobClass]] = {
    AdmissionState.OPEN: {
        JobClass.USER_INTERACTIVE,
        JobClass.USER_BACKGROUND,
        JobClass.MAINTENANCE,
        JobClass.COMPUTE,
        JobClass.BULK,
    },
    AdmissionState.THROTTLED: {JobClass.USER_INTERACTIVE, JobClass.USER_BACKGROUND},
    AdmissionState.CLOSED: {JobClass.USER_INTERACTIVE},
}

# --- Tuning ---------------------------------------------------------------

SAMPLE_INTERVAL = 2.0
EWMA_ALPHA = 0.25  # ~8s effective window at a 2s sample rate

# Close when ANY of these is exceeded; reopen only when ALL are below the open
# threshold. The gap is what stops the state from flapping.
CPU_CLOSE, CPU_OPEN = 85.0, 70.0
MEM_CLOSE, MEM_OPEN = 88.0, 75.0
LOAD_CLOSE, LOAD_OPEN = 1.5, 1.0  # normalized by core count
DISK_FREE_CLOSE_PCT, DISK_FREE_OPEN_PCT = 5.0, 8.0
DISK_FREE_CLOSE_BYTES = 20 * 1024**3
DISK_FREE_OPEN_BYTES = 30 * 1024**3
SWAP_CLOSE_MB_S, SWAP_OPEN_MB_S = 5.0, 1.0

# THROTTLED sits between the two bands: elevated but not yet critical.
CPU_THROTTLE = 75.0
MEM_THROTTLE = 80.0

DWELL_SECONDS = 20.0
RAMP_INTERVAL = 5.0  # after reopening, at most one admission per this window
RAMP_COUNT = 3  # consecutive clean admissions before normal operation

# Sustained *external* load (an aligner running in a terminal) would otherwise
# hold the governor CLOSED forever, and maintenance would never run. A
# verify_files job that never runs is a silent failure, so it gets a way out.
#
# The escape is deliberately limited to maintenance (see worker._maintenance_
# starving). Compute does not qualify: a waiting pipeline run is *visible* as
# waiting in the activity view, so it fails loudly rather than silently, and
# forcing a multi-hour job onto an already-strained machine is the outcome the
# governor exists to prevent.
STARVATION_ESCAPE_SECONDS = 30 * 60


@dataclass
class LoadSample:
    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    mem_available: int = 0
    load1: float = 0.0
    swap_in_mb_s: float = 0.0
    disk_free_bytes: int = 0
    disk_free_percent: float = 100.0
    at: float = field(default_factory=time.monotonic)


class LoadGovernor:
    """Samples system load and decides which job classes may be admitted."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self.state = AdmissionState.OPEN
        self.ewma: LoadSample | None = None
        # Backdated so the first genuine transition is never blocked by dwell.
        # Starting at "now" would mean a worker booting onto an already-loaded
        # machine ignores that load for the first 20 seconds.
        self._state_changed_at = clock() - DWELL_SECONDS
        self._ramp_started_at: float | None = None
        self._ramp_admissions = 0
        self._last_admission_at = 0.0
        self._last_swap_in: int | None = None
        self._last_swap_at: float | None = None
        self._primed = False

    # --- sampling ---------------------------------------------------------

    def sample(self) -> LoadSample:
        """Take one raw reading and fold it into the EWMA."""
        # psutil.cpu_percent(interval=None) returns 0.0 on its first call: it
        # reports the delta since the previous call, and there isn't one yet.
        raw_cpu = psutil.cpu_percent(interval=None)
        if not self._primed:
            self._primed = True
            raw_cpu = 0.0

        vm = psutil.virtual_memory()
        cores = self.cpu_budget()
        try:
            load1 = os.getloadavg()[0] / max(cores, 1)
        except OSError:
            load1 = 0.0

        sample = LoadSample(
            cpu_percent=raw_cpu,
            mem_percent=vm.percent,
            mem_available=vm.available,
            load1=load1,
            swap_in_mb_s=self._swap_rate(),
            **self._disk(),
            at=self._clock(),
        )

        if self.ewma is None:
            self.ewma = sample
        else:
            a = EWMA_ALPHA
            self.ewma = LoadSample(
                cpu_percent=a * sample.cpu_percent + (1 - a) * self.ewma.cpu_percent,
                mem_percent=a * sample.mem_percent + (1 - a) * self.ewma.mem_percent,
                mem_available=sample.mem_available,
                load1=a * sample.load1 + (1 - a) * self.ewma.load1,
                swap_in_mb_s=a * sample.swap_in_mb_s + (1 - a) * self.ewma.swap_in_mb_s,
                disk_free_bytes=sample.disk_free_bytes,
                disk_free_percent=sample.disk_free_percent,
                at=sample.at,
            )
        return sample

    def _swap_rate(self) -> float:
        """Swap-in rate in MB/s -- the strongest single signal of real trouble."""
        try:
            swap = psutil.swap_memory()
        except Exception:  # noqa: BLE001
            return 0.0
        now = self._clock()
        sin = getattr(swap, "sin", 0)
        if self._last_swap_in is None or self._last_swap_at is None:
            self._last_swap_in, self._last_swap_at = sin, now
            return 0.0
        elapsed = now - self._last_swap_at
        if elapsed <= 0:
            return 0.0
        delta = max(0, sin - self._last_swap_in)
        self._last_swap_in, self._last_swap_at = sin, now
        return (delta / elapsed) / (1024 * 1024)

    def _disk(self) -> dict:
        """Free space at BIOINFO_HOME.

        Known wrong under Docker Desktop: VirtioFS answers statfs from the
        filesystem hosting the share root (/Volumes), so this measures the
        Mac's boot disk rather than the drive the data is on. That misreads in
        both directions -- a full boot disk stops pipeline work needlessly, and
        a full data drive goes unnoticed. Nothing inside the container can see
        past the share; the fix is a host-side reporter, sketched in
        docs/TODO.md.
        """
        try:
            usage = shutil.disk_usage(settings.bioinfo_home)
            return {
                "disk_free_bytes": usage.free,
                "disk_free_percent": usage.free / usage.total * 100 if usage.total else 100.0,
            }
        except OSError:
            # Storage being unreachable is the storage layer's problem to
            # report; the governor must not treat it as "no space".
            return {"disk_free_bytes": 0, "disk_free_percent": 100.0}

    # --- budgets ----------------------------------------------------------

    def cpu_budget(self) -> float:
        """Cores actually available to us.

        Inside Docker Desktop, psutil reports the *Linux VM's* resources, not
        the Mac's. When a cgroup quota exists that is the real constraint; on
        this setup Docker Desktop leaves it unset (`max`), so the VM's core
        count is the honest answer.
        """
        if settings.bioinfo_cpu_budget:
            return float(settings.bioinfo_cpu_budget)
        quota = _read_cgroup_cpu()
        if quota is not None:
            return quota
        return float(psutil.cpu_count() or 1)

    def mem_budget_bytes(self) -> int:
        if settings.bioinfo_mem_budget_mb:
            return settings.bioinfo_mem_budget_mb * 1024 * 1024
        limit = _read_cgroup_mem()
        if limit is not None:
            return limit
        return psutil.virtual_memory().total

    # --- state machine ----------------------------------------------------

    def evaluate(self) -> AdmissionState:
        """Recompute the admission state from the smoothed metrics."""
        if self.ewma is None:
            return self.state

        now = self._clock()
        desired = self._desired_state(self.ewma)

        if desired is self.state:
            return self.state

        # Dwell: refuse to change state again too soon. This alone removes most
        # oscillation, because it puts a floor on the flap frequency.
        if now - self._state_changed_at < DWELL_SECONDS:
            return self.state

        previous = self.state
        self.state = desired
        self._state_changed_at = now

        # Reopening starts a ramp: admitting everything at once would simply
        # re-saturate the machine and close the governor again.
        if _severity(desired) < _severity(previous):
            self._ramp_started_at = now
            self._ramp_admissions = 0
        else:
            self._ramp_started_at = None

        log.info(
            "governor_state_changed",
            from_state=previous.value,
            to_state=desired.value,
            cpu=round(self.ewma.cpu_percent, 1),
            mem=round(self.ewma.mem_percent, 1),
            load1=round(self.ewma.load1, 2),
        )
        return self.state

    def _desired_state(self, m: LoadSample) -> AdmissionState:
        if self._breaches_close(m):
            return AdmissionState.CLOSED
        if self.state is AdmissionState.CLOSED and not self._clears_open(m):
            # Between the bands while closed: hold, do not half-reopen.
            return AdmissionState.CLOSED
        if self._breaches_throttle(m):
            return AdmissionState.THROTTLED
        if self.state is AdmissionState.THROTTLED and not self._clears_open(m):
            return AdmissionState.THROTTLED
        return AdmissionState.OPEN

    def _breaches_close(self, m: LoadSample) -> bool:
        budget = self.mem_budget_bytes()
        return (
            m.cpu_percent > CPU_CLOSE
            or m.mem_percent > MEM_CLOSE
            or m.mem_available < min(0.12 * budget, 2 * 1024**3)
            or m.load1 > LOAD_CLOSE
            or m.swap_in_mb_s > SWAP_CLOSE_MB_S
            or (0 < m.disk_free_percent < DISK_FREE_CLOSE_PCT)
            or (0 < m.disk_free_bytes < DISK_FREE_CLOSE_BYTES)
        )

    def _clears_open(self, m: LoadSample) -> bool:
        budget = self.mem_budget_bytes()
        return (
            m.cpu_percent < CPU_OPEN
            and m.mem_percent < MEM_OPEN
            and m.mem_available > min(0.20 * budget, 4 * 1024**3)
            and m.load1 < LOAD_OPEN
            and m.swap_in_mb_s < SWAP_OPEN_MB_S
            and (m.disk_free_percent > DISK_FREE_OPEN_PCT or m.disk_free_bytes == 0)
        )

    def _breaches_throttle(self, m: LoadSample) -> bool:
        return m.cpu_percent > CPU_THROTTLE or m.mem_percent > MEM_THROTTLE

    # --- admission --------------------------------------------------------

    def allowed_classes(self) -> list[str]:
        return sorted(c.value for c in ADMITTED_CLASSES[self.state])

    def may_admit_now(self) -> bool:
        """Rate limit during ramp-up after a reopen."""
        if self._ramp_started_at is None:
            return True
        now = self._clock()
        if now - self._last_admission_at < RAMP_INTERVAL:
            return False
        return True

    def record_admission(self) -> None:
        now = self._clock()
        self._last_admission_at = now
        if self._ramp_started_at is None:
            return
        self._ramp_admissions += 1
        if self._ramp_admissions >= RAMP_COUNT:
            # Survived enough admissions without tripping: resume full speed.
            self._ramp_started_at = None
            self._ramp_admissions = 0

    def snapshot(self) -> dict:
        m = self.ewma or LoadSample()
        return {
            "state": self.state.value,
            "admitted_classes": self.allowed_classes(),
            "ramping": self._ramp_started_at is not None,
            "cpu": {
                "percent": round(m.cpu_percent, 1),
                "budget": self.cpu_budget(),
                "load1_normalized": round(m.load1, 2),
            },
            "memory": {
                "percent": round(m.mem_percent, 1),
                "available_bytes": m.mem_available,
                "budget_bytes": self.mem_budget_bytes(),
                "swap_in_mb_s": round(m.swap_in_mb_s, 2),
            },
            "disk": {
                "free_bytes": m.disk_free_bytes,
                "free_percent": round(m.disk_free_percent, 1),
            },
            "governor_active": True,
        }


def _read_cgroup_cpu() -> float | None:
    """Cores from the cgroup v2 quota, or None when unlimited."""
    try:
        raw = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if not raw or raw[0] == "max":
            return None
        return int(raw[0]) / int(raw[1])
    except (OSError, ValueError, IndexError):
        return None


def _read_cgroup_mem() -> int | None:
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def _severity(state: AdmissionState) -> int:
    return {AdmissionState.OPEN: 0, AdmissionState.THROTTLED: 1, AdmissionState.CLOSED: 2}[
        state
    ]


# --- Shared state across workers -------------------------------------------
#
# Only the leader samples and publishes. If every worker decided independently
# they would disagree at the margins, and half of them would admit while the
# other half refused.

STATE_KEY = "bp:load:state"
SNAPSHOT_KEY = "bp:load:snapshot"
SNAPSHOT_TTL = 15


async def publish(redis, governor: LoadGovernor) -> None:
    snap = governor.snapshot()
    await redis.set(STATE_KEY, governor.state.value, ex=60)
    await redis.set(SNAPSHOT_KEY, json.dumps(snap), ex=SNAPSHOT_TTL)


async def read_state(redis) -> AdmissionState:
    """The governor decision every worker follows.

    A missing key means no leader has published recently. Defaulting to OPEN is
    deliberate: a stalled sampler must not silently halt all work.
    """
    try:
        raw = await redis.get(STATE_KEY)
        return AdmissionState(raw) if raw else AdmissionState.OPEN
    except Exception:  # noqa: BLE001
        return AdmissionState.OPEN


async def read_snapshot(redis) -> dict | None:
    try:
        raw = await redis.get(SNAPSHOT_KEY)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


async def current_state() -> AdmissionState:
    from app.db.redis_client import get_redis

    return await read_state(get_redis())


def allowed_classes(state: AdmissionState = AdmissionState.OPEN) -> list[str]:
    return sorted(c.value for c in ADMITTED_CLASSES[state])


async def _node_breakdown() -> tuple[list[dict], str | None]:
    """Per-node running/queued/reserved, and an error string if it failed.

    Imported inside the function: `app.api.v1.nodes` imports from
    `app.queue`, so a module-level import here would be circular.

    Runs unconditionally on every call, including from the three views that
    poll this endpoint every 5s but don't render `nodes` (only the nodes
    settings table does). Accepted rather than cached: node counts on a
    single-user local install are small, and this repo optimizes for "the
    person using it can see their change" over correctness-at-scale --
    caching would trade a real staleness risk for a poll-cost saving that
    doesn't matter at this scale.
    """
    try:
        from app.api.v1.nodes import enumerate_nodes
        from app.queue import node_stats as node_stats_mod

        by_node = await enumerate_nodes()
        known = set(by_node)
        orphans = await node_stats_mod.orphaned_queue_nodes(known)
        stats = await node_stats_mod.node_stats(list(known) + orphans)

        nodes = []
        for node_id in sorted(known | set(orphans)):
            info = by_node.get(node_id, {})
            s = stats.get(node_id, {"queued": 0, "cpu": 0, "mem_mb": 0})
            nodes.append(
                {
                    "node_id": node_id,
                    "running": info.get("running_jobs", 0),
                    "queued": s["queued"],
                    "cpu": s["cpu"],
                    "mem_mb": s["mem_mb"],
                    "workers": info.get("workers", 0),
                    "known": node_id in known,
                }
            )
        return nodes, None
    except Exception as e:  # noqa: BLE001
        log.warning("node_breakdown_failed", error=str(e))
        return [], str(e)


async def current_load() -> dict:
    """Backing data for /system/load and the header indicator.

    The per-node breakdown is computed here rather than published in the
    leader's snapshot: baking it in would make it stale up to the snapshot TTL
    and absent entirely on the fallback path below, so the whole feature would
    vanish exactly when the leader lock lapses.
    """
    from app.db.redis_client import get_redis

    nodes, nodes_error = await _node_breakdown()

    snap = await read_snapshot(get_redis())
    if snap is not None:
        snap["nodes"] = nodes
        if nodes_error:
            snap["nodes_error"] = nodes_error
        return snap

    # No leader has published yet (or Redis is empty): report raw metrics so the
    # endpoint stays useful, and say the governor is not driving anything.
    vm = psutil.virtual_memory()
    load = {
        "state": AdmissionState.OPEN.value,
        "admitted_classes": allowed_classes(),
        "ramping": False,
        "cpu": {"percent": psutil.cpu_percent(interval=None), "budget": psutil.cpu_count()},
        "memory": {"percent": vm.percent, "available_bytes": vm.available},
        "disk": None,
        "governor_active": False,
        "nodes": nodes,
    }
    if nodes_error:
        load["nodes_error"] = nodes_error
    return load
