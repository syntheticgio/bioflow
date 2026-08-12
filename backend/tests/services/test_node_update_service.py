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
