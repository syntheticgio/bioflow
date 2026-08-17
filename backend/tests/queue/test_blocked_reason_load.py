"""The reason reaches /system/load (#457)."""

import json

from app.queue import blocked_reason, governor


class TestLoadCarriesTheReason:
    async def test_absent_when_nothing_is_blocked(self, redis, monkeypatch):
        monkeypatch.setattr(
            "app.db.redis_client.get_redis", lambda: redis, raising=False
        )
        monkeypatch.setattr(governor, "_node_breakdown", _no_nodes)

        load = await governor.current_load()
        assert load.get("blocked_reason") is None

    async def test_present_when_a_gate_blocked_the_head_job(self, redis, monkeypatch):
        monkeypatch.setattr(
            "app.db.redis_client.get_redis", lambda: redis, raising=False
        )
        monkeypatch.setattr(governor, "_node_breakdown", _no_nodes)
        await redis.set(
            blocked_reason.reason_key(),
            json.dumps({"gate": "mem", "need": 32768, "free": 8192}),
        )

        load = await governor.current_load()
        assert load["blocked_reason"] == {
            "gate": "mem",
            "need": 32768,
            "free": 8192,
            "class": None,
            "admitted": None,
        }


async def _no_nodes():
    return [], None
