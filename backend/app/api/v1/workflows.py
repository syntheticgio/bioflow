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

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import OwnerDep
from app.models.workflow import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
)
from app.pipelines.node_types import NODE_TYPES
from app.services import workflow_orchestrator, workflow_service

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


class NodeTypeOut(BaseModel):
    node_type: str
    label: str
    inputs: list[PortOut]
    outputs: list[PortOut]


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
            )
            for p in specs
        ]

    return [
        NodeTypeOut(
            node_type=node_type,
            label=spec.label,
            inputs=ports(spec.inputs),
            outputs=ports(spec.outputs),
        )
        for node_type, spec in sorted(NODE_TYPES.items())
    ]


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
