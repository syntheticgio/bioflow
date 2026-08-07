"""The server is reachable at the path the settings panel hands out.

`/api/v1/mcp` rather than a bare `/mcp` because `vite.config.ts` proxies
`/api` -- the versioned path is reachable from both 5173 and 8000 with no new
proxy rule in either vite.config.ts or nginx.conf. This test is what catches
someone "tidying" the path later and silently breaking every configured agent.
"""

import pytest

from app.main import app

# `tests/mcp/conftest.py`'s autouse `clean_profiles` fixture calls
# `Profile.find_all()` before and after every test in this package, and
# Beanie raises `CollectionWasNotInitialized` for any Document class that
# hasn't gone through `init_beanie` yet -- same reasoning as
# tests/mcp/test_surface.py's identical pytestmark, and needed here for the
# same reason even though this test itself is pure route-table introspection.
pytestmark = pytest.mark.usefixtures("beanie_models")


def test_mcp_is_mounted_under_the_versioned_api_path():
    paths = [getattr(r, "path", "") for r in app.routes]

    assert any(p.startswith("/api/v1/mcp") for p in paths), (
        f"No /api/v1/mcp route. Found: {sorted(p for p in paths if 'mcp' in p)}"
    )
