"""Launch-time validation and payload construction.

These cover the pure decisions -- what may be trimmed, which file leads a pair,
what makes two runs identical -- without a database or HTTP.
"""

import pytest

from app.errors import ValidationError
from app.models import FormatKind, ObjectStatus
from app.services import pipeline_service


class FakeObject:
    """Enough of a DataObject for the checks under test."""

    def __init__(self, name="sample_R1.fastq.gz", *, kind=FormatKind.FASTQ,
                 status=ObjectStatus.READY):
        self.name = name
        self.format = type("F", (), {"kind": kind})()
        self.status = status
        self.id = name


class TestTrimmable:
    def test_accepts_ready_fastq(self):
        pipeline_service._check_fastq_ready(FakeObject())

    @pytest.mark.parametrize(
        "status",
        [ObjectStatus.UPLOADING, ObjectStatus.HASHING, ObjectStatus.INGESTING,
         ObjectStatus.ERROR, ObjectStatus.MISSING],
    )
    def test_rejects_a_file_that_is_not_ready(self, status):
        """Trimming a file still being written would read a partial archive."""
        with pytest.raises(ValidationError, match="not ready"):
            pipeline_service._check_fastq_ready(FakeObject(status=status))

    @pytest.mark.parametrize(
        "kind", [FormatKind.BAM, FormatKind.VCF, FormatKind.FASTA, FormatKind.BED]
    )
    def test_rejects_anything_that_is_not_fastq(self, kind):
        """fastp reads FASTQ. Handing it a BAM produces a confusing parse error
        several minutes into a job rather than an answer now."""
        with pytest.raises(ValidationError, match="not FASTQ"):
            pipeline_service._check_fastq_ready(FakeObject(kind=kind))

    def test_the_error_names_the_file(self):
        """The message ends up in a toast; 'an object is not ready' would not
        tell the user which one."""
        with pytest.raises(ValidationError, match="reads_R1"):
            pipeline_service._check_fastq_ready(
                FakeObject("reads_R1.fastq.gz", status=ObjectStatus.ERROR)
            )


class TestQcSharesTheFastqCheck:
    """QC has the same input requirement as trim, and reuses the same check.
    What differs is only the verb in the message."""

    def test_accepts_ready_fastq(self):
        pipeline_service._check_fastq_ready(FakeObject(), verb="QC")

    def test_rejects_a_bam(self):
        with pytest.raises(ValidationError, match="not FASTQ"):
            pipeline_service._check_fastq_ready(
                FakeObject(kind=FormatKind.BAM), verb="QC"
            )

    def test_the_message_names_the_operation_that_was_asked_for(self):
        """'not ready to trim' on a QC run would send the user looking for a
        trim they never started."""
        with pytest.raises(ValidationError, match="not ready to QC"):
            pipeline_service._check_fastq_ready(
                FakeObject(status=ObjectStatus.ERROR), verb="QC"
            )

    def test_defaults_to_trim_for_the_existing_callers(self):
        with pytest.raises(ValidationError, match="not ready to trim"):
            pipeline_service._check_fastq_ready(FakeObject(status=ObjectStatus.ERROR))


class TestParamsFingerprint:
    def test_identical_params_match(self):
        a = {"threads": 4, "min_length": 15}
        b = {"min_length": 15, "threads": 4}  # key order must not matter
        assert pipeline_service._params_fingerprint(
            a
        ) == pipeline_service._params_fingerprint(b)

    def test_different_params_differ(self):
        """Re-trimming with new settings is a different run, so the dedup key
        must not collapse it into the first one."""
        assert pipeline_service._params_fingerprint(
            {"min_length": 15}
        ) != pipeline_service._params_fingerprint({"min_length": 50})

    def test_is_short_enough_for_a_key(self):
        fp = pipeline_service._params_fingerprint({"threads": 4})
        assert len(fp) == 12


class TestDefaults:
    def test_threads_come_from_settings(self):
        from app.config import settings

        assert pipeline_service.default_params()["threads"] == (
            settings.pipeline_default_threads
        )

    def test_defaults_are_serializable(self):
        """They cross the wire to the launch form."""
        import json

        json.dumps(pipeline_service.default_params())

    def test_adapter_detection_is_on_by_default(self):
        """For paired reads fastp finds adapters by overlap analysis, which is
        more reliable than matching a known list."""
        assert pipeline_service.default_params()["detect_adapter_for_pe"] is True

    def test_poly_g_is_left_unset(self):
        """None means 'let fastp decide from the instrument', which is better
        than anything this application can guess."""
        assert pipeline_service.default_params()["trim_poly_g"] is None
