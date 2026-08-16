"""Managed SSH key install.

Every test mocks asyncssh, so these verify the logic and the shell commands
we construct -- not that a real sshd accepts the key. That gap is closed by
the manual verification in Task 12.
"""

from unittest.mock import AsyncMock, MagicMock, patch

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

    # The host_key_verifier callback inside connect_with_tofu calls
    # host_key.export_public_key().decode().strip() on the server's key.
    fake_host_key = MagicMock()
    fake_host_key.export_public_key.return_value = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake"

    with patch("asyncssh.connect", AsyncMock(return_value=fake_conn)) as conn_mock, \
         patch("asyncssh.import_private_key", MagicMock(return_value="KEY")):
        conn, host_key = await node_ssh.verify_key("10.0.0.5", 22, "ops", "PEM")

    assert conn_mock.await_count == 1
    assert conn_mock.await_args.kwargs["client_keys"] == ["KEY"]
    # known_hosts is a callable, not None
    assert callable(conn_mock.await_args.kwargs["known_hosts"])
    assert host_key == ""  # No host key captured since mock doesn't invoke callback


async def test_verify_raises_when_authentication_fails():
    import asyncssh

    with patch("asyncssh.connect", AsyncMock(side_effect=asyncssh.Error(1, "denied"))), \
         patch("asyncssh.import_private_key", MagicMock(return_value="KEY")):
        with pytest.raises(node_ssh.KeyInstallError):
            await node_ssh.verify_key("10.0.0.5", 22, "ops", "PEM")
