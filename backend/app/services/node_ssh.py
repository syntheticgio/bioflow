"""Managed SSH keys for compute nodes.

BioFlow installs a keypair of its own on each node it provisions rather than
retaining the password or key the user typed. The user's credential is used
once, to install this key, and never stored: a generated key is revocable per
node, survives the user changing their password, and is scoped to this purpose.

The install is verified by opening a second connection that authenticates with
the new key. Appending to authorized_keys can succeed and still leave a key
that sshd ignores -- wrong directory permissions, an AuthorizedKeysFile
pointing elsewhere, PubkeyAuthentication disabled. Only a round trip proves it.
"""

import asyncio

import asyncssh

from app.logging import get_logger

log = get_logger(__name__)

_VERIFY_TIMEOUT_SECONDS = 15


class KeyInstallError(Exception):
    """The managed key could not be installed or did not authenticate."""


def generate_keypair(node_name: str) -> tuple[str, str]:
    """A new Ed25519 keypair as (private PEM, public authorized_keys line).

    `export_private_key()`/`export_public_key()` return `bytes`, hence the
    `.decode()`. The comment passed to `generate_private_key` is baked into
    the exported public key line automatically -- `export_public_key()`
    takes no `comment` kwarg of its own.
    """
    key = asyncssh.generate_private_key(
        "ssh-ed25519", comment=f"bioflow-node-{node_name}"
    )
    private_pem = key.export_private_key().decode()
    public_line = key.export_public_key().decode().strip()
    return private_pem, public_line


async def install_public_key(conn, public_line: str) -> None:
    """Append `public_line` to the remote user's authorized_keys.

    Append, never overwrite: this file usually holds the key the user reaches
    their own machine with, and truncating it would lock them out.
    """
    commands = [
        "mkdir -p ~/.ssh",
        "chmod 700 ~/.ssh",
        "touch ~/.ssh/authorized_keys",
        f"printf '%s\\n' {_quote(public_line)} >> ~/.ssh/authorized_keys",
        "chmod 600 ~/.ssh/authorized_keys",
    ]
    for command in commands:
        result = await asyncio.wait_for(conn.run(command, check=False), timeout=15)
        if result.exit_status != 0:
            raise KeyInstallError(
                f"Could not install the BioFlow key on this node: {command!r} "
                f"failed ({result.stderr or result.stdout or 'no output'})."
            )


async def verify_key(host: str, port: int, username: str, private_pem: str) -> None:
    """Prove the installed key authenticates, by using it.

    `import_private_key` takes the PEM directly as `bytes | str` -- it does
    NOT accept a file-like object such as `io.StringIO`; passing one raises
    `AttributeError` because the implementation calls `.startswith()` on the
    argument. Confirmed empirically against asyncssh 2.24.0.
    """
    try:
        key = asyncssh.import_private_key(private_pem)
        conn = await asyncio.wait_for(
            asyncssh.connect(
                host,
                port=port,
                username=username,
                known_hosts=None,
                client_keys=[key],
            ),
            timeout=_VERIFY_TIMEOUT_SECONDS,
        )
    except (TimeoutError, asyncssh.Error, ValueError) as e:
        raise KeyInstallError(
            "The BioFlow key was written to this node but does not "
            f"authenticate: {e}. Check that sshd allows public-key login."
        ) from e

    try:
        result = await asyncio.wait_for(conn.run("true", check=False), timeout=15)
        if result.exit_status != 0:
            raise KeyInstallError("The BioFlow key authenticated but no command ran.")
    finally:
        conn.close()


def _quote(value: str) -> str:
    """Single-quote a value for a POSIX shell."""
    return "'" + value.replace("'", "'\\''") + "'"
