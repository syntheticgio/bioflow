# Combined App + Launcher Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `make release VERSION=X.Y.Z` cut one release carrying both the container images and the launcher bundles, under one `v` tag, while keeping a constrained launcher-only escape hatch.

**Architecture:** `VERSION` becomes the single source of truth. `bump_version.py` gains a `tauri.conf.json` writer (the file Tauri actually reads, and the cause of a live mislabelling bug) and the `app` line calls the launcher bump as well. `release.sh`'s launcher path re-bases its version check on `VERSION` and rejects pre-releases. The launcher's CI build becomes a reusable `workflow_call` workflow with two callers: the renamed `release.yml` for `v*` tags, and `release-launcher.yml` for `launcher-v*` tags.

**Tech Stack:** Bash, Python 3 (stdlib only), pytest, GitHub Actions, Tauri v2.

**Spec:** [`docs/superpowers/specs/2026-08-12-combined-release-design.md`](../specs/2026-08-12-combined-release-design.md) — requirement IDs below (CR-*, LR-*, TG-*, PB-*, DC-*) refer to it.

---

## Context an implementer needs before starting

**Run the tests like this**, from the repo root. These are host-side tests of shell and Python tooling — they do *not* go through the `api` container, unlike the rest of this repo's backend suite:

```bash
python3 -m pytest ops/tests/ -q
```

**The bug being fixed.** `launcher/src-tauri/tauri.conf.json` contains a hardcoded `"version": "0.1.0"`. Tauri reads that key in preference to `Cargo.toml`, and `bump_version.py` has never written it, so every launcher bundle published so far is named `0.1.0` regardless of its tag. Task 1 fixes it. Do not "tidy" this by deleting the key from `tauri.conf.json` to force the Cargo.toml fallback — the schema treats it as the canonical field and the fallback is a Tauri implementation detail.

**Two existing tests assert the behaviour this plan deliberately reverses.** They are not broken tests to work around; they encode the old two-version-line design and must be rewritten:
- `ops/tests/test_bump_version.py::TestAppLine::test_does_not_touch_the_launcher_line`
- `ops/tests/test_release_preflight.py::TestSuccessfulRelease::test_app_release_leaves_the_launcher_version_alone`

**Version vocabulary used throughout:** the *full* version may carry a suffix (`0.5.0-alpha`); the *core* version never does (`0.5.0`). `tauri.conf.json` always receives the core version (CR-4); every other file receives the full one (CR-3).

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `ops/lib/bump_version.py` | Modify | Writes every version declaration. Gains `tauri.conf.json`; `bump_app` gains the launcher files. |
| `ops/release.sh` | Modify | Git ceremony + preflight. Launcher path re-bases on `VERSION`, rejects suffixes. |
| `ops/check_tag_matches_version.sh` | Modify | Tag guard. Gains the two-launcher-files-agree check (TG-5). |
| `ops/tests/test_bump_version.py` | Modify | CR-2, CR-3, CR-4, CR-7, LR-1, LR-6. |
| `ops/tests/test_release_preflight.py` | Modify | LR-2, LR-3, plus the inverted overwrite test. |
| `ops/tests/test_tag_guard.py` | Modify | TG-3, TG-5. |
| `.github/workflows/launcher-build.yml` | Create | Reusable launcher build. Builds and uploads artifacts; creates no release. |
| `.github/workflows/release.yml` | Rename from `publish-images.yml` | Owns the `v*` release: images + launcher + one GitHub release. |
| `.github/workflows/release-launcher.yml` | Modify | Launcher-only publish path; delegates its build to the reusable workflow. |
| `Makefile` | Modify | Help text for the two release targets. |
| `VERSION.md` | Modify | DC-1, DC-2, DC-3. |
| `CLAUDE.md` | Modify | Release methodology section references one combined command. |

---

## Task 1: `tauri.conf.json` joins the launcher bump

Fixes the live `0.1.0` defect and satisfies CR-2 (launcher half), CR-4, LR-1.

**Files:**
- Modify: `ops/lib/bump_version.py`
- Test: `ops/tests/test_bump_version.py`

- [ ] **Step 1: Write the failing tests**

In `ops/tests/test_bump_version.py`, extend the `launcher_tree` fixture to include the file, replacing the existing fixture:

```python
@pytest.fixture
def launcher_tree(tmp_path):
    (tmp_path / "launcher" / "src-tauri").mkdir(parents=True)
    (tmp_path / "launcher" / "src-tauri" / "Cargo.toml").write_text(
        '[package]\nname = "bioflow-launcher"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (tmp_path / "launcher" / "package.json").write_text(
        json.dumps({"name": "bioflow-launcher", "version": "0.1.0"}, indent=2) + "\n"
    )
    (tmp_path / "launcher" / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps(
            {
                "productName": "BioFlow Launcher",
                "version": "0.1.0",
                "identifier": "com.bioflow.launcher",
            },
            indent=2,
        )
        + "\n"
    )
    return tmp_path
```

Add these tests to `class TestLauncherLine`:

```python
    def test_writes_tauri_conf_json(self, launcher_tree):
        """The file Tauri actually reads for the bundle filename.

        Regression test for the defect where launcher-v0.2.0 shipped bundles
        named 0.1.0: tauri.conf.json overrides Cargo.toml and was never bumped.
        """
        r = run_bump(launcher_tree, "launcher", "0.1.1")
        assert r.returncode == 0, r.stderr

        conf = json.loads(
            (launcher_tree / "launcher" / "src-tauri" / "tauri.conf.json").read_text()
        )
        assert conf["version"] == "0.1.1"

    def test_tauri_conf_keeps_its_other_keys(self, launcher_tree):
        run_bump(launcher_tree, "launcher", "0.1.1")
        conf = json.loads(
            (launcher_tree / "launcher" / "src-tauri" / "tauri.conf.json").read_text()
        )
        assert conf["productName"] == "BioFlow Launcher"
        assert conf["identifier"] == "com.bioflow.launcher"

    def test_tauri_conf_gets_the_core_version_on_a_prerelease(self, launcher_tree):
        """CR-4: the macOS CFBundleShortVersionString must stay numeric."""
        r = run_bump(launcher_tree, "launcher", "0.5.0-alpha")
        assert r.returncode == 0, r.stderr

        conf = json.loads(
            (launcher_tree / "launcher" / "src-tauri" / "tauri.conf.json").read_text()
        )
        assert conf["version"] == "0.5.0"
        assert 'version = "0.5.0-alpha"' in (
            launcher_tree / "launcher" / "src-tauri" / "Cargo.toml"
        ).read_text()
        assert (
            json.loads((launcher_tree / "launcher" / "package.json").read_text())["version"]
            == "0.5.0-alpha"
        )

    def test_reports_tauri_conf_as_written(self, launcher_tree):
        """release.sh git-adds exactly what this prints, so an unlisted file
        would be bumped but left out of the release commit."""
        r = run_bump(launcher_tree, "launcher", "0.1.1")
        assert "tauri.conf.json" in r.stdout

    def test_missing_tauri_conf_is_an_error(self, tmp_path):
        (tmp_path / "launcher" / "src-tauri").mkdir(parents=True)
        (tmp_path / "launcher" / "src-tauri" / "Cargo.toml").write_text(
            '[package]\nversion = "0.1.0"\n'
        )
        (tmp_path / "launcher" / "package.json").write_text('{\n  "version": "0.1.0"\n}\n')
        r = run_bump(tmp_path, "launcher", "0.1.1")
        assert r.returncode != 0
        assert "tauri.conf.json" in r.stderr
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m pytest ops/tests/test_bump_version.py -q -k "tauri"
```

Expected: 5 failures. `test_writes_tauri_conf_json` fails with `assert '0.1.0' == '0.1.1'` — the file exists in the fixture but nothing writes it.

- [ ] **Step 3: Implement the core-version helper and the writer**

In `ops/lib/bump_version.py`, add after `_replace_json_version`:

```python
def _core_version(version: str) -> str:
    """The version with any pre-release suffix removed.

    tauri.conf.json feeds the macOS CFBundleShortVersionString, which must be
    numeric-only -- a `-alpha` there risks a build failure or a bundle macOS
    rejects. Every other declaration keeps the full string.
    """
    for suffix in ("-alpha", "-beta"):
        if version.endswith(suffix):
            return version[: -len(suffix)]
    return version
```

Then replace `bump_launcher` with:

```python
def bump_launcher(root: Path, version: str) -> list[Path]:
    cargo = root / "launcher" / "src-tauri" / "Cargo.toml"
    package = root / "launcher" / "package.json"
    tauri_conf = root / "launcher" / "src-tauri" / "tauri.conf.json"
    for p in (cargo, package, tauri_conf):
        if not p.exists():
            fail(f"missing {p}")

    _replace_first_version(cargo, version)
    _replace_json_version(package, version)
    # Tauri reads this in preference to Cargo.toml for the bundle filename and
    # the macOS Info.plist. It went unbumped until #335, which is why
    # launcher-v0.1.0 and launcher-v0.2.0 both shipped bundles named 0.1.0.
    _replace_json_version(tauri_conf, _core_version(version))
    return [cargo, package, tauri_conf]
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python3 -m pytest ops/tests/test_bump_version.py -q
```

Expected: all pass, including the pre-existing launcher and app tests.

- [ ] **Step 5: Commit**

```bash
git add ops/lib/bump_version.py ops/tests/test_bump_version.py
git commit -m "fix(launcher): bump tauri.conf.json so bundles carry the release version

Tauri reads tauri.conf.json's version key in preference to Cargo.toml, and
bump_version.py never wrote it -- so launcher-v0.1.0 and launcher-v0.2.0 both
published bundles named 0.1.0. Pre-releases get the core version there,
because the macOS CFBundleShortVersionString it feeds must stay numeric."
```

---

## Task 2: A combined cut bumps the launcher too

Satisfies CR-2, CR-3, CR-7. Inverts one existing test.

**Files:**
- Modify: `ops/lib/bump_version.py`
- Test: `ops/tests/test_bump_version.py`

- [ ] **Step 1: Replace the test that asserts the old behaviour**

In `ops/tests/test_bump_version.py`, **delete** `TestAppLine::test_does_not_touch_the_launcher_line` entirely:

```python
    def test_does_not_touch_the_launcher_line(self, app_tree, launcher_tree):
        # Both fixtures use tmp_path, so this reads the same tree.
        run_bump(app_tree, "app", "0.2.0")
        cargo = (app_tree / "launcher" / "src-tauri" / "Cargo.toml").read_text()
        assert 'version = "0.1.0"' in cargo
```

Replace it with these, in the same class:

```python
    def test_bumps_the_launcher_line_too(self, app_tree, launcher_tree):
        """#335: one version for both lines. Both fixtures use tmp_path, so
        this reads the same tree."""
        r = run_bump(app_tree, "app", "0.2.0")
        assert r.returncode == 0, r.stderr

        assert 'version = "0.2.0"' in (
            app_tree / "launcher" / "src-tauri" / "Cargo.toml"
        ).read_text()
        assert (
            json.loads((app_tree / "launcher" / "package.json").read_text())["version"]
            == "0.2.0"
        )
        assert (
            json.loads(
                (app_tree / "launcher" / "src-tauri" / "tauri.conf.json").read_text()
            )["version"]
            == "0.2.0"
        )

    def test_overwrites_a_launcher_version_that_is_ahead(self, app_tree, launcher_tree):
        """CR-7: a launcher-only release leaves the launcher ahead; the next
        combined cut reclaims the number rather than preserving the drift."""
        run_bump(app_tree, "launcher", "0.9.0")
        r = run_bump(app_tree, "app", "0.2.0")
        assert r.returncode == 0, r.stderr

        assert 'version = "0.2.0"' in (
            app_tree / "launcher" / "src-tauri" / "Cargo.toml"
        ).read_text()

    def test_app_bump_reports_all_seven_files(self, app_tree, launcher_tree):
        r = run_bump(app_tree, "app", "0.2.0")
        written = [line for line in r.stdout.splitlines() if line.strip()]
        assert len(written) == 7, written

    def test_app_prerelease_gives_tauri_conf_the_core_version(self, app_tree, launcher_tree):
        r = run_bump(app_tree, "app", "0.3.0-alpha")
        assert r.returncode == 0, r.stderr

        assert (app_tree / "VERSION").read_text() == "0.3.0-alpha\n"
        assert 'version = "0.3.0-alpha"' in (
            app_tree / "launcher" / "src-tauri" / "Cargo.toml"
        ).read_text()
        assert (
            json.loads(
                (app_tree / "launcher" / "src-tauri" / "tauri.conf.json").read_text()
            )["version"]
            == "0.3.0"
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m pytest ops/tests/test_bump_version.py -q -k "TestAppLine"
```

Expected: 4 failures. `test_bumps_the_launcher_line_too` fails because `bump_app` returns only the four app files and never touches the launcher tree.

- [ ] **Step 3: Make `bump_app` call the launcher bump**

In `ops/lib/bump_version.py`, replace the final `return` of `bump_app`:

```python
    return [version_file, module, pyproject, package]
```

with:

```python
    # #335: one release, one version. The launcher line has no independent
    # number any more -- a combined cut overwrites whatever it declared, which
    # is what keeps a launcher-only release's drift from surviving.
    return [version_file, module, pyproject, package] + bump_launcher(root, version)
```

`bump_launcher` is defined below `bump_app` in the module. That is fine — the call is resolved at call time, not at definition time.

- [ ] **Step 4: Run the full file to verify**

```bash
python3 -m pytest ops/tests/test_bump_version.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ops/lib/bump_version.py ops/tests/test_bump_version.py
git commit -m "feat(ops): bump the launcher version as part of an app release

One release, one version (#335). A combined cut overwrites whatever the
launcher declared, so a launcher-only release's drift never survives the
next app release."
```

---

## Task 3: The launcher-only cut re-bases on `VERSION`

Satisfies LR-2, LR-3. Inverts one existing test.

**Files:**
- Modify: `ops/release.sh:~150` (the `CURRENT` block) and the preflight
- Test: `ops/tests/test_release_preflight.py`

- [ ] **Step 1: Add `tauri.conf.json` to the `repo` fixture**

In `ops/tests/test_release_preflight.py`, find this line in the `repo` fixture:

```python
    (work / "launcher" / "package.json").write_text('{\n  "version": "0.1.0"\n}\n')
```

Add immediately after it:

```python
    (work / "launcher" / "src-tauri" / "tauri.conf.json").write_text(
        '{\n  "version": "0.1.0"\n}\n'
    )
```

Without this every release in this file fails at the bump step, since Task 1 made the file mandatory.

- [ ] **Step 2: Write the failing tests and invert the stale one**

**Delete** `TestSuccessfulRelease::test_app_release_leaves_the_launcher_version_alone`:

```python
    def test_app_release_leaves_the_launcher_version_alone(self, repo):
        run_release(repo, "app", "0.2.0")
        cargo = (repo / "launcher" / "src-tauri" / "Cargo.toml").read_text()
        assert 'version = "0.1.0"' in cargo
```

Replace it with:

```python
    def test_app_release_bumps_the_launcher_version(self, repo):
        """#335: one commit carries all seven declarations (CR-5)."""
        r = run_release(repo, "app", "0.2.0")
        assert r.returncode == 0, r.stderr

        cargo = (repo / "launcher" / "src-tauri" / "Cargo.toml").read_text()
        assert 'version = "0.2.0"' in cargo

        files = set(git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split())
        assert files == {
            "VERSION",
            "backend/app/version.py",
            "backend/pyproject.toml",
            "frontend/package.json",
            "launcher/src-tauri/Cargo.toml",
            "launcher/package.json",
            "launcher/src-tauri/tauri.conf.json",
            "CHANGELOG.md",
        }
```

Update `test_launcher_line_uses_its_own_tag_prefix` — its version must now exceed `VERSION` (0.1.0), and its file set has grown. Change the call from `"0.1.1"` to `"0.2.1"`, the two `launcher-v0.1.1` strings to `launcher-v0.2.1`, the `release/0.1.1` strings to `release/0.2.1`, and replace its `files ==` assertion with:

```python
        files = set(git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split())
        assert files == {
            "launcher/src-tauri/Cargo.toml",
            "launcher/package.json",
            "launcher/src-tauri/tauri.conf.json",
        }
```

Add a new class after `TestSuccessfulRelease`:

```python
class TestLauncherOnlyConstraints:
    """The escape hatch is constrained so the launcher version never sits
    below the app's -- see the invariant in the #335 design."""

    def test_refuses_a_version_at_or_below_the_app_version(self, repo):
        # VERSION is 0.1.0 in the fixture; the launcher declares 0.1.0 too.
        r = run_release(repo, "launcher", "0.1.0")
        assert r.returncode != 0
        assert "0.1.0" in (r.stdout + r.stderr)

    def test_refuses_a_version_below_the_app_version(self, repo):
        (repo / "VERSION").write_text("0.5.0\n")
        git(repo, "commit", "-am", "bump app")
        r = run_release(repo, "launcher", "0.4.0")
        assert r.returncode != 0
        assert "0.5.0" in (r.stdout + r.stderr)

    def test_accepts_a_version_above_the_app_version(self, repo):
        (repo / "VERSION").write_text("0.5.0\n")
        git(repo, "commit", "-am", "bump app")
        r = run_release(repo, "launcher", "0.5.1")
        assert r.returncode == 0, r.stderr
        assert "launcher-v0.5.1" in git(repo, "tag", "-l").stdout.split()

    def test_refuses_an_alpha_suffix(self, repo):
        r = run_release(repo, "launcher", "0.2.0-alpha")
        assert r.returncode != 0
        assert "pre-release" in (r.stdout + r.stderr).lower()

    def test_refuses_a_beta_suffix(self, repo):
        r = run_release(repo, "launcher", "0.2.0-beta")
        assert r.returncode != 0
        assert "pre-release" in (r.stdout + r.stderr).lower()
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
python3 -m pytest ops/tests/test_release_preflight.py -q -k "LauncherOnly or bumps_the_launcher"
```

Expected: `test_refuses_a_version_at_or_below_the_app_version` fails by *succeeding* (returncode 0) — today the launcher compares against `Cargo.toml`, where 0.1.0 → 0.1.0 is caught, but 0.1.0 against a fixture launcher of 0.1.0 is the equal case. Confirm each of the five new tests fails for the reason you expect before continuing; a test that passes before the change is not testing the change.

- [ ] **Step 4: Implement both refusals**

In `ops/release.sh`, find the launcher branch of the preflight:

```bash
else
  [ "$BRANCH" = "main" ] \
    || die "launcher releases are cut from main, not '$BRANCH'"
fi
```

Replace it with:

```bash
else
  [ "$BRANCH" = "main" ] \
    || die "launcher releases are cut from main, not '$BRANCH'"
  # The escape hatch ships a launcher fix without rebuilding images (#335).
  # It is production-only: staging a component through alpha/beta branches
  # when it has no images to be tested against is ceremony with no payoff.
  case "$VERSION" in
    *-alpha|*-beta)
      die "a launcher-only release cannot be a pre-release -- cut it as a production version, or use 'make release' to include the launcher in a staged app release" ;;
  esac
fi
```

Then find the block reading the current version:

```bash
if [ "$LINE" = "app" ]; then
  CURRENT="$(tr -d '[:space:]' < VERSION)"
else
  CURRENT="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' \
    launcher/src-tauri/Cargo.toml | head -n1)"
fi
[ -n "$CURRENT" ] || die "could not read the current $LINE version"
```

Replace the whole block with:

```bash
# Both lines compare against VERSION (#335). For the launcher that enforces
# the invariant "launcher version >= app version", which is what lets a
# combined cut overwrite the launcher version unconditionally without ever
# rewinding a number that has already shipped in a bundle filename.
CURRENT="$(tr -d '[:space:]' < VERSION)"
[ -n "$CURRENT" ] || die "could not read the current version from VERSION"
```

The existing greater-than check below it needs no change — it already compares `VERSION` against `CURRENT` via `rank_version`. Its failure message says "the current version", which now reads correctly for both lines.

- [ ] **Step 5: Run the whole file to verify**

```bash
python3 -m pytest ops/tests/test_release_preflight.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add ops/release.sh ops/tests/test_release_preflight.py
git commit -m "feat(ops): constrain launcher-only releases to exceed the app version

Keeps the launcher-never-below-app invariant that lets a combined cut
overwrite the launcher version safely (#335), and rejects pre-release
launcher-only cuts, which have no images to be staged against."
```

---

## Task 4: The tag guard checks the two launcher files agree

Satisfies TG-3, TG-5.

**Files:**
- Modify: `ops/check_tag_matches_version.sh`
- Test: `ops/tests/test_tag_guard.py`

- [ ] **Step 1: Write the failing tests**

In `ops/tests/test_tag_guard.py`, extend the `tree` fixture, replacing it wholesale:

```python
@pytest.fixture
def tree(tmp_path):
    (tmp_path / "VERSION").write_text("0.2.0\n")
    (tmp_path / "launcher" / "src-tauri").mkdir(parents=True)
    (tmp_path / "launcher" / "src-tauri" / "Cargo.toml").write_text(
        '[package]\nname = "bioflow-launcher"\nversion = "0.1.1"\nedition = "2021"\n'
    )
    (tmp_path / "launcher" / "src-tauri" / "tauri.conf.json").write_text(
        '{\n  "version": "0.1.1"\n}\n'
    )
    return tmp_path
```

Add a new class at the end of the file:

```python
class TestLauncherFilesAgree:
    """TG-5. The regression test for the defect where tauri.conf.json sat at
    0.1.0 while Cargo.toml tracked the tag: bundles were named 0.1.0 and
    nothing failed, because the guard only ever read Cargo.toml."""

    def test_rejects_a_v_tag_when_tauri_conf_was_not_bumped(self, tree):
        (tree / "VERSION").write_text("0.5.0\n")
        (tree / "launcher" / "src-tauri" / "Cargo.toml").write_text(
            '[package]\nversion = "0.5.0"\n'
        )
        (tree / "launcher" / "src-tauri" / "tauri.conf.json").write_text(
            '{\n  "version": "0.1.0"\n}\n'
        )
        r = run_guard(tree, "v0.5.0")
        assert r.returncode != 0
        assert "tauri.conf.json" in (r.stdout + r.stderr)

    def test_accepts_a_v_tag_when_both_launcher_files_agree(self, tree):
        (tree / "VERSION").write_text("0.5.0\n")
        (tree / "launcher" / "src-tauri" / "Cargo.toml").write_text(
            '[package]\nversion = "0.5.0"\n'
        )
        (tree / "launcher" / "src-tauri" / "tauri.conf.json").write_text(
            '{\n  "version": "0.5.0"\n}\n'
        )
        r = run_guard(tree, "v0.5.0")
        assert r.returncode == 0, r.stderr

    def test_accepts_a_prerelease_where_tauri_conf_holds_the_core_version(self, tree):
        """CR-4 makes this the correct state, not a mismatch."""
        (tree / "VERSION").write_text("0.5.0-alpha\n")
        (tree / "launcher" / "src-tauri" / "Cargo.toml").write_text(
            '[package]\nversion = "0.5.0-alpha"\n'
        )
        (tree / "launcher" / "src-tauri" / "tauri.conf.json").write_text(
            '{\n  "version": "0.5.0"\n}\n'
        )
        r = run_guard(tree, "v0.5.0-alpha")
        assert r.returncode == 0, r.stderr

    def test_rejects_a_launcher_tag_when_tauri_conf_disagrees(self, tree):
        (tree / "launcher" / "src-tauri" / "tauri.conf.json").write_text(
            '{\n  "version": "0.9.9"\n}\n'
        )
        r = run_guard(tree, "launcher-v0.1.1")
        assert r.returncode != 0
        assert "tauri.conf.json" in (r.stdout + r.stderr)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m pytest ops/tests/test_tag_guard.py -q -k "LauncherFilesAgree"
```

Expected: the two `rejects_` tests fail by succeeding — nothing reads `tauri.conf.json` yet. The two `accepts_` tests pass already; that is expected and they guard against an over-strict implementation in the next step.

- [ ] **Step 3: Implement the check**

In `ops/check_tag_matches_version.sh`, replace everything from the `case "$TAG" in` line down to the final `echo`, with:

```bash
# The version the two launcher files must agree on, suffix-stripped: CR-4
# gives tauri.conf.json the core version while Cargo.toml keeps the full one.
core_of() {
  local v="$1"
  v="${v%-alpha}"
  printf '%s\n' "${v%-beta}"
}

read_toml_version() {
  # First `version = "..."` only: [package] is the first table, so a
  # dependency's version further down must not be read instead.
  sed -n 's/^version[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' "$1" | head -n1
}

read_json_version() {
  sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -n1
}

case "$TAG" in
  launcher-v*)
    EXPECTED="${TAG#launcher-v}"
    SOURCE="launcher/src-tauri/Cargo.toml"
    ACTUAL="$(read_toml_version "$SOURCE")"
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

# TG-5: the two launcher declarations must agree with each other, whichever
# tag prefix brought us here. Tauri reads tauri.conf.json in preference to
# Cargo.toml, so a stale one there publishes mislabelled bundles while every
# other check passes -- which is exactly what shipped as launcher-v0.2.0.
# Prefix-independent, so it is checked separately from the tag comparison
# above rather than folded into it.
CARGO="launcher/src-tauri/Cargo.toml"
TAURI_CONF="launcher/src-tauri/tauri.conf.json"
[ -f "$TAURI_CONF" ] || die "missing $TAURI_CONF"

CARGO_VERSION="$(read_toml_version "$CARGO")"
TAURI_VERSION="$(read_json_version "$TAURI_CONF")"
[ -n "$TAURI_VERSION" ] || die "could not read a version from $TAURI_CONF"

CARGO_CORE="$(core_of "$CARGO_VERSION")"
if [ "$CARGO_CORE" != "$TAURI_VERSION" ]; then
  die "$TAURI_CONF says $TAURI_VERSION but $CARGO says $CARGO_VERSION (core $CARGO_CORE) -- Tauri reads tauri.conf.json, so the bundles would be named $TAURI_VERSION"
fi

echo "$TAG matches $SOURCE ($ACTUAL); launcher files agree at $TAURI_VERSION"
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python3 -m pytest ops/tests/test_tag_guard.py -q
```

Expected: all pass, including the pre-existing `TestAppTags` and `TestLauncherTags`.

- [ ] **Step 5: Commit**

```bash
git add ops/check_tag_matches_version.sh ops/tests/test_tag_guard.py
git commit -m "fix(ops): fail a release tag whose launcher version files disagree

Tauri reads tauri.conf.json, not Cargo.toml, so a stale one publishes
bundles labelled with the wrong version while every other check passes.
That is what shipped as launcher-v0.2.0 (#335)."
```

---

## Task 5: Run the full tooling suite and the real bump end-to-end

A checkpoint before touching CI. No new behaviour.

- [ ] **Step 1: Run every ops test**

```bash
python3 -m pytest ops/tests/ -q
```

Expected: all pass. Read the count, not just the exit code.

- [ ] **Step 2: Verify the real tree bumps correctly, without committing**

```bash
python3 ops/lib/bump_version.py app 0.5.0-alpha --root .
```

Expected output: seven paths. Then confirm the two launcher versions:

```bash
grep -m1 '^version' launcher/src-tauri/Cargo.toml && grep -m1 '"version"' launcher/src-tauri/tauri.conf.json
```

Expected: `version = "0.5.0-alpha"` and `"version": "0.5.0"` — the full version in Cargo.toml, the core version in tauri.conf.json.

- [ ] **Step 3: Confirm the guard accepts that state**

```bash
./ops/check_tag_matches_version.sh v0.5.0-alpha
```

Expected: exits 0, printing `launcher files agree at 0.5.0`.

- [ ] **Step 4: Discard the experiment**

```bash
git checkout -- VERSION backend/ frontend/ launcher/
```

Then confirm the tree is clean:

```bash
git status --porcelain
```

Expected: no output. If anything remains, discard it before continuing — a stray bump would end up in the next commit.

---

## Task 6: Extract the launcher build into a reusable workflow

Satisfies D8. No behaviour change yet: same jobs, same triggers, one new caller-shaped seam.

**Files:**
- Create: `.github/workflows/launcher-build.yml`
- Modify: `.github/workflows/release-launcher.yml`

- [ ] **Step 1: Create the reusable workflow**

Create `.github/workflows/launcher-build.yml`. Copy the `macos` and `linux` jobs from `.github/workflows/release-launcher.yml` **verbatim, including every comment** — they document the keychain and notarization behaviour and that reasoning must not be lost in the move. Make exactly these changes to the copies:

- Remove `needs: [version-guard]` from both jobs (the caller owns the guard).
- Leave every `${{ secrets.NAME }}` reference exactly as written. Reusable workflows read `secrets.` the same way — but each one must also be declared in the `on.workflow_call.secrets` block below, or it resolves to an empty string at runtime with no error. The four in use are `CI_KEYCHAIN_PASSWORD`, `APPLE_API_KEY_P8_BASE64`, `APPLE_API_KEY_ID`, and `APPLE_API_ISSUER_ID` (line 114 area of the current file).
- On line 114, replace `APPLE_TEAM_ID: ${{ vars.APPLE_TEAM_ID }}` with `APPLE_TEAM_ID: ${{ inputs.apple_team_id }}`. Repository `vars` are not passed into a called workflow the way `secrets: inherit` can pass secrets, so this has to arrive as an input from each caller.
- On line 130, `if [ "${{ inputs.notarize == false }}" = "true" ]; then` stays as-is — it still reads the declared boolean correctly.
- On line 145, replace `if: ${{ inputs.notarize != false }}` with `if: ${{ inputs.notarize }}`. The input is now a declared boolean with a default of `true`, never null, so the `!= false` guard against an unset dispatch input is no longer needed.

The header of the new file:

```yaml
# The launcher bundle build, as a reusable workflow.
#
# Two callers since #335: release.yml builds the bundles for a combined `v*`
# release, and release-launcher.yml builds them for a launcher-only
# `launcher-v*` release. It was extracted rather than copied because a
# duplicated signing-and-notarization sequence drifts the first time the
# certificate story changes -- and that sequence is the fiddliest thing in
# this repo's CI (see docs/macos-signing.md).
#
# It builds and uploads artifacts. It never creates or edits a GitHub
# release: the caller owns that, so there is exactly one job in any run that
# can publish.
name: Build launcher bundles

on:
  workflow_call:
    inputs:
      notarize:
        description: 'Notarize the macOS bundle (slow; needs Apple credentials)'
        type: boolean
        default: true
      apple_team_id:
        description: 'Apple Developer Team ID (a repo var at the caller)'
        type: string
        required: true
    secrets:
      CI_KEYCHAIN_PASSWORD:
        required: true
      APPLE_API_KEY_P8_BASE64:
        required: true
      APPLE_API_KEY_ID:
        required: true
      APPLE_API_ISSUER_ID:
        required: true

jobs:
  # <macos job copied verbatim from release-launcher.yml, minus `needs:`>
  # <linux job copied verbatim from release-launcher.yml, minus `needs:`>
```

- [ ] **Step 2: Point `release-launcher.yml` at it**

In `.github/workflows/release-launcher.yml`, **delete** the entire `macos:` and `linux:` job bodies and replace them with one calling job:

```yaml
  bundles:
    name: Build bundles
    needs: [version-guard]
    uses: ./.github/workflows/launcher-build.yml
    permissions:
      contents: read
    with:
      notarize: ${{ inputs.notarize != false }}
      apple_team_id: ${{ vars.APPLE_TEAM_ID }}
    secrets:
      CI_KEYCHAIN_PASSWORD: ${{ secrets.CI_KEYCHAIN_PASSWORD }}
      APPLE_API_KEY_P8_BASE64: ${{ secrets.APPLE_API_KEY_P8_BASE64 }}
      APPLE_API_KEY_ID: ${{ secrets.APPLE_API_KEY_ID }}
      APPLE_API_ISSUER_ID: ${{ secrets.APPLE_API_ISSUER_ID }}
```

Then change the `release` job's `needs: [macos, linux]` to:

```yaml
    needs: [bundles]
```

`version-guard` stays exactly as it is. The `release` job's steps stay exactly as they are.

- [ ] **Step 3: Validate the YAML parses**

```bash
python3 -c "import yaml,sys
for f in ['.github/workflows/launcher-build.yml','.github/workflows/release-launcher.yml']:
    yaml.safe_load(open(f)); print(f, 'OK')"
```

Expected: both print `OK`. If `yaml` is missing, run `python3 -m pip install --user pyyaml` first.

- [ ] **Step 4: Confirm no job in the reusable workflow can publish**

```bash
grep -n "gh release" .github/workflows/launcher-build.yml
```

Expected: **no output.** A match means a release-creating step was copied in and two jobs could race to publish.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/launcher-build.yml .github/workflows/release-launcher.yml
git commit -m "refactor(ci): extract the launcher build into a reusable workflow

It gains a second caller in the combined release (#335), and two copies of
the signing and notarization sequence would drift the first time the
certificate story changes."
```

---

## Task 7: The combined release builds and attaches the bundles

Satisfies PB-2, PB-3, PB-4, PB-5, PB-6, PB-7, PB-8, PB-11.

**Files:**
- Rename: `.github/workflows/publish-images.yml` → `.github/workflows/release.yml`
- Modify: the renamed file

- [ ] **Step 1: Rename the workflow file**

```bash
git mv .github/workflows/publish-images.yml .github/workflows/release.yml
```

Change its `name:` line from:

```yaml
name: Publish container images
```

to:

```yaml
name: Release
```

and its `concurrency.group` from `publish-images-${{ github.ref }}` to `release-${{ github.ref }}`.

Update the file's header comment, replacing the first paragraph with:

```yaml
# Builds and publishes the BioFlow container images, builds the native launcher
# bundles, and creates the one GitHub release that carries both (#335).
#
# Two images, not three. `api` and `worker` are the same image with different
# `command:` values (see docker-compose.yml), so they share one published
# `bioflow-backend`. Building them separately would push ~8GB of identical
# layers twice.
#
# Note that a push to `main` publishes nothing: `build` needs `version-guard`,
# which is gated on `refs/tags/v`, so the whole chain skips. Only a `v*` tag
# publishes.
```

- [ ] **Step 2: Add the launcher job**

Insert this job after the `manifest` job and before the `release` job:

```yaml
  # The launcher half of a combined release (#335). Tag-gated: a push to main
  # must never start a slow, notarized desktop build.
  launcher:
    name: Launcher bundles
    needs: [version-guard]
    if: startsWith(github.ref, 'refs/tags/v')
    uses: ./.github/workflows/launcher-build.yml
    permissions:
      contents: read
    with:
      apple_team_id: ${{ vars.APPLE_TEAM_ID }}
    secrets:
      CI_KEYCHAIN_PASSWORD: ${{ secrets.CI_KEYCHAIN_PASSWORD }}
      APPLE_API_KEY_P8_BASE64: ${{ secrets.APPLE_API_KEY_P8_BASE64 }}
      APPLE_API_KEY_ID: ${{ secrets.APPLE_API_KEY_ID }}
      APPLE_API_ISSUER_ID: ${{ secrets.APPLE_API_ISSUER_ID }}
```

It hangs off `version-guard`, not `manifest`, so the launcher build runs in parallel with the image build rather than after it.

- [ ] **Step 3: Rewire the `release` job's gate**

Replace the `release` job's `needs:` and `if:` lines:

```yaml
    needs: manifest
    # Tag-only: this workflow also runs on every push to main.
    if: startsWith(github.ref, 'refs/tags/v')
```

with:

```yaml
    needs: [manifest, launcher]
    # `always()` is what lets this run when `launcher` failed -- PB-5: the
    # images are already public in GHCR by then, so withholding the release
    # page only hides what has already shipped. The explicit manifest check
    # still withholds it when the images failed (PB-6), and the startsWith
    # clause is load-bearing under always(): without it a push to main --
    # where manifest and launcher both skip -- would reach this job.
    if: always() && needs.manifest.result == 'success' && startsWith(github.ref, 'refs/tags/v')
```

- [ ] **Step 4: Download the bundles and attach them**

In the `release` job, insert this step **before** the existing `Publish` step:

```yaml
      - name: Download the launcher bundles
        # continue-on-error, not `if: needs.launcher.result == 'success'`: a
        # launcher build that failed after the macOS leg uploaded still has
        # artifacts worth attaching, and a run with none must not fail here.
        continue-on-error: true
        uses: actions/download-artifact@v8
        with:
          path: bundles
          pattern: launcher-*
          merge-multiple: true
```

Then, inside the `Publish` step's script, find this line:

```bash
            echo "Both serve linux/amd64 and linux/arm64. \`:latest\` now points here."
```

and insert immediately after it:

```bash
            # The linux artifact keeps its bundle/deb, bundle/rpm subpaths
            # (merge-multiple flattens artifact *names*, not the directories
            # inside them), so find files rather than globbing bundles/*,
            # which would hand `gh` a directory to upload and fail.
            assets=()
            if [ -d bundles ]; then
              mapfile -t assets < <(find bundles -type f)
            fi
            if [ "${#assets[@]}" -gt 0 ]; then
              echo
              echo "Native launcher bundles are attached to this release."
            fi
```

Finally, replace the two `gh release` invocations at the end of that step:

```bash
          gh release create "$TAG" \
            --repo "${{ github.repository }}" \
            --title "BioFlow ${version}" \
            --notes-file "$notes_file" \
            $prerelease \
          || gh release edit "$TAG" \
            --repo "${{ github.repository }}" \
            --title "BioFlow ${version}" \
            --notes-file "$notes_file" \
            $prerelease
```

with:

```bash
          # PB-11: idempotent, because the recovery path after a launcher
          # failure is "Re-run failed jobs" on the whole run, which re-runs
          # this job against a release that already exists.
          gh release create "$TAG" \
            --repo "${{ github.repository }}" \
            --title "BioFlow ${version}" \
            --notes-file "$notes_file" \
            $prerelease \
          || gh release edit "$TAG" \
            --repo "${{ github.repository }}" \
            --title "BioFlow ${version}" \
            --notes-file "$notes_file" \
            $prerelease

          if [ "${#assets[@]}" -gt 0 ]; then
            printf '%s\n' "${assets[@]}"
            gh release upload "$TAG" \
              --repo "${{ github.repository }}" \
              --clobber \
              "${assets[@]}"
          else
            echo "::warning::No launcher bundles were attached to ${TAG}. Re-run the failed jobs on this run to build and attach them."
          fi
```

- [ ] **Step 5: Validate the YAML and check the gates**

```bash
python3 -c "import yaml; d=yaml.safe_load(open('.github/workflows/release.yml')); print(sorted(d['jobs'])); print(d['jobs']['release']['needs'])"
```

Expected: the job list includes `launcher`, and `release`'s needs is `['manifest', 'launcher']`.

- [ ] **Step 6: Confirm nothing still references the old filename**

```bash
grep -rn "publish-images" --include=*.yml --include=*.md --include=*.sh . | grep -v CHANGELOG.md
```

Expected: matches in `VERSION.md` and `CLAUDE.md` only — both are updated in Task 8. A match in any `.yml` or `.sh` must be fixed now.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/
git commit -m "feat(ci): publish images and launcher bundles in one release

A v* tag now builds the launcher alongside the images and attaches the
bundles to the single GitHub release it creates (#335). A launcher failure
leaves the release published with images and no bundles rather than
withholding a page that describes images already public in GHCR."
```

---

## Task 8: Documentation

Satisfies DC-1, DC-2, DC-3.

**Files:**
- Modify: `VERSION.md`
- Modify: `CLAUDE.md`
- Modify: `Makefile`

- [ ] **Step 1: Rewrite VERSION.md's opening**

Replace the two-version-lines table and the paragraph under it (from "BioFlow has **two independent version lines.**" through "...no compatibility contract between the two numbers.") with:

```markdown
BioFlow has **one version line.** `VERSION` at the repo root is the source of
truth for the container images and the native launcher alike, and one `vX.Y.Z`
tag publishes both.

| | What it versions |
|---|---|
| `VERSION` | backend + web container images, and the launcher |
| Tag | `v0.5.0` |
| Workflow | `.github/workflows/release.yml` |
| Produces | GHCR images, `.dmg`/`.deb`/`.rpm`, and one GitHub release |

This replaced two independent lines on 2026-08-12 ([#335](https://github.com/syntheticgio/bioflow/issues/335)).
The launcher is re-released on every app release even when nothing in it
changed, which is expected to be the common case and is deliberate: the cost
is a slow build, and the benefit is that a version number means one thing.

**The launcher version is never lower than the app version.** A combined cut
sets them equal; a launcher-only release (below) opens a gap upward. Nothing
moves the launcher below the app, which is what lets a combined cut overwrite
the launcher version without ever rewinding a number that has already shipped
in a bundle filename.

### The launcher-only escape hatch

A launcher fix that needs no image rebuild can ship on its own:

```bash
make release-launcher VERSION=0.5.1
```

It is constrained so the invariant above holds: the version must be **greater
than the current `VERSION`**, and it cannot be a pre-release. It tags
`launcher-v0.5.1`, publishes bundles and no images, and the next combined cut
reclaims the number.

### The file Tauri actually reads

`launcher/src-tauri/tauri.conf.json` carries the version Tauri uses for the
bundle filename and the macOS `Info.plist`; it takes precedence over
`launcher/src-tauri/Cargo.toml`. It went unbumped until #335, so the bundles
attached to `launcher-v0.1.0` and `launcher-v0.2.0` are both named `0.1.0`.
`ops/check_tag_matches_version.sh` now fails any release tag where the two
files disagree.

On a pre-release, `tauri.conf.json` receives the **core** version (`0.5.0` for
`0.5.0-alpha`) because the macOS `CFBundleShortVersionString` derived from it
must be numeric. Every other declaration keeps the full string.
```

- [ ] **Step 2: Update VERSION.md's file tables and CI section**

In the "What each line writes" section, replace the two tables with one:

```markdown
**One command** — `make release VERSION=…` writes all seven:

| File | Consumer |
|---|---|
| `VERSION` | source of truth; read by CI's guard |
| `backend/app/version.py` | **generated** — imported by `main.py` and the version endpoint |
| `backend/pyproject.toml` | none (no Python dist is published) |
| `frontend/package.json` | none (no npm package is published) |
| `launcher/src-tauri/Cargo.toml` | read by CI's guard; Tauri's fallback |
| `launcher/package.json` | none |
| `launcher/src-tauri/tauri.conf.json` | **Tauri reads this** for the bundle filename and `Info.plist` |

`make release-launcher VERSION=…` writes the last three only.
```

In "What CI does", replace the `v*` and `launcher-v*` sections with:

```markdown
**`v*`** → `release.yml`:
1. **version-guard** — fails if the tag disagrees with `VERSION`, or if the two
   launcher version files disagree with each other
2. **build** — backend and web, amd64 and arm64, on native runners
3. **launcher** — macOS `.dmg` (signed, notarized) and Linux `.deb`/`.rpm`, in
   parallel with the image build
4. **manifest** — one multi-arch tag per image; publishes `<version>` and moves `latest`
5. **release** — creates the GitHub release with the image tags in the body and
   the launcher bundles attached

**If the launcher build fails, the release is still created** with the images
and no bundles, and the run goes red. The images are already in GHCR by then,
so withholding the page would only hide what has shipped. Recover with
**Re-run failed jobs** on the run — not a re-run of the launcher job alone,
which would rebuild the artifacts and attach nothing, since the release job
has already completed.

**`launcher-v*`** → `release-launcher.yml`: the escape hatch. Same bundle build
(both workflows call `launcher-build.yml`), its own release, no images.
```

- [ ] **Step 3: Update the CLAUDE.md release table**

In `CLAUDE.md`'s "Release methodology" section, the stage table at line 248 already reads "images and launchers built" for the Production row and needs no change — it describes the new behaviour correctly by accident.

Find this line at line 273 in the same section:

```
`ops/release.sh` now accepts `-alpha` and `-beta` pre-release suffixes and
```

and insert this paragraph before it:

```markdown
**One release covers both the app and the launcher** since
[#335](https://github.com/syntheticgio/bioflow/issues/335). `make release
VERSION=X.Y.Z` bumps and publishes both; the launcher rides along even when
nothing in it changed. `make release-launcher` still exists for a launcher-only
fix, constrained to production versions above the current `VERSION`. See
[VERSION.md](VERSION.md).
```

- [ ] **Step 4: Update the Makefile help text**

Replace the two release target help comments:

```makefile
release: ## Cut an app release: make release VERSION=0.2.0 (also 0.3.0-alpha, 0.3.0-beta)
```

with:

```makefile
release: ## Cut a release (images + launcher): make release VERSION=0.2.0 (also -alpha, -beta)
```

and:

```makefile
release-launcher: ## Cut a launcher release: make release-launcher VERSION=0.1.1
```

with:

```makefile
release-launcher: ## Launcher-only release; must exceed VERSION and be a production version
```

- [ ] **Step 5: Verify no stale references remain**

```bash
grep -rn "two independent version lines\|publish-images.yml\|make release-launcher" VERSION.md CLAUDE.md Makefile
```

Expected: only the `make release-launcher` mentions that Task 8 wrote intentionally. Any `publish-images.yml` or "two independent version lines" match is stale and must be fixed.

- [ ] **Step 6: Commit**

```bash
git add VERSION.md CLAUDE.md Makefile
git commit -m "docs: describe the combined app and launcher release

One version line, one tag, one release (#335), with the launcher-only
escape hatch and its constraints, and the tauri.conf.json precedence that
caused two releases of mislabelled bundles."
```

---

## Task 9: Verify the launcher still builds

The one thing no test in this repo can check, and the spec's named open question: whether Tauri accepts the version strings this plan writes.

- [ ] **Step 1: Install the launcher's dependencies**

```bash
cd launcher && npm ci
```

Expected: completes without error. This is why the check could not be done at design time — `node_modules` is not in the checkout.

- [ ] **Step 2: Bump the tree to a pre-release version, without committing**

From the repo root:

```bash
python3 ops/lib/bump_version.py app 0.5.0-alpha --root .
```

- [ ] **Step 3: Build the launcher and read the bundle filename**

```bash
cd launcher && npm run tauri build 2>&1 | tail -20
```

Expected: the build succeeds and the bundle is named with the **core** version, e.g. `BioFlow Launcher_0.5.0_aarch64.dmg` — not `0.1.0`, which is what today's tree produces, and not `0.5.0-alpha`.

If the build instead **fails** parsing the version, that is still a pass for CR-4's approach: it confirms the suffix-stripping is necessary. Record which happened in the issue.

If the bundle is named `0.1.0`, Task 1 did not take effect — stop and investigate before releasing.

- [ ] **Step 4: Discard the experiment**

```bash
git checkout -- VERSION backend/ frontend/ launcher/ && git status --porcelain
```

Expected: no output. `launcher/node_modules` and `launcher/src-tauri/target` are gitignored, so they will not show up; leave them or delete them as you prefer.

- [ ] **Step 5: Record the result on the issue**

```bash
gh issue comment 335 --body "Verified the launcher builds under the new version scheme: \`npm run tauri build\` on a tree bumped to 0.5.0-alpha produced <BUNDLE FILENAME HERE>. The tauri.conf.json fix is confirmed to reach the bundle filename."
```

Replace the placeholder with the actual filename observed in Step 3.

---

## Task 10: Full suite, PR, and CI

- [ ] **Step 1: Run the ops suite**

```bash
python3 -m pytest ops/tests/ -q
```

Expected: all pass. Read the count.

- [ ] **Step 2: Run the backend suite**

Per CLAUDE.md, inside the container from the main checkout:

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass. Nothing in this plan touches backend code, so this is a regression check — `backend/tests/test_version_consistency.py` is the one that could plausibly react, since it compares `VERSION` against `backend/app/version.py`.

- [ ] **Step 3: Lint the Python that changed**

```bash
docker compose exec api ruff check /app 2>/dev/null || python3 -m ruff check ops/
```

Expected: clean. CI runs `ruff` with an import-order rule (`I001`) that a local run can miss — see CLAUDE.md.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --title "feat(ops): one release for the app and the launcher" --body "$(cat <<'EOF'
## Why

Releasing BioFlow meant two commands, two tags, two workflow runs, and two GitHub releases — and a Releases page where two entries for the same product carried different numbers. This makes `make release VERSION=X.Y.Z` publish the images and the launcher bundles together, under one tag, in one release.

The launcher is re-released even when nothing in it changed. That is deliberate and expected to be the common case: the cost is a slow build, the benefit is that a version number means one thing.

## A live defect this fixes

`launcher/src-tauri/tauri.conf.json` carries a hardcoded `"version"` that Tauri reads in preference to `Cargo.toml`, and `bump_version.py` never wrote it. **Both launcher releases cut so far shipped bundles named `0.1.0`** — `launcher-v0.2.0` has `BioFlow.Launcher_0.1.0_aarch64.dmg` attached. The tag guard missed it because it validated `Cargo.toml`, which is not the file Tauri reads. The guard now fails any release tag where the two launcher files disagree.

Published releases are left alone; rewriting shipped assets is the opposite of the release-forward rule.

## What is kept

`make release-launcher` survives as an escape hatch for a launcher fix that needs no image rebuild, constrained to production versions above the current `VERSION` so the launcher version can never sit below the app's.

## Failure behaviour

A launcher build failure does not withhold the release: the images are already public in GHCR by then, so the release publishes with images and no bundles, and the run goes red. Recovery is "Re-run failed jobs" on the whole run, which the release job is now idempotent against.

## Notes for review

- The launcher build moved into a reusable `launcher-build.yml` with two callers.
- `publish-images.yml` is renamed `release.yml` — it no longer only publishes images.
- Two tests that asserted the old two-line behaviour were inverted rather than deleted.

Closes #335
EOF
)"
```

- [ ] **Step 5: Label the PR**

```bash
gh pr edit --add-label "type:maintenance" --add-label "area:infrastructure"
```

`.github/release.yml` categorizes the notes by label, not by the title's prefix, so an unlabelled PR lands under "Other changes".

- [ ] **Step 6: Watch CI to a conclusion**

```bash
gh pr checks --watch
```

Poll until every check reports pass or fail — a "pending" read seconds after creation is the run not having started, not a reason to stop watching. Also check for conflicts:

```bash
gh pr view --json mergeable,mergeStateStatus
```

`UNSTABLE` means checks are still running; keep waiting. A real conflict means rebase on `origin/main` and push again.

If a check fails, read the job log, apply the minimal fix, push, and re-poll. Do not leave a red check for the user to find.

- [ ] **Step 7: Update the issue**

```bash
gh issue edit 335 --remove-label "status: implementation plan" --add-label "status:ready"
```

Then comment with the PR URL and note that the implementation is complete and awaiting review. Per CLAUDE.md the user merges; do not merge this yourself.

---

## Self-review notes

**Spec coverage.** Every requirement maps to a task: CR-1 (unchanged, covered by existing tests), CR-2/3/4 → Tasks 1–2, CR-5 → Task 3 Step 2's file-set assertion, CR-6 (unchanged), CR-7 → Task 2, CR-8 (unchanged). LR-1 → Task 1, LR-2/LR-3 → Task 3, LR-4/LR-5 (unchanged), LR-6 → Task 1's launcher test. TG-1/TG-2/TG-4 (unchanged, existing tests re-run), TG-3/TG-5 → Task 4. PB-1 (unchanged), PB-2/3/4/5/6/7/8/11 → Task 7, PB-9 → Task 6, PB-10 (unchanged). DC-1/2/3 → Task 8.

**The one thing that cannot be verified before merge.** PB-5 and PB-7 — the release-publishes-anyway-on-launcher-failure path — have no local harness and are not exercised by a green run. They are verified by reading the job graph in Task 7 Step 5. The first cut under this tooling is what actually tests them, and the failure mode if the gate is wrong is either a release that never publishes or one that publishes on failed images. Worth a deliberate look at that `if:` during review.

**Ordering constraint.** Task 3 depends on Task 1: adding `tauri.conf.json` to the `repo` fixture is only correct once the bump requires the file. Running Task 3 first leaves every release test in that file failing.
