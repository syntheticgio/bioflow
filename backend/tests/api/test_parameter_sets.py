"""The parameter-set API (#414)."""

import math

import pytest
from beanie import PydanticObjectId

from app.models.parameter_set import ParameterSet

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def _create(client, headers, **kw):
    body = {
        "name": "Nanopore fast",
        "tool": "minimap2",
        "family": "aligner",
        "params": {"threads": 8},
    }
    body.update(kw)
    return await client.post("/api/v1/parameter-sets", json=body, headers=headers)


class TestCrud:
    async def test_create_then_list_by_tool(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        r = await _create(client, a_headers)
        assert r.status_code == 201, r.text
        assert r.json()["revision"] == 1

        listed = await client.get(
            "/api/v1/parameter-sets", params={"tool": "minimap2"}, headers=a_headers
        )
        assert [s["name"] for s in listed.json()] == ["Nanopore fast"]

    async def test_list_requires_a_tool(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        r = await client.get("/api/v1/parameter-sets", headers=a_headers)
        assert r.status_code == 422

    async def test_rename_keeps_revision(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        sid = (await _create(client, a_headers)).json()["id"]
        r = await client.patch(
            f"/api/v1/parameter-sets/{sid}", json={"name": "Renamed"}, headers=a_headers
        )
        assert r.json() == {**r.json(), "name": "Renamed", "revision": 1}

    async def test_editing_params_bumps_revision(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        sid = (await _create(client, a_headers)).json()["id"]
        r = await client.patch(
            f"/api/v1/parameter-sets/{sid}",
            json={"params": {"threads": 12}},
            headers=a_headers,
        )
        assert r.json()["revision"] == 2

    async def test_delete(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        sid = (await _create(client, a_headers)).json()["id"]
        assert (
            await client.delete(f"/api/v1/parameter-sets/{sid}", headers=a_headers)
        ).status_code == 204
        listed = await client.get(
            "/api/v1/parameter-sets", params={"tool": "minimap2"}, headers=a_headers
        )
        assert listed.json() == []


class TestSaveDropsIneligibleKeys:
    async def test_input_bindings_are_not_stored(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        r = await _create(
            client, a_headers,
            params={"threads": 8, "reference_id": "68a1f00000000000000000aa", "chunked": True},
        )
        assert r.json()["params"] == {"threads": 8}

    async def test_unset_none_values_are_not_stored(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        r = await _create(
            client,
            a_headers,
            params={
                "preset": "map-ont",
                "kmer_size": None,
                "window_size": 10,
                "emit_md": None,
            },
        )
        assert r.json()["params"] == {"preset": "map-ont", "window_size": 10}


class TestUniqueness:
    async def test_same_name_same_tool_collides(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        await _create(client, a_headers)
        assert (await _create(client, a_headers)).status_code == 409

    async def test_same_name_different_tool_is_fine(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        await _create(client, a_headers)
        r = await _create(client, a_headers, tool="star", params={})
        assert r.status_code == 201


class TestOwnerIsolation:
    async def test_b_cannot_list_as_own(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        b_headers = two_profiles["b_headers"]
        await _create(client, a_headers)
        r = await client.get(
            "/api/v1/parameter-sets", params={"tool": "minimap2"}, headers=b_headers
        )
        assert r.json() == []

    async def test_b_cannot_delete_as_own(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        b_headers = two_profiles["b_headers"]
        sid = (await _create(client, a_headers)).json()["id"]
        assert (
            await client.delete(f"/api/v1/parameter-sets/{sid}", headers=b_headers)
        ).status_code == 404


class TestSupported:
    async def test_true_for_a_tool_with_fields(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        r = await client.get(
            "/api/v1/parameter-sets/supported",
            params={"family": "aligner", "tool": "minimap2"},
            headers=a_headers,
        )
        assert r.json() == {"supported": True}

    async def test_false_for_a_tool_without_fields(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        r = await client.get(
            "/api/v1/parameter-sets/supported",
            params={"family": "assembler", "tool": "hifiasm"},
            headers=a_headers,
        )
        assert r.json() == {"supported": False}


class TestResolve:
    async def test_returns_applied_and_set_identity(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        created = (await _create(client, a_headers)).json()
        r = await client.post(
            f"/api/v1/parameter-sets/{created['id']}/resolve", headers=a_headers
        )
        body = r.json()
        assert body["applied"]["threads"] == 8
        assert body["rejected"] == []
        assert body["set"] == {"id": created["id"], "name": "Nanopore fast", "revision": 1}

    async def test_reapplies_saved_float_values(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        created = (
            await _create(
                client,
                a_headers,
                params={"secondary_ratio": 0.35},
            )
        ).json()
        body = (
            await client.post(
                f"/api/v1/parameter-sets/{created['id']}/resolve", headers=a_headers
            )
        ).json()
        assert body["applied"] == {"secondary_ratio": 0.35}
        assert body["rejected"] == []

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    async def test_rejects_non_finite_saved_float_values(self, client, two_profiles, value):
        a_headers = two_profiles["a_headers"]
        created = (await _create(client, a_headers)).json()
        await ParameterSet.find_one(
            ParameterSet.id == PydanticObjectId(created["id"])
        ).update({"$set": {"params": {"secondary_ratio": value}}})
        body = (
            await client.post(
                f"/api/v1/parameter-sets/{created['id']}/resolve", headers=a_headers
            )
        ).json()
        assert body["applied"] == {}
        assert body["rejected"][0]["key"] == "secondary_ratio"
        assert body["rejected"][0]["reason"] == "out_of_range"

    async def test_flags_a_drifted_key(self, client, two_profiles):
        a_headers = two_profiles["a_headers"]
        created = (await _create(client, a_headers)).json()
        await ParameterSet.find_one(
            ParameterSet.id == PydanticObjectId(created["id"])
        ).update({"$set": {"params": {"threads": 8, "gone": 1}}})
        body = (
            await client.post(
                f"/api/v1/parameter-sets/{created['id']}/resolve", headers=a_headers
            )
        ).json()
        assert body["applied"] == {"threads": 8}
        assert body["rejected"][0]["key"] == "gone"
        assert body["rejected"][0]["reason"] == "unknown_field"
