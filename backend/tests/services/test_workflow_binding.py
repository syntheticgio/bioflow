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
from app.models.object import FormatKind, ObjectRole
from app.models.workflow import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.services.workflow_binding import OutputCandidate, bind_downstream_inputs
from beanie import PydanticObjectId


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

    def test_a_mate_pair_fills_the_primary_and_mate_ports(self):
        """A paired trim produces two FASTQs from one declared output port, and
        the consumer has a `mate` port for exactly that. Treating them as two
        rivals for `reads` is what stalled a real trim -> align workflow: both
        matched, neither could be chosen, and nothing launched.

        The first candidate leads (`_outputs_of` orders R1 first), so this
        needs no filename parsing of its own.
        """
        graph = _graph(
            [_action("trim", "trim"), _action("align", "align")],
            [("trim", "trimmed", "align", "reads")],
        )
        r1 = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS, name="s_R1.fq")
        r2 = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS, name="s_R2.fq")

        bound = bind_downstream_inputs(graph, "trim", [r1, r2], paired=True)

        assert bound[("align", "reads")] == r1.object_id
        assert bound[("align", "mate")] == r2.object_id

    def test_a_pair_whose_consumer_has_no_mate_port_binds_only_the_primary(self):
        """QC takes one file and has no `mate` port -- a paired library is two
        files and gets two QC runs, which its docstring says outright. R1 must
        still bind rather than the whole node stalling."""
        graph = _graph(
            [_action("trim", "trim"), _action("qc", "qc")],
            [("trim", "trimmed", "qc", "reads")],
        )
        r1 = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS, name="s_R1.fq")
        r2 = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS, name="s_R2.fq")

        bound = bind_downstream_inputs(graph, "trim", [r1, r2], paired=True)

        assert bound == {("qc", "reads"): r1.object_id}

    def test_ambiguous_candidates_with_no_declared_mapping_bind_nothing_on_a_scalar_port(
        self,
    ):
        """Two equally type-compatible candidates and nothing saying which is
        which. Picking either is the arbitrary choice this module exists to
        avoid -- better to leave the dependent waiting somewhere visible than
        to feed a scalar port the wrong mate.

        `bam_stats`'s `alignment` port is scalar (unlike `align`'s `reads`,
        which became `multiple` for #94 and is covered by
        `test_ambiguous_candidates_bind_every_match_on_a_multi_port` below),
        so this keeps exercising the refuse-to-guess rule this test used to
        check against `align` before that port's type changed.
        """
        graph = _graph(
            [_action("align", "align"), _action("stats", "bam_stats")],
            [("align", "alignment", "stats", "alignment")],
        )
        first = _candidate(FormatKind.BAM, ObjectRole.ALIGNMENT, name="a.bam")
        second = _candidate(FormatKind.BAM, ObjectRole.ALIGNMENT, name="b.bam")

        assert bind_downstream_inputs(graph, "align", [first, second]) == {}

    def test_ambiguous_candidates_bind_every_match_on_a_multi_port(self):
        """The same shape as the scalar case above, but the target port is
        `multiple` -- `align`'s `reads`, since #94. There, "several
        candidates and no declared mapping" is not ambiguity to refuse; a
        multi port's whole point is to take everything type-compatible."""
        graph = _graph(
            [_action("trim", "trim"), _action("align", "align")],
            [("trim", "trimmed", "align", "reads")],
        )
        first = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS, name="a_R1.fq")
        second = _candidate(FormatKind.FASTQ, ObjectRole.TRIMMED_READS, name="a_R2.fq")

        bound = bind_downstream_inputs(graph, "trim", [first, second])

        assert bound == {("align", "reads"): [first.object_id, second.object_id]}

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
