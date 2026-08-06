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

import pytest

from app.mcp import resources

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
