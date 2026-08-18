"""`Worker._resource_budgets` actually honours the kernel hard ceiling.

`test_resource_limit_service.py::test_hard_mem_mb_lowers_the_admission_budget`
pins the pure helper's contract, but nothing there calls the worker's own
method -- a regression that silently dropped the `hard_mem_mb=...` argument
from `worker.py`'s call to `admission_budget_mb` would leave every test in
that file green. These tests call `Worker._resource_budgets()` itself, with
a real `ResourceLimits` document backed by the test Mongo (matching the
pattern in `test_resource_limit_admission.py`), and patch
`resource_limit_service.hard_mem_mb` -- the seam `worker.py` actually reads
-- to prove the returned `mem_mb` is capped by it.
"""

import pytest
import pytest_asyncio

from app.models.resource_limits import ResourceLimits
from app.queue.worker import Worker
from app.services import resource_limit_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean():
    await ResourceLimits.find_all().delete()


class TestResourceBudgetsRespectsHardMemMb:
    async def test_hard_mem_mb_caps_the_returned_budget(self, monkeypatch):
        """A kernel ceiling far below both the stored limit and the machine's
        own memory must still cap what the worker offers to claim.lua.

        Without `hard_mem_mb=resource_limit_service.hard_mem_mb()` wired
        through in `worker.py`'s call to `admission_budget_mb`, this would
        return a budget based on the much larger stored/machine figures and
        fail the assertion below.
        """
        limits = await ResourceLimits.load()
        limits.max_mem_mb = None
        await limits.save()

        # Force the hard ceiling to a tiny, unmistakable value and make the
        # live `available_mb` reading irrelevant by keeping it large -- the
        # cap has to come from budget_mb (the ceiling), not the live clamp.
        monkeypatch.setattr(resource_limit_service, "hard_mem_mb", lambda: 500)
        monkeypatch.setattr(
            "app.queue.worker.psutil.virtual_memory",
            lambda: _FakeVirtualMemory(total=64_000 * 1024 * 1024, available=64_000 * 1024 * 1024),
        )

        worker = Worker(worker_id="test-worker")
        budgets = await worker._resource_budgets()

        # admission_budget_mb applies MEM_HEADROOM_FRACTION (0.7) to the
        # ceiling, so 500 MB hard limit -> 350 MB, well under the live
        # available reading of 64000 MB.
        assert budgets["mem_mb"] <= 350

    async def test_no_hard_mem_mb_leaves_the_larger_ceiling_in_effect(self, monkeypatch):
        """The control case: with no hard limit configured, the budget comes
        from the machine's own memory (headroom-adjusted), not a tiny cap."""
        limits = await ResourceLimits.load()
        limits.max_mem_mb = None
        await limits.save()

        monkeypatch.setattr(resource_limit_service, "hard_mem_mb", lambda: None)
        monkeypatch.setattr(
            "app.queue.worker.psutil.virtual_memory",
            lambda: _FakeVirtualMemory(total=64_000 * 1024 * 1024, available=64_000 * 1024 * 1024),
        )

        worker = Worker(worker_id="test-worker")
        budgets = await worker._resource_budgets()

        # 64000 MB machine * 0.7 headroom = 44800 MB ceiling; the live
        # available clamp (also 64000 MB) doesn't bind here.
        assert budgets["mem_mb"] > 350
        assert budgets["mem_mb"] <= 44800


class _FakeVirtualMemory:
    def __init__(self, *, total: int, available: int):
        self.total = total
        self.available = available
