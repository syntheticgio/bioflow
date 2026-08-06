"""The guides, and the tests that keep them true.

Hand-written prose about code goes stale silently. This repo has been bitten
three times -- the 2026-07-31 TODO audit, the `ToolMeta.runnable` comment
citing cutadapt years after `trim_reads` grew its dispatch, and
`results._SIDECAR_ROLES` dropping STAR's index files with the suite green
throughout. A guide that confidently names a tool which no longer exists is
worse than no guide, because the entire point of the feature is telling an
agent what is true.

These tests are why guides must name symbols as backticked literals rather
than paraphrase: prose saying "the alignment job" instead of `align` is
invisible here and free to rot.
"""

import re

import pytest

from app.mcp import resources
from app.main import app
from app.pipelines.tools import TOOL_META
from app.queue.registry import all_handlers

# test_guides.py itself never touches the database -- load_guide and
# GuideTopic are pure file/enum reads. beanie_models is required anyway
# because tests/mcp/conftest.py's clean_profiles fixture is autouse for the
# whole package and calls Profile.find_all(), which needs Beanie initialized
# regardless of what this module's own tests do.
pytestmark = pytest.mark.usefixtures("beanie_models")


def test_every_topic_has_a_file():
    for topic in resources.GuideTopic:
        assert resources.load_guide(topic).strip(), f"{topic} is empty"


def test_every_file_has_a_topic():
    """The other direction: a stray .md is a guide nothing can reach.

    `set(enum) == set(files)` is the exhaustiveness pattern CLAUDE.md names as
    the one to copy, and this is the half that catches a file added without a
    topic to serve it.
    """
    on_disk = {p.stem for p in resources.GUIDES_DIR.glob("*.md")}
    declared = {t.value for t in resources.GuideTopic}

    assert on_disk == declared


def _backticked(text: str) -> set[str]:
    """Every `literal` in the guide.

    Backticks are the marker that says "this is a real symbol, check it".
    Anything a guide wants to say without being checked it simply writes
    without them.
    """
    return set(re.findall(r"`([^`\n]+)`", text))


def _all_guide_symbols() -> set[str]:
    symbols: set[str] = set()
    for topic in resources.GuideTopic:
        symbols |= _backticked(resources.load_guide(topic))
    return symbols


def test_job_type_names_are_real():
    """A guide naming a job type that isn't registered would send an agent to
    `bioflow_run_pipeline` with a kind that can never run."""
    registered = set(all_handlers())
    # Only check symbols that look like job types -- lowercase words with
    # underscores. A guide also backticks tool names, paths and parameters,
    # and those are checked by their own tests below. MCP tool names
    # (`bioflow_*`) are excluded here too -- they match the same shape but
    # are checked by test_mcp_tool_names_in_guides_exist instead.
    candidates = {
        s
        for s in _all_guide_symbols()
        if re.fullmatch(r"[a-z][a-z0-9_]+", s) and not s.startswith("bioflow_")
    }

    unknown = {c for c in candidates if c not in registered and c not in TOOL_META}
    # Names that are neither a job type nor a tool are allowed only if they
    # are on this list, which exists so a guide can say `format` or `role`
    # without inventing a checkable symbol for it.
    allowed_prose = {
        "format",
        "role",
        "available",
        "unavailable",
        "needs_install",
        "kind",
        "params",
        "object_id",
    }

    assert unknown <= allowed_prose, f"Guides name unknown symbols: {unknown - allowed_prose}"


def test_tool_names_are_real():
    """Every bioinformatics tool a guide names must be in TOOL_META.

    This is the `runnable`-comment failure made loud: that comment cited
    cutadapt and Trimmomatic for years after they stopped being what
    `trim_reads` dispatched to, and nothing failed because a comment cannot.

    Checked from a fixed list of names this project's guides are allowed to
    mention rather than by pattern-matching every backticked token: a tool
    name has no shape that distinguishes it from a job type or a field name,
    so a pattern would either miss real drift or reject prose.
    """
    documented = {k.lower() for k in TOOL_META}

    # Tools the guides are expected to name. Grown as guides are written; a
    # name added here that TOOL_META does not have fails immediately, which
    # is the point.
    named_in_guides = {
        "fastp",
        "minimap2",
        "samtools",
        "bcftools",
    }

    unknown = {n for n in named_in_guides if n not in documented}
    assert not unknown, f"Guides name tools absent from TOOL_META: {unknown}"

    # And every one of those must actually appear in some guide -- a name
    # left here after the guide stopped mentioning it makes this test look
    # like it is checking more than it is.
    all_text = " ".join(resources.load_guide(t).lower() for t in resources.GuideTopic)
    unused = {n for n in named_in_guides if f"`{n}`" not in all_text}
    assert not unused, f"Listed as named in guides but absent from all of them: {unused}"


def test_endpoint_paths_are_real():
    """A guide naming a REST path must name one the app actually serves."""
    routes = {getattr(r, "path", None) for r in app.routes}
    routes.discard(None)

    named = {s for s in _all_guide_symbols() if s.startswith("/")}

    for path in named:
        # Path params are written as {id} in guides and {object_id} in routes,
        # so compare with params normalised away.
        normalised = re.sub(r"\{[^}]+\}", "{}", path)
        known = {re.sub(r"\{[^}]+\}", "{}", r) for r in routes}
        assert normalised in known, f"Guide names unknown endpoint: {path}"


def test_mcp_tool_names_in_guides_exist():
    """A guide telling an agent to call `bioflow_foo` when no such tool is
    registered is the most direct way this feature can mislead."""
    from app.mcp import tools

    registered = set(tools.TOOL_NAMES)
    named = {s for s in _all_guide_symbols() if s.startswith("bioflow_")}
    # Guides write calls as `bioflow_get_job` or `bioflow_run_pipeline(kind, params)`;
    # strip any argument list before comparing.
    bare = {n.split("(")[0] for n in named}

    assert bare <= registered, f"Guides name unknown MCP tools: {bare - registered}"
