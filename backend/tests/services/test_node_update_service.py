"""The update executor: pull, drain, restart, verify."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.models.node import Node
from app.models.node_update import NodeUpdateTask
from app.services import node_update_service as svc

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean():
    """conftest.py drops collections at session start, not between tests --
    these tests each insert a Node and a task under fixed ids."""
    yield
    await NodeUpdateTask.find_all().delete()
    await Node.find_all().delete()


def _conn(pull_status: int = 0, up_status: int = 0):
    conn = MagicMock()

    async def run(command, **kwargs):
        result = MagicMock()
        result.stdout = ""
        result.stderr = "boom" if pull_status else ""
        if " pull " in command or command.endswith(" pull"):
            result.exit_status = pull_status
        elif "up -d" in command:
            result.exit_status = up_status
        else:
            result.exit_status = 0
        return result

    conn.run = AsyncMock(side_effect=run)
    conn.close = MagicMock()
    return conn


async def _node(node_id="un1"):
    node = Node(
        node_id=node_id, ssh_host="10.0.0.7", ssh_username="ops",
        ssh_key_enc=b"enc", image_digest="sha256:old",
    )
    await node.insert()
    return node


async def test_failed_pull_leaves_the_node_running():
    """Pull before stop: a failed download must cost nothing (NU-23, NU-24)."""
    node = await _node("un-pull")
    task = NodeUpdateTask(task_id="u1", node_id="un-pull")
    await task.insert()
    conn = _conn(pull_status=1)

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)):
        await svc.run_update("u1", node, drain=False)

    done = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "u1")
    assert done.status == "failed"
    assert done.phase == "pull_image"

    commands = " ".join(str(c) for c in conn.run.call_args_list)
    assert "up -d" not in commands  # nothing was restarted
    await node.delete()
    await done.delete()


async def test_success_requires_the_new_digest_to_be_reported():
    """NU-25: exit 0 from compose is also what a crash-looping container returns."""
    node = await _node("un-ok")
    task = NodeUpdateTask(task_id="u2", node_id="un-ok")
    await task.insert()
    conn = _conn()

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)), \
         patch.object(svc, "_await_digest", AsyncMock(return_value="sha256:new")):
        await svc.run_update("u2", node, drain=False)

    done = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "u2")
    assert done.status == "success"
    assert done.to_digest == "sha256:new"
    await node.delete()
    await done.delete()


async def test_worker_that_never_reports_fails_the_update():
    node = await _node("un-crash")
    task = NodeUpdateTask(task_id="u3", node_id="un-crash")
    await task.insert()
    conn = _conn()

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)), \
         patch.object(svc, "_await_digest", AsyncMock(return_value=None)):
        await svc.run_update("u3", node, drain=False)

    done = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "u3")
    assert done.status == "failed"
    assert done.phase == "verify"
    assert "120" in done.error or "did not" in done.error.lower()
    await node.delete()
    await done.delete()


async def test_drain_stops_the_worker_before_restarting():
    node = await _node("un-drain")
    task = NodeUpdateTask(task_id="u4", node_id="un-drain")
    await task.insert()
    conn = _conn()

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)), \
         patch.object(svc, "_await_drained", AsyncMock(return_value=True)), \
         patch.object(svc, "_await_digest", AsyncMock(return_value="sha256:new")):
        await svc.run_update("u4", node, drain=True)

    commands = [str(c) for c in conn.run.call_args_list]
    joined = " ".join(commands)
    assert "stop" in joined
    # The pull happens before the stop, so the download overlaps with jobs
    # finishing rather than running after them.
    assert joined.index(" pull ") < joined.index("stop")


async def test_restart_starts_only_the_worker():
    """A launcher-provisioned node has the *full* stack compose file at
    ~/.bioflow/docker-compose.yml, so a bare `up -d` would start mongo,
    redis, api and web on a compute node -- a second database the worker is
    not even pointed at. The restart must stay scoped to the worker.
    """
    node = await _node("un-scope")
    task = NodeUpdateTask(task_id="u6", node_id="un-scope")
    await task.insert()
    conn = _conn()

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)), \
         patch.object(svc, "_await_digest", AsyncMock(return_value="sha256:new")):
        await svc.run_update("u6", node, drain=False)

    up_commands = [
        str(c) for c in conn.run.call_args_list if "up -d" in str(c)
    ]
    assert up_commands, "expected a restart command"
    for command in up_commands:
        assert "--no-deps" in command
        assert "worker" in command

    await node.delete()
    await (await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "u6")).delete()


async def test_unreachable_machine_reports_connect_failure():
    """NU-20: a node whose worker is down is still attempted; only a failed
    SSH connection reports the machine unreachable."""
    import asyncssh

    node = await _node("un-down")
    task = NodeUpdateTask(task_id="u5", node_id="un-down")
    await task.insert()

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(side_effect=asyncssh.Error(1, "refused"))):
        await svc.run_update("u5", node, drain=False)

    done = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "u5")
    assert done.status == "failed"
    assert done.phase == "connect"
    await node.delete()
    await done.delete()


async def test_task_lookup_failure_returns_without_raising():
    """A transient Mongo error on the initial find_one must not escape
    run_update -- it's a fire-and-forget background task with no caller to
    catch it."""
    node = await _node("un-lookup-fail")

    with patch.object(
        NodeUpdateTask, "find_one", AsyncMock(side_effect=RuntimeError("mongo down"))
    ):
        await svc.run_update("u6", node, drain=False)  # must not raise

    await node.delete()


async def test_fail_save_failure_is_swallowed_not_reraised():
    """If _fail's own task.save() raises (Mongo unreachable mid-update), that
    must not propagate out of run_update's outer exception handler."""
    node = await _node("un-save-fail")
    task = NodeUpdateTask(task_id="u7", node_id="un-save-fail")
    await task.insert()
    conn = _conn(pull_status=1)

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)), \
         patch.object(NodeUpdateTask, "save", AsyncMock(side_effect=RuntimeError("mongo down"))):
        await svc.run_update("u7", node, drain=False)  # must not raise

    await node.delete()
    await task.delete()


# ---------------------------------------------------------------------------
# Repairing a stale .env (#822)
# ---------------------------------------------------------------------------


async def test_refresh_env_rewrites_stale_connection_urls():
    """A node provisioned before #803 carries a Docker bridge address in its
    .env. Updating it must repoint the three connection URLs at the primary."""
    stale = (
        "NODE_TYPE=compute\n"
        "MONGO_URL=mongodb://172.19.0.6:27017/biopipe?replicaSet=rs0&directConnection=true\n"
        "REDIS_URL=redis://172.19.0.6:6379/0\n"
        "WORKER_NODE_ID=ai-gen-desktop\n"
        "PRIMARY_API_URL=http://172.19.0.6:8000\n"
        "BIOINFO_HOME=/mnt/data\n"
        "BIOINFO_REGISTER_ROOTS=/mnt/data\n"
        "BIOFLOW_TAG=latest\n"
        "WORKER_REPLICAS=2\n"
    )

    fixed = svc._refresh_env_urls(stale, "192.168.1.249")

    assert (
        "MONGO_URL=mongodb://192.168.1.249:27017/biopipe"
        "?replicaSet=rs0&directConnection=true" in fixed
    )
    assert "REDIS_URL=redis://192.168.1.249:6379/0" in fixed
    assert "PRIMARY_API_URL=http://192.168.1.249:8000" in fixed
    assert "172.19.0.6" not in fixed


async def test_refresh_env_preserves_every_other_setting():
    """Only the connection URLs are the primary's to decide. The node's name,
    storage location, pinned tag and replica count must survive untouched --
    the update path does not know them and must not invent them."""
    stale = (
        "NODE_TYPE=compute\n"
        "MONGO_URL=mongodb://172.19.0.6:27017/biopipe\n"
        "REDIS_URL=redis://172.19.0.6:6379/0\n"
        "WORKER_NODE_ID=ai-gen-desktop\n"
        "PRIMARY_API_URL=http://172.19.0.6:8000\n"
        "BIOINFO_HOME=/mnt/55e23b05\n"
        "BIOINFO_REGISTER_ROOTS=/mnt/55e23b05\n"
        "BIOFLOW_TAG=v0.5.9\n"
        "WORKER_REPLICAS=7\n"
        "WORKER_MAX_CONCURRENT=12\n"
    )

    fixed = svc._refresh_env_urls(stale, "192.168.1.249")

    assert "WORKER_NODE_ID=ai-gen-desktop" in fixed
    assert "BIOINFO_HOME=/mnt/55e23b05" in fixed
    assert "BIOINFO_REGISTER_ROOTS=/mnt/55e23b05" in fixed
    assert "BIOFLOW_TAG=v0.5.9" in fixed  # a pinned tag is not bumped
    assert "WORKER_REPLICAS=7" in fixed
    assert "WORKER_MAX_CONCURRENT=12" in fixed
    assert "NODE_TYPE=compute" in fixed


async def test_refresh_env_leaves_a_correct_env_byte_identical():
    """A healthy node must not be rewritten at all -- no needless churn, and
    no chance of mangling a hand-edited file."""
    good = (
        "NODE_TYPE=compute\n"
        "MONGO_URL=mongodb://192.168.1.249:27017/biopipe?replicaSet=rs0\n"
        "REDIS_URL=redis://192.168.1.249:6379/0\n"
        "PRIMARY_API_URL=http://192.168.1.249:8000\n"
        "WORKER_NODE_ID=n1\n"
    )

    assert svc._refresh_env_urls(good, "192.168.1.249") == good


async def test_refresh_env_keeps_credentials_in_the_url():
    """Rewriting the host must not drop a password embedded in the URL."""
    stale = (
        "MONGO_URL=mongodb://user:pw@172.19.0.6:27017/biopipe\n"
        "REDIS_URL=redis://:secret@172.19.0.6:6379/0\n"
        "PRIMARY_API_URL=http://172.19.0.6:8000\n"
    )

    fixed = svc._refresh_env_urls(stale, "10.0.0.4")

    assert "MONGO_URL=mongodb://user:pw@10.0.0.4:27017/biopipe" in fixed
    assert "REDIS_URL=redis://:secret@10.0.0.4:6379/0" in fixed


async def test_update_repairs_a_stale_env_before_restarting():
    """The regression #822 is about: an unreachable MONGO_URL survives every
    update, so the worker crash-loops forever. The update must rewrite the
    node's .env before `up -d`, or the restart just reuses the bad address."""
    node = await _node("un-stale-env")
    task = NodeUpdateTask(task_id="u8", node_id="un-stale-env")
    await task.insert()

    stale = (
        "NODE_TYPE=compute\n"
        "MONGO_URL=mongodb://172.19.0.6:27017/biopipe?replicaSet=rs0\n"
        "REDIS_URL=redis://172.19.0.6:6379/0\n"
        "WORKER_NODE_ID=ai-gen-desktop\n"
        "PRIMARY_API_URL=http://172.19.0.6:8000\n"
    )

    conn = MagicMock()
    written: list[str] = []

    async def run(command, **kwargs):
        result = MagicMock()
        result.stdout = stale if "cat " in command and ">" not in command else ""
        result.stderr = ""
        result.exit_status = 0
        if "HERMESEOF" in command:
            written.append(command)
        return result

    conn.run = AsyncMock(side_effect=run)
    conn.close = MagicMock()

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)), \
         patch("app.api.v1.nodes._primary_hostname", return_value="192.168.1.249"), \
         patch.object(svc, "_await_digest", AsyncMock(return_value="sha256:new")):
        await svc.run_update("u8", node, drain=False)

    assert written, "the update never rewrote the node's .env"
    assert "192.168.1.249" in written[0]
    assert "172.19.0.6" not in written[0]

    # ...and the repair lands before the worker is restarted, not after.
    commands = [str(c) for c in conn.run.call_args_list]
    env_at = next(i for i, c in enumerate(commands) if "HERMESEOF" in c)
    up_at = next(i for i, c in enumerate(commands) if "up -d" in c)
    assert env_at < up_at

    await node.delete()
    await task.delete()


async def test_update_survives_an_unroutable_primary_address():
    """If the primary cannot work out its own address, that is no reason to
    abandon the update -- the image pull is still worth doing, and the node's
    existing .env may well be fine. Repair is best-effort."""
    from app.api.v1.nodes import UnroutablePrimaryHost

    node = await _node("un-no-host")
    task = NodeUpdateTask(task_id="u9", node_id="un-no-host")
    await task.insert()
    conn = _conn()

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)), \
         patch(
             "app.api.v1.nodes._primary_hostname",
             side_effect=UnroutablePrimaryHost("no idea"),
         ), \
         patch.object(svc, "_await_digest", AsyncMock(return_value="sha256:new")):
        await svc.run_update("u9", node, drain=False)

    done = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "u9")
    assert done.status == "success"

    await node.delete()
    await done.delete()
