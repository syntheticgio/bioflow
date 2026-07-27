"""Alignment launch rules.

The pure decisions -- what may be aligned, what a read group defaults to, which
preset suits a platform, what the output is called -- without a database or
HTTP, mirroring test_launch_rules.py for trimming.
"""

import pytest

from app.errors import ValidationError
from app.models import FormatKind, ObjectStatus
from app.pipelines.align_runner import Preset
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
    ):
        self.name = name
        self.format = type("F", (), {"kind": kind})()
        self.status = status
        self.metadata = metadata or {}
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
            ("DNBSEQ-T7", "BGI"),
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

    def test_an_unrecognized_platform_becomes_other(self):
        """OTHER is in the SAM vocabulary; passing the raw label through would
        not be."""
        assert pipeline_service.sam_platform("Some New Sequencer") == "OTHER"


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
        assert pipeline_service.suggested_preset("OTHER") == Preset.SHORT_READ


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
