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
