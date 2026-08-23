"""Tests for node provisioning endpoints and executor."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.errors import register_exception_handlers
from app.models.node_provision import NodeProvisionTask

# The autouse cleanup fixture below queries NodeProvisionTask after every test
# in this module, including the pure-function ones, so beanie must be
# initialized for all of them -- without this the teardown raises
# CollectionWasNotInitialized and every test errors.
pytestmark = pytest.mark.usefixtures("beanie_models")
# Applied per test: this module mixes async API tests with pure sync ones.
asyncio_module_loop = pytest.mark.asyncio(loop_scope="module")

# ---- helpers ----

def _verify_key_mock() -> AsyncMock:
    """A `node_ssh.verify_key` stand-in returning the (conn, host_key) pair.

    `verify_key` has returned a two-tuple since host keys were pinned on first
    use, and `_provision_node` unpacks it. A bare `AsyncMock()` returns a
    MagicMock, which unpacks to nothing -- so provisioning died with
    `ValueError: not enough values to unpack` inside the executor's catch-all
    `except Exception`, and the run simply stopped after `install_key` with no
    sign that the *mock*, not the code, was wrong (#444).
    """
    conn = MagicMock()
    conn.close = MagicMock()
    return AsyncMock(
        return_value=(conn, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake")
    )


def _app():
    """Bare FastAPI app with only the nodes router."""
    app = FastAPI()
    app.include_router(pytest.importorskip("app.api.v1.nodes").router)
    register_exception_handlers(app)
    return app


@pytest.fixture
def app():
    return _app()


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_node_provisions():
    """Each test starts with no provisioning tasks.

    `loop_scope="module"` must match `beanie_models`: a plain
    `@pytest.fixture` runs on a fresh per-function loop, and the module-scoped
    Mongo client refuses to be used from it ("Cannot use AsyncMongoClient in
    different event loop"). Cleaning on entry rather than exit also leaves a
    failed run's documents behind for inspection, as `beanie_models` does.
    """
    await NodeProvisionTask.find_all().delete()


# ---- POST /nodes/provision validation ----

@asyncio_module_loop
async def test_provision_missing_host_returns_422(client):
    res = await client.post("/nodes/provision", json={
        "username": "test", "password": "x", "node_name": "n",
    })
    assert res.status_code == 422


@asyncio_module_loop
async def test_provision_neither_password_nor_key_returns_422(client):
    res = await client.post("/nodes/provision", json={
        "host": "1.2.3.4", "username": "test", "node_name": "n",
    })
    assert res.status_code == 422


@asyncio_module_loop
async def test_provision_both_password_and_key_returns_422(client):
    res = await client.post("/nodes/provision", json={
        "host": "1.2.3.4", "username": "test",
        "password": "x", "private_key": "y",
        "node_name": "n",
    })
    assert res.status_code == 422


@asyncio_module_loop
async def test_provision_valid_request_returns_201_and_task_id(client):
    with patch("app.api.v1.nodes._provision_node", new_callable=lambda: AsyncMock()):
        res = await client.post("/nodes/provision", json={
            "host": "1.2.3.4", "username": "test",
            "password": "x", "node_name": "test-node",
        })
    assert res.status_code == 201
    data = res.json()
    assert "task_id" in data
    assert data["status"] == "provisioning"


@asyncio_module_loop
async def test_provision_with_private_key_returns_201(client):
    with patch("app.api.v1.nodes._provision_node", new_callable=lambda: AsyncMock()):
        res = await client.post("/nodes/provision", json={
            "host": "1.2.3.4", "username": "test",
            "private_key": "fake-key-content",
            "node_name": "test-node",
        })
    assert res.status_code == 201


# ---- GET /nodes/provision/{task_id} ----

@asyncio_module_loop
async def test_provision_status_unknown_task_returns_404(client):
    res = await client.get("/nodes/provision/nonexistent")
    assert res.status_code == 404


@asyncio_module_loop
async def test_provision_status_known_task_returns_fields(client):
    task = NodeProvisionTask(
        task_id="test123",
        status="provisioning",
        phase="validate_ssh",
        message="Connecting…",
        node_name="test-node",
        host="1.2.3.4",
    )
    await task.insert()

    res = await client.get("/nodes/provision/test123")
    assert res.status_code == 200
    data = res.json()
    assert data["task_id"] == "test123"
    assert data["status"] == "provisioning"
    assert data["phase"] == "validate_ssh"
    assert data["message"] == "Connecting…"
    assert data["node_name"] == "test-node"
    assert data["host"] == "1.2.3.4"


@asyncio_module_loop
async def test_provision_status_success(client):
    task = NodeProvisionTask(
        task_id="done",
        status="success",
        phase="enrolled",
        message="Node enrolled ✓",
        node_name="done-node",
        host="1.2.3.4",
        finished_at=datetime.now(UTC),
    )
    await task.insert()

    res = await client.get("/nodes/provision/done")
    assert res.status_code == 200
    assert res.json()["status"] == "success"


@asyncio_module_loop
async def test_provision_status_failed(client):
    task = NodeProvisionTask(
        task_id="bad",
        status="failed",
        phase="validate_ssh",
        message="Connection refused",
        error="Connection refused",
        node_name="bad-node",
        host="1.2.3.4",
        finished_at=datetime.now(UTC),
    )
    await task.insert()

    res = await client.get("/nodes/provision/bad")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "failed"
    assert data["error"] == "Connection refused"


# ---- helpers ----

def test_render_node_compose_is_valid_yaml_declaring_only_the_worker():
    """The generated compose file parses, and names no service but worker.

    The point of generating it rather than shipping the primary's own
    docker-compose.yml is that a compute node must not stand up a second
    mongo/redis/api/web. This is the test that would fail if one crept back.
    """
    import yaml

    from app.api.v1.nodes import _render_node_compose

    parsed = yaml.safe_load(_render_node_compose())

    assert list(parsed["services"]) == ["worker"]
    # depends_on would reference a service this file does not define, so
    # `docker compose up` on the node would fail outright.
    assert "depends_on" not in parsed["services"]["worker"]


def test_render_node_compose_reads_only_keys_the_env_defines():
    """Every ${VAR} in the compose file is set by _render_node_env, or
    carries its own default.

    The two are written to the node side by side and nothing else supplies
    values, so a reference to a key the .env never sets silently resolves to
    an empty string at `docker compose up` time.
    """
    import re

    from app.api.v1.nodes import _render_node_compose, _render_node_env

    env = _render_node_env(
        mongo_url="mongodb://192.168.1.50:27017/biopipe",
        redis_url="redis://192.168.1.50:6379/0",
        api_url="http://192.168.1.50:8000",
        node_name="test-node",
        storage_location="/data/scratch",
        worker_replicas=2,
    )
    env_keys = {line.split("=", 1)[0] for line in env.splitlines() if "=" in line}

    compose = _render_node_compose()
    for ref in re.findall(r"\$\{([A-Z_][A-Z0-9_]*)([:?-][^}]*)?\}", compose):
        name, modifier = ref
        # A `:-default` reference is fine unset; `:?` requires a real value.
        if modifier.startswith(":-"):
            continue
        assert name in env_keys, f"{name} is referenced but never written to .env"


def test_render_node_compose_pins_storage_to_the_configured_location():
    """BIOINFO_HOME from the .env is what gets bind-mounted at /data."""
    from app.api.v1.nodes import _render_node_compose

    compose = _render_node_compose()

    assert "- ${BIOINFO_HOME}:/data" in compose
    # /data is the in-container path and must stay hardcoded: the env's own
    # BIOINFO_HOME is the *host* path, and mounting it at itself would
    # diverge from the primary, where /data is what every tool path assumes.
    assert "BIOINFO_HOME: /data" in compose


def test_render_node_env_matches_launcher():
    """_render_node_env output must match the launcher's format."""
    from app.api.v1.nodes import _render_node_env

    mongo_url = (
        "mongodb://192.168.1.50:27017/biopipe"
        "?replicaSet=rs0&directConnection=true"
    )
    env = _render_node_env(
        mongo_url=mongo_url,
        redis_url="redis://192.168.1.50:6379/0",
        api_url="http://192.168.1.50:8000",
        node_name="test-node",
        storage_location="/data/scratch",
        worker_replicas=2,
    )

    assert "NODE_TYPE=compute" in env
    assert f"MONGO_URL={mongo_url}" in env
    assert "REDIS_URL=redis://192.168.1.50:6379/0" in env
    assert "WORKER_NODE_ID=test-node" in env
    assert "PRIMARY_API_URL=http://192.168.1.50:8000" in env
    assert "BIOINFO_HOME=/data/scratch" in env
    assert "BIOINFO_REGISTER_ROOTS=/data/scratch" in env
    assert "BIOFLOW_TAG=latest" in env
    assert "WORKER_REPLICAS=2" in env
    # Must NOT contain any SSH credential
    assert "password" not in env.lower()
    assert "private_key" not in env


def test_primary_hostname_uses_config():
    """_primary_hostname returns PRIMARY_HOSTNAME when set."""
    from app.api.v1 import nodes as mod
    from app.api.v1.nodes import _primary_hostname

    with patch.object(mod.settings, "primary_hostname", "myhost.local"):
        assert _primary_hostname() == "myhost.local"


def test_primary_hostname_falls_back_to_socket():
    """_primary_hostname falls back to gethostname when config is empty."""
    import socket

    from app.api.v1 import nodes as mod
    from app.api.v1.nodes import _primary_hostname

    with patch.object(mod.settings, "primary_hostname", ""), \
         patch.object(mod.socket, "socket", side_effect=OSError("no net")):
        name = _primary_hostname()
        assert len(name) > 0
        assert name == socket.gethostname()


def test_rewrite_host_replaces_docker_names():
    """_rewrite_host replaces 'mongo' and 'redis' with the given host."""
    from app.api.v1.nodes import _rewrite_host

    # The *host* is replaced, not every occurrence of the name: "mongo" and
    # "redis" survive in the scheme ("mongodb://", "redis://"), which is why
    # this asserts on the full rewritten URL rather than `name not in result`.
    assert _rewrite_host("mongodb://mongo:27017/db", "10.0.0.1") == "mongodb://10.0.0.1:27017/db"
    assert _rewrite_host("redis://redis:6379/0", "10.0.0.1") == "redis://10.0.0.1:6379/0"

    # A real address already in the URL passes through untouched.
    unchanged = "mongodb://192.168.1.5:27017/db"
    assert _rewrite_host(unchanged, "10.0.0.1") == unchanged
    assert _rewrite_host("", "10.0.0.1") == ""


def test_provision_request_model_rejects_empty_creds():
    """ProvisionRequest rejects when neither password nor key is set."""
    from app.api.v1.nodes import ProvisionRequest

    with pytest.raises(ValueError, match="password or private_key"):
        ProvisionRequest(
            host="x", username="x",
            password=None, private_key=None,
            node_name="x",
        )


def test_provision_request_model_rejects_both_creds():
    """ProvisionRequest rejects when both password and key are set."""
    from app.api.v1.nodes import ProvisionRequest

    with pytest.raises(ValueError, match="exactly one"):
        ProvisionRequest(
            host="x", username="x",
            password="x", private_key="x",
            node_name="x",
        )


def test_provision_request_accepts_password_only():
    from app.api.v1.nodes import ProvisionRequest

    req = ProvisionRequest(
        host="x", username="x", password="x", node_name="x",
    )
    assert req.password == "x"
    assert req.private_key is None


def test_provision_request_accepts_key_only():
    from app.api.v1.nodes import ProvisionRequest

    req = ProvisionRequest(
        host="x", username="x", private_key="k", node_name="x",
    )
    assert req.private_key == "k"
    assert req.password is None


# ---- orphan cleanup ----

@asyncio_module_loop
async def test_orphaned_provision_marked_failed(client):
    """Tasks in 'provisioning' status with no active Task are marked failed."""
    task = NodeProvisionTask(
        task_id="orph",
        status="provisioning",
        phase="pull_image",
        node_name="orph-node",
        host="1.2.3.4",
    )
    await task.insert()

    from app.api.v1.nodes import _active_provisions, _clean_orphaned_provisions

    # Ensure the task_id is NOT in _active_provisions
    _active_provisions.pop("orph", None)

    await _clean_orphaned_provisions()

    updated = await NodeProvisionTask.find_one(
        NodeProvisionTask.task_id == "orph"
    )
    assert updated is not None
    assert updated.status == "failed"
    assert updated.error is not None
    assert "restart" in updated.error.lower()


# ---- managed SSH key installation (NU-14, NU-15) ----

@asyncio_module_loop
async def test_provision_stores_encrypted_key_not_the_password():
    """The user's password is used once and never stored (NU-15)."""
    from app.api.v1.nodes import ProvisionRequest, _provision_node
    from app.models.node import Node

    req = ProvisionRequest(
        host="10.0.0.9", username="ops", password="hunter2", node_name="keynode",
    )

    # _VERIFY_SETTLE_SECONDS is the post-`up -d` settle window: a real 5s
    # sleep in production, where it lets a crash-looping worker die before
    # the state is read. Nothing here asserts on settle timing, so leaving it
    # unpatched costs five seconds of wall clock for nothing.
    with patch("app.api.v1.nodes.asyncssh") as ssh, \
         patch("app.services.node_ssh.verify_key", _verify_key_mock()), \
         patch("app.api.v1.nodes._VERIFY_SETTLE_SECONDS", 0), \
         patch("app.api.v1.nodes.asyncssh.scp", AsyncMock()):
        conn = ssh.connect.return_value
        conn.run = AsyncMock(return_value=type("R", (), {
            "exit_status": 0, "stdout": "", "stderr": "",
        })())
        # `patch(module)` gives every attribute a plain MagicMock, including
        # `connect` -- but `_provision_node` awaits `asyncssh.connect(...)`,
        # which needs the call itself to return an awaitable, not just its
        # `.return_value` to be pre-set. Without this, `asyncio.wait_for`
        # raises on a non-awaitable MagicMock before any phase runs.
        ssh.connect = AsyncMock(return_value=conn)
        await _provision_node("t-key", req)

    node = await Node.find_one(Node.node_id == "keynode")
    assert node is not None
    assert node.ssh_key_enc is not None
    assert b"hunter2" not in node.ssh_key_enc
    assert node.ssh_host == "10.0.0.9"
    assert node.ssh_username == "ops"
    assert node.ssh_key_installed_at is not None

    from app.services.ai import crypto
    assert "PRIVATE KEY" in crypto.decrypt(node.ssh_key_enc)
    await node.delete()


@asyncio_module_loop
async def test_provision_fails_loudly_when_key_cannot_be_installed():
    """No fallback to storing the user's credential (NU-14): a node that
    cannot take a managed key is not provisioned at all."""
    from app.api.v1.nodes import ProvisionRequest, _provision_node
    from app.models.node import Node
    from app.services.node_ssh import KeyInstallError

    req = ProvisionRequest(
        host="10.0.0.10", username="ops", password="pw", node_name="badkey",
    )

    with patch("app.api.v1.nodes.asyncssh") as ssh, \
         patch("app.services.node_ssh.install_public_key",
               AsyncMock(side_effect=KeyInstallError("read-only home"))), \
         patch("app.api.v1.nodes.asyncssh.scp", AsyncMock()):
        conn = ssh.connect.return_value
        conn.run = AsyncMock(return_value=type("R", (), {
            "exit_status": 0, "stdout": "", "stderr": "",
        })())
        # See the sibling test above for why `connect` itself must be an
        # AsyncMock, not just its return_value.
        ssh.connect = AsyncMock(return_value=conn)
        await _provision_node("t-badkey", req)

    task = await NodeProvisionTask.find_one(NodeProvisionTask.task_id == "t-badkey")
    assert task.status == "failed"
    assert "read-only home" in task.error
    assert task.phase == "install_key"

    # The image must not have been pulled: provisioning stopped first.
    commands = " ".join(str(c) for c in conn.run.call_args_list)
    assert "docker pull" not in commands

    assert await Node.find_one(Node.node_id == "badkey") is None


# ---- regression: import_private_key receives a PEM string, not a StringIO ----

@asyncio_module_loop
async def test_provision_private_key_uses_real_import_private_key():
    """Regression guard for issue #352: `import_private_key` was called with
    `io.StringIO(req.private_key)` instead of the PEM string directly.
    Because every existing test mocked the entire `asyncssh` module, the
    signature mismatch was invisible — `io.StringIO` has no `.startswith`,
    which asyncssh relies on internally.

    This test generates a real key pair (so `import_private_key` runs for
    real) and only mocks `asyncssh.connect` and `asyncssh.scp`, not the
    key-parsing path itself. If the StringIO misuse returns, the real
    `import_private_key` will raise `AttributeError` before we ever reach
    the mocked connect.
    """
    from app.api.v1.nodes import ProvisionRequest, _provision_node
    from app.models.node import Node
    from app.services.node_ssh import generate_keypair

    private_pem, public_line = generate_keypair("regnode")

    req = ProvisionRequest(
        host="10.0.0.11", username="ops",
        private_key=private_pem, node_name="regnode",
    )

    # See the note on _VERIFY_SETTLE_SECONDS above: patched to 0 because this
    # test is about import_private_key, not the settle window.
    with patch("asyncssh.connect", AsyncMock()) as connect_mock, \
         patch("app.services.node_ssh.verify_key", _verify_key_mock()), \
         patch("app.api.v1.nodes._VERIFY_SETTLE_SECONDS", 0), \
         patch("app.api.v1.nodes.asyncssh.scp", AsyncMock()):
        conn = connect_mock.return_value
        # asyncssh's close() is synchronous, so spec it as a plain Mock: the
        # executor's finally block calls conn.close() without await, and an
        # auto-generated AsyncMock close would return a coroutine nobody
        # awaits, warning on garbage collection (#788).
        conn.close = MagicMock()
        conn.run = AsyncMock(return_value=type("R", (), {
            "exit_status": 0, "stdout": "", "stderr": "",
        })())
        await _provision_node("t-reg-352", req)

    # If we get here, import_private_key succeeded on the real PEM string.
    node = await Node.find_one(Node.node_id == "regnode")
    assert node is not None
    assert node.ssh_host == "10.0.0.11"
    await node.delete()


# ---- _execute_remote_commands (#402) ----
#
# These run against a fake connection object, which is the point of the
# extraction: every failure branch below was previously reachable only by
# arranging a real remote machine to fail in that exact way.


class _FakeResult:
    def __init__(self, exit_status=0, stdout="", stderr=""):
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


class _FakeConn:
    """Records the commands it was asked to run and replays canned results."""

    def __init__(self, results=None):
        self.commands = []
        self._results = list(results or [])

    async def run(self, command, check=False):
        self.commands.append(command)
        if self._results:
            return self._results.pop(0)
        return _FakeResult()


def _step(phase, command, timeout=15, message=None):
    from app.api.v1.nodes import RemoteStep

    return RemoteStep(
        phase=phase,
        message=message or f"running {command}",
        command=command,
        timeout=timeout,
        describe_failure=lambda r: f"{command} failed: {r.stderr or r.stdout}",
    )


@asyncio_module_loop
async def test_execute_remote_commands_runs_every_step_in_order():
    from app.api.v1.nodes import _execute_remote_commands

    conn = _FakeConn()
    progress = []

    async def on_progress(phase, message):
        progress.append((phase, message))

    await _execute_remote_commands(
        conn,
        [_step("a", "first"), _step("b", "second"), _step("c", "third")],
        on_progress=on_progress,
    )

    assert conn.commands == ["first", "second", "third"]
    assert [p for p, _ in progress] == ["a", "b", "c"]


@asyncio_module_loop
async def test_execute_remote_commands_stops_at_the_failing_command():
    """A command that fails mid-sequence stops the sequence and reports which
    one -- the later steps assume the earlier ones ran."""
    from app.api.v1.nodes import RemoteCommandError, _execute_remote_commands

    conn = _FakeConn([
        _FakeResult(0),
        _FakeResult(1, stderr="permission denied"),
    ])

    async def on_progress(phase, message):
        pass

    with pytest.raises(RemoteCommandError) as excinfo:
        await _execute_remote_commands(
            conn,
            [_step("a", "first"), _step("b", "second"), _step("c", "third")],
            on_progress=on_progress,
        )

    # The third command never ran.
    assert conn.commands == ["first", "second"]
    # The error names the step that failed, not merely that something did.
    assert excinfo.value.step.phase == "b"
    assert excinfo.value.step.command == "second"
    assert "permission denied" in excinfo.value.reason


@asyncio_module_loop
async def test_execute_remote_commands_reports_each_phase_once():
    """Commands sharing a phase report progress once, so a later command in a
    phase does not replace the message the user is already reading."""
    from app.api.v1.nodes import _execute_remote_commands

    progress = []

    async def on_progress(phase, message):
        progress.append((phase, message))

    await _execute_remote_commands(
        _FakeConn(),
        [
            _step("setup_install", "mkdir", message="Preparing install directory…"),
            _step("setup_install", "write-compose", message="ignored"),
            _step("write_env", "write-env", message="Writing node configuration…"),
        ],
        on_progress=on_progress,
    )

    assert progress == [
        ("setup_install", "Preparing install directory…"),
        ("write_env", "Writing node configuration…"),
    ]


@asyncio_module_loop
async def test_provision_emits_the_documented_phase_sequence():
    """The provisioning UI renders `phase` verbatim, so the sequence and the
    exact strings are a user-visible contract (#402)."""
    from app.api.v1.nodes import ProvisionRequest, _provision_node
    from app.models.node import Node

    req = ProvisionRequest(
        host="10.0.0.12", username="ops", password="pw", node_name="phasenode",
    )

    phases = []
    real_save = NodeProvisionTask.save

    async def _record(self):
        phases.append(self.phase)
        return await real_save(self)

    with patch("app.api.v1.nodes.asyncssh") as ssh, \
         patch("app.services.node_ssh.verify_key", _verify_key_mock()), \
         patch("app.api.v1.nodes.asyncssh.scp", AsyncMock()), \
         patch("app.api.v1.nodes._VERIFY_SETTLE_SECONDS", 0), \
         patch.object(NodeProvisionTask, "save", _record):
        conn = ssh.connect.return_value
        # `running` because the verify phase reads this command's stdout as
        # the worker's container state; an empty stdout means "not present".
        conn.run = AsyncMock(return_value=type("R", (), {
            "exit_status": 0, "stdout": "running", "stderr": "",
        })())
        ssh.connect = AsyncMock(return_value=conn)
        await _provision_node("t-phases", req)

    # Consecutive duplicates collapsed: what matters is the order of distinct
    # phases the user sees, not how many times each was written.
    seen = [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]]
    assert seen == [
        "validate_ssh",
        "verify_docker",
        "setup_install",
        "write_env",
        "install_key",
        "pull_image",
        "start_worker",
        "verify",
        "enrolled",
    ]

    node = await Node.find_one(Node.node_id == "phasenode")
    if node:
        await node.delete()


# ---- _verify_node_operational (#402) ----


@asyncio_module_loop
async def test_verify_node_operational_passes_when_worker_is_running():
    from app.api.v1.nodes import _verify_node_operational

    conn = _FakeConn([_FakeResult(0, stdout="running\n")])

    with patch("app.api.v1.nodes._VERIFY_SETTLE_SECONDS", 0):
        assert await _verify_node_operational(conn, "~/.bioflow", "h") is None


@asyncio_module_loop
async def test_verify_node_operational_detects_a_container_that_exited():
    """`docker compose up -d` exits 0 for a container that immediately dies;
    only the state check afterwards catches it (#402)."""
    from app.api.v1.nodes import _verify_node_operational

    conn = _FakeConn([_FakeResult(0, stdout="exited\n")])

    with patch("app.api.v1.nodes._VERIFY_SETTLE_SECONDS", 0):
        problem = await _verify_node_operational(conn, "~/.bioflow", "10.0.0.5")

    assert problem is not None
    assert "started and then stopped" in problem
    assert "exited" in problem
    # Points at the machine that has the logs, not at the primary.
    assert "10.0.0.5" in problem


@asyncio_module_loop
async def test_verify_node_operational_detects_a_missing_container():
    """No output means `up -d` created nothing at all."""
    from app.api.v1.nodes import _verify_node_operational

    conn = _FakeConn([_FakeResult(0, stdout="")])

    with patch("app.api.v1.nodes._VERIFY_SETTLE_SECONDS", 0):
        problem = await _verify_node_operational(conn, "~/.bioflow", "10.0.0.6")

    assert problem is not None
    assert "not present" in problem


@asyncio_module_loop
async def test_verify_node_operational_fails_when_one_replica_is_down():
    """A node runs several worker replicas; all of them must be up."""
    from app.api.v1.nodes import _verify_node_operational

    conn = _FakeConn([_FakeResult(0, stdout="running\nexited\n")])

    with patch("app.api.v1.nodes._VERIFY_SETTLE_SECONDS", 0):
        problem = await _verify_node_operational(conn, "~/.bioflow", "h")

    assert problem is not None
    assert "started and then stopped" in problem


@asyncio_module_loop
async def test_verify_node_operational_reports_an_unusable_state_probe():
    from app.api.v1.nodes import _verify_node_operational

    conn = _FakeConn([_FakeResult(1, stderr="compose: command not found")])

    with patch("app.api.v1.nodes._VERIFY_SETTLE_SECONDS", 0):
        problem = await _verify_node_operational(conn, "~/.bioflow", "h")

    assert problem is not None
    assert "command not found" in problem


@asyncio_module_loop
async def test_provision_fails_when_the_worker_exits_after_starting():
    """End to end: a crash-looping worker fails provisioning at `verify`
    rather than reporting the node enrolled (#402)."""
    from app.api.v1.nodes import ProvisionRequest, _provision_node

    req = ProvisionRequest(
        host="10.0.0.13", username="ops", password="pw", node_name="crashnode",
    )

    async def _run(command, check=False):
        # Everything succeeds; the worker just isn't running afterwards.
        if "ps --format" in command:
            return _FakeResult(0, stdout="exited")
        return _FakeResult(0)

    with patch("app.api.v1.nodes.asyncssh") as ssh, \
         patch("app.services.node_ssh.verify_key", _verify_key_mock()), \
         patch("app.api.v1.nodes.asyncssh.scp", AsyncMock()), \
         patch("app.api.v1.nodes._VERIFY_SETTLE_SECONDS", 0):
        conn = ssh.connect.return_value
        conn.run = _run
        ssh.connect = AsyncMock(return_value=conn)
        await _provision_node("t-crash", req)

    task = await NodeProvisionTask.find_one(NodeProvisionTask.task_id == "t-crash")
    assert task.status == "failed"
    assert task.phase == "verify"
    assert "started and then stopped" in task.error
