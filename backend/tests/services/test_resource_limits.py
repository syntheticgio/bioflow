"""The stored resource budget.

An admission budget, not an enforced ceiling: it governs what BioFlow plans
to start, never what a running job may use. See
docs/superpowers/specs/2026-08-07-resource-limits-admission-design.md.
"""

import pytest
import pytest_asyncio
from app.models.resource_limits import ResourceLimits
from app.services import resource_limit_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean():
    await ResourceLimits.find_all().delete()


class TestLoad:
    async def test_load_creates_the_document_on_first_read(self):
        """Upsert-on-read, matching AiRouting: there is exactly one, and a
        missing one is indistinguishable from a fresh install."""
        loaded = await ResourceLimits.load()
        assert loaded.id == ResourceLimits.SINGLETON_ID

    async def test_a_fresh_install_sets_no_limits(self):
        """None means "use the machine's own budget", which is a real state
        rather than a null needing cleanup -- the same reasoning AiRouting
        uses for an absent slot."""
        loaded = await ResourceLimits.load()
        assert loaded.max_mem_mb is None
        assert loaded.max_cpu is None
        assert loaded.max_threads is None

    async def test_load_returns_the_stored_document_once_saved(self):
        first = await ResourceLimits.load()
        first.max_mem_mb = 16384
        await first.save()

        second = await ResourceLimits.load()
        assert second.max_mem_mb == 16384

    async def test_load_is_idempotent(self):
        """Two loads must not create two documents."""
        await ResourceLimits.load()
        await ResourceLimits.load()
        assert await ResourceLimits.count() == 1


class TestResolveMemBudget:
    """A stored limit resolved against what the machine actually has.

    Pure, so the clamping rules are testable without a worker or a host probe.
    These are plain sync tests; the module's asyncio pytestmark (needed by
    TestLoad above) applies to them too and pytest-asyncio warns about it on
    every run. Harmless -- verified the bodies still execute for real, not
    silently skipped -- and not worth a file split for a cosmetic warning.
    """

    def test_no_stored_limit_uses_the_machine_budget(self):
        assert resource_limit_service.resolve_mem_budget_mb(
            stored_mb=None, machine_mb=16384
        ) == 16384

    def test_a_stored_limit_below_the_machine_budget_wins(self):
        """The whole point: the user asked for less than the host has."""
        assert resource_limit_service.resolve_mem_budget_mb(
            stored_mb=8192, machine_mb=16384
        ) == 8192

    def test_a_stored_limit_above_the_machine_budget_is_clamped(self):
        """Typing 64 GB on a 16 GB machine cannot conjure headroom. The limit
        is a budget to stay under, not a claim about the hardware."""
        assert resource_limit_service.resolve_mem_budget_mb(
            stored_mb=65536, machine_mb=16384
        ) == 16384

    def test_a_zero_or_negative_stored_limit_is_ignored(self):
        """Zero would admit nothing at all and stall the queue silently.
        Treated as 'no opinion' rather than as a real ceiling."""
        assert resource_limit_service.resolve_mem_budget_mb(
            stored_mb=0, machine_mb=16384
        ) == 16384
        assert resource_limit_service.resolve_mem_budget_mb(
            stored_mb=-5, machine_mb=16384
        ) == 16384
