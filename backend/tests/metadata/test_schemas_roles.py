"""Every role must be deliberately accounted for.

`FORMAT_DERIVED_ROLES` exists so that adding a role without thinking about
its questions fails a test rather than silently showing a protein FASTA the
reference-genome form.
"""

from app.metadata import schemas
from app.models import FormatKind, ObjectRole


class TestEveryRoleIsAccountedFor:
    def test_each_role_has_fields_or_defers_to_format(self):
        for role in ObjectRole:
            has_own = role in schemas.ROLE_FIELDS
            defers = role in schemas.FORMAT_DERIVED_ROLES
            assert has_own or defers, (
                f"{role} is in neither ROLE_FIELDS nor FORMAT_DERIVED_ROLES. "
                "Decide which questions it deserves."
            )

    def test_no_role_both_has_fields_and_defers(self):
        """Both would be ambiguous: fields_for prefers ROLE_FIELDS, so the
        FORMAT_DERIVED_ROLES membership would be a lie."""
        overlap = set(schemas.ROLE_FIELDS) & schemas.FORMAT_DERIVED_ROLES
        assert not overlap, f"contradictory: {overlap}"


class TestSequenceSetFields:
    def test_protein_is_not_asked_reference_questions(self):
        """A protein FASTA has no assembly level and no scaffold N50. Asking
        would imply it is a genome."""
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTA, ObjectRole.PROTEIN)}
        assert "assembly_accession" in keys
        assert "scaffold_n50" not in keys
        assert "is_primary_assembly" not in keys

    def test_transcript_shares_the_protein_vocabulary(self):
        """Both are sequence sets derived from an assembly; two vocabularies
        for one question shape would be worse than one shared."""
        protein = {f.key for f in schemas.fields_for(FormatKind.FASTA, ObjectRole.PROTEIN)}
        transcript = {f.key for f in schemas.fields_for(FormatKind.FASTA, ObjectRole.TRANSCRIPT)}
        assert protein == transcript

    def test_annotation_gets_the_interval_questions(self):
        """A GFF3's questions already exist as INTERVAL_FIELDS. Deferring to
        format reuses them rather than inventing a second interval vocabulary."""
        keys = {f.key for f in schemas.fields_for(FormatKind.GFF, ObjectRole.ANNOTATION)}
        assert "source" in keys

    def test_sequence_set_fields_are_known_for_validation(self):
        """all_known_fields drives coercion of unscoped edits; a field missing
        from it is treated as an unknown key and skips coercion."""
        known = schemas.all_known_fields()
        assert "sequence_count" in known
