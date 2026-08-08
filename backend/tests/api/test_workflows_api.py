"""Workflow definition and run endpoints.

Definitions are user data, so every route here is owner-scoped and the
isolation assertions are B asking for A's row -- the reason `two_profiles`
exists. This matters more than usual for this router: `workflow_service`'s
`update_definition` takes no owner argument at all, so the scoping has to be
established here or not at all.

The validation cases are deliberately thin: `validate_definition` has its own
suite in tests/services/test_workflow_validation.py. What is tested here is
that its errors reach the client as a 422 carrying the full list, rather than
a 500 or a single message.
"""

import pytest
from beanie import PydanticObjectId

# Module-scoped database and loop, matching tests/api/test_profiles.py: the
# `two_profiles` fixture instantiates Documents, which Beanie refuses before
# init_beanie.
pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

TRIM_GRAPH = {
    "name": "trim then align",
    "description": "",
    "nodes": [
        {"node_id": "reads", "kind": "input", "label": "reads",
         "accepts": {"format": "fastq"}},
        {"node_id": "reference", "kind": "input", "label": "reference",
         "accepts": {"format": "fasta", "role": "reference"}},
        {"node_id": "trim", "kind": "action", "node_type": "trim"},
        {"node_id": "align", "kind": "action", "node_type": "align"},
    ],
    "edges": [
        {"from_node": "reads", "from_port": "object",
         "to_node": "trim", "to_port": "reads"},
        {"from_node": "trim", "from_port": "trimmed",
         "to_node": "align", "to_port": "reads"},
        {"from_node": "reference", "from_port": "object",
         "to_node": "align", "to_port": "reference"},
    ],
}


async def _create(client, headers, graph=None) -> dict:
    resp = await client.post(
        "/api/v1/workflows", json=graph or TRIM_GRAPH, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    async def test_creates_a_valid_definition(self, client, two_profiles):
        body = await _create(client, two_profiles["a_headers"])
        assert body["name"] == "trim then align"
        assert body["version"] == 1

    async def test_an_invalid_graph_is_422_with_every_error(self, client, two_profiles):
        """The full list, not the first problem: the canvas marks every bad
        wire at once, and a builder that reports one error per save is one you
        fix by trial and error. `InvalidGraph` carries `.errors` for exactly
        this."""
        broken = {
            "name": "broken",
            "description": "",
            # `align` needs both `reads` and `reference`; neither is wired, and
            # the node type does not exist either.
            "nodes": [{"node_id": "a", "kind": "action", "node_type": "nope"},
                      {"node_id": "b", "kind": "action", "node_type": "align"}],
            "edges": [],
        }
        resp = await client.post(
            "/api/v1/workflows", json=broken, headers=two_profiles["a_headers"]
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "invalid_graph"
        # Under `details`, because that is what `AppError.to_dict` serializes.
        errors = body["details"]["errors"]
        assert len(errors) >= 2
        # Each one names what to mark, not just that something is wrong.
        assert {e["code"] for e in errors} >= {"unknown_node_type", "missing_required_input"}
        assert any(e["node_id"] for e in errors)

    async def test_a_cycle_is_rejected(self, client, two_profiles):
        cyclic = {
            "name": "loop",
            "description": "",
            "nodes": [
                {"node_id": "a", "kind": "action", "node_type": "trim"},
                {"node_id": "b", "kind": "action", "node_type": "trim"},
            ],
            "edges": [
                {"from_node": "a", "from_port": "trimmed",
                 "to_node": "b", "to_port": "reads"},
                {"from_node": "b", "from_port": "trimmed",
                 "to_node": "a", "to_port": "reads"},
            ],
        }
        resp = await client.post(
            "/api/v1/workflows", json=cyclic, headers=two_profiles["a_headers"]
        )
        assert resp.status_code == 422


class TestListAndGet:
    async def test_lists_only_my_definitions(self, client, two_profiles):
        await _create(client, two_profiles["a_headers"])

        resp = await client.get("/api/v1/workflows", headers=two_profiles["b_headers"])

        assert resp.status_code == 200
        assert resp.json() == []

    async def test_another_owners_definition_is_not_found(self, client, two_profiles):
        """404 rather than 403, matching `get_project`/`get_object`: the whole
        codebase denies the same way, and saying 'forbidden' confirms the row
        exists."""
        mine = await _create(client, two_profiles["a_headers"])

        resp = await client.get(
            f"/api/v1/workflows/{mine['id']}", headers=two_profiles["b_headers"]
        )

        assert resp.status_code == 404

    async def test_gets_my_own_definition(self, client, two_profiles):
        mine = await _create(client, two_profiles["a_headers"])

        resp = await client.get(
            f"/api/v1/workflows/{mine['id']}", headers=two_profiles["a_headers"]
        )

        assert resp.status_code == 200
        assert resp.json()["id"] == mine["id"]


class TestUpdate:
    async def test_bumps_the_version(self, client, two_profiles):
        mine = await _create(client, two_profiles["a_headers"])
        renamed = {**TRIM_GRAPH, "name": "renamed"}

        resp = await client.put(
            f"/api/v1/workflows/{mine['id']}",
            json=renamed,
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200
        assert resp.json()["version"] == 2

    async def test_another_owner_cannot_update(self, client, two_profiles):
        """`workflow_service.update_definition` takes no owner and checks
        none, so without a check in the router any profile could rewrite any
        other's saved graph."""
        mine = await _create(client, two_profiles["a_headers"])

        resp = await client.put(
            f"/api/v1/workflows/{mine['id']}",
            json={**TRIM_GRAPH, "name": "stolen"},
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 404

        # And the graph is untouched.
        after = await client.get(
            f"/api/v1/workflows/{mine['id']}", headers=two_profiles["a_headers"]
        )
        assert after.json()["name"] == "trim then align"

    async def test_an_invalid_update_leaves_the_stored_graph_alone(
        self, client, two_profiles
    ):
        """Validate before persist. A rejected edit that had already mutated
        the document would leave an unrunnable graph saved."""
        mine = await _create(client, two_profiles["a_headers"])

        await client.put(
            f"/api/v1/workflows/{mine['id']}",
            json={"name": "x", "description": "", "nodes": [
                {"node_id": "a", "kind": "action", "node_type": "align"}], "edges": []},
            headers=two_profiles["a_headers"],
        )

        after = await client.get(
            f"/api/v1/workflows/{mine['id']}", headers=two_profiles["a_headers"]
        )
        assert after.json()["version"] == 1
        assert after.json()["name"] == "trim then align"


class TestPalette:
    async def test_exposes_every_node_type(self, client, two_profiles):
        """Generated from NODE_TYPES rather than hand-listed. A tool added to
        the registry must appear here without anyone editing the frontend --
        the whole reason the registry carries labels and ports."""
        resp = await client.get(
            "/api/v1/workflows/node-types", headers=two_profiles["a_headers"]
        )

        assert resp.status_code == 200
        palette = resp.json()
        from app.pipelines.node_types import NODE_TYPES

        assert {entry["node_type"] for entry in palette} == set(NODE_TYPES)

    async def test_carries_typed_ports_for_wire_validation(self, client, two_profiles):
        resp = await client.get(
            "/api/v1/workflows/node-types", headers=two_profiles["a_headers"]
        )
        align = next(e for e in resp.json() if e["node_type"] == "align")

        reference = next(p for p in align["inputs"] if p["name"] == "reference")
        assert reference["type"]["format"] == "fasta"
        # The role is the point: a protein FASTA must not reach this port, and
        # the canvas cannot enforce that without the role travelling with it.
        assert reference["type"]["role"] == "reference"
        assert reference["required"] is True


class TestLaunch:
    async def test_launching_an_unknown_definition_is_404(self, client, two_profiles):
        """Paired with a real launch below on purpose. On its own this passed
        before the router existed at all -- an absent route 404s too, so it
        asserted nothing. The sibling test is what proves the 404 here is the
        route's answer rather than its absence."""
        resp = await client.post(
            f"/api/v1/workflows/{PydanticObjectId()}/runs",
            json={"project_id": str(PydanticObjectId()), "label": "x", "bindings": {}},
            headers=two_profiles["a_headers"],
        )
        assert resp.status_code == 404

    async def test_launching_my_own_definition_creates_a_run(
        self, client, two_profiles, monkeypatch
    ):
        """The launcher is stubbed: really launching would run fastp. What
        matters here is that the route reaches the orchestrator and hands back
        a run id, not what the tools do."""
        from app.services import workflow_orchestrator as orch

        async def fake_launch(node_type, *, inputs, params, owner):
            return None  # deduplicated-away shape; the node still goes RUNNING

        monkeypatch.setattr(orch, "_launch_node", fake_launch)

        mine = await _create(client, two_profiles["a_headers"])

        resp = await client.post(
            f"/api/v1/workflows/{mine['id']}/runs",
            json={
                "project_id": str(PydanticObjectId()),
                "label": "my run",
                "bindings": {
                    "reads": str(PydanticObjectId()),
                    "reference": str(PydanticObjectId()),
                },
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["id"]
        assert resp.json()["status"]

    async def test_cannot_launch_another_owners_definition(self, client, two_profiles):
        mine = await _create(client, two_profiles["a_headers"])

        resp = await client.post(
            f"/api/v1/workflows/{mine['id']}/runs",
            json={"project_id": str(PydanticObjectId()), "label": "x", "bindings": {}},
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 404


class TestDerive:
    async def test_derive_is_not_swallowed_by_the_id_route(self, client, two_profiles):
        """`/workflows/derive` sits under the same prefix as
        `/workflows/{definition_id}`. Declared the wrong way round, "derive"
        parses as an id and the endpoint 404s or 422s instead of running."""
        resp = await client.post(
            "/api/v1/workflows/derive",
            json={"run_ids": []},
            headers=two_profiles["a_headers"],
        )
        assert resp.status_code == 200
        assert resp.json() == {"nodes": [], "edges": [], "skipped": []}

    async def test_an_unknown_run_is_reported_as_skipped(self, client, two_profiles):
        resp = await client.post(
            "/api/v1/workflows/derive",
            json={"run_ids": [str(PydanticObjectId())]},
            headers=two_profiles["a_headers"],
        )
        assert resp.status_code == 200
        assert len(resp.json()["skipped"]) == 1
