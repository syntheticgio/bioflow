"""The Auto re-plan button's endpoint.

A module of its own rather than another block in `pipelines.py`: one
responsibility, no shared state with the launch routes, and `pipelines.py` is
already 1400+ lines.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.queue.governor import LoadGovernor
from app.services import replan_service

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


class ReplanRequest(BaseModel):
    job_type: str
    params: dict = Field(default_factory=dict)
    # Deliberately no budget fields. A client that states its own budget can
    # state a larger one, and the feasibility test becomes a formality.
    # Pydantic ignores unknown keys by default, so one sent anyway is dropped.


@router.post("/replan")
async def replan(body: ReplanRequest) -> dict:
    """Propose a fitting configuration, or say why there is none.

    Returns a tagged union so the card can tell "nothing fits" from "there is
    nothing here to tune" -- different next steps, different prose. The button
    renders only for `proposal`, which is the design's guarantee that it is
    never offered and then refused.

    `replan_service` verifies every proposal against the same estimator that
    produced the refusal, so nothing is re-verified here.
    """
    governor = LoadGovernor()
    result = replan_service.replan(
        job_type=body.job_type,
        params=body.params,
        budget_mb=int(governor.mem_budget_bytes() / (1024 * 1024)),
        cpu_budget=governor.cpu_budget() or 1,
    )
    # The same serializer the enqueue-time refusal uses (Task 5), so the two
    # paths cannot drift into describing a proposal differently.
    return replan_service.as_payload(result)
