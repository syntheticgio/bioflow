"""The persisted workflow graph.

Port types reuse FormatKind/ObjectRole rather than a parallel vocabulary,
because the rule they enforce already exists: ObjectRole.PROTEIN is commented
in models/object.py as the thing that keeps a protein FASTA out of the
aligner's reference picker. A canvas refusing that wire is that same rule.
"""

import pytest

from app.models.workflow import (
    PortType,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.models import FormatKind, ObjectRole

pytestmark = pytest.mark.asyncio(loop_scope="module")


class TestPortType:
    def test_same_format_and_role_accepts(self):
        port = PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE)
        assert port.accepts(FormatKind.FASTA, ObjectRole.REFERENCE)

    def test_a_protein_fasta_is_not_a_reference(self):
        """The failure this typing exists to prevent."""
        port = PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE)
        assert not port.accepts(FormatKind.FASTA, ObjectRole.PROTEIN)

    def test_wrong_format_never_accepts(self):
        port = PortType(format=FormatKind.BAM, role=None)
        assert not port.accepts(FormatKind.FASTQ, None)

    def test_a_null_role_accepts_any_role(self):
        """A port that cares only about format -- QC reads any FASTQ,
        trimmed or raw."""
        port = PortType(format=FormatKind.FASTQ, role=None)
        assert port.accepts(FormatKind.FASTQ, ObjectRole.TRIMMED_READS)
        assert port.accepts(FormatKind.FASTQ, None)

    def test_a_typed_port_rejects_an_untyped_object(self):
        """An object with no role cannot satisfy a port that requires one:
        the role is what carries the intent, and guessing is what
        ObjectRole exists to avoid."""
        port = PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE)
        assert not port.accepts(FormatKind.FASTA, None)


class TestWorkflowNodeKind:
    def test_both_kinds_exist(self):
        assert {k.value for k in WorkflowNodeKind} == {"input", "action"}


class TestWorkflowDefinition:
    async def test_a_definition_holds_nodes_and_edges(self, beanie_models):
        definition = WorkflowDefinition(
            name="trim then align",
            description="",
            nodes=[
                WorkflowNode(
                    node_id="reads",
                    kind=WorkflowNodeKind.INPUT,
                    label="sample reads",
                    accepts=PortType(format=FormatKind.FASTQ),
                ),
                WorkflowNode(
                    node_id="trim1",
                    kind=WorkflowNodeKind.ACTION,
                    node_type="trim",
                ),
            ],
            edges=[
                WorkflowEdge(
                    from_node="reads",
                    from_port="object",
                    to_node="trim1",
                    to_port="reads",
                )
            ],
        )
        await definition.insert()
        found = await WorkflowDefinition.get(definition.id)
        assert [n.node_id for n in found.nodes] == ["reads", "trim1"]
        assert found.edges[0].to_port == "reads"

    def test_a_new_definition_starts_at_version_one(self):
        """Runs pin the version they ran, so a historical run stays readable
        after the definition is edited."""
        definition = WorkflowDefinition(name="x", description="")
        assert definition.version == 1

    def test_a_definition_holds_no_object_ids(self):
        """The invariant that makes a definition reusable across projects.
        If this ever fails, someone has made saved graphs project-scoped."""
        fields = WorkflowDefinition.model_fields
        assert "project_id" not in fields
        assert "bindings" not in fields

    def test_continue_on_failure_defaults_off(self):
        node = WorkflowNode(
            node_id="a", kind=WorkflowNodeKind.ACTION, node_type="qc"
        )
        assert node.continue_on_failure is False
