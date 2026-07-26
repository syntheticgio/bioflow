"""Liveness and readiness endpoints."""

from fastapi import APIRouter, Response

from app.api.v1.schemas import HealthOut
from app.db import client as mongo
from app.db import redis_client as redis
from app.storage.home import check_home

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthOut)
async def healthz() -> HealthOut:
    """Liveness: the process is up. Deliberately checks no dependencies, so a
    database blip does not cause Docker to restart a healthy container."""
    return HealthOut(status="ok", checks={})


@router.get("/readyz", response_model=HealthOut)
async def readyz(response: Response) -> HealthOut:
    """Readiness: every dependency needed to serve real traffic.

    Storage failures report the actionable cause -- almost always an unmounted
    drive or a path missing from Docker Desktop's file sharing list.
    """
    home = check_home()
    checks = {
        "mongo": {"ok": await mongo.ping()},
        "redis": {"ok": await redis.ping()},
        "storage": {"ok": home.ok, "detail": home.detail, "path": home.path},
    }
    ok = all(c["ok"] for c in checks.values())
    if not ok:
        response.status_code = 503
    return HealthOut(status="ok" if ok else "unavailable", checks=checks)
