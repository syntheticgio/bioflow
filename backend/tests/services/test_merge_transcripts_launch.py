"""launch_merge_transcripts's validation and dedup shape.

Mirrors test_sv_merge_launch.py's beanie-backed style: the launcher touches the
database (it looks each GTF up and creates a run), so it runs against real
objects with the queue path stubbed. The checks that matter here -- fewer than
two inputs, a non-assembly input, an input from another project -- all fire
before the enqueue, so a test that reaches enqueue at all proves the valid
input passed every gate.
"""

import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.errors import ValidationError
from app.models import ObjectRole
from app.services import object_service, pipeline_service, project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

OWNER = "merge-transcripts-launch-owner"


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    async def _skip_ingest(obj, **kwargs):
        return ""

    async def _skip_enqueue(*args, **kwargs):
        return None

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
    monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)


def _scratch_file(*, suffix: str = "") -> Path:
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    path = settings.tmp_dir / f"merge-transcripts-{uuid.uuid4().hex}{suffix}"
    path.write_bytes(uuid.uuid4().bytes)
    return path


async def _setup_assemblies(*, count: int = 3, project=None):
    if project is None:
        project = await project_service.create_project(
            name=f"proj-{uuid.uuid4().hex}", owner=OWNER
        )
    objects = []
    for i in range(count):
        obj = await object_service.ingest_local_file(
            owner=OWNER,
            project_id=project.id,
            path=_scratch_file(suffix=".gtf"),
            name=f"sample{i + 1}.transcripts.gtf",
            role=ObjectRole.ASSEMBLED_TRANSCRIPTS,
        )
        objects.append(obj)
    return project, objects


async def test_refuses_fewer_than_two_inputs():
    project, [single] = await _setup_assemblies(count=1)
    with pytest.raises(ValidationError, match="At least two"):
        await pipeline_service.launch_merge_transcripts(
            project_id=project.id,
            owner=OWNER,
            gtf_object_ids=[single.id],
        )


async def test_refuses_empty_input_list():
    project = await project_service.create_project(
        name=f"proj-{uuid.uuid4().hex}", owner=OWNER
    )
    with pytest.raises(ValidationError, match="At least two"):
        await pipeline_service.launch_merge_transcripts(
            project_id=project.id, owner=OWNER, gtf_object_ids=[]
        )


async def test_refuses_a_non_assembled_transcript_input():
    project = await project_service.create_project(
        name=f"proj-{uuid.uuid4().hex}", owner=OWNER
    )
    _, [a1, a2] = await _setup_assemblies(count=2, project=project)
    non_assembly = await object_service.ingest_local_file(
        owner=OWNER,
        project_id=project.id,
        path=_scratch_file(suffix=".gtf"),
        name="reference.gtf",
        role=ObjectRole.ANNOTATION,
    )
    with pytest.raises(ValidationError, match="not an assembled-transcripts GTF"):
        await pipeline_service.launch_merge_transcripts(
            project_id=project.id,
            owner=OWNER,
            gtf_object_ids=[a1.id, non_assembly.id, a2.id],
        )


async def test_refuses_inputs_from_another_project():
    _, [a1, a2] = await _setup_assemblies(count=2)
    other = await project_service.create_project(
        name=f"proj-{uuid.uuid4().hex}", owner=OWNER
    )
    with pytest.raises(ValidationError, match="different project"):
        await pipeline_service.launch_merge_transcripts(
            project_id=other.id,
            owner=OWNER,
            gtf_object_ids=[a1.id, a2.id],
        )


async def test_succeeds_with_two_assemblies(monkeypatch):
    from unittest.mock import MagicMock

    from beanie import PydanticObjectId

    async def _stub_enqueue(*args, **kwargs):
        j = MagicMock()
        j.id = PydanticObjectId()
        return j

    monkeypatch.setattr("app.queue.queue.enqueue", _stub_enqueue)
    from app.pipelines import tools

    monkeypatch.setattr(tools, "require", lambda tool: tool)

    project, [a1, a2] = await _setup_assemblies(count=2)

    job = await pipeline_service.launch_merge_transcripts(
        project_id=project.id,
        owner=OWNER,
        gtf_object_ids=[a1.id, a2.id],
    )
    assert job is not None


def test_merge_dedup_key_is_order_independent():
    from bson import ObjectId

    from app.services.pipeline_service import _merge_transcripts_dedup_key

    g1, g2, g3 = (ObjectId() for _ in range(3))
    assert _merge_transcripts_dedup_key(gtf_ids=[g1, g2, g3]) == (
        _merge_transcripts_dedup_key(gtf_ids=[g3, g1, g2])
    )


def test_merge_dedup_key_distinguishes_different_sets():
    from bson import ObjectId

    from app.services.pipeline_service import _merge_transcripts_dedup_key

    a, b, c = (ObjectId() for _ in range(3))
    assert _merge_transcripts_dedup_key(gtf_ids=[a, b]) != _merge_transcripts_dedup_key(
        gtf_ids=[a, c]
    )
