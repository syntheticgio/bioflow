"""Metadata schema resolution, coercion, and validation."""

import pytest
from app.metadata import schemas
from app.metadata.schemas import FieldType
from app.models import FormatKind, ObjectRole


class TestFieldResolution:
    def test_common_fields_apply_to_every_format(self):
        for kind in (FormatKind.FASTQ, FormatKind.BAM, FormatKind.VCF, FormatKind.FASTA):
            keys = {f.key for f in schemas.fields_for(kind)}
            assert {"sample_id", "organism", "assay"} <= keys

    def test_molecule_type_and_library_source_are_common_fields(self):
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTQ)}
        assert {"molecule_type", "library_source", "assay"} <= keys

    def test_molecule_type_is_closed_vocabulary(self):
        field = next(f for f in schemas.COMMON_FIELDS if f.key == "molecule_type")
        assert field.type == FieldType.ENUM
        assert field.options == ("DNA", "RNA", "Other")
        assert field.open_vocabulary is False

    def test_library_source_is_open_vocabulary(self):
        field = next(f for f in schemas.COMMON_FIELDS if f.key == "library_source")
        assert field.type == FieldType.ENUM
        # No "Other": the field is open, so an unlisted source is stored as
        # itself rather than flattened to a sentinel. Enforced for every open
        # field by test_schemas_open_vocabulary.TestOtherSentinelIsGone.
        assert field.options == (
            "Genomic", "Transcriptomic", "Metagenomic",
            "Metatranscriptomic", "Synthetic", "Viral RNA",
        )
        assert field.open_vocabulary is True

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

    def test_enum_value_outside_a_closed_field_is_kept_with_a_warning(self):
        """Lab vocabularies always outgrow a fixed list, so the value is kept.

        Uses read_type, a closed vocabulary: single/paired is the complete
        set, so an off-list value here really is worth flagging. This test
        used `aligner` until #66 made that an open vocabulary -- see
        test_open_vocabulary_value_is_not_a_warning below for that direction.
        """
        r = schemas.coerce_and_validate({"read_type": "triple-end"}, FormatKind.FASTQ)
        assert r.values["read_type"] == "triple-end"
        assert any("not one of the suggested" in w["message"] for w in r.warnings)

    def test_open_vocabulary_value_is_not_a_warning(self):
        """The defect from #66: every SRA-enriched file carried a warning
        that was wrong about which value was the authoritative one."""
        r = schemas.coerce_and_validate({"platform": "NextSeq 550"}, FormatKind.FASTQ)
        assert r.values["platform"] == "NextSeq 550"
        assert r.warnings == []

    def test_open_vocabulary_suppression_is_per_field(self):
        """One metadata dict, one open and one closed field, so a blanket
        suppression that ignores the flag fails here."""
        r = schemas.coerce_and_validate(
            {"platform": "NextSeq 550", "read_type": "triple-end"},
            FormatKind.FASTQ,
        )
        keys = {w["key"] for w in r.warnings}
        assert keys == {"read_type"}

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

    def test_open_vocabulary_reaches_the_api(self):
        """The frontend picks its widget from this flag, so a field that is
        open on the backend and closed on the wire renders as a <select>
        and silently blocks the values SRA writes."""
        out = schemas.schema_for_api(FormatKind.FASTQ)
        flat = {f["key"]: f for g in out["groups"] for f in g["fields"]}
        assert flat["platform"]["open_vocabulary"] is True
        assert flat["read_type"]["open_vocabulary"] is False


class TestRoleAwareFields:
    def test_reference_role_replaces_format_fields(self):
        """A reference FASTQ is a genome build, not a sequencing run: library
        and flowcell questions stop being meaningful."""
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTQ, role=ObjectRole.REFERENCE)}
        assert "reference_build" in keys
        assert "assembly_accession" in keys
        assert "library_prep" not in keys
        assert "flowcell" not in keys

    def test_reference_role_keeps_common_fields(self):
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTQ, role=ObjectRole.REFERENCE)}
        assert {"sample_id", "organism", "notes"} <= keys

    def test_reference_role_applies_regardless_of_format(self):
        fastq = {f.key for f in schemas.fields_for(FormatKind.FASTQ, role=ObjectRole.REFERENCE)}
        fasta = {f.key for f in schemas.fields_for(FormatKind.FASTA, role=ObjectRole.REFERENCE)}
        assert fastq == fasta

    def test_plain_fasta_is_no_longer_assumed_to_be_a_reference(self):
        """Reference fields now come from role, not from the format."""
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTA)}
        assert keys == {f.key for f in schemas.COMMON_FIELDS}
        assert "assembly_accession" not in keys

    def test_fastq_without_a_role_is_unaffected(self):
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTQ)}
        assert "library_prep" in keys

    def test_schema_for_api_accepts_a_role(self):
        out = schemas.schema_for_api(FormatKind.FASTQ, role=ObjectRole.REFERENCE)
        groups = {g["group"] for g in out["groups"]}
        assert "Reference" in groups
        assert "Library" not in groups


class TestMetadataSurvivesConversion:
    """Reversibility depends on old-role values not being destroyed."""

    def test_previous_role_values_are_kept_as_unknown_keys(self):
        result = schemas.coerce_and_validate(
            {"flowcell": "HXXXDSX3", "lane": 4, "reference_build": "GRCh38"},
            FormatKind.FASTQ,
            role=ObjectRole.REFERENCE,
        )
        assert result.values["flowcell"] == "HXXXDSX3"
        assert result.values["reference_build"] == "GRCh38"

    def test_round_trip_conversion_preserves_values(self):
        original = {"flowcell": "HXXXDSX3", "library_prep": "TruSeq"}
        as_reference = schemas.coerce_and_validate(
            original, FormatKind.FASTQ, role=ObjectRole.REFERENCE
        ).values
        back_to_reads = schemas.coerce_and_validate(
            as_reference, FormatKind.FASTQ, role=None
        ).values
        assert back_to_reads["flowcell"] == "HXXXDSX3"
        assert back_to_reads["library_prep"] == "TruSeq"

    def test_leftover_keys_from_a_previous_role_still_coerce(self):
        """A flowcell left on a converted reference should still type-coerce."""
        result = schemas.coerce_and_validate(
            {"lane": "4"}, FormatKind.FASTQ, role=ObjectRole.REFERENCE
        )
        assert result.values["lane"] == 4  # int, not the string "4"


class TestReferenceFieldDefinitions:
    def test_new_reference_fields_exist_with_expected_types(self):
        fields = schemas.field_map(None, role=ObjectRole.REFERENCE)
        assert fields["assembly_accession"].type is FieldType.TEXT
        assert fields["is_primary_assembly"].type is FieldType.BOOLEAN
        assert fields["has_decoy"].type is FieldType.BOOLEAN
        assert fields["index_types"].type is FieldType.TEXT
        assert fields["masked"].type is FieldType.BOOLEAN

    def test_reference_build_stays_free_text(self):
        """Builds are open-ended (custom assemblies, patches), so no enum."""
        spec = schemas.field_map(None, role=ObjectRole.REFERENCE)["reference_build"]
        assert spec.type is FieldType.TEXT
        assert spec.options == ()

    def test_reference_fields_are_grouped_together(self):
        fields = schemas.field_map(None, role=ObjectRole.REFERENCE)
        for key in ("reference_build", "source", "assembly_accession", "has_decoy"):
            assert fields[key].group == "Reference"

    def test_reference_build_is_not_validated_against_the_alignment_enum(self):
        """reference_build is free text for a reference but an enum for a BAM.

        The scoped definition must win over the global fallback, or a custom
        assembly name produces a spurious 'not a suggested option' warning.
        """
        result = schemas.coerce_and_validate(
            {"reference_build": "T2T-CHM13-patch1"},
            FormatKind.FASTQ,
            role=ObjectRole.REFERENCE,
        )
        assert result.values["reference_build"] == "T2T-CHM13-patch1"
        assert result.warnings == []

    def test_alignment_reference_build_no_longer_warns_on_an_unknown_value(self):
        """reference_build's alignment/variant ENUM copy became open-vocabulary
        in #66: real values are NCBI assembly accessions (ASM231043v1, R64),
        none of which were ever in the closed suggestion list, so every one of
        them used to warn. This used to assert the opposite -- that an
        off-list value here still warned -- before that was recognized as the
        same defect #66 fixed for `platform`. See the design spec's measured
        6-warnings-on-reference_build finding."""
        result = schemas.coerce_and_validate(
            {"reference_build": "not-a-real-build"}, FormatKind.BAM
        )
        assert result.values["reference_build"] == "not-a-real-build"
        assert result.warnings == []

    def test_every_role_is_accounted_for(self):
        """A new ObjectRole must either carry its own field group or be
        explicitly recorded as deferring to the format's. Neither one is a
        silent default, which is the point: an unconsidered role would
        otherwise quietly narrow the form the user sees."""
        assert set(ObjectRole) == set(schemas.ROLE_FIELDS) | schemas.FORMAT_DERIVED_ROLES

    def test_a_role_and_its_field_group_are_mutually_exclusive(self):
        """Listing a role in both places would make fields_for's precedence
        ambiguous."""
        assert not set(schemas.ROLE_FIELDS) & schemas.FORMAT_DERIVED_ROLES

    def test_every_format_kind_is_accounted_for(self):
        """The FORMAT_FIELDS mirror of the role test above.

        A new FormatKind absent from both FORMAT_FIELDS and
        FORMAT_COMMON_ONLY would fall through to COMMON_FIELDS in fields_for
        with nothing to say that was deliberate -- the same silent-skip shape
        results._SIDECAR_ROLES had for a new SidecarRole.
        """
        assert set(FormatKind) == set(schemas.FORMAT_FIELDS) | schemas.FORMAT_COMMON_ONLY

    def test_a_format_and_its_field_group_are_mutually_exclusive(self):
        """Listing a format in both places would make fields_for's precedence
        ambiguous, same as for roles."""
        assert not set(schemas.FORMAT_FIELDS) & schemas.FORMAT_COMMON_ONLY

    def test_trimmed_reads_still_get_the_fastq_fields(self):
        """The regression this exists for: trimmed output is FASTQ exactly like
        its input, so declaring the role must not strip the library-prep
        questions a raw FASTQ is asked."""
        raw = schemas.field_map(FormatKind.FASTQ)
        trimmed = schemas.field_map(FormatKind.FASTQ, role=ObjectRole.TRIMMED_READS)
        assert set(raw) == set(trimmed)

    def test_reference_role_still_overrides_the_format(self):
        """A role that *does* have a field group keeps winning outright."""
        fields = schemas.field_map(FormatKind.FASTA, role=ObjectRole.REFERENCE)
        assert "reference_build" in fields

    def test_ncbi_enrichment_fields_exist(self):
        """Fields the NCBI assembly lookup fills in."""
        fields = schemas.field_map(None, role=ObjectRole.REFERENCE)
        assert fields["tax_id"].type is FieldType.INTEGER
        assert fields["assembly_level"].type is FieldType.ENUM
        assert fields["assembly_date"].type is FieldType.DATE
        assert fields["paired_accession"].type is FieldType.TEXT
        for key in ("tax_id", "assembly_level", "assembly_date", "paired_accession"):
            assert fields[key].group == "Reference"

    def test_enrichment_fields_are_not_suggested(self):
        """They are filled by lookup, so they should not clutter the form."""
        fields = schemas.field_map(None, role=ObjectRole.REFERENCE)
        for key in ("tax_id", "assembly_level", "assembly_date", "paired_accession"):
            assert not fields[key].suggested
