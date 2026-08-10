"""FastAPI dependencies shared across routers.

`get_current_owner` is the seam every partitioned query goes through: it turns
the client-supplied X-BioFlow-Profile header into the `owner` string that
service functions take as an explicit parameter. The resolution itself lives in
`resolve_owner`, which the SSE route calls directly with a query parameter --
`EventSource` cannot send headers. See
docs/superpowers/specs/2026-07-31-profiles-design.md, "Request scoping" -- the
explicit-parameter choice there is why this dependency returns a plain `str`
rather than stashing anything in request state.

Rejecting an unknown header is *not* authentication. Profiles are an
organizational boundary; the API stays unauthenticated, so any client can send
any profile's id and get that profile's data. The rejection below exists to
catch a stale or mistyped header before it silently partitions someone's
library into a profile that does not exist, not to keep anyone out.
"""

from contextvars import ContextVar
from typing import Annotated

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import Depends, Header, Query

from app.errors import ProfileUnresolvedError
from app.models import Profile


async def resolve_owner(value: str | None) -> str:
    """Turn a client-supplied profile id into an `owner` string.

    Split out from `get_current_owner` because the SSE stream needs the same
    resolution from a *query parameter*: `EventSource` cannot send custom
    headers, which is a limitation of the browser API rather than a second
    sanctioned way into the application. Every other route goes through the
    header and the `OwnerDep` alias below.
    """
    if not value:
        # Names both carriers rather than just the header: the SSE stream comes
        # through here with a query parameter, and a message saying a header is
        # missing would send someone looking for one that was never sent.
        raise ProfileUnresolvedError(
            "No profile supplied (X-BioFlow-Profile header, or ?profile= on /events)"
        )

    # "local" is the one owner id that is not a real ObjectId: documents from
    # before this feature already carry the `owner: "local"` default from
    # TimestampedDocument, and one profile adopts them by returning the literal
    # "local" from `Profile.owner_id()` instead of its own id, so nothing has to
    # be migrated. That profile is identified by its `adopted_legacy_owner`
    # flag and not by its name -- the adopted profile can be called anything.
    if value == "local":
        if await Profile.find_one({"adopted_legacy_owner": True}) is None:
            raise ProfileUnresolvedError("No profile exists to own 'local'")
        return "local"

    try:
        profile_id = PydanticObjectId(value)
    except InvalidId as e:
        # bson raises InvalidId, which is a BSONError -- *not* a ValueError.
        # Catching the wrong type turns a typo'd header into an unhandled 500.
        raise ProfileUnresolvedError("Malformed profile id") from e

    profile = await Profile.get(profile_id)
    if profile is None:
        raise ProfileUnresolvedError(f"Unknown profile: {value}")

    return profile.owner_id()


async def get_current_owner(
    x_bioflow_profile: str | None = Header(default=None),
) -> str:
    return await resolve_owner(x_bioflow_profile)


# Routes take `owner: OwnerDep` rather than repeating the full `Depends(...)`
# annotation. Roughly a dozen route files follow this one, and the alias keeps
# the dependency named in a single place -- swapping what resolution does is
# then a change here, not in every signature that consumes an owner.
OwnerDep = Annotated[str, Depends(get_current_owner)]


target_node_ctx: ContextVar[str | None] = ContextVar("target_node", default=None)


async def get_current_owner_linkable(
    x_bioflow_profile: str | None = Header(default=None),
    profile: str | None = Query(default=None),
) -> str:
    """`OwnerDep`, plus a `?profile=` fallback for routes reached by a plain
    `<a href>` link rather than the app's own `fetch` wrapper.

    A browser-native navigation -- a download link, a report opened in a new
    tab -- never runs the JS that attaches `X-BioFlow-Profile`, the same
    constraint `resolve_owner`'s docstring already notes for `EventSource`.
    The header still wins when both are somehow present, matching
    `profileHeaders()` being the only thing that sets it from application
    code; the query param exists for markup the app does not control at
    request time.
    """
    return await resolve_owner(x_bioflow_profile or profile)


LinkableOwnerDep = Annotated[str, Depends(get_current_owner_linkable)]

async def get_profile_id(
    x_bioflow_profile: str | None = Header(default=None),
    profile: str | None = Query(default=None),
) -> str:
    """The raw profile id, for routes that must forward it rather than its owner.

    `get_current_owner` returns the `owner` string because every partitioned
    query wants that. The agent endpoints want the client-supplied value
    unchanged: the agent subprocess embeds it in `?profile=` on its MCP
    connection, and the MCP server accepts exactly these values. Validation
    still goes through `resolve_owner`, so both routes agree on what a valid
    id is; only the return value differs.

    Header wins when both are present, matching `profileHeaders()` being the
    only thing that sets it from application code; the query parameter exists
    for the SSE stream, which `EventSource` cannot attach a header to.
    """
    value = x_bioflow_profile or profile
    await resolve_owner(value)
    return value

ProfileIdDep = Annotated[str, Depends(get_profile_id)]
