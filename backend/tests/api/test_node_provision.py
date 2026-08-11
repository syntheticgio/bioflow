"""Tests for node provisioning endpoints and executor."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.models.node_provision import NodeProvisionTask

# ---- helpers ----

def _app():
    """Bare FastAPI app with only the nodes router."""
    app = FastAPI()
    app.include_router(pytest.importorskip("app.api.v1.nodes").router)
    return app


@pytest.fixture
def app():
    return _app()


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _clean_node_provisions():
    """Remove any NodeProvisionTask docs after each test."""
    yield
    await NodeProvisionTask.find_all().delete()


# ---- POST /nodes/provision validation ----

async def test_provision_missing_host_returns_422(client):
    res = await client.post("/nodes/provision", json={
        "username": "test", "password": "x", "node_name": "n",
    })
    assert res.status_code == 422


async def test_provision_neither_password_nor_key_returns_422(client):
    res = await client.post("/nodes/provision", json={
        "host": "1.2.3.4", "username": "test", "node_name": "n",
    })
    assert res.status_code == 422


async def test_provision_both_password_and_key_returns_422(client):
    res = await client.post("/nodes/provision", json={
        "host": "1.2.3.4", "username": "test",
        "password": "x", "private_key": "y",
        "node_name": "n",
    })
    assert res.status_code == 422


async def test_provision_valid_request_returns_201_and_task_id(client):
    with patch("app.api.v1.nodes._provision_node", new_callable=lambda: AsyncMock()):
        res = await client.post("/nodes/provision", json={
            "host": "1.2.3.4", "username": "test",
            "password": "x", "node_name": "test-node",
        })
    assert res.status_code == 201
    data = res.json()
    assert "task_id" in data
    assert data["status"] == "provisioning"


async def test_provision_with_private_key_returns_201(client):
    with patch("app.api.v1.nodes._provision_node", new_callable=lambda: AsyncMock()):
        res = await client.post("/nodes/provision", json={
            "host": "1.2.3.4", "username": "test",
            "private_key": "fake-key-content",
            "node_name": "test-node",
        })
    assert res.status_code == 201


# ---- GET /nodes/provision/{task_id} ----

async def test_provision_status_unknown_task_returns_404(client):
    res = await client.get("/nodes/provision/nonexistent")
    assert res.status_code == 404


async def test_provision_status_known_task_returns_fields(client):
    task = NodeProvisionTask(
        task_id="test123",
        status="provisioning",
        phase="validate_ssh",
        message="Connecting…",
        node_name="test-node",
        host="1.2.3.4",
    )
    await task.insert()

    res = await client.get("/nodes/provision/test123")
    assert res.status_code == 200
    data = res.json()
    assert data["task_id"] == "test123"
    assert data["status"] == "provisioning"
    assert data["phase"] == "validate_ssh"
    assert data["message"] == "Connecting…"
    assert data["node_name"] == "test-node"
    assert data["host"] == "1.2.3.4"


async def test_provision_status_success(client):
    task = NodeProvisionTask(
        task_id="done",
        status="success",
        phase="enrolled",
        message="Node enrolled ✓",
        node_name="done-node",
        host="1.2.3.4",
        finished_at=datetime.now(UTC),
    )
    await task.insert()

    res = await client.get("/nodes/provision/done")
    assert res.status_code == 200
    assert res.json()["status"] == "success"


async def test_provision_status_failed(client):
    task = NodeProvisionTask(
        task_id="bad",
        status="failed",
        phase="validate_ssh",
        message="Connection refused",
        error="Connection refused",
        node_name="bad-node",
        host="1.2.3.4",
        finished_at=datetime.now(UTC),
    )
    await task.insert()

    res = await client.get("/nodes/provision/bad")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "failed"
    assert data["error"] == "Connection refused"


# ---- helpers ----

def test_render_node_env_matches_launcher():
    """_render_node_env output must match the launcher's format."""
    from app.api.v1.nodes import _render_node_env

    mongo_url = (
        "mongodb://192.168.1.50:27017/biopipe"
        "?replicaSet=rs0&directConnection=true"
    )
    env = _render_node_env(
        mongo_url=mongo_url,
        redis_url="redis://192.168.1.50:6379/0",
        api_url="http://192.168.1.50:8000",
        node_name="test-node",
        storage_location="/data/scratch",
        worker_replicas=2,
    )

    assert "NODE_TYPE=compute" in env
    assert f"MONGO_URL={mongo_url}" in env
    assert "REDIS_URL=redis://192.168.1.50:6379/0" in env
    assert "WORKER_NODE_ID=test-node" in env
    assert "PRIMARY_API_URL=http://192.168.1.50:8000" in env
    assert "BIOINFO_HOME=/data/scratch" in env
    assert "BIOINFO_REGISTER_ROOTS=/data/scratch" in env
    assert "BIOFLOW_TAG=latest" in env
    assert "WORKER_REPLICAS=2" in env
    # Must NOT contain any SSH credential
    assert "password" not in env.lower()
    assert "private_key" not in env


def test_primary_hostname_uses_config():
    """_primary_hostname returns PRIMARY_HOSTNAME when set."""
    from app.api.v1 import nodes as mod
    from app.api.v1.nodes import _primary_hostname

    with patch.object(mod.settings, "primary_hostname", "myhost.local"):
        assert _primary_hostname() == "myhost.local"


def test_primary_hostname_falls_back_to_socket():
    """_primary_hostname falls back to gethostname when config is empty."""
    import socket

    from app.api.v1 import nodes as mod
    from app.api.v1.nodes import _primary_hostname

    with patch.object(mod.settings, "primary_hostname", ""), \
         patch.object(mod.socket, "socket", side_effect=OSError("no net")):
        name = _primary_hostname()
        assert len(name) > 0
        assert name == socket.gethostname()


def test_rewrite_host_replaces_docker_names():
    """_rewrite_host replaces 'mongo' and 'redis' with the given host."""
    from app.api.v1.nodes import _rewrite_host

    result = _rewrite_host("mongodb://mongo:27017/db", "10.0.0.1")
    assert "10.0.0.1" in result
    assert "mongo" not in result

    result = _rewrite_host("redis://redis:6379/0", "10.0.0.1")
    assert "10.0.0.1" in result
    assert "redis" not in result


def test_provision_request_model_rejects_empty_creds():
    """ProvisionRequest rejects when neither password nor key is set."""
    from app.api.v1.nodes import ProvisionRequest

    with pytest.raises(ValueError, match="password or private_key"):
        ProvisionRequest(
            host="x", username="x",
            password=None, private_key=None,
            node_name="x",
        )


def test_provision_request_model_rejects_both_creds():
    """ProvisionRequest rejects when both password and key are set."""
    from app.api.v1.nodes import ProvisionRequest

    with pytest.raises(ValueError, match="exactly one"):
        ProvisionRequest(
            host="x", username="x",
            password="x", private_key="x",
            node_name="x",
        )


def test_provision_request_accepts_password_only():
    from app.api.v1.nodes import ProvisionRequest

    req = ProvisionRequest(
        host="x", username="x", password="x", node_name="x",
    )
    assert req.password == "x"
    assert req.private_key is None


def test_provision_request_accepts_key_only():
    from app.api.v1.nodes import ProvisionRequest

    req = ProvisionRequest(
        host="x", username="x", private_key="k", node_name="x",
    )
    assert req.private_key == "k"
    assert req.password is None


# ---- orphan cleanup ----

async def test_orphaned_provision_marked_failed(client):
    """Tasks in 'provisioning' status with no active Task are marked failed."""
    task = NodeProvisionTask(
        task_id="orph",
        status="provisioning",
        phase="pull_image",
        node_name="orph-node",
        host="1.2.3.4",
    )
    await task.insert()

    from app.api.v1.nodes import _active_provisions, _clean_orphaned_provisions

    # Ensure the task_id is NOT in _active_provisions
    _active_provisions.pop("orph", None)

    await _clean_orphaned_provisions()

    updated = await NodeProvisionTask.find_one(
        NodeProvisionTask.task_id == "orph"
    )
    assert updated is not None
    assert updated.status == "failed"
    assert updated.error is not None
    assert "restart" in updated.error.lower()
