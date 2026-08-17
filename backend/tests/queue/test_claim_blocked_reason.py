"""What claim.lua says about the job it could not start (#457).

The script already evaluates every gate; before this it discarded which one
failed. These tests pin the attribution, the fixed gate order, and the
guarantee that recording never changes what gets claimed.
"""

import json

import pytest

from tests.queue.conftest import ALL_CLASSES
from tests.queue.test_claim import LEASE_MS, NOW_MS, claim

REASON_KEY = "bp:why:bp:q:ready"


async def reason(redis):
    raw = await redis.get(REASON_KEY)
    return json.loads(raw) if raw else None


class TestGateAttribution:
    async def test_memory_gate_records_need_and_free(self, redis, scripts, job_factory):
        await job_factory("job1", mem_mb=32768)
        assert await claim(scripts, mem=8192) is None

        r = await reason(redis)
        assert r["gate"] == "mem"
        assert r["need"] == 32768
        assert r["free"] == 8192

    async def test_cpu_gate_records_need_and_free(self, redis, scripts, job_factory):
        await job_factory("job1", cpu=16)
        assert await claim(scripts, cpu=4) is None

        r = await reason(redis)
        assert r["gate"] == "cpu"
        assert r["need"] == 16
        assert r["free"] == 4

    async def test_io_gate_records_the_heavy_slot(self, redis, scripts, job_factory):
        await job_factory("job1", io="heavy")
        assert await claim(scripts, io=0) is None

        r = await reason(redis)
        assert r["gate"] == "io"

    async def test_class_gate_records_class_and_admitted(self, redis, scripts, job_factory):
        await job_factory("job1", job_class="bulk")
        assert await claim(scripts, classes="user_interactive") is None

        r = await reason(redis)
        assert r["gate"] == "class"
        assert r["class"] == "bulk"
        assert r["admitted"] == "user_interactive"

    async def test_free_is_headroom_after_reservations(self, redis, scripts, job_factory):
        """The recorded free must be what the gate compared against, not the
        raw budget: a half-reserved machine and an idle one give different
        answers to 'why is this waiting'."""
        await redis.set("bp:conc:mem_mb", 6144)
        await job_factory("job1", mem_mb=32768)
        assert await claim(scripts, mem=8192) is None

        r = await reason(redis)
        assert r["free"] == 2048


class TestGateOrder:
    async def test_class_wins_over_every_resource_gate(self, redis, scripts, job_factory):
        """All four gates fail at once. The fixed order makes the sentence
        deterministic; class first because governor closure explains every
        queued job at once rather than anything about this one."""
        await job_factory("job1", job_class="bulk", cpu=16, mem_mb=32768, io="heavy")
        assert await claim(scripts, classes="user_interactive", cpu=1, mem=128, io=0) is None

        assert (await reason(redis))["gate"] == "class"

    async def test_cpu_wins_over_mem_and_io(self, redis, scripts, job_factory):
        await job_factory("job1", cpu=16, mem_mb=32768, io="heavy")
        assert await claim(scripts, cpu=1, mem=128, io=0) is None

        assert (await reason(redis))["gate"] == "cpu"

    async def test_mem_wins_over_io(self, redis, scripts, job_factory):
        await job_factory("job1", mem_mb=32768, io="heavy")
        assert await claim(scripts, mem=128, io=0) is None

        assert (await reason(redis))["gate"] == "mem"


class TestRecordingIsInert:
    async def test_a_successful_claim_records_nothing(self, redis, scripts, job_factory):
        await job_factory("job1")
        assert (await claim(scripts))[0] == "job1"
        assert await reason(redis) is None

    async def test_recording_does_not_change_which_job_is_claimed(
        self, redis, scripts, job_factory
    ):
        """job1 sorts first and does not fit; job2 does. The scan must still
        reach job2 -- recording a reason must not short-circuit selection."""
        await job_factory("job1", mem_mb=32768, score=1)
        await job_factory("job2", mem_mb=128, score=2)

        result = await claim(scripts, mem=8192)
        assert result[0] == "job2"

    async def test_only_the_head_of_queue_is_described(self, redis, scripts, job_factory):
        """Two jobs, neither fits, blocked on different gates. The reason
        describes the one actually next in line."""
        await job_factory("job1", mem_mb=32768, score=1)
        await job_factory("job2", cpu=16, score=2)

        assert await claim(scripts, cpu=8, mem=8192) is None
        assert (await reason(redis))["gate"] == "mem"

    async def test_an_empty_queue_records_nothing(self, redis, scripts):
        assert await claim(scripts) is None
        assert await reason(redis) is None

    async def test_the_reason_expires(self, redis, scripts, job_factory):
        await job_factory("job1", mem_mb=32768)
        await claim(scripts, mem=8192)
        assert 0 < await redis.ttl(REASON_KEY) <= 15
