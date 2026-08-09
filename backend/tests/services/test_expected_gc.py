"""The expected-GC cascade.

Tier 1 (a measured project reference) has its own tests in this file once the
database seam exists; these cover the table and the resolution order.
"""

import pytest

from app.services import expected_gc


class TestGenomeTable:
    def test_every_entry_carries_a_citation(self):
        """The reason the table is allowed to exist at all. A GC percentage
        drawn as an authoritative reference curve with no source is the
        fabricated-value failure TOOL_META's required fields exist to stop."""
        for key, entry in expected_gc.GENOME_GC.items():
            assert entry.citation, f"{key} has no citation"
            assert entry.source_name, f"{key} has no source_name"

    def test_every_value_is_a_plausible_percentage(self):
        for key, entry in expected_gc.GENOME_GC.items():
            assert 0 < entry.percent < 100, f"{key} has an impossible GC"

    def test_keys_are_already_normalized(self):
        """Lookup normalizes the user's input, not the table. A table key that
        is not already normalized is unreachable, silently."""
        from app.models import normalize_organism

        for key in expected_gc.GENOME_GC:
            assert key == normalize_organism(key)


class TestFromOrganism:
    def test_resolves_a_known_organism(self):
        got = expected_gc.from_organism("Homo sapiens")
        assert got is not None
        assert got.source == "table"
        assert 40 < got.percent < 42

    def test_is_case_and_whitespace_insensitive(self):
        """'homo sapiens' and 'Homo  sapiens' are one species; normalize_organism
        is what the OrganismBlurb cache already keys on."""
        a = expected_gc.from_organism("homo  sapiens")
        b = expected_gc.from_organism("Homo sapiens")
        assert a is not None and b is not None
        assert a.percent == b.percent

    def test_an_unknown_organism_resolves_to_nothing(self):
        assert expected_gc.from_organism("Nonexistent organism") is None

    def test_blank_input_resolves_to_nothing(self):
        assert expected_gc.from_organism("") is None
        assert expected_gc.from_organism(None) is None

    def test_attribution_names_the_organism_and_the_assembly(self):
        """The curve must always be able to say where its number came from.

        `source_name` lives on the table entry, not on the resolved
        ExpectedGc -- the resolved object carries only the finished
        attribution string, so this reads the table for the expected text."""
        got = expected_gc.from_organism("Escherichia coli K-12")
        assert got is not None
        assert "Escherichia coli K-12" in got.attribution
        assert expected_gc.GENOME_GC["escherichia coli k-12"].source_name in got.attribution

    def test_bare_species_does_not_match_a_strain_specific_entry(self):
        """GC genuinely varies by E. coli strain; the table is keyed to K-12
        specifically (see the comment on that entry), so an organism string
        with no strain must not silently pick up K-12's figure."""
        assert expected_gc.from_organism("Escherichia coli") is None


class FakeObject:
    """A stand-in for DataObject carrying only what the resolver reads."""

    def __init__(self, name, gc=None, role=None):
        from app.models import ObjectRole

        self.name = name
        self.facts = {} if gc is None else {"gc_content_percent": gc}
        self.role = ObjectRole(role) if role else None


class TestFromReferences:
    def test_measures_from_a_single_reference(self):
        refs = [FakeObject("GRCh38.fa", gc=40.9, role="reference")]
        got = expected_gc.from_references(refs)
        assert got is not None
        assert got.percent == 40.9
        assert got.source == "reference"

    def test_attribution_names_the_file_it_measured(self):
        """'expected 40.9%' with no provenance is a number the user cannot
        check. The filename is what makes it checkable."""
        refs = [FakeObject("GRCh38.fa", gc=40.9, role="reference")]
        assert "GRCh38.fa" in expected_gc.from_references(refs).attribution

    def test_ignores_a_protein_fasta(self):
        """The `protein.faa` mistake. A project that downloaded an NCBI
        assembly holds protein and CDS FASTA alongside the genome; their GC is
        not the genome's, and a curve drawn from one is confidently wrong."""
        refs = [
            FakeObject("protein.faa", gc=52.1, role="protein"),
            FakeObject("GRCh38.fa", gc=40.9, role="reference"),
        ]
        assert expected_gc.from_references(refs).percent == 40.9

    def test_a_project_of_only_protein_fasta_resolves_to_nothing(self):
        refs = [FakeObject("protein.faa", gc=52.1, role="protein")]
        assert expected_gc.from_references(refs) is None

    def test_ignores_a_transcript_fasta(self):
        refs = [FakeObject("cds_from_genomic.fna", gc=54.0, role="transcript")]
        assert expected_gc.from_references(refs) is None

    def test_a_reference_with_no_measured_gc_is_skipped(self):
        """Still ingesting, or a format fasta_stats found nothing in."""
        refs = [FakeObject("pending.fa", gc=None, role="reference")]
        assert expected_gc.from_references(refs) is None

    def test_two_copies_of_the_same_assembly_are_not_a_disagreement(self):
        """The 'same assembly stored twice' case: identical values are one
        answer, not two competing ones."""
        refs = [
            FakeObject("GRCh38.fa", gc=40.9, role="reference"),
            FakeObject("GRCh38_copy.fa", gc=40.9, role="reference"),
        ]
        assert expected_gc.from_references(refs).percent == 40.9

    def test_two_disagreeing_references_resolve_to_nothing(self):
        """Two genuinely different genomes in one project. There is no basis
        for picking one, and picking wrong draws an authoritative-looking
        curve for the wrong organism."""
        refs = [
            FakeObject("human.fa", gc=40.9, role="reference"),
            FakeObject("ecoli.fa", gc=50.8, role="reference"),
        ]
        assert expected_gc.from_references(refs) is None

    def test_no_objects_at_all(self):
        assert expected_gc.from_references([]) is None


class TestResolveOrder:
    def test_a_measured_reference_beats_the_table(self):
        """Tier 1 outranks tier 2: the user's own file is a measurement, the
        table is a published figure for a different assembly of the species."""
        refs = [FakeObject("my_ecoli.fa", gc=50.1, role="reference")]
        got = expected_gc.resolve(references=refs, organism="Escherichia coli")
        assert got.source == "reference"
        assert got.percent == 50.1

    def test_falls_through_to_the_table(self):
        got = expected_gc.resolve(references=[], organism="Escherichia coli K-12")
        assert got.source == "table"

    def test_falls_through_to_nothing(self):
        assert expected_gc.resolve(references=[], organism=None) is None


from app.models import ObjectRole
from tests.services.helpers import make_object, make_project


async def seed_reference(project, name, gc, role=ObjectRole.REFERENCE):
    """A stored object carrying a measured GC, as ingest would leave it."""
    obj = await make_object(project, name)
    obj.role = role
    obj.facts = {"gc_content_percent": gc}
    await obj.save()
    return obj


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
class TestReferencesForProject:
    """The database seam. Async because it queries; the resolver itself is
    pure and stays synchronous, which is why every other test here is too."""

    async def test_returns_the_projects_reference_objects(self, beanie_models):
        project = await make_project("gc-refs")
        await seed_reference(project, "GRCh38.fa", 40.9)
        found = await expected_gc.references_for_project(
            project_id=project.id, owner=project.owner
        )
        assert [o.name for o in found] == ["GRCh38.fa"]

    async def test_excludes_objects_with_no_role(self, beanie_models):
        """A FASTQ in the same project is not a reference genome."""
        project = await make_project("gc-mixed")
        await make_object(project, "reads_1.fastq")
        await seed_reference(project, "GRCh38.fa", 40.9)
        found = await expected_gc.references_for_project(
            project_id=project.id, owner=project.owner
        )
        assert [o.name for o in found] == ["GRCh38.fa"]

    async def test_does_not_cross_projects(self, beanie_models):
        """Another project's reference says nothing about this file."""
        mine = await make_project("gc-mine")
        theirs = await make_project("gc-theirs")
        await seed_reference(theirs, "ecoli.fa", 50.8)
        found = await expected_gc.references_for_project(
            project_id=mine.id, owner=mine.owner
        )
        assert found == []

    async def test_resolves_end_to_end_from_stored_objects(self, beanie_models):
        """The whole cascade against real documents rather than fakes -- the
        seam where a query that returns the wrong shape would still satisfy
        every unit test above it."""
        project = await make_project("gc-e2e")
        await seed_reference(project, "GRCh38.fa", 40.9)
        refs = await expected_gc.references_for_project(
            project_id=project.id, owner=project.owner
        )
        got = expected_gc.resolve(references=refs, organism=None)
        assert got is not None
        assert got.source == "reference"
        assert "GRCh38.fa" in got.attribution
