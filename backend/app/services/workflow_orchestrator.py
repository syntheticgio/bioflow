"""The workflow engine: launch, progressive advance, retry, cancel, recover.

The two decisions this makes live elsewhere and are pure: `workflow_planner`
says what may start and what never can, `workflow_binding` says which output
feeds which port. What is left here is the I/O -- creating documents, calling
launchers, and keeping node state in step with the jobs underneath it.

**Node state comes from jobs, not from `PipelineRun`s.** The design's §6
specifies the completion hook in terms of a node's run reaching a terminal
state, but 13 of the 22 node types create no run at all -- QC, bam_stats, and
the assembly QC family among them -- and that is deliberate on their part
rather than a registry gap (`launch_qc`'s docstring: a run wrapping a single
job "would add a row to the activity view that says nothing the job does
not"). Keying on runs would hang any workflow whose QC node feeds something,
which is precisely the shape `continue_on_failure` exists for. Every launcher
returns a `Job`, uniformly, so jobs are the grain that works for all 22.
`run_id` is still recorded where a run exists, so §1.5 holds and the activity
view keeps seeing ordinary runs. See the deviation note on #18/#78.

Design note: docs/superpowers/specs/2026-08-07-workflow-dag-design.md
"""

import structlog
from beanie import PydanticObjectId

from app.errors import NotFoundError
from app.models.workflow import (
    NodeRunState,
    WorkflowDefinition,
    WorkflowNodeKind,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowStatus,
    derive_status,
)
from app.pipelines.node_types import NODE_TYPES
from app.services.workflow_binding import OutputCandidate, bind_downstream_inputs
from app.services.workflow_planner import doomed_nodes, runnable_nodes

log = structlog.get_logger(__name__)

__all__ = [
    "OutputCandidate",
    "cancel_workflow",
    "launch_workflow",
    "on_node_finished",
    "reconcile_workflows",
    "retry_node",
    "status_of",
]

# Terminal for a node: nothing further will happen to it on its own.
_TERMINAL = frozenset(
    {
        NodeRunState.SUCCEEDED,
        NodeRunState.FAILED,
        NodeRunState.CANCELLED,
        NodeRunState.SKIPPED,
    }
)


async def _launch_node(node_type: str, *, inputs: dict, params: dict, owner: str):
    """Call the registry's adapter for one node type.

    A module-level function rather than an inline call so tests can replace the
    one seam that would otherwise run real tools. Patch *this*, not
    `NODE_TYPES` -- the specs are frozen dataclasses holding function objects
    captured at import time, so rebinding a service attribute never reaches
    them. Same trap CLAUDE.md records for `aligner_registry`.
    """
    spec = NODE_TYPES.get(node_type)
    if spec is None:
        raise NotFoundError(f"No node type named {node_type!r}.")
    return await spec.launch(inputs=inputs, params=params, owner=owner)


async def _rows(workflow_run_id: PydanticObjectId) -> list[WorkflowNodeRun]:
    return await WorkflowNodeRun.find(
        WorkflowNodeRun.workflow_run_id == workflow_run_id
    ).to_list()


def _latest(rows: list[WorkflowNodeRun]) -> dict[str, WorkflowNodeRun]:
    """The current attempt for each node.

    Retry adds a row rather than overwriting one (§1.4 -- a DEAD job cannot be
    un-deaded), so a node's *state* is its highest-numbered attempt. Reading
    all rows would count a node's failed first attempt against a succeeded
    second one and report a healthy run as PARTIAL forever.
    """
    latest: dict[str, WorkflowNodeRun] = {}
    for row in rows:
        prev = latest.get(row.node_id)
        if prev is None or row.attempt > prev.attempt:
            latest[row.node_id] = row
    return latest


async def _load(workflow_run_id: PydanticObjectId):
    run = await WorkflowRun.get(workflow_run_id)
    if run is None:
        raise NotFoundError(f"No workflow run {workflow_run_id}.")
    definition = await WorkflowDefinition.get(run.definition_id)
    if definition is None:
        raise NotFoundError(f"No definition {run.definition_id}.")
    return run, definition


def _bound_inputs(
    definition: WorkflowDefinition,
    node_id: str,
    bindings: dict[str, PydanticObjectId],
    resolved: dict[tuple[str, str], PydanticObjectId],
) -> dict:
    """Every input port of one node, filled from bindings and upstream outputs.

    An edge from an INPUT node reads the run's binding; an edge from an ACTION
    node reads what that node produced, which `on_node_finished` recorded.
    """
    by_id = {n.node_id: n for n in definition.nodes}
    inputs: dict = {}
    for edge in definition.edges:
        if edge.to_node != node_id:
            continue
        source = by_id.get(edge.from_node)
        if source is None:
            continue
        if source.kind is WorkflowNodeKind.INPUT:
            if source.node_id in bindings:
                inputs[edge.to_port] = bindings[source.node_id]
        elif (edge.to_node, edge.to_port) in resolved:
            inputs[edge.to_port] = resolved[(edge.to_node, edge.to_port)]
    return inputs


async def _advance(
    run: WorkflowRun, definition: WorkflowDefinition, *, owner: str
) -> int:
    """Launch every node that has become runnable. The engine's core step.

    Idempotent by construction: `runnable_nodes` only returns PENDING nodes,
    and each launch flips its node to RUNNING before the next call can see it.
    That is what makes the hook safe to run twice, which it will be -- job
    completions are not exactly-once.
    """
    rows = await _rows(run.id)
    latest = _latest(rows)
    states = {node_id: row.state for node_id, row in latest.items()}

    resolved: dict[tuple[str, str], PydanticObjectId] = {}
    for node_id, row in latest.items():
        if row.state is NodeRunState.SUCCEEDED and row.outputs:
            candidates = await _candidates_for(row.outputs)
            resolved.update(bind_downstream_inputs(definition, node_id, candidates))

    bindings = {b.node_id: b.object_id for b in run.bindings}
    launched = 0

    for node_id in sorted(runnable_nodes(definition, states)):
        node = next((n for n in definition.nodes if n.node_id == node_id), None)
        if node is None or node.node_type is None:
            continue
        row = latest.get(node_id)
        if row is None:
            continue

        inputs = _bound_inputs(definition, node_id, bindings, resolved)

        # The planner says the graph is ready; this says the *objects* are.
        # They can disagree: an upstream node can succeed without producing the
        # file its port expected, or that file can be deleted before the
        # successor starts. Launching regardless would call a launcher with a
        # missing required input, and every launcher validates -- so the branch
        # would fail with a confusing error attributed to the wrong node.
        # Leaving it PENDING keeps it visibly unlaunched, which is what the
        # reconciler and the UI can both act on.
        spec = NODE_TYPES.get(node.node_type)
        if spec is not None:
            missing = [p.name for p in spec.inputs if p.required and p.name not in inputs]
            if missing:
                log.warning(
                    "workflow_node_inputs_unresolved",
                    workflow_run_id=str(run.id),
                    node_id=node_id,
                    missing=missing,
                )
                continue

        job = await _launch_node(
            node.node_type, inputs=inputs, params=node.params, owner=owner
        )

        # A launcher returning None means the job was deduplicated away -- the
        # work is already happening (or has happened) under another job. Treat
        # the node as running rather than failing the branch over a cache hit;
        # the reconciler picks it up if the original job is already terminal.
        update = {WorkflowNodeRun.state: NodeRunState.RUNNING}
        if job is not None:
            update[WorkflowNodeRun.job_ids] = [job.id]
            run_id = await _run_for_job(job.id)
            if run_id is not None:
                update[WorkflowNodeRun.run_id] = run_id
        await row.set(update)
        launched += 1
        log.info(
            "workflow_node_launched",
            workflow_run_id=str(run.id),
            node_id=node_id,
            node_type=node.node_type,
        )

    return launched


async def _candidates_for(object_ids: list[PydanticObjectId]) -> list[OutputCandidate]:
    """Look up the format and role of a node's recorded outputs.

    A node run stores output *ids* only, so binding re-reads the objects. That
    is deliberate: the alternative is denormalizing format and role onto the
    row, where they would be a second copy of something the ingest path owns
    and can correct later.

    An id with no object behind it is skipped rather than raising -- a deleted
    output should leave its dependent visibly unlaunched, not break the run's
    every subsequent advance.
    """
    from app.models.object import DataObject

    if not object_ids:
        return []
    objects = await DataObject.find({"_id": {"$in": object_ids}}).to_list()
    by_id = {o.id: o for o in objects}
    return [
        OutputCandidate(
            object_id=obj.id,
            format=obj.format.kind,
            role=obj.role,
            name=obj.name,
        )
        for object_id in object_ids
        if (obj := by_id.get(object_id)) is not None
    ]


async def _run_for_job(job_id: PydanticObjectId) -> PydanticObjectId | None:
    from app.services import run_service

    try:
        return await run_service.run_for_job(job_id)
    except Exception as e:  # noqa: BLE001 - grouping must never fail a launch
        log.debug("workflow_run_lookup_failed", job_id=str(job_id), error=str(e))
        return None


async def launch_workflow(
    *,
    definition_id: PydanticObjectId,
    project_id: PydanticObjectId,
    bindings: dict[str, PydanticObjectId],
    owner: str,
    label: str,
) -> WorkflowRun:
    """Create a run and launch its initial wave.

    The definition's version is pinned onto the run rather than looked up
    later: the definition may be edited afterwards, and a run described by a
    graph it did not execute is worse than no record at all.
    """
    from app.models.workflow import WorkflowBinding

    definition = await WorkflowDefinition.get(definition_id)
    if definition is None:
        raise NotFoundError(f"No definition {definition_id}.")

    run = WorkflowRun(
        definition_id=definition.id,
        definition_version=definition.version,
        project_id=project_id,
        label=label,
        owner=owner,
        bindings=[
            WorkflowBinding(node_id=node_id, object_id=object_id, name=node_id)
            for node_id, object_id in bindings.items()
        ],
    )
    await run.insert()

    for node in definition.nodes:
        # An INPUT node is satisfied the moment the run exists -- it binds a
        # file rather than computing one. Marking it SUCCEEDED up front is what
        # lets the planner treat its edges as ready without a special case at
        # every read.
        state = (
            NodeRunState.SUCCEEDED
            if node.kind is WorkflowNodeKind.INPUT
            else NodeRunState.PENDING
        )
        await WorkflowNodeRun(
            workflow_run_id=run.id,
            node_id=node.node_id,
            state=state,
            owner=owner,
        ).insert()

    await _advance(run, definition, owner=owner)
    return run


async def on_node_finished(
    workflow_run_id: PydanticObjectId,
    node_id: str,
    *,
    succeeded: bool,
    outputs: list[OutputCandidate],
) -> None:
    """Record a node's outcome and advance the graph.

    Safe to call more than once for the same node: a node already terminal is
    left alone, which is what keeps a duplicate job completion from
    double-launching the successor.
    """
    run, definition = await _load(workflow_run_id)
    rows = await _rows(run.id)
    latest = _latest(rows)
    row = latest.get(node_id)
    if row is None:
        return
    if row.state in _TERMINAL:
        return  # already resolved; a repeat event

    # A cancelled run must not be resurrected by a late completion. Running
    # jobs stop cooperatively, so their terminal write lands *after* the
    # cancellation -- this is the normal case, not an exotic race.
    if any(r.state is NodeRunState.CANCELLED for r in latest.values()):
        return

    await row.set(
        {
            WorkflowNodeRun.state: (
                NodeRunState.SUCCEEDED if succeeded else NodeRunState.FAILED
            ),
            WorkflowNodeRun.outputs: [o.object_id for o in outputs],
        }
    )
    if not succeeded:
        states = {nid: r.state for nid, r in latest.items()}
        states[node_id] = NodeRunState.FAILED
        for doomed_id in doomed_nodes(definition, states):
            doomed_row = latest.get(doomed_id)
            if doomed_row is not None and doomed_row.state is NodeRunState.PENDING:
                await doomed_row.set({WorkflowNodeRun.state: NodeRunState.SKIPPED})

    await _advance(run, definition, owner=run.owner)


async def retry_node(
    workflow_run_id: PydanticObjectId, node_id: str, *, owner: str
) -> None:
    """Retry one failed node in place, per §1.4.

    A *new* attempt row and a new job: a DEAD job cannot be revived, so retry
    re-points the node at fresh work while succeeded siblings keep their
    original links and are not re-executed.

    Descendants skipped when this node failed are returned to PENDING. Leaving
    them SKIPPED is the failure mode that makes retry useless -- the node
    succeeds and the graph still never advances.
    """
    run, definition = await _load(workflow_run_id)
    rows = await _rows(run.id)
    latest = _latest(rows)
    row = latest.get(node_id)
    if row is None:
        raise NotFoundError(f"No node {node_id!r} in workflow run {workflow_run_id}.")
    if row.state is not NodeRunState.FAILED:
        return

    await WorkflowNodeRun(
        workflow_run_id=run.id,
        node_id=node_id,
        state=NodeRunState.PENDING,
        attempt=row.attempt + 1,
        owner=owner,
    ).insert()

    for other_id, other in latest.items():
        if other_id != node_id and other.state is NodeRunState.SKIPPED:
            await other.set({WorkflowNodeRun.state: NodeRunState.PENDING})

    await _advance(run, definition, owner=owner)


async def cancel_workflow(workflow_run_id: PydanticObjectId, *, owner: str) -> None:
    """Stop a run: cancel its in-flight jobs, mark its unfinished nodes.

    Nodes that already finished keep their state -- a succeeded node's output
    is still on disk and still usable, and rewriting it to CANCELLED would
    erase a real result.
    """
    from app.queue import queue

    run, _ = await _load(workflow_run_id)
    latest = _latest(await _rows(run.id))

    for row in latest.values():
        if row.state in _TERMINAL:
            continue
        for job_id in row.job_ids:
            try:
                await queue.request_cancel(str(job_id))
            except Exception as e:  # noqa: BLE001 - one job must not stop the rest
                log.warning(
                    "workflow_cancel_job_failed", job_id=str(job_id), error=str(e)
                )
        await row.set({WorkflowNodeRun.state: NodeRunState.CANCELLED})

    log.info("workflow_cancelled", workflow_run_id=str(run.id))


async def status_of(workflow_run_id: PydanticObjectId) -> WorkflowStatus:
    """The run's derived status. Never stored -- see `derive_status`.

    INPUT nodes are excluded: they are SUCCEEDED from creation, and counting
    them would report a run whose every action is still pending as already
    partly done.
    """
    run, definition = await _load(workflow_run_id)
    action_ids = {
        n.node_id for n in definition.nodes if n.kind is WorkflowNodeKind.ACTION
    }
    latest = _latest(await _rows(run.id))
    return derive_status(
        [row.state for node_id, row in latest.items() if node_id in action_ids]
    )


async def reconcile_workflows() -> int:
    """Restart runs stranded by a process that died mid-advance.

    The design's §10 names this as a structural risk rather than an edge case:
    the completion hook *is* the workflow engine, so a crash between a node
    finishing and its successor launching leaves a run that nothing will ever
    revive. There is no timer on a workflow and no dependency to release -- the
    run simply stops, looking healthy.

    The check is cheap and the fix is the ordinary advance step: any run with a
    runnable node and nothing in flight to launch it was stranded.
    """
    recovered = 0

    async for run in WorkflowRun.find_all():
        definition = await WorkflowDefinition.get(run.definition_id)
        if definition is None:
            continue
        latest = _latest(await _rows(run.id))
        states = {node_id: row.state for node_id, row in latest.items()}
        if any(s is NodeRunState.CANCELLED for s in states.values()):
            continue
        if not runnable_nodes(definition, states):
            continue

        launched = await _advance(run, definition, owner=run.owner)
        if launched:
            recovered += launched
            log.warning(
                "workflow_run_recovered",
                workflow_run_id=str(run.id),
                launched=launched,
            )

    return recovered
