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

import math
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

# Guards on trusting the measured model over the heuristic. Both are judgment
# calls rather than derived values -- the same honesty resource_estimator's
# module docstring practices about its own coefficients.
#
# A pure extrapolation check is not enough on its own. `_fit_memory` falls back
# to a flat model, and memory genuinely is flat in input size for many tools
# (the reference or index dominates). A flat model extrapolates fine: 14 GB
# regardless of input is still 14 GB at 10x the input. A model with a real
# slope, fit on five scattered test rows, does not -- and both report the same
# factor_beyond. r_squared is what separates them.
MAX_EXTRAPOLATION_FACTOR = 2.0
MIN_R_SQUARED = 0.5

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


async def records_for_object(
    object_id: str, *, limit: int | None = None
) -> list[JobRunTiming]:
    """Every run that touched one object, **including failures**.

    The deliberate counterpart to `_samples`: provenance is the one reader
    that wants failed runs, since a failure is the most informative record a
    user can read. Named explicitly so that opting out of the filter is a
    visible choice rather than an omission.

    `limit` defaults to unbounded so existing callers are unaffected; the
    per-object provenance route passes `limit + 1` and truncates, which is
    where its `has_more` flag comes from.
    """
    query = JobRunTiming.find(JobRunTiming.object_id == object_id).sort(
        "-finished_at"
    )
    if limit is not None:
        query = query.limit(limit)
    return await query.to_list()


async def runs_for_type(
    job_type: str, *, limit: int | None = None, offset: int = 0
) -> list[JobRunTiming]:
    """Recent runs of one job type, **including failures**.

    The read path behind the Metrics page's per-run tables, and the second
    explicitly-named opt-out of the outcome filter alongside
    `records_for_object`. It must not be built on `_modelled`: that filter
    exists so a failed run cannot bias a predictive fit, but a user reading
    "what has call_variants been doing" is owed the failures -- they are the
    most informative rows on the page. Naming it plainly is what keeps that a
    visible choice rather than an omission.

    Newest first, so a caller taking the first N gets the most recent N.
    """
    query = JobRunTiming.find(JobRunTiming.job_type == job_type).sort(
        "-finished_at"
    )
    if offset:
        query = query.skip(offset)
    if limit is not None:
        query = query.limit(limit)
    return await query.to_list()


async def recent_runs_by_type(*, limit: int = 5) -> dict[str, dict]:
    """The most recent `limit` runs of every job type, plus each type's total.

    One call rather than one per type: the Metrics page renders a table per
    job type, and a component fetching its own rows would turn a page load
    into N requests.

    `total` counts every recorded run of the type, failures included, so the
    UI can decide whether a "see more" link is warranted without a second
    round trip. It is deliberately the full history while `runs` is only the
    recent window -- the same split `metrics()` makes between its outcome
    counts and its summaries.
    """
    out: dict[str, dict] = {}
    for job_type in sorted(await JobRunTiming.distinct("job_type")):
        out[job_type] = {
            "runs": await runs_for_type(job_type, limit=limit),
            "total": await JobRunTiming.find(
                JobRunTiming.job_type == job_type
            ).count(),
        }
    return out


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


async def estimate(
    job_type: str, input_bytes: int, *, threads: int | None = None
) -> dict | None:
    """Predicted duration in ms for a run of this type and size.

    None means "not enough history" -- callers should show no estimate rather
    than guessing.

    `threads=None` (the default) is byte-only, identical to this function's
    behavior before segmentation existed. `threads=<int>` prefers a
    same-thread-count segment's fit when one has enough samples, falling back
    to the same pooled bytes-only fit `threads=None` would have used.
    """
    records = await _modelled(job_type)
    samples = _duration_samples_from(records)

    if threads is None:
        model = _fit(samples)
        answered_by = None
        scoring_samples = samples
    else:
        segments = _fit_segmented(records, _duration_samples_from)
        if threads in segments:
            model, answered_by = segments[threads], threads
            scoring_samples = _segment_samples(records, threads, _duration_samples_from)
        elif None in segments:
            model, answered_by = segments[None], None
            scoring_samples = samples
        else:
            model, answered_by = None, None
            scoring_samples = samples

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
        "r_squared": round(_r_squared(scoring_samples, model), 3),
        "throughput_mb_s": (
            round(1000 / (model["slope"] * 1024 * 1024), 1)
            if model["slope"] > 0
            else None
        ),
        "range": _observed_range(scoring_samples, input_bytes),
        "segment": {"threads": answered_by, "samples": model["n"]},
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


def _fit_segmented(
    records: list[JobRunTiming],
    sample_fn,
) -> dict[int | None, dict]:
    """One fit per thread count with `>= MIN_SAMPLES` samples, plus a
    bytes-only fallback fit over every record regardless of thread count,
    keyed `None`.

    `sample_fn` is `_duration_samples_from` or `_memory_samples_from` --
    whichever `(input_bytes, y)` extraction the caller wants segmented, so
    duration and memory share this grouping logic rather than each
    reimplementing it. Records with `threads is None` never form or join a
    per-thread group (an unknown thread count can't be assigned one) but do
    count toward the `None` fallback, matching today's un-segmented
    behavior exactly when nothing has a thread count yet.

    Reuses `MIN_SAMPLES`, the same threshold `_fit` already enforces --
    see the design doc's "Threshold" section for why a separate,
    segment-specific constant was not introduced.
    """
    by_threads: dict[int, list[JobRunTiming]] = {}
    for record in records:
        if record.threads is not None:
            by_threads.setdefault(record.threads, []).append(record)

    out: dict[int | None, dict] = {}
    for threads, group in by_threads.items():
        samples = sample_fn(group)
        if len(samples) >= MIN_SAMPLES:
            model = _fit(samples)
            if model is not None:
                out[threads] = model

    fallback = _fit(sample_fn(records))
    if fallback is not None:
        out[None] = fallback

    return out


def _segment_samples(
    records: list[JobRunTiming], threads: int, sample_fn
) -> list[tuple[int, int]]:
    """The `(bytes, y)` samples for one thread count's own records --
    what a segment's `r_squared`/`range` must be scored against, never the
    pooled set. Split out so `estimate()`, `estimate_memory()`, and `stats()`
    can't independently drift on this filter the way they did before the
    r_squared scoring bug (see the design doc's "Threshold" section and the
    fix in this feature's git history) -- one function, one place to get it
    right.
    """
    return sample_fn([r for r in records if r.threads == threads])


async def estimate_memory(
    job_type: str, input_bytes: int, *, threads: int | None = None
) -> dict | None:
    """Predicted peak RSS in bytes for a run of this type and size.

    **Modelled outcomes only** -- reads via `_modelled()`, the same
    outcome-filtered accessor `_samples()` uses, so a failed/OOM-killed run's
    peak RSS never enters the fit (that peak is the ceiling the run hit, not
    what it needed, and folding it in would bias predictions toward the exact
    number that caused the OOM).

    Returns `known: False` rather than a guess when there is not enough
    history. Only runs above the sampling floor carry a measured peak, so this
    can stay silent long after the duration model has become confident.

    `threads` behaves exactly as it does in `estimate()` -- see that
    docstring.
    """
    records = await _modelled(job_type)
    samples = _memory_samples_from(records)

    if threads is None:
        model = _fit_memory(samples)
        answered_by = None
        scoring_samples = samples
    else:
        segments = _fit_segmented(records, _memory_samples_from)
        if threads in segments:
            model, answered_by = segments[threads], threads
            scoring_samples = _segment_samples(records, threads, _memory_samples_from)
        elif None in segments:
            model, answered_by = segments[None], None
            scoring_samples = samples
        else:
            model, answered_by = None, None
            scoring_samples = samples

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
        "r_squared": round(_r_squared(scoring_samples, model), 3),
        "range": _observed_range(scoring_samples, input_bytes),
        "segment": {"threads": answered_by, "samples": model["n"]},
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

        thread_counts = sorted(
            {r.threads for r in records if r.threads is not None}
        )
        duration_segments = _fit_segmented(records, _duration_samples_from)
        segments = [
            {
                "threads": threads,
                "samples": duration_segments[threads]["n"],
                "model": {
                    "slope_ms_per_byte": duration_segments[threads]["slope"],
                    "intercept_ms": round(duration_segments[threads]["intercept"]),
                    "r_squared": round(
                        _r_squared(
                            _segment_samples(records, threads, _duration_samples_from),
                            duration_segments[threads],
                        ),
                        3,
                    ),
                },
            }
            for threads in thread_counts
            if threads in duration_segments
        ]

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
                # Per-thread-count duration fits that qualified (>=
                # MIN_SAMPLES same-thread rows), for the diagnostics view to
                # show what's actually segmenting versus falling back. Empty
                # until real runs at varying thread counts accumulate -- see
                # docs/superpowers/specs/2026-08-08-thread-count-segmentation-design.md.
                "segments": segments,
            }
        )
    return out


def _percentile(values: list[int], pct: float) -> int | None:
    """Nearest-rank percentile of a list of measurements.

    `None` when there are none: the metrics view follows the same convention
    as the models -- an unmeasured run is not a run that measured zero, and a
    column with no data shows nothing rather than 0.
    """
    if not values:
        return None
    ordered = sorted(values)
    # Nearest-rank: the rank is ceil(pct/100 * N), so the median of an
    # even-sized sample is the lower middle. Determinism over convention.
    rank = math.ceil((pct / 100) * len(ordered))
    idx = max(0, min(len(ordered) - 1, rank - 1))
    return ordered[idx]


def _summary(values: list[int]) -> dict:
    """median/p90 over a list of measurements; both null when empty."""
    return {
        "median": _percentile(values, 50),
        "p90": _percentile(values, 90),
    }


def _numeric_features(records: list[JobRunTiming], key: str) -> list[int]:
    """Positive numeric values of one opportunistic feature across records.

    Feature values are untyped at the schema level (a pipeline records what
    it can), so each is checked rather than trusted.
    """
    out = []
    for record in records:
        value = record.features.get(key)
        if isinstance(value, int) and value > 0:
            out.append(value)
    return out


def _tool_counts(records: list[JobRunTiming]) -> list[dict]:
    """Which binaries these runs used, most-used first."""
    counts: dict[tuple[str | None, str | None], int] = {}
    for record in records:
        key = (record.tool, record.tool_version)
        counts[key] = counts.get(key, 0) + 1
    return [
        {"name": name, "version": version, "runs": n}
        for (name, version), n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


async def _outcome_counts() -> dict[str, dict[str, int]]:
    """Every recorded run, grouped by job type then outcome.

    Deliberately *not* read through `_modelled`: counts are diagnostics and
    never model input, and a page that shows how often runs fail needs the
    failures. This mirrors `records_for_object`'s "failures on purpose"
    stance -- the asymmetry is the point. No fit ever sees these.
    """
    # The document’s own bound collection, not get_db(): get_db() reads the
    # app-level client, which tests deliberately never initialize (they bind
    # their own client to a private database instead), while this collection
    # is exactly the store `_modelled` queries -- so an outcome count and a
    # duration summary can never disagree about which database they read.
    # `$ifNull` is not defensive padding: the real collection holds rows
    # recorded before the outcome field existed (2026-08-03), which carry no
    # `outcome` at all. Per the computation-records design, those were
    # recorded on the success path and nowhere else, so they count as
    # succeeded -- not as a fourth, mysterious outcome bucket.
    pipeline = [
        {
            "$group": {
                "_id": {
                    "job_type": "$job_type",
                    "outcome": {"$ifNull": ["$outcome", "succeeded"]},
                },
                "n": {"$sum": 1},
            }
        }
    ]
    out: dict[str, dict[str, int]] = {}
    async for row in await JobRunTiming.get_pymongo_collection().aggregate(pipeline):
        key = row["_id"]
        out.setdefault(key["job_type"], {})[key["outcome"]] = row["n"]
    return out


async def metrics() -> dict:
    """Aggregated computation cost, for the Reference → Metrics page.

    Diagnostics, not model input. Every "how long / how much memory / how
    big" number here is a plain percentile over successful runs, read through
    the same `_modelled` accessor the predictive models use, so a failure can
    never leak into a summary that reads like a model's answer. Outcome counts
    are the one deliberately separate piece: they cover *every* recorded run
    (failures included -- the most informative record a user can read), via
    `_outcome_counts`, and the two sources are never mixed.

    `_modelled` caps at MAX_SAMPLES recent runs per type, so the percentiles
    describe the recent window while `outcomes` describes the whole history.
    """
    counts = await _outcome_counts()
    totals: dict[str, int] = {}
    for by_outcome in counts.values():
        for outcome, n in by_outcome.items():
            totals[outcome] = totals.get(outcome, 0) + n

    types = await JobRunTiming.distinct("job_type")
    out = []
    for job_type in sorted(types):
        records = await _modelled(job_type)
        durations = [r.duration_ms for r in records if r.duration_ms > 0]
        inputs = [r.input_bytes for r in records if r.input_bytes > 0]
        memory = [
            r.resources.peak_rss_bytes for r in records if r.resources.peak_rss_bytes
        ]
        out.append(
            {
                "job_type": job_type,
                "outcomes": counts.get(job_type, {}),
                "duration_ms": _summary(durations),
                "input_bytes": _summary(inputs),
                "peak_rss_bytes": _summary(memory),
                "read_count": _summary(_numeric_features(records, "read_count")),
                "tools": _tool_counts(records),
            }
        )
    return {"totals": totals, "types": out}
