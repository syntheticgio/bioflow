"""What the assembly applier decides before touching the database.

The applier itself needs Mongo, so what is tested here is the pure mapping:
which role each staged component becomes, and what metadata ties the four
files to one assembly. That mapping is where a mistake is invisible until an
aligner offers a protein FASTA as a reference.
"""

from app.queue import results


class TestComponentRoleMapping:
    def test_each_component_becomes_its_role(self):
        staged = [
            {"name": "g.fna", "component": "genome", "role": "reference"},
            {"name": "genomic.gff", "component": "gff3", "role": "annotation"},
            {"name": "protein.faa", "component": "protein", "role": "protein"},
            {"name": "cds_from_genomic.fna", "component": "cds", "role": "transcript"},
        ]
        roles = {s["name"]: results._role_for_component(s) for s in staged}
        assert roles["g.fna"] == "reference"
        assert roles["genomic.gff"] == "annotation"
        assert roles["protein.faa"] == "protein"
        assert roles["cds_from_genomic.fna"] == "transcript"

    def test_an_unknown_component_gets_no_role(self):
        """None rather than a guess: an unroled file is merely uncategorized,
        while a wrongly-roled one is actively misleading."""
        assert results._role_for_component({"component": "mystery"}) is None


class TestComponentMetadata:
    def test_every_component_carries_the_assembly_accession(self):
        """The key that makes the four files find each other -- and what
        `already_downloaded` matches on."""
        meta = results._component_metadata(
            {"organism": "Trypanosoma brucei"}, "GCF_000002445.2", "protein"
        )
        assert meta["assembly_accession"] == "GCF_000002445.2"
        assert meta["organism"] == "Trypanosoma brucei"

    def test_reference_build_is_not_claimed_for_a_protein_set(self):
        """reference_build describes a genome. On a protein FASTA it would
        assert the file is an assembly, which is what the roles exist to deny."""
        meta = results._component_metadata(
            {"reference_build": "ASM244v1"}, "GCF_000002445.2", "protein"
        )
        assert "reference_build" not in meta

    def test_the_genome_keeps_its_build(self):
        meta = results._component_metadata(
            {"reference_build": "ASM244v1"}, "GCF_000002445.2", "genome"
        )
        assert meta["reference_build"] == "ASM244v1"

    def test_tax_id_assembly_date_and_paired_accession_are_not_claimed_for_a_protein_set(self):
        """These three are genome-specific in exactly the way reference_build
        is: they come from AssemblyMetadata.to_metadata() and are listed in
        REFERENCE_FIELDS, not SEQUENCE_SET_FIELDS. Leaving them on a protein or
        CDS FASTA would reintroduce the "genome metadata on a non-genome file"
        confusion via three different keys."""
        meta = results._component_metadata(
            {
                "tax_id": 9913,
                "assembly_date": "2018-04-11",
                "paired_accession": "GCA_000002445.2",
            },
            "GCF_000002445.2",
            "protein",
        )
        assert "tax_id" not in meta
        assert "assembly_date" not in meta
        assert "paired_accession" not in meta

    def test_the_genome_keeps_tax_id_assembly_date_and_paired_accession(self):
        meta = results._component_metadata(
            {
                "tax_id": 9913,
                "assembly_date": "2018-04-11",
                "paired_accession": "GCA_000002445.2",
            },
            "GCF_000002445.2",
            "genome",
        )
        assert meta["tax_id"] == 9913
        assert meta["assembly_date"] == "2018-04-11"
        assert meta["paired_accession"] == "GCA_000002445.2"
