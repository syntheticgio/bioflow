"""Tests for the nodes router endpoint."""

import json
from datetime import UTC, datetime, timedelta
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
    # Offline means "past the 60s heartbeat threshold", not "abandoned". A
    # last_seen far enough back to be reaped as a dead worker's leftovers
    # (see worker_registry) would test something else entirely.
    last_seen = now if online else now - timedelta(minutes=5)
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
        with patch("app.queue.worker_registry.get_redis", return_value=fake_redis):
            nodes = await list_nodes()
        assert nodes == []

    async def test_single_online_worker(self, fake_redis):
        await _seed_workers(
            fake_redis,
            **{"host1:1234": _worker_blob("primary", slots=4, running=["j1", "j2"])},
        )
        with patch("app.queue.worker_registry.get_redis", return_value=fake_redis):
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
        with patch("app.queue.worker_registry.get_redis", return_value=fake_redis):
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
        with patch("app.queue.worker_registry.get_redis", return_value=fake_redis):
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
        with patch("app.queue.worker_registry.get_redis", return_value=fake_redis):
            nodes = await list_nodes()
        assert len(nodes) == 2
        node_ids = {n["node_id"] for n in nodes}
        assert node_ids == {"primary", "child-node"}

    async def test_worker_without_node_id_is_not_a_node(self, fake_redis):
        """A payload with no node_id is malformed, not a node called "unknown".

        Before #451 this synthesized a phantom "unknown" row in the settings
        table, which also collided with the real "unknown" enrollment status.
        """
        blob = {
            "last_seen": datetime.now(UTC).isoformat(),
            "slots": 1,
            "running": [],
            "draining": False,
        }
        await _seed_workers(fake_redis, **{"old:worker": blob})
        with patch("app.queue.worker_registry.get_redis", return_value=fake_redis):
            nodes = await list_nodes()
        assert nodes == []

    async def test_stale_worker_is_reaped_not_listed(self, fake_redis):
        """The hash keeps dead workers' heartbeats; a read drops and deletes them.

        This is the #451 root cause: nothing removes an entry when a worker
        dies without a graceful shutdown, so the node table counted week-old
        corpses as workers.
        """
        long_dead = _worker_blob("primary")
        long_dead["last_seen"] = (datetime.now(UTC) - timedelta(days=11)).isoformat()
        await _seed_workers(
            fake_redis,
            **{"live:1": _worker_blob("primary"), "dead:1": long_dead},
        )
        with patch("app.queue.worker_registry.get_redis", return_value=fake_redis):
            nodes = await list_nodes()
        assert len(nodes) == 1
        assert nodes[0]["workers"] == 1
        assert await fake_redis.hkeys("bp:workers") == ["live:1"]

    async def test_recently_offline_worker_survives_the_reap(self, fake_redis):
        """Offline is not dead. A node down for an hour still belongs in the table."""
        recent = _worker_blob("primary")
        recent["last_seen"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await _seed_workers(fake_redis, **{"napping:1": recent})
        with patch("app.queue.worker_registry.get_redis", return_value=fake_redis):
            nodes = await list_nodes()
        assert len(nodes) == 1
        assert nodes[0]["workers"] == 1
        assert nodes[0]["online"] is False
        assert await fake_redis.hkeys("bp:workers") == ["napping:1"]


class TestNodeQueueStats:
    async def test_queued_jobs_reports_ready_queue_depth(self, fake_redis):
        await _seed_workers(
            fake_redis,
            **{"host1:1234": _worker_blob("primary", slots=4)},
        )
        await fake_redis.zadd("bp:q:ready:primary", {"j1": 1, "j2": 2})
        with (
            patch("app.queue.worker_registry.get_redis", return_value=fake_redis),
            patch("app.queue.node_stats.get_redis", return_value=fake_redis),
        ):
            nodes = await list_nodes()
        assert nodes[0]["queued_jobs"] == 2

    async def test_queued_jobs_is_zero_with_no_queue(self, fake_redis):
        await _seed_workers(
            fake_redis,
            **{"host1:1234": _worker_blob("primary")},
        )
        with (
            patch("app.queue.worker_registry.get_redis", return_value=fake_redis),
            patch("app.queue.node_stats.get_redis", return_value=fake_redis),
        ):
            nodes = await list_nodes()
        assert nodes[0]["queued_jobs"] == 0

    async def test_offline_node_still_reports_reservations(self, fake_redis):
        # Workers died mid-job and the counters have not been reaped. That is
        # a real condition and hiding it behind zeros makes it undiagnosable.
        await _seed_workers(
            fake_redis,
            **{"dead:host": _worker_blob("primary", online=False)},
        )
        await fake_redis.mset({"bp:conc:cpu:primary": "4"})
        with (
            patch("app.queue.worker_registry.get_redis", return_value=fake_redis),
            patch("app.queue.node_stats.get_redis", return_value=fake_redis),
        ):
            nodes = await list_nodes()
        assert nodes[0]["online"] is False
        assert nodes[0]["reserved"]["cpu"] == 4

    async def test_orphaned_queue_appears_as_unknown_node(self, fake_redis):
        await _seed_workers(
            fake_redis,
            **{"host1:1234": _worker_blob("primary")},
        )
        await fake_redis.zadd("bp:q:ready:gpu-nodee", {"j1": 1})
        with (
            patch("app.queue.worker_registry.get_redis", return_value=fake_redis),
            patch("app.queue.node_stats.get_redis", return_value=fake_redis),
        ):
            nodes = await list_nodes()
        orphan = next(n for n in nodes if n["node_id"] == "gpu-nodee")
        assert orphan["queued_jobs"] == 1
        assert orphan["online"] is False
        assert orphan["workers"] == 0
        assert orphan["enrollment"] == "unknown"


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
async def test_node_stores_ssh_and_version_fields():
    from app.models.node import Node

    node = Node(
        node_id="n1",
        ssh_host="10.0.0.5",
        ssh_port=2222,
        ssh_username="ops",
        ssh_key_enc=b"ciphertext",
        image_digest="sha256:abc",
        version="0.4.0",
    )
    await node.insert()

    loaded = await Node.find_one(Node.node_id == "n1")
    assert loaded.ssh_host == "10.0.0.5"
    assert loaded.ssh_port == 2222
    assert loaded.ssh_username == "ops"
    assert loaded.ssh_key_enc == b"ciphertext"
    assert loaded.image_digest == "sha256:abc"
    assert loaded.version == "0.4.0"
    await loaded.delete()


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
async def test_node_ssh_fields_default_to_none():
    """A hand-provisioned node has no stored key -- that is what makes it
    non-updatable, so the null must survive a round trip."""
    from app.models.node import Node

    node = Node(node_id="n2")
    await node.insert()

    loaded = await Node.find_one(Node.node_id == "n2")
    assert loaded.ssh_key_enc is None
    assert loaded.ssh_host is None
    assert loaded.ssh_port == 22
    assert loaded.image_digest is None
    await loaded.delete()


async def test_current_version_reports_primary_digest(client):
    with patch("app.api.v1.nodes._own_image_digest", return_value="sha256:cur"):
        res = await client.get("/api/v1/nodes/current-version")
    assert res.status_code == 200
    body = res.json()
    assert body["image_digest"] == "sha256:cur"
    assert body["version"]


async def test_current_version_tolerates_unknown_digest(client):
    with patch("app.api.v1.nodes._own_image_digest", return_value=None):
        res = await client.get("/api/v1/nodes/current-version")
    assert res.status_code == 200
    assert res.json()["image_digest"] is None
