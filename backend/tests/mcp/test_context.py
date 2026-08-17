"""Profile resolution for MCP requests.

The MCP transport has no startup picker and cannot send the X-BioFlow-Profile
header the web UI uses, so the profile arrives as a query parameter -- the
same accommodation `deps.resolve_owner` already makes for the SSE stream.
"""

import pytest

from app.errors import ProfileUnresolvedError
from app.mcp import context
from app.services import profile_service

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


async def test_explicit_profile_resolves_to_its_owner():
    profile = await profile_service.create_profile(username="mcp-explicit")

    owner = await context.owner_for(str(profile.id))

    assert owner == profile.owner_id()


async def test_absent_profile_falls_back_to_the_only_profile():
    """A single-person install should not need the query string at all.

    This cannot guess wrong: there is nothing to guess between.
    """
    profile = await profile_service.create_profile(username="mcp-sole")

    owner = await context.owner_for(None)

    assert owner == profile.owner_id()


async def test_absent_profile_with_two_profiles_names_the_parameter():
    await profile_service.create_profile(username="mcp-ambiguous-a")
    await profile_service.create_profile(username="mcp-ambiguous-b")

    with pytest.raises(ProfileUnresolvedError) as exc:
        await context.owner_for(None)

    # The message has to say what to add and where to get it: an agent reads
    # this string to decide what to do next, and "no profile" alone tells it
    # nothing actionable.
    assert "?profile=" in str(exc.value)


async def test_unknown_profile_is_rejected():
    with pytest.raises(ProfileUnresolvedError):
        await context.owner_for("507f1f77bcf86cd799439011")


async def test_empty_string_profile_falls_back_like_absent():
    """`?profile=` with no value and no `?profile=` at all must behave the
    same way. This pins that choice: `owner_for` mirrors `resolve_owner`'s own
    `if not value:` truthiness check rather than treating "" as a real,
    resolvable id.
    """
    profile = await profile_service.create_profile(username="mcp-empty-param")

    owner = await context.owner_for("")

    assert owner == profile.owner_id()
