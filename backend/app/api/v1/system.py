"""System status: storage usage and (from Phase 1) queue statistics."""

import shutil

from fastapi import APIRouter

from app.config import settings
from app.db.client import get_db
from app.logging import get_logger
from app.models import Blob, DataObject, Project
from app.pipelines import sources
from app.storage.home import check_home

log = get_logger(__name__)

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/stats")
async def system_stats() -> dict:
    home = check_home()

    # Free space is reported but not trusted for display. Under Docker
    # Desktop's VirtioFS every path below BIOINFO_HOME returns the statfs of
    # the filesystem hosting the *share root* (/Volumes), which is the Mac's
    # boot disk rather than the external drive the data actually sits on --
    # verified: 995 GB "total" against a drive that is really 3.7 TB. There is
    # no path inside the container that reports otherwise.
    #
    # The governor still consults it, and that is wrong in both directions --
    # a full boot disk would stop pipeline work needlessly, and a full data
    # drive would go unnoticed. Fixing it needs a host-side reporter, since
    # nothing inside the container can see past the share; see docs/TODO.md.
    disk = None
    if home.ok:
        try:
            usage = shutil.disk_usage(settings.bioinfo_home)
            disk = {
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "percent_used": round(usage.used / usage.total * 100, 1),
                # Tells the UI not to present these as the drive's numbers.
                "reliable": False,
            }
        except OSError:
            disk = None

    # What the library itself occupies, which we can count exactly. Blobs are
    # deduplicated, so summing blob sizes is the true on-disk cost -- summing
    # object sizes would double-count shared content.
    library_bytes = 0
    async for row in await get_db().blobs.aggregate(
        [{"$group": {"_id": None, "total": {"$sum": "$size"}}}]
    ):
        library_bytes = row.get("total") or 0

    stats: dict = {
        "storage": {
            "ok": home.ok,
            "detail": home.detail,
            "path": home.path,
            "disk": disk,
            "library_bytes": library_bytes,
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


@router.get("/sources")
async def data_sources() -> dict:
    """The external data sources behind the Sources help page.

    On `system` rather than `pipelines` because these are not pipeline
    tools -- nothing here is dispatched to by a job. Static data, so no
    probe and no I/O: unlike /pipelines/tools this cannot be slow and
    cannot fail.
    """
    return {"sources": sources.all_sources()}
