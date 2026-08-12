# Versioning and releases

> **This file documents the tooling as of the staged release methodology
> adopted on 2026-08-09.** `ops/release.sh` now accepts `-alpha` and `-beta`
> pre-release suffixes and cuts onto `alpha/X.Y.Z` / `beta/X.Y.Z` / `release/X.Y.Z`
> branches accordingly. See "Release methodology" in [CLAUDE.md](CLAUDE.md) for
> the intended flow and the diagrams in `assets/`.

BioFlow has **one version line.** `VERSION` at the repo root is the source of
truth for the container images and the native launcher alike, and one `vX.Y.Z`
tag publishes both.

| | What it versions |
|---|---|
| `VERSION` | backend + web container images, and the launcher |
| Tag | `v0.5.0` |
| Workflow | `.github/workflows/release.yml` |
| Produces | GHCR images, `.dmg`/`.deb`/`.rpm`, and one GitHub release |

This replaced two independent lines on 2026-08-12
([#335](https://github.com/syntheticgio/bioflow/issues/335)). The launcher is
re-released on every app release even when nothing in it changed, which is
expected to be the common case and is deliberate: the cost is a slow build,
and the benefit is that a version number means one thing.

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

## Cutting a release

One command. It bumps, commits, tags, and pushes:

```bash
make release VERSION=0.2.0            # production release
make release VERSION=0.3.0-alpha      # alpha pre-release
make release VERSION=0.3.0-beta       # beta pre-release
```

```bash
make release-launcher VERSION=0.1.1
```

Give the bare version — `0.2.0`, not `v0.2.0`. The script adds the prefix.

The version suffix determines the stage and the target branch:

| Version | Stage | Source branch | Target branch | Tag |
|---|---|---|---|---|
| `0.2.0` | production | `main` or `beta/0.2.0` | `release/0.2.0` | `v0.2.0` |
| `0.3.0-alpha` | alpha | `main` | `alpha/0.3.0` | `v0.3.0-alpha` |
| `0.3.0-beta` | beta | `alpha/0.3.0` | `beta/0.3.0` | `v0.3.0-beta` |

The script leaves you on the stage branch after cutting. Switch back to `main`
after the cut if you need to continue working there.

For the app line, the release commit also carries a regenerated
`CHANGELOG.md`: the script runs git-cliff (`--unreleased --tag`), which
renders the commits since the last tag as the new version's section and
prepends it, so the tag's tree contains the changelog that describes it.
See `cliff.toml` and [#106](https://github.com/syntheticgio/bioflow/issues/106).

Bumping and tagging are deliberately not separable. Done by hand as two steps,
the failure modes are "bumped but never tagged" and "tagged but never bumped",
and recovering from the second means deleting a tag CI has already acted on.

### What it refuses

The script stops, with a message naming the cause, if:

- the version is not bare `MAJOR.MINOR.PATCH`, optionally suffixed with
  `-alpha` or `-beta` (no `v` prefix, no `-rc`, no `-ALPHA`)
- the working tree is dirty
- you are not on the correct source branch for the stage (see table above)
- the tag already exists, locally or on `origin`
- the version is not greater than the current one

The source-branch check is per-stage: alpha cuts require `main`, beta cuts
require `alpha/X.Y.Z`, and production cuts accept `main` or `beta/X.Y.Z`.
This replaces the old `main`-only check from the pre-methodology tooling.

## Which number to bump

Ordinary semver, read from the user's side:

- **Patch** (`0.1.0` → `0.1.1`) — bug fixes, performance, docs. Nothing about
  using the app changes.
- **Minor** (`0.1.0` → `0.2.0`) — new pipelines, tools, endpoints, or UI. Old
  projects and data keep working.
- **Major** (`0.9.0` → `1.0.0`) — a migration is required, or something that
  worked before stops working.

Pre-release `-alpha` and `-beta` suffixes are supported (see the stage table
above). Other suffixes (`-rc`, `-ALPHA`, build metadata) are rejected.

## What each line writes

Only `VERSION` and `tauri.conf.json` have a consumer that would notice if they
went stale. The rest are kept consistent so the tree does not contradict
itself.

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
| `CHANGELOG.md` | none (human-facing; regenerated by the release script from commit history, #106) |

`make release-launcher VERSION=…` writes the last three launcher files only.

`backend/app/version.py` is generated and committed. Do not edit it by hand —
`backend/tests/test_version_consistency.py` fails if it drifts from `VERSION`.

## What CI does

Pushing the tag is what starts everything.

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

A failed version-guard means the tag was created by hand rather than by
`ops/release.sh`. Delete the tag (it has published nothing yet), then use the
`make release` command.

### If `docker/login-action` fails on the arm64 build with `-25308`

This applied when the arm64 leg of `release.yml` ran on a self-hosted
macOS runner (before [#338](https://github.com/syntheticgio/bioflow/issues/338)
moved it to hosted `ubuntu-24.04-arm`, which has no keychain and doesn't hit
this). Kept here in case a future self-hosted macOS job needs it again.

`error saving credentials ... User interaction is not allowed. (-25308)` shows
up when a job runs as a launchd LaunchAgent with `SessionCreate: true`, which
puts the job in its own macOS security session rather than the interactive
login session — and `docker login`'s default credential store on macOS is
`osxkeychain`, compiled in as the unconditional platform default. An unset,
empty, or bogus `credsStore` in `config.json` does not avoid it, and
`login-action` has no silent plaintext fallback: a helper it can't exec is a
fatal error, not a degrade. Keychain refuses a cross-session credential write
without an interactive prompt, which a background session can never answer.

The fix ([docker/login-action#566](https://github.com/docker/login-action/issues/566)):
unlock the login keychain with the real password immediately before
`docker/login-action`, in its own step:

```yaml
- name: Unlock the login keychain
  env:
    KEYCHAIN_PASSWORD: ${{ secrets.MACOS_LOGIN_KEYCHAIN_PASSWORD }}
  run: security unlock-keychain -p "$KEYCHAIN_PASSWORD" ~/Library/Keychains/login.keychain-db
```

A `SessionCreate` session can still *unlock* a keychain given the real
password — it just can't get an *interactive* unlock prompt answered, which
is what `-25308` is actually complaining about. `release.yml`'s
`build` job carries this today, gated to `matrix.arch == 'arm64'` only: the
`amd64` leg runs on the Linux runner, where `security(1)` doesn't exist at
all, and running this unconditionally there fails immediately with
`security: command not found`. `manifest` never needs it — it always runs
on the Linux runner.

Requires the repo secret `MACOS_LOGIN_KEYCHAIN_PASSWORD`: the runner's own
macOS account login password (the login keychain's password matches it
unless changed separately). If this ever needs rotating, update the secret
with `gh secret set MACOS_LOGIN_KEYCHAIN_PASSWORD --repo syntheticgio/bioflow`.

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
in practice, one that failed the version-guard, or failed partway through
`build` before `manifest`/`release` ran. Both leave nothing tagged in GHCR
and no GitHub release, so nothing downstream has a stale reference to clean
up. Check `gh release list` and `docker buildx imagetools inspect
<image>:<version>` before deleting, not just the run's pass/fail status —
those are what confirm nothing actually published, not the CI conclusion
alone. A workflow-infrastructure failure (a broken CI step unrelated to the
code being released, like the keychain issue above) can take several
attempts to resolve; each failed attempt gets a fresh patch version and
its tag deleted once confirmed to have published nothing, same as any
other unpublished failure.
