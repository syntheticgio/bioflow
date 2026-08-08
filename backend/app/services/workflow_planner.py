"""The scheduling decision for a workflow run: what may start, what never can.

Pure and database-free, the same choice `classify_dependencies` and
`derive_status` made and for the same reason: the decision is the part worth
getting right, and it is easy to get subtly wrong in a way only a rare graph
shape reveals. The orchestrator around this does I/O; this module does not.

`runnable_nodes` and `doomed_nodes` are duals. A node in neither is still
waiting on something that has not finished yet.

Design note: docs/superpowers/specs/2026-08-07-workflow-dag-design.md
"""

from collections import defaultdict

from app.models.workflow import (
    NodeRunState,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowNodeKind,
)

# Terminal and unsuccessful. A node in one of these will never produce the
# output its dependents are waiting for, which is what makes them doom-bearing
# -- unless the node opted into `continue_on_failure`.
UNSUCCESSFUL = frozenset(
    {NodeRunState.FAILED, NodeRunState.CANCELLED, NodeRunState.SKIPPED}
)
# Kept as a private alias so this module's own body reads unchanged; the public
# name is what other modules import.
_UNSUCCESSFUL = UNSUCCESSFUL


def _parents(definition: WorkflowDefinition) -> dict[str, list[str]]:
    parents: dict[str, list[str]] = defaultdict(list)
    for edge in definition.edges:
        parents[edge.to_node].append(edge.from_node)
    return parents


def _children(definition: WorkflowDefinition) -> dict[str, list[str]]:
    children: dict[str, list[str]] = defaultdict(list)
    for edge in definition.edges:
        children[edge.from_node].append(edge.to_node)
    return children


def _by_id(definition: WorkflowDefinition) -> dict[str, WorkflowNode]:
    return {node.node_id: node for node in definition.nodes}


def _blocks_dependents(node: WorkflowNode | None, state: NodeRunState) -> bool:
    """Whether this node ending in `state` stops the nodes below it.

    An INPUT node never blocks: it is a binding, satisfied from the moment the
    run is created, and it has no state of its own to be in.
    """
    if node is not None and node.kind is WorkflowNodeKind.INPUT:
        return False
    if state not in _UNSUCCESSFUL:
        return True  # still in flight, or succeeded-but-not-yet-considered
    return not (node is not None and node.continue_on_failure)


def runnable_nodes(
    definition: WorkflowDefinition, states: dict[str, NodeRunState]
) -> set[str]:
    """ACTION nodes whose every input is satisfied and which have not started.

    Called after each job completion, so it must be idempotent: a node already
    RUNNING is not returned again, or the hook would enqueue its work twice.

    An INPUT parent is always satisfied -- the binding exists from run creation.
    A parent marked `continue_on_failure` satisfies its children even when it
    failed, which is the graph-level half of the queue behaviour #77 added.
    """
    by_id = _by_id(definition)
    parents = _parents(definition)
    runnable: set[str] = set()

    for node in definition.nodes:
        if node.kind is not WorkflowNodeKind.ACTION:
            continue
        if states.get(node.node_id, NodeRunState.PENDING) is not NodeRunState.PENDING:
            continue

        ready = True
        for parent_id in parents.get(node.node_id, ()):
            parent = by_id.get(parent_id)
            if parent is not None and parent.kind is WorkflowNodeKind.INPUT:
                continue
            parent_state = states.get(parent_id, NodeRunState.PENDING)
            if parent_state is NodeRunState.SUCCEEDED:
                continue
            # A tolerated parent that has *finished* badly still releases its
            # children; one that is still running does not. Tolerating failure
            # is not the same as not waiting -- the same distinction the queue
            # draws in `classify_dependencies`.
            if parent_state in _UNSUCCESSFUL and not _blocks_dependents(
                parent, parent_state
            ):
                continue
            ready = False
            break

        if ready:
            runnable.add(node.node_id)

    return runnable


def doomed_nodes(
    definition: WorkflowDefinition, states: dict[str, NodeRunState]
) -> set[str]:
    """Nodes that can never run, because something above them failed.

    Transitive on purpose: marking only the immediate child of a failure would
    leave the grandchild PENDING forever, and a run that never reaches a
    terminal status never tells the user it has finished.

    Only nodes still PENDING are returned. A node that already succeeded before
    its sibling failed keeps its result -- re-marking it would erase an output
    the user can still use.
    """
    by_id = _by_id(definition)
    children = _children(definition)

    frontier = [
        node.node_id
        for node in definition.nodes
        if _blocks_dependents(node, states.get(node.node_id, NodeRunState.PENDING))
        and states.get(node.node_id, NodeRunState.PENDING) in _UNSUCCESSFUL
    ]

    doomed: set[str] = set()
    seen = set(frontier)
    while frontier:
        for child_id in children.get(frontier.pop(), ()):
            if child_id in seen:
                continue
            seen.add(child_id)
            if states.get(child_id, NodeRunState.PENDING) is not NodeRunState.PENDING:
                continue  # already ran, or already resolved
            child = by_id.get(child_id)
            if child is not None and child.continue_on_failure:
                continue  # survives its parent, and so does everything below it
            doomed.add(child_id)
            frontier.append(child_id)

    return doomed
