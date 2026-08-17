"""Genome-size inference for de novo assembly.

Every case here came out of running the rules against the real library rather
than from imagining inputs, which is the only reason two of them exist. The
fixtures below are built to match what that library actually contains --
including the parts of it that are wrong.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
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


class TestLaunchReachesTheQueue:
    """The launch path itself, not just its inference.

    This class exists because of a bug it would have caught and did not: the
    first version of `launch_assembly` built `RunInput(object_id=..., role=...)`
    without the required `name`, and every test passed. The inference tests
    above never call `launch_assembly`, so a 500 from `create_run` reached the
    running app before anything noticed.

    A test that stops at "the service returned" would have missed it too --
    the ValidationError comes from constructing the run, which is three
    statements past validation. So these run the whole way to the enqueue.
    """

    @staticmethod
    def _reads_object():
        # Real ObjectIds, because RunInput validates the type -- which is the
        # very check the production bug tripped. A string id here would make
        # the fixture disagree with the model and hide the thing under test.
        return SimpleNamespace(
            id=PydanticObjectId(),
            name="SRR39891651.fastq",
            format=SimpleNamespace(kind=FormatKind.FASTQ),
            role=None,
            metadata={"organism": "Saccharomyces cerevisiae"},
            facts={"qc_read_chemistry": "hifi"},
            status=ObjectStatus.READY,
            project_id=PydanticObjectId(),
            owner="local",
            blob_sha256="a" * 64,
            size=None,
        )

    async def test_a_hifi_fastq_reaches_the_queue_with_a_valid_run(self):
        from app.models import RunInput, RunInputRole

        reads = self._reads_object()
        created = {}

        async def _create_run(**kwargs):
            # Constructing the real RunInput is the assertion: it is a pydantic
            # model with a required `name`, so a launch that omits it raises
            # here exactly as it did in production.
            for item in kwargs["inputs"]:
                assert isinstance(item, RunInput)
                assert item.name
                assert item.role is RunInputRole.READS
            created.update(kwargs)
            return SimpleNamespace(id="run1", owner="local")

        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued["type"] = job_type
            enqueued.update(kwargs)
            return SimpleNamespace(id="job1")

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(return_value=reads),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=("a" * 64, None)),
            ),
            patch("app.services.run_service.create_run", _create_run),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
            patch(
                "app.services.object_service.list_objects", AsyncMock(return_value=[])
            ),
        ):
            job = await pipeline_service.launch_assembly(
                object_id=reads.id, owner="local"
            )

        assert job.id == "job1"
        assert enqueued["type"] == "assemble_reads"
        assert enqueued["payload"]["assembler"] == "flye"
        # Chemistry-driven, not defaulted: HiFi reads must not be assembled as
        # error-prone Nanopore.
        assert enqueued["payload"]["params"]["mode"] == "pacbio-hifi"
        # One attempt, matching the handler: a retried assembly costs hours and
        # fails the same way.
        assert enqueued["max_attempts"] == 1
        assert created["kind"].value == "assembly"

    async def test_short_reads_no_longer_refused(self):
        """The #490 refusal is gone: short reads now have an installed
        assembler (ABySS), so `launch_assembly` no longer raises a
        chemistry-based refusal for them. Superseded by
        `test_short_reads_are_refused_before_any_run_is_created`, which
        exercised the deleted `ReadChemistry.SHORT` branch in
        `launch_assembly` -- that branch is gone because Task 4 made ABySS
        available for SHORT chemistry, so the refusal it tested is now
        false."""
        from app.pipelines import align_runner, assembler_registry
        from app.pipelines.assemblers import Assembler

        spec = assembler_registry.spec_for_chemistry(align_runner.ReadChemistry.SHORT)
        assert spec is not None
        assert spec.assembler is Assembler.ABYSS

    async def test_a_short_read_launch_with_no_params_fills_in_k(self):
        """The final-review bug: `default_assembly_params` called
        `assembler_registry.mode_for_chemistry` unconditionally, which does a
        bare `spec.mode_flags[chemistry]` lookup. ABySS's spec has an empty
        `mode_flags` (it has no chemistry-graded mode, unlike Flye), so every
        short-read launch through the Actions-tab card -- which sends only
        `object_id`, forcing `launch_assembly` to call
        `default_assembly_params` -- raised KeyError before this fix. This
        drives the real `launch_assembly` path end-to-end, not a mock of it,
        so it would have caught the regression the 3-line registry-lookup
        test next to this one could not.
        """
        import dataclasses

        from app.models import RunInput, RunInputRole
        from app.pipelines import assembler_registry

        class _Available:
            available = True

        abyss_installed = dataclasses.replace(
            assembler_registry.ABYSS_SPEC, tool=lambda: _Available()
        )

        reads = self._reads_object()
        reads.facts = {"qc_read_chemistry": "short"}
        created = {}

        async def _create_run(**kwargs):
            for item in kwargs["inputs"]:
                assert isinstance(item, RunInput)
                assert item.name
                assert item.role is RunInputRole.READS
            created.update(kwargs)
            return SimpleNamespace(id="run1", owner="local")

        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued["type"] = job_type
            enqueued.update(kwargs)
            return SimpleNamespace(id="job1")

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(return_value=reads),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=("a" * 64, None)),
            ),
            patch("app.services.run_service.create_run", _create_run),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
            patch(
                "app.services.object_service.list_objects", AsyncMock(return_value=[])
            ),
            patch(
                "app.services.pipeline_service.resolve_assembly_mate",
                AsyncMock(return_value=None),
            ),
            patch.object(
                assembler_registry,
                "spec_for_chemistry",
                return_value=abyss_installed,
            ),
        ):
            # params=None is the exact shape the Actions-tab card sends --
            # forces launch_assembly through default_assembly_params.
            job = await pipeline_service.launch_assembly(
                object_id=reads.id, owner="local", params=None
            )

        assert job.id == "job1"
        assert enqueued["payload"]["assembler"] == "abyss"
        # The one parameter that most changes a short-read assembly must be
        # pre-filled, not missing.
        assert enqueued["payload"]["params"]["k"] == 51
        assert created["kind"].value == "assembly"

    async def test_unknown_chemistry_is_refused_with_different_advice(self):
        reads = self._reads_object()
        reads.facts = {}

        with patch(
            "app.services.object_service.get_object", AsyncMock(return_value=reads)
        ):
            with pytest.raises(ValidationError) as excinfo:
                await pipeline_service.launch_assembly(
                    object_id=reads.id, owner="local"
                )

        assert "qc" in str(excinfo.value).lower()


def _reads_stub(name, *, first_read_ids=None, metadata=None):
    return SimpleNamespace(
        id=name,
        name=name,
        facts={"first_read_ids": first_read_ids} if first_read_ids else {},
        metadata=metadata or {},
    )


class TestResolveAssemblyMate:
    """`resolve_assembly_mate` delegates discovery to `suggest_mate` and adds
    exactly one thing on top: an explicitly-supplied candidate that fails
    `pairing.verdict()` with REJECTED_READ_IDS refuses the launch instead of
    silently falling back to single-end, which is what `suggest_mate` alone
    would do."""

    async def test_mate_rejected_on_read_ids_refuses_the_launch(self):
        """Two filename-mates whose read IDs disagree must not be assembled.

        This is the case that produces a plausible-looking wrong assembly
        with no error, which is why it refuses rather than falling back to
        single-end.
        """
        with pytest.raises(ValidationError, match="do not appear to be mates"):
            await pipeline_service.resolve_assembly_mate(
                _reads_stub(name="s_R1.fastq", first_read_ids=["A:1:2"]),
                candidate=_reads_stub(name="s_R2.fastq", first_read_ids=["Z:9:9"]),
            )

    async def test_explicit_matching_mate_is_accepted(self):
        """A confirmed pair passes through and gets assembled together."""
        r2 = _reads_stub(name="s_R2.fastq", first_read_ids=["A:1:2"])
        mate = await pipeline_service.resolve_assembly_mate(
            _reads_stub(name="s_R1.fastq", first_read_ids=["A:1:2"]), candidate=r2
        )
        assert mate is r2

    async def test_no_candidate_delegates_to_suggest_mate(self):
        """With no explicit candidate, resolution is entirely `suggest_mate`'s
        job -- this wrapper adds no discovery logic of its own."""
        reads = _reads_stub(name="s_R1.fastq")
        sentinel = object()
        with patch(
            "app.services.pipeline_service.suggest_mate",
            AsyncMock(return_value=sentinel),
        ) as mocked:
            result = await pipeline_service.resolve_assembly_mate(reads)
        mocked.assert_awaited_once_with(reads)
        assert result is sentinel
