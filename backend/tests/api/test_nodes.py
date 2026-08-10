"""Tests for the nodes router endpoint."""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import fakeredis.aioredis
import pytest

from app.api.v1.nodes import list_nodes


@pytest.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _worker_blob(
    node_id: str = "primary",
    slots: int = 4,
    running: list | None = None,
    online: bool = True,
) -> dict:
    now = datetime.now(UTC)
    last_seen = now if online else now.replace(year=2000)
    return {
        "last_seen": last_seen.isoformat(),
        "slots": slots,
        "running": running or [],
        "draining": False,
        "node_id": node_id,
    }


async def _seed_workers(redis, **workers):
    for wid, blob in workers.items():
        await redis.hset("bp:workers", wid, json.dumps(blob))
    # Seed concurrency counters for all expected node ids so _node_conc
    # doesn't fail on the MGET.
    await redis.mset(
        {
            "bp:conc:cpu:primary": "0",
            "bp:conc:mem_mb:primary": "0",
            "bp:conc:io_heavy:primary": "0",
        }
    )


class TestListNodes:
    async def test_empty_when_no_workers(self, fake_redis):
        with patch("app.api.v1.nodes.get_redis", return_value=fake_redis):
            nodes = await list_nodes()
        assert nodes == []

    async def test_single_online_worker(self, fake_redis):
        await _seed_workers(
            fake_redis,
            **{"host1:1234": _worker_blob("primary", slots=4, running=["j1", "j2"])},
        )
        with patch("app.api.v1.nodes.get_redis", return_value=fake_redis):
            nodes = await list_nodes()
        assert len(nodes) == 1
        assert nodes[0]["node_id"] == "primary"
        assert nodes[0]["online"] is True
        assert nodes[0]["online_workers"] == 1
        assert nodes[0]["workers"] == 1
        assert nodes[0]["running_jobs"] == 2
        assert nodes[0]["slots"] == 4

    async def test_offline_worker(self, fake_redis):
        await _seed_workers(
            fake_redis,
            **{"dead:host": _worker_blob("primary", online=False)},
        )
        with patch("app.api.v1.nodes.get_redis", return_value=fake_redis):
            nodes = await list_nodes()
        assert nodes[0]["online"] is False
        assert nodes[0]["online_workers"] == 0

    async def test_multiple_workers_same_node(self, fake_redis):
        await _seed_workers(
            fake_redis,
            **{
                "host1:100": _worker_blob("gpu-node", slots=2, running=["a"]),
                "host2:200": _worker_blob("gpu-node", slots=2, running=["b"]),
            },
        )
        with patch("app.api.v1.nodes.get_redis", return_value=fake_redis):
            nodes = await list_nodes()
        assert len(nodes) == 1
        assert nodes[0]["node_id"] == "gpu-node"
        assert nodes[0]["workers"] == 2
        assert nodes[0]["online_workers"] == 2
        assert nodes[0]["running_jobs"] == 2
        assert nodes[0]["slots"] == 4

    async def test_multiple_nodes(self, fake_redis):
        await _seed_workers(
            fake_redis,
            **{
                "a:1": _worker_blob("primary"),
                "b:1": _worker_blob("child-node"),
            },
        )
        with patch("app.api.v1.nodes.get_redis", return_value=fake_redis):
            nodes = await list_nodes()
        assert len(nodes) == 2
        node_ids = {n["node_id"] for n in nodes}
        assert node_ids == {"primary", "child-node"}

    async def test_unknown_node_id_for_missing_field(self, fake_redis):
        blob = {
            "last_seen": datetime.now(UTC).isoformat(),
            "slots": 1,
            "running": [],
            "draining": False,
        }
        await _seed_workers(fake_redis, **{"old:worker": blob})
        with patch("app.api.v1.nodes.get_redis", return_value=fake_redis):
            nodes = await list_nodes()
        assert len(nodes) == 1
        assert nodes[0]["node_id"] == "unknown"
