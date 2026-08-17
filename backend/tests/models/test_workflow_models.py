"""The persisted workflow graph.

Port types reuse FormatKind/ObjectRole rather than a parallel vocabulary,
because the rule they enforce already exists: ObjectRole.PROTEIN is commented
in models/object.py as the thing that keeps a protein FASTA out of the
aligner's reference picker. A canvas refusing that wire is that same rule.
"""

import pytest
from beanie import PydanticObjectId

from app.models import FormatKind, ObjectRole
from app.models.workflow import (
    NodeRunState,
    PortType,
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowStatus,
    derive_status,
)

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


class TestDerivedStatus:
    """Status is computed from node states, never stored -- following
    RunStatus's docstring. A stored status is a second source of truth that
    drifts the first time a write is lost."""

    def test_nothing_started_is_waiting(self):
        assert derive_status([NodeRunState.PENDING, NodeRunState.PENDING]) is WorkflowStatus.WAITING

    def test_any_running_is_running(self):
        assert (
            derive_status([NodeRunState.SUCCEEDED, NodeRunState.RUNNING])
            is WorkflowStatus.RUNNING
        )

    def test_all_succeeded_is_succeeded(self):
        assert (
            derive_status([NodeRunState.SUCCEEDED, NodeRunState.SUCCEEDED])
            is WorkflowStatus.SUCCEEDED
        )

    def test_a_finished_run_with_a_failure_is_partial(self):
        """The branch-scoped failure rule: an independent branch succeeded, so
        the run is not simply failed -- real outputs exist."""
        assert derive_status(
            [NodeRunState.SUCCEEDED, NodeRunState.FAILED]
        ) is WorkflowStatus.PARTIAL

    def test_everything_failed_is_failed(self):
        assert derive_status([NodeRunState.FAILED, NodeRunState.CANCELLED]) is WorkflowStatus.FAILED

    def test_a_pending_node_keeps_the_run_running(self):
        """A node still waiting on an upstream node means the workflow is not
        finished, even though nothing is executing this instant. Reporting
        PARTIAL here would call a live run finished."""
        assert derive_status(
            [NodeRunState.FAILED, NodeRunState.PENDING]
        ) is WorkflowStatus.RUNNING

    def test_an_empty_run_is_waiting(self):
        assert derive_status([]) is WorkflowStatus.WAITING


class TestWorkflowNodeRun:
    async def test_retry_adds_an_attempt_rather_than_overwriting(self, beanie_models):
        """The reason node runs are their own documents: a DEAD job cannot be
        un-deaded, so retry points the node at new work while its siblings
        keep their original links."""
        workflow_run_id = PydanticObjectId()
        first = WorkflowNodeRun(
            workflow_run_id=workflow_run_id,
            node_id="align1",
            attempt=1,
            state=NodeRunState.FAILED,
        )
        await first.insert()
        second = WorkflowNodeRun(
            workflow_run_id=workflow_run_id,
            node_id="align1",
            attempt=2,
            state=NodeRunState.RUNNING,
        )
        await second.insert()

        rows = await WorkflowNodeRun.find(
            WorkflowNodeRun.workflow_run_id == workflow_run_id
        ).to_list()
        assert sorted(r.attempt for r in rows) == [1, 2]

    async def test_one_row_per_node_attempt(self, beanie_models):
        """Re-linking on a retry must not duplicate a member and double-count
        it in the derived status -- the guard RunJob.uniq_run_job provides."""
        import pymongo.errors

        workflow_run_id = PydanticObjectId()
        await WorkflowNodeRun(
            workflow_run_id=workflow_run_id, node_id="n", attempt=1,
            state=NodeRunState.RUNNING,
        ).insert()
        with pytest.raises(pymongo.errors.DuplicateKeyError):
            await WorkflowNodeRun(
                workflow_run_id=workflow_run_id, node_id="n", attempt=1,
                state=NodeRunState.RUNNING,
            ).insert()


class TestWorkflowRun:
    async def test_a_run_pins_its_definition_version(self, beanie_models):
        run = WorkflowRun(
            definition_id=PydanticObjectId(),
            definition_version=3,
            project_id=PydanticObjectId(),
            label="trim then align",
            bindings=[
                WorkflowBinding(
                    node_id="reads",
                    object_id=PydanticObjectId(),
                    name="specimen_R1.fastq.gz",
                )
            ],
        )
        await run.insert()
        found = await WorkflowRun.get(run.id)
        assert found.definition_version == 3
        assert found.bindings[0].name == "specimen_R1.fastq.gz"

    def test_status_is_not_a_stored_field(self):
        """If this fails, someone has added a second source of truth."""
        assert "status" not in WorkflowRun.model_fields
