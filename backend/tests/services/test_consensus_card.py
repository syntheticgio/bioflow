"""The Actions-tab card for iVar consensus calling.

Same reasoning test_completeness_card.py documents: the image ships iVar
installed once the Dockerfile change lands, so asserting *availability*
alone would pass whether or not a patch to `tools.ivar` actually took
effect. The failing-direction test is the one that matters.

`build_consensus_card` takes the BAM's already-resolved alignment target
(or None) rather than resolving it itself, the same reasoning `chemistry`
is resolved in the orchestrator and passed into `build_variants_card`:
resolving provenance is an async database walk, and every builder in this
module is deliberately synchronous and pure.
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.models import FormatKind, ObjectRole, ObjectStatus
from app.pipelines.tools import Tool
from app.services.suggestion_service import CardStatus, build_consensus_card

_AVAILABLE = Tool(name="ivar", path="/usr/bin/ivar", version="1.4.4")
_MISSING = Tool(name="ivar", path=None, version=None, error="not found")


def _bam(*, kind=FormatKind.BAM, status=ObjectStatus.READY):
    return SimpleNamespace(
        id="bam1",
        name="aln.bam",
        format=SimpleNamespace(kind=kind),
        role=ObjectRole.ALIGNMENT,
        status=status,
        project_id="proj1",
        owner="local",
    )


def _reference(*, name="ref.fasta"):
    return SimpleNamespace(id="ref1", name=name, role=ObjectRole.REFERENCE)


class TestConsensusCardShape:
    def test_not_offered_for_a_fasta(self):
        obj = _bam(kind=FormatKind.FASTA)
        assert build_consensus_card(obj, reference=_reference()) is None

    def test_not_offered_for_a_fastq(self):
        obj = _bam(kind=FormatKind.FASTQ)
        assert build_consensus_card(obj, reference=_reference()) is None

    def test_offered_for_a_cram(self):
        with patch("app.services.suggestion_service.tools.ivar", return_value=_AVAILABLE):
            card = build_consensus_card(
                _bam(kind=FormatKind.CRAM), reference=_reference()
            )
        assert card is not None


class TestConsensusCardAvailability:
    def test_available_when_installed_and_target_resolved(self):
        with patch("app.services.suggestion_service.tools.ivar", return_value=_AVAILABLE):
            card = build_consensus_card(_bam(), reference=_reference())
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/consensus"
        assert card.launch["body"]["bam_object_id"] == "bam1"

    def test_it_flips_to_unavailable_when_ivar_is_absent(self):
        """The direction that fails when the patch seam breaks: the image
        ships iVar installed once the Dockerfile change lands, so asserting
        availability alone would pass whether or not the patch worked."""
        with patch("app.services.suggestion_service.tools.ivar", return_value=_MISSING):
            card = build_consensus_card(_bam(), reference=_reference())
        assert card.status is CardStatus.UNAVAILABLE
        # tool.error ("not found") is what a real probe failure carries --
        # matches build_completeness_card's own reason (`tool.error or
        # "compleasm is not installed."`), so this asserts the same shape.
        assert "not found" in card.reason.lower() or "not installed" in card.reason.lower()

    def test_unavailable_when_no_target_could_be_resolved(self):
        """reference=None is what the orchestrator passes when
        resolve_alignment_target_for_bam raised -- no recorded target, or
        an ambiguous one. The card must say so rather than crash or offer a
        launch with nothing to consense against."""
        with patch("app.services.suggestion_service.tools.ivar", return_value=_AVAILABLE):
            card = build_consensus_card(_bam(), reference=None)
        assert card.status is CardStatus.UNAVAILABLE
        assert "reference" in card.reason.lower()

    def test_the_why_names_the_reference(self):
        with patch("app.services.suggestion_service.tools.ivar", return_value=_AVAILABLE):
            card = build_consensus_card(
                _bam(), reference=_reference(name="MN908947.3.fasta")
            )
        assert "MN908947.3.fasta" in card.why

    def test_not_gated_on_the_reference_looking_viral(self):
        """The protein.faa trap in a new costume: a genome-size or organism
        check would refuse a legitimate bacterial or plasmid consensus. The
        card only checks that a target was resolved, not what it looks
        like."""
        with patch("app.services.suggestion_service.tools.ivar", return_value=_AVAILABLE):
            card = build_consensus_card(
                _bam(), reference=_reference(name="ecoli_k12.fasta")
            )
        assert card.status is CardStatus.AVAILABLE
