# Vendored fonts

Source Serif 4 (latin subset, woff2) — the Broadsheet theme's display and body
face. Four faces: regular/semibold, upright and italic.

Vendored rather than loaded from `fonts.googleapis.com` because the app runs
in Docker and should not need internet at runtime. They live under `src/`
rather than `public/` on purpose: `docker-compose.override.yml` bind-mounts
`./frontend/src`, so files here show up in the running container without a
rebuild, and Vite fingerprints them as assets.

Licensed under the SIL Open Font License 1.1 (upstream:
https://github.com/adobe-fonts/source-serif). The OFL permits redistribution
and bundling; the license text travels with the upstream project.

Broadsheet is the app's only theme, so these faces load on every page.
