"""Node types parameterized by a tool.

`align` is one node type, not seven, and which aligner it runs lives in
`node.params["aligner"]` -- where `launch_alignment` already reads it from.
That is the whole reason this is a params key rather than a new model field:
the launchers already work this way, and `workflow_derive` already recovers
the tool from the PipelineRun.

The port set follows from the tool. STAR is the case that forces it: it can
build an annotation-aware index (see `aligners.STAR_ANNOTATED_DIR_SUFFIX`,
and `index_role`, which raises for a non-STAR annotated index), so a STAR
node has a GTF port that a minimap2 node has no meaning for.

Kept out of `node_types.py` because that file already carries the whole
registry at 789 lines and this is a separate responsibility: that file says
what nodes exist, this says how a parameterized one resolves.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.models.object import FormatKind
from app.models.workflow import PortType


@dataclass(frozen=True)
class ToolOption:
    value: str
    label: str


@dataclass(frozen=True)
class ToolChoice:
    """Which tool a node runs, and what that implies.

    `param_key` names where the choice lives in `node.params`, so this stays
    aligned with the launcher rather than duplicating it -- `align` reads
    `aligner`, `call_variants` reads `caller`.
    """

    param_key: str
    options: tuple[ToolOption, ...]
    default: str
    # (base_inputs, base_outputs, tool) -> (inputs, outputs). Takes the static
    # tuples so a resolver expresses the *difference* a tool makes rather than
    # restating every shared port -- which is what would drift.
    resolve: Callable


def _aligner_options() -> tuple[ToolOption, ...]:
    """Built from the aligner registry, not hand-listed.

    A second list of aligners here is the list nobody updates -- the exact
    failure `aligner_registry`'s docstring says it was created to end.
    """
    from app.pipelines.aligners import Aligner

    labels = {
        Aligner.MINIMAP2: "minimap2 (long reads)",
        Aligner.BWA_MEM2: "bwa-mem2 (short reads)",
        Aligner.BOWTIE2: "bowtie2 (short reads)",
        Aligner.HISAT2: "HISAT2 (spliced)",
        Aligner.STAR: "STAR (spliced, RNA-seq)",
        Aligner.WINNOWMAP: "Winnowmap (repetitive)",
    }
    return tuple(
        ToolOption(value=a.value, label=labels.get(a, a.value)) for a in Aligner
    )


def _resolve_align_ports(base_inputs, base_outputs, tool: str):
    """STAR alone gains an annotation port; every other aligner is the base set."""
    from app.pipelines.node_types import PortSpec

    if tool != "star":
        return base_inputs, base_outputs
    annotation = PortSpec(
        "annotation",
        PortType(format=FormatKind.GTF),
        # Optional: STAR supports an index with or without an annotation, and
        # both are legitimate. Required here would refuse a genomic STAR run
        # that works.
        required=False,
    )
    return (*base_inputs, annotation), base_outputs


ALIGN_TOOL_CHOICE = ToolChoice(
    param_key="aligner",
    options=_aligner_options(),
    default="minimap2",
    resolve=_resolve_align_ports,
)


def _resolve_annotate_ports(base_inputs, base_outputs, tool: str):
    """Both annotators (bcftools csq and SnpEff) share the same ports."""
    return base_inputs, base_outputs


ANNOTATION_TOOL_CHOICE = ToolChoice(
    param_key="annotator",
    options=(
        ToolOption(value="bcftools_csq", label="bcftools csq"),
        ToolOption(value="snpeff", label="SnpEff"),
    ),
    default="bcftools_csq",
    resolve=_resolve_annotate_ports,
)
