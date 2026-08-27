"""Tests for the shared-storage round-trip probe.

The cases that matter most here are the two the probe exists to separate: a
node that answers "no such file" (a real `False`) and a probe that could not
run at all (`StorageProbeError`). Recording the second as the first would
exclude a working node from filesystem-dependent work permanently, so several
tests below exist only to pin that distinction.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import node_storage_probe
from app.services.node_storage_probe import (
    ProbeResult,
    StorageProbeError,
    probe_shared_storage,
)

STORAGE = "/mnt/data"


def _conn(*, stdout="", stderr="", exit_status=0):
    """A stub SSH connection whose `run` returns one canned result."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.exit_status = exit_status
    conn = MagicMock()
    conn.run = AsyncMock(return_value=result)
    return conn


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Give the probe a real writable `BIOINFO_HOME` to write its sentinel in.

    `meta_dir` is a computed property, so it is set by moving `bioinfo_home`
    underneath it -- the pattern the pipeline tests already use.
    """
    monkeypatch.setattr(node_storage_probe.settings, "bioinfo_home", tmp_path)
    meta = tmp_path / ".biopipe"
    meta.mkdir(parents=True)
    return meta


def _token_from(conn) -> str:
    """Recover the nonce the probe generated, from the command it ran."""
    command = conn.run.await_args.args[0]
    return command.split(node_storage_probe.SENTINEL_PREFIX)[1].rstrip("'")


class TestRoundTrip:
    """R2/R3/R4 -- what the probe concludes from what the node returns."""

    @pytest.mark.asyncio
    async def test_node_returning_the_token_is_shared(self, home):
        """R3. The node read the file this probe just wrote."""
        conn = _conn()

        async def run(command, **kwargs):
            # Echo back the real sentinel: this is what a shared mount does.
            token = command.split(node_storage_probe.SENTINEL_PREFIX)[1].rstrip("'")
            result = MagicMock()
            result.stdout = node_storage_probe._sentinel_body(token)
            result.stderr = ""
            result.exit_status = 0
            return result

        conn.run = AsyncMock(side_effect=run)

        result = await probe_shared_storage(conn, STORAGE)

        assert isinstance(result, ProbeResult)
        assert result.shared is True

    @pytest.mark.asyncio
    async def test_missing_file_is_not_shared(self, home):
        """R4. Exit 1, empty stdout, message on stderr -- a real node's shape.

        Verified against real hardware 2026-08-27; this is that transcript.
        """
        conn = _conn(
            exit_status=1,
            stdout="",
            stderr="cat: /mnt/data/.biopipe/probe-abc: No such file or directory\n",
        )

        result = await probe_shared_storage(conn, STORAGE)

        assert result.shared is False
        assert "No such file" in result.detail

    @pytest.mark.asyncio
    async def test_the_existing_sentinel_does_not_count_as_shared(self, home):
        """R4, and the whole reason this module does not reuse VERSION.

        A node with its own separately-initialised home holds a byte-identical
        `.biopipe/VERSION`. If the probe compared *that* file it would report
        shared. Here the node returns exactly those bytes for the probe's own
        path, and the answer must still be not-shared.
        """
        conn = _conn(exit_status=0, stdout="biopipe-home-v1\n")

        result = await probe_shared_storage(conn, STORAGE)

        assert result.shared is False
        assert "does not contain this probe's token" in result.detail

    @pytest.mark.asyncio
    async def test_a_stale_token_from_an_earlier_probe_is_not_shared(self, home):
        """Content from a *different* probe run must not pass."""
        conn = _conn(exit_status=0, stdout="0123456789abcdef0123456789abcdef\n")

        result = await probe_shared_storage(conn, STORAGE)

        assert result.shared is False


class TestCannotAnswer:
    """R11 -- a probe that did not run is never a `False`."""

    @pytest.mark.asyncio
    async def test_timeout_raises_rather_than_recording_false(self, home):
        conn = MagicMock()
        conn.run = AsyncMock(side_effect=TimeoutError())

        with pytest.raises(StorageProbeError) as excinfo:
            await probe_shared_storage(conn, STORAGE)

        assert "unknown" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_broken_connection_raises(self, home):
        conn = MagicMock()
        conn.run = AsyncMock(side_effect=OSError("connection lost"))

        with pytest.raises(StorageProbeError):
            await probe_shared_storage(conn, STORAGE)

    @pytest.mark.asyncio
    async def test_primary_that_cannot_write_its_own_sentinel_raises(self, home):
        """The fault is the primary's, so it says nothing about the node."""
        conn = _conn()

        with patch(
            "pathlib.Path.write_text", side_effect=OSError(13, "Permission denied")
        ):
            with pytest.raises(StorageProbeError) as excinfo:
                await probe_shared_storage(conn, STORAGE)

        assert "primary" in str(excinfo.value)
        # Never reached the node: nothing was concluded about it.
        conn.run.assert_not_awaited()


class TestSentinelLifecycle:
    """R1/R5/R6 -- what is written, and that it is cleaned up."""

    @pytest.mark.asyncio
    async def test_sentinel_is_removed_after_a_successful_probe(self, home):
        conn = _conn(exit_status=0, stdout="")

        await probe_shared_storage(conn, STORAGE)

        assert list(home.iterdir()) == []

    @pytest.mark.asyncio
    async def test_sentinel_is_removed_after_a_failed_probe(self, home):
        conn = MagicMock()
        conn.run = AsyncMock(side_effect=TimeoutError())

        with pytest.raises(StorageProbeError):
            await probe_shared_storage(conn, STORAGE)

        assert list(home.iterdir()) == []

    @pytest.mark.asyncio
    async def test_a_cleanup_failure_does_not_change_the_answer(self, home):
        """R6. The probe's conclusion outranks its own tidiness."""
        conn = _conn(exit_status=1, stderr="No such file or directory\n")

        with patch("pathlib.Path.unlink", side_effect=OSError(5, "I/O error")):
            result = await probe_shared_storage(conn, STORAGE)

        assert result.shared is False

    @pytest.mark.asyncio
    async def test_each_probe_uses_a_fresh_token(self, home):
        """A reused nonce would let one probe's success vouch for the next."""
        seen = set()
        for _ in range(3):
            conn = _conn(exit_status=1)
            await probe_shared_storage(conn, STORAGE)
            seen.add(_token_from(conn))

        assert len(seen) == 3

    @pytest.mark.asyncio
    async def test_the_sentinel_explains_itself_to_whoever_finds_it(self, home):
        """It lands in a user's data directory; it should say what it is."""
        body = node_storage_probe._sentinel_body("deadbeef")

        assert "deadbeef" in body
        assert "BioFlow" in body
        assert "safe to delete" in body


class TestRemoteCommand:
    """The command sent to the node."""

    @pytest.mark.asyncio
    async def test_storage_location_is_shell_quoted(self, home):
        """`storage_location` has no validator and reaches a remote shell."""
        conn = _conn(exit_status=1)

        await probe_shared_storage(conn, "/mnt/my data; rm -rf /")

        command = conn.run.await_args.args[0]
        assert "'/mnt/my data; rm -rf /" in command
        # The metacharacters are inside the quotes, not acting as syntax.
        assert not command.endswith("rm -rf /")

    @pytest.mark.asyncio
    async def test_reads_the_probe_path_under_the_nodes_storage_location(self, home):
        conn = _conn(exit_status=1)

        await probe_shared_storage(conn, STORAGE)

        command = conn.run.await_args.args[0]
        assert command.startswith("cat ")
        assert f"{STORAGE}/.biopipe/{node_storage_probe.SENTINEL_PREFIX}" in command

    @pytest.mark.asyncio
    async def test_reads_once(self, home):
        """No retry loop today -- #848 owns revisiting this on a real mount."""
        conn = _conn(exit_status=1)

        await probe_shared_storage(conn, STORAGE)

        assert conn.run.await_count == 1
