# Workflow DAG: models, registry, and queue semantics — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist a reusable, typed pipeline-DAG definition and its run instances, with a node-type registry that cannot silently omit a tool, plus the queue change that lets one branch fail without killing the rest.

**Architecture:** Three new Beanie documents (`WorkflowDefinition`, `WorkflowRun`, `WorkflowNodeRun`) alongside an unchanged `PipelineRun`. A hand-maintained `NODE_TYPES` registry adapts BioFlow's 24 differently-shaped `launch_*` functions onto a generic `(inputs, params)` call, guarded by an exhaustiveness test. Wire validation reuses `FormatKind`/`ObjectRole` as port types. No execution/orchestration in this plan — that is the next one.

**Tech Stack:** Python 3.12, Beanie/Motor (MongoDB), Pydantic v2, pytest + pytest-asyncio.

**Scope:** This plan covers spec §8.3 (queue semantics) and §8.1 (models, registry, validation) from
[`docs/superpowers/specs/2026-08-07-workflow-dag-design.md`](../specs/2026-08-07-workflow-dag-design.md).
Orchestration (§8.2), canvas UI (§8.4), and activity presentation (§8.5) are separate plans.

**Testing note — read before Task 1.** Run tests from this worktree with:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Never `docker compose exec api python -m pytest` from a worktree — it silently tests `main`'s code, not this tree's. See CLAUDE.md, "Verifying changes".

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/models/workflow.py` (create) | All three workflow documents + node/edge/port value objects + enums. One file because they change together and are meaningless apart. |
| `backend/app/models/__init__.py` (modify) | Register new documents in `ALL_MODELS` so indexes are created. |
| `backend/app/pipelines/node_types.py` (create) | `NODE_TYPES` registry, `NodeTypeSpec`, `PortSpec`, `EXCLUDED_LAUNCHES`. Lives in `pipelines/` beside `tools.py`, which it resembles. |
| `backend/app/services/workflow_service.py` (create) | Definition CRUD + graph validation (types, cycles, required ports). |
| `backend/tests/models/test_workflow_models.py` (create) | Model shape, derived status, index declarations. |
| `backend/tests/pipelines/test_node_types.py` (create) | Registry exhaustiveness + port declarations. |
| `backend/tests/services/test_workflow_validation.py` (create) | Wire type rules, cycle detection, required-input rules. |
| `backend/app/queue/queue.py` (modify) | `classify_dependencies` gains failure-tolerance awareness. |
| `backend/tests/queue/test_dependencies.py` (modify) | Cases for the tolerant path. |

---

## Task 1: Queue — tolerate a failed dependency when asked

Spec §1.3/§8.3. Landing this first because §8.2's orchestration depends on it, and it is isolated.

`classify_dependencies` is pure and already tested without a database, which makes it the right seam. Today it returns `(unfinished, failed)` and every caller treats a non-empty `failed` as fatal. We add an opt-in set of job ids whose failure is tolerable.

**Files:**
- Modify: `backend/app/queue/queue.py:177-210`
- Test: `backend/tests/queue/test_dependencies.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/queue/test_dependencies.py`:

```python
class TestTolerantDependencies:
    """`continue_on_failure` nodes: a dependency whose failure must not
    cascade.

    The workflow case this exists for is a QC node feeding a downstream step.
    QC failing means we lack a report, not that the assembly behind it is
    unusable -- the same judgement `OPTIONAL_ROLES` already encodes for runs.
    """

    def test_a_tolerated_failure_does_not_fail_the_dependent(self):
        dep = make_job(JobState.FAILED)
        unfinished, failed = classify_dependencies(
            [dep], tolerate_failure_of={dep.id}
        )
        assert unfinished == []
        assert failed == []

    def test_an_untolerated_failure_still_fails_the_dependent(self):
        """Tolerance is per-id, not a global switch."""
        tolerated = make_job(JobState.FAILED)
        fatal = make_job(JobState.FAILED)
        unfinished, failed = classify_dependencies(
            [tolerated, fatal], tolerate_failure_of={tolerated.id}
        )
        assert [j.id for j in failed] == [fatal.id]

    def test_a_tolerated_dependency_still_blocks_while_active(self):
        """Tolerating failure is not the same as not waiting. A running QC
        node still has to finish before its dependent starts, or the dependent
        races the file QC is reading."""
        dep = make_job(JobState.RUNNING)
        unfinished, failed = classify_dependencies(
            [dep], tolerate_failure_of={dep.id}
        )
        assert len(unfinished) == 1
        assert failed == []

    def test_default_is_unchanged(self):
        """Omitting the argument must behave exactly as before -- every
        existing caller relies on it."""
        unfinished, failed = classify_dependencies([make_job(JobState.FAILED)])
        assert len(failed) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/queue/test_dependencies.py -v
```

Expected: the four new tests FAIL with `TypeError: classify_dependencies() got an unexpected keyword argument 'tolerate_failure_of'`. Pre-existing tests in the file PASS.

- [ ] **Step 3: Implement**

In `backend/app/queue/queue.py`, replace the `classify_dependencies` signature and body:

```python
def classify_dependencies(
    jobs: list[Job],
    *,
    tolerate_failure_of: set[PydanticObjectId] | None = None,
) -> tuple[list[Job], list[Job]]:
    """Split dependency jobs into (unfinished, failed).

    Pure, so the release decision can be tested without a database -- the
    decision itself is the part worth getting right, and it is easy to get
    subtly wrong in a way only a rare interleaving reveals.

    A dependency id with no job behind it is neither unfinished nor failed: the
    record was pruned by the 30-day TTL, or never existed. Treating a missing
    dependency as failed would break exactly the long-lived chains the TTL is
    most likely to touch.

    `tolerate_failure_of` names dependencies whose failure must not cascade --
    workflow nodes marked `continue_on_failure`. It is a set of ids rather than
    a boolean because tolerance is per-edge: a node may depend on both an
    optional QC step and a mandatory alignment, and only the first is
    survivable. Note that a tolerated dependency still *blocks* while active;
    tolerating a failure is not the same as not waiting for the work.
    """
    tolerated = tolerate_failure_of or set()
    unfinished: list[Job] = []
    failed: list[Job] = []
    for job in jobs:
        if job.state in ACTIVE_STATES:
            unfinished.append(job)
        elif job.state != JobState.SUCCEEDED and job.id not in tolerated:
            failed.append(job)
    return unfinished, failed
```

Verify `ACTIVE_STATES` is imported in `queue.py`; if not, add it to the existing `from app.models import ...` line.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/test_dependencies.py -v
```

Expected: PASS, all tests in the file.

- [ ] **Step 5: Run the whole queue suite for regressions**

```bash
./backend/run-worktree-tests.sh tests/queue/ -q
```

Expected: no failures. Read the count, not just the exit code.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/queue.py backend/tests/queue/test_dependencies.py
git commit -m "feat(queue): let a dependent tolerate named dependencies' failure"
```

---

## Task 2: Workflow enums and port types

**Files:**
- Create: `backend/app/models/workflow.py`
- Test: `backend/tests/models/test_workflow_models.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/models/test_workflow_models.py`:

```python
"""The persisted workflow graph.

Port types reuse FormatKind/ObjectRole rather than a parallel vocabulary,
because the rule they enforce already exists: ObjectRole.PROTEIN is commented
in models/object.py as the thing that keeps a protein FASTA out of the
aligner's reference picker. A canvas refusing that wire is that same rule.
"""

from app.models.workflow import PortType, WorkflowNodeKind
from app.models import FormatKind, ObjectRole


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
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/models/test_workflow_models.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.workflow'`.

- [ ] **Step 3: Implement**

Create `backend/app/models/workflow.py`:

```python
"""Reusable user-defined pipeline DAGs: definition, run, and node instance.

A definition is a *saved graph*, deliberately project-independent and holding
no object ids -- it says what to do, never which files. Binding real objects
happens per run.

This is the third concept describing "work that happened", and the boundaries
matter. `Job` is a queue unit. `PipelineRun` is one launch, one user intent,
and its docstring's test ("does this describe a user's request or the machine's
plan?") is why graph structure lives here instead of being bolted onto it. A
`WorkflowRun` parents ordinary `PipelineRun`s rather than replacing them, which
is what lets the activity view, provenance panel and suggestion cards keep
working on workflow-produced runs unchanged -- they see ordinary runs, because
that is what they are.

Design note: docs/superpowers/specs/2026-08-07-workflow-dag-design.md
"""

from enum import StrEnum

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models.base import TimestampedDocument
from app.models.object import FormatKind, ObjectRole


class WorkflowNodeKind(StrEnum):
    # A declared input slot. The workflow's parameters are exactly its INPUT
    # nodes -- see the design note's "Inputs are explicit nodes".
    INPUT = "input"
    # A pipeline action: one launch, which fans out into several jobs.
    ACTION = "action"


class PortType(BaseModel):
    """What may flow down a wire.

    Reuses the two enums that already describe a file rather than inventing a
    parallel vocabulary. `role=None` means "any role for this format", which is
    the honest type for a port like QC's that genuinely does not care.
    """

    format: FormatKind
    role: ObjectRole | None = None

    def accepts(self, format: FormatKind, role: ObjectRole | None) -> bool:
        """Whether an object of this format/role may connect here.

        A required role is not satisfied by an absent one. An object with no
        role has not declared its intent, and treating that as a match is
        exactly the guess `ObjectRole` exists to prevent -- it is how a
        protein FASTA reaches an aligner's reference port.
        """
        if self.format != format:
            return False
        if self.role is None:
            return True
        return self.role == role
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/models/test_workflow_models.py -v
```

Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/workflow.py backend/tests/models/test_workflow_models.py
git commit -m "feat(models): workflow port types and node kinds"
```

---

## Task 3: `WorkflowDefinition` document

**Files:**
- Modify: `backend/app/models/workflow.py`
- Test: `backend/tests/models/test_workflow_models.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/models/test_workflow_models.py`:

```python
import pytest

from app.models.workflow import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
)


class TestWorkflowDefinition:
    @pytest.mark.asyncio
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
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/models/test_workflow_models.py -v
```

Expected: FAIL with `ImportError: cannot import name 'WorkflowDefinition'`.

- [ ] **Step 3: Implement**

Append to `backend/app/models/workflow.py`:

```python
class NodePosition(BaseModel):
    """Canvas coordinates. Presentation only -- never read by execution."""

    x: float = 0.0
    y: float = 0.0


class WorkflowNode(BaseModel):
    # Stable across edits, so a run instance keeps pointing at the right node
    # after the graph is rearranged. Not the array index, which shifts.
    node_id: str
    kind: WorkflowNodeKind
    # ACTION only: key into pipelines/node_types.NODE_TYPES. Deliberately a
    # str rather than a RunKind -- only 9 of 24 launch_* functions create a
    # PipelineRun at all, so keying on RunKind would make every QC node
    # unrepresentable. See the design note, "Why it is keyed by its own
    # string".
    node_type: str | None = None
    params: dict = Field(default_factory=dict)
    # Per node rather than global: a graph usually has exactly one or two
    # steps whose failure is survivable (QC, stats), and the rest are load
    # bearing. Mirrors OPTIONAL_ROLES in run.py.
    continue_on_failure: bool = False
    position: NodePosition = Field(default_factory=NodePosition)
    # INPUT only. The label is why input nodes are explicit rather than
    # implied by an unwired port: "tumor reads" and "normal reads" are the
    # same type and only a name tells them apart.
    label: str | None = None
    accepts: PortType | None = None


class WorkflowEdge(BaseModel):
    from_node: str
    from_port: str
    to_node: str
    to_port: str


class WorkflowDefinition(TimestampedDocument):
    """A saved graph. No project, no object ids -- see the module docstring.

    Ports are *not* stored on nodes; they are declared by NODE_TYPES and
    looked up by `node_type`. Storing them would mean a definition saved today
    keeps stale ports after a tool gains an input.
    """

    name: str
    description: str = ""
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    # Incremented on edit. A WorkflowRun pins the version it ran, for the same
    # reason PipelineRun denormalizes its input names: a record whose meaning
    # dissolves when its source changes is not a record.
    version: int = 1

    class Settings:
        name = "workflow_definitions"
        indexes = [
            IndexModel([("owner", ASCENDING), ("name", ASCENDING)], name="by_owner_name"),
            IndexModel([("updated_at", DESCENDING)], name="recent"),
        ]
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/models/test_workflow_models.py -v
```

Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/workflow.py backend/tests/models/test_workflow_models.py
git commit -m "feat(models): WorkflowDefinition document"
```

---

## Task 4: `WorkflowRun`, `WorkflowNodeRun`, and derived status

**Files:**
- Modify: `backend/app/models/workflow.py`
- Test: `backend/tests/models/test_workflow_models.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/models/test_workflow_models.py`:

```python
from app.models.workflow import (
    NodeRunState,
    WorkflowBinding,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowStatus,
    derive_status,
)


class TestDerivedStatus:
    """Status is computed from node states, never stored -- following
    RunStatus's docstring. A stored status is a second source of truth that
    drifts the first time a write is lost."""

    def test_nothing_started_is_waiting(self):
        assert derive_status([NodeRunState.PENDING, NodeRunState.PENDING]) is WorkflowStatus.WAITING

    def test_any_running_is_running(self):
        assert derive_status([NodeRunState.SUCCEEDED, NodeRunState.RUNNING]) is WorkflowStatus.RUNNING

    def test_all_succeeded_is_succeeded(self):
        assert derive_status([NodeRunState.SUCCEEDED, NodeRunState.SUCCEEDED]) is WorkflowStatus.SUCCEEDED

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
    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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
    @pytest.mark.asyncio
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
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/models/test_workflow_models.py -v
```

Expected: FAIL with `ImportError: cannot import name 'NodeRunState'`.

- [ ] **Step 3: Implement**

Append to `backend/app/models/workflow.py`:

```python
class NodeRunState(StrEnum):
    # Not yet launched -- either waiting on an upstream node, or the run has
    # only just been created. Distinct from the queue's own PENDING: a node
    # has no job at all yet.
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Upstream failed and this node will never run. Distinct from CANCELLED,
    # which is a user action -- the two read differently in a UI, and only
    # this one is resolved by retrying something else.
    SKIPPED = "skipped"


_ACTIVE_NODE_STATES = frozenset({NodeRunState.PENDING, NodeRunState.RUNNING})
_UNSUCCESSFUL_NODE_STATES = frozenset(
    {NodeRunState.FAILED, NodeRunState.CANCELLED, NodeRunState.SKIPPED}
)


class WorkflowStatus(StrEnum):
    """Derived from node states, never stored.

    Same reasoning as RunStatus: a stored status is a second source of truth
    about something the node runs already know, and it drifts the first time a
    write is lost. This enum is the vocabulary of the API rather than a column.
    """

    WAITING = "waiting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Finished with real outputs and at least one failure. The branch-scoped
    # failure rule makes this the common ending for a graph with an optional
    # QC leaf, not an exotic one.
    PARTIAL = "partial"


def derive_status(states: list[NodeRunState]) -> WorkflowStatus:
    """Roll node states up into one workflow status.

    Pure and total, so the rule can be tested without a database -- the same
    reason `classify_dependencies` is pure.

    Order matters: "anything still active" is checked before any verdict on
    failures, because a run holding a failed node *and* a pending one is still
    live. Calling it PARTIAL there would report a running workflow as finished.
    """
    if not states:
        return WorkflowStatus.WAITING
    if all(s is NodeRunState.PENDING for s in states):
        return WorkflowStatus.WAITING
    if any(s in _ACTIVE_NODE_STATES for s in states):
        return WorkflowStatus.RUNNING
    if all(s is NodeRunState.SUCCEEDED for s in states):
        return WorkflowStatus.SUCCEEDED
    if any(s is NodeRunState.SUCCEEDED for s in states):
        return WorkflowStatus.PARTIAL
    return WorkflowStatus.FAILED


class WorkflowBinding(BaseModel):
    """One INPUT node bound to one real object, for one run."""

    node_id: str
    object_id: PydanticObjectId
    # Denormalized for the reason RunInput.name is: a run must stay readable
    # after its inputs are deleted.
    name: str


class WorkflowRun(TimestampedDocument):
    definition_id: PydanticObjectId
    # Pinned, not looked up: the definition may be edited after this ran, and
    # a run described by a graph it did not execute is worse than no record.
    definition_version: int
    project_id: PydanticObjectId
    label: str
    bindings: list[WorkflowBinding] = Field(default_factory=list)

    class Settings:
        name = "workflow_runs"
        indexes = [
            IndexModel(
                [("project_id", ASCENDING), ("created_at", DESCENDING)],
                name="project_listing",
            ),
            IndexModel([("definition_id", ASCENDING)], name="by_definition"),
        ]


class WorkflowNodeRun(TimestampedDocument):
    """One node's execution within one workflow run.

    A separate document rather than an array on WorkflowRun, because retry
    enqueues *new* work: a DEAD job cannot be revived, so retrying a node
    creates a new job and a new PipelineRun and points a new attempt at them,
    while succeeded siblings keep their original links. An array would force
    that history to be overwritten. This is RunJob's link-collection shape one
    level up.
    """

    workflow_run_id: PydanticObjectId
    node_id: str
    # The PipelineRun this node produced. None until launched -- and for an
    # INPUT node, forever: an input binds a file, it does not run anything.
    run_id: PydanticObjectId | None = None
    attempt: int = 1
    state: NodeRunState = NodeRunState.PENDING
    outputs: list[PydanticObjectId] = Field(default_factory=list)

    class Settings:
        name = "workflow_node_runs"
        indexes = [
            IndexModel([("workflow_run_id", ASCENDING)], name="by_workflow_run"),
            # One row per (run, node, attempt): a retry must not duplicate a
            # member and double-count it in the derived status.
            IndexModel(
                [
                    ("workflow_run_id", ASCENDING),
                    ("node_id", ASCENDING),
                    ("attempt", ASCENDING),
                ],
                name="uniq_node_attempt",
                unique=True,
            ),
        ]
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/models/test_workflow_models.py -v
```

Expected: PASS (20 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/workflow.py backend/tests/models/test_workflow_models.py
git commit -m "feat(models): WorkflowRun, WorkflowNodeRun, derived status"
```

---

## Task 5: Register the documents so indexes are created

Adding a model to `ALL_MODELS` is what creates its indexes — the unique index in Task 4 does not exist until this lands.

**Files:**
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/models/test_workflow_models.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/models/test_workflow_models.py`:

```python
class TestRegistration:
    def test_workflow_documents_are_registered(self):
        """Unregistered documents get no indexes -- including the unique one
        that stops a retry double-counting."""
        from app.models import ALL_MODELS

        for model in (WorkflowDefinition, WorkflowRun, WorkflowNodeRun):
            assert model in ALL_MODELS
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/models/test_workflow_models.py::TestRegistration -v
```

Expected: FAIL — `assert WorkflowDefinition in ALL_MODELS`.

- [ ] **Step 3: Implement**

In `backend/app/models/__init__.py`, add the import beside the `run` import:

```python
from app.models.workflow import (
    NodePosition,
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
```

Then add `WorkflowDefinition`, `WorkflowRun`, and `WorkflowNodeRun` to the `ALL_MODELS` list, and add every imported name above to `__all__` if the module defines one. Check both:

```bash
grep -n "ALL_MODELS\s*=\|__all__" backend/app/models/__init__.py
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/models/test_workflow_models.py -q
```

Expected: PASS (21 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/__init__.py backend/tests/models/test_workflow_models.py
git commit -m "feat(models): register workflow documents in ALL_MODELS"
```

---

## Task 6: Node-type registry with its exhaustiveness test

The load-bearing component. Its test is the only thing standing between a new tool and silent absence from the canvas — the same failure shape as the STAR `_SIDECAR_ROLES` bug that cost a `build_index` job its eight index files while the suite stayed green.

**Files:**
- Create: `backend/app/pipelines/node_types.py`
- Test: `backend/tests/pipelines/test_node_types.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_node_types.py`:

```python
"""The canvas node registry.

This file's most important test is the exhaustiveness one. A launch_* function
absent from both NODE_TYPES and EXCLUDED_LAUNCHES is a tool that installs
cleanly, passes every other test, and simply never appears on the canvas --
the STAR/_SIDECAR_ROLES failure in a new place.
"""

import inspect

from app.models import FormatKind, ObjectRole
from app.pipelines.node_types import (
    EXCLUDED_LAUNCHES,
    NODE_TYPES,
    launch_function_names,
)


class TestExhaustiveness:
    def test_every_launch_function_is_classified(self):
        """Every launch_* either has a node type or is explicitly excluded.

        If this fails after you added a launcher: add a NODE_TYPES entry, or
        add it to EXCLUDED_LAUNCHES *with a comment saying why*. Do not delete
        the assertion.
        """
        classified = {spec.launch_name for spec in NODE_TYPES.values()} | EXCLUDED_LAUNCHES
        assert launch_function_names() == classified

    def test_exclusions_are_real_functions(self):
        """A typo'd exclusion silently stops guarding anything."""
        assert EXCLUDED_LAUNCHES <= launch_function_names()

    def test_no_launcher_is_both_used_and_excluded(self):
        used = {spec.launch_name for spec in NODE_TYPES.values()}
        assert not (used & EXCLUDED_LAUNCHES)


class TestSpecs:
    def test_every_spec_declares_a_callable_launch(self):
        for key, spec in NODE_TYPES.items():
            assert callable(spec.launch), f"{key} has no callable launch adapter"

    def test_every_spec_has_a_label(self):
        """The palette renders these; a blank one is an unusable node."""
        for key, spec in NODE_TYPES.items():
            assert spec.label.strip(), f"{key} has no label"

    def test_port_names_are_unique_within_a_spec(self):
        """Output->port resolution is by declared name, so duplicates make it
        ambiguous."""
        for key, spec in NODE_TYPES.items():
            names = [p.name for p in spec.inputs]
            assert len(names) == len(set(names)), f"{key} has duplicate input ports"
            out_names = [p.name for p in spec.outputs]
            assert len(out_names) == len(set(out_names)), f"{key} has duplicate outputs"

    def test_align_declares_a_reference_port_that_rejects_protein(self):
        """The concrete rule the typing exists for."""
        spec = NODE_TYPES["align"]
        reference = next(p for p in spec.inputs if p.name == "reference")
        assert reference.type.accepts(FormatKind.FASTA, ObjectRole.REFERENCE)
        assert not reference.type.accepts(FormatKind.FASTA, ObjectRole.PROTEIN)

    def test_trim_consumes_fastq_and_produces_trimmed_reads(self):
        spec = NODE_TYPES["trim"]
        reads = next(p for p in spec.inputs if p.name == "reads")
        assert reads.type.accepts(FormatKind.FASTQ, None)
        out = spec.outputs[0]
        assert out.type.role is ObjectRole.TRIMMED_READS


class TestAdapterSignatures:
    def test_every_adapter_takes_inputs_and_params(self):
        """The registry's whole purpose is presenting 24 differently-shaped
        launchers behind one call shape."""
        for key, spec in NODE_TYPES.items():
            sig = inspect.signature(spec.launch)
            assert {"inputs", "params", "owner"} <= set(sig.parameters), (
                f"{key}'s adapter does not take (inputs, params, owner)"
            )
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipelines.node_types'`.

- [ ] **Step 3: Implement**

Create `backend/app/pipelines/node_types.py`. Start with the scaffolding and **three** node types (`trim`, `align`, `qc`); the remaining entries are Task 7.

```python
"""What a canvas node can be, and how it launches.

Every canvas capability reads from here: which nodes exist, which ports they
expose, which wires validate, and how a node actually runs.

Keyed by its own string rather than by RunKind, which was the obvious choice
and is wrong. Measured on main at design time: 24 launch_* functions exist
across services/, and only 9 create a PipelineRun -- every QC and stats
launcher enqueues jobs with no run record, and RunKind.REFERENCE_ASSEMBLY has
no launcher at all. Keying on RunKind would make most QC nodes unrepresentable,
which are precisely the nodes a user wants as continue_on_failure leaves.
`run_kind` is therefore an attribute of a spec, not its identity.

Per CLAUDE.md's rules for hand-maintained registries: this is the third
category, where the keys belong to a set outside any single enum, so full
derivation is impossible. The checkable invariant runs the other direction --
every launch_* is either here or in EXCLUDED_LAUNCHES, asserted in
tests/pipelines/test_node_types.py. Without that test, a new tool is silently
absent from the canvas.
"""

from collections.abc import Callable
from dataclasses import dataclass

from beanie import PydanticObjectId

from app.models.object import FormatKind, ObjectRole
from app.models.run import RunKind
from app.models.workflow import PortType
from app.services import pipeline_service


@dataclass(frozen=True)
class PortSpec:
    name: str
    type: PortType
    required: bool = True


@dataclass(frozen=True)
class NodeTypeSpec:
    label: str
    # The launch_* function this adapts, by name. Stored so the exhaustiveness
    # test can compare against what actually exists in services/ rather than
    # against a second hand-written list that would drift.
    launch_name: str
    launch: Callable
    inputs: tuple[PortSpec, ...]
    outputs: tuple[PortSpec, ...]
    # None where the launcher creates no PipelineRun -- true of 15 of the 24.
    run_kind: RunKind | None = None


async def _launch_trim(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_trim(
        object_id=inputs["reads"],
        mate_id=inputs.get("mate"),
        params=params,
        owner=owner,
    )


async def _launch_align(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_alignment(
        reads_id=inputs["reads"],
        mate_id=inputs.get("mate"),
        reference_id=inputs["reference"],
        params=params,
        owner=owner,
    )


async def _launch_qc(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_qc(
        object_id=inputs["reads"], owner=owner
    )


NODE_TYPES: dict[str, NodeTypeSpec] = {
    "trim": NodeTypeSpec(
        label="Trim reads",
        launch_name="launch_trim",
        launch=_launch_trim,
        run_kind=RunKind.TRIM,
        inputs=(
            PortSpec("reads", PortType(format=FormatKind.FASTQ)),
            PortSpec("mate", PortType(format=FormatKind.FASTQ), required=False),
        ),
        outputs=(
            PortSpec(
                "trimmed",
                PortType(format=FormatKind.FASTQ, role=ObjectRole.TRIMMED_READS),
            ),
        ),
    ),
    "align": NodeTypeSpec(
        label="Align to reference",
        launch_name="launch_alignment",
        launch=_launch_align,
        run_kind=RunKind.ALIGNMENT,
        inputs=(
            PortSpec("reads", PortType(format=FormatKind.FASTQ)),
            PortSpec("mate", PortType(format=FormatKind.FASTQ), required=False),
            # The role is required here, and it is the whole point: a protein
            # FASTA and a genome are both FormatKind.FASTA.
            PortSpec(
                "reference",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
        ),
        outputs=(
            PortSpec(
                "alignment",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
            ),
        ),
    ),
    "qc": NodeTypeSpec(
        label="Read QC",
        launch_name="launch_qc",
        # Creates no PipelineRun -- one of the 15.
        run_kind=None,
        launch=_launch_qc,
        inputs=(PortSpec("reads", PortType(format=FormatKind.FASTQ)),),
        outputs=(),
    ),
}


# Launchers deliberately not offered as canvas nodes. Each needs a reason --
# an entry without one is indistinguishable from an oversight.
EXCLUDED_LAUNCHES: frozenset[str] = frozenset(
    {
        # Auto-attached by launch_alignment (pipeline_service.py:1550 calls
        # _enqueue_build_index itself). A separate node would let a user build
        # a graph that indexes twice, or not at all.
        "launch_build_index",
        # AI annotations over an existing object, not pipeline steps. They
        # produce a summary field rather than an object a downstream node
        # could consume, so they have no output port to wire.
        "launch_summary",
        "launch_de_summary",
        "launch_variant_summary",
    }
)


def launch_function_names() -> set[str]:
    """Every `launch_*` defined in the services layer.

    Discovered by inspection rather than listed, so the exhaustiveness test
    compares against reality instead of against a second hand-written list
    that would drift from the first.
    """
    import inspect

    from app.services import ncbi_assembly_service, pipeline_service as ps

    names: set[str] = set()
    for module in (ps, ncbi_assembly_service):
        for name, obj in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("launch_") and obj.__module__ == module.__name__:
                names.add(name)
    return names
```

- [ ] **Step 4: Run and expect a specific, informative failure**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -v
```

Expected: most tests PASS; `test_every_launch_function_is_classified` FAILS, listing the ~17 launchers not yet classified. **This is the test doing its job** — it is the signal Task 7 works from. Copy the reported names.

If the adapter keyword names in `_launch_trim`/`_launch_align` do not match the real signatures, you will see a `TypeError` instead. Check the true signatures and fix the adapters:

```bash
sed -n '193,215p;1412,1440p' backend/app/services/pipeline_service.py
```

- [ ] **Step 5: Commit the scaffolding**

```bash
git add backend/app/pipelines/node_types.py backend/tests/pipelines/test_node_types.py
git commit -m "feat(pipelines): node-type registry scaffolding with exhaustiveness test"
```

---

## Task 7: Classify every remaining launcher

Drive this loop directly from Task 6's failing test until it passes.

**Files:**
- Modify: `backend/app/pipelines/node_types.py`

- [ ] **Step 1: List what is unclassified**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py::TestExhaustiveness -v
```

The assertion diff names every unclassified launcher. Expect roughly: `launch_variant_calling`, `launch_quantify`, `launch_differential_expression`, `launch_assembly`, `launch_consensus`, `launch_polish`, `launch_scaffold`, `launch_annotation`, `launch_bam_stats`, `launch_vcf_stats`, `launch_completeness`, `launch_misassembly_qc`, `launch_assembly_error_qc`, `launch_qv_qc`, `launch_continuity_qc`, `launch_lineage_download`, `launch_download`.

- [ ] **Step 2: Read each launcher's real signature before adapting it**

For each name from Step 1:

```bash
grep -n "async def <name>" -A 25 backend/app/services/pipeline_service.py
```

Record: required object-id parameters (these become input ports), what it produces (output ports), and whether it passes a `RunKind`.

- [ ] **Step 3: Add a NODE_TYPES entry or an EXCLUDED_LAUNCHES line**

Use this rule, and write the reason into the code either way:

- **Produces an object a downstream node could consume** → `NODE_TYPES` entry with real ports.
- **Annotates an existing object in place** (stats, QC reports, lineage) → still a `NODE_TYPES` entry with `outputs=()`, because these are exactly the `continue_on_failure` leaves the design is for.
- **Auto-attached by another launcher, or has no wireable output** → `EXCLUDED_LAUNCHES`, with a comment saying which launcher attaches it.

Worked example for variant calling — follow this shape:

```python
    "call_variants": NodeTypeSpec(
        label="Call variants",
        launch_name="launch_variant_calling",
        launch=_launch_variant_calling,
        run_kind=RunKind.VARIANT_CALLING,
        inputs=(
            PortSpec(
                "alignment",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
            ),
            PortSpec(
                "reference",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
        ),
        outputs=(
            PortSpec(
                "variants",
                PortType(format=FormatKind.VCF, role=ObjectRole.VARIANTS),
            ),
        ),
    ),
```

with its adapter beside the others:

```python
async def _launch_variant_calling(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_variant_calling(
        alignment_id=inputs["alignment"],
        reference_id=inputs["reference"],
        params=params,
        owner=owner,
    )
```

- [ ] **Step 4: Re-run until exhaustive**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -v
```

Expected: PASS, including `test_every_launch_function_is_classified`.

- [ ] **Step 5: Check the registry against the real database**

Fixtures that mirror the code prove nothing here — CLAUDE.md's "Check a rule against the real database" applies directly. From the **main checkout root**, not this worktree:

```bash
docker compose exec api python -c "
from app.pipelines.node_types import NODE_TYPES
for key, spec in NODE_TYPES.items():
    print(key, '<-', [(p.name, p.type.format.value, p.type.role.value if p.type.role else None) for p in spec.inputs])
"
```

Read the output and confirm each declared port matches what that launcher actually accepts. Fix any that do not.

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/node_types.py
git commit -m "feat(pipelines): classify every launcher as a node type or exclusion"
```

---

## Task 8: Graph validation

**Files:**
- Create: `backend/app/services/workflow_service.py`
- Test: `backend/tests/services/test_workflow_validation.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_workflow_validation.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_workflow_validation.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.workflow_service'`.

- [ ] **Step 3: Implement**

Create `backend/app/services/workflow_service.py`:

```python
"""Workflow definitions: CRUD and graph validation.

Validation returns a list rather than raising on the first problem, because
the canvas marks every bad wire at once -- a builder that reports one error per
save is a builder you fix by trial and error.
"""

from dataclasses import dataclass

from app.models.workflow import (
    WorkflowDefinition,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.pipelines.node_types import NODE_TYPES, NodeTypeSpec, PortSpec


@dataclass(frozen=True)
class ValidationError:
    code: str
    message: str
    node_id: str | None = None
    port: str | None = None


def _spec_for(node: WorkflowNode) -> NodeTypeSpec | None:
    if node.kind is not WorkflowNodeKind.ACTION or node.node_type is None:
        return None
    return NODE_TYPES.get(node.node_type)


def _input_port(spec: NodeTypeSpec, name: str) -> PortSpec | None:
    return next((p for p in spec.inputs if p.name == name), None)


def _output_type(node: WorkflowNode, port_name: str):
    """The PortType flowing out of a node's named output port.

    An INPUT node has exactly one output, `object`, carrying whatever type it
    accepts -- it is a slot, not a computation.
    """
    if node.kind is WorkflowNodeKind.INPUT:
        return node.accepts if port_name == "object" else None
    spec = _spec_for(node)
    if spec is None:
        return None
    port = next((p for p in spec.outputs if p.name == port_name), None)
    return port.type if port else None


def validate_definition(definition: WorkflowDefinition) -> list[ValidationError]:
    errors: list[ValidationError] = []
    by_id: dict[str, WorkflowNode] = {}

    for node in definition.nodes:
        if node.node_id in by_id:
            errors.append(
                ValidationError(
                    "duplicate_node_id",
                    f"More than one node has the id {node.node_id!r}.",
                    node_id=node.node_id,
                )
            )
            continue
        by_id[node.node_id] = node
        if node.kind is WorkflowNodeKind.ACTION and _spec_for(node) is None:
            errors.append(
                ValidationError(
                    "unknown_node_type",
                    f"No node type named {node.node_type!r}.",
                    node_id=node.node_id,
                )
            )

    wired: set[tuple[str, str]] = set()

    for edge in definition.edges:
        source = by_id.get(edge.from_node)
        target = by_id.get(edge.to_node)
        if source is None or target is None:
            missing = edge.from_node if source is None else edge.to_node
            errors.append(
                ValidationError(
                    "unknown_node", f"Edge names a node that does not exist: {missing!r}."
                )
            )
            continue

        spec = _spec_for(target)
        if spec is None:
            continue  # already reported as unknown_node_type

        port = _input_port(spec, edge.to_port)
        if port is None:
            errors.append(
                ValidationError(
                    "unknown_port",
                    f"{target.node_id!r} has no input port {edge.to_port!r}.",
                    node_id=target.node_id,
                    port=edge.to_port,
                )
            )
            continue

        key = (edge.to_node, edge.to_port)
        if key in wired:
            errors.append(
                ValidationError(
                    "duplicate_wire",
                    f"Port {edge.to_port!r} on {target.node_id!r} already has an input.",
                    node_id=target.node_id,
                    port=edge.to_port,
                )
            )
            continue
        wired.add(key)

        produced = _output_type(source, edge.from_port)
        if produced is None:
            errors.append(
                ValidationError(
                    "unknown_port",
                    f"{source.node_id!r} has no output port {edge.from_port!r}.",
                    node_id=source.node_id,
                    port=edge.from_port,
                )
            )
            continue

        if not port.type.accepts(produced.format, produced.role):
            errors.append(
                ValidationError(
                    "type_mismatch",
                    f"{source.node_id!r} produces {produced.format.value}"
                    f"/{produced.role.value if produced.role else 'any'}, which "
                    f"{target.node_id!r}'s {edge.to_port!r} port does not accept.",
                    node_id=target.node_id,
                    port=edge.to_port,
                )
            )

    for node in definition.nodes:
        spec = _spec_for(node)
        if spec is None:
            continue
        for port in spec.inputs:
            if port.required and (node.node_id, port.name) not in wired:
                errors.append(
                    ValidationError(
                        "missing_required_input",
                        f"{node.node_id!r} needs an input on {port.name!r}.",
                        node_id=node.node_id,
                        port=port.name,
                    )
                )

    if _has_cycle(definition):
        errors.append(
            ValidationError("cycle", "The graph contains a cycle and could never run.")
        )

    return errors


def _has_cycle(definition: WorkflowDefinition) -> bool:
    """Depth-first three-colour cycle detection.

    Iterative rather than recursive: a deep graph should report a cycle, not
    exhaust the interpreter's stack.
    """
    adjacency: dict[str, list[str]] = {n.node_id: [] for n in definition.nodes}
    for edge in definition.edges:
        if edge.from_node in adjacency and edge.to_node in adjacency:
            adjacency[edge.from_node].append(edge.to_node)

    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(adjacency, WHITE)

    for start in adjacency:
        if colour[start] != WHITE:
            continue
        stack = [(start, iter(adjacency[start]))]
        colour[start] = GREY
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if colour[child] == GREY:
                    return True
                if colour[child] == WHITE:
                    colour[child] = GREY
                    stack.append((child, iter(adjacency[child])))
                    advanced = True
                    break
            if not advanced:
                colour[node] = BLACK
                stack.pop()
    return False
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_workflow_validation.py -v
```

Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/workflow_service.py backend/tests/services/test_workflow_validation.py
git commit -m "feat(services): workflow graph validation"
```

---

## Task 9: Definition CRUD with version bumping

**Files:**
- Modify: `backend/app/services/workflow_service.py`
- Test: `backend/tests/services/test_workflow_validation.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_workflow_validation.py`:

```python
from app.services.workflow_service import (
    InvalidGraph,
    create_definition,
    update_definition,
)


class TestCrud:
    @pytest.mark.asyncio
    async def test_saving_an_invalid_graph_is_refused(self, beanie_models):
        """Invalid graphs must not reach storage: a saved graph that cannot
        run is a bug that surfaces much later, at launch."""
        with pytest.raises(InvalidGraph) as caught:
            await create_definition(
                name="bad",
                description="",
                nodes=[_input("reads", FormatKind.FASTQ), _action("a", "align")],
                edges=[
                    WorkflowEdge(from_node="reads", from_port="object", to_node="a", to_port="reads")
                ],
                owner="test-owner",
            )
        assert any(e.code == "missing_required_input" for e in caught.value.errors)

    @pytest.mark.asyncio
    async def test_an_edit_bumps_the_version(self, beanie_models):
        """Runs pin a version, so an edit must produce a new one."""
        created = await create_definition(
            name="ok",
            description="",
            nodes=[_input("reads", FormatKind.FASTQ), _action("t", "trim")],
            edges=[
                WorkflowEdge(from_node="reads", from_port="object", to_node="t", to_port="reads")
            ],
            owner="test-owner",
        )
        assert created.version == 1

        updated = await update_definition(
            created.id,
            name="ok, renamed",
            description="",
            nodes=created.nodes,
            edges=created.edges,
        )
        assert updated.version == 2

    @pytest.mark.asyncio
    async def test_a_saved_definition_carries_its_owner(self, beanie_models):
        """Non-'local' on purpose: every document defaults to 'local', so
        asserting that value would prove nothing."""
        created = await create_definition(
            name="owned",
            description="",
            nodes=[_input("reads", FormatKind.FASTQ), _action("t", "trim")],
            edges=[
                WorkflowEdge(from_node="reads", from_port="object", to_node="t", to_port="reads")
            ],
            owner="profile-123",
        )
        assert created.owner == "profile-123"
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_workflow_validation.py::TestCrud -v
```

Expected: FAIL with `ImportError: cannot import name 'InvalidGraph'`.

- [ ] **Step 3: Implement**

Append to `backend/app/services/workflow_service.py`:

```python
class InvalidGraph(Exception):
    """Raised rather than returned, because a caller that ignores a returned
    error list stores an unrunnable graph."""

    def __init__(self, errors: list[ValidationError]):
        self.errors = errors
        super().__init__(f"{len(errors)} validation error(s)")


async def create_definition(
    *,
    name: str,
    description: str,
    nodes: list[WorkflowNode],
    edges: list,
    owner: str,
) -> WorkflowDefinition:
    definition = WorkflowDefinition(
        name=name, description=description, nodes=nodes, edges=edges, owner=owner
    )
    errors = validate_definition(definition)
    if errors:
        raise InvalidGraph(errors)
    await definition.insert()
    return definition


async def update_definition(
    definition_id,
    *,
    name: str,
    description: str,
    nodes: list[WorkflowNode],
    edges: list,
) -> WorkflowDefinition:
    """Replace a definition's graph, bumping its version.

    The version bump is unconditional rather than change-detecting: a
    WorkflowRun pins the version it ran, and a cheap extra version is far
    better than two different graphs sharing one.
    """
    definition = await WorkflowDefinition.get(definition_id)
    if definition is None:
        raise InvalidGraph(
            [ValidationError("not_found", f"No definition {definition_id}.")]
        )

    candidate = WorkflowDefinition(
        name=name,
        description=description,
        nodes=nodes,
        edges=edges,
        owner=definition.owner,
    )
    errors = validate_definition(candidate)
    if errors:
        raise InvalidGraph(errors)

    definition.name = name
    definition.description = description
    definition.nodes = nodes
    definition.edges = edges
    definition.version += 1
    definition.touch()
    await definition.save()
    return definition
```

Add `WorkflowEdge` to the module's imports from `app.models.workflow`.

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_workflow_validation.py -v
```

Expected: PASS (14 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/workflow_service.py backend/tests/services/test_workflow_validation.py
git commit -m "feat(services): workflow definition CRUD with version bumping"
```

---

## Task 10: Full suite, then merge

- [ ] **Step 1: Run the whole backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: no failures. **Read the reported count**, not just the exit code — CLAUDE.md is explicit that "green" means reading the number. Compare against a pre-change baseline if anything looks off.

- [ ] **Step 2: Confirm no regression in the run/queue areas this touched**

```bash
./backend/run-worktree-tests.sh tests/queue/ tests/models/ -q
```

Expected: no failures.

- [ ] **Step 3: Merge to main and push**

Only if the suite is green and `main` is clean. Per CLAUDE.md, no permission needed for this.

```bash
git checkout main && git pull && git merge --no-ff - && ./backend/run-worktree-tests.sh tests/ -q
```

If `main` moved during the work, re-run the suite after merging rather than assuming the earlier green still holds. Then:

```bash
git push origin main
```

- [ ] **Step 4: Update the issues**

```bash
gh issue comment 20 --repo syntheticgio/bioflow --body "Models, node-type registry, validation, and the queue's tolerant-dependency path have landed on main. Orchestration (spec §8.2), canvas UI (§8.4), and activity presentation (§8.5) remain."
```

Move #20's label from `status:specification document` to reflect implementation state, and leave epic #18 open — four of its five child slices are still outstanding.

---

## Out of scope for this plan

Listed so the boundary is explicit rather than assumed:

- **Orchestration** (spec §8.2) — completion hook, output→port resolution, progressive launch, retry-in-place, cancellation. The next plan.
- **Canvas UI** (§8.4).
- **Activity presentation** (§8.5).
- **Deriving a definition from a run** (§7) — depends on the registry landing first.
- **API routes** — no HTTP surface here; `workflow_service` is called by the orchestrator and, later, by routes added with the UI.
