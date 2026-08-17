"""The managed-key round trip against a real sshd.

Everything in `tests/services/test_node_ssh.py` mocks `asyncssh`, so it proves
the shell commands we build and nothing about whether a real sshd accepts the
key. Issue #356 was filed to close that gap by hand, against a real machine.
This closes it in code instead, for the parts that need sshd rather than
hardware: `authorized_keys` permissions, `AuthorizedKeysFile`, and
`PubkeyAuthentication` are sshd behaviours, and a container runs a real sshd.

It found the bug it was written to look for on its first run. `verify_key`
passed its host-key callback as `known_hosts=`, where asyncssh treats a
callable as a known-hosts *lookup* and calls it with three strings -- so the
callback raised `AttributeError: 'str' object has no attribute
'export_public_key'` against every real server while the mocked tests, which
never invoke the callback, stayed green. TOFU pinning had never once worked.

What still cannot be tested here, and stays manual on #356: a real update's
success and failure paths, which need Docker on a second machine and a worker
that re-enrolls.

Skipped unless BIOFLOW_TEST_SSHD_HOST is set. The test container has no Docker
socket, so it cannot start sshd itself; `run-worktree-tests.sh` starts the
sidecar and points this at it, the same way it provisions a private Mongo.
Skip rather than fail: an ordinary suite run should not go red for not having
opted into a container sidecar.
"""

import os

import asyncssh
import pytest

from app.services import node_ssh

SSHD_HOST = os.environ.get("BIOFLOW_TEST_SSHD_HOST")
SSHD_PORT = int(os.environ.get("BIOFLOW_TEST_SSHD_PORT", "2222"))
SSHD_USER = os.environ.get("BIOFLOW_TEST_SSHD_USER", "bioflow")
SSHD_PASSWORD = os.environ.get("BIOFLOW_TEST_SSHD_PASSWORD", "testpw")

pytestmark = pytest.mark.skipif(
    not SSHD_HOST,
    reason="Set BIOFLOW_TEST_SSHD_HOST (run-worktree-tests.sh --with-sshd)",
)

# A key that is already in the file before BioFlow touches it: the one the
# user reaches their own machine with. Every assertion about appending is
# really an assertion that this line survived.
PREEXISTING = "ssh-ed25519 AAAAPREEXISTINGKEYNOTOURS user@their-laptop"


async def _password_conn():
    """A session as the user would first reach the node: password auth."""
    return await asyncssh.connect(
        SSHD_HOST,
        port=SSHD_PORT,
        username=SSHD_USER,
        password=SSHD_PASSWORD,
        known_hosts=None,
    )


@pytest.fixture
async def clean_authorized_keys():
    """Reset authorized_keys to exactly one pre-existing key.

    Per-test, not per-module: these tests append to the same real file, and a
    leaked key from an earlier test would make a later `grep -c` assertion
    pass or fail for the wrong reason.
    """
    async with await _password_conn() as conn:
        await conn.run(
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"printf '%s\\n' '{PREEXISTING}' > ~/.ssh/authorized_keys",
            check=True,
        )
    yield


async def _authorized_keys() -> str:
    async with await _password_conn() as conn:
        result = await conn.run("cat ~/.ssh/authorized_keys", check=True)
    return result.stdout


async def test_install_appends_without_destroying_existing_keys(
    clean_authorized_keys,
):
    """#356's check 1: the user must not be locked out of their own machine.

    The mocked sibling test infers this by grepping our command string for
    `>>`. This reads the file back off a real sshd afterwards.
    """
    _, public_line = node_ssh.generate_keypair("roundtrip")

    async with await _password_conn() as conn:
        await node_ssh.install_public_key(conn, public_line)

    contents = await _authorized_keys()
    assert PREEXISTING in contents, "the user's own key was destroyed"
    assert public_line in contents
    assert contents.count("bioflow-node-") == 1
    assert len(contents.strip().splitlines()) == 2


async def test_installed_key_actually_authenticates(clean_authorized_keys):
    """#356's check 2, and the one only a real sshd can answer.

    An appended key can still be ignored -- wrong directory permissions, an
    AuthorizedKeysFile pointing elsewhere, PubkeyAuthentication off. Only a
    round trip proves it, which is why `verify_key` opens a second connection
    rather than reusing the session that wrote the file.
    """
    private_pem, public_line = node_ssh.generate_keypair("roundtrip")

    async with await _password_conn() as conn:
        await node_ssh.install_public_key(conn, public_line)

    conn, host_key = await node_ssh.verify_key(
        SSHD_HOST, SSHD_PORT, SSHD_USER, private_pem
    )
    conn.close()

    # A real host key, captured for TOFU pinning -- not the empty string the
    # mocked tests settle for because their connect mock never calls back.
    assert host_key
    assert host_key.split()[0].startswith("ssh-")


async def test_verify_fails_when_the_key_was_never_installed():
    """The negative direction, which is the one that fails when the seam breaks.

    Per CLAUDE.md: asserting the happy path passes whether or not the test is
    really reaching sshd. Authenticating with a key the server has never seen
    must be refused.
    """
    private_pem, _ = node_ssh.generate_keypair("never-installed")

    with pytest.raises(node_ssh.KeyInstallError) as excinfo:
        await node_ssh.verify_key(SSHD_HOST, SSHD_PORT, SSHD_USER, private_pem)

    assert "does not authenticate" in str(excinfo.value)


async def test_install_fails_loudly_when_authorized_keys_is_unwritable(
    clean_authorized_keys,
):
    """#356's check 3(a): the key cannot be written, without breaking a machine.

    NU-14 says a node that cannot take the key is not provisioned at all --
    never a silent fallback to storing the user's password. The endpoint half
    of that (no Node document, no pull) is asserted in
    `tests/api/test_node_provision.py`; this is the half that needs sshd to
    actually refuse the write.

    The issue suggests `chmod 500 ~`, which does not reproduce here: creating a
    file in a 500 directory is still permitted for this user, and
    `install_public_key` starts with `mkdir -p`/`touch` that both succeed. An
    unwritable `authorized_keys` blocks the append itself, which is the
    operation whose failure actually matters.
    """
    async with await _password_conn() as conn:
        await conn.run("chmod 400 ~/.ssh/authorized_keys", check=True)
        try:
            _, public_line = node_ssh.generate_keypair("unwritable")
            with pytest.raises(node_ssh.KeyInstallError) as excinfo:
                await node_ssh.install_public_key(conn, public_line)
        finally:
            await conn.run("chmod 600 ~/.ssh/authorized_keys", check=False)

    message = str(excinfo.value)
    # The failing command and sshd's own stderr, not a generic failure.
    assert "authorized_keys" in message
    assert "Could not install the BioFlow key" in message

    # And nothing was written despite the error.
    assert "bioflow-node-" not in await _authorized_keys()


async def test_pinned_host_key_is_enforced(clean_authorized_keys):
    """TOFU pinning, both directions.

    This is the code path that could not execute at all before #356: the
    verifier crashed before ever comparing keys, so a changed host key was
    never actually refused. Asserting only the accept direction would pass
    against a verifier that trusts everything.
    """
    private_pem, public_line = node_ssh.generate_keypair("pinned")

    async with await _password_conn() as conn:
        await node_ssh.install_public_key(conn, public_line)

    conn, host_key = await node_ssh.connect_with_tofu(
        SSHD_HOST, SSHD_PORT, SSHD_USER, private_pem, None
    )
    conn.close()
    assert host_key

    # The same key on re-connect is accepted.
    conn, again = await node_ssh.connect_with_tofu(
        SSHD_HOST, SSHD_PORT, SSHD_USER, private_pem, host_key
    )
    conn.close()
    assert again == host_key

    # A different key means a different machine: refused, and said so.
    with pytest.raises(asyncssh.HostKeyNotVerifiable) as excinfo:
        await node_ssh.connect_with_tofu(
            SSHD_HOST,
            SSHD_PORT,
            SSHD_USER,
            private_pem,
            "ssh-ed25519 AAAASOMEOTHERMACHINE bogus",
        )
    assert "changed" in str(excinfo.value)
    assert host_key in str(excinfo.value)
