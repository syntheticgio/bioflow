"""Alignment launch rules.

The pure decisions -- what may be aligned, what a read group defaults to, which
preset suits a platform, what the output is called -- without a database or
HTTP, mirroring test_launch_rules.py for trimming.
"""

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import ACTIVE_STATES, FormatKind, ObjectStatus
from app.pipelines.align_runner import Preset, ReadChemistry
from app.pipelines.aligners import Aligner
from app.services import pipeline_service


class FakeObject:
    """Enough of a DataObject for the checks under test."""

    def __init__(
        self,
        name="sample_R1.fastq.gz",
        *,
        kind=FormatKind.FASTQ,
        status=ObjectStatus.READY,
        metadata=None,
        facts=None,
    ):
        self.name = name
        self.format = type("F", (), {"kind": kind})()
        self.status = status
        self.metadata = metadata or {}
        self.facts = facts or {}
        self.id = name


class TestAlignable:
    def test_accepts_ready_fastq(self):
        pipeline_service._check_alignable(FakeObject())

    @pytest.mark.parametrize(
        "status",
        [ObjectStatus.UPLOADING, ObjectStatus.HASHING, ObjectStatus.INGESTING,
         ObjectStatus.ERROR, ObjectStatus.MISSING],
    )
    def test_rejects_a_file_that_is_not_ready(self, status):
        with pytest.raises(ValidationError, match="not ready"):
            pipeline_service._check_alignable(FakeObject(status=status))

    @pytest.mark.parametrize("kind", [FormatKind.BAM, FormatKind.FASTA, FormatKind.VCF])
    def test_rejects_anything_that_is_not_fastq(self, kind):
        with pytest.raises(ValidationError, match="not FASTQ"):
            pipeline_service._check_alignable(FakeObject(kind=kind))


class TestReferenceChecks:
    def test_accepts_ready_fasta(self):
        pipeline_service._check_reference(FakeObject("genome.fna", kind=FormatKind.FASTA))

    def test_rejects_fastq_as_a_reference(self):
        """Aligning against reads instead of a genome is a plausible misclick
        and produces a very confusing failure much later."""
        with pytest.raises(ValidationError, match="not a FASTA reference"):
            pipeline_service._check_reference(FakeObject(kind=FormatKind.FASTQ))

    def test_rejects_a_reference_still_being_written(self):
        with pytest.raises(ValidationError, match="not ready"):
            pipeline_service._check_reference(
                FakeObject("g.fna", kind=FormatKind.FASTA, status=ObjectStatus.HASHING)
            )


class TestSamPlatform:
    """The metadata vocabulary is human-facing; SAM's PL field has its own
    controlled vocabulary, and a value outside it makes downstream callers
    behave inconsistently."""

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Illumina NovaSeq", "ILLUMINA"),
            ("Illumina MiSeq", "ILLUMINA"),
            ("Oxford Nanopore", "ONT"),
            ("PacBio", "PACBIO"),
            ("Element", "ELEMENT"),
        ],
    )
    def test_maps_every_schema_option(self, label, expected):
        assert pipeline_service.sam_platform(label) == expected

    def test_is_case_insensitive(self):
        """Metadata is user-editable free text in practice."""
        assert pipeline_service.sam_platform("illumina novaseq") == "ILLUMINA"

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            # The bug this class of test exists for. SRA enrichment writes
            # INSTRUMENT_MODEL into metadata.platform, so real files say
            # "NextSeq 550" and never "Illumina NextSeq". An exact-match table
            # read every SRA-enriched file as OTHER, and the BAM header said
            # PL:OTHER for a perfectly ordinary Illumina run.
            ("NextSeq 550", "ILLUMINA"),
            ("NovaSeq X Plus", "ILLUMINA"),
            ("HiSeq 2500", "ILLUMINA"),
            ("MiSeq", "ILLUMINA"),
            ("iSeq 100", "ILLUMINA"),
            ("Illumina Genome Analyzer II", "ILLUMINA"),
            ("MinION", "ONT"),
            ("PromethION", "ONT"),
            ("PacBio Sequel IIe", "PACBIO"),
            ("Revio", "PACBIO"),
            ("DNBSEQ-T7", "DNBSEQ"),
            ("Ion Torrent S5", "IONTORRENT"),
            ("454 GS FLX+", "LS454"),
            ("AVITI", "ELEMENT"),
        ],
    )
    def test_maps_instrument_models(self, model, expected):
        assert pipeline_service.sam_platform(model) == expected

    def test_a_long_read_instrument_selects_a_long_read_preset(self):
        """The consequence of getting the mapping wrong: a PromethION run read
        as OTHER would be aligned with the short-read preset, which produces
        silently poor alignments rather than an error."""
        assert (
            pipeline_service.suggested_preset(
                pipeline_service.sam_platform("PromethION")
            )
            == Preset.MAP_ONT
        )

    def test_whitespace_is_tolerated(self):
        assert pipeline_service.sam_platform("  NextSeq 550  ") == "ILLUMINA"

    def test_unset_platform_defaults_to_illumina(self):
        """The overwhelmingly common case, and a defensible default: a wrong
        guess here is visible in the BAM header rather than silent."""
        assert pipeline_service.sam_platform(None) == "ILLUMINA"

    def test_an_unrecognized_platform_is_omitted(self):
        """This test used to assert OTHER, on the docstring's claim that OTHER
        "is in the SAM vocabulary." It is not -- SAMv1.tex lists twelve values
        and OTHER is not among them. The spec says to omit PL when the
        technology is not in the list, so None (meaning "omit") is the correct
        answer and there is no placeholder to substitute.
        """
        assert pipeline_service.sam_platform("Some New Sequencer") is None


class TestPresetSelection:
    def test_nanopore_gets_the_ont_preset(self):
        """The wrong preset for long reads produces silently poor alignments
        rather than an error, which is why this is inferred rather than left
        at a short-read default."""
        assert pipeline_service.suggested_preset("ONT") == Preset.MAP_ONT

    def test_pacbio_gets_the_pb_preset(self):
        assert pipeline_service.suggested_preset("PACBIO") == Preset.MAP_PB

    def test_illumina_gets_short_read(self):
        assert pipeline_service.suggested_preset("ILLUMINA") == Preset.SHORT_READ

    def test_unknown_platforms_get_short_read(self):
        """None is what sam_platform now returns for an unrecognized platform;
        it must still fall through to the short-read default rather than
        raising."""
        assert pipeline_service.suggested_preset(None) == Preset.SHORT_READ

    def test_no_chemistry_argument_still_works(self):
        """Existing callers that only know the platform must keep working
        unchanged."""
        assert pipeline_service.suggested_preset("PACBIO") == Preset.MAP_PB

    def test_hifi_chemistry_overrides_the_pacbio_platform_default(self):
        """The regression guard for the actual bug: a PacBio file must not
        keep getting map-pb once its chemistry is known to be HiFi."""
        assert (
            pipeline_service.suggested_preset("PACBIO", chemistry=ReadChemistry.HIFI)
            == Preset.MAP_HIFI
        )

    def test_clr_chemistry_keeps_the_pacbio_preset(self):
        assert (
            pipeline_service.suggested_preset("PACBIO", chemistry=ReadChemistry.CLR)
            == Preset.MAP_PB
        )

    def test_ont_duplex_chemistry_overrides_the_ont_platform_default(self):
        assert (
            pipeline_service.suggested_preset("ONT", chemistry=ReadChemistry.ONT_DUPLEX)
            == Preset.LR_HQ
        )

    def test_unknown_chemistry_on_pacbio_falls_back_to_the_platform_default(self):
        """map-pb is the conservative choice when chemistry can't be
        determined: running CLR parameters on HiFi loses sensitivity, but
        running HiFi parameters on genuinely noisy CLR reads loses far
        more."""
        assert (
            pipeline_service.suggested_preset("PACBIO", chemistry=ReadChemistry.UNKNOWN)
            == Preset.MAP_PB
        )

    def test_none_chemistry_falls_back_to_the_platform_default(self):
        assert (
            pipeline_service.suggested_preset("PACBIO", chemistry=None) == Preset.MAP_PB
        )


class TestDefaultAlignParams:
    """default_align_params reads facts.qc_read_chemistry when present, and
    stays conservative (map-pb, not map-hifi) when it isn't."""

    def test_reads_chemistry_from_facts_when_present(self):
        obj = FakeObject(
            metadata={"platform": "PacBio Sequel IIe"},
            facts={"qc_read_chemistry": ReadChemistry.HIFI.value},
        )
        params = pipeline_service.default_align_params(obj)
        assert params["preset"] == Preset.MAP_HIFI

    def test_falls_back_to_platform_when_facts_have_no_chemistry(self):
        obj = FakeObject(metadata={"platform": "PacBio Sequel IIe"})
        params = pipeline_service.default_align_params(obj)
        assert params["preset"] == Preset.MAP_PB

    def test_falls_back_to_platform_when_object_is_none(self):
        """No object means no chemistry to read, so this must behave exactly
        as it did before chemistry existed: Illumina/short-read defaults."""
        params = pipeline_service.default_align_params(None)
        assert params["preset"] in (Preset.SHORT_READ, "")

    def test_an_unrecognized_chemistry_value_in_facts_does_not_raise(self):
        """Facts are attacker- and tool-controlled data, not a validated enum
        -- a stale or malformed value must degrade to the platform default
        rather than crash the align dialog."""
        obj = FakeObject(
            metadata={"platform": "PacBio Sequel IIe"},
            facts={"qc_read_chemistry": "not-a-real-chemistry"},
        )
        params = pipeline_service.default_align_params(obj)
        assert params["preset"] == Preset.MAP_PB


class TestDefaultReadGroup:
    def test_uses_sample_metadata_when_present(self):
        """sample_id and library_prep are already in the schema, so the dialog
        is usually a confirmation rather than data entry."""
        rg = pipeline_service.default_read_group(
            FakeObject(metadata={"sample_id": "PT-042", "library_prep": "KAPA-HyperPrep",
                                 "platform": "Illumina NovaSeq"})
        )
        assert rg == {"sample": "PT-042", "library": "KAPA-HyperPrep",
                      "platform": "ILLUMINA"}

    def test_falls_back_to_the_filename_for_an_unannotated_file(self):
        """A placeholder the user can see and correct beats an empty required
        field in a dialog they cannot submit."""
        rg = pipeline_service.default_read_group(FakeObject("SRR123456_R1.fastq.gz"))
        assert rg["sample"] == "SRR123456_R1"

    def test_library_falls_back_to_the_platform(self):
        """Reads off one instrument are normally one library, so the instrument
        is a better guess than repeating the sample name -- and it is what
        distinguishes two libraries of the same sample, which is what LB is
        for."""
        rg = pipeline_service.default_read_group(
            FakeObject(metadata={"sample_id": "S1", "platform": "NextSeq 550"})
        )
        assert rg["library"] == "NextSeq 550"

    def test_library_prefers_an_explicit_prep_over_the_platform(self):
        rg = pipeline_service.default_read_group(
            FakeObject(metadata={"sample_id": "S1", "library_prep": "TruSeq",
                                 "platform": "NextSeq 550"})
        )
        assert rg["library"] == "TruSeq"

    def test_library_prefers_a_library_id_over_the_platform(self):
        rg = pipeline_service.default_read_group(
            FakeObject(metadata={"sample_id": "S1", "library_id": "LIB-7",
                                 "platform": "NextSeq 550"})
        )
        assert rg["library"] == "LIB-7"

    def test_library_uses_the_readable_instrument_not_the_sam_tag(self):
        """"NextSeq 550" identifies a library run; "ILLUMINA" does not."""
        rg = pipeline_service.default_read_group(
            FakeObject(metadata={"platform": "NextSeq 550"})
        )
        assert rg["library"] == "NextSeq 550"
        assert rg["platform"] == "ILLUMINA"

    def test_library_falls_back_to_the_sample_with_nothing_else(self):
        """LB is required, and a duplicate of the sample name is at least
        honest about carrying no extra information."""
        rg = pipeline_service.default_read_group(FakeObject(metadata={"sample_id": "S1"}))
        assert rg["library"] == "S1"

    def test_a_blank_platform_does_not_become_the_library(self):
        """Whitespace-only metadata is an empty field, not a library name."""
        rg = pipeline_service.default_read_group(
            FakeObject(metadata={"sample_id": "S1", "platform": "   "})
        )
        assert rg["library"] == "S1"

    def test_every_required_field_is_populated(self):
        """The dialog cannot be submitted without these, so a default that
        omits one would block the user rather than help them."""
        rg = pipeline_service.default_read_group(FakeObject())
        assert all(rg.get(k) for k in ("sample", "library", "platform"))


class TestActiveIndexJobQuery:
    """The lookup that finds an in-flight index build to wait on.

    It runs only when two alignments race for the same unindexed reference, so
    it had no coverage and shipped broken: it used `Job.state.in_(...)`, and a
    Beanie ExpressionField has neither `.in_()` nor a resolvable `state`
    attribute outside a query context. Every call raised. Asserting the query
    shape here is what makes the branch checkable without staging the race.
    """

    def test_matches_only_index_builds(self):
        q = pipeline_service.active_index_job_query(PydanticObjectId())
        assert q["type"] == "build_index"

    def test_matches_only_jobs_still_in_flight(self):
        q = pipeline_service.active_index_job_query(PydanticObjectId())
        states = set(q["state"]["$in"])
        assert states == {s.value for s in ACTIVE_STATES}
        assert "succeeded" not in states
        assert "failed" not in states

    def test_includes_blocked(self):
        """An index build is never blocked today, but deriving the list from
        ACTIVE_STATES means a state added later is covered without an edit."""
        q = pipeline_service.active_index_job_query(PydanticObjectId())
        assert "blocked" in q["state"]["$in"]

    def test_scoped_to_the_reference(self):
        ref = PydanticObjectId()
        assert pipeline_service.active_index_job_query(ref)["object_id"] == ref

    def test_is_a_plain_mongo_query(self):
        """Values must be primitives Mongo understands, not Beanie expression
        objects -- the specific mistake that broke this."""
        q = pipeline_service.active_index_job_query(PydanticObjectId())
        assert isinstance(q, dict)
        assert all(isinstance(s, str) for s in q["state"]["$in"])


class TestBamNaming:
    def test_strips_compression_and_extension(self):
        assert pipeline_service._bam_name("sample_R1.fastq.gz", "S1") == "sample.bam"

    def test_strips_the_mate_token(self):
        """R1 and R2 align together into one BAM, so keeping the token would
        name it after only half its input."""
        assert pipeline_service._bam_name("sample_R1.fastq.gz", "S1") == "sample.bam"

    def test_keeps_the_reads_stem_rather_than_the_sample(self):
        """Two libraries from one sample must not produce two files with the
        same name."""
        assert pipeline_service._bam_name("lib2_R1.fastq.gz", "SAMP") == "lib2.bam"

    def test_handles_an_uncompressed_input(self):
        assert pipeline_service._bam_name("reads.fastq", "S1") == "reads.bam"

    def test_falls_back_to_the_sample_when_the_stem_is_empty(self):
        assert pipeline_service._bam_name(".fastq.gz", "SAMP").endswith(".bam")

    def test_always_ends_in_bam(self):
        """samtools infers the output format from the filename."""
        for name in ("a_R1.fastq.gz", "b.fq", "c_R2.fastq.bz2"):
            assert pipeline_service._bam_name(name, "S").endswith(".bam")


class TestAlignerToolResolution:
    def test_every_aligner_resolves_to_its_own_tool(self):
        """_aligner_tool used to be an if/else over two tools, and returned
        minimap2 for anything that was not bwa-mem2 -- so a bowtie2 job would
        have silently run minimap2 against a bowtie2 index."""
        from app.pipelines.aligners import Aligner
        from app.queue.align_handlers import _aligner_tool

        for aligner in Aligner:
            assert _aligner_tool(aligner).name == aligner.value


class TestDeclaredMemory:
    """What the queue reserves for an alignment or an index build.

    Both handlers used to declare a flat 8 GB whatever the aligner and
    whatever the genome. That is roughly right for bwa-mem2 on a human genome
    and wrong in both directions elsewhere -- and wrong in the dangerous
    direction for STAR, whose human index needs about 30 GB. The governor
    would admit the job believing there was room, and the result is an OOM
    kill with a log that says nothing.
    """

    HUMAN_BASES = 3_100_000_000
    YEAST_BASES = 12_000_000

    async def test_star_reserves_far_more_than_the_old_flat_8gb(self):
        mem = await pipeline_service.declared_align_mem_mb(
            aligner=Aligner.STAR,
            reference_bases=self.HUMAN_BASES,
            threads=4,
            sort_memory_mb=1024,
            building_index=False,
        )
        # ~10 bytes/base is ~29.6 GB of index alone.
        assert mem > 8192
        assert mem > 25_000

    async def test_star_reserves_more_than_bwa_for_the_same_genome(self):
        """The comparison the flat number could not express. Both were 8192
        before, so the governor could not tell them apart.

        The margin was `bwa * 3` until #100 corrected bwa-mem2's resident-index
        coefficient from 2.0 to the ~3.2 bytes/base its README documents, which
        narrowed a ratio that was only ever incidental. What matters is that
        STAR's uncompressed suffix array is visibly dearer than an FM-index --
        ~35 GB against ~15 GB here -- not the particular multiple, so this
        asserts a clear separation rather than a number recalibrated to
        whatever the coefficients currently happen to produce.
        """
        common = dict(
            reference_bases=self.HUMAN_BASES,
            threads=4,
            sort_memory_mb=1024,
            building_index=False,
        )
        star = await pipeline_service.declared_align_mem_mb(
            aligner=Aligner.STAR, **common
        )
        bwa = await pipeline_service.declared_align_mem_mb(
            aligner=Aligner.BWA_MEM2, **common
        )
        assert star > bwa * 2

    async def test_a_small_genome_reserves_less_than_the_old_flat_8gb(self):
        """The other direction, which is what stops one yeast alignment from
        occupying the budget for four."""
        mem = await pipeline_service.declared_align_mem_mb(
            aligner=Aligner.BWA_MEM2,
            reference_bases=self.YEAST_BASES,
            threads=2,
            sort_memory_mb=1024,
            building_index=False,
        )
        assert mem < 8192

    async def test_a_floor_applies_when_the_reference_size_is_unknown(self):
        """`reference.size` can be missing, and an estimate near zero would
        let the governor admit the job alongside everything else."""
        mem = await pipeline_service.declared_align_mem_mb(
            aligner=Aligner.BWA_MEM2,
            reference_bases=0,
            threads=1,
            sort_memory_mb=0,
            building_index=False,
        )
        assert mem == pipeline_service.MIN_DECLARED_MEM_MB

    async def test_building_an_index_reserves_more_than_loading_one(self):
        """The multiplier is why the two jobs declare different numbers, and
        is most of the reason bowtie2 and HISAT2 need their own figure."""
        common = dict(
            aligner=Aligner.HISAT2,
            reference_bases=self.HUMAN_BASES,
            threads=4,
            sort_memory_mb=1024,
        )
        building = await pipeline_service.declared_align_mem_mb(
            building_index=True, **common
        )
        loading = await pipeline_service.declared_align_mem_mb(
            building_index=False, **common
        )
        assert building > loading
