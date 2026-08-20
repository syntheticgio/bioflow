"""Launch unit tests for variants_in_regions, annotation_comparison, and sequence_extraction.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from beanie import PydanticObjectId

from app.models import FormatKind, ObjectStatus, SidecarRole
from app.services import pipeline_service


def _fake_obj(obj_id=None, name="file.dat", kind=FormatKind.FASTA, role=None, project_id=None):
    return SimpleNamespace(
        id=obj_id or PydanticObjectId(),
        name=name,
        format=SimpleNamespace(kind=kind),
        status=ObjectStatus.READY,
        role=role,
        facts={},
        metadata={},
        blob_sha256="a" * 64,
        project_id=project_id or PydanticObjectId(),
        owner="test",
    )


@pytest.mark.asyncio
async def test_launch_variants_in_regions_enqueues_payload():
    vcf = _fake_obj(name="vars.vcf", kind=FormatKind.VCF)
    ref = _fake_obj(name="ref.fa", kind=FormatKind.FASTA)
    anno = _fake_obj(name="anno.gff", kind=FormatKind.GFF)
    fai = _fake_obj(name="ref.fa.fai", kind=FormatKind.FAI)

    async def fake_get_object(obj_id, owner):
        if str(obj_id) == str(vcf.id):
            return vcf
        return anno

    async def fake_resolve_ref(*args, **kwargs):
        return ref

    async def fake_resolve_anno(*args, **kwargs):
        return anno

    async def fake_sidecar(reference, role):
        if role == SidecarRole.FAI:
            return fai
        return None

    async def fake_readable(obj):
        return ("sha256-" + obj.name, "/path/" + obj.name)

    captured = {}

    async def fake_enqueue(job_name, owner, payload, **kwargs):
        captured["job_name"] = job_name
        captured["payload"] = payload
        return SimpleNamespace(id=PydanticObjectId())

    with (
        patch("app.services.object_service.get_object", side_effect=fake_get_object),
        patch(
            "app.services.pipeline_service._resolve_variant_reference",
            side_effect=fake_resolve_ref,
        ),
        patch("app.services.pipeline_service.resolve_annotation", side_effect=fake_resolve_anno),
        patch("app.services.pipeline_service._sidecar_of_role", side_effect=fake_sidecar),
        patch("app.services.pipeline_service._resolve_readable", side_effect=fake_readable),
        patch("app.queue.queue.enqueue", side_effect=fake_enqueue),
    ):
        job = await pipeline_service.launch_variants_in_regions(vcf_id=vcf.id, owner="test")
        assert job is not None
        assert captured["job_name"] == "variants_in_regions"
        assert captured["payload"]["vcf_id"] == str(vcf.id)
        assert captured["payload"]["annotation_id"] == str(anno.id)


@pytest.mark.asyncio
async def test_launch_annotation_comparison_enqueues_payload():
    proj_id = PydanticObjectId()
    anno_a = _fake_obj(
        obj_id=PydanticObjectId(), name="a.gff", kind=FormatKind.GFF, project_id=proj_id
    )
    anno_b = _fake_obj(
        obj_id=PydanticObjectId(), name="b.gff", kind=FormatKind.GFF, project_id=proj_id
    )

    async def fake_get_object(obj_id, owner):
        if str(obj_id) == str(anno_a.id):
            return anno_a
        return anno_b

    async def fake_readable(obj):
        return ("sha256-" + obj.name, "/path/" + obj.name)

    captured = {}

    async def fake_enqueue(job_name, owner, payload, **kwargs):
        captured["job_name"] = job_name
        captured["payload"] = payload
        return SimpleNamespace(id=PydanticObjectId())

    with (
        patch("app.services.object_service.get_object", side_effect=fake_get_object),
        patch("app.services.pipeline_service._resolve_readable", side_effect=fake_readable),
        patch("app.queue.queue.enqueue", side_effect=fake_enqueue),
    ):
        job = await pipeline_service.launch_annotation_comparison(
            annotation_id=anno_a.id, other_annotation_id=anno_b.id, owner="test"
        )
        assert job is not None
        assert captured["job_name"] == "annotation_comparison"
        assert captured["payload"]["annotation_a_id"] == str(anno_a.id)
        assert captured["payload"]["annotation_b_id"] == str(anno_b.id)


@pytest.mark.asyncio
async def test_launch_sequence_extraction_enqueues_payload():
    assembly = _fake_obj(name="ref.fa", kind=FormatKind.FASTA)
    fai = _fake_obj(name="ref.fa.fai", kind=FormatKind.FAI)

    async def fake_get_object(obj_id, owner):
        return assembly

    async def fake_sidecar(reference, role):
        return fai

    async def fake_readable(obj):
        return ("sha256-" + obj.name, "/path/" + obj.name)

    captured = {}

    async def fake_enqueue(job_name, owner, payload, **kwargs):
        captured["job_name"] = job_name
        captured["payload"] = payload
        return SimpleNamespace(id=PydanticObjectId())

    with (
        patch("app.services.object_service.get_object", side_effect=fake_get_object),
        patch("app.services.pipeline_service._sidecar_of_role", side_effect=fake_sidecar),
        patch("app.services.pipeline_service._resolve_readable", side_effect=fake_readable),
        patch("app.pipelines.mosdepth_runner.contig_lengths_from_fai", return_value={"chr1": 1000}),
        patch("app.queue.queue.enqueue", side_effect=fake_enqueue),
    ):
        job = await pipeline_service.launch_sequence_extraction(
            assembly_id=assembly.id, query_text="chr1:100-500", owner="test"
        )
        assert job is not None
        assert captured["job_name"] == "sequence_extraction"
        assert captured["payload"]["assembly_id"] == str(assembly.id)
        assert captured["payload"]["regions"] == [("chr1", 99, 500)]
