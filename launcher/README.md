# BioFlow Launcher

Native installer and control panel for the BioFlow Docker Compose stack. See
[`docs/superpowers/specs/2026-08-04-native-launcher-contract-design.md`](../docs/superpowers/specs/2026-08-04-native-launcher-contract-design.md)
for the contract this implements, and
[`docs/superpowers/plans/2026-08-05-native-launcher-contract-implementation.md`](../docs/superpowers/plans/2026-08-05-native-launcher-contract-implementation.md)
for the build plan.

## The bundled compose file

`src-tauri/tauri.conf.json`'s `bundle.resources` maps
`../../docker-compose.yml` (the repository's own compose file) into the
bundle. **This must stay a reference to the real file, never a copy checked
into `launcher/`.** The whole point of building the launcher in-tree is that
the file it ships *is* `docker-compose.yml`, not a second definition of the
stack that can drift from it — see the spec's "The compose file is shipped,
never generated" section. If this path is ever changed to point at a copy
under `launcher/`, that reintroduces the drift the in-tree decision exists to
avoid.

`docker-compose.override.yml` is not bundled — it is the local hot-reload dev
setup and is never shipped to installed users.

## The stack this launcher starts has access to the host's Docker daemon

`docker-compose.yml` mounts `/var/run/docker.sock` into both the `api` and
`worker` services. This is a real privilege increase — a container that can
reach the daemon can start, stop, or inspect any container on the host, not
only the ones this stack manages. It is accepted because BioFlow is a
single-user, local application, not a hosted service with other tenants.

The reason: some pipeline tools (DeepVariant today, more from
[epic #5](https://github.com/syntheticgio/bioflow/issues/5) going forward) are
too large to bundle into the backend image and instead run as sibling
containers, pulled on demand and started through the host daemon. See
`docs/superpowers/specs/2026-07-31-deepvariant-sidecar-design.md` and
`docs/superpowers/specs/2026-08-05-optional-tool-delivery-design.md`.

This mount used to live only in `docker-compose.override.yml`, which — per the
section above — this launcher never bundles. That meant a launcher-installed
user had no socket and any sibling-container tool silently could not run for
them, with no error pointing at the cause. It now lives in the base file so
every install this launcher produces has it.

## Development

```bash
npm install
npm run tauri dev
```

## Tests

```bash
npm test              # vitest, UI-side pure logic
cargo test             # from src-tauri/, the Docker interface and state machine
```
