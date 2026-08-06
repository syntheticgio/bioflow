"""The derived resources.

Each is generated from the code it describes, so these tests assert the
derivation reaches the real registry rather than a copy -- a resource built
from a hand-written list would pass a shallow test and drift exactly like the
prose it was meant to replace.
"""

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


def test_installed_tools_resource_carries_the_documentation_fields():
    """`/help/software` requires homepage, citation, license and usage for
    every tool. An agent deserves the same, not a bare name list."""
    payload = resources.installed_tools()
    sample = next(iter(payload["tools"].values()))

    assert {"homepage", "citation", "license", "usage"} <= set(sample)


def test_job_types_resource_matches_the_handler_registry():
    from app.queue.registry import all_handlers

    payload = resources.job_types()

    assert set(payload["job_types"]) == set(all_handlers())


def test_sources_resource_is_not_empty():
    payload = resources.data_sources()

    assert payload["sources"]
