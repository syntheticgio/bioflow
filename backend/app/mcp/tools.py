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

from beanie import PydanticObjectId
from bson.errors import InvalidId

from app.errors import ProfileUnresolvedError
from app.models import Profile
from app.services import object_service, project_service, suggestion_service


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
}
