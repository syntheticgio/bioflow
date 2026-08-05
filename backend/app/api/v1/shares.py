"""Share endpoints: offer, inbox/outbox, accept, decline, revoke.

Every route here takes `OwnerDep`, unlike `profiles.py` -- sharing always has
a caller who already has a profile, unlike the picker's own endpoints, which
are what a client calls before it has one.
"""

from beanie import PydanticObjectId
from fastapi import APIRouter, status

from app.api.deps import OwnerDep
from app.api.v1.schemas import ShareAccept, ShareCreate, ShareOut
from app.models import ShareState
from app.services import share_service

router = APIRouter(prefix="/shares", tags=["shares"])


async def _out(share) -> ShareOut:
    parties = await share_service.resolve_owner_profiles([share.from_owner, share.to_owner])
    return ShareOut.of(share, parties)


async def _out_many(shares: list) -> list[ShareOut]:
    if not shares:
        return []
    owners = {o for s in shares for o in (s.from_owner, s.to_owner)}
    parties = await share_service.resolve_owner_profiles(owners)
    return [ShareOut.of(s, parties) for s in shares]


@router.post("", response_model=ShareOut, status_code=status.HTTP_201_CREATED)
async def offer_share(body: ShareCreate, owner: OwnerDep) -> ShareOut:
    share = await share_service.offer_share(
        owner=owner,
        object_id=PydanticObjectId(body.object_id),
        to_profile_id=body.to_profile_id,
        message=body.message,
    )
    return await _out(share)


@router.get("/inbox", response_model=list[ShareOut])
async def list_inbox(owner: OwnerDep) -> list[ShareOut]:
    """Pending offers made to this profile."""
    shares = await share_service.list_inbox(owner=owner, state=ShareState.OFFERED)
    return await _out_many(shares)


@router.get("/outbox", response_model=list[ShareOut])
async def list_outbox(owner: OwnerDep) -> list[ShareOut]:
    """Every offer this profile has made, in every state."""
    shares = await share_service.list_outbox(owner=owner)
    return await _out_many(shares)


@router.post("/{share_id}/accept", response_model=ShareOut, status_code=status.HTTP_200_OK)
async def accept_share(share_id: PydanticObjectId, body: ShareAccept, owner: OwnerDep) -> ShareOut:
    project_id = PydanticObjectId(body.project_id) if body.project_id else None
    await share_service.accept_share(owner=owner, share_id=share_id, project_id=project_id)
    share = await share_service.load_for_recipient(share_id, owner=owner)
    return await _out(share)


@router.post("/{share_id}/decline", response_model=ShareOut)
async def decline_share(share_id: PydanticObjectId, owner: OwnerDep) -> ShareOut:
    share = await share_service.decline_share(owner=owner, share_id=share_id)
    return await _out(share)


@router.delete("/{share_id}", response_model=ShareOut)
async def revoke_share(share_id: PydanticObjectId, owner: OwnerDep) -> ShareOut:
    """Withdraw an un-accepted offer. Refused (409) once accepted -- see
    share_service.revoke_share."""
    share = await share_service.revoke_share(owner=owner, share_id=share_id)
    return await _out(share)
