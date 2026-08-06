"""The Actions-tab card for QUAST reference-based misassembly QC.

Same reasoning test_scaffold_card.py documents, its closest sibling (the
identical draft-plus-reference input shape): the image ships QUAST
installed, so asserting *availability* alone would pass whether or not a
patch to `tools.quast` took effect. The failing direction is the one that
proves the seam.

The ambiguity test here is the one that matters most in practice, more than
its polish-card equivalent: a project holding two reference-role FASTA for
one organism is the *ordinary* shape, not an edge case (the real yeast
project carries both the GCA and GCF genomic FASTA), so this card refuses
more often than it launches.
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.models import FormatKind, ObjectRole, ObjectStatus
from app.pipelines.tools import Tool
from app.services.suggestion_service import CardStatus, build_misassembly_card

_QUAST = Tool(name="quast", path="/usr/local/bin/quast.py", version="5.3.0")
_QUAST_MISSING = Tool(name="quast", path=None, version=None, error="not found")


def _draft(*, kind=FormatKind.FASTA, role=None, status=ObjectStatus.READY):
    return SimpleNamespace(
        id="draft1",
        name="assembly.fasta",
        format=SimpleNamespace(kind=kind),
        role=role,
        status=status,
        project_id="proj1",
        owner="local",
    )


def _reference(name, oid):
    return SimpleNamespace(id=oid, name=name)


ONE_REF = [_reference("ref.fasta", "ref1")]
TWO_REFS = [_reference("ref_gca.fasta", "ref1"), _reference("ref_gcf.fasta", "ref2")]


def _patched(quast=_QUAST):
    return patch("app.services.suggestion_service.tools.quast", return_value=quast)


class TestAnchorShape:
    def test_no_card_for_reads(self):
        with _patched():
            assert build_misassembly_card(_draft(kind=FormatKind.FASTQ), ONE_REF) is None

    def test_no_card_for_an_alignment(self):
        with _patched():
            assert build_misassembly_card(_draft(kind=FormatKind.BAM), ONE_REF) is None

    def test_no_card_for_protein_fasta(self):
        """The `protein.faa` case, which is the bug this repo already
        shipped once against these same tool families."""
        with _patched():
            assert (
                build_misassembly_card(_draft(role=ObjectRole.PROTEIN), ONE_REF) is None
            )

    def test_no_card_for_transcript_fasta(self):
        with _patched():
            assert (
                build_misassembly_card(_draft(role=ObjectRole.TRANSCRIPT), ONE_REF)
                is None
            )

    def test_no_card_for_an_unready_object(self):
        with _patched():
            assert (
                build_misassembly_card(_draft(status=ObjectStatus.INGESTING), ONE_REF)
                is None
            )


class TestToolGating:
    def test_unavailable_when_quast_is_missing(self):
        """The failing direction -- the image ships QUAST installed, so the
        available branch alone would pass whether or not this patch took
        effect."""
        with _patched(quast=_QUAST_MISSING):
            card = build_misassembly_card(_draft(), ONE_REF)
        assert card.status is CardStatus.UNAVAILABLE
        assert "not found" in card.reason


class TestReferenceGating:
    def test_unavailable_without_a_reference(self):
        with _patched():
            card = build_misassembly_card(_draft(), [])
        assert card.status is CardStatus.UNAVAILABLE
        assert "reference assembly" in card.reason

    def test_unavailable_with_two_candidate_references(self):
        """The ordinary case in a real project, not the edge case -- this
        must refuse rather than pick one, since a card cannot host a
        chooser. The manual dialog is where launch happens here."""
        with _patched():
            card = build_misassembly_card(_draft(), TWO_REFS)
        assert card.status is CardStatus.UNAVAILABLE
        assert "2 reference assemblies" in card.reason

    def test_available_with_exactly_one_reference(self):
        with _patched():
            card = build_misassembly_card(_draft(), ONE_REF)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/misassemblies"
        assert card.launch["body"] == {
            "draft_object_id": "draft1",
            "reference_object_id": "ref1",
        }

    def test_why_names_the_reference_it_will_use(self):
        with _patched():
            card = build_misassembly_card(_draft(), ONE_REF)
        assert "ref.fasta" in card.why


class TestNotGatedOnAssemblyProvenance:
    def test_offered_for_an_assembly_of_unknown_origin(self):
        with _patched():
            card = build_misassembly_card(_draft(role=None), ONE_REF)
        assert card.status is CardStatus.AVAILABLE

    def test_offered_for_an_object_already_marked_reference(self):
        """An assembly BioFlow produced carries ObjectRole.REFERENCE
        (results.py:1246), so it is in the reference pool by default --
        this must still be offered as a *draft* here, distinct from the
        exclusion that keeps it out of its own candidate list."""
        with _patched():
            card = build_misassembly_card(_draft(role=ObjectRole.REFERENCE), ONE_REF)
        assert card.status is CardStatus.AVAILABLE


class TestCategory:
    def test_category_is_assembly_qc_not_reference_assembly(self):
        """This evaluates an assembly rather than improving it -- unlike
        polish and scaffold, which sit under REFERENCE_ASSEMBLY -- so it is
        grouped with the completeness card instead."""
        with _patched():
            card = build_misassembly_card(_draft(), ONE_REF)
        assert card.category == "ASSEMBLY_QC"
