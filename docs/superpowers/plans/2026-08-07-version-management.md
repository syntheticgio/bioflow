# Version Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give BioFlow two independent, script-driven version lines — one for the app, one for the launcher — and make the app's version visible at runtime.

**Architecture:** A `VERSION` file at the repo root is the app's source of truth; `launcher/src-tauri/Cargo.toml` is the launcher's. One shell script (`ops/release.sh`) bumps the derived files, commits, tags, and pushes in a single command, so "bumped but not tagged" and "tagged but not bumped" cease to be reachable states. The backend reads a generated `backend/app/version.py` at import time and serves it from a new `/api/v1/version` route, which the About page displays. Both CI workflows gain a guard that fails if the pushed tag disagrees with the committed source of truth.

**Tech Stack:** Bash, Make, Python 3.12 / FastAPI / pytest, React / TypeScript, GitHub Actions.

Spec: [`docs/superpowers/specs/2026-08-07-version-management-design.md`](../specs/2026-08-07-version-management-design.md). Issue: [#53](https://github.com/syntheticgio/bioflow/issues/53).

---

## Context an engineer needs before starting

**Run tests from a worktree with `./backend/run-worktree-tests.sh tests/ -q`**, never `docker compose exec api pytest` — that command tests *main's* code even when run inside a worktree, because the `api` container bind-mounts the main checkout. This is the single most common way to waste an hour here.

**The five current version declarations and who reads them.** Only two have a real consumer; the plan treats the rest as consistency copies:

| File | Line | Real consumer? |
|---|---|---|
| `backend/app/main.py` | 93 | **Yes** — `FastAPI(version=)`, shown at `/docs`. Deleted by Task 4. |
| `launcher/src-tauri/Cargo.toml` | 3 | **Yes** — Tauri bundle filename and macOS `Info.plist`. |
| `backend/pyproject.toml` | 3 | No — no Python dist is published. |
| `frontend/package.json` | 4 | No — no npm package is published. |
| `launcher/package.json` | 4 | No — Tauri reads the Cargo one. |

**Routing.** `api_router` (`backend/app/api/v1/__init__.py:26`) carries `prefix="/api/v1"`, and every router mounted under it declares its own further prefix (`system.py` is `/system`, etc.). `health.py` is mounted separately and unprefixed, which is why `/healthz` is at the root. Neither is a home for a bare `/api/v1/version`, so Task 5 adds a new prefix-less router mounted under `api_router`.

**`publish-images.yml` fires on both `main` pushes and `v*` tags.** Any tag-related step must be guarded with `if: startsWith(github.ref, 'refs/tags/v')` or it will run — and fail — on every push to `main`.

---

## File Structure

**Created:**
- `VERSION` — app source of truth. One line, bare version, no `v`, trailing newline.
- `backend/app/version.py` — generated, committed. Holds `__version__`.
- `backend/app/api/v1/version.py` — the `/api/v1/version` route.
- `ops/release.sh` — preflight, bump, commit, tag, push.
- `ops/lib/bump_version.py` — the file-rewriting half, in Python so TOML/JSON edits are not regex-on-config.
- `ops/check_tag_matches_version.sh` — the CI guard, one script serving both workflows.
- `backend/tests/test_version_consistency.py` — asserts `VERSION` and `version.py` agree.
- `backend/tests/api/test_version_endpoint.py` — the route's test.
- `ops/tests/test_release_preflight.bats`… — **no.** See Task 3: the preflight is tested from pytest, not bats, because this repo has no bats and adding a test runner for one script is not worth it.
- `ops/tests/test_bump_version.py` — bump logic against temp trees.
- `VERSION.md` — the operator guide.

**Modified:**
- `backend/app/main.py:93` — literal replaced by the import.
- `backend/app/api/v1/__init__.py` — register the new router.
- `backend/pyproject.toml:3`, `frontend/package.json:4`, `launcher/package.json:4`, `launcher/src-tauri/Cargo.toml:3` — written by the bump.
- `frontend/src/api/client.ts` — a `getVersion()` call.
- `frontend/src/api/types.ts` — its response type.
- `frontend/src/components/HelpAbout.tsx` — render the version.
- `.github/workflows/publish-images.yml` — guard + release job.
- `.github/workflows/release-launcher.yml` — guard.
- `Makefile` — `release` and `release-launcher` targets.

**Task order rationale:** the source-of-truth files and their consistency test come first (Tasks 1–2), so every later task has something real to read. The bump script (Task 3) is built before the script that calls it. Runtime visibility (4–6) is independent of CI (7–8), and either could be done first; CI is later because it is the hardest to test locally. `VERSION.md` (Task 9) is last because it documents what the earlier tasks actually built rather than what this plan predicted.

---

### Task 1: The `VERSION` file and the generated `version.py`

**Files:**
- Create: `VERSION`
- Create: `backend/app/version.py`
- Test: `backend/tests/test_version_consistency.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_version_consistency.py`:

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

```bash
./backend/run-worktree-tests.sh tests/test_version_consistency.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named 'app.version'`.

- [ ] **Step 3: Create `VERSION`**

The repo's last tag is `v0.1.0`, so the current version is `0.1.0`. This task records where we already are; it does not bump anything.

```bash
printf '0.1.0\n' > VERSION
```

- [ ] **Step 4: Create the generated module**

Create `backend/app/version.py`:

```python
"""BioFlow's version, as a Python import.

GENERATED FILE -- do not edit by hand. `ops/release.sh` rewrites this from
the repo-root `VERSION` file, which is the source of truth. A hand-edit here
is caught by tests/test_version_consistency.py.
"""

__version__ = "0.1.0"
```

- [ ] **Step 5: Run the tests and watch them pass**

```bash
./backend/run-worktree-tests.sh tests/test_version_consistency.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit**

```bash
git add VERSION backend/app/version.py backend/tests/test_version_consistency.py
git commit -m "feat(version): VERSION file and generated version module (#53)"
```

---

### Task 2: The bump library

The half that rewrites files, separated from the git ceremony so it can be tested against temp trees without touching a real repo.

**Files:**
- Create: `ops/lib/bump_version.py`
- Test: `ops/tests/test_bump_version.py`

- [ ] **Step 1: Write the failing test**

Create `ops/tests/test_bump_version.py`:

```python
"""Bumping rewrites every declaration for a line, and nothing else."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

BUMP = Path(__file__).resolve().parents[1] / "lib" / "bump_version.py"


def run_bump(root: Path, line: str, version: str):
    return subprocess.run(
        [sys.executable, str(BUMP), line, version, "--root", str(root)],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def app_tree(tmp_path):
    """A minimal tree with the app line's four declarations."""
    (tmp_path / "VERSION").write_text("0.1.0\n")
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "version.py").write_text('__version__ = "0.1.0"\n')
    (tmp_path / "backend" / "pyproject.toml").write_text(
        '[project]\nname = "biopipe-backend"\nversion = "0.1.0"\ndescription = "x"\n'
    )
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(
        json.dumps({"name": "biopipe-frontend", "private": True, "version": "0.1.0"}, indent=2)
        + "\n"
    )
    return tmp_path


@pytest.fixture
def launcher_tree(tmp_path):
    (tmp_path / "launcher" / "src-tauri").mkdir(parents=True)
    (tmp_path / "launcher" / "src-tauri" / "Cargo.toml").write_text(
        '[package]\nname = "bioflow-launcher"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (tmp_path / "launcher" / "package.json").write_text(
        json.dumps({"name": "bioflow-launcher", "version": "0.1.0"}, indent=2) + "\n"
    )
    return tmp_path


class TestAppLine:
    def test_writes_every_app_declaration(self, app_tree):
        r = run_bump(app_tree, "app", "0.2.0")
        assert r.returncode == 0, r.stderr

        assert (app_tree / "VERSION").read_text() == "0.2.0\n"
        assert '__version__ = "0.2.0"' in (app_tree / "backend" / "app" / "version.py").read_text()
        assert 'version = "0.2.0"' in (app_tree / "backend" / "pyproject.toml").read_text()
        assert json.loads((app_tree / "frontend" / "package.json").read_text())["version"] == "0.2.0"

    def test_leaves_other_fields_intact(self, app_tree):
        run_bump(app_tree, "app", "0.2.0")

        pyproject = (app_tree / "backend" / "pyproject.toml").read_text()
        assert 'name = "biopipe-backend"' in pyproject
        pkg = json.loads((app_tree / "frontend" / "package.json").read_text())
        assert pkg["name"] == "biopipe-frontend"
        assert pkg["private"] is True

    def test_does_not_touch_the_launcher_line(self, app_tree, launcher_tree):
        # Both fixtures use tmp_path, so this reads the same tree.
        run_bump(app_tree, "app", "0.2.0")
        cargo = (app_tree / "launcher" / "src-tauri" / "Cargo.toml").read_text()
        assert 'version = "0.1.0"' in cargo

    def test_only_the_first_version_key_in_cargo_style_toml(self, app_tree):
        """A dependency's `version = ` must never be mistaken for the package's."""
        (app_tree / "backend" / "pyproject.toml").write_text(
            '[project]\nversion = "0.1.0"\n\n[tool.other]\nversion = "9.9.9"\n'
        )
        run_bump(app_tree, "app", "0.2.0")
        text = (app_tree / "backend" / "pyproject.toml").read_text()
        assert 'version = "0.2.0"' in text
        assert 'version = "9.9.9"' in text


class TestLauncherLine:
    def test_writes_cargo_and_package_json(self, launcher_tree):
        r = run_bump(launcher_tree, "launcher", "0.1.1")
        assert r.returncode == 0, r.stderr

        assert 'version = "0.1.1"' in (
            launcher_tree / "launcher" / "src-tauri" / "Cargo.toml"
        ).read_text()
        assert (
            json.loads((launcher_tree / "launcher" / "package.json").read_text())["version"]
            == "0.1.1"
        )


class TestValidation:
    def test_rejects_a_non_semver_version(self, app_tree):
        r = run_bump(app_tree, "app", "0.2")
        assert r.returncode != 0
        assert "semver" in r.stderr.lower()

    def test_rejects_an_unknown_line(self, app_tree):
        r = run_bump(app_tree, "nonsense", "0.2.0")
        assert r.returncode != 0

    def test_rejects_a_missing_file(self, tmp_path):
        r = run_bump(tmp_path, "app", "0.2.0")
        assert r.returncode != 0
        assert "VERSION" in r.stderr
```

- [ ] **Step 2: Run it and watch it fail**

```bash
./backend/run-worktree-tests.sh ../ops/tests/test_bump_version.py -q
```

Expected: every test fails — the script does not exist. If the relative path is awkward from the container, run these particular tests on the host with `python -m pytest ops/tests/ -q`; they use only `tmp_path` and `subprocess`, with no Mongo or app imports.

- [ ] **Step 3: Write the bump library**

Create `ops/lib/bump_version.py`:

```python
#!/usr/bin/env python3
"""Rewrite every version declaration for one release line.

Split out of release.sh so the file-rewriting half can be tested against
temporary trees without a git repo. The git ceremony lives in release.sh.

TOML and JSON are edited textually rather than parsed-and-redumped: a
round-trip through a TOML writer would reformat and strip the comments these
files carry, and `json.dump` would reflow package.json. The substitutions are
anchored tightly enough to be safe -- see `_replace_first_version`.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _replace_first_version(path: Path, version: str) -> None:
    """Rewrite the first `version = "..."` line in a TOML file.

    First only, and line-anchored: `[project]`/`[package]` is the first table
    in both files this touches, so the first such line is the package's own.
    A dependency's `version =` further down -- or a `rust-version` -- must not
    be rewritten, which is why the pattern anchors to line start and requires
    the exact key.
    """
    text = path.read_text()
    pattern = re.compile(r'^version\s*=\s*"[^"]*"', re.MULTILINE)
    if not pattern.search(text):
        fail(f"no `version = \"...\"` line in {path}")
    path.write_text(pattern.sub(f'version = "{version}"', text, count=1))


def _replace_json_version(path: Path, version: str) -> None:
    """Rewrite `"version": "..."` in a package.json, preserving formatting."""
    text = path.read_text()
    pattern = re.compile(r'"version"\s*:\s*"[^"]*"')
    if not pattern.search(text):
        fail(f'no `"version": "..."` key in {path}')
    path.write_text(pattern.sub(f'"version": "{version}"', text, count=1))
    # Parse it back to prove the edit did not corrupt the file.
    try:
        json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        fail(f"{path} is not valid JSON after the edit: {exc}")


def _replace_generated_module(path: Path, version: str) -> None:
    text = path.read_text()
    pattern = re.compile(r'^__version__\s*=\s*"[^"]*"', re.MULTILINE)
    if not pattern.search(text):
        fail(f"no `__version__ = \"...\"` line in {path}")
    path.write_text(pattern.sub(f'__version__ = "{version}"', text, count=1))


def bump_app(root: Path, version: str) -> list[Path]:
    version_file = root / "VERSION"
    if not version_file.exists():
        fail(f"no VERSION file at {version_file}")

    module = root / "backend" / "app" / "version.py"
    pyproject = root / "backend" / "pyproject.toml"
    package = root / "frontend" / "package.json"
    for p in (module, pyproject, package):
        if not p.exists():
            fail(f"missing {p}")

    version_file.write_text(f"{version}\n")
    _replace_generated_module(module, version)
    _replace_first_version(pyproject, version)
    _replace_json_version(package, version)
    return [version_file, module, pyproject, package]


def bump_launcher(root: Path, version: str) -> list[Path]:
    cargo = root / "launcher" / "src-tauri" / "Cargo.toml"
    package = root / "launcher" / "package.json"
    for p in (cargo, package):
        if not p.exists():
            fail(f"missing {p}")

    _replace_first_version(cargo, version)
    _replace_json_version(package, version)
    return [cargo, package]


LINES = {"app": bump_app, "launcher": bump_launcher}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("line", choices=sorted(LINES))
    parser.add_argument("version")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    if not SEMVER.match(args.version):
        fail(f"{args.version!r} is not semver MAJOR.MINOR.PATCH")

    written = LINES[args.line](args.root.resolve(), args.version)
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests and watch them pass**

```bash
python -m pytest ops/tests/test_bump_version.py -q
```

Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add ops/lib/bump_version.py ops/tests/test_bump_version.py
git commit -m "feat(version): bump library for both release lines (#53)"
```

---

### Task 3: The release script

**Files:**
- Create: `ops/release.sh`
- Test: `ops/tests/test_release_preflight.py`

**Note on the test approach:** the preflight is tested by driving `release.sh` against throwaway git repositories built in `tmp_path`, from pytest. This repo has no bats and adding a shell-test runner for one script is not worth the dependency. Each test builds a repo in a known-bad state and asserts the script refuses with a message naming the cause.

- [ ] **Step 1: Write the failing test**

Create `ops/tests/test_release_preflight.py`:

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m pytest ops/tests/test_release_preflight.py -q
```

Expected: every test fails — `ops/release.sh` does not exist.

- [ ] **Step 3: Write the release script**

Create `ops/release.sh` (and `chmod +x` it in Step 4):

```bash
#!/usr/bin/env bash
# Cut a release: bump the version declarations, commit, tag, push.
#
# Bumping and tagging are one command on purpose. Done as two manual steps
# the failure mode is "bumped but never tagged" or "tagged but never bumped",
# and recovering from the second means deleting a pushed tag that CI has
# already acted on. One command makes both states unreachable.
#
#   ops/release.sh app 0.2.0        -> tag v0.2.0
#   ops/release.sh launcher 0.1.1   -> tag launcher-v0.1.1
#
# See VERSION.md for the operator's guide and what CI does with each tag.

set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

usage() {
  cat >&2 <<'EOF'
usage: ops/release.sh <app|launcher> <version>

  app       bump VERSION, backend/app/version.py, backend/pyproject.toml,
            frontend/package.json  -> tag v<version>
  launcher  bump launcher/src-tauri/Cargo.toml, launcher/package.json
            -> tag launcher-v<version>

<version> is bare semver: 0.2.0, not v0.2.0.
EOF
  exit 2
}

[ $# -eq 2 ] || usage
LINE="$1"
VERSION="$2"

case "$LINE" in
  app)      TAG_PREFIX="v" ;;
  launcher) TAG_PREFIX="launcher-v" ;;
  *)        usage ;;
esac

TAG="${TAG_PREFIX}${VERSION}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# --- preflight -------------------------------------------------------------
# Every check refuses rather than warns, and names the precondition it tripped.

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || die "'$VERSION' is not semver MAJOR.MINOR.PATCH (no 'v', no -rc suffix)"

[ -z "$(git status --porcelain)" ] \
  || die "working tree is not clean -- commit or stash first"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] \
  || die "releases are cut from main, not '$BRANCH'"

git rev-parse -q --verify "refs/tags/$TAG" >/dev/null \
  && die "tag $TAG already exists locally"
if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
  die "tag $TAG already exists on origin"
fi

# The current version, read from the line's own source of truth.
if [ "$LINE" = "app" ]; then
  CURRENT="$(tr -d '[:space:]' < VERSION)"
else
  CURRENT="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' \
    launcher/src-tauri/Cargo.toml | head -n1)"
fi
[ -n "$CURRENT" ] || die "could not read the current $LINE version"

# sort -V puts the greater version last; equal versions are caught first.
[ "$VERSION" != "$CURRENT" ] || die "$VERSION is already the current version"
GREATER="$(printf '%s\n%s\n' "$CURRENT" "$VERSION" | sort -V | tail -n1)"
[ "$GREATER" = "$VERSION" ] \
  || die "$VERSION is not greater than the current version $CURRENT"

# --- bump, commit, tag, push ----------------------------------------------

echo "Releasing $LINE $CURRENT -> $VERSION (tag $TAG)"

mapfile -t WRITTEN < <(python3 "$SCRIPT_DIR/lib/bump_version.py" "$LINE" "$VERSION" --root "$REPO_ROOT")
[ "${#WRITTEN[@]}" -gt 0 ] || die "bump wrote no files"

git add -- "${WRITTEN[@]}"
git commit -m "release: $TAG"
git tag -a "$TAG" -m "$TAG"

# Commit and tag together: a pushed tag whose commit never landed is a tag CI
# cannot check out.
git push origin main "refs/tags/$TAG"

echo
echo "Pushed $TAG. CI is now building it -- watch:"
echo "  gh run list --limit 5"
```

- [ ] **Step 4: Make it executable**

```bash
chmod +x ops/release.sh
```

- [ ] **Step 5: Run the tests and watch them pass**

```bash
python -m pytest ops/tests/test_release_preflight.py -q
```

Expected: `9 passed`.

- [ ] **Step 6: Commit**

```bash
git add ops/release.sh ops/tests/test_release_preflight.py
git commit -m "feat(version): release script with preflight refusals (#53)"
```

---

### Task 4: The backend reads its own version

**Files:**
- Modify: `backend/app/main.py:93`
- Test: `backend/tests/test_version_consistency.py` (extend)

- [ ] **Step 1: Add the failing test**

Append to `backend/tests/test_version_consistency.py`:

```python
def test_fastapi_app_reports_the_generated_version():
    """The OpenAPI version is the one place a stale literal was user-visible."""
    from app.main import app

    assert app.version == __version__


def test_main_py_holds_no_hardcoded_version_literal():
    """A second declaration that only exists to be kept in sync is the same
    trap one level down -- main.py must import, not restate."""
    main_py = (REPO_ROOT / "backend" / "app" / "main.py").read_text()
    assert 'version="0.' not in main_py
```

- [ ] **Step 2: Run and watch it fail**

```bash
./backend/run-worktree-tests.sh tests/test_version_consistency.py -q
```

Expected: `test_main_py_holds_no_hardcoded_version_literal` FAILS (the literal is at line 93). `test_fastapi_app_reports_the_generated_version` happens to pass, because both currently read `0.1.0` — which is exactly the coincidence this work removes.

- [ ] **Step 3: Replace the literal with the import**

In `backend/app/main.py`, add to the imports:

```python
from app.version import __version__
```

Then change the `create_app` body (line 93):

```python
    app = FastAPI(
        title="BioFlow",
        description="Local bioinformatics data manager",
        version=__version__,
        lifespan=lifespan,
    )
```

- [ ] **Step 4: Run and watch it pass**

```bash
./backend/run-worktree-tests.sh tests/test_version_consistency.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/tests/test_version_consistency.py
git commit -m "refactor(version): main.py imports the version instead of restating it (#53)"
```

---

### Task 5: The `/api/v1/version` endpoint

**Files:**
- Create: `backend/app/api/v1/version.py`
- Modify: `backend/app/api/v1/__init__.py`
- Test: `backend/tests/api/test_version_endpoint.py`

**Why a new router:** every router under `api_router` declares its own prefix (`system.py` is `/system`), so none of them can host a bare `/api/v1/version`. `health.py` is mounted unprefixed at the root, which is why `/healthz` has no `/api/v1`. A small prefix-less router mounted under `api_router` is the only way to land on the path the spec names.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_version_endpoint.py`:

```python
"""The version endpoint: what the About page and a support conversation read.

The launcher's update check compares image *digests*, not versions, so without
this a user running `:latest` has no way to discover what they have.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.version import __version__

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestVersionEndpoint:
    async def test_returns_the_running_version(self, client):
        r = await client.get("/api/v1/version")

        assert r.status_code == 200
        assert r.json() == {"version": __version__}

    async def test_needs_no_profile_or_auth(self, client):
        """It must answer before anything is configured -- a user asking "what
        am I running?" may be mid-setup with no profile selected."""
        r = await client.get("/api/v1/version")
        assert r.status_code == 200
```

- [ ] **Step 2: Run and watch it fail**

```bash
./backend/run-worktree-tests.sh tests/api/test_version_endpoint.py -q
```

Expected: FAIL with `assert 404 == 200`.

- [ ] **Step 3: Write the router**

Create `backend/app/api/v1/version.py`:

```python
"""What version this instance is running.

Deliberately dependency-free: no profile, no database, no auth. Someone asking
"what am I running?" is often mid-setup or mid-support-conversation, and an
endpoint that needs a working stack to answer is useless in exactly those
cases.

No prefix, unlike its sibling routers -- the path is /api/v1/version, and
api_router supplies the /api/v1 half.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.version import __version__

router = APIRouter(tags=["version"])


class VersionOut(BaseModel):
    version: str


@router.get("/version", response_model=VersionOut)
async def get_version() -> VersionOut:
    return VersionOut(version=__version__)
```

- [ ] **Step 4: Register it**

In `backend/app/api/v1/__init__.py`, add `version` to the import block (it is alphabetised — it goes after `uploads`):

```python
from app.api.v1 import (
    ...
    uploads,
    version,
)
```

and register it at the end of the `include_router` list:

```python
api_router.include_router(settings.router)
api_router.include_router(version.router)
```

- [ ] **Step 5: Run and watch it pass**

```bash
./backend/run-worktree-tests.sh tests/api/test_version_endpoint.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Run the whole suite — nothing else should move**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. Read the count, not the exit code.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/version.py backend/app/api/v1/__init__.py backend/tests/api/test_version_endpoint.py
git commit -m "feat(version): GET /api/v1/version (#53)"
```

---

### Task 6: The version on the About page

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/components/HelpAbout.tsx`

There is no headless component-testing setup in this repo and none is expected; this task is verified in the browser (Step 5).

- [ ] **Step 1: Add the response type**

In `frontend/src/api/types.ts`, add (keeping the file's alphabetical ordering where it has one):

```typescript
export interface VersionInfo {
  version: string;
}
```

- [ ] **Step 2: Add the client call**

In `frontend/src/api/client.ts`, add `VersionInfo` to the type import block at the top, then add the call alongside the other simple GETs:

```typescript
export async function getVersion(): Promise<VersionInfo> {
  return request<VersionInfo>("/version");
}
```

`request` already prefixes `BASE` (`/api/v1`), so the path here is just `/version`.

- [ ] **Step 3: Render it on the About page**

Rewrite `frontend/src/components/HelpAbout.tsx`:

```tsx
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { getVersion } from "../api/client";

/**
 * What BioFlow is, for anyone who lands on this page without the context
 * everyone building it already has.
 */
export function HelpAbout() {
  // Null until it arrives, and null forever if the fetch fails. A version
  // nobody can read is a missing line; a version that throws is a broken
  // page, and this page's job is to explain the app to someone who may
  // already be confused by it.
  const [version, setVersion] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getVersion()
      .then((v) => {
        if (!cancelled) setVersion(v.version);
      })
      .catch(() => {
        /* no version line */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="help-page">
      <h1>About BioFlow</h1>
      {version && <p className="help-version">Version {version}</p>}
      <p className="help-intro">
        A local, single-user web application for managing bioinformatics data
        files — projects, uploads, metadata, and a priority- and load-aware
        background job queue.
      </p>

      <div className="software-prose">
        <p>
          BioFlow runs entirely on your own machine: the file library, the
          job queue, and the database all live locally rather than in a
          hosted service. It's built as the foundation for assigning rich
          metadata to files and launching computations and pipelines —
          alignment, variant calling, and more — against them.
        </p>
        <p>
          See <Link to="/help/software">Software</Link> for what's installed
          and how each tool is used,{" "}
          <Link to="/help/sources">Data Sources</Link> for where reference
          data comes from, and{" "}
          <Link to="/help/calculations">BioFlow Calculations</Link> for what
          the numbers on a file mean.
        </p>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Style the line**

`.help-intro` is defined at `frontend/src/styles.css:2566`. Add directly above it:

```css
.help-version {
  margin: -0.5rem 0 1rem;
  opacity: 0.6;
  font-size: 0.9em;
}
```

- [ ] **Step 5: Verify in the browser**

From this worktree:

```bash
./ops/worktree-up.sh
```

Open `http://localhost:5273/help/about`. Expected: "Version 0.1.0" under the heading. Confirm the endpoint directly too:

```bash
curl -s http://localhost:8100/api/v1/version
```

Expected: `{"version":"0.1.0"}`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts frontend/src/components/HelpAbout.tsx frontend/src/styles.css
git commit -m "feat(version): show the running version on the About page (#53)"
```

---

### Task 7: The CI tag/version agreement guard

**Files:**
- Create: `ops/check_tag_matches_version.sh`
- Modify: `.github/workflows/publish-images.yml`
- Modify: `.github/workflows/release-launcher.yml`
- Test: `ops/tests/test_tag_guard.py`

- [ ] **Step 1: Write the failing test**

Create `ops/tests/test_tag_guard.py`:

```python
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
```

- [ ] **Step 2: Run and watch it fail**

```bash
python -m pytest ops/tests/test_tag_guard.py -q
```

Expected: all fail — the script does not exist.

- [ ] **Step 3: Write the guard**

Create `ops/check_tag_matches_version.sh`:

```bash
#!/usr/bin/env bash
# Fail if a release tag disagrees with the version committed in the tree.
#
# ops/release.sh makes disagreement unreachable, so in normal operation this
# never fires. It exists for the tag typed by hand -- `git tag v0.9.0` against
# a tree that says 0.1.0 publishes images labelled 0.9.0 and a release page
# that lies, with nothing else in the pipeline noticing.
#
#   ops/check_tag_matches_version.sh v0.2.0
#   ops/check_tag_matches_version.sh launcher-v0.1.1

set -euo pipefail

die() { echo "::error::$*"; exit 1; }

[ $# -eq 1 ] || { echo "usage: $0 <tag>" >&2; exit 2; }
TAG="$1"

case "$TAG" in
  launcher-v*)
    EXPECTED="${TAG#launcher-v}"
    SOURCE="launcher/src-tauri/Cargo.toml"
    # First `version = "..."` only: [package] is the first table, so a
    # dependency's version further down must not be read instead.
    ACTUAL="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' "$SOURCE" | head -n1)"
    ;;
  v*)
    EXPECTED="${TAG#v}"
    SOURCE="VERSION"
    ACTUAL="$(tr -d '[:space:]' < "$SOURCE")"
    ;;
  *)
    die "tag '$TAG' has no known release prefix (expected v* or launcher-v*)"
    ;;
esac

[ -n "$ACTUAL" ] || die "could not read a version from $SOURCE"

if [ "$EXPECTED" != "$ACTUAL" ]; then
  die "tag $TAG expects version $EXPECTED but $SOURCE says $ACTUAL -- the tag was probably created by hand instead of by ops/release.sh"
fi

echo "$TAG matches $SOURCE ($ACTUAL)"
```

- [ ] **Step 4: Make it executable and run the tests**

```bash
chmod +x ops/check_tag_matches_version.sh
python -m pytest ops/tests/test_tag_guard.py -q
```

Expected: `6 passed`.

- [ ] **Step 5: Wire it into `publish-images.yml`**

This workflow runs on `main` pushes **and** `v*` tags, so the guard must be tag-only or it fails every push to `main`. Add a job before the others, and make `build` depend on it. Insert directly after the `jobs:` line:

```yaml
jobs:
  # A hand-rolled tag publishes images labelled with a version the tree does
  # not have. ops/release.sh makes that unreachable; this catches the tag
  # someone typed by hand anyway. Tag-only: this workflow also runs on every
  # push to main, where there is no tag to check.
  version-guard:
    name: Tag matches VERSION
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: [self-hosted, X64]
    steps:
      - uses: actions/checkout@v4
      - run: ops/check_tag_matches_version.sh "${{ github.ref_name }}"
```

Then find the `build:` job and add the dependency. It currently has no `needs:`; add one immediately under its `name:` line:

```yaml
    needs: [version-guard]
```

**This is load-bearing and easy to get wrong:** a skipped job (`if:` false on a `main` push) is *not* a failed one, so `needs: [version-guard]` still lets `build` run on `main` pushes. Verify this in Step 8 rather than assuming it.

- [ ] **Step 6: Wire it into `release-launcher.yml`**

That workflow only triggers on `launcher-v*` tags and `workflow_dispatch`, so a dispatch run has no tag. Add a job before `macos`, immediately after the `jobs:` line:

```yaml
jobs:
  version-guard:
    name: Tag matches Cargo.toml
    if: startsWith(github.ref, 'refs/tags/launcher-v')
    runs-on: [self-hosted, X64]
    steps:
      - uses: actions/checkout@v4
      - run: ops/check_tag_matches_version.sh "${{ github.ref_name }}"
```

Then add `needs: [version-guard]` to both the `macos` and `linux` jobs, under each one's `name:` line.

- [ ] **Step 7: Check the YAML parses**

```bash
python -c "import yaml,sys; [yaml.safe_load(open(f)) for f in ['.github/workflows/publish-images.yml','.github/workflows/release-launcher.yml']]; print('both parse')"
```

Expected: `both parse`.

- [ ] **Step 8: Commit**

```bash
git add ops/check_tag_matches_version.sh ops/tests/test_tag_guard.py .github/workflows/publish-images.yml .github/workflows/release-launcher.yml
git commit -m "ci(version): fail a release whose tag disagrees with the tree (#53)"
```

---

### Task 8: A `v*` tag creates a GitHub release

Today `publish-images.yml` pushes images and stops — no release page is created, which is why the issue's completion check is satisfiable for the launcher and not for the app.

**Files:**
- Modify: `.github/workflows/publish-images.yml`

- [ ] **Step 1: Add the release job**

Append to the end of `.github/workflows/publish-images.yml`, at the same indentation as the other jobs (two spaces). It mirrors `release-launcher.yml`'s `release` job, including its `create || upload --clobber` fallback for a re-run against an existing release:

```yaml
  # A v* tag publishes images but, before #53, created nothing a person could
  # link to. This is the half that makes a tag a release rather than just a
  # registry push.
  release:
    name: Create the GitHub release
    needs: manifest
    # Tag-only: this workflow also runs on every push to main.
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: [self-hosted, X64]
    permissions:
      contents: write
    steps:
      - name: Publish
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          TAG: ${{ github.ref_name }}
          NAMESPACE: ${{ env.IMAGE_NAMESPACE }}
        run: |
          set -euo pipefail
          version="${TAG#v}"
          notes_file="$(mktemp)"
          cat > "$notes_file" <<EOF
          Container images published for this release:

          \`\`\`
          ${NAMESPACE}/bioflow-backend:${version}
          ${NAMESPACE}/bioflow-web:${version}
          \`\`\`

          Both serve linux/amd64 and linux/arm64. \`:latest\` now points here.

          Update an existing install with the launcher's Update button, or:

          \`\`\`
          docker compose pull && docker compose up -d
          \`\`\`
          EOF

          gh release create "$TAG" \
            --repo "${{ github.repository }}" \
            --title "BioFlow ${version}" \
            --generate-notes \
            --notes-file "$notes_file" \
          || gh release edit "$TAG" \
            --repo "${{ github.repository }}" \
            --title "BioFlow ${version}" \
            --notes-file "$notes_file"
```

Note `--generate-notes` and `--notes-file` together: GitHub appends its
auto-generated commit list below the supplied body.

- [ ] **Step 2: Check the YAML parses and the job graph is right**

```bash
python -c "
import yaml
w = yaml.safe_load(open('.github/workflows/publish-images.yml'))
jobs = w['jobs']
print('jobs:', list(jobs))
assert jobs['release']['needs'] == 'manifest'
assert jobs['build']['needs'] == ['version-guard']
print('graph ok')
"
```

Expected: the job list including `version-guard`, `build`, `manifest`, `release`, then `graph ok`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/publish-images.yml
git commit -m "ci(version): create a GitHub release for v* tags (#53)"
```

---

### Task 9: `VERSION.md`

**Files:**
- Create: `VERSION.md`
- Modify: `README.md` (link to it)

- [ ] **Step 1: Write it**

Create `VERSION.md`. It must not state the current version number anywhere — that would make it a sixth declaration that goes stale.

````markdown
# Versioning and releases

BioFlow has **two independent version lines.** They never need to agree, and
bumping one does not imply bumping the other.

| | App | Launcher |
|---|---|---|
| What it versions | backend + web container images | the native desktop launcher |
| Source of truth | `VERSION` (repo root) | `launcher/src-tauri/Cargo.toml` |
| Tag | `v0.2.0` | `launcher-v0.2.0` |
| Workflow | `.github/workflows/publish-images.yml` | `.github/workflows/release-launcher.yml` |
| Produces | GHCR images + a GitHub release | `.dmg`, `.deb`, `.rpm` on a GitHub release |

They are independent because a macOS launcher build is signed and notarized —
slow, and dependent on a certificate that expires yearly. There is no reason an
API change should trigger one. The launcher pulls `:latest` images and does no
version negotiation, so there is no compatibility contract between the two
numbers.

## Cutting a release

One command. It bumps, commits, tags, and pushes:

```bash
make release VERSION=0.2.0
```

```bash
make release-launcher VERSION=0.1.1
```

Give the bare version — `0.2.0`, not `v0.2.0`. The script adds the prefix.

Bumping and tagging are deliberately not separable. Done by hand as two steps,
the failure modes are "bumped but never tagged" and "tagged but never bumped",
and recovering from the second means deleting a tag CI has already acted on.

### What it refuses

The script stops, with a message naming the cause, if:

- the version is not bare `MAJOR.MINOR.PATCH`
- the working tree is dirty
- you are not on `main`
- the tag already exists, locally or on `origin`
- the version is not greater than the current one

The `main` check is a hard refusal even though much of this repo's work happens
in worktrees. If it ever obstructs something real, loosening it is a one-line
change in `ops/release.sh` — that is the expected direction, not a design
failure.

## Which number to bump

Ordinary semver, read from the user's side:

- **Patch** (`0.1.0` → `0.1.1`) — bug fixes, performance, docs. Nothing about
  using the app changes.
- **Minor** (`0.1.0` → `0.2.0`) — new pipelines, tools, endpoints, or UI. Old
  projects and data keep working.
- **Major** (`0.9.0` → `1.0.0`) — a migration is required, or something that
  worked before stops working.

Pre-release and `-rc` versions are not supported; the script rejects them.

## What each line writes

Only two of these files have a consumer that would notice if it went stale.
The rest are kept consistent so the tree does not contradict itself.

**App** — `make release VERSION=…`

| File | Consumer |
|---|---|
| `VERSION` | source of truth; read by CI's guard |
| `backend/app/version.py` | **generated** — imported by `main.py` and the version endpoint |
| `backend/pyproject.toml` | none (no Python dist is published) |
| `frontend/package.json` | none (no npm package is published) |

**Launcher** — `make release-launcher VERSION=…`

| File | Consumer |
|---|---|
| `launcher/src-tauri/Cargo.toml` | **source of truth** — Tauri reads it for the bundle filename and macOS `Info.plist` |
| `launcher/package.json` | none |

`backend/app/version.py` is generated and committed. Do not edit it by hand —
`backend/tests/test_version_consistency.py` fails if it drifts from `VERSION`.

## What CI does

Pushing the tag is what starts everything.

**`v*`** → `publish-images.yml`:
1. **version-guard** — fails if the tag disagrees with `VERSION`
2. **build** — backend and web, amd64 and arm64, on native runners
3. **manifest** — one multi-arch tag per image; publishes `<version>` and moves `latest`
4. **release** — creates the GitHub release with the image tags in the body

**`launcher-v*`** → `release-launcher.yml`:
1. **version-guard** — fails if the tag disagrees with `Cargo.toml`
2. **macos** — builds, signs, notarizes the `.dmg`
3. **linux** — builds `.deb` and `.rpm`
4. **release** — attaches all bundles to the GitHub release

A failed version-guard means the tag was created by hand rather than by
`ops/release.sh`. Delete the tag (it has published nothing yet), then use the
`make release` command.

## Verifying a release landed

```bash
gh run list --limit 5
gh release view v0.2.0
```

For the app line, also confirm the images and the running version:

```bash
docker buildx imagetools inspect ghcr.io/syntheticgio/bioflow-backend:0.2.0
```

```bash
curl -s http://localhost:8000/api/v1/version
```

The last one reports what the *running* instance is, which is not the same
question as what was published — it only changes after a `docker compose pull`.
The version is also shown on the About page at `/help/about`.

## Fixing a bad release

**Release forward. Do not move or delete a published tag.**

Once a tag has run, GHCR images and release assets exist downstream of it.
Moving the tag leaves them in place, still labelled with the old version but
built from different code — and anyone who already pulled has something that
does not match what the tag now points at.

The fix is a new patch version:

```bash
make release VERSION=0.2.1
```

Deleting is only correct for a tag whose CI has not yet published anything —
in practice, one that failed the version-guard.
````

- [ ] **Step 2: Link it from the README**

The README has no docs-index section (its headings are `## Quick start`,
`## Background`, `## Security`, `## Required macOS setup`). Add a short line at
the end of the `## Quick start` section, before `## Background` at line 62:

```markdown
Releases and versioning: see [`VERSION.md`](VERSION.md).
```

- [ ] **Step 3: Verify every command in it is real**

Not a formality — this file is the one an operator follows verbatim.

```bash
grep -n "make release" Makefile
ls ops/release.sh ops/check_tag_matches_version.sh backend/app/version.py VERSION
```

Expected: both Make targets exist (added in Task 10 — if you are doing this task first, do Task 10 before this step), and all four paths resolve.

- [ ] **Step 4: Confirm it states no version number**

```bash
grep -nE '\b0\.[0-9]+\.[0-9]+\b' VERSION.md
```

Expected: matches appear **only** in example commands and the semver-bump
explanation, never as a claim about what the current version is. If any line
reads like "BioFlow is currently at X", delete it.

- [ ] **Step 5: Commit**

```bash
git add VERSION.md README.md
git commit -m "docs(version): VERSION.md operator guide (#53)"
```

---

### Task 10: Makefile targets

**Files:**
- Modify: `Makefile`

- [ ] **Step 1: Add the targets**

In `Makefile`, add `release release-launcher` to the `.PHONY` line:

```make
.PHONY: up down logs ps build test test-queue lint shell mongo redis clean check-home release release-launcher
```

Then append at the end of the file:

```make
release: ## Cut an app release: make release VERSION=0.2.0
	@test -n "$(VERSION)" || (echo "usage: make release VERSION=0.2.0"; exit 2)
	./ops/release.sh app $(VERSION)

release-launcher: ## Cut a launcher release: make release-launcher VERSION=0.1.1
	@test -n "$(VERSION)" || (echo "usage: make release-launcher VERSION=0.1.1"; exit 2)
	./ops/release.sh launcher $(VERSION)
```

- [ ] **Step 2: Verify the guard fires without a VERSION**

```bash
make release
```

Expected: `usage: make release VERSION=0.2.0`, exit 2. Nothing is committed or tagged.

- [ ] **Step 3: Verify the script is reached with one**

Run from this worktree, where the branch is not `main` — so the preflight must refuse, which proves the target reaches the script:

```bash
make release VERSION=99.0.0
```

Expected: `error: releases are cut from main, not '<branch>'`. Nothing committed or tagged.

- [ ] **Step 4: Commit**

```bash
git add Makefile
git commit -m "feat(version): make release targets (#53)"
```

---

### Task 11: Full verification

- [ ] **Step 1: Run the whole backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. **Read the count**, not the exit code — the repo's convention is that "green" means the number, and this run should be the pre-existing count plus the tests added by Tasks 1, 4, and 5.

- [ ] **Step 2: Run the ops tests**

```bash
python -m pytest ops/tests/ -q
```

Expected: `24 passed` (9 bump + 9 preflight + 6 guard).

- [ ] **Step 3: Confirm the version is consistent everywhere**

```bash
cat VERSION
grep -n '__version__' backend/app/version.py
grep -n '^version' backend/pyproject.toml
grep -n '"version"' frontend/package.json
```

Expected: all four read the same number.

- [ ] **Step 4: Verify in the running app**

```bash
./ops/worktree-up.sh
```

Then check all three surfaces:

```bash
curl -s http://localhost:8100/api/v1/version
```

Expected: `{"version":"0.1.0"}`. Open `http://localhost:5273/help/about` — the version line shows. Open `http://localhost:8100/docs` — the OpenAPI header shows the same number, now from the import rather than a literal.

- [ ] **Step 5: Tear down the worktree stack**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 6: Merge and push**

Per CLAUDE.md: once the suite is green and `main` is clean, merge and push without asking. If `main` has moved, re-run the suite after merging rather than assuming the green still holds.

```bash
git checkout main && git merge --no-ff - && ./backend/run-worktree-tests.sh tests/ -q && git push origin main
```

- [ ] **Step 7: Update the issue**

```bash
gh issue edit 53 --remove-label "status: implementation plan" --add-label "status:ready"
```

Comment on [#53](https://github.com/syntheticgio/bioflow/issues/53) with what shipped, and note that the completion check ("version is successfully used for a release") is satisfied by construction but **not yet exercised** — no `v*` tag has been cut through the new script. The first real `make release` is what closes it.

---

## First real release (do this after the plan is merged)

The plan builds the machinery; it does not cut a release. From the **main
checkout**, not a worktree:

```bash
make release VERSION=0.2.0
```

`0.2.0` rather than `0.1.1`: this adds a version endpoint and an About-page
surface, which is a minor bump by the rule in `VERSION.md`.

Then verify per VERSION.md's checklist — the run succeeds, the release page
exists, `ghcr.io/syntheticgio/bioflow-backend:0.2.0` resolves and serves both
architectures. **That is what closes #53.**

---

## Self-review notes

Checked against the spec, section by section:

- Two independent version lines → Tasks 1, 2, 3, 10
- Files the bump writes → Task 2 (library) + Task 1 (`version.py` created)
- `main.py` literal deleted, not synced → Task 4, with a test asserting the
  literal is gone rather than merely correct
- The bump script and its preflight refusals → Task 3
- `/api/v1/version` → Task 5
- About page → Task 6
- Tag/VERSION agreement guard, both workflows → Task 7
- GitHub release for `v*` → Task 8
- `VERSION.md` → Task 9
- Testing section → Tasks 1, 2, 3, 5, 7; browser verification in 6 and 11

Two things resolved while writing that the spec had left implicit:

- **The endpoint's router.** The spec named `system.py`, but that router
  carries `prefix="/system"`, which would have produced
  `/api/v1/system/version`. Task 5 creates a prefix-less router instead and
  explains why in the module docstring.
- **`publish-images.yml` runs on `main` pushes too.** Every tag-specific step
  needs an `if:` guard, and `build`'s new `needs: [version-guard]` relies on a
  skipped job not counting as a failed one. Task 7 flags this explicitly
  rather than leaving it to be discovered by a broken `main` build.
