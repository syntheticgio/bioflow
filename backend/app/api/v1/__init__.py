"""v1 API router assembly."""

from fastapi import APIRouter

from app.api.v1 import (
    events,
    feedback,
    jobs,
    local_databases,
    ncbi,
    objects,
    pipelines,
    profiles,
    project_qa,
    projects,
    replan,
    runs,
    schedules,
    search,
    settings,
    shares,
    sra,
    system,
    uniprot,
    uploads,
    version,
)

api_router = APIRouter(prefix="/api/v1")
# search first: its /objects/bulk-* routes must not be shadowed by the
# /objects/{object_id} path parameter in the objects router.
api_router.include_router(search.router)
api_router.include_router(profiles.router)
api_router.include_router(shares.router)
api_router.include_router(projects.router)
api_router.include_router(project_qa.router)
api_router.include_router(objects.router)
api_router.include_router(uploads.router)
api_router.include_router(jobs.router)
api_router.include_router(pipelines.router)
api_router.include_router(replan.router)
api_router.include_router(runs.router)
api_router.include_router(sra.router)
api_router.include_router(ncbi.router)
api_router.include_router(uniprot.router)
api_router.include_router(schedules.router)
api_router.include_router(system.router)
api_router.include_router(events.router)
api_router.include_router(feedback.router)
api_router.include_router(local_databases.router)
api_router.include_router(settings.router)
api_router.include_router(version.router)

__all__ = ["api_router"]
