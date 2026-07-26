"""Metadata schema resolution, coercion, and validation."""

import pytest

from app.metadata import schemas
from app.metadata.schemas import FieldType
from app.models import FormatKind


class TestFieldResolution:
    def test_common_fields_apply_to_every_format(self):
        for kind in (FormatKind.FASTQ, FormatKind.BAM, FormatKind.VCF, FormatKind.FASTA):
            keys = {f.key for f in schemas.fields_for(kind)}
            assert {"sample_id", "organism", "assay"} <= keys

    def test_format_specific_fields_are_added(self):
        fastq = {f.key for f in schemas.fields_for(FormatKind.FASTQ)}
        bam = {f.key for f in schemas.fields_for(FormatKind.BAM)}
        vcf = {f.key for f in schemas.fields_for(FormatKind.VCF)}

        assert "library_prep" in fastq and "library_prep" not in bam
        assert "aligner" in bam and "aligner" not in fastq
        assert "variant_caller" in vcf and "variant_caller" not in bam

    def test_alignment_formats_share_a_field_set(self):
        bam = {f.key for f in schemas.fields_for(FormatKind.BAM)}
        cram = {f.key for f in schemas.fields_for(FormatKind.CRAM)}
        assert bam == cram

    def test_format_specific_definition_wins_on_key_collision(self):
        """Both BAM and VCF define reference_build; each should get its own
        help text rather than a generic one."""
        bam = schemas.field_map(FormatKind.BAM)["reference_build"]
        assert bam.group == "Alignment"
        assert bam.help and "build" in bam.help.lower()

    def test_unknown_kind_falls_back_to_common_fields(self):
        keys = {f.key for f in schemas.fields_for("not-a-real-format")}
        assert keys == {f.key for f in schemas.COMMON_FIELDS}

    def test_none_kind_is_safe(self):
        assert schemas.fields_for(None)

    def test_string_kind_is_accepted(self):
        assert {f.key for f in schemas.fields_for("bam")} == {
            f.key for f in schemas.fields_for(FormatKind.BAM)
        }


class TestCoercion:
    def test_integers_are_stored_as_numbers(self):
        """A number typed into a text box must sort and compare as a number."""
        r = schemas.coerce_and_validate({"lane": "3"}, FormatKind.FASTQ)
        assert r.values["lane"] == 3
        assert isinstance(r.values["lane"], int)

    def test_floats_are_stored_as_numbers(self):
        r = schemas.coerce_and_validate({"mean_coverage": "31.7"}, FormatKind.BAM)
        assert r.values["mean_coverage"] == pytest.approx(31.7)

    @pytest.mark.parametrize(
        "raw,expected",
        [("true", True), ("yes", True), ("1", True),
         ("false", False), ("no", False), ("0", False), (True, True)],
    )
    def test_boolean_forms(self, raw, expected):
        r = schemas.coerce_and_validate({"duplicates_marked": raw}, FormatKind.BAM)
        assert r.values["duplicates_marked"] is expected

    @pytest.mark.parametrize(
        "raw", ["2026-03-14", "14/03/2026", "2026/03/14"]
    )
    def test_dates_normalize_to_iso(self, raw):
        r = schemas.coerce_and_validate({"collection_date": raw})
        assert r.values["collection_date"] == "2026-03-14"

    def test_empty_values_become_null(self):
        """Null is how the UI clears a field."""
        r = schemas.coerce_and_validate({"sample_id": ""})
        assert r.values["sample_id"] is None


class TestValidationIsAdvisory:
    def test_unparseable_number_is_kept_with_a_warning(self):
        """Refusing to store what someone typed loses information; telling
        them it looks wrong does not."""
        r = schemas.coerce_and_validate({"lane": "not a number"}, FormatKind.FASTQ)
        assert r.values["lane"] == "not a number"
        assert any(w["key"] == "lane" for w in r.warnings)

    def test_enum_value_outside_the_options_is_kept(self):
        """Lab vocabularies always outgrow a fixed list."""
        r = schemas.coerce_and_validate(
            {"aligner": "SomeNewAligner-2026"}, FormatKind.BAM
        )
        assert r.values["aligner"] == "SomeNewAligner-2026"
        assert any("not one of the suggested" in w["message"] for w in r.warnings)

    def test_valid_enum_value_produces_no_warning(self):
        r = schemas.coerce_and_validate({"aligner": "BWA-MEM"}, FormatKind.BAM)
        assert r.values["aligner"] == "BWA-MEM"
        assert r.warnings == []

    def test_unknown_keys_pass_through_untouched(self):
        """The schema suggests; it does not restrict."""
        r = schemas.coerce_and_validate(
            {"our_lab_internal_code": "XYZ-9", "freezer_shelf": 4}
        )
        assert r.values["our_lab_internal_code"] == "XYZ-9"
        assert r.values["freezer_shelf"] == 4
        assert r.warnings == []

    def test_bad_date_is_kept_as_text(self):
        r = schemas.coerce_and_validate({"collection_date": "sometime last spring"})
        assert r.values["collection_date"] == "sometime last spring"
        assert r.warnings


class TestApiShape:
    def test_groups_are_ordered_for_rendering(self):
        out = schemas.schema_for_api(FormatKind.BAM)
        groups = [g["group"] for g in out["groups"]]
        assert groups.index("Sample") < groups.index("Alignment")

    def test_every_field_serializes(self):
        import json

        for kind in FormatKind:
            json.dumps(schemas.schema_for_api(kind))

    def test_suggested_fields_are_marked(self):
        out = schemas.schema_for_api(FormatKind.FASTQ)
        flat = [f for g in out["groups"] for f in g["fields"]]
        assert any(f["suggested"] for f in flat)

    def test_enum_fields_carry_their_options(self):
        out = schemas.schema_for_api(FormatKind.VCF)
        flat = {f["key"]: f for g in out["groups"] for f in g["fields"]}
        assert "GATK HaplotypeCaller" in flat["variant_caller"]["options"]
        assert flat["variant_caller"]["type"] == FieldType.ENUM.value
