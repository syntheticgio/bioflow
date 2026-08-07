"""The generated `version.py` must agree with the `VERSION` file.

This is the test that catches a hand-edit of one without the other. Before
this existed, five files declared a version and nothing compared them; all
five read 0.1.0 by coincidence rather than by construction.
"""

import re
from pathlib import Path

from app.version import __version__

# tests/ -> backend/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_version_file_exists_and_is_semver():
    raw = (REPO_ROOT / "VERSION").read_text()
    assert raw.endswith("\n"), "VERSION must end with a newline"
    assert SEMVER.match(raw.strip()), f"VERSION is not MAJOR.MINOR.PATCH: {raw!r}"


def test_generated_module_matches_version_file():
    expected = (REPO_ROOT / "VERSION").read_text().strip()
    assert __version__ == expected
