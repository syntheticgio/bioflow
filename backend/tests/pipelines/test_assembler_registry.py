import dataclasses
from types import SimpleNamespace
from unittest.mock import patch

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


class TestSelectableAssemblers:
    """`selectable_for_chemistry` backs the dialog's assembler picker.

    The picker lists every *installed* assembler and disables the ones whose
    layout cannot take these reads, rather than hiding them -- a user who
    wonders why MEGAHIT is absent for a Nanopore file is better served by
    seeing it greyed out with a reason than by it not existing. So this
    function partitions rather than filters, and `compatible` is the flag the
    dialog disables on.

    Availability is patched throughout, except where a test is *about*
    availability. Without that these assert against whichever assemblers the
    host image happens to ship, so they would pass or fail on the container
    rather than on the partition logic they exist to check -- and an
    "is compatible" assertion is exactly the shape that passes for the wrong
    reason when the probe is the thing that moved. Patched via `SPECS`
    entries rather than `tools.<name>`: each spec is a frozen dataclass that
    captured its probe function at import, so the module attribute is no
    longer what `spec.tool` refers to (see `spec_for`).
    """

    @staticmethod
    def _all_installed():
        """Every declared-and-installable assembler probing as present.

        hifiasm is left alone: its `tool` is None, which is the fact
        `test_a_declared_but_uninstalled_assembler_is_never_listed` checks.
        """
        specs = {
            assembler: dataclasses.replace(spec, tool=lambda: SimpleNamespace(
                available=True
            ))
            for assembler, spec in assembler_registry.SPECS.items()
            if spec.tool is not None
        }
        return patch.dict(assembler_registry.SPECS, specs)

    def test_a_declared_but_uninstalled_assembler_is_never_listed(self):
        """hifiasm has `tool=None`, so `available()` is False without probing.

        Listing it would advertise a tool this build cannot run under any
        selection, which is a different thing from an installed tool that
        these particular reads cannot use.
        """
        listed = assembler_registry.selectable_for_chemistry(ReadChemistry.SHORT)
        assert Assembler.HIFIASM not in {entry.assembler for entry in listed}

    def test_short_reads_can_pick_every_paired_assembler(self):
        """The point of the issue: MEGAHIT and SPAdes exist, are correct, and
        were reachable only by an API caller passing `assembler:`.
        """
        with self._all_installed():
            listed = {
                entry.assembler: entry
                for entry in assembler_registry.selectable_for_chemistry(
                    ReadChemistry.SHORT
                )
            }
        for assembler in (Assembler.ABYSS, Assembler.SPADES, Assembler.MEGAHIT):
            assert listed[assembler].compatible is True

    def test_short_reads_see_flye_listed_but_incompatible(self):
        """Listed, not hidden -- and carrying a reason the dialog can show."""
        with self._all_installed():
            listed = {
                entry.assembler: entry
                for entry in assembler_registry.selectable_for_chemistry(
                    ReadChemistry.SHORT
                )
            }
        assert listed[Assembler.FLYE].compatible is False
        assert listed[Assembler.FLYE].incompatible_reason

    def test_long_reads_see_the_paired_assemblers_as_incompatible(self):
        with self._all_installed():
            listed = {
                entry.assembler: entry
                for entry in assembler_registry.selectable_for_chemistry(
                    ReadChemistry.HIFI
                )
            }
        assert listed[Assembler.FLYE].compatible is True
        for assembler in (Assembler.ABYSS, Assembler.SPADES, Assembler.MEGAHIT):
            assert listed[assembler].compatible is False

    def test_the_chemistry_default_is_marked_and_is_unique(self):
        """The dialog opens on this one, so exactly one entry must carry it --
        and it must agree with `spec_for_chemistry`, which is still the single
        place the default is decided.
        """
        with self._all_installed():
            listed = assembler_registry.selectable_for_chemistry(ReadChemistry.SHORT)
        defaults = [entry.assembler for entry in listed if entry.is_default]
        assert defaults == [Assembler.ABYSS]

    def test_a_default_assembler_is_always_compatible_with_its_own_chemistry(self):
        """A chemistry whose default was disabled in its own picker would open
        the dialog on an unlaunchable selection.
        """
        for chemistry in (
            ReadChemistry.SHORT,
            ReadChemistry.HIFI,
            ReadChemistry.CLR,
            ReadChemistry.ONT_SIMPLEX,
            ReadChemistry.ONT_DUPLEX,
        ):
            with self._all_installed():
                listed = assembler_registry.selectable_for_chemistry(chemistry)
            default = [entry for entry in listed if entry.is_default]
            assert len(default) == 1, chemistry
            assert default[0].compatible is True, chemistry

    def test_unknown_chemistry_has_nothing_to_pick(self):
        """`launch_assembly` refuses unknown chemistry before any picker
        matters; listing everything as compatible here would contradict it.
        """
        assert assembler_registry.selectable_for_chemistry(None) == ()
        assert assembler_registry.selectable_for_chemistry(ReadChemistry.UNKNOWN) == ()

    def test_an_assembler_whose_probe_goes_off_leaves_the_picker(self):
        """The negative direction, which the "is compatible" assertions above
        cannot establish on their own.

        CLAUDE.md's rule: the image ships most tools, so an availability
        assertion passes whether or not the patch worked. This one fails if
        `_all_installed` is silently a no-op, because it demands the listing
        *change* when one probe is turned off.
        """
        with self._all_installed():
            before = {
                entry.assembler
                for entry in assembler_registry.selectable_for_chemistry(
                    ReadChemistry.SHORT
                )
            }
        assert Assembler.MEGAHIT in before

        off = dataclasses.replace(
            assembler_registry.SPECS[Assembler.MEGAHIT],
            tool=lambda: SimpleNamespace(available=False),
        )
        with self._all_installed(), patch.dict(
            assembler_registry.SPECS, {Assembler.MEGAHIT: off}
        ):
            after = {
                entry.assembler
                for entry in assembler_registry.selectable_for_chemistry(
                    ReadChemistry.SHORT
                )
            }
        assert Assembler.MEGAHIT not in after

    def test_an_uninstalled_default_hands_the_mark_to_a_working_assembler(self):
        """Found by running the endpoint against the real image, which does
        not ship ABySS.

        `spec_for_chemistry` answers ABySS for short reads whether or not it is
        installed -- deliberately, since the refusal it produces names a tool
        the user could install. But the picker only lists installed
        assemblers, so an uninstalled default marked nothing at all: the
        dialog opened on an assembler absent from its own list, and the launch
        was refused as not-installed.

        So `is_default` marks the chemistry's pick when that pick is usable,
        and otherwise the first compatible assembler that is.
        """
        import dataclasses

        abyss_off = dataclasses.replace(
            assembler_registry.SPECS[Assembler.ABYSS],
            tool=lambda: SimpleNamespace(available=False),
        )
        with self._all_installed(), patch.dict(
            assembler_registry.SPECS, {Assembler.ABYSS: abyss_off}
        ):
            listed = assembler_registry.selectable_for_chemistry(ReadChemistry.SHORT)

        assert Assembler.ABYSS not in {entry.assembler for entry in listed}
        default = [entry for entry in listed if entry.is_default]
        assert len(default) == 1
        assert default[0].compatible is True

    def test_nothing_is_marked_default_when_nothing_compatible_is_installed(self):
        """No compatible assembler means no launchable selection, and marking
        an incompatible one would open the dialog on a choice that cannot run.
        """
        import dataclasses

        off = {
            assembler: dataclasses.replace(
                spec, tool=lambda: SimpleNamespace(available=False)
            )
            for assembler, spec in assembler_registry.SPECS.items()
            if spec.tool is not None and spec.layout == "paired"
        }
        with self._all_installed(), patch.dict(assembler_registry.SPECS, off):
            listed = assembler_registry.selectable_for_chemistry(ReadChemistry.SHORT)

        assert not any(entry.compatible for entry in listed)
        assert not any(entry.is_default for entry in listed)
