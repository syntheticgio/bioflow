"""The Redis layer behind probe warming.

Uses fakeredis rather than a live server, matching tests/queue/conftest.py.
"""

import asyncio
import os

import fakeredis.aioredis
import pytest
from app.pipelines import tool_cache, tools


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


class TestRoundTrip:
    async def test_a_written_entry_reads_back_identically(self, redis):
        entry = tools.Tool(name="fastp", path="/usr/bin/fastp", version="0.24.0")

        await tool_cache.write(redis, {"fastp": ("fp-1", entry)})
        loaded = await tool_cache.read(redis)

        assert loaded == {"fastp": ("fp-1", entry)}

    async def test_an_error_tool_round_trips(self, redis):
        """The unavailable case carries the message the launch dialog shows,
        so it must survive the round trip too."""
        entry = tools.Tool(
            name="clair3", path=None, version=None, error="not found on PATH"
        )

        await tool_cache.write(redis, {"clair3": ("c3-1", entry)})
        loaded = await tool_cache.read(redis)

        assert loaded["clair3"][1].error == "not found on PATH"
        assert not loaded["clair3"][1].available

    async def test_reading_an_empty_cache_returns_empty(self, redis):
        assert await tool_cache.read(redis) == {}


class TestFailurePosture:
    async def test_corrupt_json_is_ignored_not_raised(self, redis):
        """A cache that can fail the request is worse than no cache."""
        await redis.set(tool_cache.CACHE_KEY, "{not json at all")

        assert await tool_cache.read(redis) == {}

    async def test_a_malformed_entry_is_skipped_but_others_survive(self, redis):
        """One bad record must not discard the other fourteen."""
        await redis.set(
            tool_cache.CACHE_KEY,
            '{"fastp": {"fingerprint": "fp-1", "tool": {"name": "fastp", '
            '"path": "/usr/bin/fastp", "version": "0.24.0", "error": null}}, '
            '"broken": {"fingerprint": "b-1"}}',
        )

        loaded = await tool_cache.read(redis)

        assert "fastp" in loaded
        assert "broken" not in loaded

    async def test_an_unreachable_redis_reads_as_empty(self):
        """Redis being down must degrade to 'probe normally', never to an
        error -- the same rule the governor applies to the mount sentinel."""

        class Broken:
            async def get(self, key):
                raise ConnectionError("redis is down")

        assert await tool_cache.read(Broken()) == {}

    async def test_an_unreachable_redis_does_not_raise_on_write(self):
        class Broken:
            async def set(self, *a, **kw):
                raise ConnectionError("redis is down")

        await tool_cache.write(Broken(), {})  # must not raise


class TestWarm:
    @pytest.fixture(autouse=True)
    def clear_cache(self):
        tools.reset_cache()
        yield
        tools.reset_cache()

    async def test_warm_populates_redis_from_a_cold_start(self, redis):
        await tool_cache.warm(redis)

        stored = await tool_cache.read(redis)
        assert stored, "warm should have written probe results"
        # Every *available* tool should be stored, except the ones
        # NOT_FINGERPRINTABLE excludes on purpose (see TestDeepVariantIsNotCached).
        #
        # Availability is part of the expectation, not incidental to it: `warm`
        # only caches what it can fingerprint, and a tool with no binary has no
        # path to fingerprint. Comparing against every *declared* tool instead
        # quietly asserted that the image ships all of them, so this test broke
        # the moment a tool was declared before its image rebuild -- reporting
        # a cache bug where there was only a missing binary. The regression
        # this is here for is still caught: an installed tool that fails to
        # reach Redis is available and absent from `stored`.
        expected = {
            t.name for t in tools.all_tools() if t.available
        } - tool_cache.NOT_FINGERPRINTABLE
        assert expected <= set(stored)

    async def test_deepvariant_is_not_written_to_the_cache(self, redis):
        """DeepVariant's Tool.path is the docker client's path, not a
        DeepVariant binary -- that path fingerprints successfully, so without
        an explicit exclusion its availability would get cached against the
        docker client's identity and would not change when the image is
        pulled or removed."""
        await tool_cache.warm(redis)

        stored = await tool_cache.read(redis)
        assert "deepvariant" not in stored

    async def test_warm_seeds_probes_so_they_do_not_shell_out(self, redis, tmp_path, monkeypatch):
        """The direction that fails when the seam breaks: seed a version the
        binary does not print, then assert the probe returns it."""
        script = tmp_path / "warmtool"
        script.write_text("#!/bin/sh\necho 'warmtool 1.0.0'\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        resolved = str(script)
        await tool_cache.write(
            redis,
            {
                "warmtool": (
                    tools._fingerprint(resolved),
                    tools.Tool(name="warmtool", path=resolved, version="9.9.9"),
                )
            },
        )

        await tool_cache.warm(redis)

        assert tools._probe("warmtool", "warmtool", ["--version"]).version == "9.9.9"

    async def test_warm_survives_an_unreachable_redis(self):
        """A total Redis failure must leave behaviour exactly as it is today,
        not raise into the startup path."""

        class Broken:
            async def get(self, key):
                raise ConnectionError("redis is down")

            async def set(self, *a, **kw):
                raise ConnectionError("redis is down")

        await tool_cache.warm(Broken())  # must not raise


class TestNotFingerprintable:
    def test_deepvariant_is_excluded(self):
        """The worked case this set exists for: DeepVariant's Tool.path is
        the docker client's, not a DeepVariant binary."""
        assert "deepvariant" in tool_cache.NOT_FINGERPRINTABLE

    def test_derived_from_tool_meta_not_hand_listed(self):
        """Regression guard for the point of this task: the set used to be a
        hardcoded {"deepvariant"}, which meant a second tool moving to
        ON_DEMAND_IMAGE (Clair3, eventually) needed a matching manual edit
        here, silently, with nothing to fail if someone forgot it. Comparing
        directly against what TOOL_META declares makes that edit
        impossible to forget -- there is nothing left to edit."""
        expected = {
            name
            for name, meta in tools.TOOL_META.items()
            if meta.delivery is tools.Delivery.ON_DEMAND_IMAGE
        }
        assert tool_cache.NOT_FINGERPRINTABLE == expected

    def test_a_bundled_tool_is_not_excluded(self):
        assert "fastp" not in tool_cache.NOT_FINGERPRINTABLE
        assert "samtools" not in tool_cache.NOT_FINGERPRINTABLE


class TestInvalidationPublish:
    async def test_publishes_the_tool_name_on_the_invalidate_channel(self, redis):
        pubsub = redis.pubsub()
        await pubsub.subscribe(tool_cache.INVALIDATE_CHANNEL)
        await pubsub.get_message(timeout=1)  # discard the subscribe confirmation

        await tool_cache.publish_invalidation(redis, "deepvariant")

        message = await pubsub.get_message(timeout=1)
        assert message is not None
        assert message["data"] == "deepvariant"
        await pubsub.aclose()

    async def test_survives_an_unreachable_redis(self):
        """Same discipline as every other function here: a missed
        invalidation means a stale badge until restart, not a failed
        install -- the install already succeeded by the time this runs."""

        class Broken:
            async def publish(self, *a, **kw):
                raise ConnectionError("redis is down")

        await tool_cache.publish_invalidation(Broken(), "deepvariant")  # must not raise


class TestInvalidationListen:
    async def test_a_published_message_clears_the_probe_cache(self, redis, monkeypatch):
        """The regression this task exists to fix: without a subscriber, a
        process that did not perform the install keeps serving whatever its
        lru_cache already decided, until it happens to restart."""
        cleared = asyncio.Event()
        monkeypatch.setattr(tools, "reset_cache", cleared.set)

        listener = asyncio.create_task(tool_cache.listen_for_invalidations(redis))
        try:
            # Give the subscriber time to actually subscribe before
            # publishing, or the message has nowhere to arrive.
            await asyncio.sleep(0.1)
            await tool_cache.publish_invalidation(redis, "deepvariant")
            await asyncio.wait_for(cleared.wait(), timeout=5)
        finally:
            listener.cancel()
            with pytest.raises(asyncio.CancelledError):
                await listener

    async def test_cancellation_stops_the_loop(self, redis):
        """A subscriber that swallowed CancelledError would keep a Redis
        connection open past the process's own shutdown -- the same leak the
        warm task's cancellation exists to avoid."""
        listener = asyncio.create_task(tool_cache.listen_for_invalidations(redis))
        await asyncio.sleep(0.05)

        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(listener, timeout=5)

    async def test_a_broken_subscribe_is_retried_not_fatal(self, monkeypatch):
        """The direction that fails when the seam breaks: a subscriber that
        gives up on the first Redis error would silently stop watching for
        the rest of the process's life. Patch the retry sleep to near-zero so
        the test does not spend five real seconds proving the loop comes back
        around."""
        real_sleep = asyncio.sleep
        # `tool_cache.asyncio` *is* the `asyncio` module (a shared reference,
        # not a copy), so patching its `sleep` attribute and then calling
        # `asyncio.sleep` from inside the replacement calls the replacement
        # again -- infinite recursion. Capturing the real function above,
        # before patching, is what breaks that cycle.
        monkeypatch.setattr(tool_cache.asyncio, "sleep", lambda _: real_sleep(0))

        attempts = 0
        succeeded = asyncio.Event()

        class FlakyPubSub:
            async def subscribe(self, *a, **kw):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ConnectionError("redis is down")
                succeeded.set()

            def listen(self):
                async def _gen():
                    await asyncio.sleep(30)
                    yield {}  # pragma: no cover - never reached in this test

                return _gen()

            async def unsubscribe(self, *a, **kw):
                return None

            async def aclose(self):
                return None

        class FlakyClient:
            def pubsub(self):
                return FlakyPubSub()

        listener = asyncio.create_task(tool_cache.listen_for_invalidations(FlakyClient()))
        try:
            await asyncio.wait_for(succeeded.wait(), timeout=5)
        finally:
            listener.cancel()
            with pytest.raises(asyncio.CancelledError):
                await listener

        assert attempts >= 2
