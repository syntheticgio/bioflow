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
