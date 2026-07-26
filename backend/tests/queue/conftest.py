"""Queue test fixtures.

Tests run the real Lua scripts against fakeredis, which executes them through an
embedded Lua interpreter. That matters: the atomicity guarantees under test live
*inside* the scripts, so mocking them away would test nothing.
"""

from pathlib import Path

import fakeredis.aioredis
import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "app" / "queue" / "scripts"


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def scripts(redis):
    """Register every Lua script, mirroring db/redis_client._load_scripts."""
    return {p.stem: redis.register_script(p.read_text()) for p in SCRIPT_DIR.glob("*.lua")}


@pytest.fixture
def job_factory(redis):
    """Insert a job into the ready queue exactly as queue._push_to_redis does."""

    async def _make(
        job_id: str,
        *,
        job_class: str = "user_background",
        score: float | None = None,
        cpu: int = 1,
        mem_mb: int = 128,
        io: str = "none",
        attempts: int = 0,
        epoch: int = 0,
        job_type: str = "noop",
    ) -> str:
        from app.models import JobClass
        from app.queue.priority import BASE_SCORES

        if score is None:
            score = BASE_SCORES[JobClass(job_class)]
        await redis.hset(
            f"bp:job:{job_id}",
            mapping={
                "type": job_type,
                "class": job_class,
                "cpu": cpu,
                "mem_mb": mem_mb,
                "io": io,
                "attempts": attempts,
                "score": score,
                "epoch": epoch,
            },
        )
        await redis.zadd("bp:q:ready", {job_id: score})
        return job_id

    return _make


ALL_CLASSES = "user_interactive,user_background,maintenance,bulk"
