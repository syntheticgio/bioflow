Let me check the existing release infrastructure to understand how releases are created.Here's how releases work in this repo:

## Commands

Here's how releases work in this repo:

## Commands

```bash
# Combined release — app images AND launcher bundles, one tag vX.Y.Z
make release VERSION=0.5.0

# Pre-releases — same combined cut, onto a stage branch
make release VERSION=0.6.0-alpha
make release VERSION=0.6.0-beta

# Launcher-only escape hatch — creates tag launcher-vX.Y.Z, no images
make release-launcher VERSION=0.5.1
```

## What each does

| | Tag Format | CI Workflow | Artifacts |
|---|---|---|---|
| **App (backend + web + launcher)** | `v0.5.0` | `.github/workflows/release.yml` | GHCR images (multi-arch) and .dmg/.deb/.rpm on one GitHub Release |
| **Launcher only** | `launcher-v0.5.1` | `.github/workflows/release-launcher.yml` | .dmg, .deb, .rpm on its own GitHub Release, no images |

## Details

- **Both create tags** and push them to origin — the CI workflows are triggered by tag pushes
- **Both upload artifacts**: the app workflow pushes container images to the GitHub Container Registry, the launcher workflow builds native platform binaries and publishes them as a GitHub Release
- The versions are independent — bumping one does not bump the other
- The launcher pulls `:latest` images, so there's no version compatibility contract between the two numbers
- For the app, `ops/release.sh` also bumps `VERSION`, `backend/app/version.py`, `backend/pyproject.toml`, and `frontend/package.json`
- For the launcher, it bumps `launcher/src-tauri/Cargo.toml` and `launcher/package.json`