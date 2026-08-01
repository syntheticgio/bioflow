"""Shared fixtures for the API tests.

`client` and `two_profiles` live here rather than in each test module because
every router that resolves `OwnerDep` needs the same two things to be tested at
all: an ASGI client that can send headers, and a pair of real profiles whose
owner ids can go into them. Repeating that setup per file is how one file ends
up quietly asserting something weaker than its neighbours.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models import Profile
from app.services import profile_service


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(loop_scope="module")
async def two_profiles():
    """Profiles A and B, and the headers that address each.

    Two is the minimum that can prove anything: a single profile's request for
    its own data succeeds whether or not the route ever applied a filter, so
    every isolation assertion in this package is B asking for A's row.

    Profiles are wiped first because the module-scoped database is shared and
    `adopted_legacy_owner` is global state -- a profile left behind by a
    neighbouring module can otherwise claim the "local" owner mid-test.
    """
    await Profile.find_all().delete()
    a = await profile_service.create_profile(username="owner-a")
    b = await profile_service.create_profile(username="owner-b")
    yield {
        "a": a,
        "b": b,
        "a_headers": {"X-BioFlow-Profile": a.owner_id()},
        "b_headers": {"X-BioFlow-Profile": b.owner_id()},
    }
    await Profile.find_all().delete()
