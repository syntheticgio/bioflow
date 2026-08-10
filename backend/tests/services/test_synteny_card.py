"""The Actions-tab card for minimap2 synteny alignment.

Same reasoning test_misassembly_card.py documents, its closest sibling (the
identical draft-plus-reference input shape, reusing the exact same
`scaffold_references` list): the image ships minimap2 installed, so
asserting *availability* alone would pass whether or not a patch to
`tools.minimap2` took effect. The failing direction is the one that proves
the seam.

The dedup-by-`blob_sha256` behavior is this card's own addition, not shared
with `build_scaffold_card`/`build_misassembly_card` -- see
`build_synteny_card`'s docstring in suggestion_service.py for why: two
separate `DataObject` uploads of byte-identical content are possible in
this system, and without deduping this card would refuse as ambiguous for
what is, on disk, a single reference genome.
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.models import FormatKind, ObjectRole, ObjectStatus
from app.pipelines.tools import Tool
from app.services.suggestion_service import CardStatus, build_synteny_card

_MINIMAP2 = Tool(name="minimap2", path="/usr/local/bin/minimap2", version="2.28")
_MINIMAP2_MISSING = Tool(name="minimap2", path=None, version=None, error="not found")


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


def _reference(name, oid, *, blob_sha256=None):
    return SimpleNamespace(
        id=oid, name=name, blob_sha256=blob_sha256 or f"sha-{oid}"
    )


ONE_REF = [_reference("ref.fasta", "ref1")]
TWO_REFS = [_reference("ref_gca.fasta", "ref1"), _reference("ref_gcf.fasta", "ref2")]


def _patched(minimap2=_MINIMAP2):
    return patch(
        "app.services.suggestion_service.tools.minimap2", return_value=minimap2
    )


class TestAnchorShape:
    def test_no_card_for_reads(self):
        with _patched():
            assert build_synteny_card(_draft(kind=FormatKind.FASTQ), ONE_REF) is None

    def test_no_card_for_an_alignment(self):
        with _patched():
            assert build_synteny_card(_draft(kind=FormatKind.BAM), ONE_REF) is None

    def test_no_card_for_protein_fasta(self):
        """The `protein.faa` case, which is the bug this repo already
        shipped once against these same tool families."""
        with _patched():
            assert (
                build_synteny_card(_draft(role=ObjectRole.PROTEIN), ONE_REF) is None
            )

    def test_no_card_for_transcript_fasta(self):
        with _patched():
            assert (
                build_synteny_card(_draft(role=ObjectRole.TRANSCRIPT), ONE_REF) is None
            )

    def test_no_card_for_an_unready_object(self):
        with _patched():
            assert (
                build_synteny_card(_draft(status=ObjectStatus.INGESTING), ONE_REF)
                is None
            )


class TestToolGating:
    def test_unavailable_when_minimap2_is_missing(self):
        """The failing direction -- the image ships minimap2 installed, so
        the available branch alone would pass whether or not this patch
        took effect."""
        with _patched(minimap2=_MINIMAP2_MISSING):
            card = build_synteny_card(_draft(), ONE_REF)
        assert card.status is CardStatus.UNAVAILABLE
        assert "not found" in card.reason


class TestReferenceGating:
    def test_unavailable_without_a_reference(self):
        with _patched():
            card = build_synteny_card(_draft(), [])
        assert card.status is CardStatus.UNAVAILABLE
        assert "reference genome" in card.reason

    def test_unavailable_with_two_candidate_references(self):
        """The ordinary case in a real project, not the edge case -- this
        must refuse rather than pick one, since a card cannot host a
        chooser. The manual dialog is where launch happens here."""
        with _patched():
            card = build_synteny_card(_draft(), TWO_REFS)
        assert card.status is CardStatus.UNAVAILABLE
        assert "2 reference assemblies" in card.reason

    def test_available_with_exactly_one_reference(self):
        with _patched():
            card = build_synteny_card(_draft(), ONE_REF)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/synteny"
        assert card.launch["body"] == {
            "draft_object_id": "draft1",
            "reference_object_id": "ref1",
        }

    def test_why_names_the_reference_it_will_use(self):
        with _patched():
            card = build_synteny_card(_draft(), ONE_REF)
        assert "ref.fasta" in card.why


class TestDigestDedup:
    """Two `DataObject` records can share one blob -- a second upload of
    byte-identical content still gets its own object row, since
    `object_service` inserts the object before checking blob-level dedup.
    Without collapsing by `blob_sha256` here, that project would see this
    card refuse as ambiguous for what is really one distinct reference."""

    def test_two_records_sharing_one_blob_count_as_one_reference(self):
        same_blob = [
            _reference("upload1.fasta", "ref1", blob_sha256="sameblob"),
            _reference("upload2.fasta", "ref2", blob_sha256="sameblob"),
        ]
        with _patched():
            card = build_synteny_card(_draft(), same_blob)
        assert card.status is CardStatus.AVAILABLE

    def test_two_distinct_blobs_are_still_ambiguous(self):
        with _patched():
            card = build_synteny_card(_draft(), TWO_REFS)
        assert card.status is CardStatus.UNAVAILABLE


class TestNotGatedOnAssemblyProvenance:
    def test_offered_for_an_assembly_of_unknown_origin(self):
        with _patched():
            card = build_synteny_card(_draft(role=None), ONE_REF)
        assert card.status is CardStatus.AVAILABLE

    def test_offered_for_an_object_already_marked_reference(self):
        """An assembly BioFlow produced carries ObjectRole.REFERENCE
        (results.py:1246), so it is in the reference pool by default --
        this must still be offered as a *draft* here, distinct from the
        exclusion that keeps it out of its own candidate list."""
        with _patched():
            card = build_synteny_card(_draft(role=ObjectRole.REFERENCE), ONE_REF)
        assert card.status is CardStatus.AVAILABLE


class TestCategory:
    def test_category_is_assembly_qc_not_reference_assembly(self):
        """This evaluates an assembly against a reference rather than
        improving it -- unlike polish and scaffold, which sit under
        REFERENCE_ASSEMBLY -- so it is grouped with misassembly and
        completeness instead."""
        with _patched():
            card = build_synteny_card(_draft(), ONE_REF)
        assert card.category == "ASSEMBLY_QC"
