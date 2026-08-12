Here's how releases work in this repo:

## Commands

```bash
# Combined release — creates tag vX.Y.Z, pushes images to GHCR and
# launcher bundles to the same GitHub Release
make release VERSION=0.2.0

# Launcher-only escape hatch — creates tag launcher-vX.Y.Z, pushes builds
# to their own GitHub Release, no images. Must exceed the current VERSION
# and cannot be a pre-release.
make release-launcher VERSION=0.2.1
```

## What each does

| | Tag Format | CI Workflow | Artifacts |
|---|---|---|---|
| **Combined (app + launcher)** | `v0.2.0` | `.github/workflows/release.yml` | Docker images pushed to GHCR, plus `.dmg`/`.deb`/`.rpm` on the same GitHub Release |
| **Launcher-only** | `launcher-v0.2.1` | `.github/workflows/release-launcher.yml` | `.dmg`, `.deb`, `.rpm` on their own GitHub Release |

## Details

- **Both create tags** and push them to origin — the CI workflows are triggered by tag pushes
- One version line: `VERSION` at the repo root is the source of truth for both the images and the launcher. A combined cut writes both; a launcher-only cut writes only the launcher files and must produce a version greater than the current `VERSION`, so the launcher version is never lower than the app's
- `ops/release.sh` for the `app` line bumps `VERSION`, `backend/app/version.py`, `backend/pyproject.toml`, `frontend/package.json`, `launcher/src-tauri/Cargo.toml`, `launcher/package.json`, and `launcher/src-tauri/tauri.conf.json`
- For the `launcher` line, it bumps only the last three of those
- `launcher/src-tauri/tauri.conf.json` is the file Tauri actually reads for the bundle filename — it takes precedence over `Cargo.toml`