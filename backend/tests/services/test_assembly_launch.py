"""Genome-size inference for de novo assembly.

Every case here came out of running the rules against the real library rather
than from imagining inputs, which is the only reason two of them exist. The
fixtures below are built to match what that library actually contains --
including the parts of it that are wrong.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models import FormatKind, ObjectRole, ObjectStatus
from app.services import pipeline_service


def _fasta(name, *, organism, role=ObjectRole.REFERENCE, facts=None):
    return SimpleNamespace(
        id=name,
        name=name,
        format=SimpleNamespace(kind=FormatKind.FASTA),
        role=role,
        metadata={"organism": organism},
        facts=facts or {},
        status=ObjectStatus.READY,
    )


def _reads(organism):
    return SimpleNamespace(
        id="reads1",
        name="SRR1.fastq",
        format=SimpleNamespace(kind=FormatKind.FASTQ),
        role=None,
        metadata={"organism": organism} if organism else {},
        facts={},
        status=ObjectStatus.READY,
        project_id="proj1",
        owner="local",
    )


# The four components one NCBI download produces, as they exist in the real
# library: all four carry the *assembly's* ncbi_total_length, and the protein
# and CDS files are roled `reference` -- legacy rows from before the component
# table learned PROTEIN and TRANSCRIPT. Their own base counts are wildly
# smaller than the genome, which is the trap.
_ASSEMBLY_FACTS = {
    "ncbi_total_length": 12_071_326,
    "ncbi_assembly_name": "R64",
    "ncbi_assembly_accession": "GCF_000146045.2",
}


def _real_shaped_project():
    return [
        _fasta(
            "GCA_000146045.2_R64_cds_from_genomic.fna",
            organism="Saccharomyces cerevisiae S288C",
            facts={**_ASSEMBLY_FACTS, "total_bases": 8_800_563},
        ),
        _fasta(
            "GCF_000146045.2_R64_protein.faa",
            organism="Saccharomyces cerevisiae S288C",
            facts={**_ASSEMBLY_FACTS, "total_bases": 2_933_360},
        ),
        _fasta(
            "GCF_000146045.2_R64_genomic.fna",
            organism="Saccharomyces cerevisiae S288C",
            facts={**_ASSEMBLY_FACTS, "total_bases": 12_157_105},
        ),
    ]


class TestOrganismKey:
    def test_a_strain_suffix_does_not_defeat_the_match(self):
        """The case that made inference silently find nothing against the real
        library: SRA labels the run `Saccharomyces cerevisiae` while the
        assembly it came from is `Saccharomyces cerevisiae S288C`."""
        assert pipeline_service._organism_key(
            "Saccharomyces cerevisiae"
        ) == pipeline_service._organism_key("Saccharomyces cerevisiae S288C")

    def test_two_species_in_one_genus_stay_distinct(self):
        """Why this stops at two words rather than matching on genus: genomes
        differ severalfold across a genus, so a genus-only key would supply a
        confidently wrong number."""
        assert pipeline_service._organism_key(
            "Drosophila melanogaster"
        ) != pipeline_service._organism_key("Drosophila simulans")

    @pytest.mark.parametrize("value", ["", "   ", "Saccharomyces"])
    def test_anything_short_of_a_binomial_is_no_key_at_all(self, value):
        assert pipeline_service._organism_key(value) == ""


class TestGenomeSizeInference:
    async def test_it_takes_the_assembly_length_across_a_strain_suffix(self):
        with patch(
            "app.services.object_service.list_objects",
            AsyncMock(return_value=_real_shaped_project()),
        ):
            size, source = await pipeline_service.infer_genome_size(
                _reads("Saccharomyces cerevisiae")
            )
        assert size == 12_071_326
        # Names the assembly, not whichever component sorted first. Against
        # the real library that was `cds_from_genomic.fna`, and "genome size
        # inferred from cds_from_genomic.fna" invites the exact doubt the
        # label exists to remove.
        assert source == "R64 (GCF_000146045.2)"

    async def test_a_protein_fasta_can_never_supply_the_size(self):
        """The dangerous case, and the reason total_bases is not a fallback.

        `protein.faa` and `cds_from_genomic.fna` sit in the real library roled
        `reference` -- legacy rows the component table would not produce today
        -- so a role check alone does not exclude them. Reading their
        total_bases would call a 12.1 Mb genome 2.9 Mb and under-estimate the
        memory a real assembly needs by 4x, silently.
        """
        protein_only = [
            _fasta(
                "GCF_000146045.2_R64_protein.faa",
                organism="Saccharomyces cerevisiae S288C",
                # No assembly-level fact: only the file's own base count.
                facts={"total_bases": 2_933_360},
            )
        ]
        with patch(
            "app.services.object_service.list_objects",
            AsyncMock(return_value=protein_only),
        ):
            size, source = await pipeline_service.infer_genome_size(
                _reads("Saccharomyces cerevisiae")
            )
        assert size is None
        assert source is None

    async def test_no_organism_on_the_reads_infers_nothing(self):
        with patch(
            "app.services.object_service.list_objects",
            AsyncMock(return_value=_real_shaped_project()),
        ):
            assert await pipeline_service.infer_genome_size(_reads(None)) == (
                None,
                None,
            )

    async def test_a_different_species_is_not_borrowed_from(self):
        with patch(
            "app.services.object_service.list_objects",
            AsyncMock(return_value=_real_shaped_project()),
        ):
            size, _ = await pipeline_service.infer_genome_size(
                _reads("Escherichia coli")
            )
        assert size is None

    async def test_an_empty_project_is_not_an_error(self):
        """De novo assembly is what you do when there is no reference, so
        finding nothing is the normal path rather than a failure."""
        with patch(
            "app.services.object_service.list_objects", AsyncMock(return_value=[])
        ):
            assert await pipeline_service.infer_genome_size(
                _reads("Saccharomyces cerevisiae")
            ) == (None, None)
