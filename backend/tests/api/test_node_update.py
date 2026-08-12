"""Update endpoints: what can be updated, and what cannot."""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.models.node import Node
from app.models.node_update import NodeUpdateTask

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(pytest.importorskip("app.api.v1.nodes").router)
    return app


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        yield ac


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean():
    yield
    await NodeUpdateTask.find_all().delete()
    await Node.find_all().delete()


async def test_update_rejects_node_without_stored_key(client):
    """A hand-provisioned node has no key: the button is not offered, and the
    endpoint refuses it (NU-18)."""
    await Node(node_id="manual").insert()
    res = await client.post("/nodes/manual/update", json={"drain": True})
    assert res.status_code == 409
    assert "provision" in res.json()["detail"].lower()


async def test_update_rejects_unknown_node(client):
    res = await client.post("/nodes/ghost/update", json={"drain": True})
    assert res.status_code == 404


async def test_update_rejects_concurrent_update(client):
    """NU-19: two updates racing on one node would fight over the container."""
    await Node(node_id="busy", ssh_host="h", ssh_username="u", ssh_key_enc=b"k").insert()
    await NodeUpdateTask(task_id="running", node_id="busy", status="updating").insert()

    res = await client.post("/nodes/busy/update", json={"drain": True})
    assert res.status_code == 409
    assert "already" in res.json()["detail"].lower()


async def test_update_starts_and_returns_task_id(client):
    await Node(node_id="ok", ssh_host="h", ssh_username="u", ssh_key_enc=b"k").insert()

    with patch("app.api.v1.nodes.node_update_service.run_update", AsyncMock()):
        res = await client.post("/nodes/ok/update", json={"drain": True})

    assert res.status_code == 201
    assert res.json()["task_id"]


async def test_update_passes_the_drain_choice(client):
    await Node(node_id="d", ssh_host="h", ssh_username="u", ssh_key_enc=b"k").insert()
    runner = AsyncMock()

    with patch("app.api.v1.nodes.node_update_service.run_update", runner):
        await client.post("/nodes/d/update", json={"drain": False})
        # The endpoint schedules a background task; give it a tick to start.
        import asyncio
        await asyncio.sleep(0.05)

    assert runner.await_args.kwargs["drain"] is False


async def test_update_status_returns_progress(client):
    await NodeUpdateTask(
        task_id="t9", node_id="n", status="updating", phase="pull_image",
        message="Pulling…",
    ).insert()

    res = await client.get("/nodes/update/t9")
    assert res.status_code == 200
    assert res.json()["phase"] == "pull_image"


async def test_update_status_404_for_unknown_task(client):
    res = await client.get("/nodes/update/nope")
    assert res.status_code == 404


async def test_orphaned_updates_are_failed_on_startup():
    """An API restart mid-update leaves a task nothing will ever finish."""
    from app.api.v1.nodes import _clean_orphaned_provisions

    await NodeUpdateTask(task_id="orphan", node_id="n", status="updating").insert()
    await _clean_orphaned_provisions()

    task = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "orphan")
    assert task.status == "failed"
    assert "restart" in task.error.lower()
