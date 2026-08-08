"""The scheduling decision: which nodes may launch, and which can never run.

Pure and database-free, for the same reason `classify_dependencies` and
`derive_status` are: this is the part that is easy to get subtly wrong in a way
only a rare graph shape reveals, and it should be testable without standing up
Mongo, Redis, and a launcher.

The two decisions here are duals of each other. `runnable_nodes` asks what may
start now; `doomed_nodes` asks what never can. A node missing from both is
simply still waiting.
"""

from app.models.workflow import (
    NodeRunState,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.services.workflow_planner import doomed_nodes, runnable_nodes


def _input(node_id: str) -> WorkflowNode:
    return WorkflowNode(node_id=node_id, kind=WorkflowNodeKind.INPUT, label=node_id)


def _action(node_id: str, node_type: str = "qc", **kw) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id, kind=WorkflowNodeKind.ACTION, node_type=node_type, **kw
    )


def _graph(nodes, edges) -> WorkflowDefinition:
    """Built with `model_construct` so these stay database-free.

    `WorkflowDefinition` is a Beanie Document and its normal constructor needs
    an initialized collection. The planner only ever reads `.nodes` and
    `.edges`, so skipping validation here keeps the pure tests pure -- the same
    motivation as `FakeJob` in tests/queue/test_dependencies.py.
    """
    return WorkflowDefinition.model_construct(
        name="t",
        owner="tester",
        nodes=nodes,
        edges=[
            WorkflowEdge(from_node=f, from_port=fp, to_node=t, to_port=tp)
            for f, fp, t, tp in edges
        ],
    )


class TestRunnableNodes:
    def test_a_node_fed_only_by_inputs_is_runnable_immediately(self):
        """The initial wave: nothing upstream has to compute first."""
        graph = _graph(
            [_input("reads"), _action("qc")],
            [("reads", "object", "qc", "reads")],
        )
        assert runnable_nodes(graph, {"qc": NodeRunState.PENDING}) == {"qc"}

    def test_a_node_waits_for_its_upstream_action(self):
        graph = _graph(
            [_input("reads"), _action("trim"), _action("qc")],
            [("reads", "object", "trim", "reads"), ("trim", "trimmed", "qc", "reads")],
        )
        states = {"trim": NodeRunState.RUNNING, "qc": NodeRunState.PENDING}
        assert runnable_nodes(graph, states) == set()

    def test_a_node_becomes_runnable_when_its_upstream_succeeds(self):
        graph = _graph(
            [_input("reads"), _action("trim"), _action("qc")],
            [("reads", "object", "trim", "reads"), ("trim", "trimmed", "qc", "reads")],
        )
        states = {"trim": NodeRunState.SUCCEEDED, "qc": NodeRunState.PENDING}
        assert runnable_nodes(graph, states) == {"qc"}

    def test_a_node_waits_for_the_slowest_of_several_upstreams(self):
        """The diamond's join. Launching on the first parent to finish is the
        bug this guards -- the second parent's output would not exist yet."""
        graph = _graph(
            [_input("reads"), _action("a"), _action("b"), _action("join")],
            [
                ("reads", "object", "a", "reads"),
                ("reads", "object", "b", "reads"),
                ("a", "report", "join", "reads"),
                ("b", "report", "join", "mate"),
            ],
        )
        states = {
            "a": NodeRunState.SUCCEEDED,
            "b": NodeRunState.RUNNING,
            "join": NodeRunState.PENDING,
        }
        assert runnable_nodes(graph, states) == set()

    def test_an_already_launched_node_is_not_runnable_again(self):
        """Idempotence. The hook re-runs this after every job completion, and a
        node that is RUNNING must not be launched a second time -- that would
        double-enqueue the work."""
        graph = _graph(
            [_input("reads"), _action("qc")],
            [("reads", "object", "qc", "reads")],
        )
        for state in (
            NodeRunState.RUNNING,
            NodeRunState.SUCCEEDED,
            NodeRunState.FAILED,
            NodeRunState.CANCELLED,
            NodeRunState.SKIPPED,
        ):
            assert runnable_nodes(graph, {"qc": state}) == set()

    def test_input_nodes_are_never_runnable(self):
        """An input binds a file; it does not run anything. Returning one here
        would send the orchestrator looking for a launcher it has no spec for."""
        graph = _graph(
            [_input("reads"), _action("qc")],
            [("reads", "object", "qc", "reads")],
        )
        assert "reads" not in runnable_nodes(graph, {"qc": NodeRunState.PENDING})

    def test_a_tolerated_failure_still_releases_its_dependent(self):
        """`continue_on_failure` at the graph level, mirroring what #77 wired
        into the queue: QC failing means we lack a report, not that the file
        behind it is unusable."""
        graph = _graph(
            [_input("reads"), _action("qc", continue_on_failure=True), _action("after")],
            [("reads", "object", "qc", "reads"), ("qc", "report", "after", "reads")],
        )
        states = {"qc": NodeRunState.FAILED, "after": NodeRunState.PENDING}
        assert runnable_nodes(graph, states) == {"after"}

    def test_an_untolerated_failure_does_not_release_its_dependent(self):
        graph = _graph(
            [_input("reads"), _action("qc"), _action("after")],
            [("reads", "object", "qc", "reads"), ("qc", "report", "after", "reads")],
        )
        states = {"qc": NodeRunState.FAILED, "after": NodeRunState.PENDING}
        assert runnable_nodes(graph, states) == set()

    def test_independent_branches_run_while_another_branch_is_dead(self):
        """The whole point of branch-scoped failure: an unrelated QC node
        failing must not stop an assembly that shares only the input."""
        graph = _graph(
            [_input("reads"), _action("dead"), _action("alive")],
            [("reads", "object", "dead", "reads"), ("reads", "object", "alive", "reads")],
        )
        states = {"dead": NodeRunState.FAILED, "alive": NodeRunState.PENDING}
        assert runnable_nodes(graph, states) == {"alive"}


class TestDoomedNodes:
    """Descendants of a failure that will never run. These become SKIPPED --
    distinct from CANCELLED, which is a user action."""

    def test_a_dependent_of_a_failure_is_doomed(self):
        graph = _graph(
            [_input("reads"), _action("qc"), _action("after")],
            [("reads", "object", "qc", "reads"), ("qc", "report", "after", "reads")],
        )
        states = {"qc": NodeRunState.FAILED, "after": NodeRunState.PENDING}
        assert doomed_nodes(graph, states) == {"after"}

    def test_doom_propagates_transitively(self):
        """Two steps below the failure is equally never going to run. Marking
        only the immediate child leaves the grandchild PENDING forever, and a
        workflow that never reaches a terminal status never tells the user it
        is finished."""
        graph = _graph(
            [_input("reads"), _action("a"), _action("b"), _action("c")],
            [
                ("reads", "object", "a", "reads"),
                ("a", "report", "b", "reads"),
                ("b", "report", "c", "reads"),
            ],
        )
        states = {
            "a": NodeRunState.FAILED,
            "b": NodeRunState.PENDING,
            "c": NodeRunState.PENDING,
        }
        assert doomed_nodes(graph, states) == {"b", "c"}

    def test_a_tolerated_failure_dooms_nothing(self):
        graph = _graph(
            [_input("reads"), _action("qc", continue_on_failure=True), _action("after")],
            [("reads", "object", "qc", "reads"), ("qc", "report", "after", "reads")],
        )
        states = {"qc": NodeRunState.FAILED, "after": NodeRunState.PENDING}
        assert doomed_nodes(graph, states) == set()

    def test_an_independent_branch_is_not_doomed(self):
        graph = _graph(
            [_input("reads"), _action("dead"), _action("alive")],
            [("reads", "object", "dead", "reads"), ("reads", "object", "alive", "reads")],
        )
        states = {"dead": NodeRunState.FAILED, "alive": NodeRunState.PENDING}
        assert doomed_nodes(graph, states) == set()

    def test_a_node_that_already_ran_is_not_doomed(self):
        """A node that succeeded before its sibling failed keeps its result.
        Re-marking it SKIPPED would erase a real output the user can still use."""
        graph = _graph(
            [_input("reads"), _action("a"), _action("b")],
            [("reads", "object", "a", "reads"), ("a", "report", "b", "reads")],
        )
        states = {"a": NodeRunState.FAILED, "b": NodeRunState.SUCCEEDED}
        assert doomed_nodes(graph, states) == set()

    def test_cancelled_and_skipped_upstreams_also_doom(self):
        """A cancelled parent is as final as a failed one; its child's input
        is never coming either. This mirrors the queue treating CANCELLED and
        DEAD as unsuccessful, not just FAILED."""
        for upstream in (NodeRunState.CANCELLED, NodeRunState.SKIPPED):
            graph = _graph(
                [_input("reads"), _action("a"), _action("b")],
                [("reads", "object", "a", "reads"), ("a", "report", "b", "reads")],
            )
            states = {"a": upstream, "b": NodeRunState.PENDING}
            assert doomed_nodes(graph, states) == {"b"}, upstream
