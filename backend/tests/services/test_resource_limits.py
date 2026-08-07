"""The stored resource budget.

An admission budget, not an enforced ceiling: it governs what BioFlow plans
to start, never what a running job may use. See
docs/superpowers/specs/2026-08-07-resource-limits-admission-design.md.
"""

import pytest
import pytest_asyncio

from app.models.resource_limits import ResourceLimits

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
