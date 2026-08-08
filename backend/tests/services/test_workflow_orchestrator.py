"""The workflow engine: launch, progressive advance, retry, cancel, recover.

These are DB-backed because the orchestrator's job *is* the I/O -- the two
decisions it makes (`workflow_planner`, `workflow_binding`) are tested purely
elsewhere, and testing this layer against fakes would only assert that mocks
were called.

The launcher is the one seam that is faked. Really launching would run fastp,
so `NODE_TYPES` specs are patched with a recording stub. `spec_for` is patched
rather than `NODE_TYPES` entries directly -- the same trap CLAUDE.md records
for `aligner_registry`, where frozen dataclasses captured function objects at
import time and patching the module attribute never reached them.
"""

import pytest
from beanie import PydanticObjectId, init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS, JobState
from app.models.job import Job
from app.models.object import FormatKind, ObjectRole
from app.models.workflow import (
    NodeRunState,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowStatus,
)
from app.services import workflow_orchestrator as orch

OWNER = "tester"


@pytest.fixture(autouse=True)
async def _init_beanie_models(monkeypatch):
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=ALL_MODELS)
    monkeypatch.setattr("app.db.client.get_db", lambda: db)
    yield
    client.close()


@pytest.fixture
def launches(monkeypatch):
    """Record every launch instead of running a tool.

    Returns the list of (node_type, inputs) calls. Each launch creates a real
    PENDING Job so the completion hook has something to key on -- the whole
    point of job-grain tracking is that a node's state comes from its jobs.
    """
    calls: list[tuple[str, dict]] = []

    async def fake_launch(node_type: str, *, inputs: dict, params: dict, owner: str):
        calls.append((node_type, inputs))
        job = Job(type=f"run_{node_type}", owner=owner, state=JobState.PENDING)
        await job.insert()
        return job

    monkeypatch.setattr(orch, "_launch_node", fake_launch)
    return calls


async def _definition(nodes, edges) -> WorkflowDefinition:
    definition = WorkflowDefinition(
        name="wf", owner=OWNER, nodes=nodes, edges=[
            WorkflowEdge(from_node=f, from_port=fp, to_node=t, to_port=tp)
            for f, fp, t, tp in edges
        ],
    )
    await definition.insert()
    return definition


async def _stored_object(format: FormatKind, role: ObjectRole | None = None):
    """A real object, since binding reads format and role from the document."""
    from app.models.object import DataObject, FormatInfo

    obj = DataObject(
        name="produced.fq",
        project_id=PydanticObjectId(),
        owner=OWNER,
        format=FormatInfo(kind=format),
        role=role,
    )
    await obj.insert()
    return obj


def _input(node_id: str) -> WorkflowNode:
    return WorkflowNode(node_id=node_id, kind=WorkflowNodeKind.INPUT, label=node_id)


def _action(node_id: str, node_type: str, **kw) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id, kind=WorkflowNodeKind.ACTION, node_type=node_type, **kw
    )


async def _states(run: WorkflowRun) -> dict[str, NodeRunState]:
    rows = await WorkflowNodeRun.find(
        WorkflowNodeRun.workflow_run_id == run.id
    ).to_list()
    latest: dict[str, WorkflowNodeRun] = {}
    for row in rows:
        prev = latest.get(row.node_id)
        if prev is None or row.attempt > prev.attempt:
            latest[row.node_id] = row
    return {node_id: row.state for node_id, row in latest.items()}


async def _linear_run(launches) -> tuple[WorkflowRun, WorkflowDefinition]:
    """reads -> trim -> align, the smallest graph with a real dependency.

    `align` also takes a required `reference`, wired to its own INPUT node.
    Leaving it unwired would be a graph `validate_definition` rejects at save
    time, so a fixture without it tests a definition that cannot exist -- and
    it hid a real bug: the orchestrator used to launch a node whose required
    inputs were unresolved.
    """
    definition = await _definition(
        [
            _input("reads"),
            _input("reference"),
            _action("trim", "trim"),
            _action("align", "align"),
        ],
        [
            ("reads", "object", "trim", "reads"),
            ("trim", "trimmed", "align", "reads"),
            ("reference", "object", "align", "reference"),
        ],
    )
    run = await orch.launch_workflow(
        definition_id=definition.id,
        project_id=PydanticObjectId(),
        bindings={
            "reads": PydanticObjectId(),
            "reference": PydanticObjectId(),
        },
        owner=OWNER,
        label="test run",
    )
    return run, definition


async def _trim_output():
    """A trimmed FASTQ, the object `trim` produces and `align` consumes."""
    obj = await _stored_object(FormatKind.FASTQ, ObjectRole.TRIMMED_READS)
    return [orch.OutputCandidate(
        object_id=obj.id, format=FormatKind.FASTQ, role=ObjectRole.TRIMMED_READS
    )]


class TestLaunch:
    async def test_creates_a_node_run_for_every_node(self, launches):
        run, definition = await _linear_run(launches)
        states = await _states(run)
        assert set(states) == {"reads", "reference", "trim", "align"}

    async def test_launches_only_the_initially_runnable_node(self, launches):
        """`trim` is fed by an INPUT and starts immediately; `align` waits for
        `trim`'s output, which does not exist yet. Launching both is the
        `depends_on` mistake the design rejects -- align's launcher validates
        its inputs and would fail on a file nothing has written."""
        run, _ = await _linear_run(launches)
        assert [node_type for node_type, _ in launches] == ["trim"]

    async def test_pins_the_definition_version(self, launches):
        run, definition = await _linear_run(launches)
        assert run.definition_version == definition.version

    async def test_an_input_node_never_launches_and_is_immediately_satisfied(
        self, launches
    ):
        """An input binds a file; it does not run anything."""
        run, _ = await _linear_run(launches)
        states = await _states(run)
        assert states["reads"] is NodeRunState.SUCCEEDED

    async def test_the_launched_node_is_running(self, launches):
        run, _ = await _linear_run(launches)
        states = await _states(run)
        assert states["trim"] is NodeRunState.RUNNING
        assert states["align"] is NodeRunState.PENDING

    async def test_records_the_job_the_launch_enqueued(self, launches):
        """Job-grain tracking: the node run must remember which jobs are its
        own, or the completion hook has nothing to match against. This is what
        makes the 13 node types that create no PipelineRun trackable."""
        run, _ = await _linear_run(launches)
        row = await WorkflowNodeRun.find_one(
            WorkflowNodeRun.workflow_run_id == run.id,
            WorkflowNodeRun.node_id == "trim",
        )
        assert row.job_ids


class TestProgressiveLaunch:
    async def test_a_succeeding_node_launches_its_successor(self, launches):
        run, _ = await _linear_run(launches)
        await orch.on_node_finished(
            run.id, "trim", succeeded=True, outputs=await _trim_output()
        )

        assert [node_type for node_type, _ in launches] == ["trim", "align"]

    async def test_the_successor_receives_the_bound_output(self, launches):
        """The output→port resolution, end to end: align's `reads` port must
        receive the object trim produced, not the workflow's original input.

        A real StoredObject, because binding re-reads format and role from the
        object rather than trusting a copy denormalized onto the node run.
        """
        run, _ = await _linear_run(launches)
        produced = await _stored_object(FormatKind.FASTQ, ObjectRole.TRIMMED_READS)

        await orch.on_node_finished(
            run.id,
            "trim",
            succeeded=True,
            outputs=[
                orch.OutputCandidate(
                    object_id=produced.id,
                    format=FormatKind.FASTQ,
                    role=ObjectRole.TRIMMED_READS,
                )
            ],
        )

        _, align_inputs = launches[-1]
        assert align_inputs["reads"] == produced.id

    async def test_a_deleted_output_does_not_break_the_advance(self, launches):
        """An output id with no object behind it. The dependent should stay
        visibly unlaunched rather than every later advance raising."""
        run, _ = await _linear_run(launches)

        await orch.on_node_finished(
            run.id,
            "trim",
            succeeded=True,
            outputs=[
                orch.OutputCandidate(
                    object_id=PydanticObjectId(),
                    format=FormatKind.FASTQ,
                    role=ObjectRole.TRIMMED_READS,
                )
            ],
        )

        assert [node_type for node_type, _ in launches] == ["trim"]

    async def test_a_failing_node_does_not_launch_its_successor(self, launches):
        run, _ = await _linear_run(launches)
        await orch.on_node_finished(run.id, "trim", succeeded=False, outputs=[])

        assert [node_type for node_type, _ in launches] == ["trim"]

    async def test_a_failing_node_skips_its_descendants(self, launches):
        """SKIPPED, not left PENDING: a run holding a PENDING node never
        reaches a terminal status and never tells the user it is finished."""
        run, _ = await _linear_run(launches)
        await orch.on_node_finished(run.id, "trim", succeeded=False, outputs=[])

        states = await _states(run)
        assert states["trim"] is NodeRunState.FAILED
        assert states["align"] is NodeRunState.SKIPPED

    async def test_the_hook_is_idempotent(self, launches):
        """The hook runs off job completion, and a job can complete more than
        once from the orchestrator's point of view -- a retry, a duplicate
        event. Launching the successor twice would double-enqueue real work."""
        run, _ = await _linear_run(launches)
        produced = await _trim_output()
        await orch.on_node_finished(run.id, "trim", succeeded=True, outputs=produced)
        await orch.on_node_finished(run.id, "trim", succeeded=True, outputs=produced)

        assert [node_type for node_type, _ in launches] == ["trim", "align"]

    async def test_independent_branches_survive_a_failure(self, launches):
        """Branch-scoped failure, the design's §1.3. An unrelated QC node
        failing must not stop an assembly that shares only the input."""
        definition = await _definition(
            [_input("reads"), _action("qc", "qc"), _action("trim", "trim")],
            [("reads", "object", "qc", "reads"), ("reads", "object", "trim", "reads")],
        )
        run = await orch.launch_workflow(
            definition_id=definition.id,
            project_id=PydanticObjectId(),
            bindings={"reads": PydanticObjectId()},
            owner=OWNER,
            label="branches",
        )
        await orch.on_node_finished(run.id, "qc", succeeded=False, outputs=[])

        states = await _states(run)
        assert states["qc"] is NodeRunState.FAILED
        assert states["trim"] is NodeRunState.RUNNING

    async def test_a_tolerated_failure_still_advances_the_graph(self, launches):
        """`continue_on_failure`: QC failing means we lack a report, not that
        the file behind it is unusable.

        The realistic shape, and the one the design describes: QC *gates* the
        work rather than feeding it. `trim` takes its FASTQ from the shared
        input and depends on `qc` only for ordering -- which is why an
        untolerated QC failure would skip it, and a tolerated one must not.

        An earlier version of this test wired `qc.report` into `trim.reads`.
        That graph cannot exist: `validate_definition` rejects it as a type
        mismatch (a QC report is not a FASTQ), and a failed QC produces no
        object to bind at all -- so `trim` was unlaunchable for a reason that
        had nothing to do with tolerance.
        """
        definition = await _definition(
            [
                _input("reads"),
                _action("qc", "qc", continue_on_failure=True),
                _action("trim", "trim"),
            ],
            [
                ("reads", "object", "qc", "reads"),
                ("reads", "object", "trim", "reads"),
                # QC declares no output ports, so this edge carries no object
                # -- it is pure ordering, which is exactly what a gating node
                # is. The planner still treats it as a dependency.
                ("qc", "gate", "trim", "mate"),
            ],
        )
        run = await orch.launch_workflow(
            definition_id=definition.id,
            project_id=PydanticObjectId(),
            bindings={"reads": PydanticObjectId()},
            owner=OWNER,
            label="tolerant",
        )
        await orch.on_node_finished(run.id, "qc", succeeded=False, outputs=[])

        assert "trim" in [node_type for node_type, _ in launches]
        states = await _states(run)
        assert states["trim"] is not NodeRunState.SKIPPED

    async def test_an_untolerated_gating_failure_skips_its_dependent(self, launches):
        """The same graph without `continue_on_failure`, so the tolerance in
        the test above is doing the work rather than the wiring."""
        definition = await _definition(
            [_input("reads"), _action("qc", "qc"), _action("trim", "trim")],
            [
                ("reads", "object", "qc", "reads"),
                ("reads", "object", "trim", "reads"),
                # QC declares no output ports, so this edge carries no object
                # -- it is pure ordering, which is exactly what a gating node
                # is. The planner still treats it as a dependency.
                ("qc", "gate", "trim", "mate"),
            ],
        )
        run = await orch.launch_workflow(
            definition_id=definition.id,
            project_id=PydanticObjectId(),
            bindings={"reads": PydanticObjectId()},
            owner=OWNER,
            label="untolerant",
        )
        await orch.on_node_finished(run.id, "qc", succeeded=False, outputs=[])

        states = await _states(run)
        assert states["trim"] is NodeRunState.SKIPPED


class TestDerivedStatus:
    async def test_a_fresh_run_is_running_once_something_launched(self, launches):
        run, _ = await _linear_run(launches)
        assert await orch.status_of(run.id) is WorkflowStatus.RUNNING

    async def test_all_succeeded_is_succeeded(self, launches):
        run, _ = await _linear_run(launches)
        await orch.on_node_finished(run.id, "trim", succeeded=True, outputs=[])
        await orch.on_node_finished(run.id, "align", succeeded=True, outputs=[])

        assert await orch.status_of(run.id) is WorkflowStatus.SUCCEEDED

    async def test_a_failure_with_real_outputs_is_partial(self, launches):
        """The common ending for a graph with an optional QC leaf, per §1.3 --
        not an exotic case."""
        definition = await _definition(
            [_input("reads"), _action("qc", "qc"), _action("trim", "trim")],
            [("reads", "object", "qc", "reads"), ("reads", "object", "trim", "reads")],
        )
        run = await orch.launch_workflow(
            definition_id=definition.id,
            project_id=PydanticObjectId(),
            bindings={"reads": PydanticObjectId()},
            owner=OWNER,
            label="partial",
        )
        await orch.on_node_finished(run.id, "qc", succeeded=False, outputs=[])
        await orch.on_node_finished(run.id, "trim", succeeded=True, outputs=[])

        assert await orch.status_of(run.id) is WorkflowStatus.PARTIAL


class TestRetryInPlace:
    async def test_retry_creates_a_new_attempt(self, launches):
        """§1.4: a failed node gets a *new* job and a new attempt; a DEAD job
        cannot be un-deaded, so retry must re-point rather than reuse."""
        run, _ = await _linear_run(launches)
        await orch.on_node_finished(run.id, "trim", succeeded=False, outputs=[])

        await orch.retry_node(run.id, "trim", owner=OWNER)

        rows = await WorkflowNodeRun.find(
            WorkflowNodeRun.workflow_run_id == run.id,
            WorkflowNodeRun.node_id == "trim",
        ).to_list()
        assert sorted(r.attempt for r in rows) == [1, 2]

    async def test_retry_relaunches_the_node(self, launches):
        run, _ = await _linear_run(launches)
        await orch.on_node_finished(run.id, "trim", succeeded=False, outputs=[])
        await orch.retry_node(run.id, "trim", owner=OWNER)

        assert [node_type for node_type, _ in launches] == ["trim", "trim"]

    async def test_retry_clears_the_skip_on_descendants(self, launches):
        """A retried node's descendants were SKIPPED when it failed. Leaving
        them skipped means the retry succeeds and the graph still never
        advances -- the failure mode that makes retry useless."""
        run, _ = await _linear_run(launches)
        await orch.on_node_finished(run.id, "trim", succeeded=False, outputs=[])
        await orch.retry_node(run.id, "trim", owner=OWNER)

        states = await _states(run)
        assert states["align"] is NodeRunState.PENDING

    async def test_a_retried_node_that_succeeds_advances_the_graph(self, launches):
        run, _ = await _linear_run(launches)
        await orch.on_node_finished(run.id, "trim", succeeded=False, outputs=[])
        await orch.retry_node(run.id, "trim", owner=OWNER)
        await orch.on_node_finished(
            run.id, "trim", succeeded=True, outputs=await _trim_output()
        )

        assert "align" in [node_type for node_type, _ in launches]

    async def test_succeeded_siblings_are_not_re_executed(self, launches):
        """§1.4 again: retrying one node must not re-run a six-hour assembly
        that already succeeded."""
        definition = await _definition(
            [_input("reads"), _action("qc", "qc"), _action("trim", "trim")],
            [("reads", "object", "qc", "reads"), ("reads", "object", "trim", "reads")],
        )
        run = await orch.launch_workflow(
            definition_id=definition.id,
            project_id=PydanticObjectId(),
            bindings={"reads": PydanticObjectId()},
            owner=OWNER,
            label="siblings",
        )
        await orch.on_node_finished(run.id, "trim", succeeded=True, outputs=[])
        await orch.on_node_finished(run.id, "qc", succeeded=False, outputs=[])
        launches.clear()

        await orch.retry_node(run.id, "qc", owner=OWNER)

        assert [node_type for node_type, _ in launches] == ["qc"]


class TestCancel:
    async def test_cancelling_stops_pending_nodes(self, launches):
        run, _ = await _linear_run(launches)
        await orch.cancel_workflow(run.id, owner=OWNER)

        states = await _states(run)
        assert states["align"] is NodeRunState.CANCELLED

    async def test_cancelling_marks_the_running_node(self, launches):
        run, _ = await _linear_run(launches)
        await orch.cancel_workflow(run.id, owner=OWNER)

        states = await _states(run)
        assert states["trim"] is NodeRunState.CANCELLED

    async def test_cancelling_does_not_disturb_finished_nodes(self, launches):
        """A node that already succeeded keeps its result -- its output is
        still on disk and still usable."""
        run, _ = await _linear_run(launches)
        await orch.on_node_finished(run.id, "trim", succeeded=True, outputs=[])
        await orch.cancel_workflow(run.id, owner=OWNER)

        states = await _states(run)
        assert states["trim"] is NodeRunState.SUCCEEDED

    async def test_a_cancelled_run_launches_nothing_further(self, launches):
        """The hook must not resurrect a cancelled run. A late job completion
        arriving after cancellation is normal -- running jobs stop
        cooperatively, so their terminal write lands afterwards."""
        run, _ = await _linear_run(launches)
        await orch.cancel_workflow(run.id, owner=OWNER)
        launches.clear()

        await orch.on_node_finished(run.id, "trim", succeeded=True, outputs=[])

        assert launches == []


class TestReconcile:
    """The design's §10 risk: a process dying between a node finishing and its
    successor launching leaves the run stuck with no event coming to restart
    it. Nothing else will notice -- there is no timer on a workflow."""

    async def test_recovers_a_run_stranded_mid_advance(self, launches):
        run, _ = await _linear_run(launches)
        # Simulate the crash: the node's own row is marked succeeded with its
        # output recorded, but the successor was never launched because the
        # process died between the two writes. This is precisely the window
        # the design's §10 names -- nothing else will ever revive the run.
        produced = await _stored_object(FormatKind.FASTQ, ObjectRole.TRIMMED_READS)
        row = await WorkflowNodeRun.find_one(
            WorkflowNodeRun.workflow_run_id == run.id,
            WorkflowNodeRun.node_id == "trim",
        )
        await row.set(
            {
                WorkflowNodeRun.state: NodeRunState.SUCCEEDED,
                WorkflowNodeRun.outputs: [produced.id],
            }
        )
        launches.clear()

        recovered = await orch.reconcile_workflows()

        assert recovered >= 1
        assert [node_type for node_type, _ in launches] == ["align"]

    async def test_leaves_a_healthy_run_alone(self, launches):
        """A run whose node is legitimately still running must not be
        relaunched -- that would double-enqueue the work."""
        run, _ = await _linear_run(launches)
        launches.clear()

        await orch.reconcile_workflows()

        assert launches == []

    async def test_leaves_a_finished_run_alone(self, launches):
        run, _ = await _linear_run(launches)
        await orch.on_node_finished(run.id, "trim", succeeded=True, outputs=[])
        await orch.on_node_finished(run.id, "align", succeeded=True, outputs=[])
        launches.clear()

        await orch.reconcile_workflows()

        assert launches == []
