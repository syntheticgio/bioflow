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
    """
    has_meta_mode = {Assembler.FLYE, Assembler.SPADES}
    for assembler in Assembler:
        spec = assembler_registry.spec_for(assembler)
        declares_model = spec.meta_memory_model is not None
        assert declares_model is (assembler in has_meta_mode), assembler


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
