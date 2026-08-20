"""Rules that turn a file's facts into pipeline suggestions.

Table-driven because the rules are a mapping, not an algorithm: the value is
in pinning each branch, especially the ones whose ordering is load-bearing.
"""

import dataclasses
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from beanie import PydanticObjectId

from app.models import FormatKind, ObjectRole, ObjectStatus, SidecarRole
from app.pipelines import align_runner, aligner_registry, assembler_registry, tools
from app.pipelines.assemblers import Assembler
from app.services import pipeline_service
from app.services.suggestion_service import (
    CARD_BUILDERS,
    CardStatus,
    SuggestionCard,
    build_align_card,
    build_annotate_card,
    build_assemble_card,
    build_classify_reads_card,
    build_coverage_card,
    build_feature_coverage_card,
    build_gc_bias_card,
    build_merge_structural_variants_card,
    build_methylation_card,
    build_multiqc_card,
    build_preprocess_card,
    build_quantify_card,
    build_salmon_quantify_card,
    build_structural_variants_card,
    build_transcript_assembly_card,
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
    def __init__(
        self,
        available: bool,
        version: str = "0.23.4",
        name: str = "tool",
        install_state=None,
        error: str | None = None,
    ):
        self.available = available
        self.error = None
        self.version = version
        # Real `tools.Tool` carries this on every instance, and several
        # cards read `tool.error or "<fallback>"` when building an
        # unavailable reason. Without it here the fake diverges from the
        # thing it stands in for, and a card doing the normal thing raises
        # AttributeError in tests while working in production.
        self.error = error
        # The align card puts this in the reason, so it has to be the real
        # binary name rather than a placeholder.
        self.name = name
        # None for every ordinary tool, matching the real Tool dataclass's
        # default -- only an ON_DEMAND_IMAGE probe sets this. Tests of the
        # NEEDS_INSTALL path pass tools.InstallState.NOT_INSTALLED or .UNKNOWN
        # explicitly; every other test leaves it None, which is also what
        # `not available` alone used to mean before this field existed.
        self.install_state = install_state


def _fake_obj(
    kind=FormatKind.FASTQ,
    facts=None,
    metadata=None,
    obj_id="abc123",
    status=ObjectStatus.READY,
    project_id="proj1",
    role=None,
    blob_sha256=None,
):
    """A stand-in for DataObject carrying only what the rules read.

    A real Beanie document would need a database; the rules are pure
    functions of these attributes, so a namespace is enough. `id` is here
    because the launch body carries it -- the card assembles the complete
    request body server-side.

    `status`, `project_id` and `owner` are read only by `suggestions_for`, the
    one non-pure function here; the individual builders never look at them.
    `owner` joins them now that the reference listing is owner-scoped -- a card
    must not be built from another profile's references.
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        id=obj_id,
        format=SimpleNamespace(kind=kind),
        facts=facts or {},
        metadata=metadata or {},
        status=status,
        project_id=project_id,
        owner="local",
        # Read by resolve_alignment_target_for_bam's provenance walk, which
        # suggestions_for now calls for every BAM/SAM/CRAM to build the
        # consensus card's alignment_target.
        derived_from=[],
        # Read by build_preprocess_card to flag already-trimmed reads as
        # unavailable for re-trimming; None means "not a trim output".
        role=role,
        # Read by build_methylation_card's K1 prefix scan to locate the
        # BAM's bytes on disk. None for every other card's fixtures, which
        # never read the file itself.
        blob_sha256=blob_sha256,
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
        arbitrarily where refusing is the honest answer."""
        refs = [
            _ref("aaa", "GCF_000002445.2_ASM244v1_genomic.fna"),
            _ref("bbb", "GCF_000001405.40_GRCh38.p14_genomic.fna"),
        ]
        choice = resolve_reference(refs, organism="Homo sapiens")
        assert choice.usable is False

    def test_several_references_are_not_described_as_a_missing_download(self):
        """The refusal must not claim a genome needs fetching when the project
        holds two. That sentence describes an empty project, and it rendered
        beside two usable references -- so the card read as broken rather than
        as declining to choose. It names the count and the align dialog now."""
        refs = [
            _ref("aaa", "GCF_000002445.2_ASM244v1_genomic.fna"),
            _ref("bbb", "GCF_000001405.40_GRCh38.p14_genomic.fna"),
        ]
        choice = resolve_reference(refs, organism="Homo sapiens")
        assert choice.usable is False
        assert "not wired up" not in choice.reason
        assert "Homo sapiens" not in choice.reason
        assert "2 references" in choice.reason
        assert "Align" in choice.reason

    def test_no_references_still_names_the_species_it_cannot_fetch(self):
        """The other half of the split. With nothing in the project, fetching
        genuinely is the missing capability, and naming the species is the
        most useful thing the card can say."""
        choice = resolve_reference([], organism="Homo sapiens")
        assert choice.usable is False
        assert "Homo sapiens" in choice.reason
        assert "not wired up" in choice.reason

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

    def test_many_references_with_known_organism_refuses_by_count(self):
        refs = [_ref("bbb", "b.fna"), _ref("aaa", "a.fna")]
        choice = resolve_reference(refs, organism="Escherichia coli")
        assert choice.usable is False
        assert "2 references" in choice.reason

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
        "star": True,
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

    def test_rna_seq_stays_on_hisat2_even_with_star_installed(self):
        """STAR is the faster RNA-seq aligner and the conventional one, so
        this is a decision rather than an oversight: its index needs ~30 GB
        resident against HISAT2's ~4 GB, and a suggestion the memory
        estimator then blocks is advice that cannot be taken. STAR stays a
        deliberate pick from the align dialog.

        Asserted rather than left implicit because the natural next edit to
        this rule is to prefer STAR when it is installed."""
        obj = _fake_obj(
            facts={"qc_read_chemistry": "short"},
            metadata={"assay": "RNA-seq", "organism": "Saccharomyces cerevisiae"},
        )
        card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        assert card.launch["body"]["params"]["aligner"] != "star"

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
def installed_callers(
    clair3=True, bcftools=True, deepvariant=True, deepvariant_install_state=None
):
    """Pin the three variant-caller probes.

    Safe to patch this way -- unlike the aligners, which reach their probes
    through `aligner_registry`'s frozen specs, the variants card calls
    `tools.clair3()`, `tools.bcftools()` and `tools.deepvariant()` as plain
    module-attribute lookups on the name `suggestion_service` imported.
    Patching that name therefore does reach the call;
    `test_the_caller_patch_actually_takes_effect` pins that rather than
    trusting it, because a patch that misses would leave every test below
    silently reading whatever this host has installed.

    `deepvariant_install_state` is separate from `deepvariant` (available or
    not) because NEEDS_INSTALL needs a *third* value the plain boolean cannot
    express: not merely unavailable, but unavailable *because it has not been
    pulled yet* (tools.InstallState.NOT_INSTALLED) rather than because the
    daemon cannot be reached (.UNKNOWN) or is genuinely broken. Defaults to
    None, matching every existing call site's assumption that DeepVariant's
    unavailability carries no install-state distinction -- only the
    NEEDS_INSTALL tests below pass it explicitly.
    """
    with (
        patch("app.services.suggestion_service.tools.clair3",
              return_value=_FakeTool(clair3, name="clair3")),
        patch("app.services.suggestion_service.tools.bcftools",
              return_value=_FakeTool(bcftools, name="bcftools")),
        patch("app.services.suggestion_service.tools.deepvariant",
              return_value=_FakeTool(
                  deepvariant, name="deepvariant", install_state=deepvariant_install_state
              )),
    ):
        yield


@pytest.fixture
def all_callers_installed():
    with installed_callers():
        yield


def _bam(chemistry_facts=None, obj_id="bam456", blob_sha256=None):
    return _fake_obj(
        kind=FormatKind.BAM, facts=chemistry_facts, obj_id=obj_id, blob_sha256=blob_sha256
    )


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
        """Clair3 stays the preferred long-read caller -- DeepVariant is not
        the automatic default (see the design doc) -- so this only gates when
        DeepVariant is unavailable too."""
        with installed_callers(clair3=False, deepvariant=False):
            card = build_variants_card(
                _bam(), align_runner.ReadChemistry.ONT_SIMPLEX
            )
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "clair3" in card.reason

    def test_a_missing_clair3_falls_back_to_deepvariant_when_it_is_installed(self):
        """The rule DeepVariant's probe makes newly pickable: Clair3 remains
        preferred (this is a fallback, not a new default), but a card that
        would otherwise sit gated for a missing binary can instead run
        against an installed alternative. The direction that fails when the
        seam breaks: patch DeepVariant off too, in the test above, and the
        card must go back to gated."""
        with installed_callers(clair3=False, deepvariant=True):
            card = build_variants_card(
                _bam(), align_runner.ReadChemistry.ONT_SIMPLEX
            )
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["body"]["params"]["caller"] == "deepvariant"

    def test_a_not_yet_installed_deepvariant_offers_needs_install(self):
        """The regression this task exists to fix: an installable-but-not-
        pulled DeepVariant must not be treated as unavailable *or* silently
        substituted -- it must offer the install, with a real launch payload
        the confirm-then-chain flow (task 7) can act on."""
        with installed_callers(
            clair3=False,
            deepvariant=False,
            deepvariant_install_state=tools.InstallState.NOT_INSTALLED,
        ):
            card = build_variants_card(
                _bam(), align_runner.ReadChemistry.ONT_SIMPLEX
            )
        assert card.status is CardStatus.NEEDS_INSTALL
        assert card.launch is not None
        assert card.launch["body"]["params"]["caller"] == "deepvariant"
        assert card.launch["body"]["bam_id"] == "bam456"
        # The card already states the download size in requires_install
        # before it can be clicked, so pressing it *is* the consent
        # _require_or_offer_install (task 7) asks for -- without this flag
        # in the posted body, clicking the card would hit the refusal
        # instead of the install-and-chain path.
        assert card.launch["body"]["install_optional"] is True

    def test_needs_install_names_the_tool_and_its_size(self):
        with installed_callers(
            clair3=False,
            deepvariant=False,
            deepvariant_install_state=tools.InstallState.NOT_INSTALLED,
        ):
            card = build_variants_card(
                _bam(), align_runner.ReadChemistry.ONT_SIMPLEX
            )
        assert card.requires_install == {
            "tool": "deepvariant",
            "download_bytes": tools.TOOL_META["deepvariant"].download_bytes,
        }

    def test_an_unknown_deepvariant_state_does_not_offer_install(self):
        """UNKNOWN means the daemon could not be reached at all -- a fault,
        not an offer. Pressing Install would just fail again for the same
        reason, so this must fall through to the ordinary UNAVAILABLE gate
        rather than dangling a button that cannot work."""
        with installed_callers(
            clair3=False,
            deepvariant=False,
            deepvariant_install_state=tools.InstallState.UNKNOWN,
        ):
            card = build_variants_card(
                _bam(), align_runner.ReadChemistry.ONT_SIMPLEX
            )
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None

    def test_needs_install_is_only_reachable_for_long_reads(self):
        """Short reads have no DeepVariant fallback at all (see the branch's
        own comment on why) -- a not-yet-installed DeepVariant must not leak
        into the short-read gate."""
        with installed_callers(
            bcftools=False,
            deepvariant_install_state=tools.InstallState.NOT_INSTALLED,
        ):
            card = build_variants_card(_bam(), align_runner.ReadChemistry.SHORT)
        assert card.status is CardStatus.UNAVAILABLE

    def test_needs_install_reaches_the_api_payload(self):
        """The whole point of the field: it has to survive as_dict(), the
        boundary the frontend actually reads."""
        with installed_callers(
            clair3=False,
            deepvariant=False,
            deepvariant_install_state=tools.InstallState.NOT_INSTALLED,
        ):
            card = build_variants_card(
                _bam(), align_runner.ReadChemistry.ONT_SIMPLEX
            )
        payload = card.as_dict()
        assert payload["status"] == "needs_install"
        assert payload["launch"] is not None
        assert payload["requires_install"]["tool"] == "deepvariant"

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


class TestStructuralVariantsCard:
    @pytest.mark.parametrize(
        "chemistry",
        [
            align_runner.ReadChemistry.HIFI,
            align_runner.ReadChemistry.CLR,
            align_runner.ReadChemistry.ONT_SIMPLEX,
            align_runner.ReadChemistry.ONT_DUPLEX,
        ],
    )
    def test_card_is_offered_for_every_long_read_chemistry(self, chemistry):
        with patch(
            "app.services.suggestion_service.tools.sniffles",
            return_value=_FakeTool(True, name="sniffles"),
        ):
            card = build_structural_variants_card(_bam(), chemistry)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/structural_variants"
        assert card.launch["body"]["bam_id"] == "bam456"

    def test_clr_is_deliberately_allowed_for_structural_variants(self):
        """The opposite asymmetry from `TestVariantsCard`'s CLR tests.

        `variant_runner.caller_for_chemistry` refuses CLR for small-variant
        calling because Clair3's model needs per-base accuracy CLR does not
        have. Sniffles2 is deliberately different: it resolves breakpoints
        from alignment structure -- split reads and within-read gaps -- which
        tolerates CLR's high per-base error rate, and CLR reads are long,
        which is exactly the property SV detection needs. See
        `sniffles_runner.sv_calling_allowed_for`'s docstring for the same
        reasoning. This is a standalone, named assertion rather than only a
        parametrize case so that if `_LONG_READ` in `sniffles_runner.py` ever
        silently dropped CLR (e.g. someone "harmonising" it with
        `caller_for_chemistry`'s refusal), a test fails *by name* instead of
        a parametrized count quietly going from 4 to 3.
        """
        with patch(
            "app.services.suggestion_service.tools.sniffles",
            return_value=_FakeTool(True, name="sniffles"),
        ):
            card = build_structural_variants_card(
                _bam(), align_runner.ReadChemistry.CLR
            )
        assert card.status is CardStatus.AVAILABLE

    def test_card_is_unavailable_when_the_probe_fails(self):
        """The load-bearing direction.

        The image ships Sniffles installed, so asserting the card is
        *available* passes whether or not a patch worked. Only the flip to
        unavailable fails when the seam breaks. This is #619's third success
        criterion.
        """
        with patch(
            "app.services.suggestion_service.tools.sniffles",
            return_value=_FakeTool(False, name="sniffles"),
        ):
            card = build_structural_variants_card(
                _bam(), align_runner.ReadChemistry.ONT_SIMPLEX
            )
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason
        assert card.launch is None

    def test_short_reads_are_offered_delly(self):
        """#619 left this card's SHORT branch saying a different tool was
        needed. #620 is that tool: the reason is replaced by an offer on the
        same card, not supplemented by a second SV card. Requirement
        SV-620-9.

        Patches `tools.delly` rather than relying on the real environment
        having it installed: the app's Docker image does, but the CI runner
        image (`.github/Dockerfile.ci`) is a separate, hand-maintained tool
        list that does not include Delly (or Sniffles), so an unpatched
        assertion here passes locally and fails in CI. Matches the sibling
        Sniffles tests in this class, which all patch `tools.sniffles` for
        the same reason.
        """
        with patch(
            "app.services.suggestion_service.tools.delly",
            return_value=_FakeTool(True, name="delly"),
        ):
            card = build_structural_variants_card(
                _bam(), align_runner.ReadChemistry.SHORT
            )
        assert card.status is CardStatus.AVAILABLE
        assert card.launch is not None

    def test_short_read_card_is_unavailable_when_delly_is_missing(self):
        """The load-bearing direction. The image ships Delly installed, so
        asserting the card is *available* passes whether or not a patch
        worked -- only the flip to unavailable fails when the seam breaks.
        Requirement SV-620-10."""
        with patch(
            "app.services.suggestion_service.tools.delly",
            return_value=_FakeTool(False, name="delly"),
        ):
            card = build_structural_variants_card(
                _bam(), align_runner.ReadChemistry.SHORT
            )
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason
        assert card.launch is None

    def test_short_read_why_does_not_claim_parity_with_long_reads(self):
        """Paired-end and split-read signal detects SVs but resolves fewer
        of them than long reads do. A card that reads as equivalent
        misrepresents what the user gets."""
        with patch(
            "app.services.suggestion_service.tools.delly",
            return_value=_FakeTool(True, name="delly"),
        ):
            card = build_structural_variants_card(
                _bam(), align_runner.ReadChemistry.SHORT
            )
        assert "long reads span breakpoints" not in (card.why or "").lower()

    def test_unknown_chemistry_is_refused(self):
        """UNKNOWN means QC has not run. Running Sniffles on a BAM that turns
        out to be Illumina produces junk quietly."""
        card = build_structural_variants_card(
            _bam(), align_runner.ReadChemistry.UNKNOWN
        )
        assert card.status is CardStatus.UNAVAILABLE

    def test_none_chemistry_is_refused(self):
        card = build_structural_variants_card(_bam(), None)
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None

    def test_not_offered_for_a_fastq(self):
        assert build_structural_variants_card(
            _fake_obj(), align_runner.ReadChemistry.ONT_SIMPLEX
        ) is None


class TestAssembleCard:
    """The card that was permanently unavailable until Flye landed.

    Every case here patches `spec_for_chemistry` rather than `tools.flye`.
    That is not incidental: AssemblerSpec is a frozen dataclass that captured
    the probe as a function object at import time, so patching the module
    attribute never reaches `spec.tool` -- a test that appears to control the
    environment while silently reading the host's.
    """

    @staticmethod
    def _installed():
        class _Available:
            available = True

        return dataclasses.replace(
            assembler_registry.FLYE_SPEC, tool=lambda: _Available()
        )

    @staticmethod
    def _missing():
        class _Absent:
            available = False

        return dataclasses.replace(
            assembler_registry.FLYE_SPEC, tool=lambda: _Absent()
        )

    @staticmethod
    def _abyss_installed():
        class _Available:
            available = True

        return dataclasses.replace(
            assembler_registry.ABYSS_SPEC, tool=lambda: _Available()
        )

    def test_not_offered_for_a_bam(self):
        assert build_assemble_card(_fake_obj(kind=FormatKind.BAM)) is None

    def test_available_for_long_reads_when_the_assembler_is_installed(self):
        with patch.object(
            assembler_registry, "spec_for_chemistry", return_value=self._installed()
        ):
            card = build_assemble_card(
                _fake_obj(facts={"qc_read_chemistry": "ont_simplex"})
            )
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/assemble"
        # Object id only: genome-size inference walks the project, which is an
        # async read, and every builder in this module is synchronous and pure.
        assert card.launch["body"] == {"object_id": "abc123"}

    def test_the_why_names_the_chemistry_and_the_mode(self):
        """The mode is a claim about read accuracy, and it is the one thing a
        user might want to override -- so the card says which one it picked."""
        with patch.object(
            assembler_registry, "spec_for_chemistry", return_value=self._installed()
        ):
            card = build_assemble_card(
                _fake_obj(facts={"qc_read_chemistry": "ont_duplex"})
            )
        assert "duplex" in card.why.lower()
        assert "nano-hq" in card.why

    def test_it_flips_to_unavailable_when_the_assembler_is_absent(self):
        """The direction that fails when the patch seam breaks.

        Asserting *availability* would pass whether or not the patch worked,
        because the image ships Flye -- which is exactly how a test ends up
        reading the host machine while appearing to control it.
        """
        with patch.object(
            assembler_registry, "spec_for_chemistry", return_value=self._missing()
        ):
            card = build_assemble_card(
                _fake_obj(facts={"qc_read_chemistry": "hifi"})
            )
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "flye" in card.reason.lower()

    def test_short_reads_get_an_available_assemble_card(self):
        """The #490 screenshot's refusal must be gone: short reads have an
        installed assembler (ABySS, Task 4) now, so this is a card the user
        can launch, not a permanent refusal.

        Patches `spec_for_chemistry` rather than relying on the test image
        shipping ABySS, same reasoning as the Flye cases above: the card's
        own launch-time behavior is what is under test, not the host.
        """
        obj = _fake_obj(facts={"qc_read_chemistry": "short"})
        obj.name = "sample_R1.fastq.gz"

        with patch.object(
            assembler_registry,
            "spec_for_chemistry",
            return_value=self._abyss_installed(),
        ):
            card = build_assemble_card(obj)

        assert card.status is CardStatus.AVAILABLE
        assert "not installed" not in (card.reason or "")
        assert "abyss" in card.title.lower()

    def test_short_read_card_says_when_reads_are_unpaired(self):
        """`why` is filename-level only -- this builder is synchronous and
        pure and cannot look up the mate object, so it can only say what the
        name suggests. The real pairing verdict, including the read-ID veto,
        happens at launch time (Task 9)."""
        obj = _fake_obj(facts={"qc_read_chemistry": "short"})
        obj.name = "sample.fastq.gz"

        with patch.object(
            assembler_registry,
            "spec_for_chemistry",
            return_value=self._abyss_installed(),
        ):
            card = build_assemble_card(obj)

        assert card.status is CardStatus.AVAILABLE
        assert "unpaired" in card.why.lower()

    def test_short_read_card_says_when_reads_are_paired(self):
        obj = _fake_obj(facts={"qc_read_chemistry": "short"})
        obj.name = "sample_R1.fastq.gz"

        with patch.object(
            assembler_registry,
            "spec_for_chemistry",
            return_value=self._abyss_installed(),
        ):
            card = build_assemble_card(obj)

        assert card.status is CardStatus.AVAILABLE
        assert "paired" in card.why.lower()
        assert "unpaired" not in card.why.lower()

    def test_assemble_card_unavailable_when_abyss_is_missing(self):
        """The direction that fails when the patch seam breaks: the image
        ships ABySS, so asserting availability would pass whether or not the
        patch worked. Patch `spec_for` -- not `tools.abyss`, which a frozen
        dataclass captured at import time."""
        real = assembler_registry.spec_for

        def fake_spec_for(assembler):
            spec = real(assembler)
            if assembler is Assembler.ABYSS:
                return dataclasses.replace(
                    spec, tool=None, unavailable_reason="abyss is not installed."
                )
            return spec

        obj = _fake_obj(facts={"qc_read_chemistry": "short"})
        obj.name = "sample_R1.fastq.gz"

        with (
            patch.object(assembler_registry, "spec_for", fake_spec_for),
            patch.object(
                assembler_registry,
                "spec_for_chemistry",
                lambda c: fake_spec_for(Assembler.ABYSS),
            ),
        ):
            card = build_assemble_card(obj)

        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason

    def test_unknown_chemistry_still_says_run_qc(self):
        """The actionable refusal must survive -- it is a different failure
        than a missing tool."""
        obj = _fake_obj()
        obj.name = "sample.fastq.gz"

        card = build_assemble_card(obj)

        assert card.status is CardStatus.UNAVAILABLE
        assert "Run QC first" in card.reason

    def test_the_old_no_assembler_sentence_is_gone(self):
        """It was true while tools.py declared no assembler and became a lie
        the day Flye was installed -- the failure CLAUDE.md predicts by name.
        """
        with patch.object(
            assembler_registry, "spec_for_chemistry", return_value=self._installed()
        ):
            card = build_assemble_card(
                _fake_obj(facts={"qc_read_chemistry": "clr"})
            )
        assert card.reason is None


def _vcf(obj_id="vcf789", kind=FormatKind.VCF):
    return _fake_obj(kind=kind, obj_id=obj_id)


@contextmanager
def installed_csq(available=True, error=None):
    """Pin the bcftools-csq probe the annotate card reads.

    A plain module-attribute lookup, like the variants card's caller probes
    above -- not a frozen-spec seam like the aligners', since `bcftools_csq`
    is called directly rather than through a registry.

    Also patches the SnpEff probe to be available, since the annotate card
    now checks both tools. Tests that specifically test the SnpEff-unavailable
    path should use `installed_snpeff` explicitly instead.
    """
    with patch(
        "app.services.suggestion_service.tools.bcftools_csq",
        return_value=_FakeTool(available, name="bcftools csq"),
    ) as csq_probe, patch(
        "app.services.suggestion_service.tools.snpeff",
        return_value=_FakeTool(True, name="snpeff"),
    ):
        csq_probe.return_value.error = error
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
            # SnpEff is still available via the patch. Inputs is None (bad),
            # so the card shows UNAVAILABLE with the inputs reason.
            card = build_annotate_card(_vcf(), None)
            assert card.status is CardStatus.UNAVAILABLE
            assert card.reason == "Inputs could not be resolved."

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
        assert card.launch["body"] == {"object_id": "vcf789", "annotator": "bcftools_csq"}

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
        """When inputs do not resolve, the card is UNAVAILABLE with the
        resolver's reason, regardless of tool availability."""
        inputs = pipeline_service.AnnotationInputs(
            ok=False, reason="No annotation (GFF3) for this reference."
        )
        with installed_csq(True):
            card = build_annotate_card(_vcf(), inputs)
        assert card.status is CardStatus.UNAVAILABLE
        assert card.reason == "No annotation (GFF3) for this reference."
        assert card.launch is None

    def test_unavailable_when_csq_is_missing_but_snpeff_available(self):
        """When bcftools csq is missing but SnpEff is available, the card
        defaults to SnpEff rather than showing unavailable."""
        reference = _ref("ref1", "ref.fna")
        annotation = _ref("gff1", "annotation.gff3")
        inputs = pipeline_service.AnnotationInputs(
            ok=True, reference=reference, annotation=annotation
        )
        with installed_csq(False, error="bcftools csq requires bcftools >= 1.12."):
            card = build_annotate_card(_vcf(), inputs)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["body"]["annotator"] == "snpeff"

    def test_both_tools_available_offers_bcftools_csq_default(self):
        """When both SnpEff and bcftools csq are available, the card shows
        the tool picker and defaults to bcftools_csq."""
        reference = _ref("ref1", "ref.fna")
        annotation = _ref("gff1", "annotation.gff3")
        inputs = pipeline_service.AnnotationInputs(
            ok=True, reference=reference, annotation=annotation
        )
        with installed_csq(True), installed_snpeff(True):
            card = build_annotate_card(_vcf(), inputs)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["body"]["annotator"] == "bcftools_csq"
        assert card.title == "Annotate variants"

    def test_only_snpeff_available_defaults_to_snpeff(self):
        """When only SnpEff is available, the card defaults to snpeff
        without a tool picker."""
        reference = _ref("ref1", "ref.fna")
        annotation = _ref("gff1", "annotation.gff3")
        inputs = pipeline_service.AnnotationInputs(
            ok=True, reference=reference, annotation=annotation
        )
        with installed_csq(False, error="bcftools csq unavailable"), installed_snpeff(True):
            card = build_annotate_card(_vcf(), inputs)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["body"]["annotator"] == "snpeff"

    def test_only_csq_available_defaults_to_bcftools_csq(self):
        """When only bcftools csq is available, the card defaults to
        bcftools_csq without a tool picker."""
        reference = _ref("ref1", "ref.fna")
        annotation = _ref("gff1", "annotation.gff3")
        inputs = pipeline_service.AnnotationInputs(
            ok=True, reference=reference, annotation=annotation
        )
        with installed_csq(True), installed_snpeff(False, error="SnpEff image not found"):
            card = build_annotate_card(_vcf(), inputs)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["body"]["annotator"] == "bcftools_csq"

    def test_neither_tool_available_shows_unavailable(self):
        """When neither annotator is available, the card is UNAVAILABLE
        with the SnpEff error message (richer tool checked first)."""
        reference = _ref("ref1", "ref.fna")
        annotation = _ref("gff1", "annotation.gff3")
        inputs = pipeline_service.AnnotationInputs(
            ok=True, reference=reference, annotation=annotation
        )
        with installed_csq(False, error="bcftools csq missing"), installed_snpeff(
            False, error="SnpEff image not pulled"
        ):
            card = build_annotate_card(_vcf(), inputs)
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "SnpEff" in card.reason


@contextmanager
def installed_snpeff(available=True, error=None):
    """Pin the SnpEff probe the annotate card reads.

    Like `installed_csq` but for the on-demand image probe. The real probe
    returns a Tool with path=docker, version=None; this fake returns a
    matching shape so the card can read `.error` on the unavailable path.
    """
    with patch(
        "app.services.suggestion_service.tools.snpeff",
        return_value=_FakeTool(available, name="snpeff"),
    ) as probe:
        probe.return_value.error = error
        yield


class TestClassifyReadsCard:
    """Per CLAUDE.md, assert the *unavailable* direction hardest: the image
    ships kraken2, so an availability assertion passes whether or not the
    seam it depends on actually works. The unavailable-direction test is the
    one that fails when the probe patch stops reaching the call site."""

    def test_offered_for_fastq(self, monkeypatch):
        # The probe is patched *on* rather than left to the host. The backend
        # image ships kraken2, so trusting the real probe made this assertion
        # depend on where it ran: green in the container, red on a CI runner
        # that has no kraken2 on PATH. Patching states the precondition the
        # assertion actually needs.
        from app.pipelines.tools import Tool
        from app.services import suggestion_service

        monkeypatch.setattr(
            suggestion_service.tools,
            "kraken2",
            lambda: Tool(name="kraken2", path="/usr/bin/kraken2", version="2.1.3", error=None),
        )
        obj = _fake_obj(kind=FormatKind.FASTQ, obj_id="fq1")
        card = build_classify_reads_card(obj)
        assert card is not None
        assert card.kind == "classify_reads"
        assert card.category == "CLASSIFY_READS"
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/classify-reads"
        assert card.launch["body"] == {"object_id": "fq1"}

    def test_absent_for_fasta(self):
        obj = _fake_obj(kind=FormatKind.FASTA)
        assert build_classify_reads_card(obj) is None

    def test_flips_unavailable_when_probe_off(self, monkeypatch):
        from app.pipelines.tools import Tool
        from app.services import suggestion_service

        monkeypatch.setattr(
            suggestion_service.tools,
            "kraken2",
            lambda: Tool(name="kraken2", path=None, version=None, error="not found"),
        )
        obj = _fake_obj(kind=FormatKind.FASTQ)
        card = build_classify_reads_card(obj)
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert card.reason == "not found"
        assert card.launch is None


@contextmanager
def _no_db():
    """Silence the two database questions `suggestions_for` asks at the end.

    `attach_prior_runs` reads PipelineRun history and `attach_running` reads
    the live queue. Neither decides which cards appear, which is what the
    tests using this are about -- but both hit Beanie, so an unpatched one
    raises CollectionWasNotInitialized in a test that never wanted a
    database. Patched at the same seam each module keeps for the purpose.
    """
    with (
        patch("app.services.prior_runs._runs_touching", return_value=[]),
        patch("app.services.running_now._active_jobs_for", return_value=[]),
    ):
        yield


@contextmanager
def stub_db(references=(), chemistry=None, annotation_inputs=None):
    """Cut the four database seams `suggestions_for` reaches through.

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
        patch("app.services.prior_runs._runs_touching", return_value=[]),
        # Same reason as the line above: the DB seam, patched so these stay
        # pure unit tests. `running_now` asks the live queue whether this
        # file has work in flight; here it never does.
        patch("app.services.running_now._active_jobs_for", return_value=[]),
    ):
        yield


def _as_reference(ref, *, kind=FormatKind.FASTA, role=ObjectRole.REFERENCE):
    """Give a `_ref` the fields `suggestions_for`'s listing filter reads.

    `_ref` deliberately carries only what `resolve_reference` reads; the
    filter above it reads `format.kind` and `role`. Both are overridable so a
    test can hand the filter something it must reject.

    `status` defaults to READY: `stub_db`'s `list_objects` patch is shared by
    every listing `suggestions_for` makes, including
    `transcriptomes_for_project` (`pipeline_service.py`), which -- unlike the
    align card's own reference filter -- does not push `status=READY` into
    the query and instead filters client-side, so it reads `.status` on
    whatever `list_objects` returns.
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        id=ref.id,
        name=ref.name,
        format=SimpleNamespace(kind=kind),
        role=role,
        status=ObjectStatus.READY,
    )


CARD_KEYS = {
    "kind", "category", "title", "description",
    "why", "status", "reason", "launch", "requires_install", "prior_runs",
    "running", "configure",
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
        assert [c["kind"] for c in cards] == [
            "preprocess", "align", "classify_reads",
            "assemble", "salmon_quantify", "multiqc",
        ]

    async def test_a_launchable_card_with_a_dialog_carries_configure(self):
        """The preprocess card opens TrimDialog, so it offers Adjust."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)), stub_db(
                       references=[_ref("aaa", "ref.fna")]):
            cards = await suggestions_for(_fake_obj())
        preprocess = next(c for c in cards if c["kind"] == "preprocess")
        assert preprocess["configure"] == {"dialog": "trim"}

    async def test_an_unavailable_card_carries_no_configure(self):
        """No body to seed a dialog with, so no button that opens one.

        The assemble card is always unavailable for a short-read FASTQ, and
        an unavailable card has `launch` None -- adjusting a run that cannot
        start is a dead end dressed as a control.
        """
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)), stub_db(references=[]):
            cards = await suggestions_for(_fake_obj())
        assemble = next(c for c in cards if c["kind"] == "assemble")
        assert assemble["status"] == "unavailable"
        assert assemble["launch"] is None
        assert assemble["configure"] is None

    async def test_the_annotate_kind_now_has_a_configure_dialog(self):
        """The annotate card now has a tool picker, so it carries a
        configure dialog."""
        inputs = pipeline_service.AnnotationInputs(
            ok=True,
            reference=_ref("aaa", "ref.fna"),
            annotation=_ref("bbb", "annotation.gff3"),
        )
        with installed_csq(True), stub_db(annotation_inputs=inputs):
            cards = await suggestions_for(_vcf())
        annotate = next(c for c in cards if c["kind"] == "annotate")
        assert annotate["launch"] is not None
        assert annotate["configure"] is not None
        assert annotate["configure"]["dialog"] == "annotation"

    async def test_a_fastq_never_gets_a_variants_card(self):
        """Variants are called on an alignment, not on reads."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)), stub_db():
            cards = await suggestions_for(_fake_obj())
        assert "variants" not in [c["kind"] for c in cards]

    async def test_a_bam_gets_the_variants_and_quantify_cards(self):
        with stub_db(chemistry=align_runner.ReadChemistry.SHORT):
            cards = await suggestions_for(_bam())
        # Also gets a consensus card (unavailable: _bam()'s derived_from is
        # empty, so resolve_alignment_target_for_bam finds no target) --
        # asserting the two named cards are present rather than an exact
        # list, since a fixture BAM with no provenance is exactly the case
        # the consensus card exists to report as unavailable, not omit.
        assert "variants" in [c["kind"] for c in cards]
        assert "quantify" in [c["kind"] for c in cards]

    async def test_a_vcf_gets_only_the_annotate_card(self):
        inputs = pipeline_service.AnnotationInputs(
            ok=True,
            reference=_ref("aaa", "ref.fna"),
            annotation=_ref("bbb", "annotation.gff3"),
        )
        with installed_csq(True), stub_db(annotation_inputs=inputs):
            cards = await suggestions_for(_vcf())
        assert [c["kind"] for c in cards] == ["annotate", "phase"]

    async def test_the_order_does_not_move_with_availability(self):
        """Fixed order, not sorted by availability: a card that changes
        position between files makes the grid something to re-read rather
        than scan."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)), stub_db(references=[]):
            # No reference, so align is unavailable and assemble always is.
            cards = await suggestions_for(_fake_obj())
        assert [c["kind"] for c in cards] == [
            "preprocess", "align", "classify_reads",
            "assemble", "salmon_quantify", "multiqc",
        ]
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
        ), _no_db():
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

    async def test_salmon_card_is_fed_transcriptomes_for_project_not_protein(self):
        """`suggestions_for`'s own listing for the salmon card, mirroring
        `test_protein_and_transcript_fasta_are_not_counted_as_references`
        above. `transcriptomes_for_project` filters by
        `role is ObjectRole.TRANSCRIPT`, so `protein.faa` sitting in the same
        project must not reach the card as a usable choice.
        """
        from types import SimpleNamespace

        listed = [
            SimpleNamespace(
                id="ccc",
                name="cds_from_genomic.fna",
                status=ObjectStatus.READY,
                format=SimpleNamespace(kind=FormatKind.FASTA),
                role=ObjectRole.TRANSCRIPT,
                blob_sha256="digest-cds",
            ),
            SimpleNamespace(
                id="bbb",
                name="protein.faa",
                status=ObjectStatus.READY,
                format=SimpleNamespace(kind=FormatKind.FASTA),
                role=ObjectRole.PROTEIN,
                blob_sha256="digest-protein",
            ),
        ]
        with (
            patch("app.services.object_service.list_objects", return_value=listed),
            patch(
                "app.services.pipeline_service.read_chemistry_for_alignment",
                return_value=None,
            ),
            patch(
                "app.services.suggestion_service.tools.fastp",
                return_value=_FakeTool(True),
            ),
            installed_salmon(True),
            _no_db(),
        ):
            cards = await suggestions_for(
                _fake_obj(facts={"qc_read_chemistry": "short"})
            )

        salmon_card = next(c for c in cards if c["kind"] == "salmon_quantify")
        assert salmon_card["status"] == "available"
        assert salmon_card["launch"]["body"]["reads_id"] == "abc123"

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

        # Align is gone; the other three survive in their usual order.
        assert [c["kind"] for c in cards] == [
            "preprocess", "classify_reads", "assemble", "salmon_quantify",
            "multiqc",
        ]


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

    async def test_a_bam_costs_exactly_one_project_listing(self):
        """A BAM click costs exactly one project listing, not two.

        This test used to assert *zero*: the align card was the only consumer
        of a listing and it is FASTQ-only. The quantify card changed that --
        it needs the project's annotations, and there is no way to offer it
        without looking. So the guard becomes "once", which is the property
        that actually matters: the two consumers must not each run their own
        query, and the align card's reference filter must still not run here.
        """
        with patch(
            "app.services.object_service.list_objects", return_value=[]
        ) as listing:
            with patch(
                "app.services.pipeline_service.read_chemistry_for_alignment",
                return_value=align_runner.ReadChemistry.SHORT,
            ):
                with _no_db():
                    await suggestions_for(_bam())
        assert listing.call_count == 1

    async def test_annotations_are_not_fetched_for_a_fastq(self):
        """The mirror of the guard above. The quantify card is BAM-only, so
        listing a project's annotations on a FASTQ click is a query per click
        for a card that will not be built."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)):
            with patch(
                "app.services.pipeline_service.annotations_for_project"
            ) as annotations:
                with patch("app.services.object_service.list_objects",
                           return_value=[]):
                    with _no_db():
                        await suggestions_for(_fake_obj())
        annotations.assert_not_called()

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
                    with _no_db():
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
                # Patched because the quantify card lists a BAM's project for
                # annotations; without it this reaches a database the test has
                # not set up, and fails for a reason unrelated to what it is
                # asserting.
                with patch("app.services.object_service.list_objects",
                           return_value=[]):
                    with _no_db():
                        await suggestions_for(_bam())
        resolve.assert_not_called()

    async def test_the_ready_filter_is_pushed_into_the_listing_query(self):
        """Filtering after the fact would let a project's non-ready objects
        eat the result limit and drop references silently.

        Checks every call rather than just the last: a FASTQ click now also
        triggers `transcriptomes_for_project`'s own listing (for the salmon
        card), which -- by that function's own design -- filters `status`
        client-side rather than pushing it into the query, so asserting on
        `call_args` alone would make this test depend on which of the two
        calls happens to run last rather than on what it is actually about,
        the align card's reference query.
        """
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)):
            with patch("app.services.object_service.list_objects",
                       return_value=[]) as listing:
                with _no_db():
                    await suggestions_for(_fake_obj())
        assert any(
            call.kwargs.get("status") is ObjectStatus.READY
            for call in listing.call_args_list
        )

    async def test_every_card_carries_a_prior_runs_list(self):
        """Absent would make the frontend guard a field that is always sent;
        empty is the honest answer for a file nothing has been run on."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)), stub_db():
            cards = await suggestions_for(_fake_obj())
        assert cards
        assert all(c["prior_runs"] == [] for c in cards)


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
        from tests.api.bare_app import override_owner

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router)
        override_owner(app)
        return TestClient(app)

    def test_unknown_object_id_is_a_404_not_an_empty_grid(self, client):
        """An empty `suggestions` list would render as "nothing to do here",
        which is a different and wrong answer to "that file does not exist"."""
        from app.errors import NotFoundError

        # The lookup is now owner-scoped, and it signals both "no such id" and
        # "not yours" by raising rather than returning None -- so the stub
        # raises, matching what `object_service.get_object` really does.
        with patch(
            "app.api.v1.pipelines.object_service.get_object",
            side_effect=NotFoundError("Object not found"),
        ):
            resp = client.get(
                "/pipelines/suggestions/000000000000000000000001"
            )
        assert resp.status_code == 404

    def test_a_malformed_object_id_is_rejected_before_the_lookup(self, client):
        resp = client.get("/pipelines/suggestions/not-an-object-id")
        assert resp.status_code == 422

    def test_a_found_object_is_answered_with_its_cards(self, client):
        with patch("app.api.v1.pipelines.object_service.get_object",
                   return_value=_bam()), stub_db(
                       chemistry=align_runner.ReadChemistry.SHORT), \
                installed_callers():
            resp = client.get(
                "/pipelines/suggestions/000000000000000000000001"
            )
        assert resp.status_code == 200
        # Not an exact list -- see test_a_bam_gets_the_variants_and_quantify_cards
        # for why _bam() also yields an (unavailable) consensus card now.
        kinds = [c["kind"] for c in resp.json()["suggestions"]]
        assert "variants" in kinds
        assert "quantify" in kinds


@contextmanager
def installed_featurecounts(available=True):
    """Pin the featureCounts probe the quantify card reads.

    A plain module-attribute lookup, like the variant callers' -- not a
    frozen-spec seam like the aligners', so patching the name
    `suggestion_service` imported does reach the call.
    """
    with patch(
        "app.services.suggestion_service.tools.featurecounts",
        return_value=_FakeTool(available, name="featurecounts"),
    ) as probe:
        yield probe


def _annotation(obj_id="gtf1", kind=FormatKind.GTF):
    """A stand-in annotation.

    Carries no name: the card reads only whether the list is non-empty, and
    leaving the field off keeps that honest -- if the card ever starts naming
    its annotation, these tests should fail rather than quietly pass on a
    value invented here.
    """
    return _fake_obj(kind=kind, obj_id=obj_id)


class TestQuantifyCard:
    def test_the_probe_patch_actually_takes_effect(self):
        """Guards every test below it, for the reason CLAUDE.md spells out:
        the image ships featureCounts *installed*, so an available-card
        assertion passes whether or not the patch worked. Only the
        unavailable direction can tell a working seam from an escaped one.
        """
        with installed_featurecounts(False):
            card = build_quantify_card(_bam(), [_annotation()])
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason

    def test_a_bam_with_an_annotation_is_runnable(self):
        with installed_featurecounts(True):
            card = build_quantify_card(_bam(), [_annotation()])
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/quantify"

    def test_the_launch_body_keys_on_bam_id(self):
        """The same trap the variants card carries: this endpoint takes
        `bam_id`, not `object_id`, and sending the wrong one 422s."""
        with installed_featurecounts(True):
            card = build_quantify_card(_bam(obj_id="xyz"), [_annotation()])
        assert card.launch["body"]["bam_id"] == "xyz"
        assert "object_id" not in card.launch["body"]

    def test_the_annotation_is_left_for_the_server_to_resolve(self):
        """Omitted rather than picked here, so the server can prefer the GTF
        over the GFF3 of the same assembly -- featureCounts' conventional
        grouping attribute is absent from NCBI's GFF3 entirely."""
        with installed_featurecounts(True):
            card = build_quantify_card(_bam(), [_annotation()])
        assert "annotation_id" not in card.launch["body"]

    def test_no_annotation_gates_the_card_with_an_actionable_reason(self):
        with installed_featurecounts(True):
            card = build_quantify_card(_bam(), [])
        assert card.status is CardStatus.UNAVAILABLE
        assert "no gene annotation" in card.reason

    def test_a_missing_tool_is_reported_before_a_missing_annotation(self):
        """Both are true when neither is present. The tool is the one the user
        cannot fix by downloading a genome, so it is the one worth saying."""
        with installed_featurecounts(False):
            card = build_quantify_card(_bam(), [])
        assert "not installed" in card.reason

    def test_a_fastq_gets_no_card_at_all(self):
        with installed_featurecounts(True):
            assert build_quantify_card(_fake_obj(), [_annotation()]) is None

    def test_a_vcf_gets_no_card_at_all(self):
        with installed_featurecounts(True):
            assert build_quantify_card(_vcf(), [_annotation()]) is None


@contextmanager
def installed_bedtools(available=True):
    """Pin the bedtools probe the feature coverage card reads.

    `tools.bedtools` is a plain `@lru_cache`d function read fresh at call
    time by `build_feature_coverage_card`, not captured at import into a
    frozen dataclass the way `aligner_registry`'s specs capture their probes
    -- so a straightforward monkeypatch on the name `suggestion_service`
    imported reaches the call, the same seam `installed_featurecounts` above
    relies on for the quantify card.
    """
    with patch(
        "app.services.suggestion_service.tools.bedtools",
        return_value=_FakeTool(available, name="bedtools"),
    ) as probe:
        yield probe


@contextmanager
def installed_salmon(available=True):
    """Pin the salmon probe the card reads.

    Same seam shape as `installed_featurecounts` above: a plain module-
    attribute lookup, patched so the card calls through it at call time
    rather than a name bound at import.
    """
    with patch(
        "app.services.suggestion_service.tools.salmon",
        return_value=_FakeTool(available, name="salmon"),
    ) as probe:
        yield probe


class TestFeatureCoverageCard:
    def test_the_probe_patch_actually_takes_effect(self):
        """Guards every test below it, for the reason CLAUDE.md spells out:
        the image ships bedtools *installed*, so an available-card assertion
        passes whether or not the patch worked. Only the unavailable
        direction can tell a working seam from an escaped one.
        """
        with installed_bedtools(False):
            card = build_feature_coverage_card(_bam(), [_annotation()])
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason

    def test_a_bam_with_an_annotation_is_runnable(self):
        with installed_bedtools(True):
            card = build_feature_coverage_card(_bam(obj_id="xyz"), [_annotation()])
        assert card.status is CardStatus.AVAILABLE
        assert card.kind == "feature_coverage"
        assert card.category == "ASSEMBLY_QC"
        assert card.launch == {
            "endpoint": "/pipelines/feature-coverage",
            "body": {"bam_id": "xyz"},
        }

    def test_no_annotation_gates_the_card_with_an_actionable_reason(self):
        with installed_bedtools(True):
            card = build_feature_coverage_card(_bam(), [])
        assert card.status is CardStatus.UNAVAILABLE
        assert "no annotation" in card.reason

    def test_a_missing_tool_is_reported_before_a_missing_annotation(self):
        """Both are true when neither is present. The tool is the one the
        user cannot fix by downloading a genome, so it is the one worth
        saying."""
        with installed_bedtools(False):
            card = build_feature_coverage_card(_bam(), [])
        assert "not installed" in card.reason

    def test_a_fastq_gets_no_card_at_all(self):
        with installed_bedtools(True):
            assert build_feature_coverage_card(_fake_obj(), [_annotation()]) is None

    def test_a_vcf_gets_no_card_at_all(self):
        with installed_bedtools(True):
            assert build_feature_coverage_card(_vcf(), [_annotation()]) is None


@contextmanager
def installed_mosdepth(available=True):
    """Pin the mosdepth probe the coverage card reads.

    Same seam as `installed_bedtools` above: a plain `@lru_cache`d function
    read fresh at call time by `build_coverage_card`.
    """
    with patch(
        "app.services.suggestion_service.tools.mosdepth",
        return_value=_FakeTool(available, name="mosdepth"),
    ) as probe:
        yield probe


class TestCoverageCard:
    def test_the_probe_patch_actually_takes_effect(self):
        """Guards every test below it, for the reason CLAUDE.md spells out:
        the image ships mosdepth *installed*, so an available-card assertion
        passes whether or not the patch worked. Only the unavailable
        direction can tell a working seam from an escaped one.
        """
        with installed_mosdepth(False):
            card = build_coverage_card(_bam())
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason

    def test_any_bam_is_runnable(self):
        """No annotation needed, unlike feature coverage -- this is the
        difference that makes the card available for any completed
        alignment."""
        with installed_mosdepth(True):
            card = build_coverage_card(_bam(obj_id="xyz"))
        assert card.status is CardStatus.AVAILABLE
        assert card.kind == "coverage"
        assert card.launch == {
            "endpoint": "/pipelines/coverage",
            "body": {"bam_id": "xyz"},
        }

    def test_is_available_with_no_annotation_in_the_project(self):
        """The distinguishing case against feature_coverage, asserted rather
        than left implicit: an annotation-less project still gets this card.
        """
        with installed_mosdepth(True):
            card = build_coverage_card(_bam())
        assert card.status is CardStatus.AVAILABLE

    def test_a_fastq_gets_no_card_at_all(self):
        with installed_mosdepth(True):
            assert build_coverage_card(_fake_obj()) is None

    def test_a_vcf_gets_no_card_at_all(self):
        with installed_mosdepth(True):
            assert build_coverage_card(_vcf()) is None

    def test_is_distinct_from_the_feature_coverage_card(self):
        """Success criterion 3 of #626: the two coverage cards must be
        separable by kind and by endpoint, not two spellings of one thing."""
        with installed_mosdepth(True), installed_bedtools(True):
            coverage = build_coverage_card(_bam(obj_id="xyz"))
            feature = build_feature_coverage_card(_bam(obj_id="xyz"), [_annotation()])
        assert coverage.kind != feature.kind
        assert coverage.launch["endpoint"] != feature.launch["endpoint"]
        assert coverage.title != feature.title

    def test_is_registered_in_card_builders(self):
        """A builder absent from CARD_BUILDERS is never called, so the card
        exists in tests and nowhere in the app."""
        assert "coverage" in [kind for kind, _ in CARD_BUILDERS]


@contextmanager
def installed_modkit(available=True):
    """Pin the modkit probe the methylation card reads.

    Same seam as `installed_mosdepth` above: a plain `@lru_cache`d function
    read fresh at call time by `build_methylation_card`.
    """
    with patch(
        "app.services.suggestion_service.tools.modkit",
        return_value=_FakeTool(available, name="modkit"),
    ) as probe:
        yield probe


def _write_bam_with_tags(path, *, mm_tag_positions=(), n_reads=5):
    """Build a small synthetic BAM for build_methylation_card's K1 prefix
    scan. Mirrors tests/storage/test_parsers.py's fixture-building pattern.
    """
    pysam = pytest.importorskip("pysam")
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": "chr1", "LN": 100000}],
    }
    with pysam.AlignmentFile(str(path), "wb", header=header) as out:
        for i in range(n_reads):
            a = pysam.AlignedSegment()
            a.query_name = f"read{i}"
            a.query_sequence = "ACGT" * 25
            a.flag = 0
            a.reference_id = 0
            a.reference_start = i * 10
            a.mapping_quality = 60
            a.cigar = [(0, 100)]
            a.query_qualities = pysam.qualitystring_to_array("I" * 100)
            if i in mm_tag_positions:
                import array

                a.set_tag("MM", "C+m,0;", value_type="Z")
                a.set_tag("ML", array.array("B", [255]))
            out.write(a)


@contextmanager
def _bam_blob(tmp_path, *, mm_tag_positions=()):
    """Write a real BAM under `tmp_path` and point `blob_path` at it, so
    `build_methylation_card`'s K1 scan reads a genuine file rather than
    needing the whole blob-storage stack in a unit test.
    """
    bam_path = tmp_path / "methylation_card_fixture.bam"
    _write_bam_with_tags(bam_path, mm_tag_positions=mm_tag_positions)
    with patch(
        "app.services.suggestion_service.blob_path", return_value=bam_path
    ):
        yield "fake-digest"


class TestMethylationCard:
    def test_the_probe_patch_actually_takes_effect(self, tmp_path):
        """Guards every test below it, for the reason CLAUDE.md spells out:
        the image ships modkit *installed*, so an available-card assertion
        passes whether or not the patch worked. Only the unavailable
        direction can tell a working seam from an escaped one."""
        with installed_modkit(False):
            card = build_methylation_card(_bam(blob_sha256="digest"))
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason

    def test_a_bam_with_no_mm_tags_is_unavailable_and_explains_why(self, tmp_path):
        """Criterion 3 of #631: the message must say *why* modification
        calling cannot happen now, not just that it is unavailable. Asserted
        on the actual explanation text -- a bare "unavailable" with no
        explanation would pass a status-only test but fail the issue's
        success criterion."""
        with installed_modkit(True), _bam_blob(tmp_path, mm_tag_positions=()) as digest:
            card = build_methylation_card(_bam(blob_sha256=digest))
        assert card.status is CardStatus.UNAVAILABLE
        assert "basecalling" in card.reason
        assert "Dorado" in card.reason
        assert "cannot be added afterwards" in card.reason

    def test_a_non_bam_gets_no_card_at_all(self):
        with installed_modkit(True):
            assert build_methylation_card(_fake_obj(kind=FormatKind.FASTQ)) is None

    def test_a_vcf_gets_no_card_at_all(self):
        with installed_modkit(True):
            assert build_methylation_card(_vcf()) is None

    def test_a_bam_with_mm_tags_is_available(self, tmp_path):
        with installed_modkit(True), _bam_blob(tmp_path, mm_tag_positions={0}) as digest:
            card = build_methylation_card(_bam(obj_id="xyz", blob_sha256=digest))
        assert card.status is CardStatus.AVAILABLE
        assert card.kind == "methylation"
        assert card.launch == {
            "endpoint": "/pipelines/methylation",
            "body": {"bam_id": "xyz"},
        }

    def test_is_registered_in_card_builders(self):
        """A builder absent from CARD_BUILDERS is never called, so the card
        exists in tests and nowhere in the app."""
        assert "methylation" in [kind for kind, _ in CARD_BUILDERS]


def _reference_object(obj_id="ref1", name="reference.fna", facts=None):
    """A READY FASTA reference carrying `.name` and `.facts` -- the two
    fields `build_gc_bias_card` reads off its `alignment_target` parameter.

    `_fake_obj` already carries `.facts` for every fixture in this file but
    never `.name`, since no existing card reads a reference's name off a
    bare `_fake_obj`. `build_consensus_card` gets its `reference.name` from
    a real `DataObject` in production; here `.name` is set the same way
    `_assembly_object`/`_bam_object` add fields `_fake_obj` doesn't carry.
    """
    obj = _fake_obj(kind=FormatKind.FASTA, obj_id=obj_id, facts=facts)
    obj.name = name
    return obj


class TestGcBiasCard:
    def test_unavailable_when_no_alignment_target(self):
        obj = _bam({})
        card = build_gc_bias_card(obj, None)
        assert card.status is CardStatus.UNAVAILABLE
        assert "alignment target" in card.reason.lower()

    def test_unavailable_when_reference_has_no_gc_tracks(self):
        reference = _reference_object(facts={})
        obj = _bam({"coverage_status": "ok", "coverage_mode": "windows"})
        card = build_gc_bias_card(obj, reference)
        assert card.status is CardStatus.UNAVAILABLE
        assert "gc tracks" in card.reason.lower()

    def test_unavailable_when_no_coverage_at_all(self):
        """Minor #3 finding: this refusal must read as 'no coverage
        computed', distinct from the region-mode case below -- a user who
        already ran coverage should never see this wording."""
        reference = _reference_object(facts={"gc_tracks": {"contigs": [{}]}})
        obj = _bam({})
        card = build_gc_bias_card(obj, reference)
        assert card.status is CardStatus.UNAVAILABLE
        assert "no coverage computed" in card.reason.lower()
        assert "target region" not in card.reason.lower()

    def test_unavailable_when_coverage_mode_is_regions_not_windows(self):
        """The fourth precondition, distinct from "no coverage at all":
        Task 3's launcher refuses a region-mode coverage run separately from
        a missing one, because the two need different next steps -- rerun
        with no target BED, versus run coverage at all. Minor #3 finding:
        this message must say coverage WAS computed, not that it's missing,
        or a user who ran it correctly reads it as their job being lost."""
        reference = _reference_object(facts={"gc_tracks": {"contigs": [{}]}})
        obj = _bam({"coverage_status": "ok", "coverage_mode": "regions"})
        card = build_gc_bias_card(obj, reference)
        assert card.status is CardStatus.UNAVAILABLE
        assert "target region" in card.reason.lower()
        assert "no coverage computed" not in card.reason.lower()

    def test_available_when_all_preconditions_met(self):
        reference = _reference_object(facts={"gc_tracks": {"contigs": [{}]}})
        obj = _bam(
            {"coverage_status": "ok", "coverage_mode": "windows"}, obj_id="bam1"
        )
        card = build_gc_bias_card(obj, reference)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch == {
            "endpoint": "/pipelines/gc-bias",
            "body": {"bam_id": "bam1"},
        }

    def test_a_fastq_gets_no_card_at_all(self):
        assert build_gc_bias_card(_fake_obj(), None) is None

    def test_a_vcf_gets_no_card_at_all(self):
        assert build_gc_bias_card(_vcf(), None) is None

    def test_is_registered_in_card_builders(self):
        assert "gc_bias" in [kind for kind, _ in CARD_BUILDERS]


def _transcriptome_object(obj_id="cds1", blob_sha256="digest-cds1"):
    """A stand-in transcriptome-role FASTA.

    Carries `blob_sha256` because that is exactly what the card reads to
    decide whether two candidates are the same reference registered twice --
    unlike `_annotation`, which the quantify card never dedupes.
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        id=obj_id,
        format=SimpleNamespace(kind=FormatKind.FASTA),
        role=ObjectRole.TRANSCRIPT,
        blob_sha256=blob_sha256,
    )


def _protein_object(obj_id="protein1", blob_sha256="digest-protein1"):
    """protein.faa's shape: FASTA with no byte-level way to tell it apart
    from a transcriptome, distinguished only by role."""
    from types import SimpleNamespace
    return SimpleNamespace(
        id=obj_id,
        format=SimpleNamespace(kind=FormatKind.FASTA),
        role=ObjectRole.PROTEIN,
        blob_sha256=blob_sha256,
    )


class TestSalmonQuantifyCard:
    """The two mistakes this repo has already made with reference-picking
    rules, pinned so a third does not happen quietly: `protein.faa` counted
    as a usable reference, and one reference stored twice counted as two.
    """

    def test_the_probe_patch_actually_takes_effect(self):
        """Guards every test below it, for the reason CLAUDE.md spells out:
        the image ships salmon *installed*, so an available-card assertion
        passes whether or not the patch worked. Only the unavailable
        direction can tell a working seam from an escaped one.
        """
        with installed_salmon(False):
            card = build_salmon_quantify_card(_fake_obj(), [_transcriptome_object()])
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason

    def test_offers_salmon_when_a_transcriptome_is_available(self):
        with installed_salmon(True):
            card = build_salmon_quantify_card(_fake_obj(), [_transcriptome_object()])
        assert card is not None
        assert card.kind == "salmon_quantify"
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/salmon-quantify"

    def test_the_launch_body_keys_on_reads_id(self):
        with installed_salmon(True):
            card = build_salmon_quantify_card(
                _fake_obj(obj_id="xyz"), [_transcriptome_object()]
            )
        assert card.launch["body"]["reads_id"] == "xyz"

    def test_the_cds_caveat_is_stated_in_the_card_copy(self):
        """Required by the plan's 'Known limitation' section: a CDS
        reference covers coding transcripts only, and that has to be said
        somewhere the user actually reads before launching."""
        with installed_salmon(True):
            card = build_salmon_quantify_card(_fake_obj(), [_transcriptome_object()])
        assert (
            "UTRs and non-coding RNA are not quantified" in card.why
        )

    def test_an_empty_transcriptome_list_is_unavailable_even_with_a_protein_present(
        self,
    ):
        # REQ-CARD-1, unit-level half. This card receives `transcriptomes`
        # as a parameter -- role filtering is `transcriptomes_for_project`'s
        # job (`pipeline_service.py`), not something re-derived here, so the
        # test that actually proves protein.faa never reaches this card is
        # `TestSuggestionsFor.
        # test_salmon_card_is_fed_transcriptomes_for_project_not_protein`,
        # which patches the real `object_service.list_objects` seam and
        # checks a protein-role object sitting in the same project never
        # makes it into the card's candidate list. What this unit test pins
        # is the other half: passed an empty list (the correct outcome once
        # protein.faa is filtered out and nothing else remains), the card
        # must gate rather than treat "empty" as "unknown, so allow it".
        with installed_salmon(True):
            card = build_salmon_quantify_card(_fake_obj(), [])
        assert card.status is CardStatus.UNAVAILABLE

    def test_the_same_transcriptome_twice_counts_once(self):
        # REQ-CARD-2. Two records of one reference must not read as an
        # ambiguous choice between two references.
        tx = _transcriptome_object()
        with installed_salmon(True):
            card = build_salmon_quantify_card(_fake_obj(), [tx, tx])
        assert card is not None
        assert card.status is CardStatus.AVAILABLE
        assert "Multiple distinct transcriptomes" not in (card.why or "")

    def test_two_distinct_transcriptomes_are_flagged_as_ambiguous_in_copy(self):
        tx_a = _transcriptome_object(obj_id="cds1", blob_sha256="digest-a")
        tx_b = _transcriptome_object(obj_id="cds2", blob_sha256="digest-b")
        with installed_salmon(True):
            card = build_salmon_quantify_card(_fake_obj(), [tx_a, tx_b])
        assert card is not None
        assert card.status is CardStatus.AVAILABLE
        assert "Multiple distinct transcriptomes" in card.why

    def test_no_transcriptome_gates_the_card_with_an_actionable_reason(self):
        with installed_salmon(True):
            card = build_salmon_quantify_card(_fake_obj(), [])
        assert card.status is CardStatus.UNAVAILABLE
        assert "no transcriptome" in card.reason

    def test_a_missing_tool_is_reported_before_a_missing_transcriptome(self):
        with installed_salmon(False):
            card = build_salmon_quantify_card(_fake_obj(), [])
        assert "not installed" in card.reason

    def test_a_bam_gets_no_card_at_all(self):
        with installed_salmon(True):
            assert build_salmon_quantify_card(_bam(), [_transcriptome_object()]) is None


class TestScaffoldCardOrchestration:
    """`suggestions_for`'s own listing for the scaffold card, as opposed to
    `build_scaffold_card`'s unit tests in test_scaffold_card.py -- those hand
    the builder a pre-filtered list and cannot catch a bug in how that list
    gets built.

    This is where a real bug lived: a project with exactly one reference-role
    FASTA -- itself -- rendered an AVAILABLE scaffold card naming itself as
    the reference. Caught against a real worktree stack on 2026-08-05, not
    by any unit test, because build_scaffold_card correctly trusts whatever
    list it is given and every existing test constructed that list by hand
    with IDs that never collided with the draft's own id.
    """

    async def test_a_reference_role_fasta_is_not_offered_as_its_own_scaffold_target(self):
        from types import SimpleNamespace

        obj = _fake_obj(kind=FormatKind.FASTA, obj_id="draft1")
        obj.role = None
        with (
            patch("app.services.suggestion_service.tools.ragtag",
                  return_value=_FakeTool(True)),
            patch(
                "app.services.object_service.list_objects",
                return_value=[
                    SimpleNamespace(
                        id="draft1",
                        name="self.fasta",
                        role=ObjectRole.REFERENCE,
                        format=SimpleNamespace(kind=FormatKind.FASTA),
                        status=ObjectStatus.READY,
                    )
                ],
            ),
            patch("app.services.pipeline_service.read_chemistry_for_alignment",
                  return_value=None),
            patch("app.services.pipeline_service.resolve_annotation_inputs",
                  return_value=None),
            patch("app.services.prior_runs._runs_touching", return_value=[]),
            patch("app.services.running_now._active_jobs_for", return_value=[]),
        ):
            cards = await suggestions_for(obj)
        scaffold = next(c for c in cards if c["kind"] == "scaffold")
        assert scaffold["status"] == "unavailable"
        assert "none" in scaffold["reason"]

    async def test_a_second_real_reference_in_the_project_is_still_offered(self):
        """The fix must exclude only the anchor object, not every reference
        -- a real second reference in the project should still produce an
        available card."""
        from types import SimpleNamespace

        obj = _fake_obj(kind=FormatKind.FASTA, obj_id="draft1")
        obj.role = None
        with (
            patch("app.services.suggestion_service.tools.ragtag",
                  return_value=_FakeTool(True)),
            patch(
                "app.services.object_service.list_objects",
                return_value=[
                    SimpleNamespace(
                        id="draft1",
                        name="self.fasta",
                        role=ObjectRole.REFERENCE,
                        format=SimpleNamespace(kind=FormatKind.FASTA),
                        status=ObjectStatus.READY,
                    ),
                    SimpleNamespace(
                        id="ref2",
                        name="real_reference.fasta",
                        role=ObjectRole.REFERENCE,
                        format=SimpleNamespace(kind=FormatKind.FASTA),
                        status=ObjectStatus.READY,
                    ),
                ],
            ),
            patch("app.services.pipeline_service.read_chemistry_for_alignment",
                  return_value=None),
            patch("app.services.pipeline_service.resolve_annotation_inputs",
                  return_value=None),
            patch("app.services.prior_runs._runs_touching", return_value=[]),
            patch("app.services.running_now._active_jobs_for", return_value=[]),
        ):
            cards = await suggestions_for(obj)
        scaffold = next(c for c in cards if c["kind"] == "scaffold")
        assert scaffold["status"] == "available"
        assert scaffold["launch"]["body"]["reference_object_id"] == "ref2"


class TestMisassemblyCardOrchestration:
    """`suggestions_for`'s own listing for the misassembly card, mirroring
    `TestScaffoldCardOrchestration` above -- `build_misassembly_card` is
    handed the identical `scaffold_references` list `build_scaffold_card`
    is, which already carries the self-reference exclusion that class's own
    docstring explains the history of. These tests exist so a future
    refactor that gives the misassembly card its own candidate list cannot
    silently reintroduce that same bug for this card alone.
    """

    async def test_a_reference_role_fasta_is_not_offered_as_its_own_target(self):
        from types import SimpleNamespace

        obj = _fake_obj(kind=FormatKind.FASTA, obj_id="draft1")
        obj.role = None
        with (
            patch("app.services.suggestion_service.tools.quast",
                  return_value=_FakeTool(True)),
            patch(
                "app.services.object_service.list_objects",
                return_value=[
                    SimpleNamespace(
                        id="draft1",
                        name="self.fasta",
                        role=ObjectRole.REFERENCE,
                        format=SimpleNamespace(kind=FormatKind.FASTA),
                        status=ObjectStatus.READY,
                    )
                ],
            ),
            patch("app.services.pipeline_service.read_chemistry_for_alignment",
                  return_value=None),
            patch("app.services.pipeline_service.resolve_annotation_inputs",
                  return_value=None),
            patch("app.services.prior_runs._runs_touching", return_value=[]),
            patch("app.services.running_now._active_jobs_for", return_value=[]),
        ):
            cards = await suggestions_for(obj)
        misassembly = next(c for c in cards if c["kind"] == "misassembly")
        assert misassembly["status"] == "unavailable"
        assert "none" in misassembly["reason"]

    async def test_a_second_real_reference_in_the_project_is_still_offered(self):
        from types import SimpleNamespace

        obj = _fake_obj(kind=FormatKind.FASTA, obj_id="draft1")
        obj.role = None
        with (
            patch("app.services.suggestion_service.tools.quast",
                  return_value=_FakeTool(True)),
            patch(
                "app.services.object_service.list_objects",
                return_value=[
                    SimpleNamespace(
                        id="draft1",
                        name="self.fasta",
                        role=ObjectRole.REFERENCE,
                        format=SimpleNamespace(kind=FormatKind.FASTA),
                        status=ObjectStatus.READY,
                    ),
                    SimpleNamespace(
                        id="ref2",
                        name="real_reference.fasta",
                        role=ObjectRole.REFERENCE,
                        format=SimpleNamespace(kind=FormatKind.FASTA),
                        status=ObjectStatus.READY,
                    ),
                ],
            ),
            patch("app.services.pipeline_service.read_chemistry_for_alignment",
                  return_value=None),
            patch("app.services.pipeline_service.resolve_annotation_inputs",
                  return_value=None),
            patch("app.services.prior_runs._runs_touching", return_value=[]),
            patch("app.services.running_now._active_jobs_for", return_value=[]),
        ):
            cards = await suggestions_for(obj)
        misassembly = next(c for c in cards if c["kind"] == "misassembly")
        assert misassembly["status"] == "available"
        assert misassembly["launch"]["body"]["reference_object_id"] == "ref2"


class TestSyntenyCardOrchestration:
    """`suggestions_for`'s own listing for the synteny card, mirroring
    `TestMisassemblyCardOrchestration` above -- `build_synteny_card` is fed
    the identical `scaffold_references` list `build_scaffold_card` and
    `build_misassembly_card` are, which already carries the self-reference
    exclusion. These tests exist so a future refactor that gives the
    synteny card its own candidate list cannot silently reintroduce that
    same bug for this card alone.
    """

    async def test_a_reference_role_fasta_is_not_offered_as_its_own_target(self):
        from types import SimpleNamespace

        obj = _fake_obj(kind=FormatKind.FASTA, obj_id="draft1")
        obj.role = None
        with (
            patch("app.services.suggestion_service.tools.minimap2",
                  return_value=_FakeTool(True)),
            patch(
                "app.services.object_service.list_objects",
                return_value=[
                    SimpleNamespace(
                        id="draft1",
                        name="self.fasta",
                        role=ObjectRole.REFERENCE,
                        format=SimpleNamespace(kind=FormatKind.FASTA),
                        blob_sha256="digest-draft1",
                        status=ObjectStatus.READY,
                    )
                ],
            ),
            patch("app.services.pipeline_service.read_chemistry_for_alignment",
                  return_value=None),
            patch("app.services.pipeline_service.resolve_annotation_inputs",
                  return_value=None),
            patch("app.services.prior_runs._runs_touching", return_value=[]),
            patch("app.services.running_now._active_jobs_for", return_value=[]),
        ):
            cards = await suggestions_for(obj)
        synteny = next(c for c in cards if c["kind"] == "synteny")
        assert synteny["status"] == "unavailable"
        assert "reference genome" in synteny["reason"]

    async def test_a_second_real_reference_in_the_project_is_still_offered(self):
        from types import SimpleNamespace

        obj = _fake_obj(kind=FormatKind.FASTA, obj_id="draft1")
        obj.role = None
        with (
            patch("app.services.suggestion_service.tools.minimap2",
                  return_value=_FakeTool(True)),
            patch(
                "app.services.object_service.list_objects",
                return_value=[
                    SimpleNamespace(
                        id="draft1",
                        name="self.fasta",
                        role=ObjectRole.REFERENCE,
                        format=SimpleNamespace(kind=FormatKind.FASTA),
                        blob_sha256="digest-draft1",
                        status=ObjectStatus.READY,
                    ),
                    SimpleNamespace(
                        id="ref2",
                        name="real_reference.fasta",
                        role=ObjectRole.REFERENCE,
                        format=SimpleNamespace(kind=FormatKind.FASTA),
                        status=ObjectStatus.READY,
                        blob_sha256="digest-ref2",
                    ),
                ],
            ),
            patch("app.services.pipeline_service.read_chemistry_for_alignment",
                  return_value=None),
            patch("app.services.pipeline_service.resolve_annotation_inputs",
                  return_value=None),
            patch("app.services.prior_runs._runs_touching", return_value=[]),
            patch("app.services.running_now._active_jobs_for", return_value=[]),
        ):
            cards = await suggestions_for(obj)
        synteny = next(c for c in cards if c["kind"] == "synteny")
        assert synteny["status"] == "available"
        assert synteny["launch"]["body"]["reference_object_id"] == "ref2"


class TestTransferAnnotationCard:
    """The eukaryote annotation-transfer card (Liftoff): offered on a FASTA
    assembly once a GFF3/GTF annotation exists in the project, gated on
    organism kind (eukaryotic, not bacterial)."""

    async def test_available_when_annotation_present(self):
        from types import SimpleNamespace

        obj = _fake_obj(kind=FormatKind.FASTA, obj_id="asm1")
        obj.role = None
        with (
            patch("app.services.suggestion_service.tools.liftoff",
                  return_value=_FakeTool(True)),
            patch(
                "app.services.object_service.list_objects",
                return_value=[
                    SimpleNamespace(
                        id="ann1",
                        name="ref.gff3",
                        role=None,
                        format=SimpleNamespace(kind=FormatKind.GFF),
                        status=ObjectStatus.READY,
                    )
                ],
            ),
            patch("app.services.pipeline_service.read_chemistry_for_alignment",
                  return_value=None),
            patch("app.services.pipeline_service.resolve_annotation_inputs",
                  return_value=None),
            patch("app.services.prior_runs._runs_touching", return_value=[]),
            patch("app.services.running_now._active_jobs_for", return_value=[]),
        ):
            cards = await suggestions_for(obj)
        card = next(c for c in cards if c["kind"] == "transfer_annotation")
        assert card["status"] == "available"
        assert card["launch"]["endpoint"] == "/pipelines/transfer-annotation"
        assert card["launch"]["body"]["object_id"] == "asm1"

    async def test_unavailable_when_no_annotation(self):
        obj = _fake_obj(kind=FormatKind.FASTA, obj_id="asm1")
        obj.role = None
        with (
            patch("app.services.suggestion_service.tools.liftoff",
                  return_value=_FakeTool(True)),
            patch("app.services.object_service.list_objects",
                  return_value=[]),
            patch("app.services.pipeline_service.read_chemistry_for_alignment",
                  return_value=None),
            patch("app.services.pipeline_service.resolve_annotation_inputs",
                  return_value=None),
            patch("app.services.prior_runs._runs_touching", return_value=[]),
            patch("app.services.running_now._active_jobs_for", return_value=[]),
        ):
            cards = await suggestions_for(obj)
        card = next(c for c in cards if c["kind"] == "transfer_annotation")
        assert card["status"] == "unavailable"
        assert "No reference annotation" in card["reason"]

    async def test_not_offered_for_prokaryote(self):
        obj = _fake_obj(
            kind=FormatKind.FASTA,
            obj_id="asm1",
            metadata={"organism": "Escherichia coli"},
        )
        obj.role = None
        with (
            patch("app.services.suggestion_service.tools.liftoff",
                  return_value=_FakeTool(True)),
            patch("app.services.object_service.list_objects",
                  return_value=[]),
            patch("app.services.pipeline_service.read_chemistry_for_alignment",
                  return_value=None),
            patch("app.services.pipeline_service.resolve_annotation_inputs",
                  return_value=None),
            patch("app.services.prior_runs._runs_touching", return_value=[]),
            patch("app.services.running_now._active_jobs_for", return_value=[]),
        ):
            cards = await suggestions_for(obj)
        assert not any(c["kind"] == "transfer_annotation" for c in cards)

    async def test_unavailable_when_liftoff_missing(self):
        from types import SimpleNamespace

        obj = _fake_obj(kind=FormatKind.FASTA, obj_id="asm1")
        obj.role = None
        with (
            patch("app.services.suggestion_service.tools.liftoff",
                  return_value=_FakeTool(False)),
            patch(
                "app.services.object_service.list_objects",
                return_value=[
                    SimpleNamespace(
                        id="ann1",
                        name="ref.gff3",
                        role=None,
                        format=SimpleNamespace(kind=FormatKind.GFF),
                        status=ObjectStatus.READY,
                    )
                ],
            ),
            patch("app.services.pipeline_service.read_chemistry_for_alignment",
                  return_value=None),
            patch("app.services.pipeline_service.resolve_annotation_inputs",
                  return_value=None),
            patch("app.services.prior_runs._runs_touching", return_value=[]),
            patch("app.services.running_now._active_jobs_for", return_value=[]),
        ):
            cards = await suggestions_for(obj)
        card = next(c for c in cards if c["kind"] == "transfer_annotation")
        assert card["status"] == "unavailable"
        assert "Liftoff" in card["reason"]


def _assembly_object(obj_id="asm1"):
    """A READY assembly-shaped FASTA, matching `_fake_obj`'s pattern of a
    SimpleNamespace carrying only what the builder reads. `role` is set
    explicitly to None -- `_fake_obj` does not set it at all, and
    `_is_assembly_like` reads `obj.role`, the same fixup
    `TestScaffoldCardOrchestration`/`TestMisassemblyCardOrchestration` apply
    to their own assembly-shaped fixtures."""
    obj = _fake_obj(kind=FormatKind.FASTA, obj_id=obj_id)
    obj.role = None
    return obj


def _bam_object(assembly_id, obj_id="bam1"):
    """A READY BAM aligned against `assembly_id` -- `derived_from` contains
    the assembly's id, the provenance link `build_assembly_error_card`'s
    caller filters on."""
    bam = _fake_obj(kind=FormatKind.BAM, obj_id=obj_id)
    bam.derived_from = [assembly_id]
    return bam


class TestAssemblyErrorCard:
    """`alignments` is `(short, long, unknown)`, the pre-split
    `pipeline_service.alignments_against` returns -- matching the shape
    `launch_assembly_error_qc` itself consumes to auto-pair or refuse.
    """

    def test_unavailable_when_craq_is_not_installed(self):
        from app.pipelines import tools as tools_module
        from app.services import suggestion_service

        # `Tool.available` is a computed property (path is not None, error is
        # None, and -- for an on-demand tool -- install_state is INSTALLED),
        # not a constructor field, so an unavailable probe is built by
        # leaving `path` unset and setting `error`, not by passing
        # `available=False` directly.
        broken = tools_module.Tool(
            name="craq", path=None, version=None, error="CRAQ is not installed.",
        )
        with patch.object(suggestion_service.tools, "craq", return_value=broken):
            card = suggestion_service.build_assembly_error_card(
                _assembly_object(), ([_bam_object("asm1")], [], [])
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason

    def test_unavailable_without_any_alignment(self):
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "craq", return_value=_FakeTool(True, name="craq")
        ):
            card = suggestion_service.build_assembly_error_card(
                _assembly_object(), ([], [], [])
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "aligned" in card.reason.lower()

    def test_unavailable_with_only_unknown_chemistry_alignments(self):
        """A project with alignments CRAQ cannot classify by chemistry reads
        as "none usable", the same reason as no alignments at all --
        `launch_assembly_error_qc` never looks at `unknown` when auto-pairing,
        so a card offering to launch on unknown-only BAMs would be offering
        something the launch path refuses."""
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "craq", return_value=_FakeTool(True, name="craq")
        ):
            card = suggestion_service.build_assembly_error_card(
                _assembly_object(), ([], [], [_bam_object("asm1")])
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "aligned" in card.reason.lower()

    def test_available_with_exactly_one_alignment(self):
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "craq", return_value=_FakeTool(True, name="craq")
        ):
            card = suggestion_service.build_assembly_error_card(
                _assembly_object(), ([_bam_object("asm1")], [], [])
            )
        assert card is not None
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/assembly-errors"
        assert card.launch["body"]["object_id"] == "asm1"

    def test_available_with_one_short_and_one_unknown_chemistry_alignment(self):
        """Unknown-chemistry BAMs must not tip an otherwise-unambiguous
        project into "ambiguous" -- `launch_assembly_error_qc` auto-pairs on
        `short[0]` and ignores `unknown` entirely when ids are omitted, so
        the card should follow the same rule rather than refusing a launch
        that would actually succeed."""
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "craq", return_value=_FakeTool(True, name="craq")
        ):
            card = suggestion_service.build_assembly_error_card(
                _assembly_object(),
                ([_bam_object("asm1", obj_id="bam1")], [], [_bam_object("asm1", obj_id="bam2")]),
            )
        assert card is not None
        assert card.status is CardStatus.AVAILABLE

    def test_unavailable_with_two_short_read_alignments(self):
        """Mirrors `build_misassembly_card`'s `len(references) > 1` refusal,
        and `launch_assembly_error_qc`'s own `len(short) > 1` check -- the
        gap this test guards: the card previously went AVAILABLE for any
        nonzero alignment count with no split by chemistry, so two short-read
        BAMs against the same assembly rendered an AVAILABLE card whose
        click would raise a ValidationError at launch."""
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "craq", return_value=_FakeTool(True, name="craq")
        ):
            card = suggestion_service.build_assembly_error_card(
                _assembly_object(),
                (
                    [_bam_object("asm1", obj_id="bam1"), _bam_object("asm1", obj_id="bam2")],
                    [],
                    [],
                ),
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "alignment" in card.reason.lower()

    def test_unavailable_with_two_long_read_alignments(self):
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "craq", return_value=_FakeTool(True, name="craq")
        ):
            card = suggestion_service.build_assembly_error_card(
                _assembly_object(),
                (
                    [],
                    [_bam_object("asm1", obj_id="bam1"), _bam_object("asm1", obj_id="bam2")],
                    [],
                ),
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "alignment" in card.reason.lower()

    def test_unavailable_with_one_short_and_one_long_and_extra_short(self):
        """Ambiguity in either bucket refuses the whole card, even when the
        other bucket is unambiguous -- matching
        `len(short) > 1 or len(long_) > 1` rather than only checking the
        bucket that grew."""
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "craq", return_value=_FakeTool(True, name="craq")
        ):
            card = suggestion_service.build_assembly_error_card(
                _assembly_object(),
                (
                    [_bam_object("asm1", obj_id="bam1"), _bam_object("asm1", obj_id="bam2")],
                    [_bam_object("asm1", obj_id="bam3")],
                    [],
                ),
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE


def _read_object(obj_id="reads1", name="reads.fastq.gz"):
    """A READY FASTQ object, matching `_fake_obj`'s pattern. `read_sets` for
    `build_qv_card` is a list of these grouped into sets (each a list of one
    or two objects), the same shape `build_polish_card` consumes.

    `_fake_obj` itself carries no `.name` -- the builders that need it
    (`build_polish_card`'s `why=` string, mirrored here) set it on the
    returned namespace, same as `_bam_object`'s callers do elsewhere in this
    file."""
    obj = _fake_obj(kind=FormatKind.FASTQ, obj_id=obj_id, facts={})
    obj.name = name
    return obj


class TestQvCard:
    """`read_sets` is a `list[list[DataObject]]`, matching
    `build_polish_card`'s contract -- but unlike polish, not filtered to
    short reads, since Merqury's k-mer comparison works for any chemistry.
    """

    def test_unavailable_when_meryl_is_not_installed(self):
        """Assert the UNAVAILABLE direction. The image ships every tool
        installed by default, so an available-direction assertion alone
        would pass whether or not the patch actually reached the card."""
        from app.pipelines import tools as tools_module
        from app.services import suggestion_service

        broken = tools_module.Tool(
            name="meryl", path=None, version=None, error="meryl is not installed.",
        )
        with (
            patch.object(suggestion_service.tools, "meryl", return_value=broken),
            patch.object(
                suggestion_service.tools, "merqury",
                return_value=_FakeTool(True, name="merqury"),
            ),
        ):
            card = suggestion_service.build_qv_card(
                _assembly_object(), [[_read_object()]]
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason.lower()

    def test_unavailable_when_merqury_is_not_installed(self):
        from app.pipelines import tools as tools_module
        from app.services import suggestion_service

        broken = tools_module.Tool(
            name="merqury", path=None, version=None, error="Merqury is not installed.",
        )
        with (
            patch.object(
                suggestion_service.tools, "meryl",
                return_value=_FakeTool(True, name="meryl"),
            ),
            patch.object(suggestion_service.tools, "merqury", return_value=broken),
        ):
            card = suggestion_service.build_qv_card(
                _assembly_object(), [[_read_object()]]
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason.lower()

    def test_unavailable_without_read_sets(self):
        from app.services import suggestion_service

        with (
            patch.object(
                suggestion_service.tools, "meryl",
                return_value=_FakeTool(True, name="meryl"),
            ),
            patch.object(
                suggestion_service.tools, "merqury",
                return_value=_FakeTool(True, name="merqury"),
            ),
        ):
            card = suggestion_service.build_qv_card(_assembly_object(), [])
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "read" in card.reason.lower()

    def test_unavailable_when_ambiguous(self):
        from app.services import suggestion_service

        with (
            patch.object(
                suggestion_service.tools, "meryl",
                return_value=_FakeTool(True, name="meryl"),
            ),
            patch.object(
                suggestion_service.tools, "merqury",
                return_value=_FakeTool(True, name="merqury"),
            ),
        ):
            card = suggestion_service.build_qv_card(
                _assembly_object(),
                [
                    [_read_object(obj_id="reads1", name="a.fastq.gz")],
                    [_read_object(obj_id="reads2", name="b.fastq.gz")],
                ],
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "read set" in card.reason.lower()

    def test_available_with_exactly_one_read_set(self):
        from app.services import suggestion_service

        reads = _read_object(obj_id="reads1", name="a.fastq.gz")
        with (
            patch.object(
                suggestion_service.tools, "meryl",
                return_value=_FakeTool(True, name="meryl"),
            ),
            patch.object(
                suggestion_service.tools, "merqury",
                return_value=_FakeTool(True, name="merqury"),
            ),
        ):
            card = suggestion_service.build_qv_card(
                _assembly_object(), [[reads]]
            )
        assert card is not None
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/assembly-qv"
        assert card.launch["body"]["object_id"] == "asm1"
        assert card.launch["body"]["read_object_id"] == "reads1"


class TestContinuityCard:
    """`build_continuity_card(obj, alignments, gci_candidates)` -- `alignments`
    is the same `(short, long, unknown)` split `build_assembly_error_card`
    consumes, used only to distinguish "no long reads" from "short-read
    alignments only" in the unavailable message. `gci_candidates` is
    `(hifi_candidates, nano_candidates)`, GCI's own further split of
    `long_` by chemistry -- what `pipeline_service._gci_candidates` (and
    therefore `launch_continuity_qc`) actually uses to auto-pair, so the
    card's ambiguity/CLR gates match the launch path exactly.
    """

    def test_unavailable_when_gci_is_not_installed(self):
        """The image ships most tools installed, so an available-direction
        test alone would pass even if the tool patch never took effect --
        assert the UNAVAILABLE direction specifically."""
        from app.pipelines import tools as tools_module
        from app.services import suggestion_service

        broken = tools_module.Tool(
            name="gci", path=None, version=None, error="GCI is not installed.",
        )
        with patch.object(suggestion_service.tools, "gci", return_value=broken):
            card = suggestion_service.build_continuity_card(
                _assembly_object(),
                ([], [_bam_object("asm1")], []),
                ([_bam_object("asm1")], []),
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason

    def test_unavailable_without_any_alignment(self):
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "gci", return_value=_FakeTool(True, name="gci")
        ):
            card = suggestion_service.build_continuity_card(
                _assembly_object(), ([], [], []), ([], [])
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "long reads" in card.reason.lower()

    def test_unavailable_message_is_specific_for_short_read_only(self):
        """Short-read-only projects must not get the generic "align reads
        first" message: GCI takes no short-read input at all, so that
        advice would send the user to redo work that cannot help."""
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "gci", return_value=_FakeTool(True, name="gci")
        ):
            card = suggestion_service.build_continuity_card(
                _assembly_object(),
                ([_bam_object("asm1")], [], []),
                ([], []),
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "short-read" in card.reason.lower()
        assert "align a read set against it first" not in card.reason.lower()

    def test_unavailable_when_only_clr_long_reads(self):
        """Long reads are present (`long_` is nonempty) but none survived
        `_gci_candidates`'s chemistry split -- CLR-only. Must not be
        confused with "no long reads at all"."""
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "gci", return_value=_FakeTool(True, name="gci")
        ):
            card = suggestion_service.build_continuity_card(
                _assembly_object(),
                ([], [_bam_object("asm1")], []),
                ([], []),
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "clr" in card.reason.lower()

    def test_available_with_one_hifi_candidate(self):
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "gci", return_value=_FakeTool(True, name="gci")
        ):
            card = suggestion_service.build_continuity_card(
                _assembly_object(),
                ([], [_bam_object("asm1")], []),
                ([_bam_object("asm1")], []),
            )
        assert card is not None
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/assembly-continuity"
        assert card.launch["body"]["object_id"] == "asm1"

    def test_available_with_one_hifi_and_one_nano_candidate(self):
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "gci", return_value=_FakeTool(True, name="gci")
        ):
            card = suggestion_service.build_continuity_card(
                _assembly_object(),
                (
                    [],
                    [_bam_object("asm1", obj_id="bam1"), _bam_object("asm1", obj_id="bam2")],
                    [],
                ),
                (
                    [_bam_object("asm1", obj_id="bam1")],
                    [_bam_object("asm1", obj_id="bam2")],
                ),
            )
        assert card is not None
        assert card.status is CardStatus.AVAILABLE

    def test_unavailable_with_two_hifi_candidates(self):
        """Mirrors `build_assembly_error_card`'s ambiguity gate: more than
        one usable candidate in either GCI slot is refused rather than
        silently picking one."""
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "gci", return_value=_FakeTool(True, name="gci")
        ):
            card = suggestion_service.build_continuity_card(
                _assembly_object(),
                (
                    [],
                    [_bam_object("asm1", obj_id="bam1"), _bam_object("asm1", obj_id="bam2")],
                    [],
                ),
                (
                    [_bam_object("asm1", obj_id="bam1"), _bam_object("asm1", obj_id="bam2")],
                    [],
                ),
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "alignment" in card.reason.lower()

    def test_unavailable_with_two_nano_candidates(self):
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "gci", return_value=_FakeTool(True, name="gci")
        ):
            card = suggestion_service.build_continuity_card(
                _assembly_object(),
                (
                    [],
                    [_bam_object("asm1", obj_id="bam1"), _bam_object("asm1", obj_id="bam2")],
                    [],
                ),
                (
                    [],
                    [_bam_object("asm1", obj_id="bam1"), _bam_object("asm1", obj_id="bam2")],
                ),
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE

    def test_available_with_two_hifi_candidates_from_different_aligners(self):
        """Since winnowmap: two HiFi candidates is not ambiguous when they
        came from two different aligners -- that is GCI's own recommended
        cross-check setup, not a pick-one situation. Mirrors
        `launch_continuity_qc`'s `_group_gci_candidates_by_aligner`."""
        from app.services import suggestion_service

        mm2 = _bam_object("asm1", obj_id="bam1")
        mm2.facts = {"aligned_by": "minimap2"}
        wm2 = _bam_object("asm1", obj_id="bam2")
        wm2.facts = {"aligned_by": "winnowmap"}

        with patch.object(
            suggestion_service.tools, "gci", return_value=_FakeTool(True, name="gci")
        ):
            card = suggestion_service.build_continuity_card(
                _assembly_object(),
                ([], [mm2, wm2], []),
                ([mm2, wm2], []),
            )
        assert card is not None
        assert card.status is CardStatus.AVAILABLE
        assert "cross-check" in card.why.lower()

    def test_unavailable_with_two_hifi_candidates_from_same_aligner_despite_a_third(self):
        """A duplicate within one aligner's group must still refuse even
        when a different aligner's single candidate is also present."""
        from app.services import suggestion_service

        mm2_a = _bam_object("asm1", obj_id="bam1")
        mm2_a.facts = {"aligned_by": "minimap2"}
        mm2_b = _bam_object("asm1", obj_id="bam2")
        mm2_b.facts = {"aligned_by": "minimap2"}
        wm2 = _bam_object("asm1", obj_id="bam3")
        wm2.facts = {"aligned_by": "winnowmap"}

        with patch.object(
            suggestion_service.tools, "gci", return_value=_FakeTool(True, name="gci")
        ):
            card = suggestion_service.build_continuity_card(
                _assembly_object(),
                ([], [mm2_a, mm2_b, wm2], []),
                ([mm2_a, mm2_b, wm2], []),
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "same aligner" in card.reason.lower()


class TestCardBuilderRegistry:
    """`CARD_BUILDERS` is hand-maintained and keyed by convention.

    The registry-audit shape from CLAUDE.md: a builder that exists but is not
    registered renders nothing, with no error anywhere -- the same silent-skip
    failure that cost `results._SIDECAR_ROLES` a build_index job's eight files.
    A chain of `if` statements could not have that bug; a registry can, so it
    pays for the test.
    """

    def _builder_names(self):
        """Every `build_*_card` this module defines."""
        import app.services.suggestion_service as mod

        return {
            name
            for name in dir(mod)
            if name.startswith("build_") and name.endswith("_card")
        }

    def _registered_names(self):
        """Every `build_*_card` the registry's lambdas actually call.

        Read from each lambda's bytecode rather than by calling it: calling
        would need a fully-populated context and would make this test depend
        on card logic it is not testing.
        """
        from app.services.suggestion_service import CARD_BUILDERS

        names = set()
        for _kind, build in CARD_BUILDERS:
            names.update(
                n
                for n in build.__code__.co_names
                if n.startswith("build_") and n.endswith("_card")
            )
        return names

    def test_every_builder_is_registered(self):
        """If this fails after you added a card: add it to CARD_BUILDERS.

        Position in that tuple is the card's position in the Actions tab, so
        appending puts it last -- a UI decision worth making deliberately.
        Do not delete the assertion.
        """
        assert self._builder_names() == self._registered_names()

    def test_no_builder_is_registered_twice(self):
        """Two entries calling one builder renders that card twice."""
        from app.services.suggestion_service import CARD_BUILDERS

        called = [
            n
            for _kind, build in CARD_BUILDERS
            for n in build.__code__.co_names
            if n.startswith("build_") and n.endswith("_card")
        ]
        assert len(called) == len(set(called))

    def test_kinds_are_unique(self):
        """`kind` is what the frontend keys a card by, and what the failure log names."""
        from app.services.suggestion_service import CARD_BUILDERS

        kinds = [kind for kind, _build in CARD_BUILDERS]
        assert len(kinds) == len(set(kinds))

    def test_configure_dialogs_cover_only_real_kinds(self):
        """A key here that no builder emits is a dialog nothing can open.

        The partition matters in one direction only: `_CONFIGURE_DIALOGS` is
        deliberately partial, so a kind missing from it is a card with no
        Adjust button -- correct for the twelve kinds with no dialog. A key
        that matches *no* kind is always a bug: a renamed kind silently drops
        its button, which is the registry-audit failure shape, so hold the
        keys to CARD_BUILDERS rather than asserting equality.
        """
        from app.services.suggestion_service import (
            _CONFIGURE_DIALOGS,
            CARD_BUILDERS,
        )

        kinds = {kind for kind, _build in CARD_BUILDERS}
        assert set(_CONFIGURE_DIALOGS) <= kinds, (
            "these _CONFIGURE_DIALOGS keys match no card kind: "
            f"{sorted(set(_CONFIGURE_DIALOGS) - kinds)}"
        )

    def test_configure_dialog_names_are_known_to_the_frontend(self):
        """The dialog name is a contract with `DetailPanel`'s switch.

        Nothing mechanical ties this list to the TSX, so it is written out
        here: a name added on this side and not the other renders an Adjust
        button that opens nothing at all.
        """
        from app.services.suggestion_service import _CONFIGURE_DIALOGS

        assert set(_CONFIGURE_DIALOGS.values()) == {
            "trim",
            "align",
            "variant",
            "annotation",
            "quantify",
            "assemble",
            "scaffold",
            "completeness",
            "polish_long",
            "classify_reads",
            "phase",
        }

    def test_every_launch_endpoint_is_a_real_route(self):
        """An AVAILABLE card whose endpoint no route serves 404s on Launch,
        and nothing on screen says so -- the card looks launchable because
        it is AVAILABLE rather than UNAVAILABLE. That was #495: both meryl
        cards pointed at `/pipelines/meryl-analysis`, which existed as a
        service function and never as a route.

        Read statically from the source rather than by building every card,
        because the endpoint of a card this test cannot construct (one
        needing a fixture nobody wrote yet) is exactly the one that breaks
        unnoticed. Every `"endpoint": "..."` literal in the module counts,
        reachable or not.
        """
        import ast
        import inspect

        from app.api.v1 import pipelines
        from app.services import suggestion_service

        tree = ast.parse(inspect.getsource(suggestion_service))
        declared = {
            value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant)
            and key.value == "endpoint"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        }
        assert declared, "found no endpoint literals -- has the shape changed?"

        routes = {
            r.path
            for r in pipelines.router.routes
            if "POST" in getattr(r, "methods", set())
        }
        unknown = {ep for ep in declared if ep not in routes}
        assert unknown == set(), (
            f"cards launch endpoints with no POST route: {sorted(unknown)}"
        )


def _ont_fastq(obj_id="ont1", name="reads.ont.fastq.gz"):
    """A READY long-read FASTQ, matching `_read_object`'s pattern.

    `facts={"qc_platform": "OXFORD_NANOPORE"}` is what `is_long_read_for_polishing`
    (`reference_assembly.py`) reads via `_qc_platform` to classify a FASTQ
    as long-read -- the card builder itself never inspects facts, since
    `long_read_sets` has already done that filtering by the time the card
    sees it, but the name mirrors `_read_object` for readability.
    """
    obj = _fake_obj(
        kind=FormatKind.FASTQ,
        obj_id=obj_id,
        facts={"qc_platform": "OXFORD_NANOPORE"},
    )
    obj.name = name
    return obj


def _illumina_fastq(obj_id="ill1", name="reads.illumina.fastq.gz"):
    """A READY short-read FASTQ -- the Illumina counterpart of `_ont_fastq`,
    used only to prove the two polish cards gate on disjoint read sets."""
    obj = _fake_obj(
        kind=FormatKind.FASTQ,
        obj_id=obj_id,
        facts={"qc_platform": "ILLUMINA"},
    )
    obj.name = name
    return obj


class TestPolishLongCard:
    """`build_polish_long_card(obj, long_read_sets)` -- the Medaka sibling of
    `build_polish_card`. Same ambiguity gate, same shape, different tool and
    endpoint.
    """

    def test_unavailable_when_medaka_is_missing(self):
        """Assert the UNAVAILABLE direction, per CLAUDE.md.

        The image ships tools installed, so an "available" assertion passes
        whether or not the patch worked. This is the direction that fails
        when the seam breaks.
        """
        from app.pipelines import tools as tools_module
        from app.services import suggestion_service

        broken = tools_module.Tool(
            name="medaka", path=None, version=None, error="Medaka is not installed.",
        )
        with patch.object(suggestion_service.tools, "medaka", return_value=broken):
            card = suggestion_service.build_polish_long_card(
                _assembly_object(), [[_ont_fastq()]]
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason

    def test_unavailable_with_no_long_reads(self):
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "medaka",
            return_value=_FakeTool(True, name="medaka"),
        ):
            card = suggestion_service.build_polish_long_card(_assembly_object(), [])
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "long reads" in card.reason

    def test_unavailable_with_several_long_read_sets(self):
        """Ambiguity is unavailable, not a guess.

        Cards launch directly with the body they carry, so a card that
        picked one of several sets would silently polish with whichever it
        chose -- producing a plausible assembly that is quietly wrong.
        """
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "medaka",
            return_value=_FakeTool(True, name="medaka"),
        ):
            card = suggestion_service.build_polish_long_card(
                _assembly_object(),
                [
                    [_ont_fastq(obj_id="ont1", name="a.fastq.gz")],
                    [_ont_fastq(obj_id="ont2", name="b.fastq.gz")],
                ],
            )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
        assert "2" in card.reason

    def test_available_with_exactly_one_long_read_set(self):
        from app.services import suggestion_service

        with patch.object(
            suggestion_service.tools, "medaka",
            return_value=_FakeTool(True, name="medaka"),
        ):
            card = suggestion_service.build_polish_long_card(
                _assembly_object(), [[_ont_fastq()]]
            )
        assert card is not None
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/polish-long"
        assert card.launch["body"]["draft_object_id"] == "asm1"
        assert card.launch["body"]["reads_object_id"] == "ont1"

    def test_no_card_for_a_non_assembly(self):
        from app.services import suggestion_service

        assert suggestion_service.build_polish_long_card(_read_object(), []) is None


class TestPolishCardsDoNotCollide:
    """Success criterion 3: the two cards never offer a broken combination.

    They gate on mutually exclusive chemistry predicates, so this is a
    property of the structure rather than a rule anything enforces -- which
    is exactly why it is worth a test that would catch the structure
    changing.
    """

    def test_short_read_project_gets_polypolish_not_medaka(self):
        from app.services import suggestion_service

        obj = _assembly_object()
        with (
            patch.object(
                suggestion_service.tools, "polypolish",
                return_value=_FakeTool(True, name="polypolish"),
            ),
            patch.object(
                suggestion_service.tools, "bwa_mem2",
                return_value=_FakeTool(True, name="bwa-mem2"),
            ),
            patch.object(
                suggestion_service.tools, "medaka",
                return_value=_FakeTool(True, name="medaka"),
            ),
        ):
            short_card = suggestion_service.build_polish_card(
                obj, [[_illumina_fastq()]]
            )
            long_card = suggestion_service.build_polish_long_card(obj, [])
        assert short_card.status is CardStatus.AVAILABLE
        assert long_card.status is CardStatus.UNAVAILABLE

    def test_long_read_project_gets_medaka_not_polypolish(self):
        from app.services import suggestion_service

        obj = _assembly_object()
        with (
            patch.object(
                suggestion_service.tools, "polypolish",
                return_value=_FakeTool(True, name="polypolish"),
            ),
            patch.object(
                suggestion_service.tools, "bwa_mem2",
                return_value=_FakeTool(True, name="bwa-mem2"),
            ),
            patch.object(
                suggestion_service.tools, "medaka",
                return_value=_FakeTool(True, name="medaka"),
            ),
        ):
            long_card = suggestion_service.build_polish_long_card(
                obj, [[_ont_fastq()]]
            )
            short_card = suggestion_service.build_polish_card(obj, [])
        assert long_card.status is CardStatus.AVAILABLE
        assert short_card.status is CardStatus.UNAVAILABLE


class TestMergeStructuralVariantsCard:
    """Pin each branch of build_merge_structural_variants_card's gating.

    The card takes a *pre-fetched* sibling list from suggestions_for rather
    than re-querying, so the unit tests pass it directly -- the database
    integration is covered separately in test_sv_merge_launch.py.
    """

    @staticmethod
    def _snf_obj(obj_id: str = "snf1"):
        """A stand-in SNF sidecar: sidecar_role must be SNF for the card to apply."""
        o = _fake_obj(obj_id=obj_id)
        o.sidecar_role = SidecarRole.SNF
        return o

    def test_not_offered_for_a_non_snf(self):
        """The card only fires on SNF sidecars; anything else is None (not
        an unavailable card), so it does not waste grid space."""
        obj = _fake_obj()  # sidecar_role is unset / None
        obj.sidecar_role = None
        assert build_merge_structural_variants_card(obj, ["a", "b"]) is None

    def test_not_offered_for_a_vcf(self):
        obj = _fake_obj()
        obj.sidecar_role = None
        obj.role = ObjectRole.VARIANTS
        assert build_merge_structural_variants_card(obj, ["a", "b"]) is None

    def test_unavailable_when_sniffles_is_not_installed(self):
        with patch("app.services.suggestion_service.tools.sniffles",
                   return_value=_FakeTool(False, name="sniffles")):
            card = build_merge_structural_variants_card(self._snf_obj(), ["a", "b"])
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "sniffles" in card.reason.lower() or "not installed" in card.reason.lower()

    def test_unavailable_when_sibling_lookup_failed(self):
        """None means suggestions_for could not resolve siblings -- the card
        must decline rather than guess, which would silently offer a merge
        of one."""
        with patch("app.services.suggestion_service.tools.sniffles",
                   return_value=_FakeTool(True)):
            card = build_merge_structural_variants_card(self._snf_obj(), None)
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None

    def test_unavailable_with_only_one_sibling(self):
        """A single .snf cannot be merged with itself; the card must refuse
        rather than emit a degenerate single-sample combine."""
        with patch("app.services.suggestion_service.tools.sniffles",
                   return_value=_FakeTool(True)):
            card = build_merge_structural_variants_card(self._snf_obj(), ["snf1"])
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None

    def test_available_with_two_siblings_carries_all_ids_in_body(self):
        """The launch body must contain every sibling ID, not just the one
        the card was clicked on -- this is the whole point of the sibling
        lookup."""
        with patch("app.services.suggestion_service.tools.sniffles",
                   return_value=_FakeTool(True)):
            card = build_merge_structural_variants_card(
                self._snf_obj("snf1"), ["snf1", "snf2"]
            )
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/merge_structural_variants"
        assert card.launch["body"]["snf_object_ids"] == ["snf1", "snf2"]

    def test_available_with_many_siblings_preserves_order(self):
        """sibling_snf_callsets returns a sorted list; the card must pass it
        through unchanged so the launch body matches what the service
        dedups against."""
        ids = ["snf-a", "snf-b", "snf-c"]
        with patch("app.services.suggestion_service.tools.sniffles",
                   return_value=_FakeTool(True)):
            card = build_merge_structural_variants_card(
                self._snf_obj("snf-a"), ids
            )
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["body"]["snf_object_ids"] == ids


def _transcript_assembly_bam(aligned_by, **facts):
    # Named distinctly from the module-level `_bam` above (which takes
    # `chemistry_facts`/`obj_id`) to avoid shadowing it -- this helper's
    # signature is specific to the aligner-gate tests below.
    return SimpleNamespace(
        id=PydanticObjectId(),
        status=ObjectStatus.READY,
        format=SimpleNamespace(kind=FormatKind.BAM),
        role=None,
        facts={"aligned_by": aligned_by, **facts} if aligned_by else dict(facts),
        metadata={},
    )


class TestTranscriptAssemblyCard:
    @pytest.mark.parametrize("aligner", ["hisat2", "star"])
    def test_transcript_assembly_card_offered_for_splice_aware_alignments(
        self, aligner
    ):
        card = build_transcript_assembly_card(
            _transcript_assembly_bam(aligner),
            annotations=[SimpleNamespace(id=PydanticObjectId())],
        )
        assert card is not None
        assert card.kind == "transcript_assembly"

    @pytest.mark.parametrize(
        "aligner", ["bwa-mem2", "minimap2", "bowtie2", "winnowmap"]
    )
    def test_no_card_at_all_for_dna_aligners(self, aligner):
        """Not UNAVAILABLE -- absent.

        The capability can never apply to a DNA-seq alignment, and a card
        advertising something impossible is worse than silence. UNAVAILABLE
        is reserved for the two states a user can act on: tool missing,
        annotation missing.
        """
        assert build_transcript_assembly_card(
            _transcript_assembly_bam(aligner),
            annotations=[SimpleNamespace(id=PydanticObjectId())],
        ) is None

    def test_no_card_when_the_bam_does_not_say_which_aligner_made_it(self):
        """A deliberate false negative.

        An uploaded or register-in-place BAM has no aligned_by, and may well
        be DNA-seq. This mirrors _group_gci_candidates_by_aligner's refusal
        to merge "unknown" into a named aligner.
        """
        assert build_transcript_assembly_card(
            _transcript_assembly_bam(None),
            annotations=[SimpleNamespace(id=PydanticObjectId())],
        ) is None

    def test_card_unavailable_when_stringtie_is_not_installed(self, monkeypatch):
        """The direction that fails when the seam breaks.

        The image ships StringTie installed, so asserting the card is
        *available* would pass whether or not the patch worked. Patching
        the probe off is what actually exercises the gate.
        """
        monkeypatch.setattr(
            tools,
            "stringtie",
            lambda: SimpleNamespace(
                name="stringtie", available=False, path="", error="not installed"
            ),
        )

        card = build_transcript_assembly_card(
            _transcript_assembly_bam("hisat2"),
            annotations=[SimpleNamespace(id=PydanticObjectId())],
        )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE

    def test_card_unavailable_when_the_project_has_no_annotation(self):
        card = build_transcript_assembly_card(
            _transcript_assembly_bam("hisat2"), annotations=[]
        )
        assert card is not None
        assert card.status is CardStatus.UNAVAILABLE
class TestMultiqcCard:
    """The aggregate QC card.

    The only card whose subject is the project rather than the object it is
    rendered beside, and the only one gated on a count of files carrying
    *retained output on disk* rather than on completed runs -- see
    `build_multiqc_card`'s docstring for why those differ.
    """

    def _card(self, count, *, available=True, kind=FormatKind.FASTQ):
        with patch(
            "app.services.suggestion_service.tools.multiqc",
            return_value=_FakeTool(available),
        ):
            return build_multiqc_card(_fake_obj(kind=kind), count)

    def test_offers_a_report_when_two_files_have_qc_output(self):
        card = self._card(2)
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/multiqc"

    def test_launches_against_the_project_not_the_object(self):
        """The report covers every file in the project. A body carrying an
        object id would be the wrong request entirely, not merely a narrower
        one."""
        card = self._card(3)
        assert "project_id" in card.launch["body"]
        assert "object_id" not in card.launch["body"]

    def test_one_file_is_not_an_aggregate(self):
        """One sample is a report the per-object QC tab already shows
        better."""
        card = self._card(1)
        assert card.status is CardStatus.UNAVAILABLE

    def test_zero_files_is_unavailable(self):
        assert self._card(0).status is CardStatus.UNAVAILABLE

    def test_the_reason_explains_that_older_files_need_qc_rerun(self):
        """The likeliest reason a project full of QC'd files reports zero is
        that they were QC'd before retention shipped. A reason saying only
        "run QC on more files" would read as wrong to someone looking at a
        project where every file has already been QC'd."""
        card = self._card(0)
        assert "re-run" in card.reason

    def test_an_uncountable_project_is_unavailable_rather_than_guessed(self):
        """`None` means the count could not be taken. Treating that as zero
        would be a guess; every other builder here reports it as
        unavailable."""
        card = self._card(None)
        assert card.status is CardStatus.UNAVAILABLE

    def test_unavailable_when_multiqc_is_not_installed(self):
        card = self._card(5, available=False)
        assert card.status is CardStatus.UNAVAILABLE

    def test_not_offered_on_non_read_objects(self):
        """A user looking at an assembly is not thinking about read QC, and
        a card repeated identically on every object in the project would be
        noise rather than a shortcut."""
        assert self._card(5, kind=FormatKind.FASTA) is None
