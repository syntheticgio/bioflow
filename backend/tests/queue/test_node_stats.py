"""Tests for per-node queue and reservation counters."""

from unittest.mock import patch

import fakeredis.aioredis
import pytest

from app.queue.node_stats import node_stats, orphaned_queue_nodes


@pytest.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


class TestNodeStats:
    async def test_absent_keys_read_as_zero(self, fake_redis):
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["ghost"])
        assert stats == {
            "ghost": {"queued": 0, "cpu": 0, "mem_mb": 0, "io_heavy": 0}
        }

    async def test_counts_ready_queue_depth(self, fake_redis):
        await fake_redis.zadd("bp:q:ready:gpu", {"j1": 1, "j2": 2, "j3": 3})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["gpu"])
        assert stats["gpu"]["queued"] == 3

    async def test_reads_reservation_counters(self, fake_redis):
        await fake_redis.mset(
            {
                "bp:conc:cpu:gpu": "8",
                "bp:conc:mem_mb:gpu": "16384",
                "bp:conc:io_heavy:gpu": "1",
            }
        )
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["gpu"])
        assert stats["gpu"]["cpu"] == 8
        assert stats["gpu"]["mem_mb"] == 16384
        assert stats["gpu"]["io_heavy"] == 1

    async def test_negative_counters_clamp_to_zero(self, fake_redis):
        # A counter can go negative if a release double-decrements; the UI
        # should read zero rather than a nonsense negative reservation.
        await fake_redis.mset({"bp:conc:cpu:gpu": "-3"})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["gpu"])
        assert stats["gpu"]["cpu"] == 0

    async def test_global_ready_queue_is_not_a_node(self, fake_redis):
        # bp:q:ready (no node suffix) is the global pool. Asking for stats on
        # a node must never read it.
        await fake_redis.zadd("bp:q:ready", {"global1": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["gpu"])
        assert stats["gpu"]["queued"] == 0

    async def test_multiple_nodes_in_one_call(self, fake_redis):
        await fake_redis.zadd("bp:q:ready:gpu", {"j1": 1})
        await fake_redis.zadd("bp:q:ready:cpu-node", {"j2": 1, "j3": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            stats = await node_stats(["gpu", "cpu-node"])
        assert stats["gpu"]["queued"] == 1
        assert stats["cpu-node"]["queued"] == 2

    async def test_empty_input_makes_no_redis_call(self, fake_redis):
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            assert await node_stats([]) == {}

    async def test_redis_failure_returns_zeroes(self):
        # The endpoints must stay up when Redis is down. This asserts the
        # direction that fails when the error handling breaks -- a happy-path
        # test would pass either way.
        class Boom:
            def pipeline(self):
                raise ConnectionError("redis is down")

        with patch("app.queue.node_stats.get_redis", return_value=Boom()):
            stats = await node_stats(["gpu"])
        assert stats == {
            "gpu": {"queued": 0, "cpu": 0, "mem_mb": 0, "io_heavy": 0}
        }


class TestOrphanedQueueNodes:
    async def test_none_when_every_queue_is_known(self, fake_redis):
        await fake_redis.zadd("bp:q:ready:gpu", {"j1": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            assert await orphaned_queue_nodes({"gpu"}) == []

    async def test_finds_queue_for_unenrolled_node(self, fake_redis):
        # The typo case: someone launched with ?target_node=gpu-nodee and the
        # jobs will sit here forever, drained by nobody.
        await fake_redis.zadd("bp:q:ready:gpu-nodee", {"j1": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            assert await orphaned_queue_nodes({"gpu"}) == ["gpu-nodee"]

    async def test_global_queue_is_never_orphaned(self, fake_redis):
        # bp:q:ready has no node suffix and must not be reported as a node.
        await fake_redis.zadd("bp:q:ready", {"j1": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            assert await orphaned_queue_nodes(set()) == []

    async def test_result_is_sorted(self, fake_redis):
        await fake_redis.zadd("bp:q:ready:zeta", {"j1": 1})
        await fake_redis.zadd("bp:q:ready:alpha", {"j2": 1})
        with patch("app.queue.node_stats.get_redis", return_value=fake_redis):
            assert await orphaned_queue_nodes(set()) == ["alpha", "zeta"]

    async def test_redis_failure_returns_empty(self):
        class Boom:
            def scan_iter(self, *a, **kw):
                raise ConnectionError("redis is down")

        with patch("app.queue.node_stats.get_redis", return_value=Boom()):
            assert await orphaned_queue_nodes(set()) == []
