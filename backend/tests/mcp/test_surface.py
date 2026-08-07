"""What the MCP surface must never grow.

The spec's decision was read + create + launch + cancel, no deletes: launching
a wasteful job costs CPU time, while `delete_project` costs someone their
library with no undo and no auth layer to catch an agent misreading its own
context.

That decision lives in a design document nobody re-reads. This test is what
makes it survive contact with the next person adding a tool.
"""

import re

import pytest

from app.mcp import tools

# `tests/mcp/conftest.py`'s autouse `clean_profiles` fixture calls
# `Profile.find_all()` before and after every test in this package, and
# Beanie raises `CollectionWasNotInitialized` for any Document class that
# hasn't gone through `init_beanie` yet. `beanie_models` does that init but is
# requested explicitly rather than being autouse itself (see its docstring in
# tests/conftest.py), so any module that doesn't ask for it -- like this one,
# whose tests are pure introspection over `tools.py` and touch no database --
# still needs it purely to satisfy its neighbour's cleanup fixture.
pytestmark = pytest.mark.usefixtures("beanie_models")

DESTRUCTIVE = re.compile(r"delete|destroy|remove|uninstall|purge|wipe", re.I)


def test_no_destructive_tools_are_exposed():
    offenders = {n for n in tools.TOOL_NAMES if DESTRUCTIVE.search(n)}

    assert not offenders, (
        f"Destructive tools in the MCP surface: {offenders}. "
        "See docs/superpowers/specs/2026-08-06-mcp-server-design.md -- deletes "
        "were deliberately excluded. If that decision has changed, change it "
        "there first."
    )


def test_tool_names_match_the_registered_functions():
    """TOOL_NAMES is hand-written and could drift from what is registered.

    Every name must have a matching function in the module, with the
    `bioflow_` prefix stripped -- otherwise the guide drift test in
    test_guides.py validates against a list that no longer describes reality.
    """
    for name in tools.TOOL_NAMES:
        func_name = name.removeprefix("bioflow_")
        assert hasattr(tools, func_name), f"{name} has no function {func_name}"


def test_every_public_tool_function_is_declared():
    """The other direction: a function added without a TOOL_NAMES entry.

    `set(functions) == set(names)` is the exhaustiveness shape CLAUDE.md names
    as the pattern to copy, and this half catches the tool that silently never
    gets registered.
    """
    import inspect

    public = {
        name
        for name, obj in inspect.getmembers(tools, inspect.iscoroutinefunction)
        if not name.startswith("_") and obj.__module__ == tools.__name__
    }
    declared = {n.removeprefix("bioflow_") for n in tools.TOOL_NAMES}

    assert public == declared
