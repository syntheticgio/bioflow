"""Pipeline endpoints: launching runs and reporting tool availability."""

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.v1.jobs import JobOut
from app.config import settings
from app.errors import NotFoundError
from app.models import DataObject
from app.pipelines import tools
from app.services import pipeline_service

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


class TrimRequest(BaseModel):
    object_id: PydanticObjectId
    # Omitted means "use the detected mate"; paired=False forces single-end
    # even when one is known, which is the escape hatch for a pair that should
    # not be trimmed together.
    mate_object_id: PydanticObjectId | None = None
    paired: bool = True
    params: dict = Field(default_factory=dict)


class MateSuggestion(BaseModel):
    object_id: str
    name: str
    mate: str | None


@router.get("/tools")
async def list_tools() -> dict:
    """Resolved paths and versions for the external tools.

    Lets the launch dialog say "fastp is not installed" before a user commits
    to a run, rather than surfacing it as a job that dies minutes later.
    """
    return {
        "tools": [t.as_dict() for t in tools.all_tools()],
        "all_available": all(t.available for t in tools.all_tools()),
    }


@router.get("/defaults")
async def trim_defaults() -> dict:
    """Default trim parameters, owned by the server so the form does not
    encode its own copy."""
    return {
        "params": pipeline_service.default_params(),
        "max_threads": settings.pipeline_default_threads,
    }


@router.get("/mate/{object_id}", response_model=MateSuggestion | None)
async def detect_mate(object_id: PydanticObjectId) -> MateSuggestion | None:
    """The file this one would be trimmed alongside, if any."""
    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")

    mate = await pipeline_service.suggest_mate(obj)
    if mate is None:
        return None

    from app.pipelines import pairing

    return MateSuggestion(
        object_id=str(mate.id), name=mate.name, mate=pairing.mate_of(mate.name)
    )


@router.post("/trim", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_trim(body: TrimRequest) -> JobOut:
    """Queue an adapter-trimming run over a FASTQ file or an R1/R2 pair."""
    job = await pipeline_service.launch_trim(
        object_id=body.object_id,
        mate_object_id=body.mate_object_id,
        params=body.params,
        paired=body.paired,
    )
    return JobOut.of(job)
