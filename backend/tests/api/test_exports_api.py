"""Create, list, and download project export archives.

`two_profiles` gives real owner ids resolved through the actual `OwnerDep`
path (see `conftest.py`); the project used here is created directly with that
owner via `make_project` so a request sent with `a_headers` actually resolves
to it. `launch_project_export` enqueues through `queue.enqueue`, which pushes
to Redis -- stubbed the same way `test_queue_owner.py` and Task 7's tests do,
since this process has no Redis.
"""

import pytest

from app.queue import queue
from tests.services.helpers import make_project

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    async def _skip(*args, **kwargs):
        return None

    monkeypatch.setattr(queue, "_push_to_redis", _skip)
    monkeypatch.setattr(queue, "publish_event", _skip)


async def test_create_export_returns_a_job(client, two_profiles):
    project = await make_project("export me", owner=two_profiles["a"].owner_id())

    resp = await client.post(
        f"/api/v1/projects/{project.id}/export", headers=two_profiles["a_headers"]
    )

    assert resp.status_code == 202, resp.text
    assert "job_id" in resp.json()


async def test_create_export_404s_for_a_missing_project(client, two_profiles):
    resp = await client.post(
        "/api/v1/projects/000000000000000000000000/export",
        headers=two_profiles["a_headers"],
    )

    assert resp.status_code == 404


async def test_create_export_404s_for_another_owners_project(client, two_profiles):
    project = await make_project("not yours", owner=two_profiles["a"].owner_id())

    resp = await client.post(
        f"/api/v1/projects/{project.id}/export", headers=two_profiles["b_headers"]
    )

    assert resp.status_code == 404


async def test_download_refuses_a_traversal_name(client, two_profiles):
    resp = await client.get(
        "/api/v1/exports/..%2F..%2Fsecret.key/download",
        headers=two_profiles["a_headers"],
    )

    assert resp.status_code in (400, 404)


async def test_list_exports_returns_a_list(client, two_profiles):
    resp = await client.get("/api/v1/exports", headers=two_profiles["a_headers"])

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_download_404s_for_a_nonexistent_export(client, two_profiles):
    resp = await client.get(
        "/api/v1/exports/does-not-exist-1234.tar.gz/download",
        headers=two_profiles["a_headers"],
    )

    assert resp.status_code == 404
