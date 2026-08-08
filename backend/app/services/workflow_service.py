"""Workflow definitions: graph validation.

Validation returns a list rather than raising on the first problem, because
the canvas marks every bad wire at once -- a builder that reports one error per
save is a builder you fix by trial and error.
"""

from dataclasses import dataclass

from app import errors
from app.errors import NotFoundError
from app.models.workflow import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.pipelines.node_types import NODE_TYPES, NodeTypeSpec, PortSpec


@dataclass(frozen=True)
class ValidationError:
    code: str
    message: str
    node_id: str | None = None
    port: str | None = None


def _spec_for(node: WorkflowNode) -> NodeTypeSpec | None:
    if node.kind is not WorkflowNodeKind.ACTION or node.node_type is None:
        return None
    return NODE_TYPES.get(node.node_type)


def _input_port(spec: NodeTypeSpec, name: str) -> PortSpec | None:
    return next((p for p in spec.inputs if p.name == name), None)


def _output_type(node: WorkflowNode, port_name: str):
    """The PortType flowing out of a node's named output port.

    An INPUT node has exactly one output, `object`, carrying whatever type it
    accepts -- it is a slot, not a computation.
    """
    if node.kind is WorkflowNodeKind.INPUT:
        return node.accepts if port_name == "object" else None
    spec = _spec_for(node)
    if spec is None:
        return None
    port = next((p for p in spec.outputs if p.name == port_name), None)
    return port.type if port else None


def validate_definition(definition: WorkflowDefinition) -> list[ValidationError]:
    errors: list[ValidationError] = []
    by_id: dict[str, WorkflowNode] = {}

    for node in definition.nodes:
        if node.node_id in by_id:
            errors.append(
                ValidationError(
                    "duplicate_node_id",
                    f"More than one node has the id {node.node_id!r}.",
                    node_id=node.node_id,
                )
            )
            continue
        by_id[node.node_id] = node
        if node.kind is WorkflowNodeKind.ACTION and _spec_for(node) is None:
            errors.append(
                ValidationError(
                    "unknown_node_type",
                    f"No node type named {node.node_type!r}.",
                    node_id=node.node_id,
                )
            )

    wired: set[tuple[str, str]] = set()

    for edge in definition.edges:
        source = by_id.get(edge.from_node)
        target = by_id.get(edge.to_node)
        if source is None or target is None:
            missing = edge.from_node if source is None else edge.to_node
            errors.append(
                ValidationError(
                    "unknown_node", f"Edge names a node that does not exist: {missing!r}."
                )
            )
            continue

        spec = _spec_for(target)
        if spec is None:
            continue  # already reported as unknown_node_type

        port = _input_port(spec, edge.to_port)
        if port is None:
            errors.append(
                ValidationError(
                    "unknown_port",
                    f"{target.node_id!r} has no input port {edge.to_port!r}.",
                    node_id=target.node_id,
                    port=edge.to_port,
                )
            )
            continue

        key = (edge.to_node, edge.to_port)
        if key in wired:
            errors.append(
                ValidationError(
                    "duplicate_wire",
                    f"Port {edge.to_port!r} on {target.node_id!r} already has an input.",
                    node_id=target.node_id,
                    port=edge.to_port,
                )
            )
            continue
        wired.add(key)

        produced = _output_type(source, edge.from_port)
        if produced is None:
            errors.append(
                ValidationError(
                    "unknown_port",
                    f"{source.node_id!r} has no output port {edge.from_port!r}.",
                    node_id=source.node_id,
                    port=edge.from_port,
                )
            )
            continue

        if not port.type.accepts(produced.format, produced.role):
            errors.append(
                ValidationError(
                    "type_mismatch",
                    f"{source.node_id!r} produces {produced.format.value}"
                    f"/{produced.role.value if produced.role else 'any'}, which "
                    f"{target.node_id!r}'s {edge.to_port!r} port does not accept.",
                    node_id=target.node_id,
                    port=edge.to_port,
                )
            )

    for node in definition.nodes:
        spec = _spec_for(node)
        if spec is None:
            continue
        for port in spec.inputs:
            if port.required and (node.node_id, port.name) not in wired:
                errors.append(
                    ValidationError(
                        "missing_required_input",
                        f"{node.node_id!r} needs an input on {port.name!r}.",
                        node_id=node.node_id,
                        port=port.name,
                    )
                )

    if _has_cycle(definition):
        errors.append(
            ValidationError("cycle", "The graph contains a cycle and could never run.")
        )

    return errors


def _has_cycle(definition: WorkflowDefinition) -> bool:
    """Depth-first three-colour cycle detection.

    Iterative rather than recursive: a deep graph should report a cycle, not
    exhaust the interpreter's stack.
    """
    adjacency: dict[str, list[str]] = {n.node_id: [] for n in definition.nodes}
    for edge in definition.edges:
        if edge.from_node in adjacency and edge.to_node in adjacency:
            adjacency[edge.from_node].append(edge.to_node)

    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(adjacency, WHITE)

    for start in adjacency:
        if colour[start] != WHITE:
            continue
        stack = [(start, iter(adjacency[start]))]
        colour[start] = GREY
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if colour[child] == GREY:
                    return True
                if colour[child] == WHITE:
                    colour[child] = GREY
                    stack.append((child, iter(adjacency[child])))
                    advanced = True
                    break
            if not advanced:
                colour[node] = BLACK
                stack.pop()
    return False


class InvalidGraph(errors.AppError):
    """Raised rather than returned, because a caller that ignores a returned
    error list stores an unrunnable graph.

    A validation_error/422 in the app's own error hierarchy (see
    `app/errors.py`) rather than a bare Exception, so a future router calling
    `create_definition`/`update_definition` gets a proper HTTP response
    instead of a 500 -- `.errors` carries the full `ValidationError` list
    (the dataclass above, not `app.errors.ValidationError`) for a caller that
    wants to report every problem, while `.message` is a summary for the ones
    that just want a string.
    """

    status_code = 422
    code = "invalid_graph"

    def __init__(self, validation_errors: list[ValidationError]):
        self.errors = validation_errors
        # Also in `details`, because that is the only part `AppError.to_dict`
        # serializes -- a bare attribute reaches the exception handler and is
        # dropped, leaving the client with "3 validation error(s)" and no way
        # to learn which three. That defeats the entire reason
        # `validate_definition` returns a list instead of raising on the first
        # problem: the canvas marks every bad wire at once.
        super().__init__(
            f"{len(validation_errors)} validation error(s)",
            details={
                "errors": [
                    {
                        "code": e.code,
                        "message": e.message,
                        "node_id": e.node_id,
                        "port": e.port,
                    }
                    for e in validation_errors
                ]
            },
        )


async def create_definition(
    *,
    name: str,
    description: str,
    nodes: list[WorkflowNode],
    edges: list[WorkflowEdge],
    owner: str,
) -> WorkflowDefinition:
    definition = WorkflowDefinition(
        name=name, description=description, nodes=nodes, edges=edges, owner=owner
    )
    validation_errors = validate_definition(definition)
    if validation_errors:
        raise InvalidGraph(validation_errors)
    await definition.insert()
    return definition


async def list_definitions(*, owner: str) -> list[WorkflowDefinition]:
    """Every definition this profile owns, most recently edited first."""
    return (
        await WorkflowDefinition.find(WorkflowDefinition.owner == owner)
        .sort(-WorkflowDefinition.updated_at)
        .to_list()
    )


async def get_definition(definition_id, *, owner: str) -> WorkflowDefinition:
    """One definition, scoped to its owner.

    Another owner's definition raises `NotFoundError` rather than a permission
    error, matching `get_project`/`get_object` -- the whole codebase denies the
    same way, and "forbidden" would confirm the row exists.
    """
    definition = await WorkflowDefinition.get(definition_id)
    if definition is None or definition.owner != owner:
        raise NotFoundError(f"No definition {definition_id}.")
    return definition


async def update_definition(
    definition_id,
    *,
    name: str,
    description: str,
    nodes: list[WorkflowNode],
    edges: list[WorkflowEdge],
    owner: str | None = None,
) -> WorkflowDefinition:
    """Replace a definition's graph, bumping its version.

    The version bump is unconditional rather than change-detecting: a
    WorkflowRun pins the version it ran, and a cheap extra version is far
    better than two different graphs sharing one.

    `owner`, when given, scopes the lookup: without it any caller holding an id
    could rewrite any profile's saved graph. Optional rather than required only
    because this shipped before there was an owner to thread through; every
    caller that has one should pass it.
    """
    definition = await WorkflowDefinition.get(definition_id)
    if definition is None or (owner is not None and definition.owner != owner):
        raise NotFoundError(f"No definition {definition_id}.")

    candidate = WorkflowDefinition(
        name=name,
        description=description,
        nodes=nodes,
        edges=edges,
        owner=definition.owner,
    )
    validation_errors = validate_definition(candidate)
    if validation_errors:
        raise InvalidGraph(validation_errors)

    definition.name = name
    definition.description = description
    definition.nodes = nodes
    definition.edges = edges
    definition.version += 1
    definition.touch()
    await definition.save()
    return definition
