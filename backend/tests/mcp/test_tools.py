"""Tool behaviour and owner scoping.

Every assertion about scoping is B asking for A's data, following
`tests/api/test_route_owner_scoping.py`. A single profile's request for its
own data succeeds whether or not the tool ever applied a filter, so a test
written that way proves nothing -- which is the direction that fails when the
seam breaks.
"""

import pytest

from app.errors import NotFoundError, ProfileUnresolvedError
from app.mcp import tools
from app.services import profile_service, project_service

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


async def test_list_projects_returns_this_owners_projects():
    profile = await profile_service.create_profile(username="tools-list")
    owner = profile.owner_id()
    await project_service.create_project(name="Mine", owner=owner)

    result = await tools.list_projects(owner=owner)

    assert [p["name"] for p in result["projects"]] == ["Mine"]


async def test_list_projects_does_not_see_another_owners_projects():
    a = await profile_service.create_profile(username="tools-a")
    b = await profile_service.create_profile(username="tools-b")
    await project_service.create_project(name="A's project", owner=a.owner_id())

    result = await tools.list_projects(owner=b.owner_id())

    assert result["projects"] == []


async def test_get_project_treats_another_owners_project_as_missing():
    """Not a 403: answering differently would confirm the id is real, which
    is the reasoning already written on `jobs._owned_job`."""
    a = await profile_service.create_profile(username="tools-get-a")
    b = await profile_service.create_profile(username="tools-get-b")
    project = await project_service.create_project(name="A's", owner=a.owner_id())

    with pytest.raises(NotFoundError):
        await tools.get_project(str(project.id), owner=b.owner_id())


async def test_create_project_assigns_the_acting_owner():
    profile = await profile_service.create_profile(username="tools-create")
    owner = profile.owner_id()

    result = await tools.create_project("New project", owner=owner)

    stored = await project_service.get_project(result["id"], owner=owner)
    assert stored.name == "New project"


async def test_whoami_reports_the_acting_profile():
    profile = await profile_service.create_profile(username="tools-whoami")

    result = await tools.whoami(owner=profile.owner_id())

    assert result["username"] == "tools-whoami"


async def test_whoami_rejects_a_malformed_owner_cleanly():
    """A malformed owner should reach an agent as an actionable error, not a
    raw bson.errors.InvalidId stack trace -- the same trap app.api.deps'
    resolve_owner already guards against for the REST routes."""
    with pytest.raises(ProfileUnresolvedError):
        await tools.whoami(owner="not-a-valid-object-id")
