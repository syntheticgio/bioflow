import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.errors import ValidationError
from app.models import DataObject, FormatInfo, FormatKind, ObjectRole, SidecarRole
from app.services import object_service, pipeline_service, project_service

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
    await vcf1.set({DataObject.format: FormatInfo(kind=FormatKind.VCF)})
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
    await vcf2.set({DataObject.format: FormatInfo(kind=FormatKind.VCF)})
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

    # What this test asserts is the reference-agreement check and the enqueue,
    # neither of which is about whether sniffles is on PATH. Left unstubbed,
    # `tools.require` reads the host: fine in the backend image that ships
    # sniffles, a PermanentError on a CI runner that does not. Same stub as
    # tests/api/test_route_owner_scoping.py uses for the launch routes.
    from app.pipelines import tools

    monkeypatch.setattr(tools, "require", lambda tool: tool)

    snf1, snf2, ref1, _ = await _setup_snf_pair(same_reference=True)

    job = await pipeline_service.launch_merge_structural_variants(
        snf_object_ids=[snf1.id, snf2.id],
        owner=OWNER,
    )
    assert job is not None


async def test_sibling_snf_callsets_finds_all_snf_sidecars_on_same_reference():
    snf1, snf2, ref1, _ = await _setup_snf_pair(same_reference=True)

    siblings = await pipeline_service.sibling_snf_callsets(snf1)

    assert len(siblings) == 2
    assert str(snf1.id) in siblings
    assert str(snf2.id) in siblings
    # Sorted, as the function promises.
    assert siblings == sorted(siblings)


async def test_sibling_snf_callsets_excludes_snfs_on_different_reference():
    snf1, snf2, ref1, ref2 = await _setup_snf_pair(same_reference=False)

    siblings = await pipeline_service.sibling_snf_callsets(snf1)

    # Only snf1 is on ref1; snf2 is on ref2, so it is excluded.
    assert len(siblings) == 1
    assert str(snf1.id) in siblings


async def test_sibling_snf_callsets_returns_empty_when_not_an_snf():
    project = await project_service.create_project(
        name=f"proj-{uuid.uuid4().hex}", owner=OWNER
    )
    ref = await object_service.ingest_local_file(
        owner=OWNER,
        project_id=project.id,
        path=_scratch_file(suffix=".fa"),
        name="ref.fa",
        role=ObjectRole.REFERENCE,
    )
    plain_vcf = await object_service.ingest_local_file(
        owner=OWNER,
        project_id=project.id,
        path=_scratch_file(suffix=".vcf.gz"),
        name="sample.vcf.gz",
        role=ObjectRole.VARIANTS,
        derived_from=[ref.id],
    )
    await plain_vcf.set({DataObject.format: FormatInfo(kind=FormatKind.VCF)})

    # A plain VCF (not an SNF sidecar) has no sidecar_of, so the reference
    # walk fails and the function returns an empty list.
    siblings = await pipeline_service.sibling_snf_callsets(plain_vcf)
    assert siblings == []
