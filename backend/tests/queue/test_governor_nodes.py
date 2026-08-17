"""The per-node breakdown attached to /system/load."""

import json
from unittest.mock import patch

import fakeredis.aioredis
import pytest

from app.queue.governor import current_load


@pytest.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _worker_blob(node_id: str, running: list[str], slots: int = 4) -> str:
    from datetime import UTC, datetime

    return json.dumps(
        {
            "last_seen": datetime.now(UTC).isoformat(),
            "slots": slots,
            "running": running,
            "draining": False,
            "node_id": node_id,
        }
    )


def _patches(fake_redis):
    """Every module that reaches Redis on this path."""
    return (
        patch("app.db.redis_client.get_redis", return_value=fake_redis),
        patch("app.queue.node_stats.get_redis", return_value=fake_redis),
        patch("app.queue.worker_registry.get_redis", return_value=fake_redis),
    )


class TestSystemLoadNodes:
    async def test_nodes_key_present_without_a_governor_snapshot(self, fake_redis):
        # No leader has published. The per-node data must still be there --
        # that is the moment someone is most likely looking at the page.
        p1, p2, p3 = _patches(fake_redis)
        with p1, p2, p3:
            load = await current_load()
        assert load["governor_active"] is False
        assert load["nodes"] == []

    async def test_nodes_key_present_with_a_governor_snapshot(self, fake_redis):
        await fake_redis.set(
            "bp:load:snapshot",
            json.dumps({"state": "OPEN", "governor_active": True}),
        )
        await fake_redis.hset("bp:workers", "host1:1", _worker_blob("gpu", ["j1"]))
        p1, p2, p3 = _patches(fake_redis)
        with p1, p2, p3:
            load = await current_load()
        assert load["governor_active"] is True
        assert [n["node_id"] for n in load["nodes"]] == ["gpu"]

    async def test_node_entry_shape(self, fake_redis):
        await fake_redis.hset(
            "bp:workers", "host1:1", _worker_blob("gpu", ["j1", "j2"], slots=8)
        )
        await fake_redis.zadd("bp:q:ready:gpu", {"j3": 1, "j4": 2, "j5": 3})
        await fake_redis.mset(
            {"bp:conc:cpu:gpu": "6", "bp:conc:mem_mb:gpu": "8192"}
        )
        p1, p2, p3 = _patches(fake_redis)
        with p1, p2, p3:
            load = await current_load()
        assert load["nodes"] == [
            {
                "node_id": "gpu",
                "running": 2,
                "queued": 3,
                "cpu": 6,
                "mem_mb": 8192,
                "workers": 1,
                "known": True,
            }
        ]

    async def test_orphaned_queue_is_flagged_unknown(self, fake_redis):
        await fake_redis.zadd("bp:q:ready:gpu-nodee", {"j1": 1})
        p1, p2, p3 = _patches(fake_redis)
        with p1, p2, p3:
            load = await current_load()
        orphan = next(n for n in load["nodes"] if n["node_id"] == "gpu-nodee")
        assert orphan["known"] is False
        assert orphan["queued"] == 1
        assert orphan["workers"] == 0

    async def test_enumeration_failure_reports_the_error(self, fake_redis):
        # An empty list would read as "no nodes" rather than "the read broke",
        # which is the same reasoning as `queue_error` on /system/stats.
        p1, p2, p3 = _patches(fake_redis)
        with (
            p1,
            p2,
            p3,
            patch(
                "app.api.v1.nodes.enumerate_nodes",
                side_effect=RuntimeError("boom"),
            ),
        ):
            load = await current_load()
        assert load["nodes"] == []
        assert "boom" in load["nodes_error"]

    async def test_orphan_scan_failure_reports_the_error(self, fake_redis):
        # Same contract, but the failure comes from the second of three
        # downstream calls -- confirms the try block covers all of them, not
        # just the first.
        p1, p2, p3 = _patches(fake_redis)
        with (
            p1,
            p2,
            p3,
            patch(
                "app.queue.node_stats.orphaned_queue_nodes",
                side_effect=RuntimeError("scan boom"),
            ),
        ):
            load = await current_load()
        assert load["nodes"] == []
        assert "scan boom" in load["nodes_error"]

    async def test_node_stats_failure_reports_the_error(self, fake_redis):
        # Same contract, but the failure comes from the third downstream
        # call -- confirms the try block covers all of them, not just the
        # ones checked by the other two failure tests.
        p1, p2, p3 = _patches(fake_redis)
        with (
            p1,
            p2,
            p3,
            patch(
                "app.queue.node_stats.node_stats",
                side_effect=RuntimeError("stats boom"),
            ),
        ):
            load = await current_load()
        assert load["nodes"] == []
        assert "stats boom" in load["nodes_error"]
