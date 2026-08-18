"""HTTP client for uploading fixtures and launching pipelines in BioFlow."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import httpx

# Where a suggestion card's `launch.endpoint` is rooted. Cards carry a
# router-relative path (`/pipelines/align`), because the frontend joins it
# onto its own API base; the harness has to do the same join rather than
# POSTing the bare path.
API_PREFIX = "/api/v1"


class HttpUploadError(RuntimeError):
    pass


class HttpLaunchError(RuntimeError):
    pass


async def upload_object(base_url: str, profile: str, project_id: str, file_path: str) -> dict:
    """Upload ``file_path`` into ``project_id``. Returns the created object dict.

    Uses BioFlow's simple streamed upload: raw bytes in the body, the
    percent-encoded filename in ``X-Filename``, and the profile id (when set)
    in ``X-BioFlow-Profile``. See ``backend/app/api/v1/projects.py:upload_object``.
    """
    path = Path(file_path)
    # ASYNC240 wants anyio.Path. This is a one-shot fixture read in a test
    # harness; a brief blocking read does not justify the extra dependency.
    data = path.read_bytes()  # noqa: ASYNC240
    headers = {"X-Filename": quote(path.name)}
    if profile:
        headers["X-BioFlow-Profile"] = profile
    url = f"{base_url.rstrip('/')}/api/v1/projects/{project_id}/objects/upload"
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(url, content=data, headers=headers)
    if resp.status_code != 201:
        raise HttpUploadError(f"upload failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


async def launch_pipeline(
    base_url: str, profile: str, endpoint: str, body: dict
) -> dict:
    """POST a suggestion card's `launch` to the REST API. Returns the job dict.

    This is the step the reads path was blocked on. The QC/trim/align flow is
    deliberately *not* reachable through the `run_pipeline` MCP tool: that
    tool passes `params` straight through as the raw handler payload, so
    using it means hand-building the `object_id`/`project_id`/`name`/
    `platform`/... shape that the launch endpoints assemble server-side --
    which is a test of the harness's guess about that shape, not of BioFlow.
    `suggest_next` already returns the complete body; posting it unmodified
    is what makes this test follow the path an agent actually takes.

    `endpoint` is taken from the card verbatim and joined onto `API_PREFIX`,
    so a card that changes its endpoint moves this test with it rather than
    silently drifting from what the UI does.
    """
    path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    if not path.startswith(API_PREFIX):
        path = f"{API_PREFIX}{path}"
    url = f"{base_url.rstrip('/')}{path}"

    headers = {}
    if profile:
        headers["X-BioFlow-Profile"] = profile

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(url, json=body, headers=headers)

    # Every launch endpoint answers 201 with a JobOut. A 200 would mean some
    # other route handled it, which is worth failing on rather than treating
    # as success.
    if resp.status_code != 201:
        raise HttpLaunchError(
            f"launch {path} failed ({resp.status_code}): {resp.text[:500]}"
        )
    return resp.json()


async def patch_object(base_url: str, profile: str, object_id: str, body: dict) -> dict:
    """PATCH a data object. Returns the updated object dict.

    The reads path needs this to mark an uploaded FASTA as the reference:
    ingest assigns no role to a plain upload, and the align card counts only
    objects whose role *is* REFERENCE -- deliberately, so that a project's
    `protein.faa` is not offered as a genome to align against. Setting the
    role is what a user does in the UI, so the test does it too rather than
    reaching into the database.
    """
    url = f"{base_url.rstrip('/')}{API_PREFIX}/objects/{object_id}"
    headers = {}
    if profile:
        headers["X-BioFlow-Profile"] = profile
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.patch(url, json=body, headers=headers)
    if resp.status_code != 200:
        raise HttpLaunchError(
            f"patch object {object_id} failed ({resp.status_code}): {resp.text[:500]}"
        )
    return resp.json()


async def get_object(base_url: str, profile: str, object_id: str) -> dict:
    """GET one data object over REST. Returns the full ObjectOut.

    The MCP `get_object` tool is the natural way to read an object here, but
    it returns only `metadata` -- and a pipeline's measured numbers land in
    `facts` (flagstat's `mapped_pct`, for one). REST's ObjectOut carries
    both, so an assertion about what a run actually measured has to come
    through here.
    """
    url = f"{base_url.rstrip('/')}{API_PREFIX}/objects/{object_id}"
    headers = {}
    if profile:
        headers["X-BioFlow-Profile"] = profile
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise HttpLaunchError(
            f"get object {object_id} failed ({resp.status_code}): {resp.text[:500]}"
        )
    return resp.json()
