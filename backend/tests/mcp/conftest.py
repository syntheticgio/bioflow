"""Fixtures for the MCP tests.

Profiles are created per-test by the tests that need them rather than by a
shared fixture: `tests/api/conftest.py`'s `two_profiles` docstring records why
a fixture that deletes broadly is hazardous here, and the MCP tests need
different profile counts per case (zero, one, two) which a single fixture
cannot express.
"""

import pytest_asyncio
from app.models import Profile


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean_profiles():
    """Each MCP test starts with no profiles.

    Required rather than convenient: `owner_for(None)` branches on how many
    profiles exist, so a row left behind by a neighbouring test changes what
    this module's fallback tests assert.
    """
    await Profile.find_all().delete()
    yield
    await Profile.find_all().delete()
