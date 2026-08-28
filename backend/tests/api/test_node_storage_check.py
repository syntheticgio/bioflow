"""Tests for the fleet-wide shared-storage sweep (#846).

The load-bearing tests here are the negative ones. Every test that asserts
"the sweep recorded what the probe said" passes against an implementation
that took the shortcut this child exists to refuse -- marking a node shared
because its jobs have been working. The tests that catch that are the ones
asserting the probe was *not called*, and that a node with a long successful
history whose probe disagrees is recorded `False` anyway.

Assertions are on the reloaded `Node` document, not on the sweep's return
value: a report that is right while the write never happened is a real
failure mode, and a return-value assertion misses it completely.
"""

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.errors import register_exception_handlers
from app.models.node import Node
from app.services import node_storage_probe as probe_mod
from app.services import storage_check_service as svc

# The autouse fixtures below touch Node for every test in this module.
pytestmark = pytest.mark.usefixtures("beanie_models")
asyncio_module_loop = pytest.mark.asyncio(loop_scope="module")


# ---- fixtures ----


@pytest.fixture(autouse=True)
def _routable_primary_hostname():
    """Give every test a primary address a node could actually reach.

    These tests run inside a container, where `_primary_hostname()` discovers
    the container's own Docker-network address and refuses it (#803). Any new
    test in this area needs this or the refusal surfaces as an unrelated
    failure.
    """
    from app.api.v1 import nodes as mod

    with patch.object(mod.settings, "primary_hostname", "192.168.1.50"):
        yield


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_nodes():
    """Each test starts with an empty nodes collection.

    `loop_scope="module"` must match `beanie_models`: a plain
    `@pytest.fixture` runs on a fresh per-function loop and the module-scoped
    Mongo client refuses to be used from it. Cleaning on entry rather than
    exit leaves a failed run's documents behind for inspection.
    """
    await Node.find_all().delete()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(pytest.importorskip("app.api.v1.nodes").router)
    register_exception_handlers(app)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---- helpers ----


async def _node(node_id="node-a", **overrides) -> Node:
    """An enrolled node that is probeable unless a test says otherwise."""
    fields = {
        "node_id": node_id,
        "hostname": f"{node_id}.local",
        "status": "active",
        "ssh_host": "192.168.1.60",
        "ssh_username": "bio",
        "ssh_key_enc": b"encrypted",
        "storage_location": "/data/scratch",
        "storage_shared": None,
        "storage_checked_at": None,
    }
    fields.update(overrides)
    node = Node(**fields)
    await node.insert()
    return node


def _connect_mock() -> AsyncMock:
    """A `connect_with_tofu` stand-in returning the (conn, host_key) pair.

    `conn.close` is a `MagicMock`, never an `AsyncMock`: the service does not
    await it, and an `AsyncMock` here leaves an un-awaited coroutine warning
    rather than a failure (#788).
    """
    conn = MagicMock()
    conn.close = MagicMock()
    return AsyncMock(return_value=(conn, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake"))


def _probe_mock(shared: bool) -> AsyncMock:
    return AsyncMock(
        return_value=probe_mod.ProbeResult(shared=shared, detail="probe detail")
    )


@contextmanager
def _patched(connect=None, probe=None):
    """Patch the three things the service reaches outside itself.

    One context manager rather than a tuple of them: the mocks have to be the
    *same objects* the test then asserts against, and building them per
    `with`-clause silently gives each clause its own.
    """
    connect = connect or _connect_mock()
    probe = probe or _probe_mock(True)
    with patch.object(svc, "connect_with_tofu", connect), patch.object(
        svc.node_storage_probe, "probe_shared_storage", probe
    ), patch.object(svc.crypto, "decrypt", MagicMock(return_value="PEM")):
        yield connect, probe


# ---- R7a: a node with no recorded path is never probed at a guessed one ----


@asyncio_module_loop
async def test_no_recorded_path_does_not_probe():
    """R7a. The assertion that matters is that the probe was *not called*.

    Asserting only that `storage_shared` is unchanged would also pass for an
    implementation that fell back to `ProvisionRequest`'s "/data/scratch"
    default, probed the wrong directory, and happened to write nothing --
    which is the exact bug this test exists to catch.
    """
    node = await _node(storage_location=None)
    connect, probe = _connect_mock(), _probe_mock(False)

    with _patched(connect, probe):
        outcomes = await svc.sweep_node_storage()

    probe.assert_not_called()
    connect.assert_not_called()
    assert [o.outcome for o in outcomes] == [svc.NO_RECORDED_PATH]

    reloaded = await Node.find_one(Node.node_id == node.node_id)
    assert reloaded.storage_shared is None
    assert reloaded.storage_checked_at is None
    assert reloaded.storage_location is None


@asyncio_module_loop
async def test_supplied_path_is_recorded_and_probed():
    """R7b. The operator's second run carries the paths the first asked for."""
    node = await _node(storage_location=None)
    connect, probe = _connect_mock(), _probe_mock(True)

    with _patched(connect, probe):
        outcomes = await svc.sweep_node_storage({"node-a": "/mnt/bioflow"})

    probe.assert_awaited_once()
    assert probe.await_args.args[1] == "/mnt/bioflow"
    assert [o.outcome for o in outcomes] == [svc.SHARED]

    reloaded = await Node.find_one(Node.node_id == node.node_id)
    assert reloaded.storage_location == "/mnt/bioflow"
    assert reloaded.storage_shared is True


# ---- R6: no node is blessed on evidence other than the probe ----


@asyncio_module_loop
async def test_working_node_whose_probe_disagrees_records_false():
    """R6, written as the negative it is.

    This node looks every bit like one that must be reading the primary's
    storage: enrolled, active, heartbeating, and it has been completing work.
    The probe says otherwise, and the probe is the only evidence that counts.
    An implementation that inferred `True` from any of the rest fails here.
    """
    node = await _node(
        last_seen=datetime.now(UTC),
        image_digest="sha256:abc",
        version="0.6.0",
    )
    connect, probe = _connect_mock(), _probe_mock(False)

    with _patched(connect, probe):
        outcomes = await svc.sweep_node_storage()

    assert [o.outcome for o in outcomes] == [svc.NOT_SHARED]

    reloaded = await Node.find_one(Node.node_id == node.node_id)
    assert reloaded.storage_shared is False


# ---- R2/R3: the probe's answer is what gets recorded ----


@asyncio_module_loop
async def test_matching_probe_records_shared():
    """R2."""
    node = await _node()
    with _patched(probe=_probe_mock(True)):
        await svc.sweep_node_storage()

    reloaded = await Node.find_one(Node.node_id == node.node_id)
    assert reloaded.storage_shared is True
    assert reloaded.storage_checked_at is not None


@asyncio_module_loop
async def test_not_shared_report_names_this_nodes_location():
    """R7. One remedy, naming the path *that* node uses."""
    await _node(storage_location="/srv/bioflow")
    connect, probe = _connect_mock(), _probe_mock(False)

    with _patched(connect, probe):
        outcomes = await svc.sweep_node_storage()

    assert "/srv/bioflow" in outcomes[0].detail


# ---- R4/R5: not probed means not written, even over an existing True ----


@asyncio_module_loop
async def test_self_enrolled_node_is_not_probeable_and_untouched():
    """R4. `storage_shared` is asserted unchanged from `True`, not just None.

    A blanket reset of every node's field at the top of the sweep would pass
    a `None`-only assertion and silently withdraw a node that had been
    correctly recorded as shared.
    """
    node = await _node(ssh_key_enc=None, storage_shared=True)
    connect, probe = _connect_mock(), _probe_mock(False)

    with _patched(connect, probe):
        outcomes = await svc.sweep_node_storage()

    connect.assert_not_called()
    assert [o.outcome for o in outcomes] == [svc.NOT_PROBEABLE]

    reloaded = await Node.find_one(Node.node_id == node.node_id)
    assert reloaded.storage_shared is True


@asyncio_module_loop
async def test_unreachable_node_is_untouched():
    """R5. An offline machine is not a verified negative."""
    checked = datetime(2026, 8, 1, tzinfo=UTC)
    node = await _node(storage_shared=True, storage_checked_at=checked)
    connect = AsyncMock(side_effect=asyncssh.Error(1, "connection refused"))

    with _patched(connect):
        outcomes = await svc.sweep_node_storage()

    assert [o.outcome for o in outcomes] == [svc.UNREACHABLE]

    reloaded = await Node.find_one(Node.node_id == node.node_id)
    assert reloaded.storage_shared is True
    assert reloaded.storage_checked_at == checked


@asyncio_module_loop
async def test_self_enrolled_and_unreachable_report_differently():
    """R4 vs R5. The remedies differ, so the report must too.

    An offline node needs powering on and a re-run; a self-enrolled one can
    never be probed this way and needs re-provisioning.
    """
    await _node("node-offline")
    await _node("node-self", ssh_key_enc=None)
    connect = AsyncMock(side_effect=asyncssh.Error(1, "connection refused"))

    with _patched(connect):
        outcomes = await svc.sweep_node_storage()

    by_id = {o.node_id: o for o in outcomes}
    assert by_id["node-offline"].outcome == svc.UNREACHABLE
    assert by_id["node-self"].outcome == svc.NOT_PROBEABLE
    assert by_id["node-offline"].detail != by_id["node-self"].detail
    assert "provision" in by_id["node-self"].detail.lower()


@asyncio_module_loop
async def test_failed_probe_is_unreachable_not_false():
    """A probe that could not be carried out has learned nothing.

    `StorageProbeError` is deliberately distinct from `ProbeResult(False)` in
    #844, and that distinction has to survive the sweep -- recording `False`
    for a primary that could not write its own sentinel would turn an
    infrastructure fault into a verified negative.
    """
    node = await _node()
    probe = AsyncMock(side_effect=probe_mod.StorageProbeError("timed out."))

    with _patched(probe=probe):
        outcomes = await svc.sweep_node_storage()

    assert [o.outcome for o in outcomes] == [svc.UNREACHABLE]

    reloaded = await Node.find_one(Node.node_id == node.node_id)
    assert reloaded.storage_shared is None


# ---- R8/R9/R10: idempotent, re-runnable, honest about when it asked ----


@asyncio_module_loop
async def test_running_twice_leaves_the_same_record():
    """R8."""
    node = await _node()

    with _patched(probe=_probe_mock(True)):
        await svc.sweep_node_storage()
        first = (await Node.find_one(Node.node_id == node.node_id)).storage_shared
        await svc.sweep_node_storage()
        second = (await Node.find_one(Node.node_id == node.node_id)).storage_shared

    assert first is True and second is True


@asyncio_module_loop
async def test_second_run_records_the_new_answer():
    """R9. Not scoped to unrecorded nodes -- a share can be unmounted."""
    node = await _node()
    probe = AsyncMock(
        side_effect=[
            probe_mod.ProbeResult(shared=True, detail="matched"),
            probe_mod.ProbeResult(shared=False, detail="gone"),
        ]
    )

    with _patched(probe=probe):
        await svc.sweep_node_storage()
        assert (await Node.find_one(Node.node_id == node.node_id)).storage_shared is True
        await svc.sweep_node_storage()

    reloaded = await Node.find_one(Node.node_id == node.node_id)
    assert reloaded.storage_shared is False


@asyncio_module_loop
async def test_checked_at_moves_only_for_a_probe_that_ran():
    """R10. "When did we last actually ask?" must stay answerable.

    Which node fails is keyed on its host rather than on call order, so the
    test does not quietly depend on the order `find_all()` happens to return.
    """
    probed = await _node("node-probed", ssh_host="10.0.0.1")
    offline = await _node("node-offline", ssh_host="10.0.0.2")

    conn = MagicMock()
    conn.close = MagicMock()

    async def connect_side_effect(host, *a, **kw):
        if host == "10.0.0.2":
            raise asyncssh.Error(1, "connection refused")
        return conn, "hostkey"

    with _patched(connect=AsyncMock(side_effect=connect_side_effect)):
        await svc.sweep_node_storage()

    assert (
        await Node.find_one(Node.node_id == probed.node_id)
    ).storage_checked_at is not None
    assert (
        await Node.find_one(Node.node_id == offline.node_id)
    ).storage_checked_at is None


# ---- the mixed sweep: one node of every outcome, in one run ----


@asyncio_module_loop
async def test_mixed_sweep_reports_every_outcome():
    """All five outcomes in a single run.

    Two independent failures show up only here: one node's SSH failure
    aborting the whole sweep, and a `finally` that runs at sweep scope rather
    than per node. Neither is visible in a single-node test.

    Each node's fate is keyed on its host or path, not on the order
    `find_all()` returns them in.
    """
    await _node("n-shared", ssh_host="10.0.0.1")
    await _node("n-not-shared", ssh_host="10.0.0.2")
    await _node("n-unreachable", ssh_host="10.0.0.3")
    await _node("n-probe-failed", ssh_host="10.0.0.4")
    await _node("n-self", ssh_key_enc=None)
    await _node("n-no-path", storage_location=None)

    conns: list[MagicMock] = []

    async def connect_side_effect(host, *a, **kw):
        if host == "10.0.0.3":
            raise asyncssh.Error(1, "connection refused")
        conn = MagicMock()
        conn.close = MagicMock()
        conn.host = host
        conns.append(conn)
        return conn, "hostkey"

    async def probe_side_effect(conn, location):
        if conn.host == "10.0.0.1":
            return probe_mod.ProbeResult(shared=True, detail="matched")
        if conn.host == "10.0.0.2":
            return probe_mod.ProbeResult(shared=False, detail="missing")
        raise probe_mod.StorageProbeError("timed out.")

    with _patched(
        connect=AsyncMock(side_effect=connect_side_effect),
        probe=AsyncMock(side_effect=probe_side_effect),
    ):
        outcomes = await svc.sweep_node_storage()

    by_id = {o.node_id: o.outcome for o in outcomes}
    assert by_id == {
        "n-shared": svc.SHARED,
        "n-not-shared": svc.NOT_SHARED,
        "n-unreachable": svc.UNREACHABLE,
        # A probe that could not be carried out is not a `False` either.
        "n-probe-failed": svc.UNREACHABLE,
        "n-self": svc.NOT_PROBEABLE,
        "n-no-path": svc.NO_RECORDED_PATH,
    }
    # Every connection opened was closed, per node rather than per sweep.
    assert len(conns) == 3
    assert all(c.close.call_count == 1 for c in conns)


# ---- the endpoint ----


@asyncio_module_loop
async def test_endpoint_returns_every_node(client):
    """The route is a thin wrapper, but "thin" still has to be wired up."""
    await _node("n-a")
    await _node("n-b", storage_location=None)

    with _patched(probe=_probe_mock(True)):
        async with client as ac:
            resp = await ac.post("/nodes/storage-check")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["checked"] == 1
    assert {n["node_id"] for n in body["nodes"]} == {"n-a", "n-b"}


@asyncio_module_loop
async def test_endpoint_passes_supplied_paths_through(client):
    """The Q2a map has to reach the service, or the second run is the first."""
    await _node("n-a", storage_location=None)
    probe = _probe_mock(True)

    with _patched(probe=probe):
        async with client as ac:
            resp = await ac.post(
                "/nodes/storage-check",
                json={"storage_locations": {"n-a": "/mnt/shared"}},
            )

    assert resp.status_code == 200
    assert probe.await_args.args[1] == "/mnt/shared"
    reloaded = await Node.find_one(Node.node_id == "n-a")
    assert reloaded.storage_location == "/mnt/shared"
