"""The release script refuses to run from a state that would produce a bad release.

Each test puts a throwaway repo into one bad state and asserts a refusal. The
messages matter as much as the exit codes: the whole point of refusing is to
tell the operator which precondition they tripped.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE = REPO_ROOT / "ops" / "release.sh"

FAKE_GIT_CLIFF = """#!/usr/bin/env bash
# Mimics `git-cliff --unreleased --tag <tag> --prepend CHANGELOG.md` for the
# fixture repo: parse --tag, append a matching section, create the file.
tag=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) tag="$2"; shift 2 ;;
    *) shift ;;
  esac
done
{
  if [ -f CHANGELOG.md ]; then printf '\n'; fi
  printf '## [%s]\n\n- fixture entry\n' "${tag#v}"
} >> CHANGELOG.md
"""


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def run_release(repo: Path, line: str, version: str, env=None) -> subprocess.CompletedProcess:
    # Invoke the fixture repo's OWN copy of release.sh, not the real
    # worktree's. release.sh resolves its own lib directory via
    # `dirname "${BASH_SOURCE[0]}"`, which follows the path it was invoked
    # with -- so calling the module-level RELEASE constant here (the real
    # worktree's ops/release.sh) makes it resolve SCRIPT_DIR to the real
    # worktree's ops/, and load the real, unmodified bump_version.py
    # instead of the fixture's copy. Any test that swaps in a fake
    # bump_version.py (see TestBumpFailurePartway) would silently run the
    # real one with this bug -- the fixture copy at repo/ops/release.sh
    # made at setup time (see the `repo` fixture above) must be the one
    # actually executed.
    run_env = dict(os.environ)
    # Point the git-cliff cache at the fixture's fake binary: release.sh
    # computes GIT_CLIFF_DIR from XDG_CACHE_HOME (falling back to the real
    # $HOME/.cache), and its bootstrap runs whatever it finds there. Without
    # this the app cut would silently use the real ~/.cache git-cliff -- or
    # download it -- instead of the fake. An explicit env from a caller wins.
    run_env.setdefault("XDG_CACHE_HOME", str(repo.parent / "xdg-cache"))
    if env:
        run_env.update(env)
    return subprocess.run(
        [str(repo / "ops" / "release.sh"), line, version],
        cwd=repo,
        capture_output=True,
        text=True,
        env=run_env,
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

    # Hermetic git-cliff: the app cut regenerates CHANGELOG.md (#106), and the
    # script's bootstrap returns early when the cached binary exists -- so a
    # fake at the cache path is what runs. Never hits the network. The cache
    # dir lives under this test's own tmp_path: run_release derives
    # XDG_CACHE_HOME from the repo's parent, so no test sees another test's
    # fake and the real ~/.cache is never touched.
    xdg_cache = tmp_path / "xdg-cache"
    git_cliff_dir = xdg_cache / "bioflow-tools" / "git-cliff-2.13.1"
    git_cliff_dir.mkdir(parents=True)
    (git_cliff_dir / "git-cliff").write_text(FAKE_GIT_CLIFF)
    (git_cliff_dir / "git-cliff").chmod(0o755)
    (work / "CHANGELOG.md").write_text("# Changelog\n")

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

    def test_refuses_an_alpha_cut_off_main(self, repo):
        git(repo, "checkout", "-b", "feature/x")
        r = run_release(repo, "app", "0.3.0-alpha")
        assert r.returncode != 0
        assert "alpha" in (r.stderr + r.stdout).lower()
        assert "main" in (r.stderr + r.stdout).lower()

    def test_refuses_a_beta_cut_off_main(self, repo):
        # main is the right source for an alpha, never for a beta.
        r = run_release(repo, "app", "0.3.0-beta")
        assert r.returncode != 0
        assert "beta" in (r.stderr + r.stdout).lower()
        assert "alpha/0.3.0" in (r.stderr + r.stdout).lower()

    def test_refuses_a_beta_cut_off_the_wrong_alpha_branch(self, repo):
        git(repo, "checkout", "-b", "alpha/0.3.0")
        r = run_release(repo, "app", "0.4.0-beta")
        assert r.returncode != 0
        assert "alpha/0.4.0" in (r.stderr + r.stdout).lower()

    def test_refuses_a_prod_cut_off_an_alpha_branch(self, repo):
        git(repo, "checkout", "-b", "alpha/0.3.0")
        r = run_release(repo, "app", "0.3.0")
        assert r.returncode != 0
        assert "main" in (r.stderr + r.stdout).lower()

    def test_refuses_a_prod_cut_off_an_unrelated_branch(self, repo):
        git(repo, "checkout", "-b", "feature/x")
        r = run_release(repo, "app", "0.3.0")
        assert r.returncode != 0

    def test_refuses_a_rc_suffix(self, repo):
        r = run_release(repo, "app", "0.3.0-rc")
        assert r.returncode != 0
        assert "semver" in (r.stderr + r.stdout).lower()

    def test_refuses_an_uppercase_suffix(self, repo):
        r = run_release(repo, "app", "0.3.0-ALPHA")
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

        # The release commit touches the version declarations plus the
        # regenerated changelog.
        files = set(git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split())
        assert files == {
            "VERSION",
            "backend/app/version.py",
            "backend/pyproject.toml",
            "frontend/package.json",
            "CHANGELOG.md",
        }

        # The release commit carries the section for this tag (the fixture
        # CHANGELOG.md was committed, so the script prepends to it). The
        # fixture-entry marker is the fake git-cliff's signature -- real
        # git-cliff would render commit subjects -- so its presence proves
        # the hermetic fake ran, not a real ~/.cache binary.
        changelog = (repo / "CHANGELOG.md").read_text()
        assert "## [0.2.0]" in changelog
        assert "- fixture entry" in changelog
        assert "## [0.1.0]" not in changelog

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
        # Replace the real bump_version.py with a fake that actually mutates
        # VERSION's content (simulating partial progress -- a file genuinely
        # written mid-run), prints that one path, and then exits nonzero, as
        # a future --verbose bump_version.py might on a mid-run failure.
        #
        # The mutation matters: a fake that prints "VERSION" without touching
        # its content leaves `git commit` with nothing to commit, so the
        # commit fails on its own regardless of whether release.sh's `set -e`
        # actually caught bump_version.py's exit code. That made the original
        # version of this test pass even against the buggy process-
        # substitution script -- it was accidentally asserting that a no-op
        # commit fails, not that the script aborted before trying.
        fake = (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "with open('VERSION', 'w') as f:\n"
            "    f.write('9.9.9\\n')\n"
            "print('VERSION')\n"
            "sys.exit(1)\n"
        )
        # Committed, not left as a dirty-tree edit: the fixture repo starts
        # clean, and release.sh's own preflight refuses to run at all on a
        # dirty tree. Leaving this swap uncommitted would trip that check
        # before bump_version.py ever runs, which is a different refusal
        # than the one this test means to exercise.
        bump_path = repo / "ops" / "lib" / "bump_version.py"
        bump_path.write_text(fake)
        bump_path.chmod(0o755)
        git(repo, "add", "--", "ops/lib/bump_version.py")
        git(repo, "commit", "-m", "swap in fake bump_version.py")

        before_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        before_tags = set(git(repo, "tag", "-l").stdout.split())
        before_remote_tags = git(repo, "ls-remote", "--tags", "origin").stdout

        r = run_release(repo, "app", "0.2.0")

        assert r.returncode != 0

        # The fake script's own file write happens regardless of release.sh:
        # it runs before bump_version.py exits, so VERSION on disk is 9.9.9.
        # That's expected and not what proves the fix -- it only proves the
        # fake did its job.
        assert (repo / "VERSION").read_text() == "9.9.9\n"

        # What's load-bearing: release.sh must abort BEFORE `git add`/`git
        # commit`/`git tag`/`git push` ever run, despite the mutated VERSION
        # sitting in the working tree ready to be staged. No new commit, no
        # new local tag, nothing new pushed to origin.
        assert git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
        assert set(git(repo, "tag", "-l").stdout.split()) == before_tags
        assert git(repo, "ls-remote", "--tags", "origin").stdout == before_remote_tags


class TestStagedReleases:
    def test_alpha_cut_creates_the_stage_branch_and_pushes_it(self, repo):
        r = run_release(repo, "app", "0.3.0-alpha")
        assert r.returncode == 0, r.stderr

        # Tag on the new stage branch, not on main.
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "alpha/0.3.0"
        tags = git(repo, "tag", "-l").stdout.split()
        assert "v0.3.0-alpha" in tags
        subject = git(repo, "log", "-1", "--format=%s").stdout.strip()
        assert subject == "release: v0.3.0-alpha"

        # The tag's commit carries the suffixed version, not the bare one.
        assert (repo / "VERSION").read_text() == "0.3.0-alpha\n"

        # Branch and tag both reached origin.
        remote = git(repo, "ls-remote", "--heads", "--tags", "origin").stdout
        assert "refs/heads/alpha/0.3.0" in remote
        assert "v0.3.0-alpha" in remote

        # The operator's checkout is on the stage branch.
        assert git(repo, "status", "--porcelain", "--branch").stdout.splitlines()[0] \
            .endswith("alpha/0.3.0")

    def test_beta_cut_chains_from_the_alpha_branch(self, repo):
        # Simulate the alpha having been cut: switch to the alpha branch and
        # add a beta-worthy fix on it (the fixes flow into beta with it).
        git(repo, "switch", "-c", "alpha/0.3.0")
        (repo / "VERSION").write_text("0.3.0-alpha\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "fix: alpha feedback")

        r = run_release(repo, "app", "0.3.0-beta")
        assert r.returncode == 0, r.stderr

        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "beta/0.3.0"
        assert "v0.3.0-beta" in git(repo, "tag", "-l").stdout.split()
        assert (repo / "VERSION").read_text() == "0.3.0-beta\n"

        remote = git(repo, "ls-remote", "--heads", "--tags", "origin").stdout
        assert "refs/heads/beta/0.3.0" in remote
        assert "v0.3.0-beta" in remote

    def test_prod_quick_patch_cut_from_main(self, repo):
        # A one-line patch does not need staging: bare cuts from main stay
        # the quick path, landing on a release/ branch.
        r = run_release(repo, "app", "0.2.1")
        assert r.returncode == 0, r.stderr

        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "release/0.2.1"
        assert "v0.2.1" in git(repo, "tag", "-l").stdout.split()
        remote = git(repo, "ls-remote", "--heads", "--tags", "origin").stdout
        assert "refs/heads/release/0.2.1" in remote
        assert "v0.2.1" in remote

    def test_prod_cut_from_beta(self, repo):
        # The full gauntlet: beta/0.3.0 graduates to production.
        git(repo, "switch", "-c", "beta/0.3.0")
        (repo / "VERSION").write_text("0.3.0-beta\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "chore: beta stabilization")

        r = run_release(repo, "app", "0.3.0")
        assert r.returncode == 0, r.stderr

        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "release/0.3.0"
        assert "v0.3.0" in git(repo, "tag", "-l").stdout.split()
        assert (repo / "VERSION").read_text() == "0.3.0\n"
        remote = git(repo, "ls-remote", "--heads", "--tags", "origin").stdout
        assert "refs/heads/release/0.3.0" in remote
        assert "v0.3.0" in remote

    def test_reuses_a_stage_branch_left_at_head_by_a_failed_cut(self, repo):
        # A previous cut that died after switching branches (e.g. between
        # switching and pushing) leaves alpha/0.3.0 at HEAD. The retry must
        # proceed, not die on "branch exists".
        git(repo, "switch", "-c", "alpha/0.3.0")
        r = run_release(repo, "app", "0.3.0-alpha")
        assert r.returncode == 0, r.stderr
        assert "v0.3.0-alpha" in git(repo, "tag", "-l").stdout.split()

    def test_refuses_a_stage_branch_left_at_a_different_commit(self, repo):
        # The stage branch exists but points at a commit the operator's
        # checkout is not on -- re-cutting from here would publish a tag at a
        # different tree than the one in front of the operator. (Standing ON
        # the branch means HEAD is the branch tip, so the at-HEAD check
        # cannot fire there -- the operator's own tree is always usable.)
        git(repo, "switch", "-c", "alpha/0.3.0")
        (repo / "NOTES.md").write_text("extra work\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "extra work on the stage branch")
        git(repo, "switch", "main")
        r = run_release(repo, "app", "0.3.0-alpha")
        assert r.returncode != 0
        assert "alpha/0.3.0" in (r.stderr + r.stdout).lower()

    def test_refuses_recutting_an_older_stage(self, repo):
        # The tree already says 0.3.0 (production). Cutting 0.3.0-alpha again
        # sorts lower under `sort -V` and must refuse on the ordering check.
        (repo / "VERSION").write_text("0.3.0\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "prod at 0.3.0")
        r = run_release(repo, "app", "0.3.0-alpha")
        assert r.returncode != 0
        assert "greater" in (r.stderr + r.stdout).lower()
