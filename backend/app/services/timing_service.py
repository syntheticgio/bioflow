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
from app.models.timing import (
    MODELLED_OUTCOMES,
    JobRunTiming,
    RunMachine,
    RunOutcome,
    RunResources,
)

log = get_logger(__name__)

# Below this, report no estimate. Five points is the minimum at which a slope
# means anything; the UI says how many more are needed.
MIN_SAMPLES = 5
# Only recent runs: hardware and code both change over time.
MAX_SAMPLES = 200
OUTLIER_FACTOR = 3.0

# Below this fraction complete, `elapsed / pct` is not trusted for an ETA.
# The first percent of a run is usually its least representative stretch --
# process startup, index loading -- so at pct=0.01 the extrapolation
# multiplies elapsed time by a hundred. Below the floor, eta_seconds falls
# back to the prior-runs model instead.
ETA_PCT_FLOOR = 0.05

# Same ceiling TrimProgress/AlignProgress use for a measured-but-unverifiable
# fraction, applied here to a modelled one: an estimate must never claim
# completion, because pct_estimated's whole purpose is being visibly distinct
# from a real 100%.
MAX_ESTIMATED_PCT = 0.95


def eta_seconds(*, pct: float | None, elapsed_s: float, model_ms: int | None) -> float | None:
    """Seconds remaining, derived fresh on every call and never persisted.

    Two estimators, chosen per call rather than picked once: `elapsed / pct`
    self-corrects as a run proceeds but is only trustworthy above
    `ETA_PCT_FLOOR`; the prior-runs duration model (`estimate()` above) is
    available before any progress exists but is blind to how this particular
    run is actually going. Extrapolation wins whenever it applies -- the run's
    own progress is a better signal than history the moment there is enough
    of it to trust. Returns None when neither applies, which is the honest
    answer for a phase-only job with no history yet.

    A stored ETA would be wrong by exactly the time since it was stored, so
    this takes plain numbers and is meant to be called at read/emit time, not
    written to the job document.
    """
    if pct is not None and pct >= ETA_PCT_FLOOR:
        total = elapsed_s / pct
        return max(0.0, total - elapsed_s)
    if model_ms is not None:
        remaining = model_ms / 1000 - elapsed_s
        return max(0.0, remaining)
    return None


def pct_estimated(*, pct: float | None, elapsed_s: float, model_ms: int | None) -> float | None:
    """A modelled fraction complete, for a job with no measured `pct` at all.

    Deliberately a different field from `JobProgress.pct`, never persisted,
    and never fed back into `job_timings`: `pct` means "measured, or
    explicitly unknown", and every consumer of that collection -- the
    duration model itself, per-object provenance -- must never see a
    fabricated number written back in as if it were real. This is computed
    fresh at read/emit time exactly like `eta_seconds`, for the same reason:
    a stored value would be wrong by the time since it was stored.

    Returns None whenever a real `pct` already exists (a parser that later
    learns to measure something makes this yield to it with no caller
    change) or when there is no history yet (`model_ms` is None below
    `MIN_SAMPLES`) -- both cases where the bar should stay indeterminate
    exactly as it does today, not show a number nothing backs.

    Once elapsed time passes the model's prediction, this returns
    `MAX_ESTIMATED_PCT` rather than a fraction that keeps climbing toward or
    past 1.0. A bar parked at 99% is indistinguishable from a bar that has
    quietly stalled -- the exact ambiguity nullable `pct` exists to avoid --
    so the caller is expected to pair a pinned `MAX_ESTIMATED_PCT` with a
    "longer than expected" label rather than let it read as near-complete.
    """
    if pct is not None or model_ms is None:
        return None
    predicted_s = model_ms / 1000
    if predicted_s <= 0:
        return MAX_ESTIMATED_PCT
    return min(elapsed_s / predicted_s, MAX_ESTIMATED_PCT)


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


async def _samples(job_type: str) -> list[tuple[int, int]]:
    """Duration samples for one job type. **Successes only.**

    Every fit goes through here rather than querying the collection directly,
    which is the one thing keeping the outcome filter correct. Failed runs
    share this collection for provenance, and a failed run looks like a fast,
    cheap one -- folding them in biases estimates downward, toward predicting
    that jobs are cheaper than they are.
    """
    docs = await _modelled(job_type)
    return _duration_samples_from(docs)


def _duration_samples_from(records: list[JobRunTiming]) -> list[tuple[int, int]]:
    """(input_bytes, duration_ms) pairs from records with a real duration.

    Shared by `_samples` and `stats` so the two never drift: a zero-duration
    row (a schedule tick, not real work) is excluded the same way in both.
    """
    return [(d.input_bytes, d.duration_ms) for d in records if d.duration_ms > 0]


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
        "range": _observed_range(samples, input_bytes),
    }


def _memory_samples_from(records: list[JobRunTiming]) -> list[tuple[int, int]]:
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
        "range": _observed_range(samples, input_bytes),
    }


async def stats() -> list[dict]:
    """Per-job-type model summary, for a diagnostics view."""
    types = await JobRunTiming.distinct("job_type")
    out = []
    for t in types:
        records = await _modelled(t)
        samples = _duration_samples_from(records)
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
