"""What a canvas node can be, and how it launches.

Every canvas capability reads from here: which nodes exist, which ports they
expose, which wires validate, and how a node actually runs.

Keyed by its own string rather than by RunKind, which was the obvious choice
and is wrong. Measured on main at design time: 26 launch_* functions exist
across services/, and only 9 create a PipelineRun -- every QC and stats
launcher enqueues jobs with no run record, and RunKind.REFERENCE_ASSEMBLY has
no launcher at all. Keying on RunKind would make most QC nodes unrepresentable,
which are precisely the nodes a user wants as continue_on_failure leaves.
`run_kind` is therefore an attribute of a spec, not its identity.

Per CLAUDE.md's rules for hand-maintained registries: this is the third
category, where the keys belong to a set outside any single enum, so full
derivation is impossible. The checkable invariant runs the other direction --
every launch_* is either here or in EXCLUDED_LAUNCHES, asserted in
tests/pipelines/test_node_types.py. Without that test, a new tool is silently
absent from the canvas.

Names are qualified as `module.function_name`, not bare function names.
`launch_download` exists three times over -- in ncbi_assembly_service,
uniprot_service, and sra_service, each with an unrelated signature -- so a
bare-string registry would silently collapse three different launchers into
one classifiable unit, defeating the exhaustiveness check it was meant to
serve. Qualifying by module is what keeps them distinguishable.

Status: every launch_* is classified. 26 launch_* functions exist across
services/; 12 create a PipelineRun (trim, align, variant_calling, quantify,
differential_expression, assembly, the three downloads, and -- since GitHub
issue #91 -- consensus, polish, and scaffold) and the rest do not.

Those last three are the one place a RunKind maps to more than one node type:
all are RunKind.REFERENCE_ASSEMBLY, so `run_tool` is what tells them apart
when workflow_derive turns a run back into a node. See NodeTypeSpec.run_tool.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.models.object import FormatKind, ObjectRole
from app.models.run import RunKind
from app.models.workflow import PortType
from app.pipelines.tool_choice import ALIGN_TOOL_CHOICE, ToolChoice
from app.services import (
    ncbi_assembly_service,
    pipeline_service,
    sra_service,
    uniprot_service,
)


@dataclass(frozen=True)
class PortSpec:
    name: str
    type: PortType
    required: bool = True
    # Whether this port accepts several incoming wires, collected into a list
    # for the launcher. Only the one-wire-per-port rule relaxes -- type
    # checking still applies to each wire independently, which is what keeps a
    # multi port from becoming an untyped one.
    #
    # `continuity_qc`'s hifi_bam/nano_bam and `differential_expression`'s
    # counts are the other two ports whose launchers genuinely take lists
    # today (both currently smuggle the set through `params`). They are left
    # scalar here deliberately: each needs its own decision about how the
    # per-sample design travels, and neither is what #94 asks for.
    multiple: bool = False


@dataclass(frozen=True)
class NodeTypeSpec:
    label: str
    # The launch_* function this adapts, as "module.function_name" -- see the
    # module docstring for why bare function names are not unique. Stored so
    # the exhaustiveness test can compare against what actually exists in
    # services/ rather than against a second hand-written list that would
    # drift.
    launch_name: str
    launch: Callable
    inputs: tuple[PortSpec, ...]
    outputs: tuple[PortSpec, ...]
    # None where the launcher creates no PipelineRun -- true of most of the 24.
    run_kind: RunKind | None = None
    # Which `PipelineRun.tool` value picks *this* spec when several share a
    # run_kind. Every kind but REFERENCE_ASSEMBLY maps to exactly one node
    # type, so this stays None for them; consensus/polish/scaffold are all
    # RunKind.REFERENCE_ASSEMBLY and are told apart by the tool their
    # launcher records (ivar, polypolish, ragtag). Uniqueness of
    # (run_kind, run_tool) is asserted in tests/pipelines/test_node_types.py --
    # a second spec claiming a kind without a tool would make
    # workflow_derive._node_type_for silently pick whichever came first.
    run_tool: str | None = None
    # Set when this node type is parameterized by a tool -- which aligner,
    # which caller. The chosen tool lives in `node.params[param_key]`, and the
    # port set follows from it, so ports are resolved per *node* via
    # `ports_for` rather than read off `spec.inputs` directly. Every read of
    # `.inputs`/`.outputs` outside this module should go through `ports_for`.
    tool_choice: "ToolChoice | None" = None


async def _launch_trim(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_trim(
        object_id=inputs["reads"],
        owner=owner,
        mate_object_id=inputs.get("mate"),
        params=params,
    )


async def _launch_align(*, inputs: dict, params: dict, owner: str):
    # `reads` is a multi port, so it arrives as a list. The launcher itself
    # takes one object_id: extra read files are passed through params for the
    # runner to concatenate, which is what "they all go in together" means --
    # one alignment over every chunk, not one run per file.
    reads = inputs["reads"]
    if isinstance(reads, list):
        primary, extra = reads[0], reads[1:]
    else:
        primary, extra = reads, []
    return await pipeline_service.launch_alignment(
        object_id=primary,
        reference_id=inputs["reference"],
        owner=owner,
        mate_object_id=inputs.get("mate"),
        params={**params, "extra_reads": [str(o) for o in extra]} if extra else params,
    )


async def _launch_qc(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_qc(
        object_id=inputs["reads"], owner=owner
    )


async def _launch_bam_stats(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_bam_stats(
        object_id=inputs["alignment"], owner=owner
    )


async def _launch_vcf_stats(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_vcf_stats(
        object_id=inputs["variants"], owner=owner
    )


async def _launch_variant_calling(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_variant_calling(
        bam_id=inputs["alignment"],
        owner=owner,
        reference_id=inputs.get("reference"),
        caller=params.get("caller"),
        params=params,
        install_optional=bool(params.get("install_optional")),
    )


async def _launch_annotation(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_annotation(
        object_id=inputs["variants"], owner=owner
    )


async def _launch_quantify(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_quantify(
        bam_id=inputs["alignment"],
        owner=owner,
        annotation_id=inputs.get("annotation"),
        params=params,
    )


async def _launch_differential_expression(*, inputs: dict, params: dict, owner: str):
    # DIFFERENTIAL_EXPRESSION is the one RunKind with N inputs (see the
    # RunKind.DIFFERENTIAL_EXPRESSION comment in app/models/run.py), which
    # this file's scalar PortSpec model cannot represent directly. "counts"
    # is wired as a single representative counts port; the real per-sample
    # design (which counts object maps to which condition) and the contrast
    # both travel through params, exactly as the dialog that drives this
    # launcher today already builds them.
    return await pipeline_service.launch_differential_expression(
        project_id=params["project_id"],
        owner=owner,
        design=params["design"],
        contrast=params["contrast"],
        threads=params.get("threads"),
    )


async def _launch_assembly(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_assembly(
        object_id=inputs["reads"], owner=owner, params=params
    )


async def _launch_lineage_download(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_lineage_download(
        lineage=params["lineage"], odb=params.get("odb"), owner=owner
    )


async def _launch_completeness(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_completeness(
        object_id=inputs["assembly"],
        owner=owner,
        lineage=params.get("lineage"),
        odb=params.get("odb"),
    )


async def _launch_consensus(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_consensus(
        bam_object_id=inputs["alignment"],
        owner=owner,
        primer_bed_object_id=inputs.get("primer_bed"),
        min_quality=params.get("min_quality"),
        min_freq=params.get("min_freq"),
        min_depth=params.get("min_depth"),
    )


async def _launch_polish(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_polish(
        draft_object_id=inputs["draft"],
        owner=owner,
        reads_object_id=inputs.get("reads"),
        mate_object_id=inputs.get("mate"),
    )


async def _launch_scaffold(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_scaffold(
        draft_object_id=inputs["draft"],
        owner=owner,
        reference_object_id=inputs.get("reference"),
        divergence=params.get("divergence"),
    )


async def _launch_misassembly_qc(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_misassembly_qc(
        draft_object_id=inputs["draft"],
        owner=owner,
        reference_object_id=inputs.get("reference"),
    )


async def _launch_assembly_error_qc(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_assembly_error_qc(
        object_id=inputs["assembly"],
        owner=owner,
        ngs_bam_id=inputs.get("ngs_bam"),
        sms_bam_id=inputs.get("sms_bam"),
        break_chimera=bool(params.get("break_chimera", False)),
    )


async def _launch_qv_qc(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_qv_qc(
        inputs["assembly"],
        owner=owner,
        read_object_id=inputs.get("reads"),
        k=params.get("k"),
    )


async def _launch_continuity_qc(*, inputs: dict, params: dict, owner: str):
    # hifi_bam_ids/nano_bam_ids are list-valued on the real launcher (GCI
    # cross-checks multiple aligners against one slot -- see
    # _group_gci_candidates_by_aligner in pipeline_service.py). This file's
    # PortSpec model is scalar-only, so the ports below wire a single BAM per
    # slot -- the common case the Actions card itself only ever fires for
    # (exactly one candidate per slot) -- and each is passed through as a
    # one-element list when present.
    hifi = inputs.get("hifi_bam")
    nano = inputs.get("nano_bam")
    return await pipeline_service.launch_continuity_qc(
        object_id=inputs["assembly"],
        owner=owner,
        hifi_bam_ids=[hifi] if hifi else None,
        nano_bam_ids=[nano] if nano else None,
        map_qual=params.get("map_qual"),
        plot=params.get("plot"),
    )


async def _launch_ncbi_assembly_download(*, inputs: dict, params: dict, owner: str):
    return await ncbi_assembly_service.launch_download(
        project_id=params["project_id"],
        accession=params["accession"],
        components=params.get("components", []),
        owner=owner,
    )


async def _launch_sra_download(*, inputs: dict, params: dict, owner: str):
    return await sra_service.launch_download(
        project_id=params["project_id"],
        run_accessions=params["run_accessions"],
        owner=owner,
        run_qc=bool(params.get("run_qc", True)),
    )


async def _launch_uniprot_download(*, inputs: dict, params: dict, owner: str):
    return await uniprot_service.launch_download(
        project_id=params["project_id"],
        proteome_id=params.get("proteome_id"),
        accessions=params.get("accessions", []),
        reviewed_only=bool(params.get("reviewed_only", False)),
        owner=owner,
        organism=params.get("organism"),
        protein_count=params.get("protein_count"),
    )


NODE_TYPES: dict[str, NodeTypeSpec] = {
    "trim": NodeTypeSpec(
        label="Trim reads",
        launch_name="pipeline_service.launch_trim",
        launch=_launch_trim,
        run_kind=RunKind.TRIM,
        inputs=(
            PortSpec("reads", PortType(format=FormatKind.FASTQ)),
            PortSpec("mate", PortType(format=FormatKind.FASTQ), required=False),
        ),
        outputs=(
            PortSpec(
                "trimmed",
                PortType(format=FormatKind.FASTQ, role=ObjectRole.TRIMMED_READS),
            ),
        ),
    ),
    "align": NodeTypeSpec(
        label="Align to reference",
        launch_name="pipeline_service.launch_alignment",
        launch=_launch_align,
        run_kind=RunKind.ALIGNMENT,
        tool_choice=ALIGN_TOOL_CHOICE,
        inputs=(
            # Several read files go in together -- chunked/split reads, not
            # mates. `mate` beside it stays scalar: R2 is one file with a
            # specific meaning, and collapsing the two concepts would lose it.
            PortSpec("reads", PortType(format=FormatKind.FASTQ), multiple=True),
            PortSpec("mate", PortType(format=FormatKind.FASTQ), required=False),
            # The role is required here, and it is the whole point: a protein
            # FASTA and a genome are both FormatKind.FASTA.
            PortSpec(
                "reference",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
        ),
        outputs=(
            PortSpec(
                "alignment",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
            ),
        ),
    ),
    "qc": NodeTypeSpec(
        label="Read QC",
        launch_name="pipeline_service.launch_qc",
        # Creates no PipelineRun.
        run_kind=None,
        launch=_launch_qc,
        inputs=(PortSpec("reads", PortType(format=FormatKind.FASTQ)),),
        outputs=(),
    ),
    "bam_stats": NodeTypeSpec(
        label="Alignment stats",
        launch_name="pipeline_service.launch_bam_stats",
        run_kind=None,  # Read-only: facts + a TSV, no PipelineRun.
        launch=_launch_bam_stats,
        inputs=(
            PortSpec(
                "alignment",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
            ),
        ),
        outputs=(),
    ),
    "vcf_stats": NodeTypeSpec(
        label="Variant stats",
        launch_name="pipeline_service.launch_vcf_stats",
        run_kind=None,  # Read-only, like bam_stats.
        launch=_launch_vcf_stats,
        inputs=(
            PortSpec(
                "variants",
                PortType(format=FormatKind.VCF, role=ObjectRole.VARIANTS),
            ),
        ),
        outputs=(),
    ),
    "call_variants": NodeTypeSpec(
        label="Call variants",
        launch_name="pipeline_service.launch_variant_calling",
        launch=_launch_variant_calling,
        run_kind=RunKind.VARIANT_CALLING,
        inputs=(
            PortSpec(
                "alignment",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
            ),
            # Optional: the launcher infers the reference from the BAM's own
            # provenance (reference_for_bam) when this is not wired.
            PortSpec(
                "reference",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
                required=False,
            ),
        ),
        outputs=(
            PortSpec(
                "variants",
                PortType(format=FormatKind.VCF, role=ObjectRole.VARIANTS),
            ),
        ),
    ),
    "annotate_variants": NodeTypeSpec(
        label="Annotate variants",
        launch_name="pipeline_service.launch_annotation",
        launch=_launch_annotation,
        # Creates no PipelineRun -- launch_annotation never calls
        # run_service.create_run, unlike launch_variant_calling beside it.
        run_kind=None,
        inputs=(
            # The reference and annotation (GFF3) are resolved internally via
            # resolve_annotation_inputs, not accepted as launch arguments, so
            # there is nothing else to wire here.
            PortSpec(
                "variants",
                PortType(format=FormatKind.VCF, role=ObjectRole.VARIANTS),
            ),
        ),
        outputs=(
            # A new VCF object (role VARIANTS, see _apply_annotate_variants),
            # not an in-place edit of the input -- so this gets a real output
            # port rather than outputs=().
            PortSpec(
                "annotated",
                PortType(format=FormatKind.VCF, role=ObjectRole.VARIANTS),
            ),
        ),
    ),
    "quantify": NodeTypeSpec(
        label="Quantify (featureCounts)",
        launch_name="pipeline_service.launch_quantify",
        launch=_launch_quantify,
        run_kind=RunKind.QUANTIFY,
        inputs=(
            PortSpec(
                "alignment",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
            ),
            # Optional: resolve_annotation falls back to the project's one
            # unambiguous GTF/GFF when this is not wired, and refuses only
            # when the project holds more than one.
            PortSpec(
                "annotation",
                PortType(format=FormatKind.GTF),
                required=False,
            ),
        ),
        outputs=(
            PortSpec("counts", PortType(format=FormatKind.TEXT, role=ObjectRole.COUNTS)),
        ),
    ),
    "differential_expression": NodeTypeSpec(
        label="Differential expression",
        launch_name="pipeline_service.launch_differential_expression",
        launch=_launch_differential_expression,
        run_kind=RunKind.DIFFERENTIAL_EXPRESSION,
        # See _launch_differential_expression's docstring: the real launcher
        # takes N counts objects via a `design` dict, which this scalar port
        # model cannot express directly. One representative "counts" port
        # stands in for the set; the per-sample condition assignment and the
        # contrast are carried in params.
        inputs=(
            PortSpec(
                "counts",
                PortType(format=FormatKind.TEXT, role=ObjectRole.COUNTS),
            ),
        ),
        outputs=(
            PortSpec(
                "results",
                PortType(format=FormatKind.TEXT, role=ObjectRole.DE_RESULTS),
            ),
        ),
    ),
    "assemble": NodeTypeSpec(
        label="Assemble (de novo)",
        launch_name="pipeline_service.launch_assembly",
        launch=_launch_assembly,
        run_kind=RunKind.ASSEMBLY,
        inputs=(PortSpec("reads", PortType(format=FormatKind.FASTQ)),),
        outputs=(
            # role=REFERENCE: _apply_assemble_reads ingests the contigs that
            # way deliberately, so a downstream align/polish/scaffold node can
            # pick them up the same way any other reference can.
            PortSpec(
                "assembly",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            # The GFA graph is a second, optional output of the same job
            # (_apply_assemble_reads ingests it independently of the contigs)
            # -- not required, since assembly still succeeds when it is
            # produced but this port is left unwired.
            PortSpec(
                "graph",
                PortType(format=FormatKind.GFA, role=ObjectRole.ASSEMBLY_GRAPH),
                required=False,
            ),
        ),
    ),
    "download_lineage": NodeTypeSpec(
        label="Download compleasm lineage",
        launch_name="pipeline_service.launch_lineage_download",
        launch=_launch_lineage_download,
        # No PipelineRun: fetches a project-agnostic, shared reference
        # dataset from the network, not something derived from an object in
        # a project.
        run_kind=None,
        # No object inputs: `lineage`/`odb` are strings chosen in a dialog,
        # not a wire from another node.
        inputs=(),
        # No DataObject either -- the dataset lands under settings.lineages_dir
        # on disk, outside the object model entirely, and is consumed by
        # `launch_completeness` checking `lineage_present()` rather than by
        # wiring an output object.
        outputs=(),
    ),
    "completeness": NodeTypeSpec(
        label="Completeness (compleasm)",
        launch_name="pipeline_service.launch_completeness",
        launch=_launch_completeness,
        run_kind=None,  # No PipelineRun: facts merged onto the assembly.
        inputs=(
            PortSpec(
                "assembly",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
        ),
        outputs=(),
    ),
    "consensus": NodeTypeSpec(
        label="Consensus (iVar)",
        launch_name="pipeline_service.launch_consensus",
        launch=_launch_consensus,
        run_kind=RunKind.REFERENCE_ASSEMBLY,
        run_tool="ivar",
        inputs=(
            PortSpec(
                "alignment",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
            ),
            # No reference port: the reference is resolved from the BAM's own
            # provenance (reference_assembly.resolve_alignment_target_for_bam)
            # and is not a launch argument at all.
            PortSpec(
                "primer_bed",
                PortType(format=FormatKind.BED),
                required=False,
            ),
        ),
        outputs=(
            # role=REFERENCE, matching _apply_consensus_from_alignment: a
            # consensus is a sequence others may align against.
            PortSpec(
                "consensus",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
        ),
    ),
    "polish": NodeTypeSpec(
        label="Polish (Polypolish)",
        launch_name="pipeline_service.launch_polish",
        launch=_launch_polish,
        run_kind=RunKind.REFERENCE_ASSEMBLY,
        run_tool="polypolish",
        inputs=(
            PortSpec(
                "draft",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            # Optional: when unwired, the launcher auto-picks the project's
            # one unambiguous short-read set and refuses if there is more
            # than one.
            PortSpec("reads", PortType(format=FormatKind.FASTQ), required=False),
            PortSpec("mate", PortType(format=FormatKind.FASTQ), required=False),
        ),
        outputs=(
            PortSpec(
                "polished",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
        ),
    ),
    "scaffold": NodeTypeSpec(
        label="Scaffold (RagTag)",
        launch_name="pipeline_service.launch_scaffold",
        launch=_launch_scaffold,
        run_kind=RunKind.REFERENCE_ASSEMBLY,
        run_tool="ragtag",
        inputs=(
            PortSpec(
                "draft",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            # Optional: the launcher auto-picks the project's one unambiguous
            # reference-role FASTA when this is not wired.
            PortSpec(
                "reference",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
                required=False,
            ),
        ),
        outputs=(
            PortSpec(
                "scaffolded",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
        ),
    ),
    "misassembly_qc": NodeTypeSpec(
        label="Misassembly QC (QUAST)",
        launch_name="pipeline_service.launch_misassembly_qc",
        launch=_launch_misassembly_qc,
        run_kind=None,  # Read-only: facts merged onto the draft.
        inputs=(
            PortSpec(
                "draft",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            # Optional: auto-picked from the project's one unambiguous
            # reference-role FASTA (excluding the draft itself) when unwired.
            PortSpec(
                "reference",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
                required=False,
            ),
        ),
        outputs=(),
    ),
    "assembly_error_qc": NodeTypeSpec(
        label="Assembly error QC (CRAQ)",
        launch_name="pipeline_service.launch_assembly_error_qc",
        launch=_launch_assembly_error_qc,
        run_kind=None,  # Read-only unless break_chimera, still no PipelineRun.
        inputs=(
            PortSpec(
                "assembly",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            # Both BAM slots are individually optional on the real launcher
            # (auto-paired from alignments_against when both are omitted),
            # but at least one is required at runtime -- a constraint this
            # scalar port model cannot express, so both are marked optional
            # here and the launcher's own ValidationError is what actually
            # enforces "at least one".
            PortSpec(
                "ngs_bam",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
                required=False,
            ),
            PortSpec(
                "sms_bam",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
                required=False,
            ),
        ),
        outputs=(),
    ),
    "qv_qc": NodeTypeSpec(
        label="QV assessment (Merqury)",
        launch_name="pipeline_service.launch_qv_qc",
        launch=_launch_qv_qc,
        run_kind=None,  # Read-only: facts, plus an optional cached k-mer db.
        inputs=(
            PortSpec(
                "assembly",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            # Optional: auto-picked from the project's one unambiguous read
            # set (any chemistry) when unwired.
            PortSpec("reads", PortType(format=FormatKind.FASTQ), required=False),
        ),
        outputs=(),
    ),
    "continuity_qc": NodeTypeSpec(
        label="Continuity QC (GCI)",
        launch_name="pipeline_service.launch_continuity_qc",
        launch=_launch_continuity_qc,
        run_kind=None,  # Read-only: facts and an optional depth plot.
        inputs=(
            PortSpec(
                "assembly",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            # See _launch_continuity_qc: the real launcher takes list-valued
            # hifi_bam_ids/nano_bam_ids (GCI cross-checks BAMs from different
            # aligners), collapsed here to one scalar port per slot for the
            # common single-candidate-per-slot case the Actions card itself
            # is restricted to.
            PortSpec(
                "hifi_bam",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
                required=False,
            ),
            PortSpec(
                "nano_bam",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
                required=False,
            ),
        ),
        outputs=(),
    ),
    "download_assembly": NodeTypeSpec(
        label="Download assembly (NCBI)",
        launch_name="ncbi_assembly_service.launch_download",
        launch=_launch_ncbi_assembly_download,
        run_kind=RunKind.ASSEMBLY_DOWNLOAD,
        # No object inputs: the source is NCBI, not another node's output --
        # `inputs=[]` on the PipelineRun itself says the same thing (see
        # ncbi_assembly_service.launch_download).
        inputs=(),
        # This launcher can actually stage several components (genome,
        # gff3, protein, cds -- see ncbi_assembly_components.COMPONENT_ORDER
        # and _role_for_component in queue/results.py), each becoming its own
        # DataObject with its own role. Only the genome/REFERENCE component
        # is exposed as a port: validate_selection always forces "genome"
        # into the selection, so it is the one output every run of this node
        # is guaranteed to produce, and the scalar port model here has no way
        # to express "N outputs depending on a params list" for the rest.
        outputs=(
            PortSpec(
                "assembly",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
        ),
    ),
    "download_sra": NodeTypeSpec(
        label="Download reads (SRA)",
        launch_name="sra_service.launch_download",
        launch=_launch_sra_download,
        run_kind=RunKind.SRA_DOWNLOAD,
        inputs=(),  # Source is SRA, not another node.
        outputs=(PortSpec("reads", PortType(format=FormatKind.FASTQ)),),
    ),
    "download_uniprot": NodeTypeSpec(
        label="Download proteins (UniProt)",
        launch_name="uniprot_service.launch_download",
        launch=_launch_uniprot_download,
        run_kind=RunKind.UNIPROT_DOWNLOAD,
        inputs=(),  # Source is UniProt, not another node.
        outputs=(
            PortSpec(
                "proteins",
                PortType(format=FormatKind.FASTA, role=ObjectRole.PROTEIN),
            ),
        ),
    ),
}


# Launchers deliberately not offered as canvas nodes, qualified the same way
# as NodeTypeSpec.launch_name ("module.function_name"). Each needs a reason --
# an entry without one is indistinguishable from an oversight.
EXCLUDED_LAUNCHES: frozenset[str] = frozenset(
    {
        # Auto-attached by launch_alignment (pipeline_service.py calls
        # _enqueue_build_index/build_index itself when the reference is
        # unindexed). A separate node would let a user build a graph that
        # indexes twice, or not at all.
        "pipeline_service.launch_build_index",
        # AI annotations over an existing object, not pipeline steps. They
        # produce a summary field rather than an object a downstream node
        # could consume, so they have no output port to wire.
        "pipeline_service.launch_summary",
        "pipeline_service.launch_de_summary",
        "pipeline_service.launch_variant_summary",
    }
)


def ports_for(node) -> tuple[tuple[PortSpec, ...], tuple[PortSpec, ...]]:
    """The (inputs, outputs) for one node, given the tool it has chosen.

    Every caller that used to read `spec.inputs`/`spec.outputs` should come
    here instead: a tool-parameterized node's real port set is not on its spec.
    Node types without a `tool_choice` -- most of them -- get their static
    tuples back unchanged, so this is a safe blanket replacement.

    An unset or unrecognized tool falls back to the default rather than
    raising. A node dropped from the palette has no tool until the resolver
    supplies one, and a definition saved before an aligner was removed must
    still open -- in both cases ports that exist beat an exception.
    """
    spec = NODE_TYPES.get(node.node_type) if node.node_type else None
    if spec is None:
        return (), ()
    choice = spec.tool_choice
    if choice is None:
        return spec.inputs, spec.outputs
    tool = node.params.get(choice.param_key) or choice.default
    if tool not in {o.value for o in choice.options}:
        tool = choice.default
    return choice.resolve(spec.inputs, spec.outputs, tool)


def launch_function_names() -> set[str]:
    """Every `launch_*` defined in the services layer, as "module.name".

    Discovered by inspection rather than listed, so the exhaustiveness test
    compares against reality instead of against a second hand-written list
    that would drift from the first. Scans every service module known to
    define one, not just pipeline_service -- uniprot_service and sra_service
    each contribute a `launch_download` of their own, with unrelated
    signatures, and a hand-picked module list here would silently drop them
    the same way a hand-picked launcher list would.

    Qualified by module rather than returning bare names: three different
    modules each define a function literally named `launch_download`, so bare
    names would collapse three distinguishable launchers into one entry that
    this file could only classify once for all three.
    """
    import inspect

    from app.services import (
        ncbi_assembly_service,
        pipeline_service as ps,
        sra_service,
        uniprot_service,
    )

    names: set[str] = set()
    for module in (ps, ncbi_assembly_service, sra_service, uniprot_service):
        module_short_name = module.__name__.rsplit(".", 1)[-1]
        for name, obj in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("launch_") and obj.__module__ == module.__name__:
                names.add(f"{module_short_name}.{name}")
    return names
