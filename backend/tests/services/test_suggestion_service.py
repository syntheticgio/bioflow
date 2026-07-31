"""Rules that turn a file's facts into pipeline suggestions.

Table-driven because the rules are a mapping, not an algorithm: the value is
in pinning each branch, especially the ones whose ordering is load-bearing.
"""

import dataclasses
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models import FormatKind, ObjectRole, ObjectStatus
from app.pipelines import align_runner, aligner_registry
from app.services import pipeline_service
from app.services.suggestion_service import (
    CardStatus,
    ReferenceChoice,
    SuggestionCard,
    build_align_card,
    build_annotate_card,
    build_assemble_card,
    build_preprocess_card,
    build_variants_card,
    is_eukaryotic,
    resolve_reference,
    suggestions_for,
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


def _fake_obj(
    kind=FormatKind.FASTQ,
    facts=None,
    metadata=None,
    obj_id="abc123",
    status=ObjectStatus.READY,
    project_id="proj1",
):
    """A stand-in for DataObject carrying only what the rules read.

    A real Beanie document would need a database; the rules are pure
    functions of these attributes, so a namespace is enough. `id` is here
    because the launch body carries it -- the card assembles the complete
    request body server-side.

    `status` and `project_id` are read only by `suggestions_for`, the one
    non-pure function here; the individual builders never look at them.
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        id=obj_id,
        format=SimpleNamespace(kind=kind),
        facts=facts or {},
        metadata=metadata or {},
        status=status,
        project_id=project_id,
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

    def test_the_same_assembly_stored_twice_is_still_one_reference(self):
        """Found on live data: a project holding two copies of
        GCF_000002445.2_ASM244v1_genomic.fna counted as two references, so it
        skipped the single-reference branch and refused to align against a
        genome plainly sitting in the project. The align *button* above the
        card already collapses these by assembly name; the card has to agree
        with it."""
        refs = [
            _ref("aaa", "GCF_000002445.2_ASM244v1_genomic.fna"),
            _ref("bbb", "GCF_000002445.2_ASM244v1_genomic.fna"),
        ]
        choice = resolve_reference(refs, organism="Trypanosoma brucei brucei")
        assert choice.usable is True
        # The oldest copy, deterministically.
        assert choice.reference_id == "aaa"

    def test_two_genuinely_different_assemblies_are_two_references(self):
        """The dedup must not collapse distinct genomes -- that would pick one
        arbitrarily where the organism branch is the honest answer."""
        refs = [
            _ref("aaa", "GCF_000002445.2_ASM244v1_genomic.fna"),
            _ref("bbb", "GCF_000001405.40_GRCh38.p14_genomic.fna"),
        ]
        choice = resolve_reference(refs, organism="Homo sapiens")
        assert choice.usable is False
        assert "Homo sapiens" in choice.reason

    def test_unparseable_names_are_never_treated_as_duplicates(self):
        """Two files whose names carry no assembly cannot be shown to be the
        same genome, so each stays its own candidate."""
        refs = [_ref("aaa", "my_reference.fasta"), _ref("bbb", "other.fasta")]
        choice = resolve_reference(refs, organism=None)
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
        the machine rather than on what the test asked for.

        This matters more than it looks: the CI image ships both binaries, so
        the available-card tests below assert exactly the value an escaped
        patch would also produce. They cannot themselves tell a working seam
        from a broken one -- this test is what does, by driving the probes to
        False and checking the *card* changes, not merely the probe."""
        from app.services import suggestion_service

        with installed_callers(clair3=False, bcftools=False):
            assert suggestion_service.tools.clair3().available is False
            assert suggestion_service.tools.bcftools().available is False
            assert isinstance(suggestion_service.tools.clair3(), _FakeTool)
            # The card, not just the probe: this is the assertion that would
            # fail if the patch stopped reaching the call site.
            card = build_variants_card(_bam(), align_runner.ReadChemistry.SHORT)
            assert card.status is CardStatus.UNAVAILABLE

    def test_unknown_chemistry_enum_is_not_the_same_as_no_chemistry(self):
        """`ReadChemistry.UNKNOWN` is a value QC can write, distinct from the
        None that means nothing was resolved. It falls through to bcftools --
        `caller_for_chemistry`'s documented behaviour -- rather than hitting
        the "Unknown sequencing platform" gate, which is about absence."""
        card = build_variants_card(_bam(), align_runner.ReadChemistry.UNKNOWN)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["body"]["params"]["caller"] == "bcftools"

    def test_ont_duplex_picks_clair3(self):
        card = build_variants_card(_bam(), align_runner.ReadChemistry.ONT_DUPLEX)
        assert card.launch["body"]["params"]["caller"] == "clair3"

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


def _vcf(obj_id="vcf789", kind=FormatKind.VCF):
    return _fake_obj(kind=kind, obj_id=obj_id)


@contextmanager
def installed_csq(available=True, error=None):
    """Pin the bcftools-csq probe the annotate card reads.

    A plain module-attribute lookup, like the variants card's caller probes
    above -- not a frozen-spec seam like the aligners', since `bcftools_csq`
    is called directly rather than through a registry.
    """
    with patch(
        "app.services.suggestion_service.tools.bcftools_csq",
        return_value=_FakeTool(available, name="bcftools csq"),
    ) as probe:
        # `_FakeTool` has no `error` attribute by default; the card reads
        # `.error` only on the unavailable path, so it is added here rather
        # than widening the shared fake for every other test.
        probe.return_value.error = error
        yield


class TestAnnotateCard:
    """Per CLAUDE.md, assert the *unavailable* direction hardest: the image
    ships bcftools 1.21, so an availability assertion passes whether or not
    the seam it depends on actually works."""

    def test_the_csq_patch_actually_takes_effect(self):
        """Guards every other test in this class. bcftools 1.21 ships in the
        image and `bcftools_csq` is lru_cached, so the available-card tests
        assert exactly the value an escaped patch would also produce -- they
        cannot themselves tell a working seam from a broken one.

        The card assertion is the load-bearing one. Reading the probe back
        only proves `patch` replaced the attribute, which it always does; it
        would still pass if `build_annotate_card` reached some other reference
        to `bcftools_csq` entirely. Driving the *card* to UNAVAILABLE is what
        fails when the patch stops reaching the call site."""
        from app.services import suggestion_service

        with installed_csq(False, error="nope"):
            assert not suggestion_service.tools.bcftools_csq().available
            card = build_annotate_card(_vcf(), None)
            assert card.status is CardStatus.UNAVAILABLE
            assert card.reason == "nope"

    def test_no_card_on_a_non_vcf(self):
        with installed_csq(True):
            assert build_annotate_card(_bam(), None) is None

    def test_available_when_inputs_resolve(self):
        vcf = _vcf()
        reference = _ref("ref1", "GCF_000002445.2_ASM244v1_genomic.fna")
        annotation = _ref("gff1", "annotation.gff3")
        inputs = pipeline_service.AnnotationInputs(
            ok=True, reference=reference, annotation=annotation
        )
        with installed_csq(True):
            card = build_annotate_card(vcf, inputs)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/annotate"
        assert card.launch["body"] == {"object_id": "vcf789"}

    def test_a_bcf_is_also_offered_the_card(self):
        """`FormatKind.BCF` is the binary sibling of VCF; both are called
        variants, so both should be able to reach the same card."""
        vcf = _vcf(kind=FormatKind.BCF)
        reference = _ref("ref1", "ref.fna")
        annotation = _ref("gff1", "annotation.gff3")
        inputs = pipeline_service.AnnotationInputs(
            ok=True, reference=reference, annotation=annotation
        )
        with installed_csq(True):
            card = build_annotate_card(vcf, inputs)
        assert card is not None
        assert card.status is CardStatus.AVAILABLE

    def test_unavailable_reason_comes_from_the_resolver(self):
        inputs = pipeline_service.AnnotationInputs(
            ok=False, reason="No annotation (GFF3) for this reference."
        )
        with installed_csq(True):
            card = build_annotate_card(_vcf(), inputs)
        assert card.status is CardStatus.UNAVAILABLE
        assert card.reason == "No annotation (GFF3) for this reference."
        assert card.launch is None

    def test_unavailable_when_csq_is_missing(self):
        reference = _ref("ref1", "ref.fna")
        annotation = _ref("gff1", "annotation.gff3")
        inputs = pipeline_service.AnnotationInputs(
            ok=True, reference=reference, annotation=annotation
        )
        with installed_csq(False, error="bcftools csq requires bcftools >= 1.12."):
            card = build_annotate_card(_vcf(), inputs)
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "csq" in card.reason.lower()


@contextmanager
def stub_db(references=(), chemistry=None, annotation_inputs=None):
    """Cut the three database seams `suggestions_for` reaches through.

    `suggestions_for` is the one function in this module that is not pure: it
    lists a project's references (for the align card), walks a BAM's
    provenance for chemistry (for the variants card), and resolves a VCF's
    reference/GFF3 pair (for the annotate card). All three are patched here
    rather than backed by the Beanie fixture because everything being asserted
    -- which cards appear, in what order -- is decided above those calls, and
    a real database would only add setup that pins none of it.

    `list_objects` is patched to return exactly what the filter should keep,
    so the assertions below stay about card assembly. Whether the filter is
    correctly pushed into the query is a `list_objects` contract, covered
    where that function lives.

    `patch` autospecs an `async def` target to an AsyncMock, so `return_value`
    is what the awaited call yields -- handing it a coroutine function via
    `side_effect` would return a coroutine object the caller then treats as
    the list.
    """
    with (
        patch("app.services.object_service.list_objects",
              return_value=[_as_reference(r) for r in references]),
        patch("app.services.pipeline_service.read_chemistry_for_alignment",
              return_value=chemistry),
        patch("app.services.pipeline_service.resolve_annotation_inputs",
              return_value=annotation_inputs),
    ):
        yield


def _as_reference(ref, *, kind=FormatKind.FASTA, role=ObjectRole.REFERENCE):
    """Give a `_ref` the fields `suggestions_for`'s listing filter reads.

    `_ref` deliberately carries only what `resolve_reference` reads; the
    filter above it reads `format.kind` and `role`. Both are overridable so a
    test can hand the filter something it must reject.
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        id=ref.id,
        name=ref.name,
        format=SimpleNamespace(kind=kind),
        role=role,
    )


CARD_KEYS = {
    "kind", "category", "title", "description",
    "why", "status", "reason", "launch",
}


@pytest.mark.usefixtures("all_aligners_installed", "all_callers_installed")
class TestSuggestionsFor:
    async def test_a_file_that_is_not_ready_gets_no_cards(self):
        """Nothing to suggest for a file still ingesting or errored -- the
        launch endpoints would refuse every one of them anyway."""
        for status in (
            ObjectStatus.UPLOADING,
            ObjectStatus.INGESTING,
            ObjectStatus.ERROR,
            ObjectStatus.MISSING,
        ):
            with stub_db():
                assert await suggestions_for(_fake_obj(status=status)) == []

    async def test_a_fastq_gets_preprocess_align_and_assemble(self):
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)), stub_db(
                       references=[_ref("aaa", "ref.fna")]):
            cards = await suggestions_for(_fake_obj())
        assert [c["kind"] for c in cards] == ["preprocess", "align", "assemble"]

    async def test_a_fastq_never_gets_a_variants_card(self):
        """Variants are called on an alignment, not on reads."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)), stub_db():
            cards = await suggestions_for(_fake_obj())
        assert "variants" not in [c["kind"] for c in cards]

    async def test_a_bam_gets_only_the_variants_card(self):
        with stub_db(chemistry=align_runner.ReadChemistry.SHORT):
            cards = await suggestions_for(_bam())
        assert [c["kind"] for c in cards] == ["variants"]

    async def test_a_vcf_gets_only_the_annotate_card(self):
        inputs = pipeline_service.AnnotationInputs(
            ok=True,
            reference=_ref("aaa", "ref.fna"),
            annotation=_ref("bbb", "annotation.gff3"),
        )
        with installed_csq(True), stub_db(annotation_inputs=inputs):
            cards = await suggestions_for(_vcf())
        assert [c["kind"] for c in cards] == ["annotate"]

    async def test_the_order_does_not_move_with_availability(self):
        """Fixed order, not sorted by availability: a card that changes
        position between files makes the grid something to re-read rather
        than scan."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)), stub_db(references=[]):
            # No reference, so align is unavailable and assemble always is.
            cards = await suggestions_for(_fake_obj())
        assert [c["kind"] for c in cards] == ["preprocess", "align", "assemble"]
        assert cards[1]["status"] == "unavailable"

    async def test_protein_and_transcript_fasta_are_not_counted_as_references(self):
        """Caught on live data, not by these tests.

        A project that downloaded an assembly from NCBI also holds
        `protein.faa` and `cds_from_genomic.fna` -- FASTA files that are not
        genomes to align against. Counting them made a project with ONE real
        reference look like it had three, which pushed `resolve_reference`
        past its single-reference branch into "fetching a genome for
        <organism> is not wired up yet" while a usable reference sat right
        there in the project.
        """
        listed = [
            _as_reference(_ref("aaa", "GCF_000002445.2_genomic.fna")),
            _as_reference(_ref("bbb", "protein.faa"), role=ObjectRole.PROTEIN),
            _as_reference(
                _ref("ccc", "cds_from_genomic.fna"), role=ObjectRole.TRANSCRIPT
            ),
        ]
        with patch(
            "app.services.object_service.list_objects", return_value=listed
        ), patch(
            "app.services.pipeline_service.read_chemistry_for_alignment",
            return_value=None,
        ), patch(
            "app.services.suggestion_service.tools.fastp",
            return_value=_FakeTool(True),
        ):
            cards = await suggestions_for(
                _fake_obj(
                    facts={"qc_read_chemistry": "short"},
                    metadata={"organism": "Trypanosoma brucei brucei"},
                )
            )

        align = next(c for c in cards if c["kind"] == "align")
        # The one real reference wins outright; the organism branch never runs.
        assert align["status"] == "available"
        assert align["launch"]["body"]["reference_id"] == "aaa"

    async def test_one_failing_builder_does_not_take_the_grid_with_it(self):
        """The grid is advisory -- every operation on it is also reachable
        through Computations -- so a contract drift in one card must not cost
        the user the other three. Several builders raise deliberately when an
        upstream assumption moves; that loudness belongs to the card, not to
        the tab."""
        with patch(
            "app.services.suggestion_service.build_align_card",
            side_effect=ValueError("'snap' is not a valid Aligner"),
        ), patch(
            "app.services.suggestion_service.tools.fastp",
            return_value=_FakeTool(True),
        ), stub_db(references=[_ref("aaa", "ref.fna")]):
            cards = await suggestions_for(_fake_obj())

        # Align is gone; the other two survive in their usual order.
        assert [c["kind"] for c in cards] == ["preprocess", "assemble"]

    async def test_every_card_is_a_plain_dict_with_the_full_key_set(self):
        """This goes out as JSON -- a SuggestionCard would not serialise, and
        a missing key would read as `undefined` in the grid."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)), stub_db(
                       references=[_ref("aaa", "ref.fna")]):
            cards = await suggestions_for(_fake_obj())
        assert cards
        for card in cards:
            assert type(card) is dict
            assert set(card) == CARD_KEYS

    async def test_references_are_not_fetched_for_a_bam(self):
        """The align card is the only consumer, and it is FASTQ-only. Listing
        a project's objects to build a card that will not be built is a query
        per BAM click for nothing."""
        with patch("app.services.object_service.list_objects") as listing:
            with patch(
                "app.services.pipeline_service.read_chemistry_for_alignment",
                return_value=align_runner.ReadChemistry.SHORT,
            ):
                await suggestions_for(_bam())
        listing.assert_not_called()

    async def test_chemistry_is_not_resolved_for_a_fastq(self):
        """`read_chemistry_for_alignment` walks a BAM's provenance to find the
        FASTQ behind it. On a FASTQ that walk is a database round trip to
        rediscover the object already in hand -- the synchronous
        `read_chemistry` the builders call is enough."""
        with patch(
            "app.services.pipeline_service.read_chemistry_for_alignment"
        ) as resolve:
            with patch("app.services.suggestion_service.tools.fastp",
                       return_value=_FakeTool(True)):
                with patch("app.services.object_service.list_objects",
                           return_value=[]):
                    await suggestions_for(_fake_obj())
        resolve.assert_not_called()

    async def test_annotation_inputs_are_not_resolved_for_a_bam(self):
        """The symmetric cost guard the other two formats each have:
        `resolve_annotation_inputs` walks provenance to a reference and lists
        the project for a GFF3, and hoisting that out of its VCF/BCF `if`
        would pay it on every BAM click for nothing."""
        with patch(
            "app.services.pipeline_service.resolve_annotation_inputs"
        ) as resolve:
            with patch(
                "app.services.pipeline_service.read_chemistry_for_alignment",
                return_value=align_runner.ReadChemistry.SHORT,
            ):
                await suggestions_for(_bam())
        resolve.assert_not_called()

    async def test_the_ready_filter_is_pushed_into_the_listing_query(self):
        """Filtering after the fact would let a project's non-ready objects
        eat the result limit and drop references silently."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)):
            with patch("app.services.object_service.list_objects",
                       return_value=[]) as listing:
                await suggestions_for(_fake_obj())
        assert listing.call_args.kwargs["status"] is ObjectStatus.READY


class TestSuggestionsEndpoint:
    """The HTTP surface only. Which cards come back is settled above, at the
    service level; what is left here is the id-to-object lookup in front of
    it, which is the endpoint's own code."""

    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.v1.pipelines import router
        from app.errors import register_exception_handlers

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router)
        return TestClient(app)

    def test_unknown_object_id_is_a_404_not_an_empty_grid(self, client):
        """An empty `suggestions` list would render as "nothing to do here",
        which is a different and wrong answer to "that file does not exist"."""
        with patch("app.api.v1.pipelines.DataObject.get", return_value=None):
            resp = client.get(
                "/pipelines/suggestions/000000000000000000000001"
            )
        assert resp.status_code == 404

    def test_a_malformed_object_id_is_rejected_before_the_lookup(self, client):
        resp = client.get("/pipelines/suggestions/not-an-object-id")
        assert resp.status_code == 422

    def test_a_found_object_is_answered_with_its_cards(self, client):
        with patch("app.api.v1.pipelines.DataObject.get",
                   return_value=_bam()), stub_db(
                       chemistry=align_runner.ReadChemistry.SHORT), \
                installed_callers():
            resp = client.get(
                "/pipelines/suggestions/000000000000000000000001"
            )
        assert resp.status_code == 200
        assert [c["kind"] for c in resp.json()["suggestions"]] == ["variants"]
