"""Recorded job durations, used to predict how long a run will take.

There is no honest way to compute an ingest's percentage complete a priori:
the phases have wildly different throughput (hashing is I/O-bound at mount
speed, header parsing is nearly instant, composition sampling is CPU-bound and
capped), so a byte-progress bar would sprint to 90% and then sit there.

Instead we record what each run actually cost and fit a simple model against
it. Until enough samples exist the UI shows no estimate at all -- a wrong
progress bar is worse than none.
"""

from datetime import datetime

from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models.base import TimestampedDocument


class JobRunTiming(TimestampedDocument):
    """One completed run of one job type."""

    job_type: str
    # The predictor variable. Bytes is the only input known before the work
    # starts, which is what makes it usable for a forecast.
    input_bytes: int
    duration_ms: int

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
            # The model query: recent samples for one job type.
            IndexModel(
                [("job_type", ASCENDING), ("finished_at", DESCENDING)],
                name="model_samples",
            ),
        ]
