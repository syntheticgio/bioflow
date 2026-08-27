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
            log.warning(
                "node_ssh_install_command_failed",
                command=command,
                exit_status=result.exit_status,
                error=result.stderr or result.stdout or "no output",
            )
            raise KeyInstallError(
                f"Could not install the BioFlow key on this node: {command!r} "
                f"failed ({result.stderr or result.stdout or 'no output'})."
            )


async def connect_with_tofu(
    host: str,
    port: int,
    username: str,
    private_key: str | None = None,
    password: str | None = None,
    stored_host_key: str | None = None,
    *,
    timeout_seconds: int = _VERIFY_TIMEOUT_SECONDS,
) -> tuple[asyncssh.SSHClientConnection, str]:
    """Connect with TOFU (trust-on-first-use) host key verification.

    If `stored_host_key` is provided, it is enforced: the server must present
    a matching key or the connection is refused. If None, the server's key is
    captured on first use and returned so the caller can persist it.

    Authentication uses `private_key` (PEM string) or `password`, never both.
    At least one must be provided.

    Returns (connection, actual_host_key).

    The host key is inspected through a `client_factory` client's
    `validate_host_public_key`, NOT by passing a callable as `known_hosts`.
    A callable given to `known_hosts` is a *known-hosts lookup*: asyncssh
    calls it with `(host, addr, port)` -- three strings -- and expects it to
    return the known-hosts entries for that host. Used as a host-key verifier
    it raises `AttributeError: 'str' object has no attribute
    'export_public_key'` against any real server, which no test caught
    because they all mock `asyncssh.connect` and never invoke the callback.

    `known_hosts` is left unset rather than passed as None: `known_hosts=None`
    disables host key validation altogether, which would silently turn TOFU
    pinning off instead of enforcing it.
    """
    if not private_key and not password:
        raise ValueError("connect_with_tofu: provide private_key or password")
    if private_key and password:
        raise ValueError("connect_with_tofu: provide private_key or password, not both")

    captured_key: list[str] = []

    class _TofuClient(asyncssh.SSHClient):
        """asyncssh calls this with the server's host key, as an SSHKey."""

        def validate_host_public_key(self, host_, addr, port_, host_key):
            actual = host_key.export_public_key().decode().strip()
            if stored_host_key is not None and actual != stored_host_key:
                # Raised, not returned False: asyncssh turns a False return
                # into its own generic "Host key is not trusted" message,
                # which loses the expected/actual detail below.
                raise asyncssh.HostKeyNotVerifiable(
                    f"Host key for {host} changed.\n"
                    f"Expected: {stored_host_key}\n"
                    f"Actual:   {actual}\n"
                    "The machine identity may have changed. Re-enroll the node "
                    "to accept the new key."
                )
            captured_key.append(actual)
            return True

    connect_kw: dict = {
        "host": host,
        "port": port,
        "username": username,
        "client_factory": _TofuClient,
    }
    if private_key:
        connect_kw["client_keys"] = [asyncssh.import_private_key(private_key)]
    if password:
        connect_kw["password"] = password

    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(**connect_kw),
            timeout=timeout_seconds,
        )
    except (TimeoutError, asyncssh.Error, ValueError) as e:
        log.warning(
            "node_ssh_connect_failed",
            host=host,
            port=port,
            username=username,
            error=str(e),
        )
        raise

    return conn, captured_key[0] if captured_key else ""


async def verify_key(
    host: str, port: int, username: str, private_pem: str
) -> tuple[asyncssh.SSHClientConnection, str]:
    """Prove the installed key authenticates, by using it.

    `import_private_key` takes the PEM directly as `bytes | str` -- it does
    NOT accept a file-like object such as `io.StringIO`; passing one raises
    `AttributeError` because the implementation calls `.startswith()` on the
    argument. Confirmed empirically against asyncssh 2.24.0.

    Returns (connection, host_key) so the caller can persist the host key.
    """
    try:
        conn, host_key = await connect_with_tofu(
            host, port, username, private_pem, stored_host_key=None,
        )
    except (TimeoutError, asyncssh.Error, ValueError) as e:
        log.warning(
            "node_ssh_verify_auth_failed",
            host=host,
            port=port,
            username=username,
            error=str(e),
        )
        raise KeyInstallError(
            "The BioFlow key was written to this node but does not "
            f"authenticate: {e}. Check that sshd allows public-key login."
        ) from e

    try:
        result = await asyncio.wait_for(conn.run("true", check=False), timeout=15)
        if result.exit_status != 0:
            log.warning(
                "node_ssh_verify_command_failed",
                host=host,
                port=port,
                username=username,
                exit_status=result.exit_status,
                error=result.stderr or result.stdout or "no output",
            )
            raise KeyInstallError("The BioFlow key authenticated but no command ran.")
    finally:
        conn.close()

    return conn, host_key


def _quote(value: str) -> str:
    """Single-quote a value for a POSIX shell."""
    return "'" + value.replace("'", "'\\''") + "'"


# The same helper, under a name another module may use. `_quote` stays private
# because every module that builds a shell command has its own -- the pipeline
# runners each define one (`align_runner`, `ivar_runner`, `variant_runner`) and
# reaching across for someone else's underscore-prefixed function would break
# that convention rather than follow it. `node_storage_probe` interpolates a
# user-supplied `storage_location` into a remote `cat`, needs exactly this
# quoting, and should not carry a fourth copy of two lines.
quote_for_shell = _quote
