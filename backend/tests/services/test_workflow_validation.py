"""Graph validation: what the canvas refuses to save.

Every rule here has a failure it prevents. The type rules stop a protein FASTA
reaching an aligner's reference port; the cycle rule stops a graph that would
never launch a single node; the required-input rule stops a graph that looks
complete and cannot run.
"""

import pytest

from app.models import FormatKind, ObjectRole
from app.models.workflow import (
    PortType,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.services.workflow_service import ValidationError, validate_definition


def _input(node_id: str, fmt: FormatKind, role: ObjectRole | None = None) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        kind=WorkflowNodeKind.INPUT,
        label=node_id,
        accepts=PortType(format=fmt, role=role),
    )


def _action(node_id: str, node_type: str) -> WorkflowNode:
    return WorkflowNode(node_id=node_id, kind=WorkflowNodeKind.ACTION, node_type=node_type)


# WorkflowDefinition is a Beanie Document; instantiating one (even without
# saving it) requires init_beanie to have run first, same reason every other
# Document-backed test in this directory requests beanie_models.
pytestmark = pytest.mark.usefixtures("beanie_models")


class TestTypeRules:
    def test_a_matching_wire_validates(self):
        definition = WorkflowDefinition(
            name="ok",
            nodes=[_input("reads", FormatKind.FASTQ), _action("t", "trim")],
            edges=[WorkflowEdge(from_node="reads", from_port="object", to_node="t", to_port="reads")],
        )
        assert validate_definition(definition) == []

    def test_a_protein_fasta_cannot_feed_an_alignment_reference(self):
        """The rule this whole typing scheme exists for."""
        definition = WorkflowDefinition(
            name="bad",
            nodes=[
                _input("reads", FormatKind.FASTQ),
                _input("prot", FormatKind.FASTA, ObjectRole.PROTEIN),
                _action("a", "align"),
            ],
            edges=[
                WorkflowEdge(from_node="reads", from_port="object", to_node="a", to_port="reads"),
                WorkflowEdge(from_node="prot", from_port="object", to_node="a", to_port="reference"),
            ],
        )
        errors = validate_definition(definition)
        assert any(e.code == "type_mismatch" and e.node_id == "a" for e in errors)

    def test_a_wire_to_an_unknown_port_is_rejected(self):
        definition = WorkflowDefinition(
            name="bad",
            nodes=[_input("reads", FormatKind.FASTQ), _action("t", "trim")],
            edges=[WorkflowEdge(from_node="reads", from_port="object", to_node="t", to_port="nope")],
        )
        assert any(e.code == "unknown_port" for e in validate_definition(definition))

    def test_an_unknown_node_type_is_rejected(self):
        """A definition saved before a tool was removed must fail loudly
        rather than silently skipping the node at launch."""
        definition = WorkflowDefinition(name="bad", nodes=[_action("x", "no_such_tool")])
        assert any(e.code == "unknown_node_type" for e in validate_definition(definition))


class TestStructuralRules:
    def test_a_cycle_is_rejected(self):
        definition = WorkflowDefinition(
            name="cyclic",
            nodes=[_action("a", "trim"), _action("b", "trim")],
            edges=[
                WorkflowEdge(from_node="a", from_port="trimmed", to_node="b", to_port="reads"),
                WorkflowEdge(from_node="b", from_port="trimmed", to_node="a", to_port="reads"),
            ],
        )
        assert any(e.code == "cycle" for e in validate_definition(definition))

    def test_a_missing_required_input_is_rejected(self):
        """align needs a reference; a graph without one looks complete on the
        canvas and cannot run."""
        definition = WorkflowDefinition(
            name="incomplete",
            nodes=[_input("reads", FormatKind.FASTQ), _action("a", "align")],
            edges=[WorkflowEdge(from_node="reads", from_port="object", to_node="a", to_port="reads")],
        )
        errors = validate_definition(definition)
        assert any(e.code == "missing_required_input" and e.port == "reference" for e in errors)

    def test_an_optional_input_may_be_unwired(self):
        """Single-end reads: `mate` is genuinely absent, not an error."""
        definition = WorkflowDefinition(
            name="single end",
            nodes=[_input("reads", FormatKind.FASTQ), _action("t", "trim")],
            edges=[WorkflowEdge(from_node="reads", from_port="object", to_node="t", to_port="reads")],
        )
        assert validate_definition(definition) == []

    def test_two_wires_into_one_port_is_rejected(self):
        """A port takes one object. Two would make the launch ambiguous."""
        definition = WorkflowDefinition(
            name="bad",
            nodes=[
                _input("r1", FormatKind.FASTQ),
                _input("r2", FormatKind.FASTQ),
                _action("t", "trim"),
            ],
            edges=[
                WorkflowEdge(from_node="r1", from_port="object", to_node="t", to_port="reads"),
                WorkflowEdge(from_node="r2", from_port="object", to_node="t", to_port="reads"),
            ],
        )
        assert any(e.code == "duplicate_wire" for e in validate_definition(definition))

    def test_an_edge_naming_a_missing_node_is_rejected(self):
        definition = WorkflowDefinition(
            name="bad",
            nodes=[_action("t", "trim")],
            edges=[WorkflowEdge(from_node="ghost", from_port="object", to_node="t", to_port="reads")],
        )
        assert any(e.code == "unknown_node" for e in validate_definition(definition))

    def test_duplicate_node_ids_are_rejected(self):
        definition = WorkflowDefinition(
            name="bad", nodes=[_action("t", "trim"), _action("t", "qc")]
        )
        assert any(e.code == "duplicate_node_id" for e in validate_definition(definition))
