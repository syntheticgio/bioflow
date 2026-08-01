"""The Redis layer behind probe warming.

Uses fakeredis rather than a live server, matching tests/queue/conftest.py.
"""

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
