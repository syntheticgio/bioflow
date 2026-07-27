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

    def test_library_falls_back_to_the_sample(self):
        """@RG requires LB, and one library per sample is the common case."""
        rg = pipeline_service.default_read_group(FakeObject(metadata={"sample_id": "S1"}))
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
