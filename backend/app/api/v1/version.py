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

from app.services import git_revision
from app.version import __version__

router = APIRouter(tags=["version"])


class VersionOut(BaseModel):
    version: str

    # The revision fields below describe the *checkout being served*, which in
    # local development is not implied by `version` at all: the api container
    # bind-mounts a source tree, so a stale branch serves stale code under an
    # unchanged version number. All None in a shipped image, which has no
    # checkout to report on. See services/git_revision.py.
    git_sha: str | None = None
    git_branch: str | None = None
    git_matches_origin_main: bool | None = None


@router.get("/version", response_model=VersionOut)
async def get_version() -> VersionOut:
    # Read through the module rather than importing the function, so the
    # GIT_DIR a test points elsewhere is the one this call actually uses.
    rev = git_revision.read_revision(git_revision.GIT_DIR)
    if rev is None:
        return VersionOut(version=__version__)
    return VersionOut(
        version=__version__,
        git_sha=rev.short_sha,
        git_branch=rev.branch,
        git_matches_origin_main=rev.matches_origin_main,
    )
