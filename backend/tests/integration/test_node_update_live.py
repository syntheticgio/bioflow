"""A real node update against a real sshd and a real Docker daemon.

Issue #474's checks 4 and 5, for the parts that do not need a second machine.

`tests/services/test_node_update_service.py` mocks `asyncssh` wholesale: its
`conn.run` returns whatever exit status the test asked for, so it proves which
commands we build and nothing about what a Docker daemon does with them. That
is the same gap #473 closed for the SSH half, and it closed it by finding a
real bug -- TOFU pinning had never worked against a real server while every
mocked test stayed green.

The condition under test here is the one the whole update feature exists for:
the compose `up -d --no-deps worker` call exits 0 for a container that starts
and immediately dies. A naive implementation reports a successful update of a
node that is actually down. `run_update` must therefore refuse to trust the
restart phase's exit status and fail at `verify` instead, and only a real
daemon can demonstrate that the exit status really is 0. The sidecar's compose
file runs `alpine:3.20 /bin/true` as its worker for exactly this reason -- it
pulls, it starts, it exits 0, and nothing re-enrolls.

What is still fiction: the sidecar reaches the *host's* Docker daemon through
a bind-mounted socket, so "the node's daemon" is this machine's daemon. The
SSH transport, the image pull, the compose parsing, the container lifecycle,
and the exit statuses are all real; the isolation is not. For the property
being asserted -- how run_update reads an exit status -- that is faithful.

What cannot be tested here at all, and keeps #474 open: a real worker
re-enrolling with the primary and reporting its new image digest. The success
case below writes that digest itself, so it asserts that run_update advances
when a digest appears, not that a real worker makes one appear.

Skipped unless BIOFLOW_TEST_NODE is set:
`run-worktree-tests.sh --with-node` starts and provisions the sidecar.
"""

import asyncio
import os

import asyncssh
import pytest
import pytest_asyncio

from app.models.node import Node
from app.models.node_update import NodeUpdateTask
from app.services import node_ssh
from app.services import node_update_service as svc

SSHD_HOST = os.environ.get("BIOFLOW_TEST_SSHD_HOST")
SSHD_PORT = int(os.environ.get("BIOFLOW_TEST_SSHD_PORT", "2222"))
SSHD_USER = os.environ.get("BIOFLOW_TEST_SSHD_USER", "bioflow")
SSHD_PASSWORD = os.environ.get("BIOFLOW_TEST_SSHD_PASSWORD", "testpw")

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("BIOFLOW_TEST_NODE"),
        reason="Set BIOFLOW_TEST_NODE (run-worktree-tests.sh --with-node)",
    ),
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

OLD_DIGEST = "sha256:oldoldold"
NEW_DIGEST = "sha256:newnewnew"


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean():
    """conftest.py drops collections at session start, not between tests."""
    yield
    await NodeUpdateTask.find_all().delete()
    await Node.find_all().delete()


@pytest_asyncio.fixture(loop_scope="module")
async def enrolled_node(monkeypatch):
    """A Node whose managed key is really installed on the sidecar's sshd.

    Built through `node_ssh` rather than by hand: the point of an integration
    test is that the key run_update authenticates with is the key provisioning
    would have installed.
    """
    private_pem, public_line = node_ssh.generate_keypair("update-live")

    async with await asyncssh.connect(
        SSHD_HOST,
        port=SSHD_PORT,
        username=SSHD_USER,
        password=SSHD_PASSWORD,
        known_hosts=None,
    ) as conn:
        await node_ssh.install_public_key(conn, public_line)

    # crypto.decrypt is the only seam left mocked. Encryption is covered by
    # its own tests, and threading a real key through the settings collection
    # would test key management rather than the update path.
    monkeypatch.setattr("app.services.ai.crypto.decrypt", lambda _blob: private_pem)

    node = Node(
        node_id="live-update-node",
        ssh_host=SSHD_HOST,
        ssh_port=SSHD_PORT,
        ssh_username=SSHD_USER,
        ssh_key_enc=b"unused-decrypt-is-patched",
        image_digest=OLD_DIGEST,
    )
    await node.insert()
    return node


async def test_update_fails_at_verify_when_the_worker_crash_loops(
    enrolled_node, monkeypatch
):
    """#474's check 5, and the reason this feature exists.

    The sidecar's worker exits 0 the instant it starts. The compose `up -d`
    call reports success for it, so a run_update that trusted the restart
    phase would mark the node updated while it is down. The verify phase must
    notice that no worker ever reported a new digest.

    This is the direction that fails when the seam breaks, per CLAUDE.md:
    asserting only the success path would pass against an implementation that
    never checks anything.
    """
    # The real 120s wait, shortened. The timeout's *value* is not under test;
    # that it is enforced at all is.
    monkeypatch.setattr(svc, "_VERIFY_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(svc, "_POLL_INTERVAL_SECONDS", 1)

    task = NodeUpdateTask(task_id="live-fail", node_id=enrolled_node.node_id)
    await task.insert()

    await svc.run_update("live-fail", enrolled_node, drain=False)

    done = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "live-fail")
    assert done.status == "failed", (
        f"a crash-looping worker was reported as {done.status}: {done.message}"
    )
    assert done.phase == "verify", (
        f"failed at {done.phase}, so the restart phase's exit status was "
        "trusted rather than the worker actually reporting in"
    )
    # The message has to be actionable: it names where to look.
    assert "logs worker" in done.error
    assert done.to_digest is None

    # And the node's recorded digest was not advanced by a failed update.
    node = await Node.find_one(Node.node_id == enrolled_node.node_id)
    assert node.image_digest == OLD_DIGEST


async def test_update_succeeds_once_a_worker_reports_a_new_digest(
    enrolled_node, monkeypatch
):
    """#474's check 4's backend half: the success path through all phases.

    The re-enrollment is simulated -- a background task writes the new digest
    onto the Node document while `_await_digest` polls for it, standing in for
    a real worker booting on the node and enrolling with the primary. Enrolment
    itself is covered by tests/api/test_node_enrollment.py; what this asserts
    is that run_update waits for it and transitions on it.

    Everything before the digest write is real: the SSH connection, the image
    pull, and the compose restart all run against the sidecar.
    """
    monkeypatch.setattr(svc, "_VERIFY_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(svc, "_POLL_INTERVAL_SECONDS", 1)

    task = NodeUpdateTask(task_id="live-ok", node_id=enrolled_node.node_id)
    await task.insert()

    async def _reenroll_after_restart():
        """Write the new digest once the update has reached `verify`.

        Keyed off the task's own phase rather than a fixed sleep: a fixed
        delay would either race the pull on a slow machine or make every run
        wait for the slowest one.
        """
        for _ in range(240):
            current = await NodeUpdateTask.find_one(
                NodeUpdateTask.task_id == "live-ok"
            )
            if current and current.phase == "verify":
                break
            await asyncio.sleep(0.25)
        node = await Node.find_one(Node.node_id == enrolled_node.node_id)
        node.image_digest = NEW_DIGEST
        await node.save()

    reenroll = asyncio.create_task(_reenroll_after_restart())
    try:
        await svc.run_update("live-ok", enrolled_node, drain=False)
    finally:
        reenroll.cancel()

    done = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "live-ok")
    assert done.status == "success", f"update failed: {done.error}"
    assert done.phase == "done"
    assert done.to_digest == NEW_DIGEST
    assert done.pct == 100
    assert done.finished_at is not None
