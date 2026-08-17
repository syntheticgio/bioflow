"""Read-only maintenance reporting.

Not owner-scoped, matching `settings.py`: there is one machine and one
filesystem here, so a profile header cannot change the answer.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.models.drift import DriftEntry, DriftReport

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


class DriftReportOut(BaseModel):
    swept_at: str
    skipped: bool
    skip_reason: str | None
    counts: dict[str, int]
    entries: list[DriftEntry]
    reclaimable_bytes: int


@router.get("/drift", response_model=DriftReportOut)
async def get_drift_report() -> DriftReportOut:
    """The most recent sweep. Reading never triggers one.

    The sweep walks the whole objects/ tree, so running it inside a request
    would block that request for as long as the walk takes. It runs on its
    schedule; this returns whatever it last stored.
    """
    report = await DriftReport.load()
    return DriftReportOut(
        swept_at=report.swept_at.isoformat(),
        skipped=report.skipped,
        skip_reason=report.skip_reason,
        counts=report.counts,
        entries=report.entries,
        reclaimable_bytes=report.reclaimable_bytes,
    )
