"""Every vendored pi skill must load.

A skill that fails to load is silent: pi starts fine and simply does not
have the workflow, and nothing reports it. This test makes the inventory
explicit — the expected set below is the contract. Adding a skill means
adding it here AND writing it; the test fails until both exist.
"""

import inspect
import re
from pathlib import Path

import pytest
from app.mcp import server as mcp_server

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "backend" / "pi-skills"

# The curated inventory (spec section "Skill set"). Keep in sync with the
# README — the test asserts each skill is documented there too.
EXPECTED_SKILLS = {
    "run-qc",
    "interpret-multiqc",
    "suggest-next-steps",
    "debug-failed-job",
    "drive-pipelines",
    "interpret-alignment",
    "variant-analysis",
    "bioflow-database-access",
}


@pytest.fixture(scope="module")
def skills_dir() -> Path:
    if not SKILLS_DIR.exists():
        pytest.skip(f"{SKILLS_DIR} not mounted in this test environment")
    return SKILLS_DIR


def _frontmatter_name(skill_md: str) -> str | None:
    m = re.search(r"^---\n(.*?)\n---", skill_md, re.DOTALL)
    if not m:
        return None
    name = re.search(r"^name:\s*(\S+)", m.group(1), re.MULTILINE)
    return name.group(1) if name else None


def _real_mcp_tools() -> set[str]:
    src = inspect.getsource(mcp_server)
    return set(re.findall(r'name="(bioflow_[a-z_]+)"', src))


def test_inventory_is_complete_and_loads(skills_dir):
    real_tools = _real_mcp_tools()
    readme = (skills_dir / "README.md").read_text()
    for name in EXPECTED_SKILLS:
        md = (skills_dir / name / "SKILL.md").read_text()
        assert _frontmatter_name(md) == name, (
            f"{name}/SKILL.md frontmatter name mismatch"
        )
        assert name in readme, f"{name} not documented in pi-skills/README.md"
        referenced = set(re.findall(r"bioflow_[a-z_]+", md))
        unknown = referenced - real_tools
        assert not unknown, (
            f"{name} references unknown MCP tools: {sorted(unknown)}"
        )
