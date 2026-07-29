"""The model vocabulary variant calling adds.

Small on purpose: these are enum members, and the reason they get a test at all
is that each one is a stored string. Renaming a member is a data migration, not
a refactor, so the values are pinned here where a change is visible.
"""

from app.models import ObjectRole, RunJobRole, RunKind, SidecarRole


class TestVariantObjectModel:
    def test_variants_role_value(self):
        assert ObjectRole.VARIANTS.value == "variants"

    def test_tbi_sidecar_value(self):
        assert SidecarRole.TBI.value == "tbi"

    def test_tbi_sits_alongside_bai(self):
        """A .tbi is scaffolding in exactly the sense a .bai is: an index the
        user never opens, attached to the file they do."""
        assert SidecarRole.BAI.value == "bai"
        assert SidecarRole.TBI is not SidecarRole.BAI


class TestVariantRunModel:
    def test_variant_calling_run_kind_value(self):
        assert RunKind.VARIANT_CALLING.value == "variant_calling"

    def test_call_variants_job_role_value(self):
        assert RunJobRole.CALL_VARIANTS.value == "call_variants"

    def test_existing_kinds_unchanged(self):
        """Guards against a careless edit to the enum renaming a sibling."""
        assert RunKind.ALIGNMENT.value == "alignment"
        assert RunKind.TRIM.value == "trim"
        assert RunKind.SRA_DOWNLOAD.value == "sra_download"
