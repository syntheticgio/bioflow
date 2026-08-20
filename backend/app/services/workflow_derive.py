"""Deriving a canvas from runs the user already did (design §7).

A convenience, and deliberately a shallow one: it reads `PipelineRun`s, maps
each to its node type, makes an INPUT node per external `RunInput`, and infers
edges where one run's output id appears in another's inputs. The result is an
*unsaved* `WorkflowDefinition` -- nothing here writes.

`RunInput` already carrying `object_id`, `name`, and `role` is what makes the
input-node derivation nearly mechanical.

Runs that cannot be represented are **reported as skipped, never silently
dropped**. A user who selects six runs and gets a four-node canvas with no
explanation has been told something false about their own history.

Design note: docs/superpowers/specs/2026-08-07-workflow-dag-design.md
"""

from dataclasses import dataclass, field

from beanie import PydanticObjectId

from app.models.run import PipelineRun, RunKind
from app.models.workflow import (
    NodePosition,
    PortType,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.pipelines.node_types import NODE_TYPES, ports_for


@dataclass(frozen=True)
class SkippedRun:
    run_id: str
    label: str
    reason: str


@dataclass
class DerivedGraph:
    nodes: list[WorkflowNode] = field(default_factory=list)
    edges: list[WorkflowEdge] = field(default_factory=list)
    skipped: list[SkippedRun] = field(default_factory=list)


def _node_type_for(kind: RunKind, tool: str | None = None) -> str | None:
    """Which node type represents this run.

    Derived from the registry rather than hand-written, so a node type added
    there is reachable here without a second list to keep in step -- the
    hand-maintained-registry hazard CLAUDE.md describes.

    `kind` alone identifies a node type for every RunKind but one.
    REFERENCE_ASSEMBLY covers three node types (consensus, polish, scaffold),
    and matching on kind alone would derive all three as whichever spec the
    registry happens to list first -- a polish run drawn as an iVar node, with
    ports it never had. Those specs carry `run_tool`, matched against the
    run's own `tool`, and a REFERENCE_ASSEMBLY run with an unrecognized tool
    yields None so it is reported as skipped rather than mislabelled.
    """
    fallback: str | None = None
    for node_type, spec in NODE_TYPES.items():
        if spec.run_kind is not kind:
            continue
        if spec.run_tool is None:
            fallback = node_type
        elif spec.run_tool == tool:
            return node_type
    return fallback


# The roles whose values do not read the same as the ports they feed. Both
# come from the reference-guided assembly nodes (#91), and both diverge for the
# same reason: a role is read in a run's own input list, where it must say what
# the file *is* ("draft" alone does not say draft what), while a port is read on
# a node that already names its tool.
#
# Kept as aliases rather than renaming either side: role values are stored on
# every existing run and port names in every saved workflow definition, so
# either rename needs a migration to fix a cosmetic mismatch. A role missing
# from here and matching no port draws no wire *and raises nothing* -- the
# canvas just quietly loses an edge -- so
# test_every_input_role_reaches_a_port_on_some_node_type asserts the coverage.
_ROLE_PORT_ALIASES: dict[str, str] = {
    "draft_assembly": "draft",
    "primers": "primer_bed",
    # An additional read set is the dialog's name for what the workflow node's
    # multi `reads` port carries -- chunked reads of the same library. A run
    # derived from the workflow carries EXTRA_READS inputs for its chunks, and
    # re-derivation must draw those wires back onto the port they came from.
    "extra_reads": "reads",
    # An additional set's mate feeds the run's R2 stream exactly as the
    # primary's mate does, so re-derivation wires it to the same port.
    "extra_mate": "mate",
    # A per-sample StringTie assembly feeds the merge node's representative
    # `transcripts` port (the run role is ASSEMBLED_TRANSCRIPTS; the port is
    # named for what it carries, matching transcript_assembly's output port so
    # the wire is drawn).
    "assembled_transcripts": "transcripts",
}


@dataclass(frozen=True)
class _NodeRef:
    """The minimal shape `ports_for` needs: a node type and its params.

    `ports_for` resolves a tool-parameterized node's real port set (e.g. a
    STAR-configured `align` node's extra `annotation` input) by reading
    `node.node_type` and `node.params`. A bare `node_type` string -- what this
    module used before -- cannot carry that, so a STAR run's `annotation`
    RunInput matched against the *base* (minimap2-shaped) port set and always
    came back None, silently dropping the edge. This wraps `(node_type,
    params)` so callers here can go through `ports_for` like every other
    caller does, without constructing a full `WorkflowNode`.
    """

    node_type: str
    params: dict


def _port_for_role(node_type: str, params: dict, role: str | None) -> str | None:
    """The input port a `RunInput` of this role feeds.

    `RunInputRole`'s values were mostly chosen to read the same as the port
    names (`reads`, `mate`, `reference`, `alignment`, `annotation`), so the
    mapping is a name match against what the resolved port set declares
    rather than a second table, with `_ROLE_PORT_ALIASES` covering the roles
    where they diverge. A role with no matching port yields None and the
    edge is simply not drawn -- the node is still there for the user to wire
    by hand.

    Resolved via `ports_for`, not `NODE_TYPES[node_type].inputs` directly:
    the static spec is only the base port set, and a tool-added port (STAR's
    `annotation` input on `align`) only shows up once the node's actual
    `params` are taken into account.
    """
    if node_type not in NODE_TYPES or role is None:
        return None
    inputs, _ = ports_for(_NodeRef(node_type=node_type, params=params))
    wanted = _ROLE_PORT_ALIASES.get(role, role)
    return next((p.name for p in inputs if p.name == wanted), None)


def _accepts_for(node_type: str, params: dict, port: str | None) -> PortType | None:
    if node_type not in NODE_TYPES or port is None:
        return None
    inputs, _ = ports_for(_NodeRef(node_type=node_type, params=params))
    match = next((p for p in inputs if p.name == port), None)
    return match.type if match else None


def _in_dependency_order(
    runs: list[PipelineRun], producer: dict[PydanticObjectId, PipelineRun]
) -> list[PipelineRun]:
    """Runs sorted so a producer comes before its consumers.

    Kahn's algorithm over the producer map. Terminates on any input, including
    a selection that somehow describes a cycle: whatever is left when no node
    has zero in-degree is appended in its original order rather than dropped or
    looped over. Real runs cannot form a cycle -- an object exists before the
    run that reads it -- but "cannot happen" is a poor reason to hang.
    """
    depends_on: dict[PydanticObjectId, set[PydanticObjectId]] = {
        run.id: set() for run in runs
    }
    for run in runs:
        for run_input in run.inputs:
            upstream = producer.get(run_input.object_id)
            if upstream is not None and upstream.id != run.id:
                depends_on[run.id].add(upstream.id)

    by_id = {run.id: run for run in runs}
    ordered: list[PipelineRun] = []
    remaining = dict(depends_on)

    while remaining:
        ready = [rid for rid, deps in remaining.items() if not (deps & remaining.keys())]
        if not ready:
            # Cycle, or something equally impossible. Keep the rest in the
            # order given rather than spinning.
            ordered.extend(by_id[rid] for rid in remaining)
            break
        # Stable within a layer, so an unrelated pair keeps its selection order.
        for rid in sorted(ready, key=lambda r: list(depends_on).index(r)):
            ordered.append(by_id[rid])
            del remaining[rid]

    return ordered


async def derive_definition(
    run_ids: list[PydanticObjectId], *, owner: str
) -> DerivedGraph:
    """Build an unsaved graph from a selection of runs.

    Owner-scoped: this reads someone's history, so a run belonging to another
    profile is skipped exactly like an unrepresentable one rather than raising
    -- a mixed selection should still produce the part the caller may see.
    """
    graph = DerivedGraph()

    runs = await PipelineRun.find({"_id": {"$in": list(run_ids)}}).to_list()
    by_id = {run.id: run for run in runs}

    # Ordered by the caller's selection so the canvas lays out predictably, and
    # so a missing id is visible as a skip rather than by absence.
    ordered: list[PipelineRun] = []
    for run_id in run_ids:
        run = by_id.get(run_id)
        if run is None or run.owner != owner:
            graph.skipped.append(
                SkippedRun(
                    run_id=str(run_id),
                    label=run.label if run else "",
                    reason="Run not found.",
                )
            )
            continue
        ordered.append(run)

    # Which selected run produced which object: the basis for every inferred
    # edge, and for knowing that an input is *internal* rather than a slot.
    producer: dict[PydanticObjectId, PipelineRun] = {}
    for run in ordered:
        for object_id in run.outputs:
            producer[object_id] = run

    # Lay the graph out in dependency order rather than selection order. The
    # canvas reads left to right, so a run placed left of the run that feeds it
    # draws every wire backwards -- correct, and unreadable. Runs are usually
    # selected newest-first, which is exactly the wrong order.
    ordered = _in_dependency_order(ordered, producer)

    node_for_run: dict[PydanticObjectId, str] = {}
    action_nodes: list[tuple[PipelineRun, str]] = []

    for index, run in enumerate(ordered):
        node_type = _node_type_for(run.kind, run.tool)
        if node_type is None:
            graph.skipped.append(
                SkippedRun(
                    run_id=str(run.id),
                    label=run.label,
                    reason=f"No canvas node type represents a {run.kind.value} run.",
                )
            )
            continue
        node_id = f"{node_type}_{index + 1}"
        node_for_run[run.id] = node_id
        action_nodes.append((run, node_type))
        graph.nodes.append(
            WorkflowNode(
                node_id=node_id,
                kind=WorkflowNodeKind.ACTION,
                node_type=node_type,
                params=dict(run.params or {}),
                position=NodePosition(x=260.0 + index * 220.0, y=60.0),
            )
        )

    # One slot per distinct external object, not per reference to it: two runs
    # reading the same file describe one input, and duplicating it would ask
    # the user to bind the same object twice.
    input_node_for: dict[PydanticObjectId, str] = {}

    for run, node_type in action_nodes:
        target = node_for_run[run.id]
        params = dict(run.params or {})
        for run_input in run.inputs:
            port = _port_for_role(
                node_type, params, run_input.role.value if run_input.role else None
            )
            if port is None:
                continue

            upstream = producer.get(run_input.object_id)
            if upstream is not None and upstream.id in node_for_run:
                # Produced by another selected run: an edge, not a slot.
                # Resolved via `ports_for` using the *producing* run's own
                # params -- the same rule as the input side -- though no tool
                # choice changes a node's outputs today (`_resolve_align_ports`
                # only ever adds an input port), so this only matters if that
                # changes later.
                source = next(
                    ((r, t) for r, t in action_nodes if r.id == upstream.id), None
                )
                from_port = None
                if source is not None:
                    source_run, source_type = source
                    _, outputs = ports_for(
                        _NodeRef(
                            node_type=source_type,
                            params=dict(source_run.params or {}),
                        )
                    )
                    from_port = outputs[0].name if outputs else None
                if from_port:
                    graph.edges.append(
                        WorkflowEdge(
                            from_node=node_for_run[upstream.id],
                            from_port=from_port,
                            to_node=target,
                            to_port=port,
                        )
                    )
                continue

            node_id = input_node_for.get(run_input.object_id)
            if node_id is None:
                node_id = f"input_{len(input_node_for) + 1}"
                input_node_for[run_input.object_id] = node_id
                graph.nodes.append(
                    WorkflowNode(
                        node_id=node_id,
                        kind=WorkflowNodeKind.INPUT,
                        label=run_input.name,
                        accepts=_accepts_for(node_type, params, port),
                        position=NodePosition(
                            x=40.0, y=60.0 + len(input_node_for) * 90.0
                        ),
                    )
                )
            graph.edges.append(
                WorkflowEdge(
                    from_node=node_id,
                    from_port="object",
                    to_node=target,
                    to_port=port,
                )
            )

    return graph


def as_definition(graph: DerivedGraph, *, name: str, owner: str) -> WorkflowDefinition:
    """The derived graph as an unsaved `WorkflowDefinition`.

    Constructed, never inserted -- §7 introduces no new persistence.
    """
    return WorkflowDefinition(
        name=name,
        description="Derived from previous runs.",
        nodes=graph.nodes,
        edges=graph.edges,
        owner=owner,
    )
