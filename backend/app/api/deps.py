"""FastAPI dependencies shared across routers.

`get_current_owner` is the seam every partitioned query goes through: it turns
the client-supplied X-BioFlow-Profile header into the `owner` string that
service functions take as an explicit parameter. See
docs/superpowers/specs/2026-07-31-profiles-design.md, "Request scoping" -- the
explicit-parameter choice there is why this dependency returns a plain `str`
rather than stashing anything in request state.

Rejecting an unknown header is *not* authentication. Profiles are an
organizational boundary; the API stays unauthenticated, so any client can send
any profile's id and get that profile's data. The 404 below exists to catch a
stale or mistyped header before it silently partitions someone's library into a
profile that does not exist, not to keep anyone out.
"""

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import Header, HTTPException

from app.models import Profile


async def get_current_owner(
    x_bioflow_profile: str | None = Header(default=None),
) -> str:
    if not x_bioflow_profile:
        raise HTTPException(status_code=400, detail="X-BioFlow-Profile header is required")

    # "local" is the one owner id that is not a real ObjectId. An installation
    # that predates this feature has documents already carrying the
    # `owner: "local"` default from TimestampedDocument, and one profile adopts
    # them by returning the literal "local" from `Profile.owner_id()` instead of
    # its own id -- so nothing has to be migrated.
    #
    # We cannot yet tell *which* profile that is. Adoption is marked by an
    # `adopted_legacy_owner` flag that a later task adds, and until it exists
    # there is no honest way to identify the adopted profile: it is emphatically
    # not "the one named local" -- a user's first profile might be called "ada"
    # and still be the adopted one, so matching on username would resolve the
    # header for the wrong profile, or fail for the right one.
    #
    # So this branch deliberately checks only that *some* profile exists, which
    # is all that can be verified today and is exactly right in the single
    # adopted profile case that is the only way a "local" header can be produced
    # before that flag lands. The `any profile at all` check still catches the
    # case worth catching here: a header arriving against an empty database.
    if x_bioflow_profile == "local":
        if await Profile.find_one() is None:
            raise HTTPException(status_code=404, detail="No profile exists to own 'local'")
        return "local"

    try:
        profile_id = PydanticObjectId(x_bioflow_profile)
    except InvalidId as e:
        # bson raises InvalidId, which is a BSONError -- *not* a ValueError.
        # Catching the wrong type turns a typo'd header into an unhandled 500.
        raise HTTPException(status_code=400, detail="Malformed profile id") from e

    profile = await Profile.get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Unknown profile: {x_bioflow_profile}")

    return profile.owner_id()
