"""What a canvas node can be, and how it launches.

Every canvas capability reads from here: which nodes exist, which ports they
expose, which wires validate, and how a node actually runs.

Keyed by its own string rather than by RunKind, which was the obvious choice
and is wrong. Measured on main at design time: 24 launch_* functions exist
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

Status: scaffolding only. Three node types are classified (trim, align, qc);
the remaining ~17 launch_* functions (several of them one of the three
`launch_download`s above) are deliberately left unclassified here -- that is
a separate, later task's worklist, and test_every_launch_function_is_classified
is expected to fail until it is done. See that test's docstring.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.models.object import FormatKind, ObjectRole
from app.models.run import RunKind
from app.models.workflow import PortType
from app.services import pipeline_service


@dataclass(frozen=True)
class PortSpec:
    name: str
    type: PortType
    required: bool = True


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


async def _launch_trim(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_trim(
        object_id=inputs["reads"],
        owner=owner,
        mate_object_id=inputs.get("mate"),
        params=params,
    )


async def _launch_align(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_alignment(
        object_id=inputs["reads"],
        reference_id=inputs["reference"],
        owner=owner,
        mate_object_id=inputs.get("mate"),
        params=params,
    )


async def _launch_qc(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_qc(
        object_id=inputs["reads"], owner=owner
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
        inputs=(
            PortSpec("reads", PortType(format=FormatKind.FASTQ)),
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
