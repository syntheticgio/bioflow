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

# Mirrors VERSION_RE in ops/release.sh: a pre-release cut writes
# `X.Y.Z-alpha` / `X.Y.Z-beta` into VERSION, so requiring bare
# MAJOR.MINOR.PATCH here reds the suite for the whole alpha and beta
# stages. Other suffixes (-rc, build metadata, a leading v) stay rejected,
# which is the part that catches a hand-edit.
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-alpha|-beta)?$")


def test_version_file_exists_and_is_semver():
    raw = (REPO_ROOT / "VERSION").read_text()
    assert raw.endswith("\n"), "VERSION must end with a newline"
    assert SEMVER.match(raw.strip()), (
        f"VERSION is not MAJOR.MINOR.PATCH, optionally -alpha or -beta: {raw!r}"
    )


def test_generated_module_matches_version_file():
    expected = (REPO_ROOT / "VERSION").read_text().strip()
    assert __version__ == expected


def test_fastapi_app_reports_the_generated_version():
    """The OpenAPI version is the one place a stale literal was user-visible."""
    from app.main import app

    assert app.version == __version__


def test_main_py_holds_no_hardcoded_version_literal():
    """A second declaration that only exists to be kept in sync is the same
    trap one level down -- main.py must import, not restate.

    Located via app.main.__file__ rather than REPO_ROOT / "backend" / "app":
    the worktree test runner mounts backend/app directly at /srv/app inside
    the container, with no /backend prefix, so a path built from REPO_ROOT
    would silently look in the wrong place there.
    """
    import app.main

    main_py = Path(app.main.__file__).read_text()
    assert 'version="0.' not in main_py
