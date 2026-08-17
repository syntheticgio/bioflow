"""annotation_contig_lengths_known round-trips launcher -> payload -> facts.

The fact exists so the ingest backfill can find annotations analyzed before
their reference was READY with a flat equality match, rather than an
$elemMatch over annotation_per_contig -- an array holding one entry per
contig, thousands of them on a scaffolded assembly. It also distinguishes
"no reference at all" from "reference resolved but missing this contig",
which are different situations that should not be repaired the same way.
"""

import pytest
from app.models.object import FormatKind, ObjectRole, ObjectStatus
from app.queue import queue as queue_module
from app.services import pipeline_service


class _Obj:
    def __init__(self, oid, kind=FormatKind.GFF, role=None, facts=None,
                 derived_from=None, project_id="p1", owner="o",
                 status=ObjectStatus.READY):
        self.id = oid
        self.format = type("F", (), {"kind": kind})()
        self.role = role
        self.facts = facts or {}
        self.derived_from = derived_from or []
        self.project_id = project_id
        self.owner = owner
        self.status = status
        self.name = str(oid)


@pytest.fixture
def wired(monkeypatch):
    """Same stub set as test_annotation_stats_reference_wiring.py."""
    objects: dict = {}
    listed: list = []
    enqueued: dict = {}

    async def fake_get_object(object_id, *, owner):
        return objects[object_id]

    async def fake_get(oid):
        return objects.get(oid)

    async def fake_list_objects(project_id, *, owner, limit=500, status=None):
        return listed

    async def fake_resolve_readable(obj):
        return None, "/tmp/ann.gff3"

    async def fake_enqueue(name, **kwargs):
        enqueued.update(kwargs)
        return type("Job", (), {"id": "job1"})()

    monkeypatch.setattr(pipeline_service.object_service, "get_object", fake_get_object)
    monkeypatch.setattr(pipeline_service.object_service, "list_objects", fake_list_objects)
    monkeypatch.setattr(pipeline_service.DataObject, "get", staticmethod(fake_get))
    monkeypatch.setattr(pipeline_service, "_resolve_readable", fake_resolve_readable)
    monkeypatch.setattr(pipeline_service, "_check_annotation_stats_callable", lambda ann: None)
    monkeypatch.setattr(queue_module, "enqueue", fake_enqueue)

    return objects, listed, enqueued


class TestLauncherRecordsIt:
    async def test_true_when_a_reference_resolves(self, wired):
        objects, listed, enqueued = wired
        genome = _Obj("gen", kind=FormatKind.FASTA, role=ObjectRole.REFERENCE,
                      facts={"ncbi_assembly_accession": "GCF_9.1",
                             "sequence_lengths": {"chr1": 1000}})
        listed.append(genome)
        objects["ann"] = _Obj("ann", facts={"ncbi_assembly_accession": "GCF_9.1"})

        await pipeline_service.launch_annotation_stats(object_id="ann", owner="o")

        assert enqueued["payload"]["contig_lengths_known"] is True

    async def test_false_when_nothing_resolves(self, wired):
        # The race this whole feature exists to survive: the annotation is
        # ready before its genome is.
        objects, listed, enqueued = wired
        objects["ann"] = _Obj("ann", facts={})

        await pipeline_service.launch_annotation_stats(object_id="ann", owner="o")

        assert enqueued["payload"]["contig_lengths_known"] is False


class TestHandlerReturnsIt:
    def test_facts_carry_the_flag(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.queue import annotation_handlers
        from app.queue.registry import JobContext

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        source = tmp_path / "a.gff"
        source.write_text(
            "##gff-version 3\n"
            "chr1\t.\tgene\t100\t900\t.\t+\t.\tID=g1;Name=BRCA1\n"
        )
        ctx = JobContext(
            job_id="j1",
            payload={
                "object_id": "507f1f77bcf86cd799439011",
                "format_kind": "gff",
                "annotation_path": str(source),
                "contig_lengths": [["chr1", 1000]],
                "contig_lengths_known": True,
            },
            epoch=1,
            attempts=1,
            owner="local",
        )

        result = annotation_handlers.run_annotation_stats(ctx)

        assert result["facts"]["annotation_contig_lengths_known"] is True

    def test_flag_is_false_when_the_payload_says_so(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.queue import annotation_handlers
        from app.queue.registry import JobContext

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        source = tmp_path / "a.gff"
        source.write_text(
            "##gff-version 3\n"
            "chr1\t.\tgene\t100\t900\t.\t+\t.\tID=g1;Name=BRCA1\n"
        )
        ctx = JobContext(
            job_id="j2",
            payload={
                "object_id": "507f1f77bcf86cd799439012",
                "format_kind": "gff",
                "annotation_path": str(source),
                "contig_lengths": [],
                "contig_lengths_known": False,
            },
            epoch=1,
            attempts=1,
            owner="local",
        )

        result = annotation_handlers.run_annotation_stats(ctx)

        assert result["facts"]["annotation_contig_lengths_known"] is False
