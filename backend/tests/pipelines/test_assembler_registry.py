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
