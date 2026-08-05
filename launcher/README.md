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

## Prerequisites

Node (any recent LTS) plus a Rust toolchain via [rustup](https://rustup.rs):

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

Tauri v2 also needs platform-specific system libraries to *build* against —
having the runtime libraries already installed is not enough, since building
needs their `-dev`/headers packages too.

**Linux (Debian/Ubuntu):**

```bash
sudo apt-get install -y \
  libwebkit2gtk-4.1-dev libxdo-dev libssl-dev \
  libayatana-appindicator3-dev librsvg2-dev build-essential
```

A GUI session (X11 or Wayland) is required to run the app, even in dev mode
— there is no headless mode.

**macOS:** Xcode Command Line Tools (`xcode-select --install`) — no extra
system packages beyond that.

**Windows:** the [Microsoft C++ Build
Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) and the
WebView2 runtime (preinstalled on current Windows 10/11).

See [Tauri's own prerequisites
guide](https://v2.tauri.app/start/prerequisites/) if a package name above has
moved on for a given distro version.

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

## Building a distributable bundle by hand

`npm run tauri dev` is enough to exercise the app, but does not produce an
installable artifact. To build one for the platform you're running on:

```bash
npm install
npm run tauri build
```

This runs the full release build (`cargo build --release` plus the Vite
production build) and then bundles it per `src-tauri/tauri.conf.json`'s
`bundle.targets: "all"` — a signed-or-not `.app` and `.dmg` on macOS, an
NSIS/MSI installer on Windows. Artifacts land under
`src-tauri/target/release/bundle/`.

**On Linux, `.deb` and `.rpm` only** — `src-tauri/tauri.linux.conf.json`
overrides `bundle.targets` to just those two, dropping AppImage. Tauri v2
auto-merges any `tauri.<platform>.conf.json` on top of the base config for
that platform's build, so this needs no `--config` flag; it just takes
effect. The reason: AppImage bundling shells out to a helper binary,
`linuxdeploy-plugin-appimage.AppImage`, that Tauri expects to run silently
as a subprocess -- but on a machine with **AppImageLauncher** installed (a
common desktop-integration daemon on several distros), *every* AppImage
execution is intercepted with a GUI "Integrate and run / Run once" prompt,
including this one. `tauri build` then hangs waiting on input it never
expected, and even answering the prompt is not enough: it still fails with
`failed to run linuxdeploy`, because linuxdeploy also expects to be run
from a real TTY/desktop session in a way the intercepted execution breaks.
`.deb` and `.rpm` build and bundle cleanly either way, cover the common
Linux install paths, and don't invoke linuxdeploy at all -- so this avoids
the trap entirely rather than asking whoever builds next to fight
AppImageLauncher's config. If AppImage output is ever needed again, the
fix belongs in this file (either dropping "appimage" back out of a full
target list per platform some other way, or configuring AppImageLauncher
to exempt `~/.cache/tauri/`), not in re-discovering this from scratch.

This is a **local, unsigned build for testing on the machine that built it**,
not the distribution story: macOS notarization, Windows code signing, and
hosting a download for end users are still open work, tracked in
[#39](https://github.com/syntheticgio/bioflow/issues/39). There is also no
CI job that does this automatically yet — every bundle today is built by a
person, by hand, on their own machine.

A hand-built bundle only reaches as far as its own OS and CPU architecture.
It bundles `docker-compose.yml` verbatim (see above), so it needs the
published container images for whatever architecture it runs on to actually
start a stack — see the root `docker-compose.yml`'s `image:` references and
[#46](https://github.com/syntheticgio/bioflow/issues/46) for which
architectures are published.

## Tests

```bash
npm test              # vitest, UI-side pure logic
cargo test             # from src-tauri/, the Docker interface and state machine
```

`npm test` currently reports "No test files found" — there are no UI-side
unit tests yet, only the Rust-side coverage in `src-tauri/`. That command is
listed for when UI tests are added, not because any exist today.

`cargo test` includes one `#[ignore]`d test
(`ghcr_client_reads_a_real_digest_from_a_public_image`) that hits the real
GHCR registry over the network; run `cargo test -- --ignored` to include it
deliberately, separately from the fast default run.
