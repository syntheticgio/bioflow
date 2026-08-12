Here's how releases work in this repo:

## Commands

```bash
# Backend (app) release — creates tag vX.Y.Z, pushes images to GHCR
make release VERSION=0.2.0

# Launcher release — creates tag launcher-vX.Y.Z, pushes builds to GitHub Releases
make release-launcher VERSION=0.1.1
```

## What each does

| | Tag Format | CI Workflow | Artifacts |
|---|---|---|---|
| **App (backend + web)** | `v0.2.0` | `.github/workflows/publish-images.yml` | Docker images pushed to GHCR |
| **Launcher** | `launcher-v0.1.1` | `.github/workflows/release-launcher.yml` | `.dmg`, `.deb`, `.rpm` on a GitHub Release |

## Details

- **Both create tags** and push them to origin — the CI workflows are triggered by tag pushes
- **Both upload artifacts**: the app workflow pushes container images to the GitHub Container Registry, the launcher workflow builds native platform binaries and publishes them as a GitHub Release
- The versions are independent — bumping one does not bump the other
- The launcher pulls `:latest` images, so there's no version compatibility contract between the two numbers
- For the app, `ops/release.sh` also bumps `VERSION`, `backend/app/version.py`, `backend/pyproject.toml`, and `frontend/package.json`
- For the launcher, it bumps `launcher/src-tauri/Cargo.toml` and `launcher/package.json`