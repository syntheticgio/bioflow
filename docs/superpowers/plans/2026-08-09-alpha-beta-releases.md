# Staged alpha/beta Releases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `ops/release.sh` cut alpha and beta pre-releases (`0.3.0-alpha` / `0.3.0-beta`) onto their own `alpha/X.Y.Z` / `beta/X.Y.Z` branches with matching tags, `--prerelease` GitHub releases, and per-stage changelog sections — without ever letting a pre-release move the `:latest` image tag.

**Architecture:** The version suffix IS the stage; there are no stage flags. `release.sh` derives the target branch from the suffix, asserts the source branch per stage, creates the stage branch from the current tip (or reuses one at HEAD), bumps, commits, tags, and pushes branch+tag. `cliff.toml` widens its `tag_pattern` so every stage tag is a changelog boundary. CI passes `--prerelease` to `gh release create`/`edit` for suffixed tags. Tests live in `ops/tests/` (host pytest; **not** run in CI) and use a hermetic fake git-cliff so success-path tests never hit the network.

**Tech Stack:** bash (`ops/release.sh`), Python (`ops/lib/bump_version.py`, pytest), TOML (`cliff.toml`), GitHub Actions YAML (`.github/workflows/publish-images.yml`).

## Global Constraints

- **Suffix is the stage, verbatim:** `0.3.0-alpha` → alpha, `0.3.0-beta` → beta, bare `0.3.0` → production. No stage flags anywhere. A version that says `-beta` while a flag says `alpha` must be unrepresentable.
- **Version regex (bash):** `^[0-9]+\.[0-9]+\.[0-9]+(-alpha|-beta)?$`. Anything else — `-rc`, `-ALPHA`, `v` prefix, build metadata — refuses.
- **Version regex (Python):** `^\d+\.\d+\.\d+(-alpha|-beta)?$`.
- **Stage branch table** (source must be on → target):

  | New version | Must be on | Creates / uses | Push includes |
  |---|---|---|---|
  | `X.Y.Z-alpha` | `main` | `alpha/X.Y.Z` | branch + `vX.Y.Z-alpha` |
  | `X.Y.Z-beta` | `alpha/X.Y.Z` | `beta/X.Y.Z` | branch + `vX.Y.Z-beta` |
  | `X.Y.Z` | `main` or `beta/X.Y.Z` | `release/X.Y.Z` | branch + `vX.Y.Z` |

  Branch names carry the bare version (no suffix): `alpha/0.3.0`, not `alpha/0.3.0-alpha`.
- **Suffix written into all four app declarations:** VERSION, `backend/app/version.py`, `backend/pyproject.toml`, `frontend/package.json` all say `0.3.0-alpha` during alpha. Forced by `check_tag_matches_version.sh`, which compares tag↔tree exactly.
- **`:latest` must not move for pre-release tags.** Both current mechanisms are safe by construction; do not loosen them. The launcher line is untouched by this work.
- **Each stage tag is a changelog boundary** (`cliff.toml` `tag_pattern` widened identically in the skip parser so tag messages don't leak).
- **Ordering:** `sort -V` already orders `0.2.6 < 0.3.0-alpha < 0.3.0-beta < 0.3.0`. The existing "not equal / must be greater" checks keep working; re-cutting an older stage (bare `0.3.0` when VERSION is `0.3.0-alpha`) still refuses.
- **Test runner:** `/opt/homebrew/bin/pytest` from the repo root (the brew pytest; system python3 has none). `ops/tests/` is host-run only — not in CI.
- **Changelog bootstrap cache:** git-cliff is cached at `$XDG_CACHE_HOME/bioflow-tools/git-cliff-2.13.1/git-cliff`; bootstrap returns early when `[ -x ]`. Tests point `XDG_CACHE_HOME` at a fixture dir containing a fake executable there.

---

### Task 1: `bump_version.py` accepts `-alpha` / `-beta`

**Files:**
- Modify: `ops/lib/bump_version.py:19` (the `SEMVER` regex)
- Test: `ops/tests/test_bump_version.py` (add to `TestAppLine` and `TestValidation`)

**Interfaces:**
- Consumes: nothing (this is the leaf change).
- Produces: `bump_version.py <app|launcher> <version> --root <dir>` accepts `X.Y.Z-alpha` / `X.Y.Z-beta` for the app line; still rejects everything else.

- [ ] **Step 1: Write the failing tests**

Append to `ops/tests/test_bump_version.py`:

```python
    def test_writes_a_prerelease_version(self, app_tree):
        r = run_bump(app_tree, "app", "0.3.0-alpha")
        assert r.returncode == 0, r.stderr

        assert (app_tree / "VERSION").read_text() == "0.3.0-alpha\n"
        assert '__version__ = "0.3.0-alpha"' in (
            app_tree / "backend" / "app" / "version.py"
        ).read_text()
        assert 'version = "0.3.0-alpha"' in (
            app_tree / "backend" / "pyproject.toml"
        ).read_text()
        assert (
            json.loads((app_tree / "frontend" / "package.json").read_text())["version"]
            == "0.3.0-alpha"
        )
```

Append to `TestValidation`:

```python
    def test_rejects_a_bare_build_metadata_suffix(self, app_tree):
        r = run_bump(app_tree, "app", "0.2.0-rc")
        assert r.returncode != 0

    def test_rejects_an_uppercase_suffix(self, app_tree):
        r = run_bump(app_tree, "app", "0.2.0-ALPHA")
        assert r.returncode != 0

    def test_rejects_a_v_prefixed_version(self, app_tree):
        r = run_bump(app_tree, "app", "v0.2.0")
        assert r.returncode != 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/opt/homebrew/bin/pytest ops/tests/test_bump_version.py -q`
Expected: the two new `TestValidation` tests and `test_writes_a_prerelease_version` FAIL (`'0.3.0-alpha' is not semver`); the pre-existing tests still pass.

- [ ] **Step 3: Widen the regex**

In `ops/lib/bump_version.py`, change line 19:

```python
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-alpha|-beta)?$")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/opt/homebrew/bin/pytest ops/tests/test_bump_version.py -q`
Expected: all pass.

- [ ] **Step 5: Lock in the tag guard for suffixed tags**

The guard (`ops/check_tag_matches_version.sh`) is prefix-agnostic — `EXPECTED="${TAG#v}"` yields `0.3.0-alpha` for `v0.3.0-alpha`, which matches the suffixed VERSION — so it needs **no code change**. It gets lock-in tests instead, which fail if anyone ever narrows it back to bare versions.

Append to `TestAppTags` in `ops/tests/test_tag_guard.py`:

```python
    def test_accepts_a_matching_prerelease_tag(self, tree):
        (tree / "VERSION").write_text("0.3.0-alpha\n")
        r = run_guard(tree, "v0.3.0-alpha")
        assert r.returncode == 0, r.stderr

    def test_rejects_a_prerelease_tag_mismatched_to_the_tree(self, tree):
        (tree / "VERSION").write_text("0.3.0-beta\n")
        r = run_guard(tree, "v0.3.0-alpha")
        assert r.returncode != 0
        assert "0.3.0-alpha" in (r.stdout + r.stderr)
```

Run: `/opt/homebrew/bin/pytest ops/tests/test_tag_guard.py -q`
Expected: all pass — these are regression locks, so they pass against today's guard; the assertion that matters is they keep passing.

- [ ] **Step 6: Commit**

```bash
git add ops/lib/bump_version.py ops/tests/test_bump_version.py ops/tests/test_tag_guard.py
git commit -m "feat(ops): accept -alpha/-beta pre-release versions in bump_version.py (#107)"
```

---

### Task 2: Repair the release-preflight fixture broken by #106's changelog step

**Files:**
- Modify: `ops/tests/test_release_preflight.py` (fixture + `run_release` + one assertion)

**Interfaces:**
- Consumes: nothing.
- Produces: a hermetic fake git-cliff at `$XDG_CACHE_HOME/bioflow-tools/git-cliff-2.13.1/git-cliff` inside the fixture; `run_release(repo, line, version, env=None)` that merges extra env. Every later task's successful app-cut tests depend on this.

**Context:** The success-path test `TestSuccessfulRelease::test_bumps_commits_tags_and_pushes` is **red on main today**. #106 added the changelog step to the app cut: `release.sh` runs `git-cliff --unreleased --tag "$TAG" --prepend CHANGELOG.md`, then greps for the section and adds `CHANGELOG.md` to the release commit. The fixture repo has no `CHANGELOG.md`, so git-cliff dies with `IO error: No such file or directory`, the grep never runs, and the script exits nonzero. It is not in CI (nothing references `ops/tests` in `.github/workflows/`), so the breakage shipped silently. Prove it first, then repair.

- [ ] **Step 1: Prove the pre-existing failure**

Run: `/opt/homebrew/bin/pytest ops/tests/test_release_preflight.py::TestSuccessfulRelease -q`
Expected: `test_bumps_commits_tags_and_pushes` FAILS with `AssertionError: ... IO error: \`No such file or directory (os error 2)\``. The launcher test passes.

- [ ] **Step 2: Make the fixture hermetic and assert the changelog behavior**

In `ops/tests/test_release_preflight.py`:

1. Add `import os` to the imports (next to `import subprocess`).
2. Add a module-level fake script constant near `RELEASE`:

```python
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
  if [ -f CHANGELOG.md ]; then printf '\\n'; fi
  printf '## [%s]\\n\\n- fixture entry\\n' "${tag#v}"
} >> CHANGELOG.md
"""
```

3. Extend `run_release` with an `env` parameter:

```python
def run_release(repo: Path, line: str, version: str, env=None) -> subprocess.CompletedProcess:
    # (existing docstring, unchanged)
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(
        [str(repo / "ops" / "release.sh"), line, version],
        cwd=repo,
        capture_output=True,
        text=True,
        env=run_env,
    )
```

4. In the `repo` fixture, before the final `git(work, "add", "-A")`, install the fake git-cliff and point the cache at it:

```python
    # Hermetic git-cliff: the app cut regenerates CHANGELOG.md (#106), and the
    # script's bootstrap returns early when the cached binary exists -- so a
    # fake at the cache path is what runs. Never hits the network.
    xdg_cache = tmp_path / "xdg-cache"
    git_cliff_dir = xdg_cache / "bioflow-tools" / "git-cliff-2.13.1"
    git_cliff_dir.mkdir(parents=True)
    (git_cliff_dir / "git-cliff").write_text(FAKE_GIT_CLIFF)
    (git_cliff_dir / "git-cliff").chmod(0o755)
    (work / "CHANGELOG.md").write_text("# Changelog\n")
```

5. Update the success-path assertion so the release commit now carries the changelog, and add a changelog-content assertion:

```python
        files = set(git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split())
        assert files == {
            "VERSION",
            "backend/app/version.py",
            "backend/pyproject.toml",
            "frontend/package.json",
            "CHANGELOG.md",
        }

        # The release commit carries the section for this tag (the fixture
        # CHANGELOG.md was committed, so the script prepends to it).
        changelog = (repo / "CHANGELOG.md").read_text()
        assert "## [0.2.0]" in changelog
        assert "## [0.1.0]" not in changelog
```

**Why the fixture stores the fake under a per-test `tmp_path`: `release.sh` computes `GIT_CLIFF_DIR` from `XDG_CACHE_HOME` (falling back to `$HOME/.cache`). Each test gets its own temp cache dir, so no test can see another test's fake, and the real `~/.cache` is never touched.**

- [ ] **Step 3: Run the tests to verify they pass**

Run: `/opt/homebrew/bin/pytest ops/tests/test_release_preflight.py -q`
Expected: all pass — including the previously-red `test_bumps_commits_tags_and_pushes` and `test_app_release_leaves_the_launcher_version_alone` (which also runs a full app cut).

- [ ] **Step 4: Commit**

```bash
git add ops/tests/test_release_preflight.py
git commit -m "test(ops): repair release-preflight fixture broken by the #106 changelog step (#107)"
```

---

### Task 3: Stage derivation and branch guards in the preflight

**Files:**
- Modify: `ops/release.sh` (preflight section)
- Test: `ops/tests/test_release_preflight.py` (add to `TestPreflightRefusals`)

**Interfaces:**
- Consumes: Task 2's fixture (refusals exit before the changelog/bootstrap, so the fake is not strictly needed here, but the fixture is the harness).
- Produces: shell variables `STAGE` (`alpha`|`beta`|`release`), `CORE` (bare version), `TARGET` (`alpha/0.3.0` etc.) that Task 4's bump/push code consumes. The preflight dies with a stage-naming message for every bad source branch.

- [ ] **Step 1: Write the failing refusal tests**

Append to `TestPreflightRefusals` in `ops/tests/test_release_preflight.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/opt/homebrew/bin/pytest ops/tests/test_release_preflight.py::TestPreflightRefusals -q`
Expected: the new tests FAIL. `-rc`/`-ALPHA` fail the existing bare-semver regex (good — but assert it's the *semver* message, which the existing message does not produce; the refusal happens, message differs). The branch tests fail because the current check requires `main` unconditionally: `test_refuses_a_beta_cut_off_main` actually *passes the preflight* today and only dies later, so it fails this test's expectations.

- [ ] **Step 3: Implement stage derivation and branch guards**

In `ops/release.sh`, replace the version and branch checks in the preflight:

```bash
VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+(-alpha|-beta)?$'
[[ "$VERSION" =~ $VERSION_RE ]] \
  || die "'$VERSION' is not semver MAJOR.MINOR.PATCH, optionally -alpha or -beta (no 'v', no -rc)"

[ -z "$(git status --porcelain)" ] \
  || die "working tree is not clean -- commit or stash first"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# The suffix IS the stage. CORE is the bare version the stage branch is named
# after: `alpha/0.3.0`, not `alpha/0.3.0-alpha`.
CORE="${VERSION%-alpha}"
CORE="${CORE%-beta}"
case "$VERSION" in
  *-alpha)
    STAGE="alpha"
    TARGET="alpha/$CORE"
    [ "$BRANCH" = "main" ] \
      || die "an alpha release must be cut from main, not '$BRANCH'"
    ;;
  *-beta)
    STAGE="beta"
    TARGET="beta/$CORE"
    [ "$BRANCH" = "alpha/$CORE" ] \
      || die "a beta release must be cut from alpha/$CORE, not '$BRANCH'"
    ;;
  *)
    STAGE="release"
    TARGET="release/$CORE"
    [ "$BRANCH" = "main" ] || [ "$BRANCH" = "beta/$CORE" ] \
      || die "a production release must be cut from main or beta/$CORE, not '$BRANCH'"
    ;;
esac
```

(Remove the old `[ "$BRANCH" = "main" ] || die "releases are cut from main, not '$BRANCH'"` block.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/opt/homebrew/bin/pytest ops/tests/test_release_preflight.py -q`
Expected: all pass, including the pre-existing `test_refuses_off_main` (bare cut from `feature/x` still refuses — the production message names `main or beta/x`).

- [ ] **Step 5: Commit**

```bash
git add ops/release.sh ops/tests/test_release_preflight.py
git commit -m "feat(ops): derive the release stage from the version suffix and guard the source branch (#107)"
```

---

### Task 4: Create the stage branch, cut from it, and push branch + tag

**Files:**
- Modify: `ops/release.sh` (after the preflight, before/around the push)
- Test: `ops/tests/test_release_preflight.py` (add `TestStagedReleases` class)

**Interfaces:**
- Consumes: Task 3's `STAGE`/`CORE`/`TARGET` variables; Task 2's hermetic fake git-cliff fixture (all these are successful app cuts).
- Produces: stage branches and tags that later tasks' changelog and CI work are validated against; the ordering guard for re-cutting an older stage.

- [ ] **Step 1: Write the failing tests**

Append a new class to `ops/tests/test_release_preflight.py`:

```python
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
        # The same branch, but with extra commits -- re-cutting from it would
        # publish a tag at the wrong tree.
        git(repo, "switch", "-c", "alpha/0.3.0")
        (repo / "NOTES.md").write_text("extra work\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "extra work on the stage branch")
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/opt/homebrew/bin/pytest ops/tests/test_release_preflight.py::TestStagedReleases -q`
Expected: four cut-path tests fail — the script still pushes `main` and never
switches branches — and three already pass before any new code:

- `test_refuses_a_stage_branch_left_at_a_different_commit` passes because
  Task 3's guard refuses any alpha cut from a branch that is not `main` (the
  message names `alpha/0.3.0`, which the test asserts).
- `test_refuses_recutting_an_older_stage` passes because the pre-existing
  ordering check (`0.3.0-alpha` sorts below `0.3.0`) refuses it.
- `test_reuses_a_stage_branch_left_at_head_by_a_failed_cut` fails — it
  expects the cut to *succeed* by reusing the branch, but Task 3's guard
  refuses an alpha cut off `main`; Task 4's code is what makes reuse work.

- [ ] **Step 3: Implement the stage-branch check (preflight) and the switch (post-commit)**

Two edits in `ops/release.sh`. First, add the target-branch usability check to the preflight, right after the stage-derivation `case` block from Task 3:

```bash
# The stage branch must be usable: absent (created below) or already at HEAD
# (a previous cut that died between switching and pushing). One pointing at a
# different commit is a different tree than the operator's checkout, so the
# release must not happen -- checked here, before any bump/commit/tag.
if git rev-parse -q --verify "refs/heads/$TARGET" >/dev/null; then
  [ "$(git rev-parse HEAD)" = "$(git rev-parse "$TARGET")" ] \
    || die "branch $TARGET exists but does not point at HEAD -- inspect it before cutting"
fi
```

Then replace the final push block:

```bash
git push origin main "refs/tags/$TAG"
```

with:

```bash
# Move the release commit onto the stage branch, then push branch and tag
# together: a pushed tag whose commit never landed is a tag CI cannot check out.
if git rev-parse -q --verify "refs/heads/$TARGET" >/dev/null; then
  git switch "$TARGET"            # at HEAD, guaranteed by the preflight
else
  git switch -c "$TARGET"         # from the current tip (the release commit)
fi
git push -u origin "refs/heads/$TARGET" "refs/tags/$TAG"
```

(`git switch` and `git switch -c` require git ≥ 2.23, 2019 — fine on this machine and in the fixture. The preflight's clean-tree check guarantees the switch cannot be blocked by uncommitted work. `-u` tracks the new branch so the operator's `git pull` works while on the stage branch.)

Also update the header comment block of `ops/release.sh` (lines 4–8) to show the staged examples:

```bash
#   ops/release.sh app 0.2.0        -> tag v0.2.0, branch release/0.2.0
#   ops/release.sh app 0.3.0-alpha  -> tag v0.3.0-alpha, branch alpha/0.3.0
#   ops/release.sh app 0.3.0-beta   -> tag v0.3.0-beta, branch beta/0.3.0
#   ops/release.sh launcher 0.1.1   -> tag launcher-v0.1.1
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/opt/homebrew/bin/pytest ops/tests/test_release_preflight.py -q`
Expected: all pass, including the new `TestStagedReleases` and every earlier class.

- [ ] **Step 5: Commit**

```bash
git add ops/release.sh ops/tests/test_release_preflight.py
git commit -m "feat(ops): cut staged releases onto alpha/beta/release branches (#107)"
```

---

### Task 5: Make every stage tag a changelog boundary

**Files:**
- Modify: `cliff.toml` (two lines)

**Interfaces:**
- Consumes: nothing.
- Produces: `vX.Y.Z-alpha` / `vX.Y.Z-beta` tags act as git-cliff boundaries; tag messages for those tags are skipped as entries. Consumed by every real release cut (the fake in Task 2 bypasses git-cliff entirely, so these tests do not exercise this).

- [ ] **Step 1: Widen `tag_pattern`**

In `cliff.toml`, find the line:

```toml
tag_pattern = "^v[0-9]+\.[0-9]+\.[0-9]+$"
```

Replace it with:

```toml
tag_pattern = "^v[0-9]+\.[0-9]+\.[0-9]+(-alpha|-beta)?$"
```

- [ ] **Step 2: Widen the tag-message skip parser**

Find the commit-parser entry that skips annotated tag messages:

```toml
{ message = "^v[0-9]+\.[0-9]+\.[0-9]+$", skip = true },
```

Replace it with:

```toml
{ message = "^v[0-9]+\.[0-9]+\.[0-9]+(-alpha|-beta)?$", skip = true },
```

(The annotated tag message — `"v0.3.0-alpha"` — is a git-cliff "commit" that is not a real commit; without the widened skip parser it leaks into the changelog exactly as the bare `"v0.2.6"` messages did before #106.)

- [ ] **Step 3: Verify boundary behavior with the real git-cliff against a scratch repo**

Run (from the repo root; uses the cached real binary — do not rely on the fixture fake):

```bash
GIT_CLIFF="$HOME/.cache/bioflow-tools/git-cliff-2.13.1/git-cliff"
CLIFF_CONFIG="$(pwd)/cliff.toml"
TMP="$(mktemp -d)"
cd "$TMP"
git init -q -b main
git config user.email t@e.co && git config user.name T
printf '# scratch\n' > README.md
git add -A && git commit -qm "feat(ui): alpha work"
git tag -a v0.3.0-alpha -m "v0.3.0-alpha"
printf 'x\n' >> README.md
git add -A && git commit -qm "fix(api): beta tweak"
git tag -a v0.3.0-beta -m "v0.3.0-beta"
printf 'y\n' >> README.md
git add -A && git commit -qm "chore: prep release"
"$GIT_CLIFF" --config "$CLIFF_CONFIG" --unreleased --tag v0.3.0 --prepend CHANGELOG.md
cat CHANGELOG.md
```

Expected:
- The `## [0.3.0]` section lists **only** `chore: prep release`.
- `## [0.3.0-beta]` lists only the beta tweak, `## [0.3.0-alpha]` only the alpha work — i.e. the alpha and beta tags are boundaries, and no `v0.3.0-alpha`/`v0.3.0-beta` tag-message lines appear as entries.
- Clean up: `rm -rf "$TMP"`.

- [ ] **Step 4: Re-run the preflight suite (fake-git-cliff path unaffected)**

Run: `/opt/homebrew/bin/pytest ops/tests/test_release_preflight.py -q`
Expected: all pass (the fixture fake bypasses `cliff.toml`, so this just confirms no regression).

- [ ] **Step 5: Commit**

```bash
git add cliff.toml
git commit -m "feat(ops): treat every stage tag as a changelog boundary (#107)"
```

---

### Task 6: `--prerelease` for stage tags in the publish workflow, plus the stale comment

**Files:**
- Modify: `.github/workflows/publish-images.yml` (release job, ~lines 320–340; metadata comment, ~lines 209–212)

**Interfaces:**
- Consumes: the tag naming from Tasks 3–4 (`vX.Y.Z-alpha` / `vX.Y.Z-beta`).
- Produces: GitHub releases for stage tags marked `prerelease`; the alpha/beta images still published (build/manifest jobs untouched), `:latest` untouched.

- [ ] **Step 1: Add the `--prerelease` flag**

In the `release` job, the block:

```yaml
          gh release create "$TAG" \
            --repo "${{ github.repository }}" \
            --title "BioFlow ${version}" \
            --notes-file "$notes_file" \
          || gh release edit "$TAG" \
            --repo "${{ github.repository }}" \
            --title "BioFlow ${version}" \
            --notes-file "$notes_file"
```

becomes:

```yaml
          # A stage tag (vX.Y.Z-alpha / vX.Y.Z-beta) is a prerelease; a bare
          # tag is not. The empty expansion when $PRERELEASE is unset is the
          # word-splitting the flag needs; both branches of set -euo pipefail
          # stay safe because the variable is always set.
          PRERELEASE=""
          case "$TAG" in
            *-alpha|*-beta) PRERELEASE="--prerelease" ;;
          esac
          gh release create "$TAG" \
            --repo "${{ github.repository }}" \
            --title "BioFlow ${version}" \
            --notes-file "$notes_file" \
            $PRERELEASE \
          || gh release edit "$TAG" \
            --repo "${{ github.repository }}" \
            --title "BioFlow ${version}" \
            --notes-file "$notes_file" \
            $PRERELEASE
```

- [ ] **Step 2: Fix the stale `latest` comment**

The comment at lines 209–212:

```yaml
      # `latest` on a push to main; on a `v*` tag, the bare version plus
      # `latest` again (metadata-action's default `latest=auto` flavor moves it
      # for semver tags). `type=sha` gives every build a permanent handle even
      # after `latest` moves off it.
```

is wrong for pre-releases (and predates them). Replace with:

```yaml
      # `latest` on a push to main only (`type=raw` is gated on
      # is_default_branch, so a tag run never triggers it). On a `v*` tag, the
      # bare version plus `latest` again for bare versions only: metadata-action's
      # default `latest=auto` never moves `latest` for pre-release versions, so
      # a vX.Y.Z-alpha/-beta run tags the version and the sha handle but leaves
      # `latest` alone. `type=sha` gives every build a permanent handle even
      # after `latest` moves off it.
```

- [ ] **Step 3: Verify**

1. Read the edited block and confirm the `case`/`esac` and the two `$PRERELEASE` continuations sit correctly inside the job's `run: |` script.
2. Confirm no other workflow step references `latest` behavior that this change affects: `grep -n "latest" .github/workflows/publish-images.yml` should show only the metadata comment, the `type=raw,value=latest` line, and any unrelated matches.
3. The workflow's trigger `tags: ['v*']` and the version-guard already accept suffixed tags (`check_tag_matches_version.sh` is prefix-agnostic); confirm by reading the guard's script — no change needed.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/publish-images.yml
git commit -m "feat(ci): mark stage-tag releases as prereleases and correct the latest-tag comment (#107)"
```

---

### Task 7: Operator docs (VERSION.md, AGENTS.md, CLAUDE.md, Makefile)

**Files:**
- Modify: `VERSION.md`, `AGENTS.md`, `CLAUDE.md`, `Makefile`

**Interfaces:** none — documentation only.

- [ ] **Step 1: `VERSION.md` — "Cutting a release"**

After the existing `make release VERSION=0.2.0` block, add the stage commands:

```markdown
The same command cuts a staged release — the `-alpha` / `-beta` suffix picks
the stage, and the script creates the stage branch for you:

```bash
make release VERSION=0.3.0-alpha   # from main            -> alpha/0.3.0, tag v0.3.0-alpha
make release VERSION=0.3.0-beta    # from alpha/0.3.0     -> beta/0.3.0,  tag v0.3.0-beta
make release VERSION=0.3.0         # from main or beta/0.3.0 -> release/0.3.0, tag v0.3.0
```

Each cut leaves your checkout on the stage branch it created, with the branch
pushed and tracked. A stage tag is a GitHub *prerelease*; fixes found in alpha
are PR'd into `alpha/X.Y.Z` and merge back to `main`, same for beta. A bad
alpha is fixed forward with a new alpha or beta — never by deleting the tag
(see "Fixing a bad release").
```

Also drop the now-false "pre-release versions are not supported" note if it
still appears in `VERSION.md`'s header (grep for `pre-release` and `-alpha`).

- [ ] **Step 2: `VERSION.md` — "What it refuses"**

Replace the bullets:

```markdown
- the version is not bare `MAJOR.MINOR.PATCH`
- you are not on `main`
```

with:

```markdown
- the version is not `MAJOR.MINOR.PATCH` with an optional `-alpha` / `-beta`
  suffix
- you are not on the stage's source branch — `main` for an alpha, `alpha/X.Y.Z`
  for a beta, `main` or `beta/X.Y.Z` for a production cut
- the stage branch (`alpha/X.Y.Z`) exists but does not point at `HEAD` —
  meaning it has commits your checkout does not
```

And replace the paragraph after the list ("The `main` check is a hard refusal
even though...") with a note that the branch rule is per-stage, by design.

- [ ] **Step 3: `VERSION.md` — "Fixing a bad release"**

Add a paragraph on pre-release tags, before the existing "Release forward" note:

```markdown
A bad alpha or beta is fixed forward with a new pre-release of the same or
next version, not by deleting the stage tag: images were published under it
and the GitHub release exists (marked prerelease). The same delete-only-if-
nothing-published rule applies — check `gh release list` and GHCR before
deleting any tag, stage or not.
```

- [ ] **Step 4: `AGENTS.md` and `CLAUDE.md` — methodology gap note**

Find the sentence in each file's "Release methodology" section that says the
script cannot cut `-alpha` / `-beta` tags (the `ops/release.sh` currently
**rejects** sentence with the `#107` link). Replace it with a short statement
that `release.sh` now accepts the suffixes and cuts onto the stage branches:

```markdown
`ops/release.sh` cuts the alpha and beta tags itself — the `-alpha` / `-beta`
suffix selects the stage, and the script creates `alpha/X.Y.Z` / `beta/X.Y.Z`
from the current stage's tip (see `VERSION.md`).
```

- [ ] **Step 5: `Makefile` — usage comment**

Update the `release` target's help/usage strings to mention the suffix:

```makefile
release: ## Cut an app release: make release VERSION=0.2.0 (or VERSION=0.3.0-alpha for a stage)
	@test -n "$(VERSION)" || (echo "usage: make release VERSION=0.2.0 (stage: VERSION=0.3.0-alpha)"; exit 2)
	./ops/release.sh app $(VERSION)
```

- [ ] **Step 6: Verify**

Run: `/opt/homebrew/bin/pytest ops/tests/test_release_preflight.py -q`
Expected: all pass (docs only, but the suite is the gate for the branch).
Also grep for leftovers: `grep -rn "cannot cut\|rejects.*-alpha\|pre-release versions are not supported" VERSION.md AGENTS.md CLAUDE.md` — expect no matches.

- [ ] **Step 7: Commit**

```bash
git add VERSION.md AGENTS.md CLAUDE.md Makefile
git commit -m "docs: staged release commands, per-stage branch rules, and pre-release fix guidance (#107)"
```

---

### Task 8: Full verification pass

**Files:** none.

- [ ] **Step 1: Whole ops suite**

Run: `/opt/homebrew/bin/pytest ops/tests -q`
Expected: every test in `test_bump_version.py`, `test_tag_guard.py`, `test_release_preflight.py` passes.

- [ ] **Step 2: Version ordering, explicitly**

Run:

```bash
printf '0.2.6\n0.3.0-alpha\n0.3.0-beta\n0.3.0\n0.3.1-alpha\n' | sort -V
```

Expected:

```
0.2.6
0.3.0-alpha
0.3.0-beta
0.3.0
0.3.1-alpha
```

- [ ] **Step 3: Manual happy-path cut against a scratch remote**

Run (no real pushes; a throwaway origin). `<repo-root>` is this worktree's
path; the copy makes the scratch repo run this branch's scripts:

```bash
TMP="$(mktemp -d)"
git init --bare -b main "$TMP/origin.git"
git clone -q "$TMP/origin.git" "$TMP/work"
cd "$TMP/work"
git config user.email t@e.co && git config user.name T
mkdir -p backend/app frontend ops/lib
printf '0.1.0\n' > VERSION
printf '__version__ = "0.1.0"\n' > backend/app/version.py
printf '[project]\nversion = "0.1.0"\n' > backend/pyproject.toml
printf '{\n  "version": "0.1.0"\n}\n' > frontend/package.json
printf '# Changelog\n' > CHANGELOG.md
cp <repo-root>/cliff.toml cliff.toml
cp <repo-root>/ops/release.sh ops/release.sh
cp <repo-root>/ops/lib/bump_version.py ops/lib/bump_version.py
chmod +x ops/release.sh
git add -A && git commit -qm "initial"
git push -q -u origin main
./ops/release.sh app 0.3.0-alpha   # from main -> alpha/0.3.0, operator left on it
./ops/release.sh app 0.3.0-beta    # from alpha/0.3.0 -> beta/0.3.0
./ops/release.sh app 0.3.0         # from beta/0.3.0 -> release/0.3.0
git log --all --oneline --graph
git ls-remote --heads --tags origin
```

Expected: three release commits, one per stage; `origin` carries
`alpha/0.3.0`, `beta/0.3.0`, `release/0.3.0` and the three tags
`v0.3.0-alpha`, `v0.3.0-beta`, `v0.3.0`; VERSION reads `0.3.0` at the end; each
tag's commit carries a `CHANGELOG.md` with its own section. Clean up: `rm -rf "$TMP"`.

- [ ] **Step 4: `:latest` immobility — documented, then observed**

The workflow's two `latest` mechanisms are safe by construction and this
branch does not change them (Task 6 only fixes the comment):
1. `type=raw,value=latest,enable={{is_default_branch}}` — a tag run has
   `is_default_branch=false`, so `latest` is not in the tag set.
2. `latest=auto` — metadata-action v5 does not treat pre-release semver
   (`0.3.0-alpha`) as a `latest` candidate.

Verify claim 1 by reading the workflow (done in Task 6). Verify claim 2 on the
first real alpha cut: after `vX.Y.Z-alpha` runs, `docker buildx imagetools
inspect ghcr.io/<ns>/bioflow-web:latest` must still show the previous
production digest, while `:0.3.0-alpha` and `:sha-…` exist. Do not gate this
PR's merge on that observation — it happens downstream of the first real cut.

- [ ] **Step 5: PR hygiene**

Run: `git log --oneline origin/main..HEAD`
Expected: eight commits, one per task, each with a `#107` reference, subjects
matching the plan. Confirm the diff touches only the planned files:
`git diff --stat origin/main`.

---

## Self-Review Notes

- **Spec coverage:** every "Decision" and "Mechanics" section maps to a task —
  suffix regex (T1, T3), suffix-written-verbatim (T4's assertions + T1),
  branch auto-creation + recovery (T4), prod-from-main-or-beta (T3 guard + T4
  tests), `--prerelease` (T6), `latest` immobility (T6 comment + T8 step 4),
  changelog boundaries (T5), docs (T7), VERSION.md refusal/fix sections (T7),
  `check_tag_matches_version.sh` untouched (prefix-agnostic, verified in T6
  step 3), launcher line untouched (T1/T4 leave it alone; existing test still
  passes).
- **Pre-existing red, owned here:** Task 2 documents and repairs the
  #106-introduced fixture breakage that the success-path tests were silently
  failing on main. Without it, no stage-cut success test could be added.
- **Hermeticity:** success-path tests use the fake git-cliff via
  `XDG_CACHE_HOME`; only Task 5's manual scratch-repo step touches the real
  cached binary, and only Task 8 step 3 touches a real (throwaway) git
  remote.
