"""Saved pipeline parameter sets.

Every route is `OwnerDep`-scoped: a set belongs to the profile that saved it,
the same partitioning every other collection uses.

`?tool=` is required on list rather than optional. An optional filter would
create a route returning every set across every tool, which is the route a
cross-tool picker would later be built on -- quietly undoing the decision to
bind a set to one specific tool.
"""

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.api.deps import OwnerDep
from app.models.parameter_set import ParameterSet, ParamSpecFamily
from app.services import parameter_set_service as svc

router = APIRouter(prefix="/parameter-sets", tags=["parameter-sets"])


class ParameterSetIn(BaseModel):
    name: str
    tool: str
    family: ParamSpecFamily
    params: dict = {}


class ParameterSetUpdate(BaseModel):
    name: str | None = None
    params: dict | None = None


class ParameterSetOut(BaseModel):
    id: str
    name: str
    tool: str
    family: ParamSpecFamily
    params: dict
    revision: int

    @classmethod
    def of(cls, s: ParameterSet) -> "ParameterSetOut":
        return cls(
            id=str(s.id), name=s.name, tool=s.tool,
            family=s.family, params=s.params, revision=s.revision,
        )


async def _owned(set_id: PydanticObjectId, owner: str) -> ParameterSet:
    """A set, or 404. Scoped by owner, so another profile's id reads as absent
    rather than forbidden -- it is not theirs to know about."""
    found = await ParameterSet.find_one(
        ParameterSet.id == set_id, ParameterSet.owner == owner
    )
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Parameter set not found")
    return found


@router.get("", response_model=list[ParameterSetOut])
async def list_parameter_sets(owner: OwnerDep, tool: str = Query(...)) -> list[ParameterSetOut]:
    sets = await ParameterSet.find(
        ParameterSet.owner == owner, ParameterSet.tool == tool
    ).sort(ParameterSet.name).to_list()
    return [ParameterSetOut.of(s) for s in sets]


@router.post("", response_model=ParameterSetOut, status_code=status.HTTP_201_CREATED)
async def create_parameter_set(body: ParameterSetIn, owner: OwnerDep) -> ParameterSetOut:
    try:
        params = svc.eligible_params(body.family, body.tool, body.params)
    except svc.UnknownToolError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    created = ParameterSet(
        name=body.name, tool=body.tool, family=body.family, params=params, owner=owner
    )
    try:
        await created.insert()
    except DuplicateKeyError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A parameter set named {body.name!r} already exists for {body.tool}",
        ) from exc
    return ParameterSetOut.of(created)


@router.patch("/{set_id}", response_model=ParameterSetOut)
async def update_parameter_set(
    set_id: PydanticObjectId, body: ParameterSetUpdate, owner: OwnerDep
) -> ParameterSetOut:
    found = await _owned(set_id, owner)

    if body.name is not None:
        found.name = body.name
    if body.params is not None:
        # Only a params edit bumps the revision; a rename does not change
        # whether two runs were configured the same way.
        found.params = svc.eligible_params(found.family, found.tool, body.params)
        found.revision += 1

    found.touch()
    try:
        await found.save()
    except DuplicateKeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "That name is already taken") from exc
    return ParameterSetOut.of(found)


@router.delete("/{set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_parameter_set(set_id: PydanticObjectId, owner: OwnerDep) -> None:
    found = await _owned(set_id, owner)
    await found.delete()


@router.get("/supported")
async def tool_supports_parameter_sets(
    family: ParamSpecFamily = Query(...), tool: str = Query(...)
) -> dict:
    """Whether this tool declares enough parameters to be worth saving.

    Not owner-scoped: it is a static property of the registry, the same
    reasoning `aligner_schema` records for itself.
    """
    return {"supported": svc.has_parameter_sets(family, tool)}


@router.post("/{set_id}/resolve")
async def resolve_parameter_set(set_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """What of this set still applies, and why the rest does not.

    A POST that does not mutate: it is server-side because the schema and the
    drift rules are backend truth, and computing the comparison in the dialog
    would mean two implementations of the contract that can disagree.
    """
    found = await _owned(set_id, owner)
    try:
        resolution = svc.resolve_params(found.family, found.tool, found.params)
    except svc.UnknownToolError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return {
        "applied": resolution.applied,
        "rejected": [r.model_dump() for r in resolution.rejected],
        "set": {"id": str(found.id), "name": found.name, "revision": found.revision},
    }
