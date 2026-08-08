"""Resolving a finished node's outputs onto the ports below it.

The design's §6 is explicit that resolution is *by declared output name, with
type as validation* -- not by type alone. Type-only matching picks arbitrarily
the moment a node produces two objects of one type, which paired trimming does
routinely.

Pure: candidates in, bindings out. Finding the candidates is the orchestrator's
I/O; choosing among them is the decision, and it lives here so it can be tested
without a database.

Design note: docs/superpowers/specs/2026-08-07-workflow-dag-design.md
"""

from dataclasses import dataclass

from beanie import PydanticObjectId

from app.models.object import FormatKind, ObjectRole
from app.models.workflow import WorkflowDefinition, WorkflowNode, WorkflowNodeKind
from app.pipelines.node_types import NODE_TYPES


@dataclass(frozen=True)
class OutputCandidate:
    """One object a node produced, reduced to what port matching needs.

    A projection of `StoredObject` rather than the document itself, so the
    matching rule cannot come to depend on fields that only exist once a file
    is fully ingested.
    """

    object_id: PydanticObjectId
    format: FormatKind
    role: ObjectRole | None = None
    name: str = ""


def _output_port_type(node: WorkflowNode, port_name: str):
    """The PortType flowing out of a node's named output port.

    Mirrors `workflow_service._output_type`. An INPUT node has exactly one
    output, `object`, carrying whatever it accepts -- it is a slot, not a
    computation.
    """
    if node.kind is WorkflowNodeKind.INPUT:
        return node.accepts if port_name == "object" else None
    if node.node_type is None:
        return None
    spec = NODE_TYPES.get(node.node_type)
    if spec is None:
        return None
    port = next((p for p in spec.outputs if p.name == port_name), None)
    return port.type if port else None


def bind_downstream_inputs(
    definition: WorkflowDefinition,
    node_id: str,
    candidates: list[OutputCandidate],
    *,
    outputs_by_port: dict[str, PydanticObjectId] | None = None,
) -> dict[tuple[str, str], PydanticObjectId]:
    """Map (downstream node, input port) -> object id, for one finished node.

    `outputs_by_port` names which produced object corresponds to which declared
    output port. It is what makes resolution by *name*: with it, a node
    producing an R1 and an R2 binds each to the right port. Without it -- the
    common single-output case, and the only shape most node types have -- the
    lone type-compatible candidate is unambiguous and is used directly.

    A candidate whose type the target port rejects is never bound. That check
    is validation rather than selection: the graph validator already rejects a
    mistyped wire at save time, so failing it here means a definition that
    predates a registry change, and binding anyway would hand a tool a file it
    cannot read.
    """
    by_id = {node.node_id: node for node in definition.nodes}
    source = by_id.get(node_id)
    if source is None:
        return {}

    named = outputs_by_port or {}
    by_object_id = {c.object_id: c for c in candidates}
    bound: dict[tuple[str, str], PydanticObjectId] = {}

    for edge in definition.edges:
        if edge.from_node != node_id:
            continue
        target = by_id.get(edge.to_node)
        if target is None or target.node_type is None:
            continue
        target_spec = NODE_TYPES.get(target.node_type)
        if target_spec is None:
            continue
        port = next((p for p in target_spec.inputs if p.name == edge.to_port), None)
        if port is None:
            continue

        # An edge naming an output port the source does not declare. Nothing
        # sensible to bind, and guessing would be worse than leaving the
        # dependent waiting where the reason is visible.
        if _output_port_type(source, edge.from_port) is None:
            continue

        chosen: OutputCandidate | None = None
        if edge.from_port in named:
            chosen = by_object_id.get(named[edge.from_port])
        elif len(candidates) == 1:
            chosen = candidates[0]
        else:
            # Several candidates and no declared mapping: fall back to type,
            # but only when it is unambiguous. Two matches mean the node type
            # owes us an `outputs_by_port`, and picking one would be the
            # arbitrary choice this module exists to avoid.
            matching = [c for c in candidates if port.type.accepts(c.format, c.role)]
            chosen = matching[0] if len(matching) == 1 else None

        if chosen is None:
            continue
        if not port.type.accepts(chosen.format, chosen.role):
            continue
        bound[(edge.to_node, edge.to_port)] = chosen.object_id

    return bound
