"""Workflow definitions, the node-type palette, and workflow runs.

Every route is owner-scoped. That is worth stating because the service layer
below is not uniformly so: `create_definition` takes an owner but
`update_definition`'s is optional, so the scoping a user actually gets is the
scoping established here.

The palette is *generated* from `NODE_TYPES` rather than hand-listed, which is
the point of the registry carrying labels and typed ports: a tool added there
appears in the canvas without anyone editing the frontend. Hand-listing it
would recreate exactly the silent-omission failure the registry's exhaustiveness
test exists to prevent.

Design note: docs/superpowers/specs/2026-08-07-workflow-dag-design.md
"""

from datetime import datetime

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import OwnerDep
from app.errors import NotFoundError
from app.models.job import Job
from app.models.workflow import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.pipelines.node_types import NODE_TYPES, ports_for
from app.services import workflow_derive, workflow_orchestrator, workflow_service

router = APIRouter(prefix="/workflows", tags=["workflows"])


class DefinitionIn(BaseModel):
    name: str
    description: str = ""
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)


class DefinitionOut(BaseModel):
    id: str
    name: str
    description: str
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]
    version: int

    @classmethod
    def of(cls, definition: WorkflowDefinition) -> "DefinitionOut":
        return cls(
            id=str(definition.id),
            name=definition.name,
            description=definition.description,
            nodes=definition.nodes,
            edges=definition.edges,
            version=definition.version,
        )


class PortOut(BaseModel):
    name: str
    type: dict
    required: bool
    # Whether this port takes several wires. The canvas needs it to know when
    # to allow a second connection rather than refusing it.
    multiple: bool = False


class ToolOptionOut(BaseModel):
    value: str
    label: str


class ToolChoiceOut(BaseModel):
    param_key: str
    options: list[ToolOptionOut]
    default: str


class PortSetOut(BaseModel):
    inputs: list[PortOut]
    outputs: list[PortOut]


class NodeTypeOut(BaseModel):
    node_type: str
    label: str
    inputs: list[PortOut]
    outputs: list[PortOut]
    # None for the node types that run exactly one tool -- most of them.
    tool_choice: ToolChoiceOut | None = None
    # Every option's port set, keyed by tool value. Empty when there is no
    # choice. Served eagerly so the canvas re-shapes a node on a dropdown
    # change without a round trip; the payload is small (six aligners, four
    # ports each) and a fetch-per-change would show ports lagging the
    # selection.
    ports_by_tool: dict[str, PortSetOut] = Field(default_factory=dict)


class DeriveIn(BaseModel):
    run_ids: list[PydanticObjectId]


class SkippedRunOut(BaseModel):
    run_id: str
    label: str
    reason: str


class DerivedOut(BaseModel):
    """An *unsaved* graph. §7 introduces no new persistence: the canvas is
    populated and the user decides whether it is worth keeping."""

    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]
    # Never empty by accident: a run that cannot be represented is reported
    # here rather than dropped, or the user gets a canvas quietly missing a
    # step they selected.
    skipped: list[SkippedRunOut]


class WorkflowRunOut(BaseModel):
    """One row of the activity listing.

    `status` is derived on read, never stored -- a second stored copy would
    drift the first time a write was lost. The counts ride along so a collapsed
    row can say "1 of 3" without a request per run.
    """

    id: str
    definition_id: str
    definition_version: int
    project_id: str
    label: str
    status: str
    node_total: int
    node_done: int
    node_failed: int
    created_at: datetime
    updated_at: datetime


class NodeJobOut(BaseModel):
    job_id: str
    type: str | None
    state: str | None
    progress: dict | None
    error: dict | None


class WorkflowNodeOut(BaseModel):
    node_id: str
    kind: str
    node_type: str | None
    label: str
    state: str
    attempt: int
    # Present only for the 9 node types that create a PipelineRun. The other 13
    # are tracked by their jobs alone -- see the deviation note on #78.
    run_id: str | None
    jobs: list[NodeJobOut]
    outputs: list[str]


class WorkflowRunDetailOut(BaseModel):
    id: str
    definition_id: str
    label: str
    status: str
    nodes: list[WorkflowNodeOut]


class LaunchIn(BaseModel):
    project_id: PydanticObjectId
    label: str
    # node_id -> object_id. A dict rather than a list because a binding is
    # identified by the INPUT node it fills, and two bindings for one node is
    # not a shape worth representing.
    bindings: dict[str, PydanticObjectId] = Field(default_factory=dict)


class RunOut(BaseModel):
    id: str
    definition_id: str
    definition_version: int
    label: str
    status: str


# Static path, declared before /{definition_id} so the path parameter cannot
# swallow it -- the same ordering hazard search.router is registered first for.
@router.get("/node-types")
async def list_node_types(owner: OwnerDep) -> list[NodeTypeOut]:
    """The canvas palette, generated from the registry."""

    def ports(specs) -> list[PortOut]:
        return [
            PortOut(
                name=p.name,
                type=p.type.model_dump(mode="json"),
                required=p.required,
                multiple=p.multiple,
            )
            for p in specs
        ]

    result = []
    for node_type, spec in sorted(NODE_TYPES.items()):
        # The default port set: what a freshly-dropped node has, before any
        # tool is chosen. Built the same way `ports_for` resolves an unset
        # tool -- a probe node with empty params -- so this never drifts from
        # what the canvas actually sees for a new node.
        default_probe = WorkflowNode(
            node_id="probe", kind=WorkflowNodeKind.ACTION, node_type=node_type
        )
        default_inputs, default_outputs = ports_for(default_probe)

        choice = spec.tool_choice
        ports_by_tool: dict[str, PortSetOut] = {}
        tool_choice_out: ToolChoiceOut | None = None
        if choice is not None:
            tool_choice_out = ToolChoiceOut(
                param_key=choice.param_key,
                options=[
                    ToolOptionOut(value=o.value, label=o.label)
                    for o in choice.options
                ],
                default=choice.default,
            )
            for option in choice.options:
                probe = WorkflowNode(
                    node_id="probe",
                    kind=WorkflowNodeKind.ACTION,
                    node_type=node_type,
                    params={choice.param_key: option.value},
                )
                tool_inputs, tool_outputs = ports_for(probe)
                ports_by_tool[option.value] = PortSetOut(
                    inputs=ports(tool_inputs), outputs=ports(tool_outputs)
                )

        result.append(
            NodeTypeOut(
                node_type=node_type,
                label=spec.label,
                inputs=ports(default_inputs),
                outputs=ports(default_outputs),
                tool_choice=tool_choice_out,
                ports_by_tool=ports_by_tool,
            )
        )
    return result


@router.get("/tool-schema/{node_type}/{tool}")
async def tool_schema(node_type: str, tool: str, owner: OwnerDep) -> dict:
    """The parameter form for one tool of one node type.

    Served rather than duplicated in the frontend for the reason
    `aligner_registry`'s docstring gives: a second copy of the field list is
    the copy nobody updates.
    """
    spec = NODE_TYPES.get(node_type)
    if spec is None or spec.tool_choice is None:
        raise NotFoundError(f"No tool-parameterized node type {node_type!r}.")
    if node_type == "align":
        from app.pipelines.aligner_registry import schema_for
        from app.pipelines.aligners import Aligner

        try:
            return schema_for(Aligner(tool))
        except ValueError as exc:
            raise NotFoundError(f"No aligner {tool!r}.") from exc
    raise NotFoundError(f"No schema for {node_type!r}.")


@router.get("")
async def list_definitions(owner: OwnerDep) -> list[DefinitionOut]:
    return [
        DefinitionOut.of(d) for d in await workflow_service.list_definitions(owner=owner)
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_definition(body: DefinitionIn, owner: OwnerDep) -> DefinitionOut:
    """Validate then persist. An invalid graph raises `InvalidGraph`, which is
    a 422 in the app's error hierarchy carrying every validation error rather
    than only the first."""
    definition = await workflow_service.create_definition(
        name=body.name,
        description=body.description,
        nodes=body.nodes,
        edges=body.edges,
        owner=owner,
    )
    return DefinitionOut.of(definition)


@router.post("/derive")
async def derive_from_runs(body: DeriveIn, owner: OwnerDep) -> DerivedOut:
    """Populate a canvas from runs the user already did.

    Declared before `/{definition_id}` for the same reason `/node-types` is:
    a path parameter would otherwise swallow it.
    """
    graph = await workflow_derive.derive_definition(body.run_ids, owner=owner)
    return DerivedOut(
        nodes=graph.nodes,
        edges=graph.edges,
        skipped=[
            SkippedRunOut(run_id=s.run_id, label=s.label, reason=s.reason)
            for s in graph.skipped
        ],
    )


@router.get("/runs")
async def list_workflow_runs(
    owner: OwnerDep, limit: int = 50
) -> list[WorkflowRunOut]:
    """Recent workflow runs. Declared before `/{definition_id}` so the path
    parameter cannot swallow "runs"."""
    summaries = await workflow_orchestrator.list_runs(owner=owner, limit=limit)
    return [
        WorkflowRunOut(
            id=str(s.run.id),
            definition_id=str(s.run.definition_id),
            definition_version=s.run.definition_version,
            project_id=str(s.run.project_id),
            label=s.run.label,
            status=s.status.value,
            node_total=s.node_total,
            node_done=s.node_done,
            node_failed=s.node_failed,
            created_at=s.run.created_at,
            updated_at=s.run.updated_at,
        )
        for s in summaries
    ]


@router.get("/runs/{workflow_run_id}")
async def get_workflow_run(
    workflow_run_id: PydanticObjectId, owner: OwnerDep
) -> WorkflowRunDetailOut:
    """One run, expanded to its nodes and each node's jobs.

    The jobs are looked up here rather than left to the client: a node's state
    comes from them, and a per-node request would put the cost of the expanded
    view on the number of nodes.
    """
    run, status_value, nodes = await workflow_orchestrator.run_detail(
        workflow_run_id, owner=owner
    )

    job_ids = [jid for node in nodes for jid in node.job_ids]
    jobs = (
        await Job.find({"_id": {"$in": job_ids}}).to_list() if job_ids else []
    )
    by_id = {job.id: job for job in jobs}

    return WorkflowRunDetailOut(
        id=str(run.id),
        definition_id=str(run.definition_id),
        label=run.label,
        status=status_value.value,
        nodes=[
            WorkflowNodeOut(
                node_id=node.node_id,
                kind=node.kind,
                node_type=node.node_type,
                label=node.label,
                state=node.state.value,
                attempt=node.attempt,
                run_id=str(node.run_id) if node.run_id else None,
                jobs=[
                    NodeJobOut(
                        job_id=str(jid),
                        # A job pruned by the 30-day TTL leaves its id on the
                        # node run. Reported as an id with nulls rather than
                        # omitted, so the count still matches what ran.
                        type=by_id[jid].type if jid in by_id else None,
                        state=by_id[jid].state.value if jid in by_id else None,
                        progress=(
                            by_id[jid].progress.model_dump(mode="json")
                            if jid in by_id
                            else None
                        ),
                        error=(
                            by_id[jid].error.model_dump(mode="json")
                            if jid in by_id and by_id[jid].error
                            else None
                        ),
                    )
                    for jid in node.job_ids
                ],
                outputs=[str(o) for o in node.outputs],
            )
            for node in nodes
        ],
    )


@router.post(
    "/runs/{workflow_run_id}/nodes/{node_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_workflow_node(
    workflow_run_id: PydanticObjectId, node_id: str, owner: OwnerDep
) -> dict:
    """Retry one failed node in place (§1.4): a new attempt and new work, with
    succeeded siblings left alone."""
    await workflow_orchestrator.retry_node(workflow_run_id, node_id, owner=owner)
    return {"retried": node_id}


@router.post(
    "/runs/{workflow_run_id}/retry-failed", status_code=status.HTTP_202_ACCEPTED
)
async def retry_failed(
    workflow_run_id: PydanticObjectId, owner: OwnerDep
) -> dict:
    count = await workflow_orchestrator.retry_failed_nodes(
        workflow_run_id, owner=owner
    )
    return {"retried": count}


@router.post("/runs/{workflow_run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_workflow_run(
    workflow_run_id: PydanticObjectId, owner: OwnerDep
) -> dict:
    await workflow_orchestrator.cancel_workflow(workflow_run_id, owner=owner)
    return {"cancelled": str(workflow_run_id)}


@router.get("/{definition_id}")
async def get_definition(definition_id: PydanticObjectId, owner: OwnerDep) -> DefinitionOut:
    return DefinitionOut.of(
        await workflow_service.get_definition(definition_id, owner=owner)
    )


@router.put("/{definition_id}")
async def update_definition(
    definition_id: PydanticObjectId, body: DefinitionIn, owner: OwnerDep
) -> DefinitionOut:
    definition = await workflow_service.update_definition(
        definition_id,
        name=body.name,
        description=body.description,
        nodes=body.nodes,
        edges=body.edges,
        owner=owner,
    )
    return DefinitionOut.of(definition)


@router.post("/{definition_id}/runs", status_code=status.HTTP_201_CREATED)
async def launch_run(
    definition_id: PydanticObjectId, body: LaunchIn, owner: OwnerDep
) -> RunOut:
    """Launch a definition against real objects.

    The ownership check is here rather than left to the orchestrator, which
    takes a definition id on trust -- launching someone else's graph would
    otherwise be possible for anyone holding its id.
    """
    definition = await workflow_service.get_definition(definition_id, owner=owner)

    run = await workflow_orchestrator.launch_workflow(
        definition_id=definition.id,
        project_id=body.project_id,
        bindings=body.bindings,
        owner=owner,
        label=body.label,
    )
    status_value = await workflow_orchestrator.status_of(run.id)
    return RunOut(
        id=str(run.id),
        definition_id=str(run.definition_id),
        definition_version=run.definition_version,
        label=run.label,
        status=status_value.value,
    )
