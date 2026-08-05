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
