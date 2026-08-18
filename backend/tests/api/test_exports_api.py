"""Create, list, and download project export archives.

`two_profiles` gives real owner ids resolved through the actual `OwnerDep`
path (see `conftest.py`); the project used here is created directly with that
owner via `make_project` so a request sent with `a_headers` actually resolves
to it. `launch_project_export` enqueues through `queue.enqueue`, which pushes
to Redis -- stubbed the same way `test_queue_owner.py` and Task 7's tests do,
since this process has no Redis.
"""

import pytest

from app.config import settings
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


async def test_list_exports_returns_a_list(client, two_profiles, tmp_path, monkeypatch):
    owner = two_profiles["a"].owner_id()
    prefix = f"{owner}__"
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    monkeypatch.setattr(settings, "exports_dir", exports_dir)

    # Create a test archive for profile A
    (exports_dir / f"{prefix}my-project-20240818T120000Z.tar.gz").write_text("fake archive")
    # Create an archive for another profile
    other_owner = two_profiles["b"].owner_id()
    (exports_dir / f"{other_owner}__other-project-20240818T120000Z.tar.gz").write_text("fake archive")

    resp = await client.get("/api/v1/exports", headers=two_profiles["a_headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == f"{prefix}my-project-20240818T120000Z.tar.gz"


async def test_list_exports_is_owner_scoped(client, two_profiles, tmp_path, monkeypatch):
    exports_dir = tmp_path / "exports2"
    exports_dir.mkdir()
    monkeypatch.setattr(settings, "exports_dir", exports_dir)

    # Create archives for both profiles
    a_owner = two_profiles["a"].owner_id()
    b_owner = two_profiles["b"].owner_id()
    (exports_dir / f"{a_owner}__a-project.tar.gz").write_text("a data")
    (exports_dir / f"{b_owner}__b-project.tar.gz").write_text("b data")

    # Profile B should only see their own
    resp = await client.get("/api/v1/exports", headers=two_profiles["b_headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == f"{b_owner}__b-project.tar.gz"


async def test_download_export_404s_for_another_owners_archive(client, two_profiles, tmp_path, monkeypatch):
    exports_dir = tmp_path / "exports3"
    exports_dir.mkdir()
    monkeypatch.setattr(settings, "exports_dir", exports_dir)

    a_owner = two_profiles["a"].owner_id()
    b_owner = two_profiles["b"].owner_id()
    a_file = exports_dir / f"{a_owner}__a-project.tar.gz"
    a_file.write_text("a data")

    # Profile B tries to download profile A's archive
    resp = await client.get(
        f"/api/v1/exports/{a_file.name}/download",
        headers=two_profiles["b_headers"],
    )
    assert resp.status_code == 404


async def test_download_404s_for_a_nonexistent_export(client, two_profiles):
    resp = await client.get(
        "/api/v1/exports/does-not-exist-1234.tar.gz/download",
        headers=two_profiles["a_headers"],
    )

    assert resp.status_code == 404
