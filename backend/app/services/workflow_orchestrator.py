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

from dataclasses import dataclass

import structlog
from beanie import PydanticObjectId

from app.errors import ConflictError, NotFoundError
from app.models.workflow import (
    NodeRunState,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowStatus,
    derive_status,
)
from app.pipelines.node_types import NODE_TYPES, ports_for
from app.services.workflow_binding import OutputCandidate, bind_downstream_inputs
from app.services.workflow_planner import UNSUCCESSFUL, doomed_nodes, runnable_nodes

log = structlog.get_logger(__name__)

__all__ = [
    "NodeDetail",
    "OutputCandidate",
    "RunSummary",
    "cancel_workflow",
    "launch_workflow",
    "on_node_finished",
    "list_runs",
    "reconcile_workflows",
    "retry_failed_nodes",
    "run_detail",
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


async def _load(workflow_run_id: PydanticObjectId, *, owner: str | None = None):
    """The run and its definition.

    `owner`, when given, scopes the lookup. Optional rather than required
    because the internal callers -- the completion hook and the reconciler --
    act on whatever run a finished job belongs to and have no user in hand;
    every caller that *does* have one must pass it. Without that, any profile
    holding a run id could retry or cancel another profile's workflow and spend
    their compute, which is what `test_another_owner_cannot_retry` caught.
    """
    run = await WorkflowRun.get(workflow_run_id)
    if run is None or (owner is not None and run.owner != owner):
        raise NotFoundError(f"No workflow run {workflow_run_id}.")
    definition = await WorkflowDefinition.get(run.definition_id)
    if definition is None:
        raise NotFoundError(f"No definition {run.definition_id}.")
    return run, definition


def _is_multi_port(node: WorkflowNode, port_name: str) -> bool:
    """Whether `port_name` on `node`'s input side is declared `multiple`.

    False for anything without a resolvable spec (no node_type, unknown
    node_type, or unknown port name) -- those are graphs `_bound_inputs`
    cannot make sense of regardless, and the scalar-overwrite behaviour is the
    existing, safe fallback for them.
    """
    if node.node_type is None:
        return False
    spec = NODE_TYPES.get(node.node_type)
    if spec is None:
        return False
    inputs, _ = ports_for(node)
    port = next((p for p in inputs if p.name == port_name), None)
    return port is not None and port.multiple


def _bound_inputs(
    definition: WorkflowDefinition,
    node_id: str,
    bindings: dict[str, PydanticObjectId | list[PydanticObjectId]],
    resolved: dict[tuple[str, str], PydanticObjectId],
) -> dict:
    """Every input port of one node, filled from bindings and upstream outputs.

    An edge from an INPUT node reads the run's binding; an edge from an ACTION
    node reads what that node produced, which `on_node_finished` recorded.

    A *multi* port can be fed by more than one edge -- legal since the
    validator's `duplicate_wire` check started skipping multi ports. Values
    for such a port are collected into a list rather than assigned directly,
    because a plain `inputs[edge.to_port] = value` assignment would let a
    second edge silently overwrite the first.

    The list is only surfaced when a multi port actually receives more than
    one contribution. A multi port fed by exactly one edge -- by far the
    common case, since most align nodes have one upstream reads source --
    keeps producing the bare scalar every launcher and every pre-existing
    test already expects; wrapping it in a one-element list would be a
    behaviour change with no wire to justify it. A contribution that is
    itself already a list (an ACTION source whose own output port is multi,
    per `bind_downstream_inputs`'s ambiguous-candidates branch) is flattened
    in rather than nested, so it counts as however many object ids it holds.
    """
    by_id = {n.node_id: n for n in definition.nodes}
    target = by_id.get(node_id)
    inputs: dict = {}
    multi_values: dict[str, list] = {}
    for edge in definition.edges:
        if edge.to_node != node_id:
            continue
        source = by_id.get(edge.from_node)
        if source is None:
            continue

        value = None
        has_value = False
        if source.kind is WorkflowNodeKind.INPUT:
            if source.node_id in bindings:
                value = bindings[source.node_id]
                has_value = True
        elif (edge.to_node, edge.to_port) in resolved:
            value = resolved[(edge.to_node, edge.to_port)]
            has_value = True

        if not has_value:
            continue

        if target is not None and _is_multi_port(target, edge.to_port):
            collected = multi_values.setdefault(edge.to_port, [])
            if isinstance(value, list):
                collected.extend(value)
            else:
                collected.append(value)
        else:
            inputs[edge.to_port] = value

    for port_name, values in multi_values.items():
        inputs[port_name] = values[0] if len(values) == 1 else values
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
            resolved.update(
                bind_downstream_inputs(
                    definition,
                    node_id,
                    candidates,
                    paired=await _is_mate_pair(row.outputs),
                )
            )

    # A dict comprehension here would overwrite on a repeated node_id, keeping
    # only the last of a multi slot's several `WorkflowBinding` rows. Accumulate
    # instead: a scalar for one row, a list once a second row shares a node_id
    # -- the same "list only when 2+" convention `_bound_inputs` above uses.
    bindings: dict[str, PydanticObjectId | list[PydanticObjectId]] = {}
    for b in run.bindings:
        existing = bindings.get(b.node_id)
        if existing is None:
            bindings[b.node_id] = b.object_id
        elif isinstance(existing, list):
            existing.append(b.object_id)
        else:
            bindings[b.node_id] = [existing, b.object_id]
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
            node_inputs, _ = ports_for(node)
            missing = [p.name for p in node_inputs if p.required and p.name not in inputs]
            if missing:
                log.warning(
                    "workflow_node_inputs_unresolved",
                    workflow_run_id=str(run.id),
                    node_id=node_id,
                    missing=missing,
                )
                continue

        try:
            job = await _launch_node(
                node.node_type, inputs=inputs, params=node.params, owner=owner
            )
        except ConflictError:
            # The launcher refuses because identical work is already queued or
            # running -- for a workflow node that is success, not failure: the
            # output it is waiting for is on its way. Treat it exactly like the
            # deduplicated-away case below.
            #
            # Not merely tidy: launchers raise this from a *reconcile* pass
            # too, and an uncaught one aborted the whole sweep, so a single
            # node with in-flight work stopped every other stalled run from
            # being recovered. Observed as
            # `workflow_reconcile_failed error='QC is already queued or
            # running for this file'` against the live stack.
            log.info(
                "workflow_node_already_running",
                workflow_run_id=str(run.id),
                node_id=node_id,
                node_type=node.node_type,
            )
            job = None

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


async def _is_mate_pair(object_ids: list[PydanticObjectId]) -> bool:
    """Whether these two outputs are the halves of one paired-end library.

    Read from `mate_object_id` rather than inferred from count or name: two
    outputs of one type are not necessarily mates (an assembly emits a FASTA
    and a GFA), and binding an unrelated pair into `reads`/`mate` would hand a
    tool two files it should never have seen together.
    """
    from app.models.object import DataObject

    if len(object_ids) != 2:
        return False
    objects = await DataObject.find({"_id": {"$in": object_ids}}).to_list()
    if len(objects) != 2:
        return False
    a, b = objects
    return a.mate_object_id == b.id and b.mate_object_id == a.id


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
    # Iterating `object_ids` rather than the query result preserves the order
    # the outputs were recorded in, which for a mate pair is R1 then R2 (see
    # `workflow_hook._mate_order`). A Mongo `$in` does not promise input order,
    # so reading the result list directly would silently swap the mates.
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
    bindings: dict[str, PydanticObjectId | list[PydanticObjectId]],
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

    # A multi slot (see PortSpec.multiple) binds several files under one
    # node_id -- one WorkflowBinding row per file, sharing the node_id. A
    # scalar binding still produces exactly one row, as before.
    binding_rows: list[WorkflowBinding] = []
    for node_id, value in bindings.items():
        object_ids = value if isinstance(value, list) else [value]
        for object_id in object_ids:
            binding_rows.append(
                WorkflowBinding(node_id=node_id, object_id=object_id, name=node_id)
            )

    run = WorkflowRun(
        definition_id=definition.id,
        definition_version=definition.version,
        project_id=project_id,
        label=label,
        owner=owner,
        bindings=binding_rows,
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

    # A cancelled run must not be resurrected by a late completion. Running
    # jobs stop cooperatively, so their terminal write lands *after* the
    # cancellation -- this is the normal case, not an exotic race.
    if any(r.state is NodeRunState.CANCELLED for r in latest.values()):
        return

    # An already-terminal node skips the *write* but must still advance. This
    # was a `return`, and it deadlocked every real multi-node workflow: a node
    # can legitimately already be SUCCEEDED when its own completion arrives --
    # `launch_workflow` and the reconciler both resolve state from the jobs --
    # so the hook returned before ever launching the successor. The unit test
    # missed it by calling the hook twice in a row, where the *first* call had
    # already advanced the graph and the second returning early was invisible.
    # Advancing is idempotent on its own (`runnable_nodes` returns only PENDING
    # nodes), so it needs no guard of its own.
    was_terminal = row.state in _TERMINAL
    if not was_terminal:
        await row.set(
            {
                WorkflowNodeRun.state: (
                    NodeRunState.SUCCEEDED if succeeded else NodeRunState.FAILED
                ),
                WorkflowNodeRun.outputs: [o.object_id for o in outputs],
            }
        )
    elif outputs and not row.outputs:
        # Terminal but with nothing recorded: keep the outputs, or the
        # successor has nothing to bind and the run stalls exactly as before.
        await row.set({WorkflowNodeRun.outputs: [o.object_id for o in outputs]})

    if not succeeded and not was_terminal:
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
    run, definition = await _load(workflow_run_id, owner=owner)
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

    run, _ = await _load(workflow_run_id, owner=owner)
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


@dataclass(frozen=True)
class RunSummary:
    """One row of the activity listing.

    Carries the counts as well as the status so a collapsed row can say "1 of
    3" without a second request per run.
    """

    run: WorkflowRun
    status: WorkflowStatus
    node_total: int
    node_done: int
    node_failed: int


async def list_runs(*, owner: str, limit: int = 50) -> list[RunSummary]:
    """Recent workflow runs with their derived status, in three queries.

    Deliberately not a loop over `status_of`: that is one `_load` plus one
    `_rows` per run, so the activity page would slow in proportion to history
    -- the exact shape `run_service.status_for_many` exists to avoid on the
    `PipelineRun` side. Runs, their definitions, and every node row are each
    fetched once and joined in memory.
    """
    runs = (
        await WorkflowRun.find(WorkflowRun.owner == owner)
        .sort(-WorkflowRun.created_at)
        .limit(limit)
        .to_list()
    )
    if not runs:
        return []

    definitions = await WorkflowDefinition.find(
        {"_id": {"$in": [r.definition_id for r in runs]}}
    ).to_list()
    by_definition = {d.id: d for d in definitions}

    rows = await WorkflowNodeRun.find(
        {"workflow_run_id": {"$in": [r.id for r in runs]}}
    ).to_list()
    by_run: dict[PydanticObjectId, list[WorkflowNodeRun]] = {}
    for row in rows:
        by_run.setdefault(row.workflow_run_id, []).append(row)

    summaries: list[RunSummary] = []
    for run in runs:
        definition = by_definition.get(run.definition_id)
        # A definition deleted out from under its run: report the run rather
        # than hiding it, since the run is the record of what happened.
        action_ids = (
            {
                n.node_id
                for n in definition.nodes
                if n.kind is WorkflowNodeKind.ACTION
            }
            if definition
            else set()
        )
        latest = _latest(by_run.get(run.id, []))
        states = [
            row.state for node_id, row in latest.items() if node_id in action_ids
        ]
        summaries.append(
            RunSummary(
                run=run,
                status=derive_status(states),
                node_total=len(states),
                node_done=sum(1 for s in states if s is NodeRunState.SUCCEEDED),
                node_failed=sum(1 for s in states if s in UNSUCCESSFUL),
            )
        )
    return summaries


@dataclass(frozen=True)
class NodeDetail:
    node_id: str
    kind: str
    node_type: str | None
    label: str
    state: NodeRunState
    attempt: int
    run_id: PydanticObjectId | None
    job_ids: list[PydanticObjectId]
    outputs: list[PydanticObjectId]


async def run_detail(
    workflow_run_id: PydanticObjectId, *, owner: str
) -> tuple[WorkflowRun, WorkflowStatus, list[NodeDetail]]:
    """One run, expanded to its nodes.

    Nodes are returned in the definition's own order rather than the node runs'
    insertion order, so the expanded view reads the way the canvas is laid out.
    """
    run = await WorkflowRun.get(workflow_run_id)
    if run is None or run.owner != owner:
        raise NotFoundError(f"No workflow run {workflow_run_id}.")
    definition = await WorkflowDefinition.get(run.definition_id)
    if definition is None:
        raise NotFoundError(f"No definition {run.definition_id}.")

    latest = _latest(await _rows(run.id))
    details: list[NodeDetail] = []
    for node in definition.nodes:
        row = latest.get(node.node_id)
        details.append(
            NodeDetail(
                node_id=node.node_id,
                kind=node.kind.value,
                node_type=node.node_type,
                label=node.label or node.node_type or node.node_id,
                state=row.state if row else NodeRunState.PENDING,
                attempt=row.attempt if row else 1,
                run_id=row.run_id if row else None,
                job_ids=list(row.job_ids) if row else [],
                outputs=list(row.outputs) if row else [],
            )
        )

    action_ids = {
        n.node_id for n in definition.nodes if n.kind is WorkflowNodeKind.ACTION
    }
    status = derive_status(
        [d.state for d in details if d.node_id in action_ids]
    )
    return run, status, details


async def retry_failed_nodes(
    workflow_run_id: PydanticObjectId, *, owner: str
) -> int:
    """Retry every failed node in one run. Returns how many were retried.

    §1.4: this is the per-node operation applied to a set, not a second
    mechanism -- so it goes through `retry_node` rather than reimplementing the
    attempt bookkeeping.
    """
    run = await WorkflowRun.get(workflow_run_id)
    if run is None or run.owner != owner:
        raise NotFoundError(f"No workflow run {workflow_run_id}.")

    latest = _latest(await _rows(run.id))
    failed = [
        node_id
        for node_id, row in latest.items()
        if row.state is NodeRunState.FAILED
    ]
    for node_id in failed:
        await retry_node(run.id, node_id, owner=owner)
    return len(failed)


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
