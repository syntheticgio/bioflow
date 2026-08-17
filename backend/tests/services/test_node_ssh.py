"""Managed SSH key install.

Every test mocks asyncssh, so these verify the logic and the shell commands
we construct -- not that a real sshd accepts the key. That gap is closed by
`tests/integration/test_node_ssh_live.py`, which runs the same calls against
a real sshd in a container.

Mocking `asyncssh.connect` means the host-key callback is never invoked by
asyncssh itself, which is how a verifier that crashed against every real
server stayed green here for the life of the feature (#356). The callback is
therefore asserted directly, not only through `connect`.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest
from app.services import node_ssh


def _conn(exit_status: int = 0):
    conn = MagicMock()
    result = MagicMock()
    result.exit_status = exit_status
    result.stdout = ""
    result.stderr = ""
    conn.run = AsyncMock(return_value=result)
    return conn


def test_generate_keypair_returns_private_and_public():
    private_pem, public_line = node_ssh.generate_keypair("mynode")
    assert "PRIVATE KEY" in private_pem
    assert public_line.startswith("ssh-ed25519 ")
    assert "bioflow-node-mynode" in public_line


async def test_install_appends_and_never_truncates():
    """Overwriting authorized_keys would destroy the user's own access."""
    conn = _conn()
    await node_ssh.install_public_key(conn, "ssh-ed25519 AAAA bioflow-node-x")

    commands = " ; ".join(c.args[0] for c in conn.run.call_args_list)
    assert ">>" in commands
    assert ">" in commands  # sanity: the append operator is present
    # A single '>' redirect into authorized_keys truncates it.
    assert "> ~/.ssh/authorized_keys" not in commands.replace(">>", "")
    assert "chmod 700" in commands
    assert "chmod 600" in commands


async def test_install_raises_when_a_command_fails():
    conn = _conn(exit_status=1)
    with pytest.raises(node_ssh.KeyInstallError):
        await node_ssh.install_public_key(conn, "ssh-ed25519 AAAA x")


async def test_verify_opens_a_new_connection_with_the_new_key():
    """Verification must authenticate with the key, not reuse the open
    session -- an appended key can still be ignored by sshd."""
    fake_conn = MagicMock()
    fake_conn.run = AsyncMock(return_value=MagicMock(exit_status=0))
    fake_conn.close = MagicMock()

    with patch("asyncssh.connect", AsyncMock(return_value=fake_conn)) as conn_mock, \
         patch("asyncssh.import_private_key", MagicMock(return_value="KEY")):
        conn, host_key = await node_ssh.verify_key("10.0.0.5", 22, "ops", "PEM")

    assert conn_mock.await_count == 1
    assert conn_mock.await_args.kwargs["client_keys"] == ["KEY"]

    kwargs = conn_mock.await_args.kwargs
    # The host key is inspected via a client_factory client, not by passing a
    # callable as known_hosts -- see connect_with_tofu's docstring. A callable
    # there is a known-hosts *lookup* (called with three strings), so the old
    # spelling raised AttributeError against every real server.
    assert issubclass(kwargs["client_factory"], asyncssh.SSHClient)
    # known_hosts must be absent, not None: None disables validation entirely.
    assert "known_hosts" not in kwargs
    assert host_key == ""  # No host key captured since mock doesn't invoke callback


async def test_tofu_client_captures_and_pins_the_host_key():
    """The verifier callback itself, which the connect mock never invokes.

    This is the code that crashed against real sshd while every test stayed
    green -- so it is asserted directly rather than only through `connect`.
    """
    host_key = MagicMock()
    host_key.export_public_key.return_value = b"ssh-ed25519 AAAAREAL server\n"

    async def client_for(stored):
        """The `_TofuClient` connect_with_tofu would hand to asyncssh."""
        with patch("asyncssh.connect", AsyncMock(return_value=MagicMock())) as m, \
             patch("asyncssh.import_private_key", MagicMock(return_value="KEY")):
            await node_ssh.connect_with_tofu("10.0.0.5", 22, "ops", "PEM", stored)
        return m.await_args.kwargs["client_factory"]()

    # First use: no stored key, so the server's key is accepted and captured.
    client = await client_for(None)
    assert client.validate_host_public_key("h", "a", 22, host_key) is True

    # Enrolled and unchanged: the matching key is accepted.
    client = await client_for("ssh-ed25519 AAAAREAL server")
    assert client.validate_host_public_key("h", "a", 22, host_key) is True

    # Changed identity: refused, and the message names both keys.
    client = await client_for("ssh-ed25519 AAAAOTHER old")
    with pytest.raises(asyncssh.HostKeyNotVerifiable) as excinfo:
        client.validate_host_public_key("h", "a", 22, host_key)
    assert "AAAAOTHER" in str(excinfo.value)
    assert "AAAAREAL" in str(excinfo.value)


async def test_verify_raises_when_authentication_fails():
    import asyncssh

    with patch("asyncssh.connect", AsyncMock(side_effect=asyncssh.Error(1, "denied"))), \
         patch("asyncssh.import_private_key", MagicMock(return_value="KEY")):
        with pytest.raises(node_ssh.KeyInstallError):
            await node_ssh.verify_key("10.0.0.5", 22, "ops", "PEM")
