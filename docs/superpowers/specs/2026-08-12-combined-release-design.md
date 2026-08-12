# Combining the launcher release with the app release

**Issue:** [#335](https://github.com/syntheticgio/bioflow/issues/335)
**Date:** 2026-08-12
**Status:** design

## Problem

BioFlow ships two release lines that are cut, tagged, and published
independently: the app (`VERSION` → `v0.4.0` → GHCR images) and the launcher
(`launcher/src-tauri/Cargo.toml` → `launcher-v0.2.0` → `.dmg`/`.deb`/`.rpm`).
Every release is therefore two commands, two tags, two workflow runs, and two
GitHub releases, and a user looking at the Releases page sees two entries that
do not obviously belong to the same version of the same product.

The user's request is one release: `make release VERSION=0.5.0` should publish
images *and* launcher bundles together, and it is acceptable for the launcher
to be re-released with no changes in it — which is expected to be the common
case.

### A defect this work must fix

`launcher/src-tauri/tauri.conf.json` carries a hardcoded `"version": "0.1.0"`.
Tauri reads that key in preference to `Cargo.toml`, and `ops/lib/bump_version.py`
never writes it. Both launcher releases cut so far therefore shipped bundles
named `0.1.0`: the `launcher-v0.2.0` release created on 2026-08-12 has
`BioFlow.Launcher_0.1.0_aarch64.dmg` attached to it.

`ops/check_tag_matches_version.sh` did not catch this because it validates the
tag against `Cargo.toml`, which is not the file Tauri actually reads.

Verified by inspecting the published assets:

```
$ gh release view launcher-v0.2.0 --json assets --jq '.assets[].name'
BioFlow.Launcher-0.1.0-1.x86_64.rpm
BioFlow.Launcher_0.1.0_aarch64.dmg
BioFlow.Launcher_0.1.0_amd64.deb
```

Any combined release that does not write `tauri.conf.json` would keep naming
every bundle `0.1.0` forever, so fixing it is in scope rather than a follow-up.

## Decisions

Recorded with their reasoning, per CLAUDE.md's requirement that a spec survive
its author.

| # | Decision | Why |
|---|---|---|
| D1 | One version number for both lines, sourced from `VERSION` | The user asked for a single release; two numbers on one release page is the confusion being removed. The launcher does no version negotiation against the app (VERSION.md), so nothing depends on them differing. |
| D2 | One tag, `v<version>`, drives both | A second tag per release reintroduces the two-run, two-release shape by another name, and makes two workflows race to create one release. |
| D3 | A launcher-only escape hatch is kept | Requested by the user. A launcher fix that needs no image rebuild should not have to cut a full app release. |
| D4 | A combined cut overwrites the launcher version | One rule, no surviving drift. The alternative — combined cuts patch-bumping an independent launcher line — is today's two-line behaviour under a new name. |
| D5 | A launcher-only cut must exceed the current app version | Keeps the invariant *launcher ≥ app*, which is what makes D4's overwrite always a step forward and never a rewind of an already-published bundle version. |
| D6 | Launcher-only cuts are production-only | The hatch exists to ship a launcher fix. Staging a component through alpha/beta branches when it has no images to be tested against is ceremony with no payoff. |
| D7 | A launcher build failure does not block the app release | Images are already public in GHCR by then, so blocking only withholds the release page that describes them. The macOS bundle depends on a yearly-expiring certificate and an Apple notarization service, neither of which should be able to withhold an image release. |
| D8 | The launcher build becomes a reusable workflow | It gains a second caller under this design. Two copies of a signing-and-notarization sequence drift the first time the certificate story changes. |
| D9 | `tauri.conf.json` receives the bare core version on pre-releases | The macOS `CFBundleShortVersionString` derived from it must be numeric-only; a `-alpha` suffix there risks a build failure or a bundle macOS rejects. |

## The invariant

**The launcher version is never lower than the app version.**

A combined cut sets them equal. A launcher-only cut opens a gap upward. No
operation moves the launcher below the app, which is what lets a combined cut
overwrite the launcher version unconditionally (D4) without ever rewinding a
version that has already shipped in a bundle filename.

## Requirements

Identifiers are permanent and are not reused, including after deletion.

### Combined release — the `make release` path

- **CR-1** — When an operator runs `make release VERSION=<v>`, the release
  script must write `<v>` to `VERSION`, `backend/app/version.py`,
  `backend/pyproject.toml`, and `frontend/package.json`.
  *(Unchanged from today; stated so the set is complete.)*
- **CR-2** — When an operator runs `make release VERSION=<v>`, the release
  script must write the launcher version to
  `launcher/src-tauri/Cargo.toml`, `launcher/package.json`, and
  `launcher/src-tauri/tauri.conf.json`.
- **CR-3** — The launcher version written by CR-2 must be `<v>` with any
  `-alpha` or `-beta` suffix retained, except in `tauri.conf.json`.
- **CR-4** — The version written to `tauri.conf.json` must be `<v>` with any
  `-alpha` or `-beta` suffix removed.
- **CR-5** — The release script must write all files named in CR-1 and CR-2 in
  a single commit.
- **CR-6** — The release script must create exactly one tag, `v<v>`, for a
  combined release.
- **CR-7** — The release script must overwrite the launcher version files
  regardless of the version they currently declare.
- **CR-8** — The release script must refuse a combined release whose version is
  not greater than the current contents of `VERSION`.
  *(Unchanged from today.)*

### Launcher-only release — the `make release-launcher` path

- **LR-1** — When an operator runs `make release-launcher VERSION=<v>`, the
  release script must write `<v>` to `launcher/src-tauri/Cargo.toml`,
  `launcher/package.json`, and `launcher/src-tauri/tauri.conf.json`.
- **LR-2** — The release script must refuse a launcher-only release whose
  version is not greater than the current contents of `VERSION`.
- **LR-3** — The release script must refuse a launcher-only release whose
  version carries an `-alpha` or `-beta` suffix.
- **LR-4** — The release script must create exactly one tag, `launcher-v<v>`,
  for a launcher-only release.
- **LR-5** — The release script must refuse a launcher-only release cut from a
  branch other than `main`.
  *(Unchanged from today.)*
- **LR-6** — A launcher-only release must not modify `VERSION`,
  `backend/app/version.py`, `backend/pyproject.toml`, or
  `frontend/package.json`.

### Tag guard

- **TG-1** — Given a tag `v<v>`, the guard must fail if `VERSION` does not
  contain `<v>`.
- **TG-2** — Given a tag `launcher-v<v>`, the guard must fail if
  `launcher/src-tauri/Cargo.toml` does not declare `<v>`.
- **TG-3** — Given a tag `launcher-v<v>`, the guard must fail if
  `launcher/src-tauri/tauri.conf.json` does not declare `<v>`.
  *(No suffix-stripping is needed here: LR-3 forbids a suffixed launcher-only
  version, so `<v>` on this path is always a bare core version. A combined cut
  produces a `v*` tag, which TG-1 handles and which does not read this file.)*
- **TG-4** — The guard must fail on a tag carrying neither a `v` nor a
  `launcher-v` prefix.
  *(Unchanged from today.)*
- **TG-5** — The guard must fail if `launcher/src-tauri/Cargo.toml` and
  `launcher/src-tauri/tauri.conf.json` disagree, once any pre-release suffix
  is removed from the `Cargo.toml` value. This check applies to both tag
  prefixes.

TG-1 deliberately does **not** require the launcher files to equal `VERSION`.
They are equal immediately after a combined cut, but a later launcher-only
release (D3) legitimately moves them ahead while historical `v*` tags remain
in the repository, so requiring agreement would fail the guard on a re-run of
an older tag.

TG-5 is what keeps the `0.1.0` defect from recurring on the combined path.
TG-1 alone would not catch it: a `v*` tag validates `VERSION` and never reads
the launcher files, so a `tauri.conf.json` that failed to get bumped would sail
through and produce mislabelled bundles exactly as it does today. Checking the
two launcher files against *each other* is prefix-independent, which is why it
is stated separately rather than folded into TG-1 or TG-3. CR-3 and CR-4
guarantee the suffix-stripped equality this asserts.

### Publishing

- **PB-1** — A `v*` tag must cause the CI system to build and publish the
  `bioflow-backend` and `bioflow-web` images.
  *(Unchanged from today.)*
- **PB-2** — A `v*` tag must cause the CI system to build the launcher's macOS,
  `.deb`, and `.rpm` bundles.
- **PB-3** — A `v*` tag must cause the CI system to create exactly one GitHub
  release.
- **PB-4** — The GitHub release created for a `v*` tag must have the launcher
  bundles attached to it when the launcher build succeeds.
- **PB-5** — The CI system must create the GitHub release for a `v*` tag when
  the image publish succeeds and the launcher build fails.
- **PB-6** — The CI system must not create a GitHub release for a `v*` tag when
  the image publish fails.
- **PB-7** — The workflow run for a `v*` tag whose launcher build failed must
  report failure.
- **PB-8** — A push to `main` must not trigger a launcher build.
  *(Satisfied by the existing job graph — see the note below.)*
- **PB-9** — A `launcher-v*` tag must cause the CI system to create a GitHub
  release with the launcher bundles attached and no images.
- **PB-10** — The GitHub release created for a tag with an `-alpha` or `-beta`
  suffix must be marked as a pre-release.
  *(Unchanged from today.)*
- **PB-11** — Re-running the release-creating job against a tag whose GitHub
  release already exists must update that release rather than fail.

PB-5 and PB-7 together are the shape D7 asks for: the release page exists and
describes the images that are already public, the bundles are simply absent,
and the red run is what tells the operator that a recovery step is needed.
PB-11 is what makes that recovery — "Re-run failed jobs" on the whole run —
safe against the release the first run already created. See the `release` job
discussion under Workflows for why re-running the launcher job alone is not
sufficient.

### Documentation

- **DC-1** — `VERSION.md` must describe one combined release command and one
  launcher-only command.
- **DC-2** — `VERSION.md` must state the launcher-≥-app invariant.
- **DC-3** — `VERSION.md` must record that `tauri.conf.json` is the file Tauri
  reads for the bundle version.

## Design

### Version files

Seven files carry a version. The combined cut writes all seven; the
launcher-only cut writes the last three.

| File | Line | Consumer |
|---|---|---|
| `VERSION` | app | source of truth; the tag guard reads it |
| `backend/app/version.py` | app | generated; imported by the version endpoint |
| `backend/pyproject.toml` | app | none |
| `frontend/package.json` | app | none |
| `launcher/src-tauri/Cargo.toml` | launcher | Tauri fallback; the tag guard reads it |
| `launcher/package.json` | launcher | none |
| `launcher/src-tauri/tauri.conf.json` | launcher | **Tauri reads this for the bundle filename and `Info.plist`** |

The last row is the one that was missing and caused the `0.1.0` bundles.

### `ops/lib/bump_version.py`

Gains a `bump_tauri_conf` helper writing `tauri.conf.json`'s `version` key with
the same textual-substitution approach the module already uses for JSON, so the
file's formatting and key order survive. `bump_launcher` calls it. The `app`
line calls `bump_launcher` as well, so a combined cut writes both sets.

The core-version rule (CR-4) lives here rather than in the shell: the module
already owns "which file gets which string", and it is the half of the tooling
that has unit tests against temporary trees.

### `ops/release.sh`

The `app` line additionally invokes the launcher bump before committing, so
`WRITTEN` covers all seven files and one commit carries them (CR-5).

The `launcher` line's preflight changes in two ways: its greater-than check
reads `VERSION` rather than `Cargo.toml` (LR-2), and it rejects a suffixed
version (LR-3). Its stage-branch behaviour is unchanged — cut from `main`, push
`main` and the tag, no stage branch (LR-5).

### `ops/check_tag_matches_version.sh`

The `launcher-v*` arm gains the `tauri.conf.json` check (TG-3), comparing
against the tag's version with any suffix stripped. The `v*` arm is unchanged
(TG-1).

### Workflows

`.github/workflows/launcher-build.yml` — **new**, reusable. Holds the `macos`
and `linux` jobs lifted verbatim from `release-launcher.yml`, exposed via
`workflow_call`. The Apple secrets (`CI_KEYCHAIN_PASSWORD`,
`APPLE_API_KEY_P8_BASE64`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER_ID`) and the
`APPLE_TEAM_ID` variable are declared as `secrets:`/`inputs:` and threaded
through by each caller. It takes a `notarize` boolean input, defaulting true,
so the existing `workflow_dispatch` build-check path keeps its opt-out. It
uploads the bundles as artifacts and creates no release.

`.github/workflows/release.yml` — `publish-images.yml` renamed. Calls
`launcher-build.yml` in a `launcher` job gated on
`startsWith(github.ref, 'refs/tags/v')` (PB-8).

A note on what that gate is actually for, because the workflow's own comments
are misleading on this point. The file triggers on pushes to `main` as well as
on `v*` tags, and its header comment reasons about docs-only pushes causing a
"no-op rebuild" — but `build` needs `version-guard`, which is itself gated on
`refs/tags/v`, so on a `main` push `version-guard` skips and every job
downstream of it skips too. Confirmed against the last five `main` runs, all
of which concluded `skipped`. **Pushes to `main` publish nothing today.** The
`launcher` job's gate is therefore belt-and-braces rather than the thing
preventing a notarized build on every merge; it is worth writing anyway so the
property does not depend on a skip-cascade two jobs away.

Its `release` job becomes:

```yaml
needs: [manifest, launcher]
if: always() && needs.manifest.result == 'success' && startsWith(github.ref, 'refs/tags/v')
```

`always()` is what lets the job run when `launcher` failed; the explicit
`manifest.result` check is what still withholds the release when the images
failed (PB-6). The `startsWith` clause is load-bearing under `always()` too:
without it, a `main` push — where `manifest` and `launcher` both skip — would
reach the job. The job downloads the bundle artifacts if present and passes
whatever it finds to `gh release create`, so a missing bundle set yields a
release with images and notes only (PB-5).

The recovery path after PB-5 fires needs care, because the bundles are uploaded
by the `release` job rather than by `launcher-build.yml`. Re-running only the
failed `launcher` job would rebuild the bundles as artifacts and attach nothing,
since `release` has already run to completion and will not run again.

The recovery is therefore **"Re-run failed jobs"** on the run as a whole, which
re-runs `launcher` *and* `release` (failed and skipped jobs and their
dependents), leaving `build` and `manifest` untouched. For that to be safe the
`release` job must be idempotent against an existing release: it keeps the
`gh release create ... || gh release edit ...` shape already in
`publish-images.yml`, and uploads bundles with
`gh release upload --clobber`, so a second run updates the release in place
rather than failing on "already exists" (PB-11).

The rename is worth the churn: the file no longer only publishes images, and
`publish-images.yml` naming the workflow that also signs and notarizes a
desktop app is the kind of stale name that misleads later readers.

`.github/workflows/release-launcher.yml` — keeps its `launcher-v*` tag trigger
and its `release` job (D3), but its build steps are replaced by a call to
`launcher-build.yml`. It remains the launcher-only publish path (PB-9) and
keeps its `workflow_dispatch` build check.

### Release notes

The combined release body keeps today's image blurb and gains a line naming the
attached bundles, followed by the generated "What's Changed" list. When the
launcher failed, the bundle line is omitted rather than naming files that are
not there.

## Testing

Backend tests run per CLAUDE.md — `docker compose exec api python -m pytest`
from the main checkout.

| Requirement | Test |
|---|---|
| CR-2, CR-3 | `test_bump_version.py`: an app bump writes all seven files with the full version |
| CR-4 | `test_bump_version.py`: `0.5.0-alpha` writes `0.5.0` to `tauri.conf.json` and `0.5.0-alpha` to `Cargo.toml` |
| CR-7 | `test_bump_version.py`: an app bump over a launcher declaring a higher version overwrites it |
| LR-1, LR-6 | `test_bump_version.py`: a launcher bump writes three files and leaves `VERSION` untouched |
| LR-2 | `test_release_preflight.py`: a launcher cut at or below `VERSION` is refused |
| LR-3 | `test_release_preflight.py`: a suffixed launcher version is refused |
| TG-3 | `test_tag_guard.py`: a `launcher-v*` tag disagreeing with `tauri.conf.json` fails |
| TG-1 | `test_tag_guard.py`: a `v*` tag passes while the launcher files sit ahead |
| TG-5 | `test_tag_guard.py`: a `v*` tag fails when `tauri.conf.json` was not bumped alongside `Cargo.toml` — the regression test for the `0.1.0` defect |
| TG-5 | `test_tag_guard.py`: a `v0.5.0-alpha` tag passes with `Cargo.toml` at `0.5.0-alpha` and `tauri.conf.json` at `0.5.0` |

Workflow behaviour (PB-*) has no local harness. PB-5 and PB-7 are verified by
reading the job graph; the first real cut is what exercises them.

### Verification this design could not complete

Tauri's tolerance for a pre-release suffix in `tauri.conf.json` was not
verified: `launcher/node_modules` is not installed in this checkout, so no
`tauri build` could be run. CR-4 is written to sidestep the question by
stripping the suffix, which is safe whether or not Tauri would have accepted
it. **Before the first alpha cut under this design, run `npm ci && npm run
tauri build` in `launcher/` and confirm the bundle is named with the core
version.** If Tauri turns out to accept suffixes, CR-4 can be revisited to keep
them; nothing else in the design depends on the answer.

## Out of scope

- Migrating or renaming the two historical `launcher-v*` releases. They stay as
  they are, mislabelled bundles included; rewriting published release assets is
  the "release forward" rule's opposite.
- Any change to how the launcher discovers or pulls images. It continues to
  pull `:latest` with no version negotiation.
- Windows launcher bundles, which remain unbuilt.
