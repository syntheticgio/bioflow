import uuid
from pathlib import Path

import pytest

from app.errors import ValidationError
from app.models import DataObject, ObjectRole, SidecarRole
from app.services import object_service, pipeline_service, project_service
from app.config import settings

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

OWNER = "sv-merge-launch-owner"


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
    path = settings.tmp_dir / f"sv-merge-{uuid.uuid4().hex}{suffix}"
    path.write_bytes(uuid.uuid4().bytes)
    return path


async def _setup_snf_pair(*, same_reference: bool = True):
    project = await project_service.create_project(
        name=f"proj-{uuid.uuid4().hex}", owner=OWNER
    )

    ref1 = await object_service.ingest_local_file(
        owner=OWNER,
        project_id=project.id,
        path=_scratch_file(suffix=".fa"),
        name="ref1.fa",
        role=ObjectRole.REFERENCE,
    )

    ref2 = ref1 if same_reference else await object_service.ingest_local_file(
        owner=OWNER,
        project_id=project.id,
        path=_scratch_file(suffix=".fa"),
        name="ref2.fa",
        role=ObjectRole.REFERENCE,
    )

    vcf1 = await object_service.ingest_local_file(
        owner=OWNER,
        project_id=project.id,
        path=_scratch_file(suffix=".vcf.gz"),
        name="sample1.vcf.gz",
        role=ObjectRole.VARIANTS,
        derived_from=[ref1.id],
    )
    snf1 = await object_service.ingest_local_file(
        owner=OWNER,
        project_id=project.id,
        path=_scratch_file(suffix=".snf"),
        name="sample1.snf",
        role=ObjectRole.VARIANTS,
        derived_from=[vcf1.id],
        sidecar_of=vcf1.id,
        sidecar_role=SidecarRole.SNF,
    )

    vcf2 = await object_service.ingest_local_file(
        owner=OWNER,
        project_id=project.id,
        path=_scratch_file(suffix=".vcf.gz"),
        name="sample2.vcf.gz",
        role=ObjectRole.VARIANTS,
        derived_from=[ref2.id],
    )
    snf2 = await object_service.ingest_local_file(
        owner=OWNER,
        project_id=project.id,
        path=_scratch_file(suffix=".snf"),
        name="sample2.snf",
        role=ObjectRole.VARIANTS,
        derived_from=[vcf2.id],
        sidecar_of=vcf2.id,
        sidecar_role=SidecarRole.SNF,
    )

    return snf1, snf2, ref1, ref2


async def test_merge_structural_variants_refuses_differing_references(monkeypatch):
    snf1, snf2, ref1, ref2 = await _setup_snf_pair(same_reference=False)

    with pytest.raises(ValidationError) as exc_info:
        await pipeline_service.launch_merge_structural_variants(
            snf_object_ids=[snf1.id, snf2.id],
            owner=OWNER,
        )

    assert "Cannot merge SV callsets across differing reference assemblies" in str(exc_info.value)


async def test_merge_structural_variants_succeeds_on_same_reference(monkeypatch):
    async def _stub_enqueue(*args, **kwargs):
        from unittest.mock import MagicMock
        from beanie import PydanticObjectId
        j = MagicMock()
        j.id = PydanticObjectId()
        return j

    monkeypatch.setattr("app.queue.queue.enqueue", _stub_enqueue)

    snf1, snf2, ref1, _ = await _setup_snf_pair(same_reference=True)

    job = await pipeline_service.launch_merge_structural_variants(
        snf_object_ids=[snf1.id, snf2.id],
        owner=OWNER,
    )
    assert job is not None
