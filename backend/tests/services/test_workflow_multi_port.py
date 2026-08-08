"""Multi-valued input ports: several wires into one port.

The rule these exercise is deliberately narrow -- only the
one-wire-per-port check relaxes. Type checking still applies to every wire
independently, which is the half that would be easy to lose.
"""

import pytest

from app.models.object import FormatKind, ObjectRole
from app.models.workflow import (
    PortType,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.pipelines.node_types import NODE_TYPES, PortSpec
from app.services.workflow_service import validate_definition

# WorkflowDefinition is a Beanie Document; instantiating one (even without
# saving it) requires init_beanie to have run first, same reason every other
# Document-backed test in this directory requests beanie_models.
pytestmark = pytest.mark.usefixtures("beanie_models")


def _reads_input(node_id: str) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        kind=WorkflowNodeKind.INPUT,
        label=node_id,
        accepts=PortType(format=FormatKind.FASTQ),
    )


def test_multiple_is_false_by_default():
    port = PortSpec("reads", PortType(format=FormatKind.FASTQ))
    assert port.multiple is False


def test_two_wires_into_a_multi_port_validate():
    """align.reads is multiple, so chunked read files all go in together."""
    definition = WorkflowDefinition(
        name="two reads files",
        owner="tester",
        nodes=[
            _reads_input("r1"),
            _reads_input("r2"),
            WorkflowNode(
                node_id="ref",
                kind=WorkflowNodeKind.INPUT,
                label="ref",
                accepts=PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            WorkflowNode(
                node_id="align_1", kind=WorkflowNodeKind.ACTION, node_type="align"
            ),
        ],
        edges=[
            WorkflowEdge(from_node="r1", from_port="object", to_node="align_1", to_port="reads"),
            WorkflowEdge(from_node="r2", from_port="object", to_node="align_1", to_port="reads"),
            WorkflowEdge(from_node="ref", from_port="object", to_node="align_1", to_port="reference"),
        ],
    )
    assert validate_definition(definition) == []


def test_two_wires_into_a_scalar_port_still_fail():
    """The relaxation is per-port, not global."""
    definition = WorkflowDefinition(
        name="two references",
        owner="tester",
        nodes=[
            _reads_input("r1"),
            WorkflowNode(
                node_id="ref_a",
                kind=WorkflowNodeKind.INPUT,
                label="ref_a",
                accepts=PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            WorkflowNode(
                node_id="ref_b",
                kind=WorkflowNodeKind.INPUT,
                label="ref_b",
                accepts=PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            WorkflowNode(
                node_id="align_1", kind=WorkflowNodeKind.ACTION, node_type="align"
            ),
        ],
        edges=[
            WorkflowEdge(from_node="r1", from_port="object", to_node="align_1", to_port="reads"),
            WorkflowEdge(from_node="ref_a", from_port="object", to_node="align_1", to_port="reference"),
            WorkflowEdge(from_node="ref_b", from_port="object", to_node="align_1", to_port="reference"),
        ],
    )
    codes = [e.code for e in validate_definition(definition)]
    assert "duplicate_wire" in codes


def test_type_checking_still_applies_to_every_wire_of_a_multi_port():
    """A multi port is not an untyped port."""
    definition = WorkflowDefinition(
        name="a bam among the reads",
        owner="tester",
        nodes=[
            _reads_input("r1"),
            WorkflowNode(
                node_id="bam",
                kind=WorkflowNodeKind.INPUT,
                label="bam",
                accepts=PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
            ),
            WorkflowNode(
                node_id="ref",
                kind=WorkflowNodeKind.INPUT,
                label="ref",
                accepts=PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            WorkflowNode(
                node_id="align_1", kind=WorkflowNodeKind.ACTION, node_type="align"
            ),
        ],
        edges=[
            WorkflowEdge(from_node="r1", from_port="object", to_node="align_1", to_port="reads"),
            WorkflowEdge(from_node="bam", from_port="object", to_node="align_1", to_port="reads"),
            WorkflowEdge(from_node="ref", from_port="object", to_node="align_1", to_port="reference"),
        ],
    )
    codes = [e.code for e in validate_definition(definition)]
    assert "type_mismatch" in codes


def test_align_reads_is_multiple():
    reads = next(p for p in NODE_TYPES["align"].inputs if p.name == "reads")
    assert reads.multiple is True
