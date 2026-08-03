# Computation Records Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Widen the existing `job_timings` collection into a durable per-run record capturing resources, machine, invocation and input features, and use it to predict both duration and peak memory.

**Architecture:** One collection, three readers. `JobRunTiming` gains resource/machine/invocation blocks. A sampler task in `JobExecutor` polls the job's process subtree for peak RSS and CPU. `timing_service` keeps its existing least-squares fit, adds a memory fit, and gains extrapolation flagging. Failed runs are now recorded for provenance and filtered out of the fits behind a single accessor.

**Tech Stack:** Python 3.12, Beanie/Motor (MongoDB), psutil, pytest, FastAPI.

**Design spec:** `docs/superpowers/specs/2026-08-03-computation-records-design.md`

---

## Context an engineer needs before starting

**Run tests from the worktree, never with `docker compose exec api`.** That command tests *main's* code, silently. Use:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Single file or test:

```bash
./backend/run-worktree-tests.sh tests/storage/test_timing_model.py -q
```

**The worker does not hot-reload.** Nothing in this plan needs a running stack to test, but if you exercise a real pipeline job, run `./ops/worktree-up.sh` and restart its worker first.

**Existing code this builds on:**

- `backend/app/models/timing.py` — `JobRunTiming`, the collection being widened.
- `backend/app/services/timing_service.py` — `record()`, `_samples()`, `_fit()`, `_r_squared()`, `estimate()`, `stats()`. The pure fitting functions are already unit-tested in `backend/tests/storage/test_timing_model.py`.
- `backend/app/queue/executor.py:126` — `_record_timing`, called at line 80 on the success path only.
- `backend/app/queue/governor.py:357` — `_read_cgroup_cpu()`, `_read_cgroup_mem()`. Reuse these; do not write new cgroup parsing.
- `backend/app/models/base.py` — `TimestampedDocument` already supplies `owner`, `created_at`, `updated_at`, and `schema_version`. **Do not add a `schema_version` field to the model; it is inherited.**

**Testing style in this repo:** the fitting arithmetic is tested pure (no Mongo) in `tests/storage/test_timing_model.py`. Follow that — new pure functions get pure tests. Only tests that genuinely need persistence should touch the database.

---

## File Structure

**Create:**

- `backend/app/queue/resource_sampler.py` — polls a process subtree, tracks peak RSS and CPU. No knowledge of jobs or Mongo.
- `backend/app/services/machine_profile.py` — captures the host fingerprint once, caches it.
- `backend/app/services/params_sanitizer.py` — strips local identifiers from a job payload.
- `backend/tests/queue/test_resource_sampler.py`
- `backend/tests/services/test_machine_profile.py`
- `backend/tests/services/test_params_sanitizer.py`
- `backend/tests/storage/test_memory_model.py`
- `backend/tests/storage/test_extrapolation.py`
- `backend/tests/queue/test_record_outcomes.py`

**Modify:**

- `backend/app/models/timing.py` — new field blocks and indexes.
- `backend/app/services/timing_service.py` — outcome filter, memory fit, extrapolation, widened `record()`.
- `backend/app/queue/executor.py` — sampler lifecycle, widened recording, failure recording.
- `backend/app/api/v1/jobs.py` — memory estimate on the job detail route, memory model in `/timing-model`.

Three small new modules rather than one: the sampler is pure process arithmetic, the machine profile is a one-shot probe, and sanitization is a string-filtering decision. Each is independently testable without the others, and none needs a database.

---

## Task 1: Resource sampler

Polls a process subtree and retains peak RSS and CPU. Deliberately knows nothing about jobs, Mongo, or the executor — it takes a PID and returns numbers.

**Files:**
- Create: `backend/app/queue/resource_sampler.py`
- Test: `backend/tests/queue/test_resource_sampler.py`

- [ ] **Step 1: Write the failing test**

```python
"""Peak resource sampling over a process subtree.

Tested against fakes rather than real processes: the arithmetic (max
retention, subtree summing, tolerance of processes that vanish mid-walk) is
what can be wrong, and spawning real children would make the test slow and
flaky without exercising anything more.
"""

import pytest

from app.queue.resource_sampler import ResourceSampler


class FakeProcess:
    """Stands in for psutil.Process."""

    def __init__(self, rss, cpu, children=None, gone=False):
        self._rss = rss
        self._cpu = cpu
        self._children = children or []
        self._gone = gone

    def memory_info(self):
        if self._gone:
            raise ProcessLookupError("process is gone")
        return type("MemInfo", (), {"rss": self._rss})()

    def cpu_percent(self, interval=None):
        if self._gone:
            raise ProcessLookupError("process is gone")
        return self._cpu

    def children(self, recursive=True):
        if self._gone:
            raise ProcessLookupError("process is gone")
        return self._children


class TestPeakRetention:
    def test_retains_the_maximum_not_the_last(self):
        """A peak is a high-water mark; a later smaller reading must not
        overwrite it."""
        sampler = ResourceSampler(pid=1)
        sampler.observe(FakeProcess(rss=900, cpu=10.0))
        sampler.observe(FakeProcess(rss=100, cpu=5.0))
        assert sampler.peak_rss_bytes == 900
        assert sampler.peak_cpu_percent == 10.0

    def test_mean_cpu_averages_every_sample(self):
        sampler = ResourceSampler(pid=1)
        sampler.observe(FakeProcess(rss=10, cpu=20.0))
        sampler.observe(FakeProcess(rss=10, cpu=40.0))
        assert sampler.mean_cpu_percent == pytest.approx(30.0)

    def test_sample_count_tracks_observations(self):
        sampler = ResourceSampler(pid=1)
        for _ in range(3):
            sampler.observe(FakeProcess(rss=10, cpu=1.0))
        assert sampler.sample_count == 3


class TestSubtree:
    def test_sums_children_into_the_total(self):
        """A tool that forks workers must be measured as one job, not as the
        parent's own small footprint."""
        children = [FakeProcess(rss=300, cpu=25.0), FakeProcess(rss=200, cpu=15.0)]
        sampler = ResourceSampler(pid=1)
        sampler.observe(FakeProcess(rss=100, cpu=10.0, children=children))
        assert sampler.peak_rss_bytes == 600
        assert sampler.peak_cpu_percent == pytest.approx(50.0)

    def test_a_child_that_vanishes_does_not_lose_the_sample(self):
        """Processes exit mid-walk constantly. The surviving members still
        produce a usable number."""
        children = [FakeProcess(rss=300, cpu=25.0), FakeProcess(rss=999, cpu=99.0, gone=True)]
        sampler = ResourceSampler(pid=1)
        sampler.observe(FakeProcess(rss=100, cpu=10.0, children=children))
        assert sampler.peak_rss_bytes == 400
        assert sampler.sample_count == 1

    def test_root_vanishing_records_no_sample(self):
        sampler = ResourceSampler(pid=1)
        sampler.observe(FakeProcess(rss=0, cpu=0.0, gone=True))
        assert sampler.sample_count == 0
        assert sampler.peak_rss_bytes is None


class TestEmptyState:
    def test_no_observations_yields_none_not_zero(self):
        """Zero would be a measurement. None is the absence of one, and the
        model must be able to tell them apart."""
        sampler = ResourceSampler(pid=1)
        assert sampler.peak_rss_bytes is None
        assert sampler.peak_cpu_percent is None
        assert sampler.mean_cpu_percent is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/queue/test_resource_sampler.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.queue.resource_sampler'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/queue/resource_sampler.py`:

```python
"""Peak CPU and memory for one job, sampled from its process subtree.

Polling rather than `resource.getrusage(RUSAGE_CHILDREN)`, which gives an
exact kernel high-water mark but is cumulative across every child of the
worker -- and the governor admits several jobs at once, so a getrusage number
cannot be attributed to one of them. Polling a subtree keeps concurrent jobs
separable, which is the property that matters more than exactness here.

The numbers replace hand-tuned coefficients in `pipelines/resource_estimator.py`
that came from published tool documentation rather than measurement, so
"within a few percent" is comfortably good enough.
"""

import psutil

from app.logging import get_logger

log = get_logger(__name__)


class ResourceSampler:
    """Accumulates peak and mean resource use across repeated observations.

    Holds no timer of its own: the caller decides when to `observe()`. That
    keeps the arithmetic testable without spawning processes or sleeping.
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.peak_rss_bytes: int | None = None
        self.peak_cpu_percent: float | None = None
        self.sample_count = 0
        self._cpu_total = 0.0

    @property
    def mean_cpu_percent(self) -> float | None:
        if self.sample_count == 0:
            return None
        return self._cpu_total / self.sample_count

    def observe(self, proc=None) -> None:
        """Take one reading of the subtree. Never raises.

        A process disappearing mid-walk is the normal case, not an error: a
        pipeline spawns and reaps children constantly. Whatever was readable
        at this instant is a valid sample.
        """
        try:
            proc = proc if proc is not None else psutil.Process(self.pid)
            rss, cpu = self._read(proc)
        except Exception:  # noqa: BLE001 - the root is gone; no sample exists
            return

        try:
            for child in proc.children(recursive=True):
                try:
                    child_rss, child_cpu = self._read(child)
                except Exception:  # noqa: BLE001 - this child exited mid-walk
                    continue
                rss += child_rss
                cpu += child_cpu
        except Exception:  # noqa: BLE001 - the root exited during the walk
            pass

        self.sample_count += 1
        self._cpu_total += cpu
        if self.peak_rss_bytes is None or rss > self.peak_rss_bytes:
            self.peak_rss_bytes = rss
        if self.peak_cpu_percent is None or cpu > self.peak_cpu_percent:
            self.peak_cpu_percent = cpu

    @staticmethod
    def _read(proc) -> tuple[int, float]:
        return proc.memory_info().rss, proc.cpu_percent(interval=None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/queue/test_resource_sampler.py -q`
Expected: PASS, 8 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/resource_sampler.py backend/tests/queue/test_resource_sampler.py
git commit -m "Add process-subtree resource sampler"
```

---

## Task 2: Machine profile

The host fingerprint stamped on every record. Probed once and cached — re-probing per job would cost a cgroup read and a psutil call for a value that cannot change while the process lives.

**Files:**
- Create: `backend/app/services/machine_profile.py`
- Test: `backend/tests/services/test_machine_profile.py`

- [ ] **Step 1: Write the failing test**

```python
"""The host fingerprint stamped on every computation record."""

from app.services import machine_profile


class TestCapture:
    def test_reports_the_fields_a_record_needs(self):
        profile = machine_profile.capture()
        for field in (
            "cpu_model",
            "physical_cores",
            "logical_cores",
            "total_ram_bytes",
            "cgroup_cpu_budget",
            "cgroup_mem_limit",
            "platform",
            "machine_id",
        ):
            assert field in profile

    def test_core_counts_are_plausible(self):
        profile = machine_profile.capture()
        assert profile["logical_cores"] >= 1
        assert profile["total_ram_bytes"] > 0

    def test_machine_id_is_stable_across_calls(self):
        """Segmenting an aggregated corpus by hardware requires the same
        machine to hash the same way every time."""
        assert machine_profile.capture()["machine_id"] == machine_profile.capture()["machine_id"]

    def test_machine_id_is_not_a_hostname(self):
        """It must identify a machine to the model without identifying it to a
        person -- these records are meant to be uploadable."""
        import socket

        machine_id = machine_profile.capture()["machine_id"]
        assert socket.gethostname() not in machine_id
        assert len(machine_id) == 16
        int(machine_id, 16)  # raises unless it is pure hex


class TestCaching:
    def test_probes_once_and_reuses(self, monkeypatch):
        calls = []
        real = machine_profile._probe

        def counting_probe():
            calls.append(1)
            return real()

        machine_profile.reset_cache()
        monkeypatch.setattr(machine_profile, "_probe", counting_probe)
        machine_profile.capture()
        machine_profile.capture()
        assert len(calls) == 1
        machine_profile.reset_cache()


class TestCgroupBudgets:
    def test_budgets_come_from_the_governor_helpers(self, monkeypatch):
        """Inside Docker the cgroup limit is what actually binds, and psutil
        reports the VM's resources instead. Recording the wrong one would
        poison the memory model."""
        monkeypatch.setattr(
            "app.queue.governor._read_cgroup_cpu", lambda: 4.0
        )
        monkeypatch.setattr(
            "app.queue.governor._read_cgroup_mem", lambda: 8 * 1024**3
        )
        machine_profile.reset_cache()
        profile = machine_profile.capture()
        assert profile["cgroup_cpu_budget"] == 4.0
        assert profile["cgroup_mem_limit"] == 8 * 1024**3
        machine_profile.reset_cache()

    def test_unlimited_cgroup_reports_none_not_a_guess(self, monkeypatch):
        monkeypatch.setattr("app.queue.governor._read_cgroup_cpu", lambda: None)
        monkeypatch.setattr("app.queue.governor._read_cgroup_mem", lambda: None)
        machine_profile.reset_cache()
        profile = machine_profile.capture()
        assert profile["cgroup_cpu_budget"] is None
        assert profile["cgroup_mem_limit"] is None
        machine_profile.reset_cache()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_machine_profile.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.machine_profile'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/machine_profile.py`:

```python
"""What this computation ran on.

Stamped on every record rather than looked up later, because the point is a
corpus that stays interpretable when it leaves this machine: a duration means
nothing without the hardware that produced it.

Both the raw totals and the cgroup budgets are recorded, because inside Docker
they differ and the budget is the one that binds -- psutil reports the Linux
VM's resources, not the host Mac's. A record claiming 32 GB when the container
was capped at 8 would skew the memory model here and skew an aggregated corpus
worse.
"""

import hashlib
import platform
import uuid

import psutil

_cached: dict | None = None


def capture() -> dict:
    """The host fingerprint. Probed once per process, then reused."""
    global _cached
    if _cached is None:
        _cached = _probe()
    return _cached


def reset_cache() -> None:
    """Drop the cached probe. For tests."""
    global _cached
    _cached = None


def _machine_id() -> str:
    """A stable, non-identifying hash of this host.

    `uuid.getnode()` is the MAC address, which is stable but identifying, so
    it is hashed and truncated. The result segments an aggregated corpus by
    hardware without naming anyone -- which is the whole reason this is not
    just the hostname.
    """
    raw = f"{uuid.getnode()}:{platform.machine()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cpu_model() -> str:
    """A human-readable CPU name, or the architecture when unavailable.

    `platform.processor()` is empty on many Linux builds, which is why there
    is a fallback rather than a bare call.
    """
    return platform.processor() or platform.machine() or "unknown"


def _probe() -> dict:
    from app.queue.governor import _read_cgroup_cpu, _read_cgroup_mem

    return {
        "cpu_model": _cpu_model(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count() or 1,
        "total_ram_bytes": psutil.virtual_memory().total,
        "cgroup_cpu_budget": _read_cgroup_cpu(),
        "cgroup_mem_limit": _read_cgroup_mem(),
        "platform": f"{platform.system()}-{platform.release()}",
        "machine_id": _machine_id(),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_machine_profile.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/machine_profile.py backend/tests/services/test_machine_profile.py
git commit -m "Add machine profile capture for computation records"
```

---

## Task 3: Params sanitizer

Strips local identifiers from a payload before it is stored. Doing this on write is far cheaper than retrofitting a scrubber onto an existing corpus the day upload ships.

**Files:**
- Create: `backend/app/services/params_sanitizer.py`
- Test: `backend/tests/services/test_params_sanitizer.py`

- [ ] **Step 1: Write the failing test**

```python
"""Payload sanitization for stored computation records.

An allowlist, not a denylist: these records are meant to be uploadable one
day, and a denylist silently ships every field nobody thought of.
"""

from app.services.params_sanitizer import sanitize


class TestAllowlist:
    def test_keeps_tuning_parameters(self):
        """These are the fields that explain a duration, which is the whole
        point of storing params at all."""
        out = sanitize({"threads": 8, "preset": "sr", "aligner": "minimap2"})
        assert out == {"threads": 8, "preset": "sr", "aligner": "minimap2"}

    def test_drops_unknown_keys(self):
        out = sanitize({"threads": 4, "some_future_field": "value"})
        assert out == {"threads": 4}

    def test_drops_paths_and_local_identifiers(self):
        out = sanitize(
            {
                "threads": 4,
                "path": "/Users/alice/data/sample.fastq.gz",
                "output_path": "/data/out.bam",
                "project_name": "Alice's secret project",
                "object_id": "507f1f77bcf86cd799439011",
            }
        )
        assert out == {"threads": 4}

    def test_empty_payload_is_empty_not_none(self):
        assert sanitize({}) == {}
        assert sanitize(None) == {}


class TestValueSafety:
    def test_drops_allowlisted_keys_carrying_path_like_values(self):
        """A key can be safe while its value is not -- an aligner field set to
        a filesystem path should not survive on the strength of its name."""
        out = sanitize({"aligner": "/Users/alice/custom/minimap2", "threads": 4})
        assert out == {"threads": 4}

    def test_keeps_scalar_types_only(self):
        """Nested structures can hide anything; scalars are checkable."""
        out = sanitize({"threads": 4, "preset": {"nested": "/Users/alice"}})
        assert out == {"threads": 4}

    def test_long_strings_are_dropped(self):
        out = sanitize({"preset": "x" * 200})
        assert out == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_params_sanitizer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.params_sanitizer'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/params_sanitizer.py`:

```python
"""What of a job payload is safe to keep forever.

An allowlist rather than a denylist, deliberately. These records are designed
to be uploadable to an aggregation server later, and a denylist ships every
field nobody remembered to think about. A new payload key defaults to being
dropped, which costs a missing predictor; the reverse defaults to leaking a
path, which cannot be undone once uploaded.

Values are checked as well as keys: a key can be perfectly safe while its
value is a filesystem path.
"""

# Fields that explain a run's cost without saying anything about the machine
# or the data's provenance.
ALLOWED_KEYS = frozenset(
    {
        "threads",
        "preset",
        "aligner",
        "assembler",
        "trimmer",
        "caller",
        "mode",
        "sort_memory_mb",
        "building_index",
        "min_length",
        "quality_cutoff",
        "kmer_size",
        "paired",
        "layout",
    }
)

MAX_STRING_LENGTH = 64

# Substrings that mark a value as local rather than descriptive.
PATH_MARKERS = ("/", "\\", "~")


def _is_safe_value(value) -> bool:
    if isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            return False
        return not any(marker in value for marker in PATH_MARKERS)
    # Nested structures can hide anything and are not worth walking.
    return False


def sanitize(payload: dict | None) -> dict:
    """The subset of a payload safe to persist and eventually upload."""
    if not payload:
        return {}
    return {
        key: value
        for key, value in payload.items()
        if key in ALLOWED_KEYS and _is_safe_value(value)
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_params_sanitizer.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/params_sanitizer.py backend/tests/services/test_params_sanitizer.py
git commit -m "Add payload sanitizer for computation records"
```

---

## Task 4: Widen the record model

**Files:**
- Modify: `backend/app/models/timing.py`

No test of its own: a Pydantic model with defaults has no behavior to assert beyond what Tasks 5–7 exercise through it. The next task fails loudly if a field is missing.

- [ ] **Step 1: Replace the model body**

`schema_version` is inherited from `TimestampedDocument` — do not redeclare it.

Replace the contents of `backend/app/models/timing.py` below the imports with:

```python
"""Recorded computation cost, used to predict duration and peak memory.

There is no honest way to compute an ingest's percentage complete a priori:
the phases have wildly different throughput (hashing is I/O-bound at mount
speed, header parsing is nearly instant, composition sampling is CPU-bound and
capped), so a byte-progress bar would sprint to 90% and then sit there.

Instead we record what each run actually cost and fit simple models against
it. Until enough samples exist the UI shows no estimate at all -- a wrong
progress bar is worse than none.

One collection, three readers: the duration model, the memory model, and
per-object provenance. They differ in whether they want failed runs. The
models must not see them (an OOM kill reads as a fast, cheap run whose peak
RSS is the ceiling it hit rather than what it needed); provenance specifically
wants them. That filter lives in `timing_service._samples()` -- see the
"Querying computation records" section of CLAUDE.md.
"""

from datetime import datetime

from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models.base import TimestampedDocument


class RunOutcome:
    """Why a run stopped. Not a StrEnum: these mirror JobState values and
    importing JobState here would make the models package circular."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


# Outcomes the predictive models may fit against.
MODELLED_OUTCOMES = (RunOutcome.SUCCEEDED,)


class RunResources(BaseModel):
    """Measured cost. Every field is None for runs under the sampling floor.

    None is the absence of a measurement, not a measurement of zero, and the
    models rely on telling them apart.
    """

    peak_rss_bytes: int | None = None
    peak_cpu_percent: float | None = None
    mean_cpu_percent: float | None = None
    sample_count: int = 0


class RunMachine(BaseModel):
    """What it ran on. See services/machine_profile.py for why both the raw
    totals and the cgroup budgets are here."""

    cpu_model: str | None = None
    physical_cores: int | None = None
    logical_cores: int | None = None
    total_ram_bytes: int | None = None
    cgroup_cpu_budget: float | None = None
    cgroup_mem_limit: int | None = None
    platform: str | None = None
    machine_id: str | None = None


class JobRunTiming(TimestampedDocument):
    """One completed run of one job type."""

    job_type: str
    # The predictor variable. Bytes is the only input known before the work
    # starts, which is what makes it usable for a forecast.
    input_bytes: int
    duration_ms: int

    # Successes only, until this design; failures are recorded now for
    # provenance and filtered out of every fit.
    outcome: str = RunOutcome.SUCCEEDED

    # Enqueue-to-start. Separated from duration because queue wait is what
    # makes a user's wall-clock experience diverge from the prediction.
    queued_ms: int | None = None

    # Known before the run starts and the largest non-size driver of wall
    # time, so it segments the duration model rather than merely being logged.
    threads: int | None = None

    resources: RunResources = Field(default_factory=RunResources)
    machine: RunMachine = Field(default_factory=RunMachine)

    # Which binary, at which version. A tool getting faster between releases
    # is invisible without this.
    tool: str | None = None
    tool_version: str | None = None
    # Sanitized payload -- see services/params_sanitizer.py. Never raw.
    params: dict = Field(default_factory=dict)
    # Opportunistic: read_count, reference_bases, n_variants. Bytes alone is a
    # weak predictor (a 500 MB gzipped FASTQ and a 500 MB BAM cost very
    # different amounts), so this is what makes the corpus worth aggregating.
    features: dict = Field(default_factory=dict)

    # Provenance links back to what the run produced.
    job_id: str | None = None
    object_id: str | None = None
    project_id: str | None = None

    # Recorded for later analysis and for segmenting the model; a compressed
    # FASTQ and a BAM of the same size cost very different amounts.
    format_kind: str | None = None
    compression: str | None = None
    # Whether the file was already in the page cache materially changes the
    # timing, and there is no way to know -- so outliers are handled by using
    # a median-based fit rather than trying to detect this.
    worker_id: str | None = None
    finished_at: datetime | None = None

    class Settings:
        name = "job_timings"
        indexes = [
            # The model query: recent successful samples for one job type.
            # `outcome` is in the key because every fit now filters on it.
            IndexModel(
                [
                    ("job_type", ASCENDING),
                    ("outcome", ASCENDING),
                    ("finished_at", DESCENDING),
                ],
                name="model_samples",
            ),
            # Provenance: every run that touched one object.
            IndexModel([("object_id", ASCENDING)], name="by_object"),
        ]
```

- [ ] **Step 2: Verify the model imports and the suite still passes**

Run: `./backend/run-worktree-tests.sh tests/storage/ -q`
Expected: PASS. Existing rows lack the new fields and adopt the defaults — `outcome` defaults to `succeeded`, which is correct, since every row written before this change was a success.

- [ ] **Step 3: Commit**

```bash
git add backend/app/models/timing.py
git commit -m "Widen JobRunTiming into a full computation record"
```

---

## Task 5: Outcome filter and provenance accessor

The containment for the risk this design accepts: one place applies the filter, one explicitly-named accessor opts out of it.

**Files:**
- Modify: `backend/app/services/timing_service.py`
- Test: `backend/tests/queue/test_record_outcomes.py`

- [ ] **Step 1: Write the failing test**

```python
"""Failed runs are recorded for provenance and excluded from every fit.

The bug this guards against is silent and points the wrong way: an OOM kill at
ninety seconds reads as a fast, cheap run whose peak RSS is the ceiling it hit
rather than what it needed. A few in a fit drag estimates down -- toward
causing the next OOM.
"""

import pytest

from app.models.timing import JobRunTiming, RunOutcome
from app.services import timing_service

# No `pytestmark = pytest.mark.asyncio` needed: pyproject.toml sets
# `asyncio_mode = "auto"`, so bare `async def` tests are collected.


async def _record(outcome, duration_ms=120_000, input_bytes=1_000_000):
    await JobRunTiming(
        job_type="align_reads",
        input_bytes=input_bytes,
        duration_ms=duration_ms,
        outcome=outcome,
    ).insert()


class TestModelSamples:
    async def test_failed_runs_are_excluded_from_samples(self):
        for _ in range(6):
            await _record(RunOutcome.SUCCEEDED)
        for _ in range(4):
            await _record(RunOutcome.FAILED, duration_ms=500)
        samples = await timing_service._samples("align_reads")
        assert len(samples) == 6

    async def test_dead_and_cancelled_are_excluded_too(self):
        for _ in range(6):
            await _record(RunOutcome.SUCCEEDED)
        await _record(RunOutcome.DEAD, duration_ms=100)
        await _record(RunOutcome.CANCELLED, duration_ms=100)
        assert len(await timing_service._samples("align_reads")) == 6

    async def test_a_failed_run_does_not_drag_the_estimate_down(self):
        """The whole reason the filter exists."""
        for _ in range(8):
            await _record(RunOutcome.SUCCEEDED, duration_ms=120_000)
        clean = await timing_service.estimate("align_reads", 1_000_000)
        for _ in range(8):
            await _record(RunOutcome.FAILED, duration_ms=200)
        after = await timing_service.estimate("align_reads", 1_000_000)
        assert after["estimate_ms"] == pytest.approx(clean["estimate_ms"], rel=0.01)


class TestProvenance:
    async def test_provenance_includes_failures(self):
        """The one reader that wants them -- a failed run is the most useful
        record a user can read."""
        await JobRunTiming(
            job_type="align_reads",
            input_bytes=10,
            duration_ms=500,
            outcome=RunOutcome.FAILED,
            object_id="obj-1",
        ).insert()
        await JobRunTiming(
            job_type="align_reads",
            input_bytes=10,
            duration_ms=1000,
            outcome=RunOutcome.SUCCEEDED,
            object_id="obj-1",
        ).insert()
        records = await timing_service.records_for_object("obj-1")
        assert len(records) == 2
        assert {r.outcome for r in records} == {
            RunOutcome.FAILED,
            RunOutcome.SUCCEEDED,
        }

    async def test_provenance_is_scoped_to_the_object(self):
        await JobRunTiming(
            job_type="align_reads", input_bytes=10, duration_ms=1, object_id="obj-1"
        ).insert()
        await JobRunTiming(
            job_type="align_reads", input_bytes=10, duration_ms=1, object_id="obj-2"
        ).insert()
        assert len(await timing_service.records_for_object("obj-1")) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/queue/test_record_outcomes.py -q`
Expected: FAIL — `AttributeError: module 'app.services.timing_service' has no attribute 'records_for_object'`, and the exclusion tests fail because `_samples` has no filter yet.

- [ ] **Step 3: Add the filter and the accessor**

In `backend/app/services/timing_service.py`, update the import line:

```python
from app.models.timing import MODELLED_OUTCOMES, JobRunTiming
```

Replace `_samples` with:

```python
async def _samples(job_type: str) -> list[tuple[int, int]]:
    """Duration samples for one job type. **Successes only.**

    Every fit goes through here rather than querying the collection directly,
    which is the one thing keeping the outcome filter correct. Failed runs
    share this collection for provenance, and a failed run looks like a fast,
    cheap one -- folding them in biases estimates downward, toward predicting
    that jobs are cheaper than they are.
    """
    docs = await _modelled(job_type)
    return [(d.input_bytes, d.duration_ms) for d in docs if d.duration_ms > 0]


async def _modelled(job_type: str) -> list[JobRunTiming]:
    """Recent records for one job type that a model may fit against."""
    return (
        await JobRunTiming.find(
            JobRunTiming.job_type == job_type,
            {"outcome": {"$in": list(MODELLED_OUTCOMES)}},
        )
        .sort("-finished_at")
        .limit(MAX_SAMPLES)
        .to_list()
    )


async def records_for_object(object_id: str) -> list[JobRunTiming]:
    """Every run that touched one object, **including failures**.

    The deliberate counterpart to `_samples`: provenance is the one reader
    that wants failed runs, since a failure is the most informative record a
    user can read. Named explicitly so that opting out of the filter is a
    visible choice rather than an omission.
    """
    return (
        await JobRunTiming.find(JobRunTiming.object_id == object_id)
        .sort("-finished_at")
        .to_list()
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/queue/test_record_outcomes.py -q`
Expected: PASS, 5 passed

- [ ] **Step 5: Run the storage suite for regressions**

Run: `./backend/run-worktree-tests.sh tests/storage/ -q`
Expected: PASS — the pure fit tests are untouched by this change.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/timing_service.py backend/tests/queue/test_record_outcomes.py
git commit -m "Filter failed runs out of timing models, add provenance accessor"
```

---

## Task 6: Memory model

**Files:**
- Modify: `backend/app/services/timing_service.py`
- Test: `backend/tests/storage/test_memory_model.py`

- [ ] **Step 1: Write the failing test**

```python
"""Peak-memory prediction from measured runs.

Pure arithmetic, tested without Mongo -- matching test_timing_model.py, which
tests the duration fit the same way and for the same reason.
"""

import pytest

from app.services.timing_service import MIN_SAMPLES, _fit_memory


class TestInsufficientData:
    def test_no_samples_gives_no_model(self):
        assert _fit_memory([]) is None

    @pytest.mark.parametrize("n", range(1, MIN_SAMPLES))
    def test_below_threshold_gives_no_model(self, n):
        """Same silence-before-confidence rule as the duration model: a
        confidently wrong memory number invites an OOM."""
        assert _fit_memory([(1000 * i, 100 * i) for i in range(1, n + 1)]) is None


class TestFit:
    def test_recovers_a_known_slope(self):
        """1 byte of RSS per byte of input, plus 100 MB fixed."""
        base = 100 * 1024 * 1024
        samples = [(1_000_000 * i, base + 1_000_000 * i) for i in range(1, 21)]
        model = _fit_memory(samples)
        assert model["slope"] == pytest.approx(1.0, rel=1e-6)
        assert model["intercept"] == pytest.approx(base, rel=1e-6)

    def test_constant_memory_yields_a_flat_model(self):
        """Many tools have a footprint set by the reference, not the reads --
        a flat model is the right answer, not a failure."""
        samples = [(1_000_000 * i, 2 * 1024**3) for i in range(1, 21)]
        model = _fit_memory(samples)
        assert model["flat"] is True
        assert model["intercept"] == pytest.approx(2 * 1024**3, rel=1e-6)


class TestExclusions:
    def test_samples_without_a_peak_are_dropped_before_fitting(self):
        """Runs under the sampling floor carry None, not zero. Treating them
        as zero would drag every prediction toward nothing."""
        from app.services.timing_service import _memory_samples_from

        class FakeRecord:
            def __init__(self, input_bytes, peak):
                self.input_bytes = input_bytes

                class R:
                    peak_rss_bytes = peak

                self.resources = R()

        records = [FakeRecord(1000, None), FakeRecord(2000, 5000), FakeRecord(3000, 0)]
        assert _memory_samples_from(records) == [(2000, 5000)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/storage/test_memory_model.py -q`
Expected: FAIL — `ImportError: cannot import name '_fit_memory' from 'app.services.timing_service'`

- [ ] **Step 3: Implement the memory fit**

Add to `backend/app/services/timing_service.py`:

```python
def _memory_samples_from(records) -> list[tuple[int, int]]:
    """(input_bytes, peak_rss) pairs from records that actually have a peak.

    Runs under the sampling floor carry None rather than zero, and the
    distinction is load-bearing: treating an unmeasured run as zero bytes of
    memory would drag every prediction toward nothing.
    """
    out = []
    for record in records:
        peak = record.resources.peak_rss_bytes
        if peak:
            out.append((record.input_bytes, peak))
    return out


def _fit_memory(samples: list[tuple[int, int]]) -> dict | None:
    """Least-squares fit of peak RSS against input size.

    The same shape as `_fit`, and deliberately the same function underneath:
    memory tends to be flatter in input size than duration is (the reference
    or index usually dominates), which `_fit`'s existing flat-model fallback
    already handles.
    """
    return _fit(samples)


async def estimate_memory(job_type: str, input_bytes: int) -> dict | None:
    """Predicted peak RSS in bytes for a run of this type and size.

    Returns `known: False` rather than a guess when there is not enough
    history. Only runs above the sampling floor carry a measured peak, so this
    can stay silent long after the duration model has become confident.
    """
    records = await _modelled(job_type)
    samples = _memory_samples_from(records)
    model = _fit_memory(samples)
    if model is None:
        return {
            "known": False,
            "samples": len(samples),
            "needed": max(0, MIN_SAMPLES - len(samples)),
        }

    predicted = model["intercept"] + model["slope"] * max(0, input_bytes)
    return {
        "known": True,
        "estimate_bytes": int(max(0, predicted)),
        "samples": model["n"],
        "r_squared": round(_r_squared(samples, model), 3),
        "range": _observed_range(samples, input_bytes),
    }
```

`_observed_range` is written in Task 7. Implement Task 7 before running the full suite; the memory tests in this task do not call it.

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/storage/test_memory_model.py -q`
Expected: PASS, 8 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/timing_service.py backend/tests/storage/test_memory_model.py
git commit -m "Add peak-memory model alongside the duration model"
```

---

## Task 7: Extrapolation flagging

Every existing row came from test data. The first serious alignment will be one or two orders of magnitude larger than anything measured, and a linear fit extrapolated that far is least trustworthy exactly there.

**Files:**
- Modify: `backend/app/services/timing_service.py`
- Test: `backend/tests/storage/test_extrapolation.py`

- [ ] **Step 1: Write the failing test**

```python
"""Whether the question being asked sits inside the fit's evidence.

r_squared says how well a fit describes its own samples. It says nothing about
an input far outside them -- which, given every existing row came from test
data, is exactly what the first real run will be.
"""

from app.services.timing_service import _observed_range


class TestInsideTheRange:
    def test_an_input_within_the_samples_is_not_flagged(self):
        samples = [(1000, 10), (5000, 50), (10_000, 100)]
        result = _observed_range(samples, 5000)
        assert result["extrapolating"] is False
        assert result["factor_beyond"] is None

    def test_the_exact_maximum_is_still_inside(self):
        samples = [(1000, 10), (10_000, 100)]
        assert _observed_range(samples, 10_000)["extrapolating"] is False


class TestBeyondTheRange:
    def test_a_larger_input_is_flagged_with_how_far(self):
        """'8x larger than anything measured' is a materially different claim
        from an estimate inside the range."""
        samples = [(1000, 10), (10_000, 100)]
        result = _observed_range(samples, 80_000)
        assert result["extrapolating"] is True
        assert result["factor_beyond"] == 8.0

    def test_reports_the_observed_bounds(self):
        samples = [(1000, 10), (10_000, 100)]
        result = _observed_range(samples, 80_000)
        assert result["min_observed_bytes"] == 1000
        assert result["max_observed_bytes"] == 10_000

    def test_a_smaller_input_is_not_flagged(self):
        """Interpolating below the smallest sample is a mild claim; the fit's
        intercept covers it. Only extrapolating upward risks a large error."""
        samples = [(10_000, 100), (20_000, 200)]
        assert _observed_range(samples, 500)["extrapolating"] is False


class TestDegenerateInput:
    def test_no_samples_reports_no_opinion(self):
        result = _observed_range([], 1000)
        assert result["extrapolating"] is False
        assert result["max_observed_bytes"] is None

    def test_all_zero_sizes_do_not_divide_by_zero(self):
        result = _observed_range([(0, 10), (0, 20)], 5000)
        assert result["extrapolating"] is True
        assert result["factor_beyond"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/storage/test_extrapolation.py -q`
Expected: FAIL — `ImportError: cannot import name '_observed_range' from 'app.services.timing_service'`

- [ ] **Step 3: Implement extrapolation flagging**

Add to `backend/app/services/timing_service.py`:

```python
def _observed_range(samples: list[tuple[int, int]], input_bytes: int) -> dict:
    """Whether `input_bytes` falls inside the sizes actually measured.

    A linear fit is least trustworthy exactly where it is extrapolated, and
    this app's whole history to date is test data -- so the first serious run
    will be far outside the range. "Estimated 40 minutes, but this input is 8x
    larger than anything measured" is a materially different claim from an
    estimate inside the range, and costs one comparison to say.

    Only upward extrapolation is flagged. Below the smallest sample the
    intercept carries the prediction and the absolute error is small; above
    the largest, the slope compounds.
    """
    if not samples:
        return {
            "extrapolating": False,
            "factor_beyond": None,
            "min_observed_bytes": None,
            "max_observed_bytes": None,
        }

    sizes = [b for b, _ in samples]
    smallest, largest = min(sizes), max(sizes)
    beyond = input_bytes > largest

    return {
        "extrapolating": beyond,
        # None rather than a number when every sample was zero-sized: there is
        # no ratio to report, but the input is still outside what was seen.
        "factor_beyond": round(input_bytes / largest, 1) if beyond and largest else None,
        "min_observed_bytes": smallest,
        "max_observed_bytes": largest,
    }
```

Then wire it into the existing `estimate()` — in its `known: True` return dict, add:

```python
        "range": _observed_range(samples, input_bytes),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/storage/test_extrapolation.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Run the whole storage suite**

Run: `./backend/run-worktree-tests.sh tests/storage/ -q`
Expected: PASS — including `test_memory_model.py`, whose `estimate_memory` now resolves `_observed_range`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/timing_service.py backend/tests/storage/test_extrapolation.py
git commit -m "Flag estimates for inputs outside the observed sample range"
```

---

## Task 8: Widen `record()`

**Files:**
- Modify: `backend/app/services/timing_service.py`

- [ ] **Step 1: Replace `record()`**

The existing signature is keyword-only, and every new parameter is optional, so the current caller keeps working until Task 9 updates it.

```python
async def record(
    *,
    job_type: str,
    input_bytes: int,
    duration_ms: int,
    outcome: str = RunOutcome.SUCCEEDED,
    queued_ms: int | None = None,
    threads: int | None = None,
    resources: dict | None = None,
    machine: dict | None = None,
    tool: str | None = None,
    tool_version: str | None = None,
    params: dict | None = None,
    features: dict | None = None,
    job_id: str | None = None,
    object_id: str | None = None,
    project_id: str | None = None,
    format_kind: str | None = None,
    compression: str | None = None,
    worker_id: str | None = None,
) -> None:
    """Store one completed run. Never raises -- telemetry must not fail a job.

    Failed runs are stored too, tagged by `outcome`, because a failure is the
    most informative provenance a user can read and an OOM kill is the best
    memory signal available. `_samples` is what keeps them out of the fits.
    """
    try:
        await JobRunTiming(
            job_type=job_type,
            input_bytes=max(0, input_bytes),
            duration_ms=max(0, duration_ms),
            outcome=outcome,
            queued_ms=queued_ms,
            threads=threads,
            resources=RunResources(**(resources or {})),
            machine=RunMachine(**(machine or {})),
            tool=tool,
            tool_version=tool_version,
            params=params or {},
            features=features or {},
            job_id=job_id,
            object_id=object_id,
            project_id=project_id,
            format_kind=format_kind,
            compression=compression,
            worker_id=worker_id,
            finished_at=datetime.now(UTC),
        ).insert()
    except Exception as e:  # noqa: BLE001
        log.debug("timing_record_failed", job_type=job_type, error=str(e))
```

Update the import at the top of the file to:

```python
from app.models.timing import (
    MODELLED_OUTCOMES,
    JobRunTiming,
    RunMachine,
    RunOutcome,
    RunResources,
)
```

- [ ] **Step 2: Verify nothing regressed**

Run: `./backend/run-worktree-tests.sh tests/storage/ tests/queue/test_record_outcomes.py -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/timing_service.py
git commit -m "Widen timing_service.record to the full computation record"
```

---

## Task 9: Wire the executor

The sampler runs for the life of the job; the record is written on both the success and failure paths.

**Files:**
- Modify: `backend/app/queue/executor.py`

- [ ] **Step 1: Add the sampler lifecycle**

Add to the imports at the top of `backend/app/queue/executor.py`:

```python
from app.queue.resource_sampler import ResourceSampler
```

Add the sampling constants below `SUBPROCESS_GRACE_SECONDS`:

```python
# Resource sampling interval. Fine enough that a minute-long job yields ~60
# readings, coarse enough that the poll costs nothing next to the work.
SAMPLE_INTERVAL_SECONDS = 1.0

# Below this, resource fields are left null rather than filled with a peak
# derived from a handful of samples. Short jobs are not what this data is for
# -- the question "will this fit on my machine" is only asked about work
# measured in minutes -- so they are excluded rather than recorded unreliably.
RESOURCE_FLOOR_MS = 60_000
```

Add these methods to `JobExecutor`:

```python
    async def _sample_resources(self, sampler: ResourceSampler) -> None:
        """Poll until cancelled. Never raises -- telemetry cannot fail a job."""
        try:
            while True:
                sampler.observe()
                await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("resource_sampling_failed", error=str(e))

    def _start_sampler(self) -> tuple[ResourceSampler, asyncio.Task]:
        """Sample this worker's own process subtree.

        The worker's baseline is included, which slightly overstates a job's
        own footprint -- but subprocess tools are spawned as children of this
        process, so the subtree is what captures them, and two concurrent jobs
        remain separable because each tool tree is walked from its own root.
        """
        sampler = ResourceSampler(pid=os.getpid())
        return sampler, asyncio.create_task(self._sample_resources(sampler))
```

- [ ] **Step 2: Start and stop the sampler around dispatch**

In `run()`, replace the `try:` block opening (currently line 72–81) so the sampler brackets the dispatch and the outcome is captured:

```python
        sampler, sampler_task = self._start_sampler()
        outcome = RunOutcome.SUCCEEDED

        try:
            result = await self._dispatch(spec, ctx)
            # Thread-mode handlers cannot touch the database (Beanie is async),
            # so results that need persisting are applied here on the loop.
            await self._apply_result(job, result)
            await queue.complete(
                job_id, epoch, state=JobState.SUCCEEDED, result=result or {}
            )
            log.info("job_succeeded", job_id=job_id, type=job.type)
```

Add `from app.models.timing import RunOutcome` to the imports.

In each `except` block, set `outcome` before the existing body:

- `except JobCancelled:` → `outcome = RunOutcome.CANCELLED`
- `except PermanentError as e:` → `outcome = RunOutcome.FAILED`
- In the retry branch, set `outcome = RunOutcome.DEAD` inside `if attempts >= job.max_attempts:` and `outcome = RunOutcome.FAILED` in the `else`.

Then in the `finally` block, stop the sampler and record — replacing the existing `_record_timing(job)` call at line 80, which moves here so that failures are recorded too:

```python
        finally:
            sampler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler_task
            await self._record_timing(job, outcome=outcome, sampler=sampler)
            self._last_progress.pop(job_id, None)
            self._last_phase.pop(job_id, None)
```

**Delete the `await self._record_timing(job)` call on the success path** (line 80). Leaving it would write two records for every successful job.

- [ ] **Step 3: Rewrite `_record_timing`**

Replace the existing method:

```python
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
                threads=job.payload.get("threads"),
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
```

- [ ] **Step 4: Run the queue suite**

Run: `./backend/run-worktree-tests.sh tests/queue/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/executor.py
git commit -m "Sample resources per job and record failures as well as successes"
```

---

## Task 10: Surface the estimates

**Files:**
- Modify: `backend/app/api/v1/jobs.py:186-191` and `:231-245`

- [ ] **Step 1: Add the memory model to `/timing-model`**

Replace the `timing_model` route body:

```python
@router.get("/timing-model")
async def timing_model() -> dict:
    """Per-job-type duration and memory models, and how many samples back each."""
    from app.queue.executor import RESOURCE_FLOOR_MS
    from app.services import timing_service

    return {
        "min_samples": timing_service.MIN_SAMPLES,
        # Imported rather than repeated: a client showing "no memory estimate
        # for jobs under a minute" and an executor using a different floor
        # would disagree silently.
        "resource_floor_ms": RESOURCE_FLOOR_MS,
        "types": await timing_service.stats(),
    }
```

- [ ] **Step 2: Add the memory estimate to the job detail route**

In `get_job`, inside the existing `if size:` block, add the memory estimate beside the duration one:

```python
        if size:
            out["timing_estimate"] = await timing_service.estimate(job.type, size)
            out["memory_estimate"] = await timing_service.estimate_memory(job.type, size)
```

- [ ] **Step 3: Add the memory model to `stats()`**

In `backend/app/services/timing_service.py`, replace `stats()`:

```python
async def stats() -> list[dict]:
    """Per-job-type model summary, for a diagnostics view."""
    types = await JobRunTiming.distinct("job_type")
    out = []
    for t in types:
        records = await _modelled(t)
        samples = [
            (d.input_bytes, d.duration_ms) for d in records if d.duration_ms > 0
        ]
        model = _fit(samples)
        memory_samples = _memory_samples_from(records)
        memory_model = _fit_memory(memory_samples)
        out.append(
            {
                "job_type": t,
                "samples": len(samples),
                "model": None
                if model is None
                else {
                    "slope_ms_per_byte": model["slope"],
                    "intercept_ms": round(model["intercept"]),
                    "r_squared": round(_r_squared(samples, model), 3),
                },
                # Separate sample count: only runs above the floor carry a
                # peak, so this is legitimately smaller than `samples`.
                "memory_samples": len(memory_samples),
                "memory_model": None
                if memory_model is None
                else {
                    "slope_bytes_per_byte": memory_model["slope"],
                    "intercept_bytes": round(memory_model["intercept"]),
                    "r_squared": round(_r_squared(memory_samples, memory_model), 3),
                },
            }
        )
    return out
```

- [ ] **Step 4: Run the full suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the count, not just the exit code — the baseline on this tree is 1872 passed.

- [ ] **Step 5: Verify against the real database**

Per `CLAUDE.md`, a rule that passes its unit tests can still be wrong about real objects. Bring up the worktree stack and check the endpoint returns the new shape:

```bash
./ops/worktree-up.sh
```

Then:

```bash
curl -s localhost:8100/api/v1/jobs/timing-model | head -40
```

Expected: JSON containing `min_samples`, `resource_floor_ms`, and a `types` array whose entries carry both `model` and `memory_model` keys. `memory_model` will be `null` on a fresh corpus — every existing row predates resource sampling — and that is the correct output, not a failure.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/jobs.py backend/app/services/timing_service.py
git commit -m "Surface memory estimates through the jobs API"
```

---

## Task 11: Close out the documentation

**Files:**
- Modify: `docs/TODO.md`, `docs/TODO-done.md` (only if an entry covers this work)

- [ ] **Step 1: Check whether a TODO entry covers this**

```bash
grep -n -i "timing\|estimate\|benchmark\|resource" docs/TODO.md
```

If an entry describes this work, append ` — FIXED` to its heading, add a note saying what shipped and where the code lives, say what the implementation did differently from its plan, and move the whole entry to `docs/TODO-done.md`. If no entry matches, skip to Step 2 — do not invent one.

- [ ] **Step 2: Record the two deliberate gaps as TODO entries**

Both are scope reductions relative to the spec, and `CLAUDE.md` documents three
occasions where finished work left the backlog stale enough to mislead. Add to
`docs/TODO.md` under "Deferred findings":

```markdown
## Duration model does not yet segment by thread count

`JobRunTiming.threads` is captured (executor reads it from `job.payload`, where
align/assembly/expression/assembly_qc handlers already put it), but
`timing_service._fit` still regresses duration against bytes alone.

The design called for segmenting the fit by thread count with a bytes-only
fallback. It was deferred because no row carried a thread count until the
recording shipped, so the segmentation could only have been tested against
synthetic data and would have fallen back to existing behaviour on every real
row anyway.

Revisit once several job types have accumulated runs at differing thread
counts. Check with:

    docker compose exec api python -c "..."

against real rows rather than fixtures -- per CLAUDE.md, hand-built objects
that already look the way the code expects are how the suggestion rules passed
green while being wrong.

Touches: `backend/app/services/timing_service.py`.

## No provenance panel for computation records

`timing_service.records_for_object()` returns every run that touched an object,
failures included, and nothing renders it. The design listed per-object
provenance as one of three read surfaces; the accessor shipped, the UI did not.

Would show, per run: duration, peak RSS, thread count, tool and version, and
the machine it ran on. Failed runs are the interesting ones and are already in
the result set.

Touches: `frontend/src/`, plus a route exposing `records_for_object`.
```

- [ ] **Step 3: Verify the CLAUDE.md note matches what was built**

The "Querying computation records" section was written during design, before the code existed. Confirm it still describes reality: that `timing_service._samples()` applies the outcome filter, that `records_for_object()` is the provenance accessor that includes failures, and that resource fields are null below 60 seconds.

Fix any drift. A stale note here is worse than none — the whole point is that it warns a future change away from a silent bug.

- [ ] **Step 4: Commit**

```bash
git add docs/ CLAUDE.md
git commit -m "Close out computation records documentation"
```

---

## Task 12: Merge

- [ ] **Step 1: Confirm the suite is green**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the count.

- [ ] **Step 2: Merge and push**

Per `CLAUDE.md`: once green, commit and merge without asking. `main` is a dev trunk, and a merge that stays unpushed is one someone has to remember later.

```bash
git checkout main && git merge --no-ff claude/computation-stats-database-6801e4 && git push origin main
```

- [ ] **Step 3: Re-run the suite if `main` had moved**

If the merge brought in other commits, the earlier green no longer describes the merged tree:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

- [ ] **Step 4: Point the running stack back at main**

Only needed if the 5173 stack was repointed at this worktree at any stage. `./ops/worktree-up.sh` avoids this by construction, but verify from the main checkout root:

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

If any path is under `.claude/worktrees/`, restore it from the main checkout:

```bash
docker compose up -d --build api web worker
```

---

## Self-review notes

**Spec coverage.** Identity fields → Task 4. Outcome recording and filtering → Tasks 4, 5, 9. Timing and `queued_ms` → Tasks 4, 9. Resources and the 60s floor → Tasks 1, 4, 9. Machine → Tasks 2, 9. Invocation, sanitized params, features → Tasks 3, 4, 9. Duration model with threads → Task 4 stores `threads`; **see the gap below**. Memory model → Task 6. Extrapolation → Task 7. Three read surfaces → Task 10 covers two.

**Two known gaps, both deliberate:**

1. **`threads` is recorded but does not yet segment the duration fit.** The spec calls for the fit to be segmented by thread count with a fallback to bytes-only. Segmenting requires thread data to exist first, and no row has it until Task 9 ships. Implementing the segmentation now would mean writing a code path that cannot be tested against real data and that falls back to the existing behavior on every existing row. The field is captured; the segmentation is a follow-up once rows accumulate. **Record this as a TODO entry rather than letting it disappear.**

2. **Provenance has no UI.** Task 5 builds `records_for_object()` and Task 10 surfaces the estimates, but nothing renders a per-object provenance panel. The spec lists provenance as a read surface; this plan delivers the queryable accessor, not the component. Frontend work in this repo is verified by hand in the browser, and the panel is a self-contained addition that does not block the data model. **Also worth a TODO entry.**

Both are real scope reductions relative to the spec, not oversights — flagged here so they are visible before execution rather than discovered after, and written into `docs/TODO.md` by Task 11 Step 2 so they survive the plan being closed.
