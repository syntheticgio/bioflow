"""Duration prediction from recorded run history.

The model is deliberately simple: a least-squares fit of duration against
input size, per job type. Ingest cost really is close to linear in bytes
(hashing dominates and runs at mount speed), so a straight line captures most
of it and nothing more elaborate is justified by the sample sizes involved.

Two properties matter more than accuracy:

  * **Silence before confidence.** With fewer than MIN_SAMPLES runs there is no
    estimate at all. A progress bar that is confidently wrong is worse than an
    honest spinner.
  * **Outlier resistance.** Page-cache hits, a busy governor, or a competing
    job make individual runs wildly unrepresentative. Samples beyond 3x the
    median are dropped before fitting.
"""

from datetime import UTC, datetime

from app.logging import get_logger
from app.models.timing import MODELLED_OUTCOMES, JobRunTiming

log = get_logger(__name__)

# Below this, report no estimate. Five points is the minimum at which a slope
# means anything; the UI says how many more are needed.
MIN_SAMPLES = 5
# Only recent runs: hardware and code both change over time.
MAX_SAMPLES = 200
OUTLIER_FACTOR = 3.0


async def record(
    *,
    job_type: str,
    input_bytes: int,
    duration_ms: int,
    format_kind: str | None = None,
    compression: str | None = None,
    worker_id: str | None = None,
) -> None:
    """Store one completed run. Never raises -- telemetry must not fail a job."""
    try:
        await JobRunTiming(
            job_type=job_type,
            input_bytes=max(0, input_bytes),
            duration_ms=max(0, duration_ms),
            format_kind=format_kind,
            compression=compression,
            worker_id=worker_id,
            finished_at=datetime.now(UTC),
        ).insert()
    except Exception as e:  # noqa: BLE001
        log.debug("timing_record_failed", job_type=job_type, error=str(e))


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


def _fit(samples: list[tuple[int, int]]) -> dict | None:
    """Least-squares fit of duration = intercept + slope * bytes.

    Returns None when the data cannot support a fit.
    """
    if len(samples) < MIN_SAMPLES:
        return None

    # Drop outliers first: one cached run at 10x speed would drag the slope
    # far more than it deserves.
    durations = sorted(d for _, d in samples)
    median = durations[len(durations) // 2]
    if median > 0:
        samples = [
            (b, d)
            for b, d in samples
            if d <= median * OUTLIER_FACTOR and d >= median / OUTLIER_FACTOR
        ]
    if len(samples) < MIN_SAMPLES:
        return None

    n = len(samples)
    sum_x = sum(b for b, _ in samples)
    sum_y = sum(d for _, d in samples)
    sum_xx = sum(b * b for b, _ in samples)
    sum_xy = sum(b * d for b, d in samples)

    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        # Every sample is the same size, so no slope is derivable -- but the
        # mean is still a perfectly good estimate for that size.
        mean = sum_y / n
        return {"slope": 0.0, "intercept": mean, "n": n, "flat": True}

    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n

    # A negative slope means bigger files finish faster, which is noise rather
    # than signal. Fall back to the mean.
    if slope <= 0:
        return {"slope": 0.0, "intercept": sum_y / n, "n": n, "flat": True}

    return {"slope": slope, "intercept": max(0.0, intercept), "n": n, "flat": False}


def _r_squared(samples: list[tuple[int, int]], model: dict) -> float:
    """How much of the variance the fit explains -- surfaced so a poor model
    can be shown as a rough estimate rather than a confident one."""
    if not samples:
        return 0.0
    mean_y = sum(d for _, d in samples) / len(samples)
    ss_tot = sum((d - mean_y) ** 2 for _, d in samples)
    ss_res = sum(
        (d - (model["intercept"] + model["slope"] * b)) ** 2 for b, d in samples
    )
    if ss_tot == 0:
        return 1.0
    return max(0.0, min(1.0, 1 - ss_res / ss_tot))


async def estimate(job_type: str, input_bytes: int) -> dict | None:
    """Predicted duration in ms for a run of this type and size.

    None means "not enough history" -- callers should show no estimate rather
    than guessing.
    """
    samples = await _samples(job_type)
    model = _fit(samples)
    if model is None:
        return {
            "known": False,
            "samples": len(samples),
            "needed": max(0, MIN_SAMPLES - len(samples)),
        }

    predicted = model["intercept"] + model["slope"] * max(0, input_bytes)
    return {
        "known": True,
        "estimate_ms": int(max(100, predicted)),
        "samples": model["n"],
        "r_squared": round(_r_squared(samples, model), 3),
        "throughput_mb_s": (
            round(1000 / (model["slope"] * 1024 * 1024), 1)
            if model["slope"] > 0
            else None
        ),
    }


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

    **Modelled outcomes only** -- reads via `_modelled()`, the same
    outcome-filtered accessor `_samples()` uses, so a failed/OOM-killed run's
    peak RSS never enters the fit (that peak is the ceiling the run hit, not
    what it needed, and folding it in would bias predictions toward the exact
    number that caused the OOM).

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
    }


async def stats() -> list[dict]:
    """Per-job-type model summary, for a diagnostics view."""
    types = await JobRunTiming.distinct("job_type")
    out = []
    for t in types:
        samples = await _samples(t)
        model = _fit(samples)
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
            }
        )
    return out
