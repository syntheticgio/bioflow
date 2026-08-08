"""Wiring the orchestrator to real job completions.

The orchestrator is inert without this: `on_node_finished` has to be *called*
by something, and the only signal that a node's work finished is its jobs
reaching a terminal state. These cover the translation from "job X finished" to
"node N of workflow run R finished", which is the part that has to find the
node from the job rather than the other way round.

Kept separate from test_workflow_orchestrator.py because that file fakes the
launcher to test the engine's logic; this one is about the seam to the queue.
"""

import pytest
from beanie import PydanticObjectId, init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS, JobState
from app.models.job import Job
from app.models.object import DataObject, FormatInfo, FormatKind, ObjectRole
from app.models.workflow import (
    NodeRunState,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowNodeRun,
    WorkflowRun,
)
from app.services import workflow_hook

OWNER = "tester"


def _lua_sources() -> dict[str, str]:
    """Read the Lua scripts synchronously.

    Separate from the async fixture below on purpose: reading files with
    `pathlib` inside an async function is blocking I/O on the event loop, which
    ruff's ASYNC240 flags. tests/queue/conftest.py gets this right by keeping
    its own script fixture sync.
    """
    from pathlib import Path

    script_dir = Path(__file__).resolve().parents[2] / "app" / "queue" / "scripts"
    return {p.stem: p.read_text() for p in script_dir.glob("*.lua")}


@pytest.fixture
async def redis(monkeypatch):
    """Local rather than inherited: tests/queue/conftest.py defines one, but
    this file lives in tests/services/.

    The real Lua scripts are registered too, because `queue.complete()` calls
    `release` -- mocking that away would skip the very path under test.
    """
    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    registered = {
        name: client.register_script(source)
        for name, source in _lua_sources().items()
    }
    monkeypatch.setattr("app.db.redis_client._scripts", registered, raising=False)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _init_beanie_models(monkeypatch):
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=ALL_MODELS)
    monkeypatch.setattr("app.db.client.get_db", lambda: db)
    yield
    client.close()


async def _node_run(state: NodeRunState, job: Job) -> tuple[WorkflowRun, WorkflowNodeRun]:
    definition = WorkflowDefinition(
        name="wf",
        owner=OWNER,
        nodes=[
            WorkflowNode(node_id="reads", kind=WorkflowNodeKind.INPUT, label="reads"),
            WorkflowNode(
                node_id="trim", kind=WorkflowNodeKind.ACTION, node_type="trim"
            ),
        ],
        edges=[
            WorkflowEdge(
                from_node="reads", from_port="object", to_node="trim", to_port="reads"
            )
        ],
    )
    await definition.insert()
    run = WorkflowRun(
        definition_id=definition.id,
        definition_version=definition.version,
        project_id=PydanticObjectId(),
        label="wf",
        owner=OWNER,
    )
    await run.insert()
    row = WorkflowNodeRun(
        workflow_run_id=run.id,
        node_id="trim",
        state=state,
        job_ids=[job.id],
        owner=OWNER,
    )
    await row.insert()
    return run, row


class TestFindingTheNode:
    async def test_a_job_belonging_to_no_workflow_is_ignored(self):
        """Almost every job in this system is not part of a workflow. The hook
        must be cheap and silent for them, not raise."""
        job = Job(type="run_qc", owner=OWNER, state=JobState.SUCCEEDED)
        await job.insert()

        await workflow_hook.on_job_finished(job.id, succeeded=True)  # must not raise

    async def test_a_succeeding_job_completes_its_node(self):
        job = Job(type="run_trim", owner=OWNER, state=JobState.SUCCEEDED)
        await job.insert()
        run, row = await _node_run(NodeRunState.RUNNING, job)

        await workflow_hook.on_job_finished(job.id, succeeded=True)

        fresh = await WorkflowNodeRun.get(row.id)
        assert fresh.state is NodeRunState.SUCCEEDED

    async def test_a_failing_job_fails_its_node(self):
        job = Job(type="run_trim", owner=OWNER, state=JobState.FAILED)
        await job.insert()
        run, row = await _node_run(NodeRunState.RUNNING, job)

        await workflow_hook.on_job_finished(job.id, succeeded=False)

        fresh = await WorkflowNodeRun.get(row.id)
        assert fresh.state is NodeRunState.FAILED

    async def test_an_already_terminal_node_is_left_alone(self):
        """A repeat completion, or a second job of a node that already
        resolved. Must not flip a succeeded node back to running."""
        job = Job(type="run_trim", owner=OWNER, state=JobState.SUCCEEDED)
        await job.insert()
        run, row = await _node_run(NodeRunState.SUCCEEDED, job)

        await workflow_hook.on_job_finished(job.id, succeeded=False)

        fresh = await WorkflowNodeRun.get(row.id)
        assert fresh.state is NodeRunState.SUCCEEDED


class TestWiredIntoTheQueue:
    """The hook existing is not the hook being called. `queue.complete()` is
    the one place a job reaches a terminal state, and without this test the
    orchestrator could be perfectly correct and still never run."""

    async def test_completing_a_job_advances_its_workflow_node(
        self, redis, monkeypatch
    ):
        from app.queue import queue

        monkeypatch.setattr(queue, "get_redis", lambda: redis)

        job = Job(type="run_trim", owner=OWNER, state=JobState.RUNNING)
        job.lease = None
        await job.insert()
        run, row = await _node_run(NodeRunState.RUNNING, job)

        # complete() is epoch-fenced; mirror what a worker holds.
        from datetime import UTC, datetime, timedelta

        from app.models.job import JobLease

        now = datetime.now(UTC)
        await job.set(
            {
                Job.lease: JobLease(
                    worker_id="w1",
                    epoch=1,
                    expires_at=now + timedelta(minutes=5),
                    heartbeat_at=now,
                )
            }
        )

        await queue.complete(str(job.id), 1, state=JobState.SUCCEEDED)

        fresh = await WorkflowNodeRun.get(row.id)
        assert fresh.state is NodeRunState.SUCCEEDED


class TestCollectingOutputs:
    async def test_objects_the_job_produced_become_the_nodes_outputs(self):
        """`produced_by_job` is how a runless node type's outputs are found at
        all -- there is no PipelineRun.outputs to read for 13 of the 22 types."""
        job = Job(type="run_trim", owner=OWNER, state=JobState.SUCCEEDED)
        await job.insert()
        produced = DataObject(
            name="trimmed.fq",
            project_id=PydanticObjectId(),
            owner=OWNER,
            format=FormatInfo(kind=FormatKind.FASTQ),
            role=ObjectRole.TRIMMED_READS,
            produced_by_job=job.id,
        )
        await produced.insert()
        run, row = await _node_run(NodeRunState.RUNNING, job)

        await workflow_hook.on_job_finished(job.id, succeeded=True)

        fresh = await WorkflowNodeRun.get(row.id)
        assert fresh.outputs == [produced.id]

    async def test_sidecars_are_not_collected_as_outputs(self):
        """Found against the real database, not by review: of 70 jobs with
        objects attributed to them, 31 produce more than one -- and several
        produce 6, 9, or 16, *all* of which are sidecars (.fai, .mmi, aligner
        index files). Returning those as binding candidates makes every
        multi-output node ambiguous and stalls the graph, for objects that are
        biologically inert and were never anyone's output."""
        job = Job(type="run_align", owner=OWNER, state=JobState.SUCCEEDED)
        await job.insert()
        real = DataObject(
            name="aligned.bam",
            project_id=PydanticObjectId(),
            owner=OWNER,
            format=FormatInfo(kind=FormatKind.BAM),
            role=ObjectRole.ALIGNMENT,
            produced_by_job=job.id,
        )
        await real.insert()
        sidecar = DataObject(
            name="aligned.bam.bai",
            project_id=PydanticObjectId(),
            owner=OWNER,
            format=FormatInfo(kind=FormatKind.UNKNOWN),
            produced_by_job=job.id,
            sidecar_of=real.id,
        )
        await sidecar.insert()
        run, row = await _node_run(NodeRunState.RUNNING, job)

        await workflow_hook.on_job_finished(job.id, succeeded=True)

        fresh = await WorkflowNodeRun.get(row.id)
        assert fresh.outputs == [real.id]

    async def test_paired_outputs_are_ordered_by_read_number(self):
        """The other real-data finding: a real trim job produces
        `_1.trimmed.fastq` and `_2.trimmed.fastq`, both FASTQ/TRIMMED_READS and
        so indistinguishable by type. `read_number` is what tells them apart,
        and R1 must come first -- an aligner handed the mates backwards is a
        silent wrong answer, not an error."""
        job = Job(type="run_trim", owner=OWNER, state=JobState.SUCCEEDED)
        await job.insert()
        project = PydanticObjectId()
        r2 = DataObject(
            name="s_2.trimmed.fastq",
            project_id=project,
            owner=OWNER,
            format=FormatInfo(kind=FormatKind.FASTQ),
            role=ObjectRole.TRIMMED_READS,
            produced_by_job=job.id,
            read_number=2,
        )
        await r2.insert()
        r1 = DataObject(
            name="s_1.trimmed.fastq",
            project_id=project,
            owner=OWNER,
            format=FormatInfo(kind=FormatKind.FASTQ),
            role=ObjectRole.TRIMMED_READS,
            produced_by_job=job.id,
            read_number=1,
            mate_object_id=r2.id,
        )
        await r1.insert()
        await r2.set({DataObject.mate_object_id: r1.id})
        run, row = await _node_run(NodeRunState.RUNNING, job)

        await workflow_hook.on_job_finished(job.id, succeeded=True)

        fresh = await WorkflowNodeRun.get(row.id)
        assert fresh.outputs == [r1.id, r2.id]

    async def test_a_mate_pair_without_read_numbers_is_ordered_by_filename(self):
        """The bug that stalled a real paired trim -> qc workflow permanently.

        A real trim of `sample_R1.fastq.gz` produces two outputs that are both
        FASTQ/TRIMMED_READS, are linked to each other by `mate_object_id`, and
        carry **`read_number=None`** -- the trim applier links mates without
        setting it (unlike the SRA path at results.py:556). So the
        `read_number` sort had nothing to sort by, the binder saw two
        type-identical candidates and correctly refused to guess, and `qc` sat
        PENDING with `workflow_node_inputs_unresolved missing=['reads']`
        retrying every 10 seconds forever.

        `split_mate` is the same filename convention the rest of the codebase
        pairs by, so R1 leads without inventing a new rule.
        """
        job = Job(type="run_trim", owner=OWNER, state=JobState.SUCCEEDED)
        await job.insert()
        project = PydanticObjectId()

        def fastq(name):
            return DataObject(
                name=name,
                project_id=project,
                owner=OWNER,
                format=FormatInfo(kind=FormatKind.FASTQ),
                role=ObjectRole.TRIMMED_READS,
                produced_by_job=job.id,
            )

        # Inserted R2 first, so a stable-sort no-op would leave it leading.
        r2 = fastq("sample_R2.trimmed.fastq.gz")
        await r2.insert()
        r1 = fastq("sample_R1.trimmed.fastq.gz")
        await r1.insert()
        await r1.set({DataObject.mate_object_id: r2.id})
        await r2.set({DataObject.mate_object_id: r1.id})
        run, row = await _node_run(NodeRunState.RUNNING, job)

        await workflow_hook.on_job_finished(job.id, succeeded=True)

        fresh = await WorkflowNodeRun.get(row.id)
        assert fresh.outputs == [r1.id, r2.id]

    async def test_another_jobs_objects_are_not_collected(self):
        job = Job(type="run_trim", owner=OWNER, state=JobState.SUCCEEDED)
        await job.insert()
        other = Job(type="run_trim", owner=OWNER, state=JobState.SUCCEEDED)
        await other.insert()
        stranger = DataObject(
            name="other.fq",
            project_id=PydanticObjectId(),
            owner=OWNER,
            format=FormatInfo(kind=FormatKind.FASTQ),
            produced_by_job=other.id,
        )
        await stranger.insert()
        run, row = await _node_run(NodeRunState.RUNNING, job)

        await workflow_hook.on_job_finished(job.id, succeeded=True)

        fresh = await WorkflowNodeRun.get(row.id)
        assert stranger.id not in fresh.outputs
