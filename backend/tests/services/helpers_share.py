"""Shared setup for share_service tests: real profiles, real ingested objects.

Per docs/superpowers/specs/2026-07-31-profiles-design.md, "Testing" -- a share
test that only proves a profile can see what was shared to it passes whether
or not any ownership check exists. These helpers drive the real create_profile
and ingest_local_file paths (not hand-built factories) so the owner values
under test are the ones production actually stamps.
"""

import uuid
from pathlib import Path

from app.config import settings
from app.models import DataObject, ObjectStatus, Project
from app.services import object_service, profile_service, project_service

_scratch_files: list[Path] = []


def scratch_file(content: bytes) -> Path:
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    path = settings.tmp_dir / f"share-test-{uuid.uuid4().hex}.txt"
    path.write_bytes(content)
    _scratch_files.append(path)
    return path


def reclaim_scratch_files() -> None:
    for path in _scratch_files:
        path.unlink(missing_ok=True)
    _scratch_files.clear()


async def make_profile(username: str) -> str:
    """A real Profile, returning its owner_id()."""
    profile = await profile_service.create_profile(username=username)
    return profile.owner_id()


async def ready_object(
    *, owner: str, project: Project | None = None, content: bytes | None = None
) -> DataObject:
    """A real, READY object with stored bytes -- ingest_local_file end to end,
    not a hand-built DataObject with status set by hand.

    `ingest_local_file` leaves the object at INGESTING and queues
    `ingest_headers` to do the header parse that flips it to READY; these
    tests stub that enqueue (it needs live Redis, which is orthogonal to
    sharing), so the flip has to happen here instead of never happening.
    """
    if project is None:
        project = await project_service.create_project(
            name=f"proj-{uuid.uuid4().hex[:8]}", owner=owner
        )
    path = scratch_file(content or uuid.uuid4().bytes)
    obj = await object_service.ingest_local_file(
        project_id=project.id, path=path, name=path.name, owner=owner
    )
    await obj.set({DataObject.status: ObjectStatus.READY})
    return await object_service.get_object(obj.id, owner=owner)
