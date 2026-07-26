"""System status: storage usage and (from Phase 1) queue statistics."""

import shutil

from fastapi import APIRouter

from app.config import settings
from app.logging import get_logger
from app.models import Blob, DataObject, Project
from app.storage.home import check_home

log = get_logger(__name__)

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/stats")
async def system_stats() -> dict:
    home = check_home()

    disk = None
    if home.ok:
        try:
            usage = shutil.disk_usage(settings.bioinfo_home)
            disk = {
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "percent_used": round(usage.used / usage.total * 100, 1),
            }
        except OSError:
            disk = None

    stats: dict = {
        "storage": {
            "ok": home.ok,
            "detail": home.detail,
            "path": home.path,
            "disk": disk,
        },
        "counts": {
            "projects": await Project.find(Project.archived == False).count(),  # noqa: E712
            "objects": await DataObject.find().count(),
            "blobs": await Blob.find().count(),
        },
    }

    # Queue stats must not break the endpoint, but a silent None hides real
    # bugs behind an empty badge -- so log the cause and report it inline.
    try:
        from app.queue import stats as queue_stats

        stats["queue"] = await queue_stats.snapshot()
    except Exception as e:  # noqa: BLE001
        log.error("queue_stats_failed", error=str(e), exc_info=True)
        stats["queue"] = None
        stats["queue_error"] = str(e)

    return stats


@router.get("/load")
async def system_load() -> dict:
    """Current load and admission state.

    The governor lands in Phase 4; until then this reports raw metrics and a
    permanently OPEN admission state.
    """
    from app.queue.governor import current_load

    return await current_load()
