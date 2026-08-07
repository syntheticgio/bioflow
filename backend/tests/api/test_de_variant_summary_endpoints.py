"""The DE/variant summary endpoints mirror /pipelines/summary/* exactly --
same status/launch shape, different slot and object role.

Fixture names match the rest of backend/tests/api/: `client` and
`two_profiles` (with its `a_headers`) come from tests/api/conftest.py, the
same fixtures test_summary_status.py and test_route_owner_scoping.py use.
There is no `owner_headers` fixture in this suite -- the plan's snippet used
it as a placeholder.
"""

import pytest

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


class TestDeSummaryStatus:
    async def test_reports_unavailable_with_no_provider(self, client):
        resp = await client.get("/api/v1/pipelines/de-summary/status")
        assert resp.status_code == 200
        assert resp.json()["available"] is False


class TestVariantSummaryStatus:
    async def test_reports_unavailable_with_no_provider(self, client):
        resp = await client.get("/api/v1/pipelines/variant-summary/status")
        assert resp.status_code == 200
        assert resp.json()["available"] is False


class TestLaunchDeSummary:
    async def test_404s_for_a_nonexistent_object(self, client, two_profiles):
        resp = await client.post(
            "/api/v1/pipelines/de-summary",
            json={"object_id": "000000000000000000000000"},
            headers=two_profiles["a_headers"],
        )
        assert resp.status_code == 404


class TestLaunchVariantSummary:
    async def test_404s_for_a_nonexistent_object(self, client, two_profiles):
        resp = await client.post(
            "/api/v1/pipelines/variant-summary",
            json={"object_id": "000000000000000000000000"},
            headers=two_profiles["a_headers"],
        )
        assert resp.status_code == 404
