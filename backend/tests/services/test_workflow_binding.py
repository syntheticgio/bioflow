"""Resolving a finished node's outputs onto the ports below it.

The design's §6 is explicit that this is resolution *by declared output name,
with type as validation* -- not by type alone. A node producing several objects
that all match one port type is the case that makes the difference: type-only
matching picks an arbitrary one of them.

Kept pure (candidates in, binding out) so the matching rule is testable without
a database, launcher, or queue. The orchestrator does the I/O of finding the
candidates.
"""

import pytest
from beanie import PydanticObjectId

from app.models.object import FormatKind, ObjectRole
from app.models.workflow import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.services.workflow_binding import OutputCandidate, bind_downstream_inputs


def _candidate(
    format: FormatKind, role: ObjectRole | None = None, name: str = "f"
) -> OutputCandidate:
    return OutputCandidate(
        object_id=PydanticObjectId(), format=format, role=role, name=name
    )


def _graph(nodes, edges) -> WorkflowDefinition:
    return WorkflowDefinition.model_construct(
        name="t",
        owner="tester",
        nodes=nodes,
        edges=[
            WorkflowEdge(from_node=f, from_port=fp, to_node=t, to_port=tp)
            for f, fp, t, tp in edges
        ],
    )


def _action(node_id: str, node_type: str) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id, kind=WorkflowNodeKind.ACTION, node_type=node_type
    )


class TestBindDownstreamInputs:
    def test_binds_a_matching_output_to_the_wired_port(self):
        graph = _graph(
            [_action("trim", "trim"), _action("align", "align")],
            [("trim", "trimmed", "align", "reads")],
        )
        trimmed = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS)

        bound = bind_downstream_inputs(graph, "trim", [trimmed])

        assert bound == {("align", "reads"): trimmed.object_id}

    def test_an_output_whose_type_the_port_rejects_is_not_bound(self):
        """The type check is validation, not selection. A declared output that
        does not typecheck is a registry bug, and binding it anyway would hand
        an aligner a file it cannot read."""
        graph = _graph(
            [_action("trim", "trim"), _action("align", "align")],
            [("trim", "trimmed", "align", "reads")],
        )
        wrong = _candidate(FormatKind.BAM, ObjectRole.ALIGNMENT)

        assert bind_downstream_inputs(graph, "trim", [wrong]) == {}

    def test_picks_by_declared_name_when_several_outputs_share_a_type(self):
        """The case the design calls out. Paired trimming produces two FASTQs
        that both match a FASTQ port; only the declared output name says which
        one feeds `reads` and which feeds `mate`.

        The declared object is deliberately *second* in the candidate list. A
        type-only implementation takes the first type-compatible candidate and
        would bind `first` here -- so this fails unless the name is what
        actually drives the choice.
        """
        graph = _graph(
            [_action("trim", "trim"), _action("align", "align")],
            [("trim", "trimmed", "align", "reads")],
        )
        first = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS, name="a_R1.fq")
        second = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS, name="a_R2.fq")

        bound = bind_downstream_inputs(
            graph,
            "trim",
            [first, second],
            outputs_by_port={"trimmed": second.object_id},
        )

        assert bound == {("align", "reads"): second.object_id}

    def test_ambiguous_candidates_with_no_declared_mapping_bind_nothing(self):
        """Two equally type-compatible candidates and nothing saying which is
        which. Picking either is the arbitrary choice this module exists to
        avoid -- better to leave the dependent waiting somewhere visible than
        to feed an aligner the wrong mate."""
        graph = _graph(
            [_action("trim", "trim"), _action("align", "align")],
            [("trim", "trimmed", "align", "reads")],
        )
        first = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS, name="a_R1.fq")
        second = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS, name="a_R2.fq")

        assert bind_downstream_inputs(graph, "trim", [first, second]) == {}

    def test_feeds_several_downstream_nodes_from_one_output(self):
        """A fan-out: one trimmed FASTQ feeding both an aligner and a QC node.
        Binding only the first would strand the second forever."""
        graph = _graph(
            [_action("trim", "trim"), _action("align", "align"), _action("qc", "qc")],
            [("trim", "trimmed", "align", "reads"), ("trim", "trimmed", "qc", "reads")],
        )
        trimmed = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS)

        bound = bind_downstream_inputs(graph, "trim", [trimmed])

        assert bound == {
            ("align", "reads"): trimmed.object_id,
            ("qc", "reads"): trimmed.object_id,
        }

    def test_ignores_edges_leaving_other_nodes(self):
        """Only the node that just finished is being resolved. Touching another
        node's edges would bind a port from an output that does not exist yet."""
        graph = _graph(
            [_action("trim", "trim"), _action("align", "align"), _action("qc", "qc")],
            [("trim", "trimmed", "align", "reads"), ("qc", "report", "align", "mate")],
        )
        trimmed = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS)

        bound = bind_downstream_inputs(graph, "trim", [trimmed])

        assert ("align", "mate") not in bound

    def test_no_candidates_binds_nothing(self):
        """A node that succeeded without producing a file -- a read-only QC
        node is exactly this. It must not raise; its dependents simply have
        nothing to receive."""
        graph = _graph(
            [_action("qc", "qc"), _action("after", "align")],
            [("qc", "report", "after", "reads")],
        )
        assert bind_downstream_inputs(graph, "qc", []) == {}

    def test_an_unknown_output_port_binds_nothing(self):
        """An edge naming an output port the node type does not declare. The
        graph validator rejects this at save time, so reaching it means a
        definition that predates a registry change -- report nothing rather
        than guessing which output was meant."""
        graph = _graph(
            [_action("trim", "trim"), _action("align", "align")],
            [("trim", "nonexistent", "align", "reads")],
        )
        trimmed = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS)

        assert bind_downstream_inputs(graph, "trim", [trimmed]) == {}

    def test_a_role_required_by_the_port_is_enforced(self):
        """The protein-FASTA case the whole PortType design exists for: a
        FASTA with no reference role must not reach an aligner's reference
        port, even though the format matches."""
        graph = _graph(
            [_action("dl", "download_assembly"), _action("align", "align")],
            [("dl", "assembly", "align", "reference")],
        )
        roleless = _candidate(FormatKind.FASTA, None)

        assert bind_downstream_inputs(graph, "dl", [roleless]) == {}


class TestUnknownNodesAreSafe:
    def test_a_node_missing_from_the_graph_binds_nothing(self):
        graph = _graph([_action("trim", "trim")], [])
        assert bind_downstream_inputs(graph, "ghost", [_candidate(FormatKind.FASTQ)]) == {}

    @pytest.mark.parametrize("node_type", ["nonexistent_type", None])
    def test_a_node_with_no_registry_spec_binds_nothing(self, node_type):
        """A definition saved before a node type was renamed or removed."""
        graph = _graph(
            [
                WorkflowNode(
                    node_id="x", kind=WorkflowNodeKind.ACTION, node_type=node_type
                ),
                _action("align", "align"),
            ],
            [("x", "out", "align", "reads")],
        )
        assert bind_downstream_inputs(graph, "x", [_candidate(FormatKind.FASTQ)]) == {}
