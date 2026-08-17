"""Tests for node enrollment, status, and revocation endpoints."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from app.errors import register_exception_handlers
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---- helpers ----


def _app():
    """Bare FastAPI app with only the nodes router."""
    app = FastAPI()
    app.include_router(pytest.importorskip("app.api.v1.nodes").router)
    register_exception_handlers(app)
    return app


@contextmanager
def _patch_settings(**overrides):
    """Patch app.api.v1.nodes.settings with the given overrides."""
    mgr = patch("app.api.v1.nodes.settings")
    mock = mgr.__enter__()
    for k, v in overrides.items():
        setattr(mock, k, v)
    try:
        yield mock
    finally:
        mgr.__exit__(None, None, None)


def _node_doc(node_id="child-1", hostname="child-laptop", status="active"):
    """Return a mock Node document with async-save support."""
    doc = type(
        "MockNode",
        (),
        {
            "node_id": node_id,
            "hostname": hostname,
            "status": status,
            "last_seen": datetime.now(UTC),
            "registered_at": datetime.now(UTC),
            "image_digest": None,
            "version": None,
            "ssh_key_enc": None,
            "save": AsyncMock(),
            "insert": AsyncMock(),
        },
    )()
    return doc


# ---- patching Node ----


@contextmanager
def _patch_node_find(find_one_return=None):
    """Patch Node.find_one and Node.node_id in ``app.api.v1.nodes``."""
    from app.api.v1 import nodes as view  # noqa: PLC0415

    async def _fake_find_one(*args, **kwargs):
        return find_one_return

    with (
        patch.object(view.Node, "node_id", create=True),
        patch.object(view.Node, "find_one", side_effect=_fake_find_one),
    ):
        yield


@contextmanager
def _patch_node_full():
    """Replace ``Node`` in ``app.api.v1.nodes`` with a simple mock class."""
    from app.api.v1 import nodes as view  # noqa: PLC0415

    original = view.Node

    class MockNode:
        node_id: str = "mock-node"
        hostname: str = ""
        last_seen: object = None
        status: str = "active"
        registered_at: object = None
        _find_one_return: object = None
        _save_called: bool = False
        _insert_called: bool = False

        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

        @classmethod
        async def find_one(cls, *args, **kwargs):
            return cls._find_one_return

        async def save(self):
            MockNode._save_called = True

        async def insert(self):
            MockNode._insert_called = True

    view.Node = MockNode
    try:
        yield MockNode
    finally:
        view.Node = original


# ---- enroll ---


class TestEnroll:
    async def test_enrolls_new_node_without_key(self):
        with _patch_settings(enrollment_key=""), _patch_node_full() as MockNode:
            MockNode._find_one_return = None
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.post(
                    "/nodes/enroll",
                    json={"node_id": "child-1", "hostname": "child-laptop"},
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["node_id"] == "child-1"
            assert data["status"] == "active"
            assert MockNode._insert_called

    async def test_enrolls_with_correct_key(self):
        with _patch_settings(enrollment_key="secret123"), _patch_node_full() as MockNode:
            MockNode._find_one_return = None
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.post(
                    "/nodes/enroll",
                    json={
                        "node_id": "child-1",
                        "hostname": "child-laptop",
                        "enrollment_key": "secret123",
                    },
                )
            assert resp.status_code == 200

    async def test_rejects_wrong_key(self):
        with _patch_settings(enrollment_key="secret123"), _patch_node_full() as MockNode:
            MockNode._find_one_return = None
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.post(
                    "/nodes/enroll",
                    json={
                        "node_id": "child-1",
                        "hostname": "child-laptop",
                        "enrollment_key": "wrong",
                    },
                )
            assert resp.status_code == 403

    async def test_rejects_missing_key_when_required(self):
        with _patch_settings(enrollment_key="secret123"), _patch_node_full() as MockNode:
            MockNode._find_one_return = None
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.post(
                    "/nodes/enroll",
                    json={"node_id": "child-1", "hostname": "child-laptop"},
                )
            assert resp.status_code == 403

    async def test_rejects_revoked_node(self):
        revoked = _node_doc(status="revoked")
        with _patch_settings(enrollment_key=""), _patch_node_full() as MockNode:
            MockNode._find_one_return = revoked
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.post(
                    "/nodes/enroll",
                    json={"node_id": "child-1", "hostname": "child-laptop"},
                )
            assert resp.status_code == 403

    async def test_idempotent_re_enroll_updates_hostname(self):
        existing = _node_doc(status="active")
        with _patch_settings(enrollment_key=""), _patch_node_full() as MockNode:
            MockNode._find_one_return = existing
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.post(
                    "/nodes/enroll",
                    json={"node_id": "child-1", "hostname": "new-hostname"},
                )
            assert resp.status_code == 200
            assert existing.hostname == "new-hostname"
            existing.save.assert_awaited_once()

    async def test_requires_node_id(self):
        with _patch_settings(enrollment_key=""):
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.post(
                    "/nodes/enroll",
                    json={"hostname": "child-laptop"},
                )
            assert resp.status_code == 422


# ---- status ----


class TestNodeStatus:
    async def test_returns_active(self):
        with _patch_node_find(_node_doc(status="active")):
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.get("/nodes/child-1/status")
            assert resp.status_code == 200
            assert resp.json()["status"] == "active"

    async def test_returns_revoked(self):
        with _patch_node_find(_node_doc(status="revoked")):
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.get("/nodes/child-1/status")
            assert resp.status_code == 200
            assert resp.json()["status"] == "revoked"

    async def test_404_for_unknown_node(self):
        with _patch_node_find(None):
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.get("/nodes/unknown/status")
            assert resp.status_code == 404


# ---- revoke ----


class TestRevoke:
    async def test_revokes_active_node(self):
        node = _node_doc(status="active")
        with _patch_node_find(node):
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.delete("/nodes/child-1")
            assert resp.status_code == 200
            assert resp.json()["status"] == "revoked"
            assert node.status == "revoked"
            node.save.assert_awaited_once()

    async def test_404_for_unknown_node(self):
        with _patch_node_find(None):
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as c:
                resp = await c.delete("/nodes/unknown")
            assert resp.status_code == 404


# ---- enhanced list_nodes (merges MongoDB) ----


class TestListNodesWithEnrollment:
    async def test_merges_mongo_enrollment_with_redis_live_data(self):
        import fakeredis.aioredis

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

        now = datetime.now(UTC)
        await redis.hset(
            "bp:workers",
            "host1:1234",
            json.dumps(
                {
                    "last_seen": now.isoformat(),
                    "slots": 4,
                    "running": [],
                    "draining": False,
                    "node_id": "child-1",
                }
            ),
        )
        await redis.mset(
            {
                "bp:conc:cpu:child-1": "0",
                "bp:conc:mem_mb:child-1": "0",
                "bp:conc:io_heavy:child-1": "0",
            }
        )

        mongo_node = _node_doc(node_id="child-1", status="active")

        class AsyncIterMock:
            def __aiter__(self):
                self._items = [mongo_node]
                self._idx = 0
                return self

            async def __anext__(self):
                if self._idx >= len(self._items):
                    raise StopAsyncIteration
                item = self._items[self._idx]
                self._idx += 1
                return item

        redis_patch = patch("app.queue.worker_registry.get_redis", return_value=redis)
        mongo_patch = patch(
            "app.api.v1.nodes.Node.find_all", return_value=AsyncIterMock()
        )

        redis_patch.start()
        mongo_patch.start()
        try:
            from app.api.v1.nodes import list_nodes

            nodes = await list_nodes()
            assert len(nodes) == 1
            assert nodes[0]["node_id"] == "child-1"
            assert nodes[0]["online"] is True
            assert nodes[0]["enrollment"] == "active"
            assert nodes[0]["hostname"] == "child-laptop"
        finally:
            redis_patch.stop()
            mongo_patch.stop()
            await redis.aclose()

    async def test_revoked_node_shows_in_list(self):
        import fakeredis.aioredis

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

        mongo_node = _node_doc(node_id="child-1", status="revoked")

        class AsyncIterMock:
            def __aiter__(self):
                self._items = [mongo_node]
                self._idx = 0
                return self

            async def __anext__(self):
                if self._idx >= len(self._items):
                    raise StopAsyncIteration
                item = self._items[self._idx]
                self._idx += 1
                return item

        redis_patch = patch("app.queue.worker_registry.get_redis", return_value=redis)
        mongo_patch = patch(
            "app.api.v1.nodes.Node.find_all", return_value=AsyncIterMock()
        )

        redis_patch.start()
        mongo_patch.start()
        try:
            from app.api.v1.nodes import list_nodes

            nodes = await list_nodes()
            assert len(nodes) == 1
            assert nodes[0]["enrollment"] == "revoked"
            assert nodes[0]["online"] is False
        finally:
            redis_patch.stop()
            mongo_patch.stop()
            await redis.aclose()

    async def test_backcompat_without_mongo(self):
        import fakeredis.aioredis

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

        now = datetime.now(UTC)
        await redis.hset(
            "bp:workers",
            "host1:1234",
            json.dumps(
                {
                    "last_seen": now.isoformat(),
                    "slots": 2,
                    "running": [],
                    "draining": False,
                    "node_id": "primary",
                }
            ),
        )
        await redis.mset(
            {
                "bp:conc:cpu:primary": "0",
                "bp:conc:mem_mb:primary": "0",
                "bp:conc:io_heavy:primary": "0",
            }
        )

        mongo_patch = patch(
            "app.api.v1.nodes.Node.find_all",
            side_effect=Exception("connection refused"),
        )
        redis_patch = patch("app.queue.worker_registry.get_redis", return_value=redis)

        redis_patch.start()
        mongo_patch.start()
        try:
            from app.api.v1.nodes import list_nodes

            nodes = await list_nodes()
            assert len(nodes) == 1
            assert nodes[0]["node_id"] == "primary"
            assert nodes[0]["online"] is True
            assert nodes[0]["enrollment"] == "unknown"
        finally:
            redis_patch.stop()
            mongo_patch.stop()
            await redis.aclose()


# ---- persisted version reporting ----


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
async def test_enroll_persists_reported_version(client):
    from app.models.node import Node

    res = await client.post("/api/v1/nodes/enroll", json={
        "node_id": "vnode",
        "hostname": "box",
        "image_digest": "sha256:aaa",
        "version": "0.4.0",
    })
    assert res.status_code == 200

    node = await Node.find_one(Node.node_id == "vnode")
    assert node.image_digest == "sha256:aaa"
    assert node.version == "0.4.0"
    await node.delete()


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
async def test_enroll_without_version_leaves_existing_value(client):
    """A node that cannot read its digest must not erase what it last
    reported (NU-3, NU-5)."""
    from app.models.node import Node

    await Node(node_id="vnode2", image_digest="sha256:old", version="0.3.0").insert()

    res = await client.post("/api/v1/nodes/enroll", json={"node_id": "vnode2"})
    assert res.status_code == 200

    node = await Node.find_one(Node.node_id == "vnode2")
    assert node.image_digest == "sha256:old"
    assert node.version == "0.3.0"
    await node.delete()
