"""The Actions-tab card for assembly completeness scoring.

Every availability case here patches `tools.compleasm` at the
`suggestion_service` import site and, separately, `lineage_handlers.
lineage_present` -- neither goes through a frozen-dataclass-captured probe the
way assembler_registry's specs do, so there is no equivalent seam trap to
document. But the image ships compleasm installed once the Dockerfile change
lands, so asserting *availability* alone would pass whether or not a patch
here actually took effect -- the same reasoning TestAssembleCard's own class
docstring gives. The failing-direction test below is the one that matters.
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.models import FormatKind, ObjectRole, ObjectStatus
from app.pipelines.tools import Tool
from app.services.suggestion_service import CardStatus, build_completeness_card

_AVAILABLE = Tool(name="compleasm", path="/usr/local/bin/compleasm", version="0.2.9")
_MISSING = Tool(name="compleasm", path=None, version=None, error="not found")


def _fasta(
    *,
    role=ObjectRole.REFERENCE,
    organism="Saccharomyces cerevisiae S288C",
    kind=FormatKind.FASTA,
    status=ObjectStatus.READY,
):
    return SimpleNamespace(
        id="assembly1",
        format=SimpleNamespace(kind=kind),
        facts={},
        metadata={"organism": organism} if organism else {},
        role=role,
        status=status,
        project_id="proj1",
        owner="local",
    )


class TestCompletenessCardShape:
    def test_not_offered_for_a_fastq(self):
        assert build_completeness_card(_fasta(kind=FormatKind.FASTQ)) is None

    def test_not_offered_for_a_protein_fasta(self):
        """protein.faa is FormatKind.FASTA and would pass any "does this look
        like a genome" sniff test that is not role-based -- the exact trap
        the align card already had to learn."""
        assert build_completeness_card(_fasta(role=ObjectRole.PROTEIN)) is None

    def test_not_offered_for_a_transcript_fasta(self):
        assert build_completeness_card(_fasta(role=ObjectRole.TRANSCRIPT)) is None

    def test_offered_for_an_unset_role(self):
        """An uploaded assembly is as eligible as a produced one -- this card
        has no derived_from walk to lean on instead of the role check."""
        with (
            patch("app.services.suggestion_service.tools.compleasm", return_value=_AVAILABLE),
            patch(
                "app.queue.lineage_handlers.lineage_present", return_value=True
            ),
        ):
            card = build_completeness_card(_fasta(role=None))
        assert card is not None


class TestCompletenessCardAvailability:
    def test_available_when_installed_lineage_present_and_organism_known(self):
        with (
            patch("app.services.suggestion_service.tools.compleasm", return_value=_AVAILABLE),
            patch(
                "app.queue.lineage_handlers.lineage_present", return_value=True
            ),
        ):
            card = build_completeness_card(_fasta())
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/completeness"
        assert card.launch["body"]["lineage"] == "saccharomycetaceae"
        assert card.launch["body"]["odb"] == "odb12"

    def test_it_flips_to_unavailable_when_compleasm_is_absent(self):
        """The direction that fails when the patch seam breaks: the image
        ships compleasm installed once the Dockerfile change lands, so
        asserting availability alone would pass whether or not the patch
        worked."""
        with patch(
            "app.services.suggestion_service.tools.compleasm", return_value=_MISSING
        ):
            card = build_completeness_card(_fasta())
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason.lower() or "not found" in card.reason.lower()

    def test_unavailable_reason_names_no_organism_not_the_tool(self):
        """Two different refusals, kept distinct like the assemble card's
        two: a missing tool and a missing fact are different things the user
        can fix."""
        with patch(
            "app.services.suggestion_service.tools.compleasm", return_value=_AVAILABLE
        ):
            card = build_completeness_card(_fasta(organism=None))
        assert card.status is CardStatus.UNAVAILABLE
        assert "organism" in card.reason.lower()

    def test_unavailable_reason_names_the_missing_lineage_not_the_organism(self):
        with (
            patch("app.services.suggestion_service.tools.compleasm", return_value=_AVAILABLE),
            patch(
                "app.queue.lineage_handlers.lineage_present", return_value=False
            ),
        ):
            card = build_completeness_card(_fasta())
        assert card.status is CardStatus.UNAVAILABLE
        assert "download" in card.reason.lower()

    def test_the_why_names_the_organism_and_the_lineage(self):
        with (
            patch("app.services.suggestion_service.tools.compleasm", return_value=_AVAILABLE),
            patch(
                "app.queue.lineage_handlers.lineage_present", return_value=True
            ),
        ):
            card = build_completeness_card(_fasta())
        assert "Saccharomyces cerevisiae" in card.why
        assert "saccharomycetaceae" in card.why
