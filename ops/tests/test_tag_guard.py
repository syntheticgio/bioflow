"""The guard that catches a hand-rolled tag.

Redundant behind release.sh by construction, which is the point: it is what
catches `git tag v0.9.0` typed by hand against a tree that says 0.1.0.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "ops" / "check_tag_matches_version.sh"


def run_guard(root: Path, tag: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(GUARD), tag], cwd=root, capture_output=True, text=True
    )


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "VERSION").write_text("0.2.0\n")
    (tmp_path / "launcher" / "src-tauri").mkdir(parents=True)
    (tmp_path / "launcher" / "src-tauri" / "Cargo.toml").write_text(
        '[package]\nname = "bioflow-launcher"\nversion = "0.1.1"\nedition = "2021"\n'
    )
    return tmp_path


class TestAppTags:
    def test_accepts_a_matching_tag(self, tree):
        r = run_guard(tree, "v0.2.0")
        assert r.returncode == 0, r.stderr

    def test_rejects_a_mismatched_tag(self, tree):
        r = run_guard(tree, "v0.9.0")
        assert r.returncode != 0
        assert "0.9.0" in (r.stdout + r.stderr)
        assert "0.2.0" in (r.stdout + r.stderr)


class TestLauncherTags:
    def test_accepts_a_matching_tag(self, tree):
        r = run_guard(tree, "launcher-v0.1.1")
        assert r.returncode == 0, r.stderr

    def test_rejects_a_mismatched_tag(self, tree):
        r = run_guard(tree, "launcher-v0.9.9")
        assert r.returncode != 0

    def test_launcher_tag_is_not_checked_against_the_app_version(self, tree):
        """launcher-v0.2.0 matches VERSION but not Cargo.toml -- it must fail.
        Checking the wrong source of truth is the subtle way this guard breaks."""
        r = run_guard(tree, "launcher-v0.2.0")
        assert r.returncode != 0


class TestUnrecognisedTags:
    def test_rejects_a_tag_with_no_known_prefix(self, tree):
        r = run_guard(tree, "release-2026-08-07")
        assert r.returncode != 0
