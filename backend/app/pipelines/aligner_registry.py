"""One spec per aligner: the single place an aligner is declared.

Before this existed, adding an aligner meant five coordinated edits --
index shape in `aligners`, command construction and defaults in
`align_runner`, tool resolution in `align_handlers`, probe and description in
`tools`, and a hand-written block in the dialog. Nothing said what an aligner
*was*, so the answer was "whatever those five files agree on", and they only
agree until someone edits four of them.

The field metadata here is also what the dialog renders its parameter form
from, so a knob is added in one place rather than two. That is the same
reasoning TOOL_META already follows: a second copy of tool descriptions in
the frontend is the copy nobody updates.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.pipelines import align_params, aligners, tools
from app.pipelines.aligners import Aligner, IndexLayout


@dataclass(frozen=True)
class Choice:
    value: str
    label: str


@dataclass(frozen=True)
class ParamField:
    """One input in the generated parameter form.

    `group` is what keeps a generated form from becoming an undifferentiated
    pile of inputs: biology fields render in the dialog body, performance
    fields under the advanced disclosure -- which is roughly how AlignDialog
    was already organized by hand.
    """

    key: str
    label: str
    kind: Literal["int", "bool", "select", "text"]
    default: Any
    help: str
    group: Literal["biology", "performance"] = "biology"
    min: int | None = None
    max: int | None = None
    choices: tuple[Choice, ...] = ()


@dataclass(frozen=True)
class MemoryModel:
    """Coefficients for estimating a run's peak memory.

    Heuristics from published tool documentation, not measurements on this
    hardware. They will be roughly right and occasionally wrong, which is why
    the estimator's block band is set at genuinely-impossible rather than
    merely-tight: a bad coefficient should cost a spurious warning, never a
    blocked run that would have worked.
    """

    # The dominant term for every aligner: index size scales with the
    # reference, and the whole index is resident during alignment.
    index_bytes_per_ref_base: float
    fixed_overhead_mb: int
    bytes_per_thread_mb: int
    # Building an index costs more than loading one. STAR's is the extreme
    # case (roughly 10x), which is the reason this field exists now.
    index_build_multiplier: float = 1.0


@dataclass(frozen=True)
class AlignerSpec:
    aligner: Aligner
    tool: Callable[[], tools.Tool]
    index: IndexLayout
    params_class: type[align_params.BaseAlignParams]
    memory_model: MemoryModel
    fields: tuple[ParamField, ...] = ()
    # The probe for the separate binary that builds this aligner's index, when
    # there is one -- bowtie2-build, hisat2-build. None for bwa-mem2 and
    # minimap2, which index through the same tool that aligns. This is the
    # single source of truth `align_handlers.build_index` dispatches through;
    # `index.builder` (on IndexLayout) is only the binary's bare name, kept
    # for cross-checking and error messages.
    builder_tool: Callable[[], tools.Tool] | None = None


# Threads and sort memory are on every aligner, so they are declared once and
# spliced into each spec rather than repeated four times.
_SHARED_FIELDS: tuple[ParamField, ...] = (
    ParamField(
        key="threads",
        label="Threads",
        kind="int",
        default=4,
        min=1,
        max=64,
        group="performance",
        help="More threads finish sooner but compete with other work.",
    ),
    ParamField(
        key="sort_memory_mb",
        label="Sort memory (MB per thread)",
        kind="int",
        default=1024,
        min=64,
        group="performance",
        help=(
            "Per thread, not total -- 8 threads at 1024 MB is 8 GB. samtools "
            "spills to disk when it runs out, which is slower."
        ),
    ),
    ParamField(
        key="mark_duplicates",
        label="Mark duplicates",
        kind="bool",
        default=False,
        group="biology",
        help=(
            "Standard for DNA-seq variant calling. Wrong for RNA-seq and "
            "amplicon data, where duplicates are expected."
        ),
    ),
)


REGISTRY: dict[Aligner, AlignerSpec] = {
    Aligner.BWA_MEM2: AlignerSpec(
        aligner=Aligner.BWA_MEM2,
        tool=tools.bwa_mem2,
        index=aligners.layout_for(Aligner.BWA_MEM2),
        params_class=align_params.Bwa2Params,
        # ~2 bytes/base: about 6 GB for a 3.1 Gb human genome, which matches
        # the figure bwa-mem2's own README gives for its index.
        memory_model=MemoryModel(
            index_bytes_per_ref_base=2.0,
            fixed_overhead_mb=512,
            bytes_per_thread_mb=256,
            index_build_multiplier=2.0,
        ),
        fields=_SHARED_FIELDS,
    ),
    Aligner.MINIMAP2: AlignerSpec(
        aligner=Aligner.MINIMAP2,
        tool=tools.minimap2,
        index=aligners.layout_for(Aligner.MINIMAP2),
        params_class=align_params.Minimap2Params,
        memory_model=MemoryModel(
            index_bytes_per_ref_base=1.5,
            fixed_overhead_mb=512,
            bytes_per_thread_mb=512,
            index_build_multiplier=1.5,
        ),
        fields=(
            ParamField(
                key="preset",
                label="Read type",
                kind="select",
                default="sr",
                group="biology",
                help="The wrong choice aligns long reads poorly rather than failing.",
                choices=(
                    Choice("sr", "Short read (Illumina)"),
                    Choice("map-ont", "Oxford Nanopore"),
                    Choice("map-pb", "PacBio (CLR)"),
                    Choice("map-hifi", "PacBio (HiFi/CCS)"),
                    Choice("lr:hq", "Oxford Nanopore (duplex / Q20+)"),
                ),
            ),
            *_SHARED_FIELDS,
        ),
    ),
    Aligner.BOWTIE2: AlignerSpec(
        aligner=Aligner.BOWTIE2,
        tool=tools.bowtie2,
        index=aligners.layout_for(Aligner.BOWTIE2),
        params_class=align_params.Bowtie2Params,
        builder_tool=tools.bowtie2_build,
        # ~1 byte/base: about 3.5 GB for human, matching the published size of
        # the prebuilt GRCh38 bowtie2 index.
        memory_model=MemoryModel(
            index_bytes_per_ref_base=1.0,
            fixed_overhead_mb=256,
            bytes_per_thread_mb=200,
            index_build_multiplier=3.0,
        ),
        fields=(
            ParamField(
                key="sensitivity",
                label="Sensitivity",
                kind="select",
                default="--sensitive",
                group="biology",
                help=(
                    "More sensitive settings find more alignments in divergent "
                    "or repetitive regions, and take proportionally longer."
                ),
                choices=(
                    Choice("--very-fast", "Very fast"),
                    Choice("--fast", "Fast"),
                    Choice("--sensitive", "Sensitive (default)"),
                    Choice("--very-sensitive", "Very sensitive"),
                ),
            ),
            ParamField(
                key="local",
                label="Local alignment (soft-clip read ends)",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "End-to-end requires the whole read to align. Local "
                    "soft-clips the ends, which suits reads with adapter "
                    "remnants or a partial reference."
                ),
            ),
            ParamField(
                key="maxins",
                label="Maximum insert size",
                kind="int",
                default=500,
                min=1,
                group="biology",
                help=(
                    "Pairs implying a longer fragment are not counted as "
                    "properly paired. Raise it for ChIP-seq or any library "
                    "with long fragments."
                ),
            ),
            ParamField(
                key="no_mixed",
                label="Suppress unpaired alignments for pairs",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "By default bowtie2 falls back to aligning each mate "
                    "alone when the pair does not align. This forbids that."
                ),
            ),
            ParamField(
                key="no_discordant",
                label="Suppress discordant alignments",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Discordant pairs align uniquely but not with the "
                    "expected orientation or spacing. Structural variant work "
                    "wants them; most other analyses do not."
                ),
            ),
            ParamField(
                key="report_k",
                label="Report up to N alignments (0 = best only)",
                kind="int",
                default=0,
                min=0,
                group="biology",
                help=(
                    "Reporting multiple alignments per read grows the BAM and "
                    "changes what downstream counting sees. Leave at 0 unless "
                    "a specific analysis needs multi-mapping reads."
                ),
            ),
            *_SHARED_FIELDS,
        ),
    ),
    Aligner.HISAT2: AlignerSpec(
        aligner=Aligner.HISAT2,
        tool=tools.hisat2,
        index=aligners.layout_for(Aligner.HISAT2),
        params_class=align_params.Hisat2Params,
        builder_tool=tools.hisat2_build,
        # HISAT2's graph FM index is notably compact -- about 4 GB for human
        # including the transcript index.
        memory_model=MemoryModel(
            index_bytes_per_ref_base=1.3,
            fixed_overhead_mb=256,
            bytes_per_thread_mb=200,
            index_build_multiplier=4.0,
        ),
        fields=(
            ParamField(
                key="rna_strandness",
                label="RNA strandness",
                kind="select",
                default="",
                group="biology",
                help=(
                    "A wrong value does not fail -- it reverses which strand "
                    "a read is attributed to, and only shows up as nonsense "
                    "in downstream counting. FR is the usual dUTP protocol."
                ),
                choices=(
                    Choice("", "Unstranded"),
                    Choice("FR", "FR (paired, forward)"),
                    Choice("RF", "RF (paired, reverse / dUTP)"),
                    Choice("F", "F (single, forward)"),
                    Choice("R", "R (single, reverse)"),
                ),
            ),
            ParamField(
                key="no_spliced_alignment",
                label="Disable spliced alignment (DNA input)",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "HISAT2 is splice-aware by default. Over genomic DNA that "
                    "invents junctions that are not there, so turn it off for "
                    "non-RNA input."
                ),
            ),
            ParamField(
                key="max_intronlen",
                label="Maximum intron length",
                kind="int",
                default=500000,
                min=1,
                group="biology",
                help=(
                    "Caps how far a spliced alignment may span. The default "
                    "suits mammalian genomes; compact genomes want far less."
                ),
            ),
            ParamField(
                key="dta",
                label="Format for transcript assembly",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Tailors the output for downstream transcript assemblers "
                    "such as StringTie. Harmless otherwise, but only useful "
                    "if that is the next step."
                ),
            ),
            ParamField(
                key="report_k",
                label="Report up to N alignments (0 = default)",
                kind="int",
                default=0,
                min=0,
                group="biology",
                help=(
                    "Reporting multiple alignments per read grows the BAM and "
                    "changes what downstream counting sees."
                ),
            ),
            *_SHARED_FIELDS,
        ),
    ),
    Aligner.STAR: AlignerSpec(
        aligner=Aligner.STAR,
        tool=tools.star,
        index=aligners.layout_for(Aligner.STAR),
        params_class=align_params.StarParams,
        # ~10 bytes/base: about 30 GB for a 3.1 Gb human genome, which is the
        # figure STAR's own manual gives for the RAM needed to align against
        # human. The index is an uncompressed suffix array held resident, so
        # unlike the FM-index aligners there is no compression to hide behind
        # -- this is the number that decides whether STAR can run at all here.
        #
        # The build multiplier is the modest one: genomeGenerate's peak is the
        # suffix-array sort, which is only somewhat above the finished index
        # rather than the 3-4x that bowtie2 and HISAT2 pay.
        memory_model=MemoryModel(
            index_bytes_per_ref_base=10.0,
            fixed_overhead_mb=1024,
            # Threads share the resident genome; only the per-thread buffers
            # scale, so this is small next to the index term.
            bytes_per_thread_mb=200,
            index_build_multiplier=1.2,
        ),
        fields=(
            ParamField(
                key="two_pass",
                label="Two-pass mapping",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Re-aligns everything against the junctions found in a "
                    "first pass, which recovers reads spanning novel "
                    "junctions. Roughly doubles the runtime."
                ),
            ),
            ParamField(
                key="out_filter_multimap_nmax",
                label="Maximum loci per read",
                kind="int",
                default=20,
                min=1,
                group="biology",
                help=(
                    "A read aligning to more places than this is written as "
                    "unmapped rather than as a multi-mapper. Raise it for "
                    "repeat-heavy work; lower it to keep only near-unique "
                    "alignments."
                ),
            ),
            ParamField(
                key="align_intron_max",
                label="Maximum intron length (0 = STAR's default)",
                kind="int",
                default=0,
                min=0,
                group="biology",
                help=(
                    "0 lets STAR derive its own ceiling of about 590 kb. Set "
                    "it to 1 to forbid spliced alignment altogether, which is "
                    "what genomic DNA input wants."
                ),
            ),
            ParamField(
                key="out_sam_unmapped",
                label="Keep unmapped reads in the BAM",
                kind="bool",
                default=True,
                group="biology",
                help=(
                    "On by default here, though STAR itself discards them. "
                    "With them dropped, the mapped-percentage on the "
                    "alignment report is computed over mapped reads only and "
                    "always reads 100%."
                ),
            ),
            *_SHARED_FIELDS,
        ),
    ),
}


def spec_for(aligner: Aligner) -> AlignerSpec:
    return REGISTRY[aligner]


def schema_for(aligner: Aligner) -> dict:
    """The field list, as JSON for the dialog.

    `asdict` on each field rather than a hand-written projection: a field
    added to ParamField should reach the form without a second edit here.
    """
    spec = spec_for(aligner)
    return {
        "aligner": aligner.value,
        "fields": [asdict(f) for f in spec.fields],
    }
