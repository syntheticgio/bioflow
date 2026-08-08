"""Reading workflow runs, and retrying their failed nodes.

Nothing exposed workflow runs before this: `/workflows/{id}/runs` launches one
and hands back an id, and there was no way to ask what happened next. That is
what the activity view needs.

The listing carries derived status rather than a stored field -- `derive_status`
is the single source, and a second stored copy would drift the first time a
write was lost.
"""

import pytest
from beanie import PydanticObjectId

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

GRAPH = {
    "name": "runs api graph",
    "description": "",
    "nodes": [
        {"node_id": "reads", "kind": "input", "label": "reads",
         "accepts": {"format": "fastq"}},
        {"node_id": "trim", "kind": "action", "node_type": "trim"},
        {"node_id": "qc", "kind": "action", "node_type": "qc"},
    ],
    "edges": [
        {"from_node": "reads", "from_port": "object",
         "to_node": "trim", "to_port": "reads"},
        {"from_node": "trim", "from_port": "trimmed",
         "to_node": "qc", "to_port": "reads"},
    ],
}


@pytest.fixture
def stub_launcher(monkeypatch):
    """Record launches instead of running fastp.

    Patches `_launch_node`, not the registry: `NodeTypeSpec` is a frozen
    dataclass holding the function object captured at import, so rebinding a
    service attribute never reaches it -- the `aligner_registry` trap CLAUDE.md
    records.
    """
    from app.models.job import Job, JobState
    from app.services import workflow_orchestrator as orch

    launched: list[str] = []

    async def fake(node_type, *, inputs, params, owner):
        launched.append(node_type)
        job = Job(type=f"run_{node_type}", owner=owner, state=JobState.PENDING)
        await job.insert()
        return job

    monkeypatch.setattr(orch, "_launch_node", fake)
    return launched


async def _launched_run(client, headers, stub_launcher) -> dict:
    created = await client.post("/api/v1/workflows", json=GRAPH, headers=headers)
    assert created.status_code == 201, created.text
    resp = await client.post(
        f"/api/v1/workflows/{created.json()['id']}/runs",
        json={
            "project_id": str(PydanticObjectId()),
            "label": "a run",
            "bindings": {"reads": str(PydanticObjectId())},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestListing:
    async def test_lists_my_workflow_runs(self, client, two_profiles, stub_launcher):
        run = await _launched_run(client, two_profiles["a_headers"], stub_launcher)

        resp = await client.get(
            "/api/v1/workflows/runs", headers=two_profiles["a_headers"]
        )

        assert resp.status_code == 200
        assert run["id"] in {r["id"] for r in resp.json()}

    async def test_does_not_list_another_owners_runs(
        self, client, two_profiles, stub_launcher
    ):
        run = await _launched_run(client, two_profiles["a_headers"], stub_launcher)

        resp = await client.get(
            "/api/v1/workflows/runs", headers=two_profiles["b_headers"]
        )

        assert run["id"] not in {r["id"] for r in resp.json()}

    async def test_carries_derived_status_and_node_counts(
        self, client, two_profiles, stub_launcher
    ):
        """The collapsed row needs both without expanding: a status, and enough
        to say '1 of 2' without a second request per run."""
        await _launched_run(client, two_profiles["a_headers"], stub_launcher)

        resp = await client.get(
            "/api/v1/workflows/runs", headers=two_profiles["a_headers"]
        )
        row = resp.json()[0]

        assert row["status"] in {"waiting", "running", "succeeded", "failed", "partial"}
        assert row["node_total"] >= 1
        assert "node_done" in row

    async def test_listing_does_not_query_per_run(
        self, client, two_profiles, stub_launcher, monkeypatch
    ):
        """Status is derived per run, so the obvious implementation is a loop
        over `status_of` -- one `_load` plus one `_rows` each, and a page that
        slows in proportion to history. This counts the node-row queries rather
        than trusting the shape of the code: three runs must still be one.

        Asserting the count is the point. A test that only checked the response
        would pass against the slow implementation, which is exactly how this
        kind of regression survives review.
        """
        from app.services import workflow_orchestrator as orch

        for _ in range(3):
            await _launched_run(client, two_profiles["a_headers"], stub_launcher)

        calls = {"n": 0}
        original = orch._rows

        async def counting(workflow_run_id):
            calls["n"] += 1
            return await original(workflow_run_id)

        monkeypatch.setattr(orch, "_rows", counting)

        resp = await client.get(
            "/api/v1/workflows/runs", headers=two_profiles["a_headers"]
        )

        assert len(resp.json()) >= 3
        assert all(r["status"] for r in resp.json())
        assert calls["n"] == 0, "list_runs should batch, not call _rows per run"


class TestDetail:
    async def test_returns_a_row_per_node(self, client, two_profiles, stub_launcher):
        run = await _launched_run(client, two_profiles["a_headers"], stub_launcher)

        resp = await client.get(
            f"/api/v1/workflows/runs/{run['id']}", headers=two_profiles["a_headers"]
        )

        assert resp.status_code == 200
        nodes = resp.json()["nodes"]
        assert {n["node_id"] for n in nodes} == {"reads", "trim", "qc"}

    async def test_a_node_carries_its_jobs(self, client, two_profiles, stub_launcher):
        """Job-grain: the 13 node types that create no PipelineRun have jobs and
        nothing else, so a detail view keyed on runs would show them empty."""
        run = await _launched_run(client, two_profiles["a_headers"], stub_launcher)

        resp = await client.get(
            f"/api/v1/workflows/runs/{run['id']}", headers=two_profiles["a_headers"]
        )
        trim = next(n for n in resp.json()["nodes"] if n["node_id"] == "trim")

        assert trim["jobs"]
        assert trim["jobs"][0]["job_id"]
        assert trim["jobs"][0]["state"]

    async def test_input_nodes_are_marked_as_such(
        self, client, two_profiles, stub_launcher
    ):
        """An INPUT is SUCCEEDED from creation and never runs. Rendering it as a
        finished step would overstate what the workflow has done."""
        run = await _launched_run(client, two_profiles["a_headers"], stub_launcher)

        resp = await client.get(
            f"/api/v1/workflows/runs/{run['id']}", headers=two_profiles["a_headers"]
        )
        reads = next(n for n in resp.json()["nodes"] if n["node_id"] == "reads")

        assert reads["kind"] == "input"

    async def test_another_owners_run_is_not_found(
        self, client, two_profiles, stub_launcher
    ):
        """Meaningful only alongside `test_returns_a_row_per_node` above: an
        absent route 404s too, so on its own this passes before the endpoint
        exists. The positive twin is what proves the 404 is the route's answer
        rather than its absence."""
        run = await _launched_run(client, two_profiles["a_headers"], stub_launcher)

        resp = await client.get(
            f"/api/v1/workflows/runs/{run['id']}", headers=two_profiles["b_headers"]
        )

        assert resp.status_code == 404

    async def test_detail_is_not_swallowed_by_the_definition_id_route(
        self, client, two_profiles
    ):
        """`/workflows/runs` shares a prefix with `/workflows/{definition_id}`.
        Declared the wrong way round, "runs" parses as a definition id."""
        resp = await client.get(
            "/api/v1/workflows/runs", headers=two_profiles["a_headers"]
        )
        assert resp.status_code == 200


class TestRetry:
    async def test_retrying_a_failed_node_starts_a_new_attempt(
        self, client, two_profiles, stub_launcher
    ):
        from app.models.workflow import NodeRunState, WorkflowNodeRun

        run = await _launched_run(client, two_profiles["a_headers"], stub_launcher)
        row = await WorkflowNodeRun.find_one(
            WorkflowNodeRun.workflow_run_id == PydanticObjectId(run["id"]),
            WorkflowNodeRun.node_id == "trim",
        )
        await row.set({WorkflowNodeRun.state: NodeRunState.FAILED})

        resp = await client.post(
            f"/api/v1/workflows/runs/{run['id']}/nodes/trim/retry",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 202, resp.text
        rows = await WorkflowNodeRun.find(
            WorkflowNodeRun.workflow_run_id == PydanticObjectId(run["id"]),
            WorkflowNodeRun.node_id == "trim",
        ).to_list()
        assert sorted(r.attempt for r in rows) == [1, 2]

    async def test_retry_all_failed_covers_every_failed_node(
        self, client, two_profiles, stub_launcher
    ):
        """§1.4: 'retry all failed' is the per-node operation applied to a set,
        not a different mechanism."""
        from app.models.workflow import NodeRunState, WorkflowNodeRun

        run = await _launched_run(client, two_profiles["a_headers"], stub_launcher)
        for node_id in ("trim", "qc"):
            row = await WorkflowNodeRun.find_one(
                WorkflowNodeRun.workflow_run_id == PydanticObjectId(run["id"]),
                WorkflowNodeRun.node_id == node_id,
            )
            await row.set({WorkflowNodeRun.state: NodeRunState.FAILED})

        resp = await client.post(
            f"/api/v1/workflows/runs/{run['id']}/retry-failed",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 202
        assert resp.json()["retried"] == 2

    async def test_another_owner_cannot_retry(
        self, client, two_profiles, stub_launcher
    ):
        """Paired with the retry tests above for the same reason the detail
        404 is paired: alone it would pass against a missing route."""
        run = await _launched_run(client, two_profiles["a_headers"], stub_launcher)

        resp = await client.post(
            f"/api/v1/workflows/runs/{run['id']}/nodes/trim/retry",
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 404


class TestCancel:
    async def test_cancelling_marks_unfinished_nodes(
        self, client, two_profiles, stub_launcher
    ):
        from app.models.workflow import NodeRunState, WorkflowNodeRun

        run = await _launched_run(client, two_profiles["a_headers"], stub_launcher)

        resp = await client.post(
            f"/api/v1/workflows/runs/{run['id']}/cancel",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 202
        rows = await WorkflowNodeRun.find(
            WorkflowNodeRun.workflow_run_id == PydanticObjectId(run["id"])
        ).to_list()
        assert any(r.state is NodeRunState.CANCELLED for r in rows)
