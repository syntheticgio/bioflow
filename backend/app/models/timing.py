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
    """Why a run stopped. Not a StrEnum: these mirror a subset of JobState's
    values, and staying a plain class keeps this schema's outcome vocabulary
    decoupled from job lifecycle semantics -- the two are free to diverge
    (e.g. this could later distinguish an OOM kill from a plain failure)
    without touching JobState."""

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
