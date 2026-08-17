"""The local-databases HTTP surface: submit and list."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.local_database import URL_MAX_LENGTH, LocalDatabase

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestSubmitLocalDatabase:
    async def test_persists_a_valid_submission(self, client):
        await LocalDatabase.find_all().delete()
        r = await client.post(
            "/api/v1/local-databases",
            json={
                "name": "Lab reference genome",
                "url": "https://example.org/genome.fasta",
                "category": "reference_assembly",
            },
        )

        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Lab reference genome"
        assert body["url"] == "https://example.org/genome.fasta"
        assert body["category"] == "reference_assembly"
        assert "id" in body
        assert "created_at" in body
        assert await LocalDatabase.find_all().count() == 1

    async def test_rejects_a_url_over_the_limit(self, client):
        r = await client.post(
            "/api/v1/local-databases",
            json={
                "name": "Too long",
                "url": "https://example.org/" + ("x" * URL_MAX_LENGTH),
                "category": "other",
            },
        )

        assert r.status_code == 422

    async def test_rejects_an_empty_name(self, client):
        r = await client.post(
            "/api/v1/local-databases",
            json={"name": "", "url": "https://example.org", "category": "other"},
        )

        assert r.status_code == 422

    async def test_rejects_an_invalid_category(self, client):
        r = await client.post(
            "/api/v1/local-databases",
            json={"name": "X", "url": "https://example.org", "category": "not_a_real_category"},
        )

        assert r.status_code == 422

    async def test_rejects_a_malformed_url(self, client):
        r = await client.post(
            "/api/v1/local-databases",
            json={"name": "X", "url": "not-a-url", "category": "other"},
        )

        assert r.status_code == 422


class TestListLocalDatabases:
    async def test_lists_submissions_newest_first(self, client):
        await LocalDatabase.find_all().delete()
        await LocalDatabase(
            name="first", url="https://example.org/a", category="other"
        ).insert()
        # Distinct created_at: Mongo sorts newest-first, and identical
        # timestamps would make the sort order non-deterministic.
        await asyncio.sleep(0.01)
        await LocalDatabase(
            name="second", url="https://example.org/b", category="annotation"
        ).insert()

        r = await client.get("/api/v1/local-databases")

        assert r.status_code == 200
        names = [item["name"] for item in r.json()]
        assert names == ["second", "first"]

    async def test_returns_empty_list_when_none_submitted(self, client):
        await LocalDatabase.find_all().delete()

        r = await client.get("/api/v1/local-databases")

        assert r.status_code == 200
        assert r.json() == []
