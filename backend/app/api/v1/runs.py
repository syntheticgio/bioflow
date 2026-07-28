"""Pipeline run endpoints: the action a user asked for, and how it is going."""

from datetime import datetime

from beanie import PydanticObjectId
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.errors import NotFoundError
from app.logging import get_logger
from app.models import PipelineRun, RunJob
from app.services import run_service

log = get_logger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])


class RunOut(BaseModel):
    id: str
    kind: str
    project_id: str
    label: str
    # Derived from member job states rather than stored, so it cannot drift
    # from what the jobs actually say.
    status: str
    inputs: list[dict]
    params: dict
    # Which tool actually ran a trim run. None for non-trim runs -- see
    # PipelineRun.tool.
    tool: str | None = None
    outputs: list[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, run: PipelineRun, status: str) -> "RunOut":
        return cls(
            id=str(run.id),
            kind=run.kind.value,
            project_id=str(run.project_id),
            label=run.label,
            status=status,
            inputs=[
                {
                    "object_id": str(i.object_id),
                    "name": i.name,
                    "role": i.role.value,
                }
                for i in run.inputs
            ],
            params=run.params,
            tool=run.tool,
            outputs=[str(o) for o in run.outputs],
            created_at=run.created_at,
            updated_at=run.updated_at,
        )


class RunDetail(RunOut):
    jobs: list[dict]


@router.get("", response_model=list[RunOut])
async def list_runs(
    project_id: str | None = None,
    limit: int = Query(50, le=200),
) -> list[RunOut]:
    """Runs, newest first, each with its derived status."""
    query: dict = {}
    if project_id:
        query["project_id"] = PydanticObjectId(project_id)

    runs = await PipelineRun.find(query).sort("-created_at").limit(limit).to_list()
    statuses = await run_service.status_for_many([r.id for r in runs])
    return [RunOut.of(r, statuses.get(r.id, "succeeded")) for r in runs]


@router.get("/for-job/{job_id}", response_model=RunOut | None)
async def run_for_job(job_id: PydanticObjectId) -> RunOut | None:
    """The run a job belongs to, if any. Lets a job view link back to context.

    Declared before `/{run_id}`: a path parameter would otherwise swallow
    "for-job" and try to parse it as an ObjectId.
    """
    link = await RunJob.find_one(RunJob.job_id == job_id)
    if link is None:
        return None
    run = await PipelineRun.get(link.run_id)
    if run is None:
        return None
    status, _ = await run_service.status_for(run.id)
    return RunOut.of(run, status.value)


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(run_id: PydanticObjectId) -> RunDetail:
    """One run, with the state of every job that served it."""
    run = await PipelineRun.get(run_id)
    if run is None:
        raise NotFoundError(f"Run not found: {run_id}")

    status, jobs = await run_service.status_for(run_id)
    return RunDetail(**RunOut.of(run, status.value).model_dump(), jobs=jobs)


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: PydanticObjectId) -> dict:
    """Cancel every job still in flight for this run.

    Reports per-job outcomes rather than a single verdict: a run part-way
    through has some jobs already finished, and "cancelled" would overstate
    what happened to those.
    """
    from app.queue import queue

    run = await PipelineRun.get(run_id)
    if run is None:
        raise NotFoundError(f"Run not found: {run_id}")

    outcomes: dict[str, str] = {}
    for link in await run_service.members(run_id):
        # A shared index build belongs to another run too, and cancelling it
        # would sabotage work this run does not own.
        if link.shared:
            outcomes[str(link.job_id)] = "skipped_shared"
            continue
        outcomes[str(link.job_id)] = await queue.request_cancel(str(link.job_id))

    log.info("run_cancel_requested", run_id=str(run_id), outcomes=outcomes)
    return {"run_id": str(run_id), "jobs": outcomes}
