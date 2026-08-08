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
from app.pipelines.node_types import NODE_TYPES, ports_for


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
    _, outputs = ports_for(node)
    port = next((p for p in outputs if p.name == port_name), None)
    return port.type if port else None


def bind_downstream_inputs(
    definition: WorkflowDefinition,
    node_id: str,
    candidates: list[OutputCandidate],
    *,
    outputs_by_port: dict[str, PydanticObjectId] | None = None,
    paired: bool = False,
) -> dict[tuple[str, str], PydanticObjectId | list[PydanticObjectId]]:
    """Map (downstream node, input port) -> object id, for one finished node.

    A *multi* port maps to a list instead of a bare id -- every
    type-compatible candidate, in the order the source produced them. The
    union return type rather than always-a-list is deliberate: every existing
    consumer reads scalars, and making them all unwrap a one-element list to
    gain nothing would be a large diff with no behaviour in it.

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
        target_inputs, _ = ports_for(target)
        port = next((p for p in target_inputs if p.name == edge.to_port), None)
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
        elif paired and len(candidates) == 2:
            # A mate pair from one declared output port. The two are
            # type-identical by construction, so the generic ambiguity rule
            # below would refuse both and stall the node -- which is exactly
            # what a real paired trim -> align workflow did. R1 (first, per
            # `_outputs_of`'s ordering) fills the wired port; R2 fills the
            # consumer's `mate` port when it has one. A consumer without one
            # takes R1 alone: QC is single-file by design, and a paired
            # library simply gets two QC runs.
            chosen = candidates[0]
            mate_port = next(
                (p for p in target_inputs if p.name == "mate"), None
            )
            if mate_port is not None and mate_port.type.accepts(
                candidates[1].format, candidates[1].role
            ):
                bound[(edge.to_node, "mate")] = candidates[1].object_id
        elif len(candidates) == 1:
            chosen = candidates[0]
        elif port.multiple:
            # Several candidates, no declared mapping, and nothing to
            # disambiguate -- but a multi port has no ambiguity to resolve in
            # the first place: "several candidates" is the answer, not a
            # problem the generic rule below needs to refuse. Every
            # type-compatible candidate binds, as a list, in production order.
            matching = [c for c in candidates if port.type.accepts(c.format, c.role)]
            if matching:
                bound[(edge.to_node, edge.to_port)] = [c.object_id for c in matching]
            continue
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
