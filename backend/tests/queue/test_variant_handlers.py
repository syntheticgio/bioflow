"""Variant calling job handler: payload validation and caller dispatch."""

import pytest

from app.errors import PermanentError
from app.pipelines.align_runner import ReadChemistry
from app.pipelines.variant_runner import VariantCaller
from app.queue import variant_handlers


class TestPayloadValidation:
    def test_missing_bam_object_id_raises(self):
        with pytest.raises(PermanentError, match="bam_object_id"):
            variant_handlers._validate_payload({})

    def test_missing_reference_object_id_raises(self):
        with pytest.raises(PermanentError, match="reference_object_id"):
            variant_handlers._validate_payload({"bam_object_id": "abc"})

    def test_valid_payload_accepted(self):
        variant_handlers._validate_payload(
            {"bam_object_id": "abc", "reference_object_id": "def"}
        )


class TestCallerResolution:
    def test_defaults_to_clair3(self):
        assert variant_handlers._resolve_caller({}) is VariantCaller.CLAIR3

    def test_reads_caller_from_payload(self):
        assert (
            variant_handlers._resolve_caller({"caller": "bcftools"})
            is VariantCaller.BCFTOOLS
        )

    def test_unknown_caller_is_permanent(self):
        """A bad caller name will not become good on retry, and the attempt
        budget is better spent on work that might succeed."""
        with pytest.raises(PermanentError, match="Unknown variant caller"):
            variant_handlers._resolve_caller({"caller": "gatk"})

    def test_deepvariant_is_resolved_not_refused(self):
        """DeepVariant now runs as a sidecar container: the handler's own
        refusal is gone, and resolving the caller no longer raises. Whether
        it can actually run is a Docker-availability question, checked later
        by tools.require(tools.deepvariant()) -- not here."""
        assert (
            variant_handlers._resolve_caller({"caller": "deepvariant"})
            is VariantCaller.DEEPVARIANT
        )


class TestChemistryGuard:
    def test_clr_is_refused(self):
        """The launch path refuses CLR too, but a payload can outlive the
        check that built it -- a job queued before a reclassification, or one
        replayed by hand."""
        with pytest.raises(PermanentError, match="CLR"):
            variant_handlers._check_chemistry(
                chemistry=ReadChemistry.CLR, caller=VariantCaller.CLAIR3
            )

    def test_absent_chemistry_is_allowed(self):
        """QC may never have run. Unknown is not a reason to refuse work."""
        variant_handlers._check_chemistry(
            chemistry=None, caller=VariantCaller.BCFTOOLS
        )

    def test_mismatch_is_allowed_through(self):
        """Overriding the suggested caller is the user's call to make; the
        handler logs it rather than blocking."""
        variant_handlers._check_chemistry(
            chemistry=ReadChemistry.ONT_SIMPLEX, caller=VariantCaller.BCFTOOLS
        )


class TestModelPath:
    def test_builds_platform_directory(self, tmp_path):
        (tmp_path / "ont").mkdir()
        assert variant_handlers._model_path("ont", root=tmp_path) == tmp_path / "ont"

    def test_missing_model_is_permanent(self, tmp_path):
        """A model absent from the image will still be absent next attempt."""
        with pytest.raises(PermanentError, match="model not found"):
            variant_handlers._model_path("hifi", root=tmp_path)
