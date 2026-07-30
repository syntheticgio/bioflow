"""The roles that make downloaded assembly components identifiable.

These exist because format cannot tell them apart: a reference genome, a
protein FASTA and a CDS FASTA are all FormatKind.FASTA. Without a role, a
protein FASTA is indistinguishable from a genome to every consumer -- most
consequentially the aligner's reference picker, which gates on
`role is ObjectRole.REFERENCE`.
"""

from app.models import ObjectRole


class TestAssemblyComponentRoles:
    def test_annotation_role_exists(self):
        assert ObjectRole.ANNOTATION == "annotation"

    def test_protein_role_exists(self):
        assert ObjectRole.PROTEIN == "protein"

    def test_transcript_role_exists(self):
        assert ObjectRole.TRANSCRIPT == "transcript"

    def test_sequence_roles_are_distinct_from_reference(self):
        """The whole point: a protein FASTA must never satisfy a
        `role is ObjectRole.REFERENCE` check."""
        assert ObjectRole.PROTEIN is not ObjectRole.REFERENCE
        assert ObjectRole.TRANSCRIPT is not ObjectRole.REFERENCE
