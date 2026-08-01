"""FastAPI dependencies shared across routers.

`get_current_owner` is the seam every partitioned query goes through: it turns
the client-supplied X-BioFlow-Profile header into the `owner` string that
service functions take as an explicit parameter. See
docs/superpowers/specs/2026-07-31-profiles-design.md, "Request scoping" -- the
explicit-parameter choice there is why this dependency returns a plain `str`
rather than stashing anything in request state.

Rejecting an unknown header is *not* authentication. Profiles are an
organizational boundary; the API stays unauthenticated, so any client can send
any profile's id and get that profile's data. The rejection below exists to
catch a stale or mistyped header before it silently partitions someone's
library into a profile that does not exist, not to keep anyone out.
"""

from typing import Annotated

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import Depends, Header

from app.errors import ProfileUnresolvedError
from app.models import Profile


async def get_current_owner(
    x_bioflow_profile: str | None = Header(default=None),
) -> str:
    if not x_bioflow_profile:
        raise ProfileUnresolvedError("X-BioFlow-Profile header is required")

    # "local" is the one owner id that is not a real ObjectId: documents from
    # before this feature already carry the `owner: "local"` default from
    # TimestampedDocument, and one profile adopts them by returning the literal
    # "local" from `Profile.owner_id()` instead of its own id, so nothing has to
    # be migrated. That profile is identified by its `adopted_legacy_owner`
    # flag and not by its name -- the adopted profile can be called anything.
    if x_bioflow_profile == "local":
        if await Profile.find_one({"adopted_legacy_owner": True}) is None:
            raise ProfileUnresolvedError("No profile exists to own 'local'")
        return "local"

    try:
        profile_id = PydanticObjectId(x_bioflow_profile)
    except InvalidId as e:
        # bson raises InvalidId, which is a BSONError -- *not* a ValueError.
        # Catching the wrong type turns a typo'd header into an unhandled 500.
        raise ProfileUnresolvedError("Malformed profile id") from e

    profile = await Profile.get(profile_id)
    if profile is None:
        raise ProfileUnresolvedError(f"Unknown profile: {x_bioflow_profile}")

    return profile.owner_id()


# Routes take `owner: OwnerDep` rather than repeating the full `Depends(...)`
# annotation. Roughly a dozen route files follow this one, and the alias keeps
# the dependency named in a single place -- swapping what resolution does is
# then a change here, not in every signature that consumes an owner.
OwnerDep = Annotated[str, Depends(get_current_owner)]
