"""The Actions-tab card for Polypolish short-read polishing.

Same reasoning test_consensus_card.py and test_completeness_card.py
document: the image ships Polypolish installed on x86_64, so asserting
*availability* alone would pass whether or not a patch to `tools.polypolish`
took effect. The failing direction is the one that proves the seam.

`build_polish_card` takes the project's already-resolved short-read sets
rather than listing the project itself, the same reasoning the consensus
card takes a pre-resolved alignment target: the builders in that module are
deliberately synchronous and pure.
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.models import FormatKind, ObjectRole, ObjectStatus
from app.pipelines.tools import Tool
from app.services.suggestion_service import CardStatus, build_polish_card

_PP = Tool(name="polypolish", path="/usr/local/bin/polypolish", version="0.7.1")
_PP_MISSING = Tool(name="polypolish", path=None, version=None, error="not found")
_BWA = Tool(name="bwa-mem2", path="/usr/local/bin/bwa-mem2", version="2.2.1")
_BWA_MISSING = Tool(name="bwa-mem2", path=None, version=None, error="not found")


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


def _reads(name, oid):
    return SimpleNamespace(id=oid, name=name, format=SimpleNamespace(kind=FormatKind.FASTQ))


PAIR = [[_reads("r_1.fastq", "r1"), _reads("r_2.fastq", "r2")]]
SINGLE = [[_reads("r.fastq", "r1")]]


def _patched(pp=_PP, bwa=_BWA):
    return (
        patch("app.services.suggestion_service.tools.polypolish", return_value=pp),
        patch("app.services.suggestion_service.tools.bwa_mem2", return_value=bwa),
    )


class TestAnchorShape:
    def test_no_card_for_reads(self):
        with _patched()[0], _patched()[1]:
            assert build_polish_card(_draft(kind=FormatKind.FASTQ), PAIR) is None

    def test_no_card_for_an_alignment(self):
        with _patched()[0], _patched()[1]:
            assert build_polish_card(_draft(kind=FormatKind.BAM), PAIR) is None

    def test_no_card_for_protein_fasta(self):
        """The `protein.faa` case, which is the bug this repo already shipped
        once: FASTA bytes whose biological meaning is not an assembly."""
        with _patched()[0], _patched()[1]:
            assert build_polish_card(_draft(role=ObjectRole.PROTEIN), PAIR) is None

    def test_no_card_for_transcript_fasta(self):
        """`cds_from_genomic.fna` -- the other half of the same bug."""
        with _patched()[0], _patched()[1]:
            assert build_polish_card(_draft(role=ObjectRole.TRANSCRIPT), PAIR) is None

    def test_no_card_for_an_unready_object(self):
        with _patched()[0], _patched()[1]:
            assert build_polish_card(_draft(status=ObjectStatus.INGESTING), PAIR) is None


class TestToolGating:
    def test_unavailable_when_polypolish_is_missing(self):
        """The direction that fails when the patch seam breaks."""
        a, b = _patched(pp=_PP_MISSING)
        with a, b:
            card = build_polish_card(_draft(), PAIR)
        assert card.status is CardStatus.UNAVAILABLE
        assert "not found" in card.reason

    def test_unavailable_when_the_aligner_is_missing(self):
        """Polypolish needs all-alignment input, which bwa-mem2 provides.
        Saying only "Polypolish is installed" would be misleading here --
        and this is the real arm64 state, where upstream ships no build."""
        a, b = _patched(bwa=_BWA_MISSING)
        with a, b:
            card = build_polish_card(_draft(), PAIR)
        assert card.status is CardStatus.UNAVAILABLE
        assert "bwa-mem2" in card.reason or "not found" in card.reason


class TestReadGating:
    def test_unavailable_without_short_reads(self):
        a, b = _patched()
        with a, b:
            card = build_polish_card(_draft(), [])
        assert card.status is CardStatus.UNAVAILABLE
        assert "short reads" in card.reason

    def test_unavailable_when_several_read_sets_could_be_meant(self):
        """Cards launch straight into the queue with no dialog in between,
        so picking one of several would silently polish with whichever was
        chosen -- a plausible assembly that is quietly wrong."""
        a, b = _patched()
        with a, b:
            card = build_polish_card(_draft(), PAIR + SINGLE)
        assert card.status is CardStatus.UNAVAILABLE
        assert "2 short-read sets" in card.reason

    def test_available_with_one_pair_and_carries_both_ids(self):
        a, b = _patched()
        with a, b:
            card = build_polish_card(_draft(), PAIR)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/polish"
        assert card.launch["body"] == {
            "draft_object_id": "draft1",
            "reads_object_id": "r1",
            "mate_object_id": "r2",
        }

    def test_available_with_single_end_reads_omits_the_mate(self):
        a, b = _patched()
        with a, b:
            card = build_polish_card(_draft(), SINGLE)
        assert card.status is CardStatus.AVAILABLE
        assert "mate_object_id" not in card.launch["body"]

    def test_why_names_the_reads_it_will_use(self):
        """The user is about to launch a run with no dialog; which reads it
        picked is the one thing they cannot otherwise see."""
        a, b = _patched()
        with a, b:
            card = build_polish_card(_draft(), PAIR)
        assert "r_1.fastq" in card.why and "r_2.fastq" in card.why


class TestNotGatedOnAssemblyProvenance:
    def test_offered_for_an_assembly_of_unknown_origin(self):
        """Deliberately not gated on the draft looking long-read-derived.

        BioFlow often cannot know how an imported assembly was produced, and
        polishing a hybrid or short-read assembly is unusual rather than
        wrong. The safe rule is about the reads, not the draft.
        """
        a, b = _patched()
        with a, b:
            card = build_polish_card(_draft(role=None), PAIR)
        assert card.status is CardStatus.AVAILABLE

    def test_offered_for_an_object_marked_reference(self):
        """A polished assembly is stored with role REFERENCE, so polishing
        one again must stay possible -- iterative polishing is a real, if
        uncommon, workflow."""
        a, b = _patched()
        with a, b:
            card = build_polish_card(_draft(role=ObjectRole.REFERENCE), PAIR)
        assert card.status is CardStatus.AVAILABLE
