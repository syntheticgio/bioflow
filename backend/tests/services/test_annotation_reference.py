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


class TestAnnotationReferenceInvariant:
    def test_rejects_both_reference_and_reason_set(self):
        obj = _Obj("gen", role=ObjectRole.REFERENCE)
        with pytest.raises(AssertionError):
            pipeline_service.AnnotationReference(reference=obj, reason="some text")

    def test_rejects_neither_reference_nor_reason_set(self):
        with pytest.raises(AssertionError):
            pipeline_service.AnnotationReference()


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


class TestAccessionTier:
    @pytest.fixture
    def project(self, monkeypatch, objects):
        """A project whose object list the accession tier scans."""
        listed: list = []

        async def fake_list(project_id, *, owner, limit=500, status=None):
            return listed

        monkeypatch.setattr(
            pipeline_service.object_service, "list_objects", fake_list
        )
        return listed

    async def test_matches_a_reference_with_the_same_accession(self, objects, project):
        genome = _Obj("gen", role=ObjectRole.REFERENCE,
                      facts={"ncbi_assembly_accession": "GCF_000001405.39"})
        project.append(genome)
        ann = _Obj("ann", kind=FormatKind.GFF,
                   facts={"ncbi_assembly_accession": "GCF_000001405.39"})

        got = await pipeline_service.resolve_annotation_reference(ann)
        assert got.reference is genome
        assert got.reason is None

    async def test_ignores_a_protein_fasta_with_the_same_accession(self, objects, project):
        # An NCBI genome download brings protein.faa and
        # cds_from_genomic.fna alongside the genome FASTA, and all three
        # carry the same accession. Filtering on FormatKind.FASTA alone
        # resolved 3 of 5 real annotations to protein.faa on this machine's
        # database -- list_objects's newest-first order has no reason to
        # prefer the genome. Listed first here so a role check regression
        # cannot pass by accident of iteration order.
        protein = _Obj("prot", kind=FormatKind.FASTA, role=ObjectRole.PROTEIN,
                       facts={"ncbi_assembly_accession": "GCF_9.1"})
        genome = _Obj("gen", role=ObjectRole.REFERENCE,
                      facts={"ncbi_assembly_accession": "GCF_9.1"})
        project.extend([protein, genome])
        ann = _Obj("ann", kind=FormatKind.GFF,
                   facts={"ncbi_assembly_accession": "GCF_9.1"})

        got = await pipeline_service.resolve_annotation_reference(ann)
        assert got.reference is genome

    async def test_version_suffixes_do_not_match(self, objects, project):
        # .39 and .40 are different assemblies with different coordinates.
        project.append(_Obj("gen", role=ObjectRole.REFERENCE,
                            facts={"ncbi_assembly_accession": "GCF_000001405.40"}))
        ann = _Obj("ann", kind=FormatKind.GFF,
                   facts={"ncbi_assembly_accession": "GCF_000001405.39"})

        got = await pipeline_service.resolve_annotation_reference(ann)
        assert got.reference is None
        assert "no reference" in got.reason.lower()

    async def test_gca_and_gcf_counterparts_do_not_match(self, objects, project):
        project.append(_Obj("gen", role=ObjectRole.REFERENCE,
                            facts={"ncbi_assembly_accession": "GCA_000001405.39"}))
        ann = _Obj("ann", kind=FormatKind.GFF,
                   facts={"ncbi_assembly_accession": "GCF_000001405.39"})

        assert (await pipeline_service.resolve_annotation_reference(ann)).reference is None

    async def test_provenance_wins_over_accession(self, objects, project):
        provenance = _Obj("gen", role=ObjectRole.REFERENCE)
        objects["gen"] = provenance
        project.append(_Obj("other", role=ObjectRole.REFERENCE,
                            facts={"ncbi_assembly_accession": "GCF_9.1"}))
        ann = _Obj("ann", kind=FormatKind.GFF, derived_from=["gen"],
                   facts={"ncbi_assembly_accession": "GCF_9.1"})

        got = await pipeline_service.resolve_annotation_reference(ann)
        assert got.reference is provenance

    async def test_non_fasta_candidates_are_ignored(self, objects, project):
        # A VCF carrying the same accession is not a coordinate axis.
        project.append(_Obj("vcf", kind=FormatKind.VCF,
                            facts={"ncbi_assembly_accession": "GCF_9.1"}))
        ann = _Obj("ann", kind=FormatKind.GFF,
                   facts={"ncbi_assembly_accession": "GCF_9.1"})

        assert (await pipeline_service.resolve_annotation_reference(ann)).reference is None

    async def test_refuses_when_the_annotation_has_no_accession(self, objects, project):
        project.append(_Obj("gen", role=ObjectRole.REFERENCE,
                            facts={"ncbi_assembly_accession": "GCF_9.1"}))
        ann = _Obj("ann", kind=FormatKind.GFF, facts={})

        got = await pipeline_service.resolve_annotation_reference(ann)
        assert got.reference is None
        assert got.reason
