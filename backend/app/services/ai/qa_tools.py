"""The two tools the project Q&A loop can call.

Both are thin wrappers over the exact search/jobs logic the UI itself calls
-- `search_service.search_objects` and the same filter shape `GET /jobs`
uses -- so the model reasons over structured data an ordinary click already
reaches, not free-text retrieval.

Scoping to one project and one owner lives entirely here, by construction
rather than by validation: the JSON schemas below have no `project_id` or
`owner` property at all, so a model cannot ask for a different project's
data because there is no field to put one in. `execute_search_objects` and
`execute_list_jobs` take `project_id`/`owner` as explicit keyword arguments
supplied by the caller, never read from the model's parsed `arguments` dict
-- mirroring `search_service.SearchQuery`'s own "owner is a keyword-only
argument, never a request field" convention.
"""

from beanie import PydanticObjectId

from app.models import DataObject, Job
from app.queue import keys
from app.services import search_service
from app.services.ai.adapters import ToolSpec

MAX_TOOL_RESULT_ROWS = 50

SEARCH_OBJECTS_SPEC = ToolSpec(
    name="search_objects",
    description="Search files in this project by kind, status, tags, or metadata.",
    parameters={
        "type": "object",
        "properties": {
            "kinds": {"type": "array", "items": {"type": "string"}},
            "statuses": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "metadata": {"type": "object"},
            "size_min": {"type": "integer"},
            "size_max": {"type": "integer"},
            "limit": {"type": "integer"},
        },
    },
)

LIST_JOBS_SPEC = ToolSpec(
    name="list_jobs",
    description="List queue jobs for this project, optionally filtered by state or type.",
    parameters={
        "type": "object",
        "properties": {
            "state": {"type": "string"},
            "job_type": {"type": "string"},
            "object_id": {"type": "string"},
        },
    },
)


def _trim_object(obj: DataObject) -> dict:
    return {
        "id": str(obj.id),
        "name": obj.name,
        "kind": obj.format.kind if obj.format else None,
        "status": obj.status,
        "size": obj.size,
        "facts": obj.facts,
    }


def _trim_job(job: Job) -> dict:
    return {
        "type": job.type,
        "state": job.state,
        "progress": job.progress.model_dump(mode="json") if job.progress else None,
        "timing": job.timing.model_dump(mode="json") if job.timing else None,
        "error": job.error.model_dump(mode="json") if job.error else None,
    }


async def execute_search_objects(
    arguments: dict, *, project_id: PydanticObjectId, owner: str
) -> dict:
    query = search_service.SearchQuery(
        project_id=project_id,
        kinds=arguments.get("kinds") or [],
        statuses=arguments.get("statuses") or [],
        tags=arguments.get("tags") or [],
        metadata=arguments.get("metadata") or {},
        size_min=arguments.get("size_min"),
        size_max=arguments.get("size_max"),
        limit=min(arguments.get("limit") or MAX_TOOL_RESULT_ROWS, MAX_TOOL_RESULT_ROWS),
    )
    result = await search_service.search_objects(query, owner=owner)
    return {
        "objects": [_trim_object(o) for o in result["objects"]],
        "total": result["total"],
        "has_more": result["has_more"],
    }


async def execute_list_jobs(
    arguments: dict, *, project_id: PydanticObjectId, owner: str
) -> dict:
    filt: dict = {"owner": {"$in": [owner, keys.SYSTEM_OWNER]}, "project_id": project_id}
    if arguments.get("state"):
        filt["state"] = arguments["state"]
    if arguments.get("job_type"):
        filt["type"] = arguments["job_type"]
    if arguments.get("object_id"):
        filt["object_id"] = PydanticObjectId(arguments["object_id"])

    limit = min(arguments.get("limit") or MAX_TOOL_RESULT_ROWS, MAX_TOOL_RESULT_ROWS)
    jobs = await Job.find(filt).limit(limit).to_list()
    return {"jobs": [_trim_job(j) for j in jobs]}
