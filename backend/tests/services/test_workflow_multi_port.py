"""Multi-valued input ports: several wires into one port.

The rule these exercise is deliberately narrow -- only the
one-wire-per-port check relaxes. Type checking still applies to every wire
independently, which is the half that would be easy to lose.
"""

import pytest
from beanie import PydanticObjectId, init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.job import Job, JobState
from app.models.object import FormatKind, ObjectRole
from app.models.workflow import (
    PortType,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.pipelines.node_types import NODE_TYPES, PortSpec
from app.services import workflow_orchestrator as orch
from app.services.workflow_service import validate_definition

OWNER = "tester"


@pytest.fixture
async def _fresh_beanie(monkeypatch):
    """A function-scoped Beanie connection, for the one test below that does
    real I/O through `launch_workflow`.

    `beanie_models` (used by every other test in this file) is module-scoped,
    bound to whatever event loop was live the first time it ran -- fine for
    the rest of this file's tests, which are pure-function assertions against
    unsaved documents. `launch_workflow` actually writes, and pytest-asyncio's
    default function-scoped loop means a module-scoped connection here would
    be attached to a different loop than this test runs on (Motor raises
    "attached to a different loop" the instant a query touches it -- the
    exact trap CLAUDE.md's AI-feature section describes for the same reason).
    A dedicated per-test connection avoids the mismatch entirely.
    """
    client = AsyncMongoClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=ALL_MODELS)
    monkeypatch.setattr("app.db.client.get_db", lambda: db)
    yield
    await client.close()

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


from beanie import PydanticObjectId

from app.services.workflow_binding import OutputCandidate, bind_downstream_inputs


def test_multi_port_binds_a_list():
    """Two upstream nodes feeding one multi port produce a list, not a
    last-writer-wins scalar."""
    definition = WorkflowDefinition(
        name="chunked reads",
        owner="tester",
        nodes=[
            WorkflowNode(
                node_id="dl", kind=WorkflowNodeKind.ACTION, node_type="download_sra"
            ),
            WorkflowNode(
                node_id="align_1", kind=WorkflowNodeKind.ACTION, node_type="align"
            ),
        ],
        edges=[
            WorkflowEdge(
                from_node="dl", from_port="reads", to_node="align_1", to_port="reads"
            ),
        ],
    )
    first, second = PydanticObjectId(), PydanticObjectId()
    bound = bind_downstream_inputs(
        definition,
        "dl",
        [
            OutputCandidate(object_id=first, format=FormatKind.FASTQ, name="chunk1.fq"),
            OutputCandidate(object_id=second, format=FormatKind.FASTQ, name="chunk2.fq"),
        ],
    )
    assert bound[("align_1", "reads")] == [first, second]


def test_scalar_port_still_binds_a_bare_id():
    """The list shape is per-port, so existing consumers are untouched."""
    definition = WorkflowDefinition(
        name="one bam",
        owner="tester",
        nodes=[
            WorkflowNode(
                node_id="align_1", kind=WorkflowNodeKind.ACTION, node_type="align"
            ),
            WorkflowNode(
                node_id="stats", kind=WorkflowNodeKind.ACTION, node_type="bam_stats"
            ),
        ],
        edges=[
            WorkflowEdge(
                from_node="align_1",
                from_port="alignment",
                to_node="stats",
                to_port="alignment",
            ),
        ],
    )
    bam = PydanticObjectId()
    bound = bind_downstream_inputs(
        definition,
        "align_1",
        [
            OutputCandidate(
                object_id=bam,
                format=FormatKind.BAM,
                role=ObjectRole.ALIGNMENT,
                name="x.bam",
            )
        ],
    )
    assert bound[("stats", "alignment")] == bam


@pytest.fixture
def launches(monkeypatch):
    """Record every launch instead of running a tool.

    Same shape as `test_workflow_orchestrator.py`'s fixture of the same name
    -- kept local rather than shared because that module's fixture is not
    exported, and duplicating a five-line fixture is cheaper than wiring up a
    cross-module import for it.
    """
    calls: list[tuple[str, dict]] = []

    async def fake_launch(node_type: str, *, inputs: dict, params: dict, owner: str):
        calls.append((node_type, inputs))
        job = Job(type=f"run_{node_type}", owner=owner, state=JobState.PENDING)
        await job.insert()
        return job

    monkeypatch.setattr(orch, "_launch_node", fake_launch)
    return calls


async def test_orchestrator_launches_a_multi_port_with_every_bound_id(
    launches, _fresh_beanie
):
    """The bug this task exists to fix: `_bound_inputs` used to do a plain
    `inputs[edge.to_port] = value` assignment, so the second of two edges
    feeding one multi port silently clobbered the first and `align` launched
    having seen only one read file. Two INPUT nodes wired into `align.reads`
    -- the smallest graph that exercises the accumulate-not-overwrite path
    end to end, through the real `launch_workflow` entry point rather than
    calling the private `_bound_inputs` helper directly.
    """
    definition = WorkflowDefinition(
        name="two input reads",
        owner=OWNER,
        nodes=[
            WorkflowNode(node_id="r1", kind=WorkflowNodeKind.INPUT, label="r1"),
            WorkflowNode(node_id="r2", kind=WorkflowNodeKind.INPUT, label="r2"),
            WorkflowNode(node_id="ref", kind=WorkflowNodeKind.INPUT, label="ref"),
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
    await definition.insert()

    r1_id, r2_id, ref_id = PydanticObjectId(), PydanticObjectId(), PydanticObjectId()
    await orch.launch_workflow(
        definition_id=definition.id,
        project_id=PydanticObjectId(),
        bindings={"r1": r1_id, "r2": r2_id, "ref": ref_id},
        owner=OWNER,
        label="test run",
    )

    [(node_type, inputs)] = launches
    assert node_type == "align"
    assert inputs["reads"] == [r1_id, r2_id]
