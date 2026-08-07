"""What version this instance is running.

Deliberately dependency-free: no profile, no database, no auth. Someone asking
"what am I running?" is often mid-setup or mid-support-conversation, and an
endpoint that needs a working stack to answer is useless in exactly those
cases.

No prefix, unlike its sibling routers -- the path is /api/v1/version, and
api_router supplies the /api/v1 half.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.version import __version__

router = APIRouter(tags=["version"])


class VersionOut(BaseModel):
    version: str


@router.get("/version", response_model=VersionOut)
async def get_version() -> VersionOut:
    return VersionOut(version=__version__)
