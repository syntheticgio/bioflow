"""Ingest launches annotation analysis without being asked.

Two triggers, because one loses a race. An NCBI download stages a genome
and its GFF concurrently and the FASTA's REFERENCE role is assigned after a
network lookup, so an annotation finishing first resolves no reference and
is analyzed with no contig lengths -- null coverage, no track axis, and
nothing saying why. Trigger 2 repairs that when the reference lands.
"""

import pytest
from app.config import settings
from app.models.object import (
    DataObject,
    FormatInfo,
    FormatKind,
    ObjectRole,
    ObjectStatus,
)
from app.queue import results as results_mod
from beanie import init_beanie
from pymongo import AsyncMongoClient

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


class TestTriggerTwo:
    async def test_a_reference_backfills_a_never_analyzed_annotation(
        self, _db, launched
    ):
        ann = await _obj("a.gff", FormatKind.GFF)
        ref = await _obj("g.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert launched == [str(ann.id)]

    async def test_a_reference_repairs_a_referenceless_analysis(self, _db, launched):
        # The race: this annotation was analyzed before its genome existed,
        # so its coverage is null everywhere.
        ann = await _obj(
            "a.gff",
            FormatKind.GFF,
            facts={
                "annotation_stats_status": "ok",
                "annotation_contig_lengths_known": False,
            },
        )
        ref = await _obj("g.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert launched == [str(ann.id)]

    async def test_a_fully_analyzed_annotation_is_left_alone(self, _db, launched):
        await _obj(
            "a.gff",
            FormatKind.GFF,
            facts={
                "annotation_stats_status": "ok",
                "annotation_contig_lengths_known": True,
            },
        )
        ref = await _obj("g.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert launched == []

    async def test_backfill_skips_sidecars(self, _db, launched):
        # The 8 misdetected .fai/.ann files must not be swept up by the
        # backfill either -- the guard has to hold on both trigger paths.
        parent = await _obj("g.fna", FormatKind.FASTA)
        await _obj("g.fna.fai", FormatKind.BED, sidecar_of=parent.id)
        ref = await _obj("ref.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert launched == []

    async def test_a_fasta_without_the_reference_role_backfills_nothing(
        self, _db, launched
    ):
        await _obj("a.gff", FormatKind.GFF)
        plain = await _obj("protein.faa", FormatKind.FASTA)

        await results_mod._auto_analyze_after_ingest(plain, owner="local")

        assert launched == []

    async def test_backfill_is_scoped_to_the_reference_s_project(self, _db, launched):
        other = DataObject(
            project_id="507f1f77bcf86cd799439099",
            name="elsewhere.gff",
            size=10,
            status=ObjectStatus.READY,
            format=FormatInfo(kind=FormatKind.GFF),
        )
        await other.insert()
        ref = await _obj("g.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert launched == []

    async def test_one_failing_backfill_does_not_stop_the_others(
        self, _db, monkeypatch
    ):
        from app.errors import ValidationError
        from app.services import pipeline_service

        seen: list = []

        async def flaky(*, object_id, owner):
            seen.append(str(object_id))
            if len(seen) == 1:
                raise ValidationError("no")
            return type("Job", (), {"id": "j"})()

        monkeypatch.setattr(pipeline_service, "launch_annotation_stats", flaky)
        await _obj("a1.gff", FormatKind.GFF)
        await _obj("a2.gff", FormatKind.GFF)
        ref = await _obj("g.fna", FormatKind.FASTA, role=ObjectRole.REFERENCE)

        await results_mod._auto_analyze_after_ingest(ref, owner="local")

        assert len(seen) == 2


async def test_the_automatic_path_shares_the_button_s_dedup_key(_db, monkeypatch):
    """A manual "Compute results" click during an automatic run must not
    create a second job. Both paths call launch_annotation_stats, whose key
    is annotation_stats:{id} -- capture it rather than trusting that."""
    from app.queue import queue as queue_module
    from app.services import pipeline_service

    keys: list = []

    async def fake_enqueue(name, **kwargs):
        keys.append(kwargs.get("dedup_key"))
        return type("Job", (), {"id": "j"})()

    async def fake_get_object(object_id, *, owner):
        return await DataObject.get(object_id)

    async def fake_resolve_readable(o):
        return None, "/tmp/a.gff"

    monkeypatch.setattr(queue_module, "enqueue", fake_enqueue)
    monkeypatch.setattr(pipeline_service.object_service, "get_object", fake_get_object)
    monkeypatch.setattr(pipeline_service, "_resolve_readable", fake_resolve_readable)

    ann = await _obj("a.gff", FormatKind.GFF)

    # The automatic path.
    await results_mod._auto_analyze_after_ingest(ann, owner="local")
    # The button.
    await pipeline_service.launch_annotation_stats(object_id=ann.id, owner="local")

    assert keys == [f"annotation_stats:{ann.id}"] * 2
