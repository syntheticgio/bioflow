"""Shared fixtures for the API tests.

`client` and `two_profiles` live here rather than in each test module because
every router that resolves `OwnerDep` needs the same two things to be tested at
all: an ASGI client that can send headers, and a pair of real profiles whose
owner ids can go into them. Repeating that setup per file is how one file ends
up quietly asserting something weaker than its neighbours.
"""

import itertools

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import profile_service


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# Usernames carry a per-process counter because `username` is unique and this
# fixture is function-scoped: reusing "owner-a" across tests collides with a row
# a previous test has not torn down yet. The counter is only ever appended to a
# name this fixture owns, which is what makes the targeted delete below safe.
_profile_seq = itertools.count()


@pytest_asyncio.fixture(loop_scope="module")
async def two_profiles():
    """Profiles A and B, and the headers that address each.

    Two is the minimum that can prove anything: a single profile's request for
    its own data succeeds whether or not the route ever applied a filter, so
    every isolation assertion in this package is B asking for A's row.

    This fixture deletes *only the two profiles it created*, by id. It used to
    open and close with `Profile.find_all().delete()`, and the reason that was
    wrong is worth spelling out, because the docstring it replaced named the
    exact hazard the code then went on to cause.

    `loop_scope="module"` sets the event loop the fixture runs on. It does not
    set the fixture's caching scope -- there is no `scope="module"` here, so
    this is function-scoped and runs once per test that requests it. A
    `find_all().delete()` therefore wiped the entire shared `profiles`
    collection twice per test, and `beanie_models` hands every module in the
    suite the same database. Any neighbouring module holding a profile across
    its own tests would have had it deleted mid-run by a test in this package.

    Neither profile is adopted, so nothing here contends for the `"local"`
    owner or the `uniq_adopted_legacy_owner` partial index -- the hazard the
    old wipe claimed to be protecting against. A module that needs an adopted
    profile creates and removes its own.
    """
    n = next(_profile_seq)
    a = await profile_service.create_profile(username=f"owner-a-{n}")
    b = await profile_service.create_profile(username=f"owner-b-{n}")
    yield {
        "a": a,
        "b": b,
        "a_headers": {"X-BioFlow-Profile": a.owner_id()},
        "b_headers": {"X-BioFlow-Profile": b.owner_id()},
    }
    await a.delete()
    await b.delete()
