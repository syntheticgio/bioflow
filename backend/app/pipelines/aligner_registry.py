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
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.pipelines import align_params, aligner_preset_ids, aligners, tools
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
    was already organized by hand. `filters` will be the annotation-export
    node's group, meant to render always-visible like biology once the form
    supports it.
    """

    key: str
    label: str
    kind: Literal["int", "float", "bool", "select", "text"]
    default: Any
    help: str
    # "filters" is the annotation-export node's group -- fields that select
    # which features to export, which are neither biology knobs nor
    # performance tuning. ParamForm still only renders biology/performance
    # today; rendering "filters" is a separate change.
    group: Literal["biology", "performance", "filters"] = "biology"
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
    # True when this aligner's index works against a subset of the reference
    # FASTA. STAR's index is tied to the exact reference; Winnowmap requires
    # whole-reference meryl preprocessing.
    chunking_supported: bool = True
    # True when this aligner's index builder reads a gzip-compressed reference
    # directly. False means `build_index` must decompress first -- see
    # `align_handlers._ensure_uncompressed`. Declared per aligner rather than
    # special-cased at the call site because the failure is silent-by-default:
    # a builder that cannot read gzip dies partway through a long build with a
    # tool-specific error, and a newly added aligner inherits whatever the
    # call site happened to assume. Measured against the binaries this image
    # ships, on both plain gzip and bgzip: bowtie2-build, bwa-mem2 index and
    # minimap2 -d accept both; hisat2-build and STAR reject both.
    builder_accepts_gzip: bool = True
    # Named bundles of parameter values, keyed by preset id. When present, the
    # dialog offers a preset selector instead of (or in addition to) individual
    # biology fields. The value is a dict of param key -> value that gets
    # applied on top of defaults. An "advanced" preset name is reserved to mean
    # "show all individual fields" -- see schema_for().
    presets: dict[str, dict[str, Any]] = field(default_factory=dict)


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
        # ~3.2 bytes/base: about 10 GB for a 3.1 Gb human genome, the figure
        # bwa-mem2's README gives for its resident index since the October
        # 2020 change to a single FM-index with a compressed suffix array.
        # (The ~6 GB this comment claimed before was the pre-2020 number.)
        #
        # The build multiplier is by far the largest here, and is not a guess:
        # the README states indexing "Requires 28N GB memory where N is the
        # size of the reference sequence" -- roughly 28 bytes/base, which
        # 3.2 * 8.75 reproduces. Modelled at 4 bytes/base effective before,
        # this under-reserved by 7x, and the queue governor duly admitted an
        # 897 Mbp build into 8 GB and had it OOM-killed (#96, #100).
        memory_model=MemoryModel(
            index_bytes_per_ref_base=3.2,
            fixed_overhead_mb=512,
            bytes_per_thread_mb=256,
            index_build_multiplier=8.75,
        ),
        fields=(
            ParamField(
                key="min_score",
                label="Min alignment score (-T)",
                kind="int",
                default=30,
                group="biology",
                help=(
                    "Alignments with a score below this threshold are not output. Lower it to keep "
                    "weak alignments; raise it to filter noisy ones."
                ),
                min=0,
            ),
            ParamField(
                key="mark_split",
                label="Mark split hits as secondary (-M)",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Marks split alignments as secondary for Picard/GATK compatibility. Needed for "
                    "GATK Best Practices but wrong for most other workflows."
                ),
            ),
            ParamField(
                key="max_seed_occ",
                label="Max seed occurrences (-c)",
                kind="int",
                default=500,
                group="biology",
                help=(
                    "Maximum number of occurrences of a seed before it is discarded. Raise for "
                    "repeat-heavy genomes (e.g. 2000 for wheat); lower to save compute if only "
                    "unique regions matter."
                ),
                min=1,
            ),
            ParamField(
                key="reseed_factor",
                label="Re-seeding trigger factor (-r)",
                kind="int",
                default=1.5,
                group="biology",
                help=(
                    "Raise to 2-3 to reduce redundant seed extensions in repeat zones. Lower for "
                    "faster seeding on simple genomes."
                ),
                min=1,
            ),
            ParamField(
                key="all_alignments",
                label="Output all alignments for unpaired reads (-a)",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Outputs all alignments for unpaired reads instead of just the best. Useful "
                    "for transposon/repeat-copy-number analysis."
                ),
            ),
            ParamField(
                key="max_mate_rescue",
                label="Max mate-rescue attempts (-m)",
                kind="int",
                default=100,
                group="biology",
                help=(
                    "Number of attempts to rescue mates that don't align near each other. Lower to "
                    "20-50 to avoid slowdowns in repetitive genomes."
                ),
                min=0,
            ),
            ParamField(
                key="soft_clip_supp",
                label="Soft-clip supplementary alignments (-Y)",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Soft-clips supplementary alignments instead of hard-clipping. Needed by "
                    "structural variant callers (Manta, LUMPY, Delly)."
                ),
            ),
            ParamField(
                key="clip_penalty",
                label="Clipping penalty (-L)",
                kind="text",
                default="5,5",
                group="biology",
                help=(
                    "Comma-separated pair: clipping penalty for 5' and 3' ends. Lower to 2,2-3,3 "
                    "for RNA-seq or heavy structural rearrangements."
                ),
            ),
            ParamField(
                key="multimap_xa",
                label="Multi-mapping XA tag limits (-h)",
                kind="text",
                default="5,200",
                group="biology",
                help=(
                    "Comma-separated: max alignments to output as XA tags, max alignments to "
                    "consider. Raise for decoy/pseudogene/contaminant tracking."
                ),
            ),
            ParamField(
                key="batch_size",
                label="Fixed read batch size (-K)",
                kind="int",
                default=0,
                group="performance",
                help=(
                    "Set a fixed batch size (e.g. 100000000) for deterministic, reproducible runs "
                    "regardless of thread count. 0 means bwa-mem2's default."
                ),
                min=0,
            ),
            *_SHARED_FIELDS,
        ),
        presets={
            "bacteria": {
                "label": "Bacteria / Virus / Yeast",
                "description": "Minimal tuning: small, compact genomes with few repeats.",
                "values": {
                    "mark_split": False,
                    "min_score": 30,
                    "max_seed_occ": 500,
                    "reseed_factor": 1.5,
                    "all_alignments": False,
                    "max_mate_rescue": 100,
                    "soft_clip_supp": False,
                    "clip_penalty": "5,5",
                    "multimap_xa": "5,200",
                    "batch_size": 0,
                },
            },
            "large_repetitive": {
                "label": "Large / Repetitive (Plants, etc.)",
                "description": (
                    "Adjusted seeding for polyploid, repeat-heavy genomes (wheat, maize, barley, "
                    "conifers)."
                ),
                "values": {
                    "mark_split": False,
                    "min_score": 30,
                    "max_seed_occ": 2000,
                    "reseed_factor": 3,
                    "all_alignments": True,
                    "max_mate_rescue": 50,
                    "soft_clip_supp": False,
                    "clip_penalty": "5,5",
                    "multimap_xa": "5,200",
                    "batch_size": 0,
                },
            },
            "eukaryote": {
                "label": "Human / other Eukaryote",
                "description": (
                    "Standard resequencing: GATK Best Practices compatible defaults for human, "
                    "mouse, and similar genomes."
                ),
                "values": {
                    "mark_split": True,
                    "min_score": 30,
                    "max_seed_occ": 500,
                    "reseed_factor": 1.5,
                    "all_alignments": False,
                    "max_mate_rescue": 100,
                    "soft_clip_supp": True,
                    "clip_penalty": "5,5",
                    "multimap_xa": "5,200",
                    "batch_size": 100000000,
                },
            },
        },
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
            ParamField(
                key="kmer_size",
                label="K-mer size (-k)",
                kind="int",
                default=None,
                min=1,
                max=28,
                group="biology",
                help=(
                    "Leave blank to keep the selected preset's seed size. "
                    "Smaller k-mers increase sensitivity; larger ones reduce "
                    "spurious seeds."
                ),
            ),
            ParamField(
                key="window_size",
                label="Window size (-w)",
                kind="int",
                default=None,
                min=1,
                max=255,
                group="biology",
                help=(
                    "Leave blank to keep the selected preset's minimizer "
                    "density. Smaller windows sample more seeds; larger "
                    "windows are faster but less sensitive."
                ),
            ),
            ParamField(
                key="min_chain_score",
                label="Minimum chain score (-m)",
                kind="int",
                default=None,
                min=1,
                group="biology",
                help=(
                    "Leave blank to keep the selected preset's weak-chain "
                    "filter. Raise it to drop marginal hits; lower it to "
                    "keep weaker alignments."
                ),
            ),
            ParamField(
                key="max_gap",
                label="Maximum minimizer gap (-g)",
                kind="int",
                default=None,
                min=1,
                group="biology",
                help=(
                    "Leave blank to keep the selected preset's chaining gap "
                    "limit. Larger values bridge bigger gaps; smaller values "
                    "make chaining stricter."
                ),
            ),
            ParamField(
                key="secondary_ratio",
                label="Secondary alignment ratio (-p)",
                kind="float",
                default=None,
                min=0,
                max=1,
                group="biology",
                help=(
                    "Leave blank to keep the selected preset's threshold for "
                    "reporting secondary hits. Lower values report more "
                    "secondary alignments when they are enabled."
                ),
            ),
            ParamField(
                key="max_secondary",
                label="Maximum secondary alignments (-N)",
                kind="int",
                default=None,
                min=1,
                group="biology",
                help=(
                    "Leave blank to keep the selected preset's cap on "
                    "secondary hits. This matters only when secondary "
                    "alignments are enabled."
                ),
            ),
            ParamField(
                key="secondary_mode",
                label="Secondary alignment mode (--secondary)",
                kind="select",
                default=align_params.MINIMAP2_SECONDARY_MODE_DEFAULT,
                group="performance",
                help=(
                    "Tool default leaves Minimap2's preset behavior "
                    "unchanged. Enable or disable secondary alignments "
                    "explicitly when downstream tools need it."
                ),
                choices=(
                    Choice(
                        align_params.MINIMAP2_SECONDARY_MODE_DEFAULT,
                        "Tool default",
                    ),
                    Choice("enabled", "Enabled"),
                    Choice("disabled", "Disabled"),
                ),
            ),
            ParamField(
                key="batch_size",
                label="Batch size (-K)",
                kind="int",
                default=None,
                min=1,
                group="performance",
                help=(
                    "Leave blank to keep the selected preset's reads-per-"
                    "batch setting. Larger batches can improve throughput at "
                    "the cost of more memory."
                ),
            ),
            ParamField(
                key="soft_clip_supplementary",
                label="Soft-clip supplementary alignments (-Y)",
                kind="bool",
                default=None,
                group="performance",
                help=(
                    "Unchecked leaves the selected preset unchanged. Check to "
                    "emit soft-clipped supplementary alignments instead of "
                    "hard-clipped ones."
                ),
            ),
            ParamField(
                key="cs_mode",
                label="cs tag output (--cs)",
                kind="select",
                default=align_params.MINIMAP2_CS_MODE_DEFAULT,
                group="performance",
                help=(
                    "None leaves cs tags off. Short and long emit the Minimap2 "
                    "difference tag in its compact or expanded form."
                ),
                choices=(
                    Choice(align_params.MINIMAP2_CS_MODE_DEFAULT, "None"),
                    Choice("short", "Short"),
                    Choice("long", "Long"),
                ),
            ),
            ParamField(
                key="emit_md",
                label="MD tag output (--MD)",
                kind="bool",
                default=None,
                group="performance",
                help=(
                    "Unchecked leaves the selected preset unchanged. Check to "
                    "emit MD tags for downstream tools that expect them."
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
                key="minins",
                label="Minimum insert size",
                kind="int",
                default=0,
                min=0,
                group="biology",
                help=(
                    "The lower bound of bowtie2's -I/-X proper-pair range. "
                    "Pairs implying a shorter fragment are not counted as "
                    "properly paired."
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
                    "The upper bound of bowtie2's -I/-X proper-pair range. "
                    "Pairs implying a longer fragment are not counted as "
                    "properly paired."
                ),
            ),
            ParamField(
                key="orientation",
                label="Expected pair orientation",
                kind="select",
                default="FR",
                group="biology",
                help=(
                    "The expected orientation for a proper pair. A wrong "
                    "value changes which pairs count as concordant rather "
                    "than failing."
                ),
                choices=[
                    Choice(
                        align_params.BOWTIE2_ORIENTATIONS[0],
                        "FR (paired-end)",
                    ),
                    Choice(
                        align_params.BOWTIE2_ORIENTATIONS[1],
                        "RF (mate-pair)",
                    ),
                    Choice(
                        align_params.BOWTIE2_ORIENTATIONS[2],
                        "FF",
                    ),
                ],
            ),
            ParamField(
                key="dovetail",
                label="Allow dovetailing mates",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Treat pairs whose alignments extend past each other as "
                    "concordant instead of excluding them."
                ),
            ),
            ParamField(
                key="no_contain",
                label="Reject contained mates",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Exclude pairs where one mate's alignment is contained "
                    "entirely within the other."
                ),
            ),
            ParamField(
                key="no_overlap",
                label="Reject overlapping mates",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Exclude pairs whose alignments overlap instead of "
                    "counting them as concordant."
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
            ParamField(
                key="report_all",
                label="Report all alignments (-a)",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Reports every alignment, which can greatly increase "
                    "output size, and cannot be combined with report_k."
                ),
            ),
            *_SHARED_FIELDS,
        ),
        presets={
            aligner_preset_ids.BOWTIE2_STANDARD_SHORT_READ: {
                "label": "Standard short-read DNA",
                "description": (
                    "Conservative paired-end defaults; check insert sizes "
                    "against the library."
                ),
                "values": {
                    "sensitivity": "--sensitive",
                    "local": False,
                    "minins": 0,
                    "maxins": 500,
                    "orientation": "FR",
                    "no_mixed": False,
                    "no_discordant": False,
                    "dovetail": False,
                    "no_contain": False,
                    "no_overlap": False,
                    "report_k": 0,
                    "report_all": False,
                },
            },
            aligner_preset_ids.BOWTIE2_LONG_INSERT: {
                "label": "Long-insert paired-end",
                "description": (
                    "Broad starting range for long-insert libraries; check "
                    "the library distribution."
                ),
                "values": {
                    "sensitivity": "--sensitive",
                    "local": False,
                    "minins": 500,
                    "maxins": 20000,
                    "orientation": "FR",
                    "no_mixed": False,
                    "no_discordant": False,
                    "dovetail": False,
                    "no_contain": False,
                    "no_overlap": False,
                    "report_k": 0,
                    "report_all": False,
                },
            },
            aligner_preset_ids.BOWTIE2_MATE_PAIR: {
                "label": "Mate-pair",
                "description": (
                    "RF mate-pair starting values; confirm orientation and "
                    "insert range for the protocol."
                ),
                "values": {
                    "sensitivity": "--sensitive",
                    "local": False,
                    "minins": 500,
                    "maxins": 20000,
                    "orientation": "RF",
                    "no_mixed": False,
                    "no_discordant": False,
                    "dovetail": False,
                    "no_contain": False,
                    "no_overlap": False,
                    "report_k": 0,
                    "report_all": False,
                },
            },
            aligner_preset_ids.BOWTIE2_ADAPTER_PARTIAL_REFERENCE: {
                "label": "Adapter-contaminated / partial reference",
                "description": (
                    "Uses local alignment to tolerate unaligned read ends or "
                    "a partial reference. The insert-size range is a starting "
                    "point; verify it against the library."
                ),
                "values": {
                    "sensitivity": "--sensitive",
                    "local": True,
                    "minins": 0,
                    "maxins": 500,
                    "orientation": "FR",
                    "no_mixed": False,
                    "no_discordant": False,
                    "dovetail": False,
                    "no_contain": False,
                    "no_overlap": False,
                    "report_k": 0,
                    "report_all": False,
                },
            },
            aligner_preset_ids.BOWTIE2_STRUCTURAL_VARIANT: {
                "label": "Structural-variant discovery",
                "description": (
                    "Preserves discordant and mixed evidence and allows "
                    "dovetailing mates. The insert-size range is a starting "
                    "point; verify it against the library."
                ),
                "values": {
                    "sensitivity": "--sensitive",
                    "local": False,
                    "minins": 0,
                    "maxins": 500,
                    "orientation": "FR",
                    "no_mixed": False,
                    "no_discordant": False,
                    "dovetail": True,
                    "no_contain": False,
                    "no_overlap": False,
                    "report_k": 0,
                    "report_all": False,
                },
            },
            aligner_preset_ids.BOWTIE2_REPEAT_MULTIMAPPING: {
                "label": "Repeat / multi-mapping analysis",
                "description": (
                    "Reports up to 10 alignments per read; output size can "
                    "grow substantially. The insert-size range is a starting "
                    "point; verify it against the library."
                ),
                "values": {
                    "sensitivity": "--sensitive",
                    "local": False,
                    "minins": 0,
                    "maxins": 500,
                    "orientation": "FR",
                    "no_mixed": False,
                    "no_discordant": False,
                    "dovetail": False,
                    "no_contain": False,
                    "no_overlap": False,
                    "report_k": 10,
                    "report_all": False,
                },
            },
        },
    ),
    Aligner.HISAT2: AlignerSpec(
        aligner=Aligner.HISAT2,
        tool=tools.hisat2,
        index=aligners.layout_for(Aligner.HISAT2),
        params_class=align_params.Hisat2Params,
        builder_tool=tools.hisat2_build,
        # hisat2-build exits 1 on a compressed reference, deleting the partial
        # .ht2 files it had already written. Unlike bowtie2-build, which shares
        # its lineage and does accept gzip.
        builder_accepts_gzip=False,
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
        chunking_supported=False,
        # genomeGenerate reads FASTA and GTF as plain text; a gzip reference
        # reaches it unusable and it fails with an "is not fasta" error.
        builder_accepts_gzip=False,
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
    Aligner.WINNOWMAP: AlignerSpec(
        aligner=Aligner.WINNOWMAP,
        tool=tools.winnowmap,
        index=aligners.layout_for(Aligner.WINNOWMAP),
        params_class=align_params.WinnowmapParams,
        chunking_supported=False,
        # builder_tool is meryl, not winnowmap's own binary -- the same
        # separate-builder shape as bowtie2/HISAT2, except what meryl
        # produces is consumed via -W rather than discovered by suffix.
        builder_tool=tools.meryl,
        # meryl's peak is the memory-hungry phase, not winnowmap's own
        # alignment, so this model describes the *index build* cost against
        # the assembly rather than a resident-alignment-index cost the way
        # every other aligner's does. No measured run exists yet; this
        # coefficient is a placeholder until one is taken against a real
        # assembly (see the design doc's implementation-order note) and
        # should not be trusted for a sizing decision before then.
        memory_model=MemoryModel(
            index_bytes_per_ref_base=2.0,
            fixed_overhead_mb=512,
            bytes_per_thread_mb=512,
            index_build_multiplier=2.0,
        ),
        fields=(
            ParamField(
                key="preset",
                label="Read type",
                kind="select",
                default="map-pb",
                group="biology",
                help=(
                    "winnowmap has no short-read mode -- it exists to "
                    "cross-check minimap2 on long reads for GCI continuity "
                    "scoring."
                ),
                choices=(
                    Choice("map-ont", "Oxford Nanopore"),
                    Choice("map-pb", "PacBio (CLR)"),
                    Choice("map-hifi", "PacBio (HiFi/CCS)"),
                ),
            ),
            ParamField(
                key="k",
                label="Meryl k-mer size",
                kind="int",
                default=15,
                min=1,
                max=28,
                group="performance",
                help=(
                    "Size of the k-mers meryl counts to find repetitive "
                    "regions. GCI's own example uses 15; winnowmap refuses "
                    "anything above 28."
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

    When the spec has presets, returns them alongside the fields. The frontend
    uses presets to offer a preset selector; selecting "advanced" (a reserved
    name) shows all individual fields instead.
    """
    spec = spec_for(aligner)
    result: dict = {
        "aligner": aligner.value,
        "fields": [asdict(f) for f in spec.fields],
    }
    if spec.presets:
        result["presets"] = {
            k: {
                "id": k,
                "label": v["label"],
                "description": v["description"],
                "values": v["values"],
            }
            for k, v in spec.presets.items()
        }
    return result
