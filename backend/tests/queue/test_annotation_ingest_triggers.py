"""Ingest launches annotation analysis without being asked.

Two triggers, because one loses a race. An NCBI download stages a genome
and its GFF concurrently and the FASTA's REFERENCE role is assigned after a
network lookup, so an annotation finishing first resolves no reference and
is analyzed with no contig lengths -- null coverage, no track axis, and
nothing saying why. Trigger 2 repairs that when the reference lands.
"""

import pytest
from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models.object import (
    DataObject,
    FormatInfo,
    FormatKind,
    ObjectRole,
    ObjectStatus,
)
from app.queue import results as results_mod

PROJECT = "507f1f77bcf86cd799439011"


@pytest.fixture
async def _db():
    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=[DataObject])
    await DataObject.delete_all()
    yield db
    await DataObject.delete_all()
    await client.close()


@pytest.fixture
def launched(monkeypatch):
    """Capture every object id handed to launch_annotation_stats."""
    calls: list = []

    async def fake_launch(*, object_id, owner):
        calls.append(str(object_id))
        return type("Job", (), {"id": "job1"})()

    from app.services import pipeline_service

    monkeypatch.setattr(pipeline_service, "launch_annotation_stats", fake_launch)
    return calls


async def _obj(
    name,
    kind,
    *,
    status=ObjectStatus.READY,
    role=None,
    sidecar_of=None,
    facts=None,
) -> DataObject:
    o = DataObject(
        project_id=PROJECT,
        name=name,
        size=1024,
        status=status,
        role=role,
        sidecar_of=sidecar_of,
        format=FormatInfo(kind=kind),
        facts=facts or {},
    )
    await o.insert()
    return o


class TestTriggerOne:
    @pytest.mark.parametrize(
        "kind",
        [FormatKind.GFF, FormatKind.GTF, FormatKind.BED, FormatKind.GENBANK],
    )
    async def test_an_ingested_annotation_is_analyzed(self, _db, launched, kind):
        ann = await _obj(f"a.{kind.value}", kind)

        await results_mod._auto_analyze_after_ingest(ann, owner="local")

        assert launched == [str(ann.id)]

    async def test_a_sidecar_is_not_analyzed(self, _db, launched):
        # A .fai misdetected as BED -- 8 of these sit on the author's real
        # database. This is the direction that fails when the guard breaks.
        ref = await _obj("g.fna", FormatKind.FASTA)
        fai = await _obj("g.fna.fai", FormatKind.BED, sidecar_of=ref.id)

        await results_mod._auto_analyze_after_ingest(fai, owner="local")

        assert launched == []

    async def test_a_non_annotation_is_not_analyzed(self, _db, launched):
        bam = await _obj("s.bam", FormatKind.BAM)

        await results_mod._auto_analyze_after_ingest(bam, owner="local")

        assert launched == []


class TestFailureIsolation:
    async def test_a_failing_launch_leaves_the_object_ready(self, _db, monkeypatch):
        from app.errors import ValidationError
        from app.services import pipeline_service

        async def boom(*, object_id, owner):
            raise ValidationError("no")

        monkeypatch.setattr(pipeline_service, "launch_annotation_stats", boom)
        ann = await _obj("a.gff", FormatKind.GFF)

        # Must not raise: a malformed annotation cannot turn a successfully
        # stored source file into an ingest failure.
        await results_mod._auto_analyze_after_ingest(ann, owner="local")

        again = await DataObject.get(ann.id)
        assert again.status is ObjectStatus.READY
