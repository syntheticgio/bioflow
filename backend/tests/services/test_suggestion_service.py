"""Rules that turn a file's facts into pipeline suggestions.

Table-driven because the rules are a mapping, not an algorithm: the value is
in pinning each branch, especially the ones whose ordering is load-bearing.
"""

import dataclasses
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models import FormatKind
from app.pipelines import align_runner, aligner_registry
from app.services.suggestion_service import (
    CardStatus,
    ReferenceChoice,
    SuggestionCard,
    build_align_card,
    build_assemble_card,
    build_preprocess_card,
    build_variants_card,
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
    def __init__(self, available: bool, version: str = "0.23.4", name: str = "tool"):
        self.available = available
        self.version = version
        # The align card puts this in the reason, so it has to be the real
        # binary name rather than a placeholder.
        self.name = name


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


@contextmanager
def installed_tools(**overrides):
    """Pin the tool probes the align card reads. Everything installed by
    default; pass e.g. `hisat2_build=False` to take one away.

    Two seams, because the card reads probes two different ways.

    `pipeline_service.tools.bwa_mem2` drives the *delegated choice* and is a
    plain module-attribute lookup, so it patches normally.

    The *gate* resolves through `aligner_registry`, whose frozen specs
    captured `tools.minimap2` and friends as function objects at import time.
    Patching `app.pipelines.tools.minimap2` therefore does NOT reach
    `spec.tool`, which still holds the original -- so `spec_for` is patched to
    hand back specs rebuilt with the probes the test asked for.

    Without this the rules are read through whatever the host happens to have:
    `default_align_params` picks bwa-mem2 only where it is installed, and this
    repo runs on both arm64 (where it never is) and x86-64. A test whose
    expected aligner depends on the machine pins nothing.
    """
    available = {
        "bwa_mem2": True, "minimap2": True, "hisat2": True,
        "hisat2_build": True, "bowtie2": True, "bowtie2_build": True,
    }
    unknown = set(overrides) - set(available)
    assert not unknown, f"unknown tool(s): {sorted(unknown)}"
    available.update(overrides)

    def probe(binary: str) -> _FakeTool:
        return _FakeTool(available[binary.replace("-", "_")], name=binary)

    def fake_spec_for(aligner):
        real = aligner_registry.REGISTRY[aligner]
        changes = {"tool": lambda: probe(real.aligner.value)}
        if real.builder_tool is not None:
            changes["builder_tool"] = lambda: probe(real.index.builder)
        return dataclasses.replace(real, **changes)

    with (
        patch("app.services.pipeline_service.tools.bwa_mem2",
              return_value=probe("bwa-mem2")),
        patch("app.services.suggestion_service.aligner_registry.spec_for",
              side_effect=fake_spec_for),
    ):
        yield


@pytest.fixture
def all_aligners_installed():
    with installed_tools():
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

    def test_a_missing_aligner_gates_an_otherwise_runnable_card(self):
        """The third gate, on its own: chemistry and reference are both fine."""
        obj = _fake_obj(facts={"qc_read_chemistry": "short"})
        # bwa-mem2 absent moves the delegated choice to minimap2, which the
        # gate then finds missing too -- so this pins the gate rather than
        # the fallback.
        with installed_tools(bwa_mem2=False, minimap2=False):
            card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "minimap2" in card.reason

    def test_a_missing_tool_suppresses_the_other_two_reasons(self):
        """Tool wins outright rather than joining the list. Neither uploading
        a reference nor running QC makes the card runnable while the binary is
        absent, so naming them would send the user off to do useless work."""
        with installed_tools(bwa_mem2=False, minimap2=False):
            card = build_align_card(_fake_obj(), [])
        assert card.status is CardStatus.UNAVAILABLE
        assert "minimap2" in card.reason
        assert "Upload a reference" not in card.reason
        assert "Run QC" not in card.reason

    def test_the_gated_card_still_describes_what_it_would_do(self):
        """An unavailable card is still the user's explanation of the step, so
        it keeps its title and description -- only `why` and `launch` are the
        available card's alone."""
        card = build_align_card(_fake_obj(), [_ref("aaa", "ref.fna")])
        assert card.status is CardStatus.UNAVAILABLE
        assert "BAM" in card.title
        assert card.description == "Align to ref.fna, sort and index."
        assert card.launch is None

    def test_the_qc_written_chemistry_reason_becomes_the_why(self):
        """QC already justified the chemistry in prose; the card surfaces that
        rather than inventing a second, vaguer account of the same call."""
        obj = _fake_obj(facts={
            "qc_read_chemistry": "hifi",
            "qc_read_chemistry_reason": "Median read length 15.2 kb at Q31.",
        })
        card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        assert card.why == "Median read length 15.2 kb at Q31."

    def test_a_gated_card_without_a_reference_still_names_the_step(self):
        card = build_align_card(_fake_obj(), [])
        assert card.description == (
            "Align these reads against a reference, sort and index."
        )

    def _rna_euk_obj(self):
        return _fake_obj(
            facts={"qc_read_chemistry": "short"},
            metadata={"assay": "RNA-seq", "organism": "Homo sapiens"},
        )

    def test_a_missing_index_builder_gates_the_card(self):
        """hisat2 indexes through a separate binary, configured by its own
        setting. Gating only on the aligner would render an available card
        whose launch succeeds and then fails inside the async index job."""
        with installed_tools(hisat2_build=False):
            card = build_align_card(self._rna_euk_obj(), [_ref("aaa", "ref.fna")])
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None

    def test_the_gate_names_the_binary_that_is_actually_missing(self):
        """"hisat2 is not installed" would send the user to check the wrong
        thing when hisat2-build is the one that is absent."""
        with installed_tools(hisat2_build=False):
            card = build_align_card(self._rna_euk_obj(), [_ref("aaa", "ref.fna")])
        assert "hisat2-build" in card.reason

    def test_an_aligner_with_no_separate_builder_is_not_gated_on_one(self):
        """bwa-mem2 and minimap2 index through the aligner itself, so a
        builder gate must not invent a second binary for them."""
        obj = _fake_obj(facts={"qc_read_chemistry": "short"})
        with installed_tools(hisat2_build=False, bowtie2_build=False):
            card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["body"]["params"]["aligner"] == "bwa-mem2"

    def test_hisat2_is_available_when_both_its_binaries_are(self):
        with installed_tools():
            card = build_align_card(self._rna_euk_obj(), [_ref("aaa", "ref.fna")])
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["body"]["params"]["aligner"] == "hisat2"

    def test_an_aligner_outside_the_registry_fails_loudly(self):
        """A card must never quietly report "installed" for a binary nobody
        probed. A silent `.get()` here would render a launchable card the
        align endpoint then refuses, so the miss has to raise."""
        obj = _fake_obj(facts={"qc_read_chemistry": "short"})
        with patch(
            "app.services.suggestion_service.pipeline_service.default_align_params",
            return_value={"aligner": "snap", "threads": 4},
        ):
            with pytest.raises(ValueError):
                build_align_card(obj, [_ref("aaa", "ref.fna")])


@contextmanager
def installed_callers(clair3=True, bcftools=True):
    """Pin the two variant-caller probes.

    Safe to patch this way -- unlike the aligners, which reach their probes
    through `aligner_registry`'s frozen specs, the variants card calls
    `tools.clair3()` and `tools.bcftools()` as plain module-attribute lookups
    on the name `suggestion_service` imported. Patching that name therefore
    does reach the call; `test_the_caller_patch_actually_takes_effect` pins
    that rather than trusting it, because a patch that misses would leave
    every test below silently reading whatever this host has installed.
    """
    with (
        patch("app.services.suggestion_service.tools.clair3",
              return_value=_FakeTool(clair3, name="clair3")),
        patch("app.services.suggestion_service.tools.bcftools",
              return_value=_FakeTool(bcftools, name="bcftools")),
    ):
        yield


@pytest.fixture
def all_callers_installed():
    with installed_callers():
        yield


def _bam(chemistry_facts=None, obj_id="bam456"):
    return _fake_obj(kind=FormatKind.BAM, facts=chemistry_facts, obj_id=obj_id)


@pytest.mark.usefixtures("all_callers_installed")
class TestVariantsCard:
    def test_the_caller_patch_actually_takes_effect(self):
        """Guards every other test in this class. If the seam were wrong the
        probes would read the host, and "clair3 is installed" would depend on
        the machine rather than on what the test asked for."""
        from app.services import suggestion_service

        with installed_callers(clair3=False, bcftools=False):
            assert suggestion_service.tools.clair3().available is False
            assert suggestion_service.tools.bcftools().available is False
            assert isinstance(suggestion_service.tools.clair3(), _FakeTool)

    def test_not_offered_for_a_fastq(self):
        assert build_variants_card(
            _fake_obj(), align_runner.ReadChemistry.SHORT
        ) is None

    def test_long_reads_pick_clair3(self):
        card = build_variants_card(_bam(), align_runner.ReadChemistry.ONT_SIMPLEX)
        assert card.status is CardStatus.AVAILABLE
        assert "Clair3" in card.title
        assert card.launch["body"]["params"]["caller"] == "clair3"

    def test_short_reads_pick_bcftools(self):
        card = build_variants_card(_bam(), align_runner.ReadChemistry.SHORT)
        assert card.status is CardStatus.AVAILABLE
        assert "bcftools" in card.title
        assert card.launch["body"]["params"]["caller"] == "bcftools"

    def test_hifi_picks_clair3(self):
        card = build_variants_card(_bam(), align_runner.ReadChemistry.HIFI)
        assert card.launch["body"]["params"]["caller"] == "clair3"

    def test_the_launch_body_keys_on_bam_id_not_object_id(self):
        """`/pipelines/variants` is the one endpoint of the three that keys on
        `bam_id`. Sending `object_id` 422s at runtime."""
        card = build_variants_card(_bam(), align_runner.ReadChemistry.SHORT)
        body = card.launch["body"]
        assert card.launch["endpoint"] == "/pipelines/variants"
        assert body["bam_id"] == "bam456"
        assert "object_id" not in body

    def test_the_reference_is_left_for_the_server_to_resolve(self):
        """`reference_for_bam` reads it out of the BAM's provenance, which is
        a database lookup this card has no business doing."""
        card = build_variants_card(_bam(), align_runner.ReadChemistry.SHORT)
        assert "reference_id" not in card.launch["body"]

    def test_unknown_chemistry_gates_the_card(self):
        card = build_variants_card(_bam(), None)
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert card.reason == "Unknown sequencing platform for this BAM."

    def test_clr_is_refused_outright(self):
        """Clair3 is trained on high-accuracy reads; at CLR's error rate it
        produces calls that look ordinary and are wrong. Refusing beats
        emitting a VCF nothing downstream flags."""
        card = build_variants_card(_bam(), align_runner.ReadChemistry.CLR)
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "CLR" in card.reason
        assert "HiFi" in card.reason

    def test_the_clr_refusal_matches_the_launch_paths_wording(self):
        """`caller_for_chemistry` raises on CLR. The card is the same refusal
        rendered rather than raised, so the two must not drift apart."""
        from app.errors import ValidationError
        from app.pipelines import variant_runner

        with pytest.raises(ValidationError) as excinfo:
            variant_runner.caller_for_chemistry(align_runner.ReadChemistry.CLR)
        card = build_variants_card(_bam(), align_runner.ReadChemistry.CLR)
        assert card.reason == str(excinfo.value)

    def test_a_missing_clair3_gates_a_long_read_card(self):
        with installed_callers(clair3=False):
            card = build_variants_card(
                _bam(), align_runner.ReadChemistry.ONT_SIMPLEX
            )
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "clair3" in card.reason

    def test_a_missing_bcftools_gates_a_short_read_card(self):
        with installed_callers(bcftools=False):
            card = build_variants_card(_bam(), align_runner.ReadChemistry.SHORT)
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "bcftools" in card.reason

    def test_the_other_callers_absence_does_not_gate_the_chosen_one(self):
        """Only the caller this chemistry would actually run is probed."""
        with installed_callers(bcftools=False):
            card = build_variants_card(
                _bam(), align_runner.ReadChemistry.ONT_SIMPLEX
            )
        assert card.status is CardStatus.AVAILABLE

    def test_the_gated_card_still_describes_what_it_would_do(self):
        card = build_variants_card(_bam(), None)
        assert card.title
        assert card.description


class TestAssembleCard:
    def test_not_offered_for_a_bam(self):
        assert build_assemble_card(_fake_obj(kind=FormatKind.BAM)) is None

    def test_offered_for_a_fastq_but_never_runnable(self):
        """Shown rather than hidden so the card count stays stable across
        files and the capability stays discoverable."""
        card = build_assemble_card(_fake_obj())
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None

    def test_the_reason_names_the_missing_assembler_not_the_dag(self):
        """Both "no assembler" and "no pipeline system" are true, but the
        absent binary is the blocking constraint and the honest one."""
        card = build_assemble_card(_fake_obj())
        assert card.reason == "No assembler is installed."
        assert "pipeline" not in card.reason.lower()
        assert "DAG" not in card.reason

    def test_it_stays_unavailable_whatever_the_chemistry(self):
        for chem in ("short", "ont_simplex", "hifi", "clr", None):
            card = build_assemble_card(
                _fake_obj(facts={"qc_read_chemistry": chem} if chem else {})
            )
            assert card.status is CardStatus.UNAVAILABLE
