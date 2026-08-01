"""Handler registry and execution context.

Every handler declares an execution mode. The executor -- not the handler
author -- is responsible for keeping blocking work off the event loop, so a
handler cannot accidentally stall the heartbeat and cause its own lease to
expire. That failure is nasty in presentation (leases expire, jobs double-run)
and easy to introduce, so it is prevented structurally rather than by
convention.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.errors import JobCancelled
from app.logging import get_logger
from app.models import JobClass, JobResources

log = get_logger(__name__)


class HandlerMode(StrEnum):
    ASYNC = "async"  # coroutine; must not block
    THREAD = "thread"  # sync, CPU/IO-bound; run via asyncio.to_thread
    SUBPROCESS = "subprocess"  # spawns processes; killed by process group


@dataclass
class JobContext:
    """Passed to every handler. The only sanctioned way to report progress or
    observe cancellation."""

    job_id: str
    payload: dict
    epoch: int
    attempts: int
    # The profile this job's events belong to, copied from `Job.owner`. It is
    # here because the progress path had nowhere else to get one: progress is
    # published from the executor's throttled writer, which knows a job id and
    # an epoch and would otherwise have to re-read the job document several
    # times a second to answer "whose stream is this?". No default, so a new
    # construction site has to answer that question rather than silently
    # inheriting someone else's channel.
    owner: str
    # Set from the async side; thread handlers poll it, which is why it is a
    # threading.Event rather than an asyncio.Event.
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _progress_cb: Callable[[dict], None] | None = None
    _extend_cb: Callable[[int], None] | None = None
    # The longest lease any handler phase has asked for. Read by the worker's
    # heartbeat loop, written from handler threads. Individual reads/writes of
    # this int cannot tear under the GIL, but the compare-and-set in
    # extend_lease is not atomic as a whole -- two threads racing could still
    # lose an update. No lock anyway: the failure mode is bounded (one
    # shorter-than-ideal lease, corrected on the next call or heartbeat tick),
    # not worth the cost here.
    lease_override_seconds: int | None = None

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check_cancel(self) -> None:
        """Raise if cancellation was requested. Call at loop boundaries."""
        if self.cancel_event.is_set():
            raise JobCancelled(f"Job {self.job_id} cancelled")

    def progress(
        self,
        *,
        pct: float | None = None,
        phase: str | None = None,
        bytes_done: int | None = None,
        bytes_total: int | None = None,
        message: str | None = None,
    ) -> None:
        if self._progress_cb is None:
            return
        update = {
            k: v
            for k, v in {
                "pct": pct,
                "phase": phase,
                "bytes_done": bytes_done,
                "bytes_total": bytes_total,
                "message": message,
            }.items()
            if v is not None
        }
        if update:
            self._progress_cb(update)

    def extend_lease(self, seconds: int) -> None:
        """Request a longer lease for a known-long phase.

        The heartbeat renews every in-flight job regardless of duration, so a
        merely slow job is already safe. What this covers is the lease *length*:
        a paused VM or a stalled event loop stops the heartbeat entirely, and
        then only the recorded TTL stands between a live job and the reaper
        requeueing it underneath itself. A handler that knows it will go quiet
        for an hour says so here.

        The longest request wins. A handler with several long phases would
        otherwise shorten its own lease by asking for less later on.
        """
        if seconds <= 0:
            return
        if self.lease_override_seconds is None or seconds > self.lease_override_seconds:
            self.lease_override_seconds = seconds
        if self._extend_cb is not None:
            self._extend_cb(seconds)


@dataclass
class HandlerSpec:
    name: str
    fn: Callable[..., Any]
    mode: HandlerMode
    default_class: JobClass
    default_resources: JobResources
    max_attempts: int


_HANDLERS: dict[str, HandlerSpec] = {}


def handler(
    name: str,
    *,
    mode: HandlerMode = HandlerMode.ASYNC,
    job_class: JobClass = JobClass.USER_BACKGROUND,
    resources: JobResources | None = None,
    max_attempts: int = 5,
):
    """Register a job handler.

    Handlers must be idempotent. Delivery is at-least-once: a lease can expire
    while work is genuinely still running, so any handler may be invoked twice
    for the same job.
    """

    def decorator(fn):
        if name in _HANDLERS:
            raise ValueError(f"Duplicate job handler: {name}")
        _HANDLERS[name] = HandlerSpec(
            name=name,
            fn=fn,
            mode=mode,
            default_class=job_class,
            default_resources=resources or JobResources(),
            max_attempts=max_attempts,
        )
        return fn

    return decorator


def get_handler(name: str) -> HandlerSpec | None:
    return _HANDLERS.get(name)


def all_handlers() -> dict[str, HandlerSpec]:
    return dict(_HANDLERS)


def load_handlers() -> None:
    """Import handler modules for their registration side effects."""
    from app.queue import handlers  # noqa: F401

    log.info("handlers_loaded", names=sorted(_HANDLERS))
