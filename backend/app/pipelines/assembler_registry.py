"""One spec per assembler: the single place an assembler is declared.

The argument `aligner_registry` makes applies here before the problem does,
which is the point of writing it this way for the first tool rather than the
third. There, adding an aligner meant five coordinated edits and nothing said
what an aligner *was*. Here there is one assembler, and the cost of the
registry is a file; the cost of not having one is discovered later, when
hifiasm arrives and its mode flags, output filenames and memory profile all
differ from Flye's in ways that would otherwise be spread across a runner, a
handler and a dialog.

`ParamField` and `Choice` are imported from `aligner_registry` rather than
redeclared. The dialog renders both through the same component, and two copies
of that dataclass is how a frontend ends up with two renderers that drift.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal

from app.pipelines import tools
from app.pipelines.align_runner import ReadChemistry
from app.pipelines.aligner_registry import Choice, ParamField
from app.pipelines.assemblers import Assembler, Output, OutputKind


@dataclass(frozen=True)
class AssemblyMemoryModel:
    """Coefficients for estimating peak memory, in the same spirit as
    `aligner_registry.MemoryModel`: published guidance, not measurements on
    this hardware.

    The dominant term is the genome, not the reads -- a repeat graph is built
    over the assembly, and coverage affects runtime far more than peak
    residency. Which is convenient, because genome size is the one number a
    user might actually know before assembling.
    """

    bytes_per_genome_base: float
    fixed_overhead_mb: int
    # Per thread, and small: Flye's parallelism is mostly over disjointigs
    # that share the graph rather than per-thread copies of it.
    mb_per_thread: int = 128
    # Bytes of peak residency per base of *input reads*. Zero for a repeat-graph
    # assembler like Flye, whose peak is dominated by the genome. Non-zero for a
    # de Bruijn assembler, where peak tracks distinct k-mers -- a function of
    # coverage as much as genome size. Defaulted to 0.0 so Flye's model is
    # arithmetically unchanged by this field existing.
    bytes_per_read_base: float = 0.0


@dataclass(frozen=True)
class AssemblerSpec:
    assembler: Assembler
    # None for a declared-but-not-installed assembler. `available()` below is
    # what callers should ask; this being None is why it can answer False
    # without probing anything.
    tool: Callable[[], tools.Tool] | None
    # Chemistry -> the tool's own input-mode flag, minus the leading `--`.
    # This is the same shape as align_runner's `_CHEMISTRY_PRESETS` and the
    # same fact feeding it: how accurate the reads are.
    mode_flags: dict[ReadChemistry, str]
    layout: Literal["single", "paired"]
    memory_model: AssemblyMemoryModel
    outputs: tuple[Output, ...]
    fields: tuple[ParamField, ...] = ()
    # Why this assembler is not usable, when it is not. Rendered by the card,
    # so it says what the user could do about it rather than naming a module.
    unavailable_reason: str = ""
    # A second model for `--meta` mode, where genome size stops being a
    # meaningful single number -- see FLYE_SPEC for why this can't just reuse
    # `memory_model`. None for every assembler without a meta mode.
    meta_memory_model: AssemblyMemoryModel | None = None

    def available(self) -> bool:
        return self.tool is not None and self.tool().available


# Every ABySS run assembles under this name, so its output filenames are
# knowable before the run starts. Not user-facing: the resulting DataObject is
# named after the reads by `assembly_handlers._contigs_name`.
ASSEMBLY_NAME_PREFIX = "asm"


_SHARED_FIELDS: tuple[ParamField, ...] = (
    ParamField(
        key="threads",
        label="Threads",
        kind="int",
        default=8,
        min=1,
        max=128,
        group="performance",
        help="More threads finish sooner but compete with other work.",
    ),
    ParamField(
        key="genome_size",
        label="Genome size",
        kind="text",
        default="",
        group="biology",
        help=(
            "Optional. Used to estimate how much memory this will need, not "
            "passed to the assembler. Accepts 4.6m or 3.1g. Left blank when "
            "nothing in the project can tell us -- which is normal for a "
            "genome with no reference."
        ),
    ),
)


FLYE_SPEC = AssemblerSpec(
    assembler=Assembler.FLYE,
    tool=tools.flye,
    mode_flags={
        # Flye's modes are graded by read *accuracy*, which is exactly what
        # ReadChemistry records, so this is a straight mapping rather than a
        # judgement per case -- with one exception, below.
        ReadChemistry.CLR: "pacbio-raw",
        ReadChemistry.HIFI: "pacbio-hifi",
        ReadChemistry.ONT_DUPLEX: "nano-hq",
        # The exception. `--nano-raw` is documented as pre-Guppy5 (<20% error)
        # and `--nano-hq` as Guppy5+ SUP or Q20 (<5%). ReadChemistry knows
        # simplex from duplex but not which basecaller ran, and most current
        # simplex data is Guppy5+ -- so this default is deliberately the
        # conservative one and deliberately overridable in the dialog.
        # Choosing nano-hq for reads that are not is the error that produces a
        # worse assembly quietly; the reverse merely leaves accuracy on the
        # table.
        ReadChemistry.ONT_SIMPLEX: "nano-raw",
    },
    layout="single",
    memory_model=AssemblyMemoryModel(
        # ~40 bytes per genome base is the order the Flye docs' own examples
        # imply (a 3Gb human assembly in the 400-600GB range is the published
        # figure for CLR; bacterial genomes land in single-digit GB). Wrong in
        # either direction costs a warning, never a refusal -- see
        # resource_estimator's band placement.
        bytes_per_genome_base=40.0,
        fixed_overhead_mb=2048,
    ),
    # `--meta` has no published memory formula -- Flye's own FAQ states peak
    # memory "grows linearly with genome size and reads coverage" but never
    # restates that for a mixed community, where "genome size" is not one
    # number. The only meta-mode data Flye publishes is its own benchmark
    # table (README, "Flye benchmarks"): four community assemblies ranging
    # from 7Gb PacBio input at 72GB RAM up to 255Gb HiFi input at 662GB RAM --
    # roughly 2.6 to 10 GB of peak RAM per Gbp of input reads, with assembled
    # community complexity confounding the ratio rather than a clean line.
    # Modelled here as `bytes_per_read_base` against *read* volume rather than
    # `bytes_per_genome_base` against a genome size that meta mode doesn't
    # have, at the conservative (high) end of that observed range: wrong in
    # the low direction is an OOM, wrong in the high direction is a warning.
    meta_memory_model=AssemblyMemoryModel(
        bytes_per_genome_base=0.0,
        bytes_per_read_base=10.0,
        fixed_overhead_mb=2048,
    ),
    outputs=(
        Output(kind=OutputKind.CONTIGS, filename="assembly.fasta", required=True),
        Output(kind=OutputKind.GRAPH, filename="assembly_graph.gfa"),
        Output(kind=OutputKind.INFO_TABLE, filename="assembly_info.txt"),
    ),
    fields=(
        *_SHARED_FIELDS,
        ParamField(
            key="meta",
            label="Metagenome mode",
            kind="bool",
            default=False,
            group="biology",
            help=(
                "For a mixed-community sample rather than a single organism. "
                "Makes Flye's graph simplification less aggressive, which "
                "suits non-uniform coverage across organisms. Genome size is "
                "ignored for the memory estimate in this mode -- a community "
                "has no single genome size -- in favour of total read volume."
            ),
        ),
        ParamField(
            key="mode",
            label="Input mode",
            kind="select",
            default="nano-raw",
            group="biology",
            help=(
                "How accurate the reads are. Set from the detected chemistry. "
                "Change it if you know better: Nanopore reads basecalled with "
                "Guppy5 or newer in SUP mode are high-quality even when "
                "simplex, and BioFlow cannot tell which basecaller ran."
            ),
            choices=(
                Choice(value="nano-raw", label="Nanopore, standard (<20% error)"),
                Choice(value="nano-hq", label="Nanopore, high quality (<5% error)"),
                Choice(value="nano-corr", label="Nanopore, corrected (<3% error)"),
                Choice(value="pacbio-raw", label="PacBio CLR (<20% error)"),
                Choice(value="pacbio-corr", label="PacBio, corrected (<3% error)"),
                Choice(value="pacbio-hifi", label="PacBio HiFi (<1% error)"),
            ),
        ),
        ParamField(
            key="iterations",
            label="Polishing rounds",
            kind="int",
            default=1,
            min=0,
            max=10,
            group="biology",
            help=(
                "Flye polishes its own draft. 0 skips it, which is reasonable "
                "for HiFi input where the reads are already accurate enough "
                "that polishing costs time for no gain."
            ),
        ),
    ),
)


# Declared so the API can say "not installed in this build" rather than
# "unknown assembler", and so a chemistry with no assembler has something to
# point at. Neither has a params class; `assembly_params.from_dict` refuses
# them before anything here is consulted.
HIFIASM_SPEC = AssemblerSpec(
    assembler=Assembler.HIFIASM,
    tool=None,
    mode_flags={},
    layout="single",
    memory_model=AssemblyMemoryModel(bytes_per_genome_base=60.0, fixed_overhead_mb=4096),
    outputs=(),
    unavailable_reason="hifiasm is not installed in this build.",
)

SPADES_SPEC = AssemblerSpec(
    assembler=Assembler.SPADES,
    tool=tools.spades,
    # Empty by construction, like ABySS: SPAdes has no read-accuracy mode
    # flag, and `spec_for_chemistry` does not reach it by chemistry lookup.
    mode_flags={},
    layout="paired",
    memory_model=AssemblyMemoryModel(
        # Published guidance, not measured on this hardware -- the same caveat
        # FLYE_SPEC and ABYSS_SPEC both carry. SPAdes holds more per genome
        # base than ABySS because it is not a Bloom-filter assembler: its
        # graph is held outright rather than in a bounded filter.
        bytes_per_genome_base=90.0,
        bytes_per_read_base=0.6,
        fixed_overhead_mb=4096,
    ),
    # metaSPAdes, where genome size stops being a meaningful input -- the same
    # problem FLYE_SPEC.meta_memory_model solves, and solved the same way:
    # against read volume instead. Without this, a community assembly (which
    # normally has no genome size) estimates to None and is guarded by nothing.
    #
    # Measured on this hardware rather than taken from published guidance,
    # which the single-genome model above could not be: two metaSPAdes 4.3.0
    # runs over a 5-organism synthetic community with a 30x abundance spread,
    # 4 threads, peak RSS via GNU time --
    #
    #     52.8 Mbp of reads ->  776 MB
    #    158.4 Mbp of reads -> 2154 MB
    #
    # a near-linear 13.7 bytes per read base on an ~87 MB intercept. Rounded
    # up to 16 and given a 1 GB floor, on #727's stated bias: wrong in the low
    # direction is an OOM (which also poisons the timing models), wrong in the
    # high direction is a warning someone can override.
    meta_memory_model=AssemblyMemoryModel(
        bytes_per_genome_base=0.0,
        bytes_per_read_base=16.0,
        fixed_overhead_mb=1024,
    ),
    outputs=(
        # Filenames confirmed against a real 4.3.0 run of the bundled
        # test dataset, not read from documentation. Confirmed again under
        # `--meta` for #731: contigs.fasta and assembly_graph_with_scaffolds.gfa
        # are both still produced, so meta mode needs no separate outputs.
        #
        # No scaffolds.fasta entry, deliberately: `harvest()` returns
        # `dict[OutputKind, Path]` keyed by kind, and there is no separate
        # "scaffolds" OutputKind (see assemblers.OutputKind) -- so a
        # scaffolds.fasta Output here would share OutputKind.CONTIGS with
        # contigs.fasta and simply lose the collision, silently absent from
        # every real run's harvested outputs despite being declared. The
        # minimal, honest fix is not declaring it: contigs.fasta (required)
        # and the graph below are both harvestable without collision.
        Output(
            kind=OutputKind.GRAPH,
            filename="assembly_graph_with_scaffolds.gfa",
        ),
        Output(kind=OutputKind.CONTIGS, filename="contigs.fasta", required=True),
    ),
    fields=(
        *_SHARED_FIELDS,
        ParamField(
            key="mode",
            label="Running mode",
            kind="select",
            default="isolate",
            group="biology",
            help=(
                "Isolate is recommended for high-coverage bacterial isolates "
                "and is the usual choice. Careful reduces mismatches and short "
                "indels but is only for small genomes. Metagenome is for a "
                "mixed-community sample rather than a single organism, and "
                "requires paired reads. No two of these can be combined."
            ),
            choices=(
                Choice(value="isolate", label="Isolate (high-coverage, recommended)"),
                Choice(value="careful", label="Careful (small genomes only)"),
                # metaSPAdes. A mode rather than a checkbox, unlike Flye's
                # `meta` field above: SPAdes rejects `--meta --isolate` and
                # `--meta --careful` outright, so the exclusivity this select
                # already enforces is exactly the exclusivity the tool wants.
                # Flye's `--meta` is orthogonal to its accuracy mode, which is
                # why the two are shaped differently on purpose.
                Choice(value="meta", label="Metagenome (paired reads only)"),
                Choice(value="standard", label="Standard"),
            ),
        ),
    ),
    unavailable_reason="SPAdes is not installed in this build.",
)

ABYSS_SPEC = AssemblerSpec(
    assembler=Assembler.ABYSS,
    tool=tools.abyss,
    # Empty by construction, not by omission: ABySS has no read-accuracy mode
    # flag. `spec_for_chemistry` routes SHORT here explicitly rather than by
    # looking a chemistry up in this map, so an empty map is correct.
    mode_flags={},
    layout="paired",
    memory_model=AssemblyMemoryModel(
        # A de Bruijn graph's peak is dominated by distinct k-mers, so the
        # genome term is small next to Flye's 40 and the read term carries the
        # weight. Published guidance, not measured on this hardware -- the same
        # caveat FLYE_SPEC's model carries.
        bytes_per_genome_base=15.0,
        bytes_per_read_base=0.5,
        fixed_overhead_mb=1024,
    ),
    outputs=(
        # Symlinks over numbered stage files (`asm-scaffolds.fa` -> `asm-8.fa`).
        # `harvest` resolves them; storing the link itself would dangle once the
        # workdir is reaped.
        Output(
            kind=OutputKind.CONTIGS,
            filename=f"{ASSEMBLY_NAME_PREFIX}-scaffolds.fa",
            required=True,
        ),
        Output(
            kind=OutputKind.GRAPH,
            filename=f"{ASSEMBLY_NAME_PREFIX}-scaffolds.dot",
        ),
        # ABySS computes N50 and friends itself, which Flye does not.
        Output(
            kind=OutputKind.INFO_TABLE,
            filename=f"{ASSEMBLY_NAME_PREFIX}-stats.tab",
        ),
    ),
    fields=(
        *_SHARED_FIELDS,
        ParamField(
            key="k",
            label="k-mer length",
            kind="int",
            default=51,
            min=16,
            max=127,
            group="biology",
            help=(
                "The single parameter that most changes a short-read assembly. "
                "51 suits 100-150 bp Illumina reads at typical coverage. Lower "
                "it for shorter reads or thin coverage; raise it for long, deep, "
                "high-quality reads."
            ),
        ),
    ),
)


MEGAHIT_SPEC = AssemblerSpec(
    assembler=Assembler.MEGAHIT,
    tool=tools.megahit,
    # Empty by construction, like ABySS and SPAdes: MEGAHIT has no
    # read-accuracy mode flag, and `spec_for_chemistry` does not reach it by
    # chemistry lookup.
    mode_flags={},
    layout="paired",
    # One model, not a pair. Every other assembler here has a single-genome
    # model and (if it has a meta mode) a second one for communities; MEGAHIT
    # is *only* ever a metagenome assembler, so its one model is the meta one
    # and `meta_memory_model` stays None. `bytes_per_genome_base` is 0.0 for
    # the reason both existing meta models give: a community has no single
    # genome size, so there is no number to multiply.
    #
    # Measured on this hardware rather than taken from published guidance, and
    # deliberately NOT copied from SPADES_SPEC -- bounded memory is the entire
    # reason this tool is here, so inheriting SPAdes' 90 (or its meta model's
    # 16) would model away the thing being modelled. MEGAHIT 1.2.9,
    # linux-aarch64, 4 threads, peak RSS via GNU time, over synthetic
    # communities of 5 to 150 genomes on 2026-08-21:
    #
    #      2.8 Mbp of reads ->  268 MB
    #     11.4 Mbp of reads ->  284 MB
    #     34.1 Mbp of reads ->  325 MB
    #     85.3 Mbp of reads ->  418 MB
    #
    # A near-perfect line: 1.91 bytes per read base on a 263 MB intercept.
    # Rounded up to 2.5 with a 384 MB floor, so every measured point sits
    # under the estimate rather than on it -- #727's stated bias, that wrong
    # in the low direction is an OOM (which also poisons the timing models)
    # while wrong in the high direction is a warning someone can override.
    #
    # Worth knowing before re-measuring: at a FIXED community size, peak RSS
    # is flat against read depth -- 1.4 Mbp and 80.7 Mbp of reads over the
    # same five genomes both peaked at ~272 MB -- and flat against `-m`
    # (500 MB, 2 GB, 8 GB and the default all gave ~266 MB on one community).
    # MEGAHIT takes what the graph needs up to the budget rather than
    # pre-allocating it. So what this coefficient really tracks is community
    # *complexity*, for which read volume is the only proxy available at
    # launch. A re-measurement that varies depth alone will see a flat line
    # and wrongly conclude the coefficient should be zero.
    memory_model=AssemblyMemoryModel(
        bytes_per_genome_base=0.0,
        bytes_per_read_base=2.5,
        fixed_overhead_mb=384,
    ),
    outputs=(
        # `final.contigs.fa` is what the wrapper's `merge_final()` writes --
        # read off v1.2.9's source rather than the docs, and confirmed against
        # a real run by install-megahit.sh's smoke assembly.
        #
        # No GRAPH output: MEGAHIT writes intermediate per-k contigs under
        # `intermediate_contigs/`, not an assembly graph anyone opens in
        # Bandage. Declaring one that never appears would be a permanently
        # absent optional output.
        Output(kind=OutputKind.CONTIGS, filename="final.contigs.fa", required=True),
    ),
    fields=(
        # No `mode` field, deliberately: MEGAHIT has no isolate/meta switch to
        # offer. `modes_for()` therefore returns an empty frozenset for it,
        # which is correct rather than missing -- there are no modes.
        #
        # `genome_size` comes with _SHARED_FIELDS and stays inert here: the
        # memory model has no genome term to feed, and the field's own help
        # text already says it is only used for the estimate. Dropping it
        # would make this the one assembler whose dialog lacks a field the
        # others share, to remove something that costs nothing.
        *_SHARED_FIELDS,
        ParamField(
            key="min_contig_len",
            label="Minimum contig length",
            kind="int",
            default=200,
            min=1,
            max=10000,
            group="biology",
            help=(
                "Contigs shorter than this are not written out. MEGAHIT's own "
                "default is 200. Raising it trims the short fragments a "
                "community assembly always produces, at the risk of dropping "
                "genuinely short sequences."
            ),
        ),
    ),
    unavailable_reason="MEGAHIT is not installed in this build.",
)

SPECS: dict[Assembler, AssemblerSpec] = {
    Assembler.FLYE: FLYE_SPEC,
    Assembler.HIFIASM: HIFIASM_SPEC,
    Assembler.SPADES: SPADES_SPEC,
    Assembler.ABYSS: ABYSS_SPEC,
    Assembler.MEGAHIT: MEGAHIT_SPEC,
}


def spec_for(assembler: Assembler) -> AssemblerSpec:
    """The spec for an assembler.

    Patch *this* to simulate an assembler being absent. Patching
    `tools.flye` does not work: `FLYE_SPEC` is a frozen dataclass that
    captured the function object at import time, so the module attribute is no
    longer what `spec.tool` refers to. That is the same seam
    `aligner_registry` documents, recorded here before someone writes the test
    that silently reads the host machine instead of the patch.
    """
    return SPECS[assembler]


def modes_for(assembler: Assembler) -> frozenset[str]:
    """Every input mode this assembler accepts, for validation.

    Read off the field declaration rather than the chemistry map: the map has
    one entry per chemistry and the dialog offers more modes than there are
    chemistries (nano-corr and pacbio-corr describe reads corrected by some
    other tool, which nothing here infers).
    """
    spec = SPECS[assembler]
    for field in spec.fields:
        if field.key == "mode":
            return frozenset(c.value for c in field.choices)
    return frozenset()


def spec_for_chemistry(chemistry: ReadChemistry | None) -> AssemblerSpec | None:
    """The assembler to use for these reads, or None if there is not one.

    ABySS for short reads, Flye for every long-read chemistry including HiFi
    (hifiasm is the better HiFi assembler and is the one this returns once it
    is installed). This function remains the single place that changes.

    None only for unknown chemistry now -- a missing fact the user can supply
    by running QC. Short reads used to land here too, as a *different* refusal
    naming a missing tool; that branch is gone because the tool is installed.
    """
    if chemistry is None or chemistry is ReadChemistry.UNKNOWN:
        return None
    if chemistry is ReadChemistry.SHORT:
        return SPECS[Assembler.ABYSS]
    spec = SPECS[Assembler.FLYE]
    if chemistry in spec.mode_flags:
        return spec
    return None


def mode_for_chemistry(spec: AssemblerSpec, chemistry: ReadChemistry) -> str:
    return spec.mode_flags[chemistry]


def schema_for(assembler: Assembler) -> dict:
    """The dialog's parameter form, as JSON.

    Built with `asdict` on the fields rather than named one by one, the same
    reasoning `tool_with_meta` follows: a field added to `ParamField` reaches
    the dialog without a second edit here.
    """
    spec = SPECS[assembler]
    return {
        "assembler": spec.assembler.value,
        "available": spec.available(),
        "unavailable_reason": spec.unavailable_reason,
        "layout": spec.layout,
        "fields": [asdict(f) for f in spec.fields],
    }
