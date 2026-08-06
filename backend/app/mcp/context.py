"""Turning an MCP request's `?profile=` into an `owner` string.

Every tool goes through `owner_for`. It is deliberately the only way an owner
enters the MCP package: a tool that forgets to call it cannot silently read
another profile's library, because it has no other source for the value.

The fallback to a sole profile is not a convenience shortcut. On a
single-person install -- the common case for this application -- there is
exactly one possible answer, and requiring the query string there would mean
the paste-ready URL in the settings panel is the only way to connect at all.
Where the answer is genuinely ambiguous, this raises instead of picking.
"""

from app.api.deps import resolve_owner
from app.errors import ProfileUnresolvedError
from app.models import Profile


async def owner_for(profile_param: str | None) -> str:
    """The `owner` this MCP request acts as.

    `profile_param` is the raw `?profile=` value, or None when absent.
    """
    if profile_param:
        # resolve_owner already handles "local", malformed ids and unknown
        # ids, raising ProfileUnresolvedError for each. Reusing it is what
        # keeps one definition of what a profile id means.
        return await resolve_owner(profile_param)

    profiles = await Profile.find_all().limit(2).to_list()

    if len(profiles) == 1:
        return profiles[0].owner_id()

    if not profiles:
        raise ProfileUnresolvedError(
            "No profiles exist yet. Create one in BioFlow first, then copy the "
            "MCP connection URL from Settings > MCP."
        )

    raise ProfileUnresolvedError(
        "More than one profile exists, so the MCP connection must say which "
        "one to use. Add ?profile=<id> to the server URL -- Settings > MCP in "
        "BioFlow shows the ready-to-paste URL for the current profile."
    )
