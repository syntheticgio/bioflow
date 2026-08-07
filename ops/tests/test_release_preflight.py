"""The release script refuses to run from a state that would produce a bad release.

Each test puts a throwaway repo into one bad state and asserts a refusal. The
messages matter as much as the exit codes: the whole point of refusing is to
tell the operator which precondition they tripped.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE = REPO_ROOT / "ops" / "release.sh"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def run_release(repo: Path, line: str, version: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(RELEASE), line, version],
        cwd=repo,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A clean repo on main, at 0.1.0, with a fake origin it can push to."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True,
                   capture_output=True)

    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")

    # The app line's files, plus the scripts the release script calls.
    (work / "VERSION").write_text("0.1.0\n")
    (work / "backend" / "app").mkdir(parents=True)
    (work / "backend" / "app" / "version.py").write_text('__version__ = "0.1.0"\n')
    (work / "backend" / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (work / "frontend").mkdir()
    (work / "frontend" / "package.json").write_text('{\n  "version": "0.1.0"\n}\n')
    (work / "launcher" / "src-tauri").mkdir(parents=True)
    (work / "launcher" / "src-tauri" / "Cargo.toml").write_text(
        '[package]\nversion = "0.1.0"\n'
    )
    (work / "launcher" / "package.json").write_text('{\n  "version": "0.1.0"\n}\n')

    # The script resolves its own directory, so ops/ must exist in the fake repo.
    (work / "ops" / "lib").mkdir(parents=True)
    (work / "ops" / "release.sh").write_bytes(RELEASE.read_bytes())
    (work / "ops" / "release.sh").chmod(0o755)
    (work / "ops" / "lib" / "bump_version.py").write_bytes(
        (REPO_ROOT / "ops" / "lib" / "bump_version.py").read_bytes()
    )

    git(work, "add", "-A")
    git(work, "commit", "-m", "initial")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-u", "origin", "main")
    return work


class TestPreflightRefusals:
    def test_refuses_a_non_semver_version(self, repo):
        r = run_release(repo, "app", "v0.2.0")
        assert r.returncode != 0
        assert "semver" in (r.stderr + r.stdout).lower()

    def test_refuses_a_dirty_tree(self, repo):
        (repo / "VERSION").write_text("0.1.0\n\n# stray edit\n")
        r = run_release(repo, "app", "0.2.0")
        assert r.returncode != 0
        assert "clean" in (r.stderr + r.stdout).lower()

    def test_refuses_off_main(self, repo):
        git(repo, "checkout", "-b", "feature/x")
        r = run_release(repo, "app", "0.2.0")
        assert r.returncode != 0
        assert "main" in (r.stderr + r.stdout).lower()

    def test_refuses_an_existing_tag(self, repo):
        git(repo, "tag", "v0.2.0")
        r = run_release(repo, "app", "0.2.0")
        assert r.returncode != 0
        assert "exists" in (r.stderr + r.stdout).lower()

    def test_refuses_a_version_that_does_not_increase(self, repo):
        r = run_release(repo, "app", "0.0.9")
        assert r.returncode != 0
        assert "greater" in (r.stderr + r.stdout).lower()

    def test_refuses_the_same_version(self, repo):
        r = run_release(repo, "app", "0.1.0")
        assert r.returncode != 0


class TestSuccessfulRelease:
    def test_bumps_commits_tags_and_pushes(self, repo):
        r = run_release(repo, "app", "0.2.0")
        assert r.returncode == 0, r.stderr

        assert (repo / "VERSION").read_text() == "0.2.0\n"

        tags = git(repo, "tag", "-l").stdout.split()
        assert "v0.2.0" in tags

        subject = git(repo, "log", "-1", "--format=%s").stdout.strip()
        assert subject == "release: v0.2.0"

        # The commit touches only version declarations.
        files = set(git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split())
        assert files == {
            "VERSION",
            "backend/app/version.py",
            "backend/pyproject.toml",
            "frontend/package.json",
        }

        # And it reached the origin, tag included.
        remote_tags = git(repo, "ls-remote", "--tags", "origin").stdout
        assert "v0.2.0" in remote_tags

    def test_launcher_line_uses_its_own_tag_prefix(self, repo):
        r = run_release(repo, "launcher", "0.1.1")
        assert r.returncode == 0, r.stderr

        tags = git(repo, "tag", "-l").stdout.split()
        assert "launcher-v0.1.1" in tags
        assert git(repo, "log", "-1", "--format=%s").stdout.strip() == (
            "release: launcher-v0.1.1"
        )

        files = set(git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split())
        assert files == {"launcher/src-tauri/Cargo.toml", "launcher/package.json"}

    def test_app_release_leaves_the_launcher_version_alone(self, repo):
        run_release(repo, "app", "0.2.0")
        cargo = (repo / "launcher" / "src-tauri" / "Cargo.toml").read_text()
        assert 'version = "0.1.0"' in cargo


class TestBumpFailurePartway:
    """bump_version.py failing after printing some output must abort the
    release, not slip past the emptiness check.

    Regression test for the process-substitution bug: `while read; done <
    <(cmd)` hides a nonzero exit from `set -e`, because the loop's own exit
    status -- not the command's -- is what the pipeline reports. A
    bump_version.py that prints a path and then fails used to leave WRITTEN
    non-empty, satisfy the `-gt 0` guard, and let release.sh continue on to
    commit/tag/push a partially-bumped release.
    """

    def test_refuses_when_bump_fails_after_partial_output(self, repo):
        # Replace the real bump_version.py with a fake that prints one
        # already-written path (simulating partial progress) and then exits
        # nonzero, as a future --verbose bump_version.py might on a mid-run
        # failure.
        fake = (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('VERSION')\n"
            "sys.exit(1)\n"
        )
        bump_path = repo / "ops" / "lib" / "bump_version.py"
        bump_path.write_text(fake)
        bump_path.chmod(0o755)

        before_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        before_tags = set(git(repo, "tag", "-l").stdout.split())
        before_remote_tags = git(repo, "ls-remote", "--tags", "origin").stdout

        r = run_release(repo, "app", "0.2.0")

        assert r.returncode != 0

        # No new commit, no new local tag, nothing new pushed to origin.
        assert git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
        assert set(git(repo, "tag", "-l").stdout.split()) == before_tags
        assert git(repo, "ls-remote", "--tags", "origin").stdout == before_remote_tags
        assert (repo / "VERSION").read_text() == "0.1.0\n"
