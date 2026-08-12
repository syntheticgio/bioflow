"""`worktree-up.sh --list` and `--prune`: the machine-wide subcommands.

These exercise the two pure decisions the subcommands rest on, without a
Docker daemon: which slugs count as *live* (so pruning spares them), and
whether the destructive path refuses to run unattended.

The slug question is the one worth testing hard. `--prune` deletes stacks and
their volumes, and it decides what to delete by asking which slugs a live
worktree occupies -- so a slug the derivation misses is a stack somebody is
using being torn down underneath them. The detached-HEAD case below is not
hypothetical: two worktrees on the machine this was written on are detached,
and an earlier draft that read only `branch` lines classified both as
orphaned.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "worktree-up.sh"


def sh(script: str, cwd: Path) -> subprocess.CompletedProcess:
    """Runs a bash snippet with the script's functions sourced.

    The script exits early on `--list`/`--prune` and does real work otherwise,
    so tests source only the function definitions: everything above the
    dispatch `case`, which is where `normalize_slug` and `live_slugs` live.
    """
    text = SCRIPT.read_text()
    marker = "# --list and --prune are dispatched here"
    assert marker in text, "dispatch marker moved; update this test"
    preamble = text.split(marker)[0]

    # Drop the `set -e` line: these snippets probe behaviour and a non-zero
    # grep is expected, not fatal.
    preamble = preamble.replace("set -euo pipefail", "set -uo pipefail")

    return subprocess.run(
        ["bash", "-c", preamble + "\n" + script],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A real git repo with a branch worktree and a detached-HEAD worktree."""
    if shutil.which("git") is None:
        pytest.skip("git is not available")

    main = tmp_path / "main"
    main.mkdir()
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(main), *a], check=True, capture_output=True
    )
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (main / "README").write_text("x")
    run("add", "README")
    run("commit", "-qm", "init")

    run("worktree", "add", "-q", "-b", "feat/my-thing", str(tmp_path / "wt-branch"))
    # Detached: no `branch` line in --porcelain output at all.
    run("worktree", "add", "-q", "--detach", str(tmp_path / "242-detached"))
    return main


class TestNormalizeSlug:
    def test_lowercases_and_replaces_separators(self, repo):
        r = sh('normalize_slug "Feat/My-Thing"', repo)
        assert r.stdout == "feat-my-thing\n"

    def test_emits_exactly_one_trailing_newline(self, repo):
        # Callers consume this as a line-per-slug stream and match with
        # `grep -qxF`. Without the newline every slug runs together into one
        # unmatchable line, which fails toward "everything looks orphaned" --
        # i.e. toward deleting live stacks.
        r = sh('normalize_slug "a"; normalize_slug "b"', repo)
        assert r.stdout == "a\nb\n"

    def test_strips_leading_and_trailing_separators(self, repo):
        r = sh('normalize_slug "/leading/and/trailing/"', repo)
        assert r.stdout == "leading-and-trailing\n"


class TestLiveSlugs:
    def test_includes_a_branch_worktrees_branch_slug(self, repo):
        r = sh(f'MAIN_ROOT="{repo}" live_slugs', repo)
        assert "feat-my-thing" in r.stdout.split("\n")

    def test_includes_a_detached_worktrees_directory_slug(self, repo):
        # `up` falls back to `basename "$WT_ROOT"` when the branch is empty,
        # so this is the slug a detached worktree's stack is actually named
        # after. Missing it makes a live stack look orphaned.
        r = sh(f'MAIN_ROOT="{repo}" live_slugs', repo)
        assert "242-detached" in r.stdout.split("\n")

    def test_includes_the_directory_slug_for_branch_worktrees_too(self, repo):
        # Both candidates are emitted per worktree. They only ever spare a
        # stack from pruning, so an extra slug can at worst leave an orphan
        # for the next run, while a missing one deletes live work.
        r = sh(f'MAIN_ROOT="{repo}" live_slugs', repo)
        assert "wt-branch" in r.stdout.split("\n")

    def test_every_line_is_a_bare_slug(self, repo):
        r = sh(f'MAIN_ROOT="{repo}" live_slugs', repo)
        lines = [ln for ln in r.stdout.split("\n") if ln]
        assert lines, "expected at least one slug"
        for line in lines:
            assert "/" not in line and " " not in line, line


class TestPruneSafety:
    def test_declines_when_not_a_terminal(self):
        # subprocess gives no TTY, which is the CI/non-interactive shape.
        # `--prune` must never assume yes for a destructive set operation.
        r = subprocess.run(
            [str(SCRIPT), "--prune"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        # Either there was nothing to prune, or it declined -- never removed.
        assert "Nothing removed." in out or "nothing to prune" in out, out
        assert "Removing " not in out, out

    def test_dry_run_removes_nothing(self):
        r = subprocess.run(
            [str(SCRIPT), "--prune", "--dry-run"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "Removing " not in out, out

    def test_rejects_an_unknown_prune_option(self):
        r = subprocess.run(
            [str(SCRIPT), "--prune", "--force"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        assert r.returncode != 0
        assert "Unknown option" in (r.stdout + r.stderr)


class TestDownProjectGuard:
    """`down_project` is the one function here that deletes things."""

    def test_refuses_the_main_project(self, repo):
        r = sh('down_project "biopipe"', repo)
        assert r.returncode != 0
        assert "Refusing to act on" in (r.stdout + r.stderr)

    def test_refuses_a_project_outside_the_worktree_prefix(self, repo):
        # --prune acts on a set, so a bug in the filter could otherwise reach
        # an unrelated Compose project.
        r = sh('down_project "some-other-app"', repo)
        assert r.returncode != 0
        assert "Refusing to act on" in (r.stdout + r.stderr)
