"""Rules that turn a file's facts into pipeline suggestions.

Table-driven because the rules are a mapping, not an algorithm: the value is
in pinning each branch, especially the ones whose ordering is load-bearing.
"""

from unittest.mock import patch

import pytest

from app.models import FormatKind
from app.services.suggestion_service import (
    CardStatus,
    ReferenceChoice,
    SuggestionCard,
    build_align_card,
    build_preprocess_card,
    is_eukaryotic,
    resolve_reference,
)


class TestGenusClassification:
    @pytest.mark.parametrize(
        "organism",
        ["Escherichia coli", "escherichia coli K-12", "Bacillus subtilis",
         "Staphylococcus aureus"],
    )
    def test_known_prokaryote_genera_are_not_eukaryotic(self, organism):
        assert is_eukaryotic(organism) is False

    @pytest.mark.parametrize(
        "organism",
        ["Homo sapiens", "Saccharomyces cerevisiae S288C",
         "Trypanosoma brucei brucei", "Lycoris aurea"],
    )
    def test_known_eukaryote_genera_are_eukaryotic(self, organism):
        assert is_eukaryotic(organism) is True

    def test_unrecognised_genus_defaults_to_eukaryotic(self):
        """Splice-aware alignment on an intron-free genome degrades
        gracefully; the reverse loses real junctions silently."""
        assert is_eukaryotic("Wobblia lunata") is True

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_missing_organism_defaults_to_eukaryotic(self, value):
        assert is_eukaryotic(value) is True


class TestCardDefaults:
    def test_available_card_carries_a_launch_payload(self):
        card = SuggestionCard(
            kind="preprocess",
            category="PREPROCESS",
            title="Trim & filter -- fastp",
            description="Adapter trim and length filter.",
            why="Short reads.",
            status=CardStatus.AVAILABLE,
            launch={"endpoint": "/pipelines/trim", "body": {"object_id": "abc"}},
        )
        assert card.as_dict()["status"] == "available"
        assert card.as_dict()["launch"]["endpoint"] == "/pipelines/trim"

    def test_unavailable_card_has_no_launch_and_carries_a_reason(self):
        card = SuggestionCard(
            kind="assemble",
            category="ASSEMBLE",
            title="De novo assembly",
            description="Assemble reads into contigs.",
            why=None,
            status=CardStatus.UNAVAILABLE,
            reason="No assembler is installed.",
        )
        data = card.as_dict()
        assert data["launch"] is None
        assert data["reason"] == "No assembler is installed."


class _FakeTool:
    def __init__(self, available: bool, version: str = "0.23.4"):
        self.available = available
        self.version = version


def _fake_obj(kind=FormatKind.FASTQ, facts=None, metadata=None, obj_id="abc123"):
    """A stand-in for DataObject carrying only what the rules read.

    A real Beanie document would need a database; the rules are pure
    functions of these attributes, so a namespace is enough. `id` is here
    because the launch body carries it -- the card assembles the complete
    request body server-side.
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        id=obj_id,
        format=SimpleNamespace(kind=kind),
        facts=facts or {},
        metadata=metadata or {},
    )


class TestPreprocessCard:
    def test_not_offered_for_a_bam(self):
        assert build_preprocess_card(_fake_obj(kind=FormatKind.BAM)) is None

    def test_available_for_a_fastq_with_no_qc_yet(self):
        """Not gated on chemistry: fastp's defaults are safe either way, and
        gating it would leave a fresh FASTQ with nothing runnable at all."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)):
            card = build_preprocess_card(_fake_obj())
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/trim"
        assert card.launch["body"]["tool"] == "fastp"
        assert card.launch["body"]["object_id"] == "abc123"
        # Tool settings nest under params -- TrimRequest's shape, not flat.
        assert "params" in card.launch["body"]

    def test_unavailable_when_fastp_is_not_installed(self):
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(False)):
            card = build_preprocess_card(_fake_obj())
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "fastp" in card.reason

    def test_long_read_and_short_read_cards_have_different_copy(self):
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)):
            long_read_card = build_preprocess_card(
                _fake_obj(facts={"qc_read_chemistry": "ont_simplex"})
            )
            short_read_card = build_preprocess_card(_fake_obj())
        assert long_read_card.description != short_read_card.description
        assert long_read_card.why != short_read_card.why

    def test_unrecognised_chemistry_degrades_to_a_card_rather_than_raising(self):
        """qc_read_chemistry is tool-written data, not a validated enum -- a
        stale or unrecognised value falls back to the short-read copy rather
        than blowing up the card grid."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)):
            card = build_preprocess_card(
                _fake_obj(facts={"qc_read_chemistry": "martian_reads"})
            )
        assert card.status is CardStatus.AVAILABLE
        assert card.description == "Adapter trim and length filter."


def _ref(object_id: str, name: str):
    from types import SimpleNamespace
    return SimpleNamespace(id=object_id, name=name)


class TestReferenceResolution:
    def test_exactly_one_uploaded_reference_is_used(self):
        refs = [_ref("aaa", "GCF_000005845.2.fna")]
        choice = resolve_reference(refs, organism="Escherichia coli")
        assert choice.reference_id == "aaa"
        assert choice.usable is True

    def test_one_uploaded_reference_beats_a_known_organism(self):
        """Load-bearing ordering. Reversing it makes the card refuse a
        perfectly good local reference in favour of an unfetchable one --
        worse behaviour on a better-configured project."""
        refs = [_ref("aaa", "local.fna")]
        choice = resolve_reference(refs, organism="Saccharomyces cerevisiae")
        assert choice.usable is True
        assert choice.reference_id == "aaa"

    def test_no_references_but_known_organism_names_the_species(self):
        choice = resolve_reference([], organism="Saccharomyces cerevisiae")
        assert choice.usable is False
        assert "Saccharomyces cerevisiae" in choice.reason
        assert "not wired up" in choice.reason

    def test_many_references_with_known_organism_names_the_species(self):
        refs = [_ref("bbb", "b.fna"), _ref("aaa", "a.fna")]
        choice = resolve_reference(refs, organism="Escherichia coli")
        assert choice.usable is False
        assert "Escherichia coli" in choice.reason

    def test_many_references_without_organism_picks_deterministically(self):
        """Sorted by id, first. A random pick would name a different
        reference on each render, which reads as a bug."""
        refs = [_ref("ccc", "c.fna"), _ref("aaa", "a.fna"), _ref("bbb", "b.fna")]
        first = resolve_reference(refs, organism=None)
        second = resolve_reference(list(reversed(refs)), organism=None)
        assert first.reference_id == "aaa"
        assert second.reference_id == "aaa"
        assert first.usable is True

    def test_nothing_at_all_asks_for_an_upload(self):
        choice = resolve_reference([], organism=None)
        assert choice.usable is False
        assert "Upload a reference" in choice.reason

    def test_blank_organism_is_treated_as_absent(self):
        """Metadata carries empty strings and whitespace, not just None."""
        refs = [_ref("ccc", "c.fna"), _ref("aaa", "a.fna")]
        choice = resolve_reference(refs, organism="   ")
        assert choice.usable is True
        assert choice.reference_id == "aaa"


@pytest.fixture
def all_aligners_installed():
    """Pin every tool probe the align card can reach.

    Without this the rules are read through whatever happens to be on the
    host: `default_align_params` picks bwa-mem2 only when it is installed,
    and this repo runs on both arm64 (where it never is) and x86-64. A test
    whose expected aligner depends on the machine pins nothing.

    Patched at two seams because two modules probe: `pipeline_service` for
    the delegated choice, `suggestion_service` for the availability gate.
    """
    with (
        patch("app.services.pipeline_service.tools.bwa_mem2",
              return_value=_FakeTool(True)),
        patch("app.services.suggestion_service.tools.bwa_mem2",
              return_value=_FakeTool(True)),
        patch("app.services.suggestion_service.tools.minimap2",
              return_value=_FakeTool(True)),
        patch("app.services.suggestion_service.tools.hisat2",
              return_value=_FakeTool(True)),
    ):
        yield


@pytest.mark.usefixtures("all_aligners_installed")
class TestAlignCard:
    def test_not_offered_for_a_bam(self):
        assert build_align_card(_fake_obj(kind=FormatKind.BAM), []) is None

    def test_unknown_chemistry_gates_the_card(self):
        card = build_align_card(_fake_obj(), [_ref("aaa", "ref.fna")])
        assert card.status is CardStatus.UNAVAILABLE
        assert "Run QC" in card.reason

    def test_long_reads_pick_minimap2_with_the_matching_preset(self):
        obj = _fake_obj(facts={"qc_read_chemistry": "ont_simplex"})
        card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        assert card.status is CardStatus.AVAILABLE
        assert "minimap2" in card.title
        assert card.launch["body"]["params"]["preset"] == "map-ont"

    def test_rna_seq_on_a_eukaryote_picks_hisat2(self):
        obj = _fake_obj(
            facts={"qc_read_chemistry": "short"},
            metadata={"assay": "RNA-seq", "organism": "Saccharomyces cerevisiae"},
        )
        card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        assert card.launch["body"]["params"]["aligner"] == "hisat2"
        assert "splice" in card.why.lower()

    def test_rna_seq_on_a_bacterium_does_not_pick_hisat2(self):
        """Bacteria have no introns, so splice-awareness is wrong there."""
        obj = _fake_obj(
            facts={"qc_read_chemistry": "short"},
            metadata={"assay": "RNA-seq", "organism": "Escherichia coli"},
        )
        card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        assert card.launch["body"]["params"]["aligner"] != "hisat2"

    def test_both_gates_failing_names_the_reference_first(self):
        """Reference first because it is actionable without waiting on a job."""
        card = build_align_card(_fake_obj(), [])
        assert card.status is CardStatus.UNAVAILABLE
        assert card.reason.index("Upload a reference") < card.reason.index("Run QC")

    def test_available_card_carries_a_complete_align_request_body(self):
        obj = _fake_obj(facts={"qc_read_chemistry": "short"})
        card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        body = card.launch["body"]
        assert card.launch["endpoint"] == "/pipelines/align"
        assert body["reference_id"] == "aaa"
        assert body["object_id"] == "abc123"
        assert "aligner" in body["params"]
        # read_group is the server's to fill from default_read_group.
        assert "read_group" not in body

    def test_long_read_rna_seq_does_not_become_hisat2(self):
        """hisat2 is a short-read aligner; ONT RNA-seq must stay on minimap2."""
        obj = _fake_obj(
            facts={"qc_read_chemistry": "ont_simplex"},
            metadata={"assay": "RNA-seq", "organism": "Homo sapiens"},
        )
        card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        assert card.launch["body"]["params"]["aligner"] == "minimap2"
