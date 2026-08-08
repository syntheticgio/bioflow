"""The seam between a finished job and the workflow waiting on it.

The orchestrator is inert on its own: `on_node_finished` has to be called by
something, and the only signal that a node's work is done is its jobs reaching
a terminal state. This translates "job X finished" into "node N of workflow run
R finished", which means finding the node *from* the job.

Deliberately a separate module from `workflow_orchestrator`, and deliberately
tiny. `queue.complete()` calls it on every job in the system, the overwhelming
majority of which belong to no workflow at all -- so the first thing it does is
establish that cheaply and return. Keeping that path short, and free of the
orchestrator's imports, is the point.

Design note: docs/superpowers/specs/2026-08-07-workflow-dag-design.md
"""

import structlog
from beanie import PydanticObjectId

log = structlog.get_logger(__name__)


def _mate_order(obj) -> tuple:
    """Sort key putting R1 before R2, single-end files last.

    `read_number` first, because it is authoritative where it is set. But it is
    frequently *not*: the trim applier links mates via `mate_object_id` without
    setting it (unlike the SRA path, which learns the numbering from
    fasterq-dump's own labelling). A real paired trim therefore yields two
    objects with `read_number=None`, and ordering on it alone left them in
    insertion order -- which the binder saw as two type-identical rivals for
    one port, refused to choose between, and so stalled the workflow
    permanently.

    The filename fallback uses `split_mate`, the same convention the rest of
    the codebase pairs by, rather than inventing a second rule.
    """
    from app.pipelines import pairing

    if obj.read_number is not None:
        return (0, obj.read_number, obj.name)
    split = pairing.split_mate(obj.name)
    if split is not None:
        # split_mate returns (stem, token, suffix); the token is "R1"/"R2".
        return (0, 1 if split[1] == "R1" else 2, obj.name)
    return (1, 0, obj.name)


async def _outputs_of(job_id: PydanticObjectId) -> list:
    """The objects a job produced, as binding candidates.

    Keyed on `produced_by_job` rather than a run's `outputs` list, because 13
    of the 22 node types create no `PipelineRun` to hold one -- this is what
    makes their outputs findable at all. For the 9 that do create a run, the
    two agree.

    Two filters, both added after checking this against the real database
    rather than fixtures -- of 70 jobs with objects attributed to them, 31
    produce more than one:

    **Sidecars are excluded.** Several real jobs produce 6, 9, or 16 objects
    that are *entirely* sidecars (`.fai`, `.mmi`, aligner index files). They
    are biologically inert, were never anyone's output, and including them
    makes every such node ambiguous to the binder -- which refuses to guess,
    so the graph would simply stall.

    **Mates are ordered by `read_number`.** A real paired trim produces two
    objects that are both FASTQ/TRIMMED_READS and so identical to the type
    system. R1 must come first: an aligner handed the mates backwards produces
    a silently wrong answer rather than an error.
    """
    from app.models.object import DataObject
    from app.services.workflow_binding import OutputCandidate

    objects = await DataObject.find(
        DataObject.produced_by_job == job_id,
        {"sidecar_of": None},
    ).to_list()

    objects.sort(key=_mate_order)

    return [
        OutputCandidate(
            object_id=obj.id, format=obj.format.kind, role=obj.role, name=obj.name
        )
        for obj in objects
    ]


async def on_job_finished(job_id: PydanticObjectId, *, succeeded: bool) -> None:
    """Advance any workflow node this job belongs to.

    A no-op for the ordinary case of a job that is not part of a workflow, and
    that check is one indexed query. Never raises: this runs on the completion
    path of every job in the system, and a workflow bookkeeping problem must
    not turn a successful job into a failed one.
    """
    from app.models.workflow import WorkflowNodeRun

    try:
        row = await WorkflowNodeRun.find_one(WorkflowNodeRun.job_ids == job_id)
        if row is None:
            return

        from app.services import workflow_orchestrator as orchestrator

        outputs = await _outputs_of(job_id) if succeeded else []
        await orchestrator.on_node_finished(
            row.workflow_run_id,
            row.node_id,
            succeeded=succeeded,
            outputs=outputs,
        )
    except Exception as e:  # noqa: BLE001 - never fail a job over workflow bookkeeping
        log.warning(
            "workflow_hook_failed",
            job_id=str(job_id),
            succeeded=succeeded,
            error=str(e),
        )
