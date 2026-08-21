import dataclasses

from app.pipelines import assembler_registry
from app.pipelines.align_runner import ReadChemistry
from app.pipelines.assemblers import Assembler, OutputKind


def test_short_reads_route_to_abyss():
    spec = assembler_registry.spec_for_chemistry(ReadChemistry.SHORT)
    assert spec is not None
    assert spec.assembler is Assembler.ABYSS
    assert spec.layout == "paired"


def test_long_reads_still_route_to_flye():
    for chemistry in (
        ReadChemistry.HIFI,
        ReadChemistry.CLR,
        ReadChemistry.ONT_SIMPLEX,
        ReadChemistry.ONT_DUPLEX,
    ):
        spec = assembler_registry.spec_for_chemistry(chemistry)
        assert spec is not None
        assert spec.assembler is Assembler.FLYE


def test_unknown_chemistry_still_has_no_assembler():
    """The 'run QC first' refusal depends on this staying None."""
    assert assembler_registry.spec_for_chemistry(None) is None
    assert assembler_registry.spec_for_chemistry(ReadChemistry.UNKNOWN) is None


def test_abyss_declares_contigs_as_required_output():
    spec = assembler_registry.spec_for(Assembler.ABYSS)
    kinds = {o.kind: o for o in spec.outputs}
    assert kinds[OutputKind.CONTIGS].required is True
    assert kinds[OutputKind.CONTIGS].filename == "asm-scaffolds.fa"


def test_abyss_charges_for_read_volume():
    """A de Bruijn assembler whose model ignored coverage would under-predict."""
    spec = assembler_registry.spec_for(Assembler.ABYSS)
    assert spec.memory_model.bytes_per_read_base > 0


def test_spades_declares_contigs_as_required_output():
    spec = assembler_registry.spec_for(Assembler.SPADES)
    kinds = {o.kind: o for o in spec.outputs}
    assert kinds[OutputKind.CONTIGS].required is True
    assert kinds[OutputKind.CONTIGS].filename == "contigs.fasta"
    assert kinds[OutputKind.GRAPH].filename == "assembly_graph_with_scaffolds.gfa"


def test_spades_does_not_declare_scaffolds_fasta():
    """`harvest()` returns dict[OutputKind, Path] keyed by kind, and there is
    no separate OutputKind.SCAFFOLDS -- a scaffolds.fasta Output would share
    OutputKind.CONTIGS with the required contigs.fasta entry and silently
    lose that collision, declaring an output the pipeline can never actually
    deliver. The fix is not declaring it at all.
    """
    spec = assembler_registry.spec_for(Assembler.SPADES)
    filenames = {o.filename for o in spec.outputs}
    assert "scaffolds.fasta" not in filenames
    assert filenames == {"contigs.fasta", "assembly_graph_with_scaffolds.gfa"}


def test_spades_offers_exactly_the_four_modes():
    assert assembler_registry.modes_for(Assembler.SPADES) == frozenset(
        {"isolate", "careful", "meta", "standard"}
    )


def test_spades_spells_metagenome_mode_as_a_mode_not_a_checkbox():
    """The opposite of Flye, and deliberately so.

    SPAdes rejects `--meta` combined with `--isolate` or `--careful`, so the
    exclusivity the select already enforces is exactly the exclusivity the
    tool wants. Flye's `--meta` is orthogonal to its accuracy mode and is a
    `bool` field for that reason -- implementing the two the same way would be
    wrong in one direction or the other.
    """
    spec = assembler_registry.spec_for(Assembler.SPADES)
    assert not any(f.key == "meta" for f in spec.fields)
    assert "meta" in assembler_registry.modes_for(Assembler.SPADES)


def test_spades_has_a_meta_memory_model_keyed_on_read_volume():
    """A community has no single genome size, so the standard model's only
    input is unavailable -- without this the estimate is None and the run is
    guarded by nothing at all."""
    spec = assembler_registry.spec_for(Assembler.SPADES)
    assert spec.meta_memory_model is not None
    assert spec.meta_memory_model.bytes_per_genome_base == 0.0
    assert spec.meta_memory_model.bytes_per_read_base > 0


def test_spades_does_not_offer_a_kmer_field():
    """SPAdes picks k from read length; ABySS does not, which is why only
    ABySS has the field."""
    spec = assembler_registry.spec_for(Assembler.SPADES)
    assert not any(f.key == "k" for f in spec.fields)


def test_flye_offers_a_meta_field():
    spec = assembler_registry.spec_for(Assembler.FLYE)
    meta_field = next(f for f in spec.fields if f.key == "meta")
    assert meta_field.kind == "bool"
    assert meta_field.default is False


def test_flye_declares_a_meta_memory_model():
    """metaFlye's memory profile is not the single-genome one -- see
    resource_estimator.estimate_assembly_mb for where this is selected."""
    spec = assembler_registry.spec_for(Assembler.FLYE)
    assert spec.meta_memory_model is not None
    assert spec.meta_memory_model.bytes_per_read_base > 0


def test_a_meta_memory_model_exists_exactly_where_a_meta_mode_does():
    """The two must agree in both directions.

    A meta mode without a model estimates to None for a community (which has
    no genome size to feed the standard model) and is guarded by nothing; a
    model without a mode is dead configuration nothing ever selects. SPAdes
    gained both in #731, having had neither when this test was written for
    #727.

    MEGAHIT (#781) is deliberately in NEITHER set: it is a metagenome
    assembler throughout, so it has no meta *mode* to switch into and its one
    `memory_model` already is the community model. Adding it to
    `has_meta_mode` to "be consistent" would demand a second model that
    nothing could ever select. See `test_megahit_needs_no_meta_memory_model`
    for the property that keeps it estimable regardless.
    """
    has_meta_mode = {Assembler.FLYE, Assembler.SPADES}
    always_meta = {Assembler.MEGAHIT}
    for assembler in Assembler:
        spec = assembler_registry.spec_for(assembler)
        declares_model = spec.meta_memory_model is not None
        assert declares_model is (assembler in has_meta_mode), assembler
        # The partition's other half: an always-meta assembler must not be
        # smuggled into the switchable set, which would be the "forcing the
        # middle case into the first one's pattern" CLAUDE.md warns about.
        assert not (assembler in has_meta_mode and assembler in always_meta)


def test_megahit_declares_final_contigs_as_required_output():
    """The filename off v1.2.9's `merge_final()`, confirmed against a real run
    by install-megahit.sh's smoke assembly."""
    spec = assembler_registry.spec_for(Assembler.MEGAHIT)
    kinds = {o.kind: o for o in spec.outputs}
    assert kinds[OutputKind.CONTIGS].required is True
    assert kinds[OutputKind.CONTIGS].filename == "final.contigs.fa"


def test_megahit_declares_no_graph_output():
    """MEGAHIT writes intermediate per-k contigs, not an assembly graph
    anyone opens. A declared GRAPH output would be permanently absent."""
    spec = assembler_registry.spec_for(Assembler.MEGAHIT)
    assert OutputKind.GRAPH not in {o.kind for o in spec.outputs}


def test_megahit_charges_for_read_volume_and_not_genome_size():
    """A community has no single genome size, so the genome term is not
    merely small here -- there is no number to multiply."""
    spec = assembler_registry.spec_for(Assembler.MEGAHIT)
    assert spec.memory_model.bytes_per_genome_base == 0.0
    assert spec.memory_model.bytes_per_read_base > 0


def test_megahit_memory_model_is_not_copied_from_spades():
    """#781's constraint, as a test rather than a comment.

    Bounded memory is the entire reason this assembler is here, so inheriting
    SPAdes' coefficient would model away the property being modelled.
    """
    megahit = assembler_registry.spec_for(Assembler.MEGAHIT).memory_model
    spades = assembler_registry.spec_for(Assembler.SPADES).memory_model
    assert megahit.bytes_per_genome_base != spades.bytes_per_genome_base


def test_megahit_needs_no_meta_memory_model_to_be_estimable():
    """The hole this would otherwise fall into.

    `estimate_assembly_mb` used to select its read-only branch on
    `meta and spec.meta_memory_model is not None`. MEGAHIT is always meta and
    has no `meta_memory_model`, so that test sent it down the genome-size
    path -- where a community, which by definition has no genome size,
    estimates to None and is guarded by nothing at all. The branch now keys
    off the model's own coefficients instead.
    """
    from app.pipelines import resource_estimator

    estimate = resource_estimator.estimate_assembly_mb(
        assembler=Assembler.MEGAHIT,
        genome_bases=None,
        threads=4,
        read_bases=500_000_000,
        meta=True,
    )
    assert estimate is not None
    assert estimate > 0


def test_megahit_offers_no_mode_field():
    """There is no isolate/meta switch to offer, so `modes_for` is empty by
    construction rather than by omission."""
    spec = assembler_registry.spec_for(Assembler.MEGAHIT)
    assert not any(f.key == "mode" for f in spec.fields)
    assert assembler_registry.modes_for(Assembler.MEGAHIT) == frozenset()


def test_megahit_is_paired_layout():
    spec = assembler_registry.spec_for(Assembler.MEGAHIT)
    assert spec.layout == "paired"


def test_short_reads_still_route_to_megahit_never_by_default():
    """Installing an assembler makes it selectable; promoting it to the
    default is a separate decision -- the same reasoning recorded for SPAdes
    below. Routing every short-read assembly to a *metagenome* assembler
    would be wrong for the isolates that are this app's common case."""
    spec = assembler_registry.spec_for_chemistry(ReadChemistry.SHORT)
    assert spec.assembler is not Assembler.MEGAHIT


def test_short_reads_still_route_to_abyss_after_spades_is_installed():
    """Installing an assembler makes it selectable. Promoting it to the
    default changes every existing user's results and is a separate decision."""
    spec = assembler_registry.spec_for_chemistry(ReadChemistry.SHORT)
    assert spec.assembler is Assembler.ABYSS


class TestExhaustiveness:
    """A declared-and-installed assembler with no command builder would be
    dispatched to another tool's builder or refused at runtime. Both are
    silent until someone runs it. See CLAUDE.md on hand-maintained registries
    keyed by an enum.
    """

    def test_every_assembler_has_a_spec(self):
        from app.pipelines.assemblers import Assembler

        assert set(assembler_registry.SPECS) == set(Assembler)

    def test_every_installable_assembler_has_a_command_builder(self):
        from pathlib import Path

        from app.pipelines import assembly_params, assembly_runner
        from app.pipelines.assemblers import Assembler

        for member in Assembler:
            spec = assembler_registry.spec_for(member)
            if spec.tool is None:
                # Declared-but-not-installed (hifiasm, spades) is exempt:
                # `assembly_params.from_dict` refuses them before a builder is
                # ever reached.
                continue
            params = assembly_params.from_dict({"assembler": member.value})
            cmd = assembly_runner.build_assembly_command(
                assembler=member,
                tool_path=f"/usr/bin/{member.value}",
                reads=Path("/work/reads.fastq"),
                out_dir=Path("/work/out"),
                params=params,
            )
            assert cmd, f"{member.value} produced an empty command line"

    def test_every_installable_assembler_has_params(self):
        from app.pipelines import assembly_params
        from app.pipelines.assemblers import Assembler

        for member in Assembler:
            spec = assembler_registry.spec_for(member)
            if spec.tool is None:
                continue
            params = assembly_params.from_dict({"assembler": member.value})
            assert params.assembler is member


def test_spades_card_goes_unavailable_when_the_probe_is_off(monkeypatch):
    """Patch spec_for, never tools.spades: AssemblerSpec is frozen and
    captured the function object at import time, so patching the module
    attribute never reaches spec.tool.

    Asserted in the unavailable direction on purpose -- the image ships
    SPAdes installed, so asserting availability passes whether or not the
    patch worked."""
    from app.pipelines import tools

    missing = tools.Tool(
        name="spades", path=None, version=None, error="not installed"
    )
    real = assembler_registry.spec_for(Assembler.SPADES)
    patched = dataclasses.replace(real, tool=lambda: missing)
    monkeypatch.setattr(
        assembler_registry, "spec_for", lambda a: patched if a is Assembler.SPADES else real
    )

    assert assembler_registry.spec_for(Assembler.SPADES).available() is False
