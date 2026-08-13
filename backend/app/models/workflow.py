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
from pydantic import BaseModel, Field, model_validator
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

    A port names either one format (`format`, how nearly every port is
    declared) or several (`formats`). The pair exists because annotation
    export accepts GFF/GTF/BED while refusing GenBank, whose features span
    several lines -- a refusal worth making at design time on the canvas
    rather than at runtime in the handler. Read `accepted_formats`, never
    either field directly: it is the one place that knows both spellings.
    """

    format: FormatKind | None = None
    formats: tuple[FormatKind, ...] | None = None
    role: ObjectRole | None = None

    @model_validator(mode="after")
    def _exactly_one_spelling(self) -> "PortType":
        if (self.format is None) == (self.formats is None):
            raise ValueError("PortType needs exactly one of `format` or `formats`")
        if self.formats is not None and not self.formats:
            raise ValueError("PortType `formats` cannot be empty")
        return self

    @property
    def accepted_formats(self) -> tuple[FormatKind, ...]:
        """Every format this port accepts, however it was declared."""
        if self.formats is not None:
            return self.formats
        assert self.format is not None  # guaranteed by _exactly_one_spelling
        return (self.format,)

    def accepts(self, format: FormatKind, role: ObjectRole | None) -> bool:
        """Whether an object of this format/role may connect here.

        A required role is not satisfied by an absent one. An object with no
        role has not declared its intent, and treating that as a match is
        exactly the guess `ObjectRole` exists to prevent -- it is how a
        protein FASTA reaches an aligner's reference port.
        """
        if format not in self.accepted_formats:
            return False
        if self.role is None:
            return True
        return self.role == role

    def accepts_any(self, produced: "PortType") -> bool:
        """Whether an object produced by `produced` may connect here.

        `produced` is itself a port-shaped type -- the output side of an
        edge, not a single stored object -- and may name several formats
        (`formats=`) rather than one. There is no single "the format" to
        check in that case, so this accepts when at least one of
        `produced`'s possible formats is accepted here, matching the role
        that format would carry. Mirrors the frontend's `portAccepts` in
        `frontend/src/lib/workflowGraph.ts`, which independently arrived at
        the same two-PortType comparison for the canvas's own wiring check.

        Not symmetric -- only `self.role` gates the match, and only
        `produced`'s formats are enumerated -- so call it as the accepting
        (input) port's method: `input_port.accepts_any(output_port)`, not
        the reverse. Both arguments share the same `PortType` type, so a
        swapped call order type-checks fine while asking a different
        question.
        """
        return any(self.accepts(fmt, produced.role) for fmt in produced.accepted_formats)


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
    assert all(s in _UNSUCCESSFUL_NODE_STATES for s in states), (
        "every state not active and not succeeded must be unsuccessful"
    )
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
    #
    # Also None for the 13 of 22 node types that create no run at all (QC,
    # bam_stats, the assembly QC family...). That is deliberate on their part
    # rather than a gap: `launch_qc`'s docstring explains that a run wrapping a
    # single job would add an activity row saying nothing the job does not.
    # Node state therefore comes from `job_ids` below, not from this -- see the
    # design deviation recorded on #78.
    run_id: PydanticObjectId | None = None
    # The jobs this attempt enqueued. The completion hook keys on these, which
    # is what makes every node type trackable rather than only the 9 that
    # create a PipelineRun. A list because one launch fans out into several
    # jobs (an alignment enqueues its index build alongside the align itself).
    job_ids: list[PydanticObjectId] = Field(default_factory=list)
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
