"""Launch validation for an assembly download.

The rules that must hold before any job is queued, tested without HTTP.
"""

import pytest
from app.errors import ValidationError
from app.services import ncbi_assembly_service


class TestSelectionValidation:
    def test_a_non_assembly_accession_is_rejected(self):
        """An SRR here means the caller routed the wrong way; queueing a
        genome download for it would fail confusingly an hour later."""
        with pytest.raises(ValidationError, match="assembly accession"):
            ncbi_assembly_service.validate_selection("SRR11768093", ["genome"])

    def test_an_unversioned_accession_is_rejected(self):
        with pytest.raises(ValidationError, match="assembly accession"):
            ncbi_assembly_service.validate_selection("GCF_000002445", ["genome"])

    def test_genome_is_forced_in(self):
        """Every other component describes coordinates or products of the
        genome sequence. A request without it is a frontend bug, not an
        intent to honor."""
        assert ncbi_assembly_service.validate_selection(
            "GCF_000002445.2", ["gff3"]
        ) == ["genome", "gff3"]

    def test_unknown_components_are_dropped(self):
        """Silently, because the alternative is failing a download over a
        component name the frontend sent by mistake."""
        assert ncbi_assembly_service.validate_selection(
            "GCF_000002445.2", ["genome", "nonsense"]
        ) == ["genome"]

    def test_components_come_back_in_display_order(self):
        """So the label and the log read consistently regardless of the order
        checkboxes were clicked."""
        assert ncbi_assembly_service.validate_selection(
            "GCF_000002445.2", ["cds", "genome", "gff3"]
        ) == ["genome", "gff3", "cds"]


class TestLabel:
    def test_a_genome_only_download_says_so(self):
        label = ncbi_assembly_service.download_label("GCF_000002445.2", ["genome"])
        assert "GCF_000002445.2" in label

    def test_a_multi_component_download_counts_them(self):
        label = ncbi_assembly_service.download_label(
            "GCF_000002445.2", ["genome", "gff3", "protein"]
        )
        assert "GCF_000002445.2" in label
        assert "3" in label
