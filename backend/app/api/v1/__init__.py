"""v1 API router assembly."""

from fastapi import APIRouter

from app.api.v1 import (
    events,
    jobs,
    objects,
    projects,
    schedules,
    search,
    system,
    uploads,
)

api_router = APIRouter(prefix="/api/v1")
# search first: its /objects/bulk-* routes must not be shadowed by the
# /objects/{object_id} path parameter in the objects router.
api_router.include_router(search.router)
api_router.include_router(projects.router)
api_router.include_router(objects.router)
api_router.include_router(uploads.router)
api_router.include_router(jobs.router)
api_router.include_router(schedules.router)
api_router.include_router(system.router)
api_router.include_router(events.router)

__all__ = ["api_router"]
