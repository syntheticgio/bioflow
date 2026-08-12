"""Which reference supplies an annotation's coordinate axis.

Untested before the track viewer, which is why the role-preference bug in
_reference_for_annotation survived: a wrong reference cost #257 only a
slightly-off coverage percentage, but costs the viewer a coordinate ruler
that is silently wrong.
"""

import pytest

from app.models.object import FormatKind, ObjectRole
from app.services import pipeline_service


class _Obj:
    """A DataObject stand-in: only the fields resolution reads."""

    def __init__(self, oid, kind=FormatKind.FASTA, role=None, facts=None,
                 derived_from=None, project_id="p1", owner="o"):
        self.id = oid
        self.format = type("F", (), {"kind": kind})()
        self.role = role
        self.facts = facts or {}
        self.derived_from = derived_from or []
        self.project_id = project_id
        self.owner = owner
        self.name = str(oid)


@pytest.fixture
def objects(monkeypatch):
    """Register objects DataObject.get resolves by id."""
    store: dict = {}

    async def fake_get(oid):
        return store.get(oid)

    monkeypatch.setattr(pipeline_service.DataObject, "get", staticmethod(fake_get))
    return store


class TestProvenanceTier:
    async def test_prefers_the_reference_role_over_a_bare_fasta(self, objects):
        # The bug: a protein FASTA listed first was returned as the reference.
        protein = _Obj("prot", role=ObjectRole.PROTEIN)
        genome = _Obj("gen", role=ObjectRole.REFERENCE)
        objects.update({"prot": protein, "gen": genome})
        ann = _Obj("ann", kind=FormatKind.GFF, derived_from=["prot", "gen"])

        got = await pipeline_service._reference_for_annotation(ann)
        assert got is genome

    async def test_falls_back_to_bare_fasta_when_no_role_is_set(self, objects):
        plain = _Obj("plain", role=None)
        objects["plain"] = plain
        ann = _Obj("ann", kind=FormatKind.GFF, derived_from=["plain"])

        assert await pipeline_service._reference_for_annotation(ann) is plain

    async def test_ignores_non_fasta_parents(self, objects):
        bam = _Obj("bam", kind=FormatKind.BAM)
        objects["bam"] = bam
        ann = _Obj("ann", kind=FormatKind.GFF, derived_from=["bam"])

        assert await pipeline_service._reference_for_annotation(ann) is None

    async def test_returns_none_with_no_provenance(self, objects):
        ann = _Obj("ann", kind=FormatKind.GFF, derived_from=[])
        assert await pipeline_service._reference_for_annotation(ann) is None
