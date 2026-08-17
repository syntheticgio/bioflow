"""#74: two concurrent claims against headroom that fits only one job.

`claim.lua` used to compare a candidate's declared mem_mb against a `mem_free`
value the caller (Worker._free_resources) computed from a Redis read one round
trip before the script ran. Redis serializes the two script executions, but
the second execution still used the *first worker's* stale free-capacity
argument -- not a number reflecting the first worker's just-completed
INCRBY. Two 6 GB jobs against 8 GB of budget could both be admitted.

The fix moves the live counter read inside the script itself (see claim.lua
and queue.claim's cpu_budget/mem_mb_budget/io_heavy_budget args), so this test
exercises the actual race through queue.claim -- not compute_free_resources,
which is pure Python and was never where the bug lived -- via asyncio.gather
on two concurrent calls against one shared fakeredis instance.
"""

import asyncio

from tests.queue.conftest import ALL_CLASSES


class TestConcurrentClaimsRespectSharedHeadroom:
    async def test_two_six_gb_jobs_against_eight_gb_only_admits_one(
        self, redis, scripts, job_factory, monkeypatch
    ):
        from app.queue import queue

        monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

        await job_factory("job-a", mem_mb=6144, score=100)
        await job_factory("job-b", mem_mb=6144, score=200)

        results = await asyncio.gather(
            queue.claim(
                "worker-a",
                allowed_classes=ALL_CLASSES.split(","),
                cpu_budget=8,
                mem_mb_budget=8192,
                io_heavy_budget=2,
            ),
            queue.claim(
                "worker-b",
                allowed_classes=ALL_CLASSES.split(","),
                cpu_budget=8,
                mem_mb_budget=8192,
                io_heavy_budget=2,
            ),
        )

        admitted = [r for r in results if r is not None]
        assert len(admitted) == 1, (
            f"8 GB of budget cannot fit two 6 GB jobs, but got {len(admitted)} admissions"
        )
        assert int(await redis.get("bp:conc:mem_mb")) == 6144

    async def test_twenty_concurrent_claims_never_exceed_the_shared_budget(
        self, redis, scripts, job_factory, monkeypatch
    ):
        """A wider version of the same race: many workers, several 3 GB jobs,
        an 8 GB shared budget -- at most two may ever be admitted at once."""
        from app.queue import queue

        monkeypatch.setattr(queue, "get_script", lambda name: scripts[name])

        for i in range(10):
            await job_factory(f"job{i}", mem_mb=3072, score=1000 + i)

        results = await asyncio.gather(
            *(
                queue.claim(
                    f"worker-{i}",
                    allowed_classes=ALL_CLASSES.split(","),
                    cpu_budget=8,
                    mem_mb_budget=8192,
                    io_heavy_budget=2,
                )
                for i in range(20)
            )
        )

        admitted = [r for r in results if r is not None]
        assert len(admitted) <= 2, (
            f"8 GB of budget cannot fit three 3 GB jobs, but got {len(admitted)} admissions"
        )
        assert int(await redis.get("bp:conc:mem_mb")) == len(admitted) * 3072
