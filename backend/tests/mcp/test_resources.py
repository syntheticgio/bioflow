"""The derived resources.

Each is generated from the code it describes, so these tests assert the
derivation reaches the real registry rather than a copy -- a resource built
from a hand-written list would pass a shallow test and drift exactly like the
prose it was meant to replace.
"""

from dataclasses import asdict

import pytest

from app.mcp import resources
from app.pipelines.tools import TOOL_META

# This module never touches the database itself, but tests/mcp/conftest.py's
# clean_profiles fixture is autouse for the whole package and calls
# Profile.find_all(), which needs Beanie initialized regardless -- same
# reasoning as test_guides.py's identical marker.
pytestmark = pytest.mark.usefixtures("beanie_models")


def test_installed_tools_resource_covers_every_documented_tool():
    payload = resources.installed_tools()

    assert set(payload["tools"]) == set(TOOL_META)


def test_installed_tools_resource_carries_the_actual_field_values():
    """Not just that a sample tool has the four keys, but that every tool's
    values are the real ToolMeta fields, not a placeholder or a value
    silently dropped for entries other than whichever iterates first."""
    payload = resources.installed_tools()

    for name, meta in TOOL_META.items():
        assert payload["tools"][name] == asdict(meta)


def test_installed_tools_resource_carries_the_documentation_fields():
    """`/help/software` requires homepage, citation, license and usage for
    every tool. An agent deserves the same, not a bare name list -- checked
    for every tool, not just one, since ToolMeta defaults all four fields to
    "" and a single-sample check can't tell a correct conversion from one
    that silently drops fields on every entry but the first."""
    payload = resources.installed_tools()

    for meta in payload["tools"].values():
        assert {"homepage", "citation", "license", "usage"} <= set(meta)


def test_job_types_resource_matches_the_handler_registry():
    from app.queue.registry import all_handlers

    payload = resources.job_types()
    handlers = all_handlers()

    assert set(payload["job_types"]) == set(handlers)
    for name, spec in handlers.items():
        assert payload["job_types"][name] == {
            "mode": spec.mode.value,
            "default_class": spec.default_class.value,
        }


def test_sources_resource_is_not_empty():
    payload = resources.data_sources()

    assert payload["sources"]
