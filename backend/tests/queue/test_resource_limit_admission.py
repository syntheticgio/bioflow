"""The stored limit reaching admission.

No new enforcement exists for this: `claim.lua` already refuses any candidate
whose declared `mem_mb` exceeds `mem_mb_free`, so the setting is one number
flowing into a gate that was already there.
"""

import pytest
import pytest_asyncio

from app.models.resource_limits import ResourceLimits
from app.queue.worker import Worker

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean():
    await ResourceLimits.find_all().delete()


class TestStoredLimitLowersHeadroom:
    async def test_a_stored_limit_reduces_offered_memory(self, monkeypatch):
        """The user set 2 GB; admission must offer no more than that however
        much RAM the host reports."""
        limits = await ResourceLimits.load()
        limits.max_mem_mb = 2048
        await limits.save()

        worker = Worker(worker_id="test-worker")
        monkeypatch.setattr(
            worker, "_read_reservations", _fake_reservations(cpu=0, mem_mb=0, io=0)
        )

        free = await worker._free_resources()
        assert free["mem_mb"] <= 2048

    async def test_no_stored_limit_leaves_behaviour_unchanged(self, monkeypatch):
        """A fresh install must admit exactly as it did before this feature."""
        limits = await ResourceLimits.load()
        limits.max_mem_mb = None
        await limits.save()

        worker = Worker(worker_id="test-worker")
        monkeypatch.setattr(
            worker, "_read_reservations", _fake_reservations(cpu=0, mem_mb=0, io=0)
        )

        free = await worker._free_resources()
        assert free["mem_mb"] > 0

    async def test_a_reservation_subtracts_from_the_stored_limit(self, monkeypatch):
        """The two halves of this slice composed: with a 4 GB limit and 3 GB
        already reserved, only 1 GB may be offered. Without Task 1's fix this
        would report the full 4 GB and over-admit."""
        limits = await ResourceLimits.load()
        limits.max_mem_mb = 4096
        await limits.save()

        worker = Worker(worker_id="test-worker")
        worker._running = {"job-1": (None, None, 0)}
        monkeypatch.setattr(
            worker, "_read_reservations", _fake_reservations(cpu=0, mem_mb=3072, io=0)
        )

        free = await worker._free_resources()
        assert free["mem_mb"] <= 1024


def _fake_reservations(*, cpu: int, mem_mb: int, io: int):
    async def _read():
        return {"cpu": cpu, "mem_mb": mem_mb, "io_heavy": io}

    return _read
