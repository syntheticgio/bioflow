"""The worker's liveness signal, and the probe that reads it.

Mongo, Redis and api all declare healthchecks; the worker -- the service that
actually runs the jobs -- did not, so it reported "Up" while wedged and nothing
noticed. The launcher's health probe reads compose healthchecks, so a wedged
worker was invisible there too (#878).

A file rather than a port or a Redis key, for three reasons:

- The worker serves nothing, so there is no endpoint to probe, and adding an
  HTTP server to a queue consumer is a lot of surface for one boolean.
- A Redis-backed check would report unhealthy whenever *Redis* was unreachable.
  That is a real problem, but it is Redis's problem, and Redis already has its
  own healthcheck. A worker that has briefly lost Redis is alive and will
  recover; conflating the two would restart a healthy worker for someone
  else's outage.
- The file lives under /tmp inside the container, so it is per-container and
  cannot be mistaken for a sibling's, and it costs one utime() per beat.

The freshness threshold is a multiple of the heartbeat interval rather than a
fixed number of seconds, because the interval is derived from
`lease_ttl_seconds` and a deployment that raises that would otherwise start
failing its own healthcheck. `_heartbeat_loop` touches the file *before* it
does any work, so a busy worker running a multi-hour job stays healthy: the
heartbeat is independent of job execution.
"""

import time
from pathlib import Path

from app.config import settings

LIVENESS_PATH = Path("/tmp/bioflow-worker-alive")  # noqa: S108 - per-container

# How many missed beats before the worker is considered wedged. Three rather
# than one: a single slow beat under load is not a fault, and a restart is a
# heavier remedy than waiting one more interval.
_MISSED_BEATS_ALLOWED = 3


def heartbeat_interval_seconds() -> float:
    """Must match `_heartbeat_loop`'s own interval, which derives from the
    lease TTL. Defined here so the probe and the loop cannot disagree."""
    return max(settings.lease_ttl_seconds / 3, 2)


def staleness_threshold_seconds() -> float:
    return heartbeat_interval_seconds() * _MISSED_BEATS_ALLOWED


def touch_liveness() -> None:
    """Record that the heartbeat loop is still going round.

    Never raises: a healthcheck that cannot be written is not a reason to kill
    a worker that is otherwise doing its job. A write that keeps failing shows
    up as the file going stale, which is the same signal.
    """
    try:
        LIVENESS_PATH.touch()
    except OSError:
        pass


def is_alive(now: float | None = None) -> bool:
    """Whether the liveness file is fresh enough. The probe's whole logic.

    Pure but for the two reads, so the threshold rule is testable without
    waiting on a real worker.
    """
    try:
        mtime = LIVENESS_PATH.stat().st_mtime
    except OSError:
        # Never touched, or unreadable. A worker that has not completed its
        # first beat is not yet healthy -- compose's start_period is what
        # covers legitimate startup.
        return False
    return (now if now is not None else time.time()) - mtime <= staleness_threshold_seconds()


def main() -> int:
    """Entry point for the compose healthcheck. Exit 0 healthy, 1 not."""
    return 0 if is_alive() else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LIVENESS_PATH",
    "heartbeat_interval_seconds",
    "is_alive",
    "main",
    "staleness_threshold_seconds",
    "touch_liveness",
]