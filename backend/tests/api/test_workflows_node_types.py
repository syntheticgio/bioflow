"""Tests for the `param_fields` served on the node-type catalog.

`TestPalette` in test_workflows_api.py already covers the rest of this
endpoint's shape (ports, tool_choice, ports_by_tool); this file is scoped to
the static parameter fields a node type can declare on itself.
"""

import pytest

# Module-scoped database and loop, matching test_workflows_api.py: the
# `two_profiles` fixture instantiates Documents, which Beanie refuses before
# init_beanie.
pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def test_node_type_catalog_serves_static_param_fields(client, two_profiles):
    """The canvas cannot render a form for fields it is never sent."""
    resp = await client.get(
        "/api/v1/workflows/node-types", headers=two_profiles["a_headers"]
    )
    assert resp.status_code == 200

    by_type = {n["node_type"]: n for n in resp.json()}
    export = by_type["annotation_export"]

    keys = [f["key"] for f in export["param_fields"]]
    assert keys == [
        "contig",
        "start_min",
        "start_max",
        "feature_type",
        "biotype",
        "name_query",
        "strand",
        "output_name",
    ]
    assert all(f["group"] == "filters" for f in export["param_fields"][:7])


async def test_node_types_without_static_fields_serve_an_empty_list(
    client, two_profiles
):
    resp = await client.get(
        "/api/v1/workflows/node-types", headers=two_profiles["a_headers"]
    )
    by_type = {n["node_type"]: n for n in resp.json()}
    assert by_type["qc"]["param_fields"] == []


async def test_multi_format_port_serves_every_accepted_format(client, two_profiles):
    """The frontend mirrors the accept rule, so it needs the whole set."""
    resp = await client.get(
        "/api/v1/workflows/node-types", headers=two_profiles["a_headers"]
    )
    by_type = {n["node_type"]: n for n in resp.json()}
    port = next(
        p for p in by_type["annotation_export"]["inputs"] if p["name"] == "annotation"
    )
    assert set(port["type"]["formats"]) == {"gff", "gtf", "bed"}
