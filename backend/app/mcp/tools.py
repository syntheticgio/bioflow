"""The MCP tool surface.

Sixteen curated tools rather than one per REST route. Auto-generating from
`openapi.json` was considered and rejected: ~120 routes becomes ~120 tools,
floods the agent's context with things like the upload-chunk plumbing, and
cannot express a workflow. The OpenAPI schema is served as a *resource* the
agent reads instead.

Every function takes `owner` as an explicit keyword argument, resolved by
`context.owner_for` at the transport edge. That is the same explicit-parameter
choice `app/api/deps.py` records for the REST routes, and it is what makes the
scoping testable without a request in flight.

No tool deletes anything. That is a guardrail against agent error, not a
security boundary -- everything omitted here is still reachable over plain
HTTP by anything on this machine.
"""

import asyncio

from beanie import PydanticObjectId
from bson.errors import InvalidId

from app.errors import NotFoundError, ProfileUnresolvedError, ValidationError
from app.mcp.resources import GuideTopic, load_guide
from app.metadata import ncbi_taxonomy
from app.models import Job, Profile
from app.pipelines import tools as pipeline_tools
from app.pipelines.tools import TOOL_META
from app.queue import queue
from app.queue.registry import all_handlers
from app.services import (
    ncbi_assembly_service,
    object_service,
    project_service,
    search_service,
    suggestion_service,
)
from app.services.search_service import SearchQuery


def _project_summary(project) -> dict:
    return {
        "id": str(project.id),
        "name": project.name,
        "description": project.description,
        "parent_id": str(project.parent_id) if project.parent_id else None,
        "tags": project.tags,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
    }


def _object_summary(obj) -> dict:
    return {
        "id": str(obj.id),
        "name": obj.name,
        "status": obj.status.value,
        # DataObject.format is a required field with a default_factory, never
        # None -- unlike role, two lines down, which genuinely can be.
        "format": obj.format.kind.value,
        "role": obj.role.value if obj.role else None,
        # Renamed at this boundary: DataObject's own field is `size`.
        "size_bytes": obj.size,
    }


async def _owned_job(job_id: str, *, owner: str) -> Job:
    """Fetch a job, treating another profile's job as one that does not
    exist -- mirrors app/api/v1/jobs.py's private _owned_job, which this
    module can't import since it's local to the routes file."""
    job = await Job.get(PydanticObjectId(job_id))
    if job is None or job.owner != owner:
        raise NotFoundError(f"Job not found: {job_id}")
    return job


async def whoami(*, owner: str) -> dict:
    """Which profile this MCP connection is acting as."""
    if owner == "local":
        profile = await Profile.find_one({"adopted_legacy_owner": True})
    else:
        try:
            profile = await Profile.get(PydanticObjectId(owner))
        except InvalidId as e:
            # bson raises InvalidId, a BSONError -- not a ValueError -- for
            # the same reason app/api/deps.py's resolve_owner catches it
            # explicitly rather than letting it become an unhandled 500.
            # owner normally reaches this function already validated by
            # context.owner_for, but whoami is the one tool here that
            # re-derives an owner from the string instead of just forwarding
            # it, so it is the one place a malformed value could still slip
            # through to a raw, unhelpful bson stack trace.
            raise ProfileUnresolvedError(f"Malformed owner: {owner!r}") from e

    if profile is None:
        return {"owner": owner, "username": None}

    return {
        "owner": owner,
        "profile_id": str(profile.id),
        "username": profile.username,
    }


async def list_projects(*, owner: str, parent_id: str | None = None) -> dict:
    """Projects at the top level, or inside `parent_id` when given."""
    projects = await project_service.list_projects(
        owner=owner,
        parent_id=PydanticObjectId(parent_id) if parent_id else None,
    )
    return {"projects": [_project_summary(p) for p in projects]}


async def get_project(project_id: str, *, owner: str) -> dict:
    """One project. Another profile's project is reported as not found."""
    project = await project_service.get_project(PydanticObjectId(project_id), owner=owner)
    return _project_summary(project)


async def create_project(
    name: str,
    *,
    owner: str,
    description: str = "",
    parent_id: str | None = None,
) -> dict:
    """Create a project, optionally nested inside another."""
    project = await project_service.create_project(
        name=name,
        owner=owner,
        description=description,
        parent_id=PydanticObjectId(parent_id) if parent_id else None,
    )
    return _project_summary(project)


async def list_objects(project_id: str, *, owner: str) -> dict:
    """Data objects in a project."""
    objects = await object_service.list_objects(
        PydanticObjectId(project_id), owner=owner
    )
    return {"objects": [_object_summary(o) for o in objects]}


async def get_object(object_id: str, *, owner: str) -> dict:
    """One data object, with the facts detected on ingest."""
    obj = await object_service.get_object(PydanticObjectId(object_id), owner=owner)
    summary = _object_summary(obj)
    summary["metadata"] = obj.metadata
    return summary


async def suggest_next(object_id: str, *, owner: str) -> dict:
    """What can be run against this object right now, and why not otherwise.

    The highest-value tool here. It lets an agent ask the platform what to do
    instead of inferring it from the guides, and the answers are computed from
    the real object -- so they account for what is installed on this machine,
    whether a reference has an index, and what has already been run.

    Cards carry `payload` when they are runnable: hand that straight to
    `run_pipeline` rather than constructing one.
    """
    obj = await object_service.get_object(PydanticObjectId(object_id), owner=owner)
    return {"suggestions": await suggestion_service.suggestions_for(obj)}


async def run_pipeline(kind: str, params: dict, *, owner: str) -> dict:
    """Start a pipeline job. Returns immediately with a job id.

    `kind` is validated against `all_handlers()` -- the same registry backing
    `GET /jobs/types` -- rather than a list written here, so a newly
    registered handler is runnable without touching this module.

    The unknown-kind error names the valid values on purpose: this is the
    message an agent reads to correct itself.
    """
    known = all_handlers()
    if kind not in known:
        raise ValidationError(
            f"Unknown pipeline kind: {kind!r}. Valid kinds: {sorted(known)}",
            details={"kind": kind, "valid": sorted(known)},
        )

    job = await queue.enqueue(kind, owner=owner, payload=params)

    if job is None:
        # enqueue returns None when a matching non-terminal job already
        # exists. That is a successful outcome, not a failure -- saying so
        # stops an agent retrying into the same dedup guard. No job_id key
        # at all (not job_id: None) so an agent that blindly reads
        # result["job_id"] fails immediately with KeyError rather than
        # carrying a plausible-looking None one hop further into
        # get_job/cancel_job, which would 404 with the confusing
        # "Job not found: None".
        return {"deduplicated": True, "kind": kind}

    return {"job_id": str(job.id), "kind": kind, "state": job.state.value}


async def get_job(job_id: str, *, owner: str) -> dict:
    """A job's current state. Poll this for progress; jobs are asynchronous."""
    job = await _owned_job(job_id, owner=owner)

    return {
        "job_id": str(job.id),
        "type": job.type,
        "state": job.state.value,
        "attempts": job.attempts,
        "error": job.error.model_dump() if job.error else None,
    }


async def list_jobs(*, owner: str, limit: int = 50) -> dict:
    """Recent jobs for this profile, newest first."""
    jobs = await Job.find({"owner": owner}).sort("-created_at").limit(limit).to_list()
    return {
        "jobs": [
            {
                "job_id": str(j.id),
                "type": j.type,
                "state": j.state.value,
                "owner": j.owner,
            }
            for j in jobs
        ]
    }


async def cancel_job(job_id: str, *, owner: str) -> dict:
    """Stop a running or queued job.

    The one "undo what I started" affordance in this surface: an agent that
    can launch a multi-hour aligner should be able to halt it.

    Cancellation is cooperative -- a running job is signalled, not killed
    instantly -- so the return value is the disposition `queue.request_cancel`
    reports rather than a bare success flag: "cancelled" (it was queued/delayed
    and is fully stopped now), "cancelling" (it was running and has been
    signalled to stop), or "already_terminal" (it had already finished).
    """
    job = await _owned_job(job_id, owner=owner)

    outcome = await queue.request_cancel(str(job_id))
    if outcome == "not_found":
        raise NotFoundError(f"Job not found: {job_id}")

    return {"job_id": job_id, "outcome": outcome}


async def search_objects(query: str, *, owner: str, limit: int = 50) -> dict:
    """Find objects across the whole library by name and metadata."""
    result = await search_service.search_objects(
        SearchQuery(text=query, limit=limit), owner=owner
    )
    return {"objects": [_object_summary(o) for o in result["objects"]]}


async def search_ncbi(term: str, *, owner: str) -> dict:
    """Search NCBI for an organism and the assemblies it has on file.

    Two-step under the hood, same as the download dialog: `term` resolves to
    candidate organisms first, then the best match's assemblies are fetched.
    Acquisition itself is a separate step -- take an `accession` from the
    result and hand it to `bioflow_download_reference`.
    """
    suggestions = await asyncio.to_thread(ncbi_taxonomy.suggest_organisms, term)
    if not suggestions:
        return {"organisms": [], "assemblies": []}

    top = suggestions[0]
    page = await asyncio.to_thread(
        ncbi_taxonomy.search_assemblies_by_taxon, top.tax_id
    )

    return {
        "organisms": [s.as_dict() for s in suggestions],
        "assemblies": [a.as_dict() for a in page.assemblies],
    }


async def download_reference(
    accession: str, project_id: str, *, owner: str
) -> dict:
    """Download an NCBI assembly's genome into a project. Returns a job id.

    Like every pipeline, this is asynchronous -- poll `bioflow_get_job`.
    """
    run, job_ids = await ncbi_assembly_service.launch_download(
        project_id=PydanticObjectId(project_id),
        accession=accession,
        components=["genome"],
        owner=owner,
    )
    return {"job_id": job_ids[0] if job_ids else None, "accession": accession}


async def list_tools(*, owner: str) -> dict:
    """The bioinformatics tools BioFlow knows about, and whether each is
    installed on this machine."""
    out = {}
    for name, meta in TOOL_META.items():
        probe = getattr(pipeline_tools, name, None)
        installed = bool(probe and probe().available) if callable(probe) else None
        out[name] = {
            "installed": installed,
            "usage": getattr(meta, "usage", None),
            "homepage": getattr(meta, "homepage", None),
        }
    return {"tools": out}


async def get_guide(topic: str, *, owner: str) -> dict:
    """A workflow guide.

    Duplicated as a tool as well as a resource because agent support for MCP
    resources is uneven while tool-calling is universal -- same content, two
    doors.
    """
    try:
        parsed = GuideTopic(topic)
    except ValueError as e:
        valid = sorted(t.value for t in GuideTopic)
        raise ValidationError(
            f"Unknown guide topic: {topic!r}. Valid topics: {valid}",
            details={"topic": topic, "valid": valid},
        ) from e

    return {"topic": parsed.value, "content": load_guide(parsed)}


# Every tool name the server registers. `tests/mcp/test_guides.py` checks
# guides against this, and `tests/mcp/test_surface.py` checks it for anything
# destructive that should not be here.
TOOL_NAMES: set[str] = {
    "bioflow_whoami",
    "bioflow_list_projects",
    "bioflow_get_project",
    "bioflow_create_project",
    "bioflow_list_objects",
    "bioflow_get_object",
    "bioflow_suggest_next",
    "bioflow_run_pipeline",
    "bioflow_get_job",
    "bioflow_list_jobs",
    "bioflow_cancel_job",
    "bioflow_search_objects",
    "bioflow_search_ncbi",
    "bioflow_download_reference",
    "bioflow_list_tools",
    "bioflow_get_guide",
}
