"""Install and uninstall jobs for ON_DEMAND_IMAGE tools.

A 3-9 GB `docker pull` needs progress, cancellation, a log, and retry -- the
queue already provides all four, so install is a job rather than a synchronous
HTTP request that would time out or leave the caller with no way to know how
far along it is. `docker image rm` earns the same treatment for symmetry, even
though it is fast, so both go through one code path with one failure model.

Imported by `handlers.py` for the `@handler` registration side effects, same
as `pipeline_handlers`.

See docs/superpowers/specs/2026-08-05-optional-tool-delivery-design.md and
docs/superpowers/plans/2026-08-05-optional-tool-delivery.md (task 4).
"""

import re
import shutil
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import tool_cache, tools
from app.queue.executor import run_subprocess
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# A line like "<12-char-hex>: <status>". Docker's own layer-id format; used to
# tell "a new layer entered the pull" from "an existing layer changed state"
# without maintaining a second source of truth for what a layer id looks like.
_LAYER_LINE = re.compile(r"^([0-9a-f]{12}):\s*(.+)$")

# Terminal per-layer states. A layer that reaches one of these will not regress
# to an earlier one, so counting them is a safe, monotonic numerator for `pct`
# -- unlike counting "Downloading" lines, which repeat as a layer's progress
# bar advances and would make the fraction jump around rather than climb.
_LAYER_DONE_STATES = {"Pull complete", "Already exists"}


class _PullProgress:
    """Turns `docker pull`'s non-interactive, line-per-status-change output
    into a phase and a coarse fraction.

    Deliberately coarse. Piped (non-TTY) output has no byte counts to read --
    `docker pull` reports those only in the animated terminal form, which
    collapses to discrete "Downloading" / "Verifying Checksum" / "Download
    complete" / "Pull complete" lines per layer when piped, confirmed against
    a real pull rather than assumed. Getting a monotonic overall percentage
    out of concurrent per-layer downloads is fiddly for what it would buy;
    counting layers that have reached a terminal state against the number of
    distinct layers seen so far is enough to show the bar moving without
    inventing precision the output does not contain.
    """

    def __init__(self) -> None:
        self._layers: dict[str, str] = {}
        self.phase = "starting"

    def feed(self, line: str) -> bool:
        """Consume a line. True if the caller should publish an update."""
        match = _LAYER_LINE.match(line)
        if match is None:
            # "latest: Pulling from library/x", "Digest: ...", "Status: ...".
            # Not layer lines, but still worth a phase update the first time
            # one of them arrives, so "starting" does not linger once the
            # daemon has clearly begun.
            if self.phase == "starting" and line.strip():
                self.phase = "pulling"
                return True
            return False

        layer_id, status = match.groups()
        changed = self._layers.get(layer_id) != status
        self._layers[layer_id] = status
        if changed:
            self.phase = "pulling"
        return changed

    @property
    def pct(self) -> float | None:
        if not self._layers:
            return None
        done = sum(1 for status in self._layers.values() if status in _LAYER_DONE_STATES)
        return done / len(self._layers)

    def message(self) -> str:
        if not self._layers:
            return "starting pull"
        done = sum(1 for status in self._layers.values() if status in _LAYER_DONE_STATES)
        return f"pulling ({done}/{len(self._layers)} layers)"


def _progress_reporter(ctx: JobContext) -> "callable":
    progress = _PullProgress()

    def on_line(line: str) -> None:
        if progress.feed(line):
            ctx.progress(pct=progress.pct, phase=progress.phase, message=progress.message())

    return on_line


def _tool_and_image(payload: dict) -> tuple[str, str]:
    """Resolve and validate the tool named in the payload.

    Looked up against TOOL_META rather than trusted from the caller, so a
    payload naming a bundled tool or a tool with no delivery entry at all
    fails here with a clear reason instead of shelling out to `docker pull`
    against whatever string happened to arrive -- the service layer (task 4's
    other half) is expected to have refused this already, but a handler must
    not assume its only caller is the one that currently exists.
    """
    name = payload.get("tool")
    if not name:
        raise PermanentError("No tool named in the install job payload")

    meta = tools.TOOL_META.get(name)
    if meta is None or meta.delivery is not tools.Delivery.ON_DEMAND_IMAGE:
        raise PermanentError(
            f"{name!r} is not an on-demand tool and cannot be installed this way"
        )
    if not meta.image:
        raise PermanentError(f"{name!r} has no image configured")

    return name, meta.image


def _docker_client() -> str:
    client = shutil.which("docker")
    if client is None:
        raise PermanentError(
            "No docker client in this container, so tool images cannot be "
            "pulled or removed."
        )
    return client


@handler(
    "install_tool",
    mode=HandlerMode.SUBPROCESS,
    # The user pressed Install and is watching, not a follow-up to something
    # they asked for and walked away from -- USER_INTERACTIVE, not COMPUTE.
    # COMPUTE is deliberately deprioritized and never promoted, which would
    # leave a multi-gigabyte pull queued behind a multi-hour alignment.
    job_class=JobClass.USER_INTERACTIVE,
    # Bandwidth-bound, not CPU- or FUSE-heavy: this is a `docker pull`, not a
    # pipeline step touching /data.
    resources=JobResources(cpu=1, mem_mb=256, io=IoClass.LIGHT),
    # A pull failure is often transient -- a network blip, a registry rate
    # limit -- unlike a missing binary, so this gets more than trim_reads'
    # floor of 2. Not open-ended either: an auth failure or a bad manifest
    # will not resolve itself by the fifth attempt, and each attempt is a
    # multi-gigabyte transfer nobody should pay for five times over.
    max_attempts=3,
)
def install_tool(ctx: JobContext) -> dict:
    """Pull an ON_DEMAND_IMAGE tool's image.

    Runs off the event loop in a worker thread (SUBPROCESS mode): a `docker
    pull` can run for minutes and must not block the heartbeat that keeps this
    job's own lease alive.
    """
    name, image = _tool_and_image(ctx.payload)
    client = _docker_client()

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="starting", pct=None, message=f"starting pull of {image}")
    log.info("tool_install_started", job_id=ctx.job_id, tool=name, image=image)

    code = run_subprocess(
        ctx,
        [client, "pull", image],
        log_path=str(log_path),
        on_line=_progress_reporter(ctx),
    )
    if code != 0:
        raise _pull_failure(code, log_path, image)

    ctx.progress(phase="done", pct=1.0, message=f"{name} installed")
    log.info("tool_install_finished", job_id=ctx.job_id, tool=name, image=image)

    _invalidate(name)

    return {"tool": name, "image": image}


@handler(
    "uninstall_tool",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=1, mem_mb=128, io=IoClass.LIGHT),
    # `docker image rm` either succeeds or fails deterministically (image in
    # use, image absent) -- retrying it five times would not change the
    # outcome, so this stays low like every other deterministic-failure
    # handler in this file's neighbours.
    max_attempts=2,
)
def uninstall_tool(ctx: JobContext) -> dict:
    """Remove an ON_DEMAND_IMAGE tool's image."""
    name, image = _tool_and_image(ctx.payload)
    client = _docker_client()

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="removing", pct=None, message=f"removing {image}")
    log.info("tool_uninstall_started", job_id=ctx.job_id, tool=name, image=image)

    code = run_subprocess(ctx, [client, "image", "rm", image], log_path=str(log_path))
    if code != 0:
        raise _rm_failure(code, log_path, image)

    ctx.progress(phase="done", pct=1.0, message=f"{name} uninstalled")
    log.info("tool_uninstall_finished", job_id=ctx.job_id, tool=name, image=image)

    _invalidate(name)

    return {"tool": name, "image": image}


def _invalidate(name: str) -> None:
    """Tell every process's probe cache to forget this tool.

    A thread handler (SUBPROCESS mode) has no event loop of its own, and
    `publish_invalidation` is async because the Redis client is.
    `asyncio.run()` on a fresh loop is the wrong escape here -- not because
    this call touches Mongo (it does not), but because the worker process has
    exactly one real event loop, the one `worker_main.main()` runs on and both
    `connect_to_mongo()` and `connect_to_redis()` were awaited from, and a
    second unrelated loop is a second, disconnected world with no bearing on
    that one. `db.client.run_from_thread` schedules onto that real loop and
    blocks this thread for the result instead -- named for the Mongo case it
    was first written for (see `summary_handlers._resolve_sync`), but nothing
    about what it does is Mongo-specific: it reaches *the* loop, whatever
    coroutine is handed to it.

    `publish_invalidation` itself only ever warns and never raises, so a
    missed publish here means a stale badge until the next restart, not a
    failed install that already succeeded.
    """
    from app.db.client import run_from_thread
    from app.db.redis_client import get_redis

    try:
        run_from_thread(tool_cache.publish_invalidation(get_redis(), name))
    except Exception as e:  # noqa: BLE001 - see docstring
        log.warning("tool_install_invalidate_failed", tool=name, error=str(e))


def _pull_failure(code: int, log_path: Path, image: str) -> Exception:
    tail = _log_tail(log_path)
    detail = f"pulling {image} exited {code}"
    if tail:
        detail = f"{detail}: {tail}"
    # 137 is SIGKILL. A pull killed mid-transfer is exactly the transient case
    # this handler's higher max_attempts exists for.
    if code == 137:
        return RetryableError(f"{detail} (killed)")
    return RetryableError(detail)


def _rm_failure(code: int, log_path: Path, image: str) -> Exception:
    tail = _log_tail(log_path)
    detail = f"removing {image} exited {code}"
    if tail:
        detail = f"{detail}: {tail}"
    # Deterministic failures (image in use by a container, image already
    # gone) will not resolve on retry, unlike a pull's transient network
    # errors -- Permanent, not Retryable.
    return PermanentError(detail)


def _log_tail(path: Path, *, lines: int = 5, max_chars: int = 600) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    tail = " / ".join(line.strip() for line in text.splitlines()[-lines:] if line.strip())
    return tail[:max_chars]
