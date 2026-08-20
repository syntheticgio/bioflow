"""Provenance helpers in app.queue.results, tested as pure functions over dicts.

Salmon and featureCounts write to the same role (COUNTS) and the same file
format, so `counted_by` is the only thing on the object itself that tells
them apart -- see `salmon_provenance`'s docstring. These tests exercise that
distinction directly rather than through the applier, which needs a live DB.
"""

import uuid

import pytest

from app.config import settings
from app.models.object import ObjectRole
from app.queue import results


class TestSalmonProvenance:
    def test_records_salmon_as_the_quantifier(self):
        prov = results.salmon_provenance(
            {
                "tool_version": "1.10.2",
                "annotation_name": "cds.fna",
                "annotation_sha256": "deadbeef",
                "facts": {"genes_detected": 12},
            }
        )
        # counts_provenance writes counted_by="featurecounts"; the two paths
        # must be distinguishable on the object itself, not only by which job
        # produced it.
        assert prov["counted_by"] == "salmon"
        assert prov["salmon_version"] == "1.10.2"
        assert prov["annotation_sha256"] == "deadbeef"
        assert prov["genes_detected"] == 12

    def test_transcriptome_name_is_carried_for_the_merge_error_message(self):
        prov = results.salmon_provenance(
            {"annotation_name": "cds.fna", "annotation_sha256": "x", "facts": {}}
        )
        assert prov["annotation_name"] == "cds.fna"


def test_transcript_assembly_applier_is_registered():
    """_APPLIERS is hand-maintained and silently skips unknown job types: a
    missing entry means the job succeeds and no object is ever created."""
    from app.queue.results import _APPLIERS

    assert "transcript_assembly" in _APPLIERS
    assert _APPLIERS["transcript_assembly"] is results._apply_transcript_assembly


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
async def test_apply_transcript_assembly_ingests_gtf_with_assembled_role(
    tmp_path, monkeypatch
):
    """The produced GTF must not be indistinguishable from a downloaded one.

    Asserting the role explicitly because it is the whole point of the
    applier: a StringTie GTF ingested as ANNOTATION would become a candidate
    reference for featureCounts and for StringTie's own -G.
    """
    from beanie import PydanticObjectId

    from app.services import object_service, project_service

    monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    settings.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
    settings.sentinel_path.write_text("biopipe-home-v1\n")
    owner = "transcript-assembly-owner"

    async def _skip_ingest(obj, **kwargs):
        return ""

    async def _skip_enqueue(*args, **kwargs):
        return None

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
    monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)

    project = await project_service.create_project(name=f"{owner}-project", owner=owner)
    bam_path = tmp_path / f"alignment-{uuid.uuid4().hex}.bam"
    bam_path.write_bytes(uuid.uuid4().bytes)
    source = await object_service.ingest_local_file(
        owner=owner,
        project_id=project.id,
        path=bam_path,
        name="alignment.bam",
        role=ObjectRole.ALIGNMENT,
    )

    gtf = tmp_path / "sample.transcripts.gtf"
    gtf.write_text("# StringTie version 2.2.1\n")

    await results._apply_transcript_assembly(
        {
            "object_id": str(source.id),
            "job_id": str(PydanticObjectId()),
            "output": {"tmp_path": str(gtf), "name": gtf.name},
            "assembled_by": "stringtie",
            "transcript_count": 12,
            "novel_transcript_count": 3,
            "gene_count": 9,
        },
        owner=source.owner,
    )

    from app.models.object import DataObject

    assembled = await DataObject.find_one(DataObject.derived_from == source.id)
    assert assembled is not None
    assert assembled.role == ObjectRole.ASSEMBLED_TRANSCRIPTS
    assert assembled.facts["assembled_by"] == "stringtie"
    assert assembled.facts["novel_transcript_count"] == 3
