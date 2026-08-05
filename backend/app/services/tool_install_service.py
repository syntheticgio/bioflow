"""Install and uninstall orchestration for ON_DEMAND_IMAGE tools.

The handlers in `app.queue.tool_handlers` do the actual `docker pull` / `docker
image rm`; this module is what decides whether one should be enqueued at all --
the checks that must hold before a job exists, not the ones the job itself
enforces once it is running.

See docs/superpowers/specs/2026-08-05-optional-tool-delivery-design.md and
docs/superpowers/plans/2026-08-05-optional-tool-delivery.md (task 4).
"""

from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging import get_logger
from app.models import ACTIVE_STATES, Job, JobClass, JobState
from app.pipelines import tools
from app.pipelines.tools import Delivery
from app.queue import queue

log = get_logger(__name__)


def _dedup_key(tool_name: str) -> str:
    return f"tool_install:{tool_name}"


def _active_install_query(tool_name: str, owner: str) -> dict:
    """This owner's in-flight install or uninstall for a tool, if there is
    one.

    Mirrors `pipeline_service.active_index_job_query`: a raw Mongo query
    rather than Beanie's field expressions, for the same reason that one is --
    `Job.state` is not resolvable as an attribute outside a query context.
    Matches either job type, since an install and an uninstall for the same
    tool should not race each other any more than two installs should.

    `owner` matters here in a way it does not for `active_index_job_query`.
    `enqueue` stamps its stored dedup key as `f"{owner}:{dedup_key}"`, so two
    different profiles installing the same tool get two different keys and
    two independent jobs -- correctly, since a dedup collision must never
    make one profile's request silently piggyback on another's. But this
    query runs only *after* `enqueue` reports a collision for `owner`
    specifically, and without this filter it would match the first active
    job for the tool by *any* owner, handing a caller back a job it does not
    own and, worse, that another profile's later duplicate call could then
    also be handed -- collapsing what should be independent per-owner work
    into one shared job. Found by a real test failure, not by inspection:
    two owners' jobs got the same id back until this filter was added.
    """
    return {
        "type": {"$in": ["install_tool", "uninstall_tool"]},
        "payload.tool": tool_name,
        "owner": owner,
        "state": {"$in": [s.value for s in ACTIVE_STATES]},
    }


def _running_job_using_query(tool_name: str) -> dict:
    """A currently-running job whose payload names this tool as the caller.

    `payload.caller` is where `call_variants` records which caller it
    dispatched to (`variant_runner.VariantCaller.DEEPVARIANT.value ==
    "deepvariant"`, matching the tool name exactly today). Narrower than
    every ACTIVE_STATES: a queued-but-not-yet-running job has not touched the
    image yet, and removing it out from under a job that has not started is
    no different from removing it before anyone launched anything -- the next
    attempt to run that job simply finds the tool missing, the same as if it
    had never been installed. Only a job already RUNNING is actually using
    the image `docker image rm` would remove out from under it.
    """
    return {
        "payload.caller": tool_name,
        "state": JobState.RUNNING.value,
    }


def _tool_meta(tool_name: str) -> tools.ToolMeta:
    meta = tools.TOOL_META.get(tool_name)
    if meta is None:
        raise NotFoundError(f"No such tool: {tool_name!r}")
    return meta


async def install(*, tool_name: str, owner: str) -> Job:
    """Queue a pull of `tool_name`'s image.

    Returns the existing job rather than a fresh one if an install (or
    uninstall) for this tool is already in flight -- one install per tool at
    a time, found through `dedup_key` the same way `enqueue` already
    deduplicates everywhere else, and then looked up explicitly because
    `enqueue` reports a duplicate as `None`, not as the job that already
    exists. `pipeline_service.launch_build_index`'s handling of a
    `build_index` race is the precedent for the lookup half of this; every
    *other* dedup call site in this codebase instead raises ConflictError on
    `None`, which read wrong here -- an Install button clicked twice by an
    impatient user should show the one job progressing, not an error.
    """
    meta = _tool_meta(tool_name)
    if meta.delivery is not Delivery.ON_DEMAND_IMAGE:
        raise ValidationError(
            f"{tool_name!r} is bundled in the image and cannot be installed",
            details={"tool": tool_name},
        )

    job = await queue.enqueue(
        "install_tool",
        owner=owner,
        payload={"tool": tool_name},
        job_class=JobClass.USER_INTERACTIVE,
        dedup_key=_dedup_key(tool_name),
    )
    if job is not None:
        return job

    existing = await Job.find_one(_active_install_query(tool_name, owner))
    if existing is not None:
        return existing

    # Deduplicated by the unique index but the job that held the key is gone
    # by the time this reads it back -- a genuine race, not a bug in the
    # lookup above. Vanishingly unlikely (the window is one Mongo round trip)
    # and not worth a retry loop for; the caller can press the button again.
    raise ConflictError(
        f"An install for {tool_name!r} was just deduplicated but could not be found",
        details={"tool": tool_name},
    )


async def uninstall(*, tool_name: str, owner: str) -> Job:
    """Queue removal of `tool_name`'s image.

    Symmetric with `install`'s eligibility check on purpose -- this is the
    rule from the design doc, not an incidental similarity: uninstall is
    offered exactly when install was, so a bundled tool refuses here for the
    identical reason it would refuse an install, and a tool that has never
    been pulled refuses because there is nothing to remove.
    """
    meta = _tool_meta(tool_name)
    if meta.delivery is not Delivery.ON_DEMAND_IMAGE:
        raise ValidationError(
            f"{tool_name!r} is bundled in the image and has nothing to uninstall",
            details={"tool": tool_name},
        )

    probe = _probe_for(tool_name)
    if probe.install_state is not tools.InstallState.INSTALLED:
        raise ValidationError(
            f"{tool_name!r} is not installed", details={"tool": tool_name}
        )

    running = await Job.find_one(_running_job_using_query(tool_name))
    if running is not None:
        raise ConflictError(
            f"A running job is using {tool_name!r}; wait for it to finish "
            "before uninstalling",
            details={"tool": tool_name, "job_id": str(running.id)},
        )

    job = await queue.enqueue(
        "uninstall_tool",
        owner=owner,
        payload={"tool": tool_name},
        job_class=JobClass.USER_INTERACTIVE,
        dedup_key=_dedup_key(tool_name),
    )
    if job is not None:
        return job

    existing = await Job.find_one(_active_install_query(tool_name, owner))
    if existing is not None:
        return existing

    raise ConflictError(
        f"An uninstall for {tool_name!r} was just deduplicated but could not be found",
        details={"tool": tool_name},
    )


def _probe_for(tool_name: str) -> tools.Tool:
    """The live probe function for `tool_name`, called by name.

    Only `deepvariant` exists today; a second ON_DEMAND_IMAGE tool (Clair3,
    eventually) adds a branch here, the same shape `_run_clair3`/
    `_run_deepvariant` already dispatch on in `variant_handlers`. A dict of
    tool name to probe function would remove the branch, but there is no such
    registry today and building one for two entries is not worth it yet --
    revisit when there is a third.
    """
    if tool_name == "deepvariant":
        return tools.deepvariant()
    raise NotFoundError(f"No probe wired up for {tool_name!r}")
