"""Typed access to what claim.lua recorded (#457)."""

import json

from app.queue import blocked_reason


class TestReadReason:
    async def test_returns_none_when_nothing_recorded(self, redis):
        assert await blocked_reason.read(redis) is None

    async def test_parses_a_resource_gate(self, redis):
        await redis.set(
            blocked_reason.reason_key("bp:q:ready"),
            json.dumps({"gate": "mem", "need": 32768, "free": 8192}),
        )
        r = await blocked_reason.read(redis)

        assert r.gate == "mem"
        assert r.need == 32768
        assert r.free == 8192
        assert r.job_class is None

    async def test_parses_the_class_gate(self, redis):
        await redis.set(
            blocked_reason.reason_key("bp:q:ready"),
            json.dumps(
                {"gate": "class", "class": "bulk", "admitted": "user_interactive"}
            ),
        )
        r = await blocked_reason.read(redis)

        assert r.gate == "class"
        assert r.job_class == "bulk"
        assert r.admitted == ["user_interactive"]
        assert r.need is None

    async def test_malformed_json_reads_as_no_reason(self, redis):
        """A reason is advisory. Never let it break the activity view."""
        await redis.set(blocked_reason.reason_key("bp:q:ready"), "{not json")
        assert await blocked_reason.read(redis) is None

    async def test_an_unknown_gate_reads_as_no_reason(self, redis):
        await redis.set(
            blocked_reason.reason_key("bp:q:ready"), json.dumps({"gate": "quantum"})
        )
        assert await blocked_reason.read(redis) is None
