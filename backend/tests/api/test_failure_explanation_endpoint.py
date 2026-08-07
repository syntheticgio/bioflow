"""The failure explanation endpoint mirrors GET /pipelines/organism/{organism}
exactly -- returns null rather than 404 when there is nothing to say, since
no provider and a model producing nothing are both ordinary states for a
decorative field, not errors the client should handle differently.
"""

import pytest

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def test_returns_null_with_no_provider_configured(client):
    resp = await client.get(
        "/api/v1/pipelines/failure-explanation",
        params={"code": "CalledProcessError", "message": "exit status 1"},
    )
    assert resp.status_code == 200
    assert resp.json() is None
