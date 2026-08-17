"""launch_annotation_stats must resolve contig lengths through the same
two-tier resolver the track viewer uses, not just the provenance tier.

Found during Task 10's browser verification of #295: launch_annotation_stats
called _reference_for_annotation directly, so an annotation with no
derived_from (the common case for an NCBI download with nothing derived from
it in-app) always got contig_lengths=[] even when a matching-accession
reference FASTA sat in the same project -- resolve_annotation_reference
would have found it. The track viewer's "no coordinate axis" refusal fired
for every annotation in a real seeded dataset because of this.
"""

import pytest
from app.models.object import FormatKind, ObjectRole, ObjectStatus
from app.queue import queue as queue_module
from app.services import pipeline_service


class _Obj:
    """A DataObject stand-in carrying only the fields the launcher reads."""

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
    """Stub every collaborator launch_annotation_stats calls except the
    resolver under test, and capture the payload handed to queue.enqueue."""
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


class TestContigLengthsResolution:
    async def test_resolves_lengths_via_accession_match_with_no_provenance(self, wired):
        # The common case: an NCBI-downloaded annotation with nothing
        # derived from it in-app, so derived_from is empty and only the
        # accession tier can find the reference.
        objects, listed, enqueued = wired
        genome = _Obj("gen", kind=FormatKind.FASTA, role=ObjectRole.REFERENCE,
                      facts={"ncbi_assembly_accession": "GCF_9.1",
                             "sequence_lengths": {"chr1": 1000}})
        listed.append(genome)
        ann = _Obj("ann", derived_from=[],
                   facts={"ncbi_assembly_accession": "GCF_9.1"})
        objects["ann"] = ann

        await pipeline_service.launch_annotation_stats(object_id="ann", owner="o")

        assert enqueued["payload"]["contig_lengths"] == [["chr1", 1000]]

    async def test_provenance_tier_still_works(self, wired):
        objects, listed, enqueued = wired
        genome = _Obj("gen", kind=FormatKind.FASTA, role=ObjectRole.REFERENCE,
                      facts={"sequence_lengths": {"chr1": 500}})
        objects["gen"] = genome
        ann = _Obj("ann", derived_from=["gen"])
        objects["ann"] = ann

        await pipeline_service.launch_annotation_stats(object_id="ann", owner="o")

        assert enqueued["payload"]["contig_lengths"] == [["chr1", 500]]

    async def test_empty_when_neither_tier_resolves(self, wired):
        objects, listed, enqueued = wired
        ann = _Obj("ann", derived_from=[], facts={})
        objects["ann"] = ann

        await pipeline_service.launch_annotation_stats(object_id="ann", owner="o")

        assert enqueued["payload"]["contig_lengths"] == []
