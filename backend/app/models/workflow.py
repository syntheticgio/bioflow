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
