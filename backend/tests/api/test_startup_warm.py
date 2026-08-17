"""Startup must not wait on tool probing.

The probe costs ~15s cold. Moving it off the request path is the entire point
of the feature; moving it *into* startup instead would be a regression, so
these tests pin the fire-and-forget shape.
"""

import asyncio

import pytest
from app import main


async def _noop(*args, **kwargs):
    return None


@pytest.fixture
def stub_startup(monkeypatch):
    """Neutralise everything in lifespan except the warm task."""
    monkeypatch.setattr(main, "initialize_home", lambda: None)
    monkeypatch.setattr(main, "connect_to_mongo", _noop)
    monkeypatch.setattr(main, "connect_to_redis", _noop)
    monkeypatch.setattr(main, "close_mongo", _noop)
    monkeypatch.setattr(main, "close_redis", _noop)
    monkeypatch.setattr(main, "load_handlers", lambda: None)
    monkeypatch.setattr(main, "get_redis", lambda: object())
    # Neutralised the same way as warm: a real subscriber would try to call
    # .pubsub() on the stub object above and spend this fixture's tests
    # retrying a doomed connection every 5s in the background. Individual
    # tests below replace this with their own stub where the invalidation
    # task itself is what is under test.
    monkeypatch.setattr(main.tool_cache, "listen_for_invalidations", _noop)


class TestStartupWarm:
    async def test_lifespan_does_not_wait_for_the_warm(self, monkeypatch, stub_startup):
        """A slow warm must not delay the app becoming ready."""
        started = asyncio.Event()

        async def slow_warm(client):
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(main.tool_cache, "warm", slow_warm)

        async with main.lifespan(None):
            # If lifespan awaited the warm, this line is unreachable for 30s.
            await asyncio.wait_for(started.wait(), timeout=5)

    async def test_a_failing_warm_does_not_break_startup(self, monkeypatch, stub_startup):
        """A probe failure must be logged, not propagated -- the app still
        serves, tools just report lazily as they do today."""
        failed = asyncio.Event()

        async def broken_warm(client):
            failed.set()
            raise RuntimeError("probing exploded")

        monkeypatch.setattr(main.tool_cache, "warm", broken_warm)

        async with main.lifespan(None):
            await asyncio.wait_for(failed.wait(), timeout=5)
            # Let the task run its exception handler.
            await asyncio.sleep(0.1)


class TestStartupInvalidationSubscriber:
    """The cross-process cache-invalidation listener (task 3) is started and
    torn down the same fire-and-forget way the warm task is -- these pin that
    shape rather than re-testing `listen_for_invalidations` itself, which has
    its own tests in test_tool_cache.py."""

    async def test_lifespan_starts_the_invalidation_listener(self, monkeypatch, stub_startup):
        """Startup must not wait for the listener either -- it runs forever
        by design, so awaiting it would mean the app never finishes starting."""
        started = asyncio.Event()

        async def fake_listener(client):
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(main.tool_cache, "listen_for_invalidations", fake_listener)

        async with main.lifespan(None):
            await asyncio.wait_for(started.wait(), timeout=5)

    async def test_shutdown_cancels_the_invalidation_listener(self, monkeypatch, stub_startup):
        """A subscriber left running past its process's lifespan would go on
        holding a Redis connection to a shutdown app -- the same leak
        `warm_task.cancel()` exists to avoid for the probe task."""
        cancelled = asyncio.Event()

        async def fake_listener(client):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr(main.tool_cache, "listen_for_invalidations", fake_listener)

        async with main.lifespan(None):
            await asyncio.sleep(0.05)  # let the task actually start

        await asyncio.wait_for(cancelled.wait(), timeout=5)
