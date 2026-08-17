"""The install directory provisioning sends over SSH must not be expanded here.

`~/.bioflow` has to reach the node unexpanded so the *remote* user's shell
resolves it. Expanding it in the API container resolved it against the
container's own HOME -- root's -- and provisioning then ran
`mkdir -p /root/.bioflow` on the node, which fails for any non-root SSH user
with a bare "Process exited with non-zero exit status 1".

These tests assert on the command strings handed to `conn.run`, because that
is the seam the bug crossed: every unit under it was individually correct.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.api.v1.nodes import ProvisionRequest, _provision_node
from app.services import node_update_service

# `beanie_models` is module-scoped with loop_scope="module"; without a matching
# loop scope here the Mongo client is created on a different event loop than
# the tests run on and every case errors. Same pairing as
# tests/models/test_node_update.py.
pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


class _Result:
    """Stand-in for asyncssh's process result."""

    def __init__(self, exit_status: int = 0, stdout: str = "", stderr: str = ""):
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


def _request(**overrides) -> ProvisionRequest:
    fields = {
        "host": "node.example.com",
        "username": "alice",
        "password": "hunter2",
        "node_name": "child-laptop",
    }
    fields.update(overrides)
    return ProvisionRequest(**fields)


class _FakeConn:
    """Records every command, and lets a test fail a chosen one."""

    def __init__(self, fail_matching: str | None = None, exit_status: int = 1):
        self.commands: list[str] = []
        self._fail_matching = fail_matching
        self._exit_status = exit_status

    async def run(self, command, check=False, **kwargs):
        self.commands.append(command)
        if self._fail_matching and self._fail_matching in command:
            return _Result(self._exit_status, stderr="Permission denied")
        return _Result(0, stdout="27.0.3")

    def close(self):
        pass


@pytest.fixture
def fake_conn():
    return _FakeConn()


def _patched(conn):
    """Patch the whole provisioning perimeter around a fake connection."""
    return (
        patch("app.api.v1.nodes.asyncssh.connect", AsyncMock(return_value=conn)),
        # No scp/pathlib patches: the compose file is rendered in-process and
        # written by the same `conn.run` heredoc as the .env, so the fake
        # connection records it like any other command.
        patch("app.api.v1.nodes.node_ssh.generate_keypair",
              MagicMock(return_value=("PEM", "ssh-ed25519 AAAA comment"))),
        patch("app.api.v1.nodes.node_ssh.install_public_key", AsyncMock()),
        patch("app.api.v1.nodes.node_ssh.verify_key",
              AsyncMock(return_value=(MagicMock(), "ssh-ed25519 AAAA"))),
        patch("app.api.v1.nodes.crypto.encrypt", MagicMock(return_value="enc")),
    )


async def _run_provision(conn, req=None):
    patches = _patched(conn)
    for p in patches:
        p.start()
    try:
        await _provision_node("task-1", req or _request())
    finally:
        for p in patches:
            p.stop()


class TestInstallDirIsRemote:
    async def test_mkdir_uses_the_unexpanded_tilde_path(self, fake_conn):
        """The literal `~` must survive into the remote command."""
        await _run_provision(fake_conn)

        mkdir = [c for c in fake_conn.commands if c.startswith("mkdir -p")]
        assert mkdir == ["mkdir -p ~/.bioflow"]

    async def test_no_command_contains_a_locally_expanded_home(self, fake_conn):
        """The regression: `/root/...` is this container's HOME, not the node's.

        Asserted across every command rather than just mkdir, since the .env
        write and the compose invocation interpolate the same value.
        """
        await _run_provision(fake_conn)

        offenders = [c for c in fake_conn.commands if "/root/" in c]
        assert offenders == []

    async def test_install_dir_is_shared_with_the_update_service(self, fake_conn):
        """Provisioning and updates must target one directory, not two.

        They are separate code paths against the same node: a node provisioned
        into one directory and updated in another silently stops updating.
        """
        await _run_provision(fake_conn)

        assert node_update_service.INSTALL_DIR == "~/.bioflow"
        assert any(
            node_update_service.INSTALL_DIR in c for c in fake_conn.commands
        )


class TestRemoteFailuresAreReported:
    """A failing remote command must surface its stderr, not asyncssh's string.

    Both of these ran with `check=True`, so asyncssh raised ProcessError and the
    generic handler reported "Process exited with non-zero exit status 1" --
    which named neither the command nor the reason.
    """

    async def test_mkdir_failure_names_the_directory_and_stderr(self):
        conn = _FakeConn(fail_matching="mkdir -p")
        await _run_provision(conn)

        from app.models.node_provision import NodeProvisionTask

        task = await NodeProvisionTask.find_one(NodeProvisionTask.task_id == "task-1")
        assert task.status == "failed"
        assert "~/.bioflow" in task.error
        assert "Permission denied" in task.error
        assert "exit status" not in task.error

    async def test_env_write_failure_names_the_file_and_stderr(self):
        # Matched on the .env path rather than a bare `cat > `: provisioning
        # writes two files by heredoc now, and the compose one goes first.
        conn = _FakeConn(fail_matching="cat > ~/.bioflow/.env")
        await _run_provision(conn)

        from app.models.node_provision import NodeProvisionTask

        task = await NodeProvisionTask.find_one(NodeProvisionTask.task_id == "task-1")
        assert task.status == "failed"
        assert ".env" in task.error
        assert "Permission denied" in task.error

    async def test_compose_write_failure_names_the_file_and_stderr(self):
        conn = _FakeConn(fail_matching="cat > ~/.bioflow/docker-compose.yml")
        await _run_provision(conn)

        from app.models.node_provision import NodeProvisionTask

        task = await NodeProvisionTask.find_one(NodeProvisionTask.task_id == "task-1")
        assert task.status == "failed"
        assert "docker-compose.yml" in task.error
        assert "Permission denied" in task.error
