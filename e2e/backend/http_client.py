"""HTTP client for uploading fixture files into a BioFlow project."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import httpx


class HttpUploadError(RuntimeError):
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
