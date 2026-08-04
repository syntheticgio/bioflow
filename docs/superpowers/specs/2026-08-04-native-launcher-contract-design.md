# Native launcher contract design

**Date:** 2026-08-04

**Issue:** [#28](https://github.com/syntheticgio/bioflow/issues/28), the first
slice of epic [#4](https://github.com/syntheticgio/bioflow/issues/4).

## Goal

Remove `docker compose` from the user's vocabulary. A small native application
window detects Docker, collects the three answers the stack needs, writes an
`.env`, starts the containers, and opens a browser at BioFlow. Thereafter it is
a status panel with Run, Stop, Update, and a settings screen.

This document defines the launcher's contract: what it does, what it refuses to
do, and what has to change in this repository to make it possible. Packaging,
signing, and distribution are deliberately excluded and tracked separately.

## Decomposition of epic #4

The epic covers more than one shippable piece. It divides as:

| Piece | Scope | Status |
|---|---|---|
| Launcher contract and runtime (#28) | Docker detection and auto-start, first-run setup, `.env` writing, run/stop/status/update, browser handoff, settings | This spec |
| Image publishing | One-time manual build and push of `api`, `worker`, `web` to ghcr.io; compose converted from `build:` to `image:` | Prerequisite, new issue |
| CI/CD release pipeline | Actions workflow building containers on push and publishing `:latest` plus version tags on release | New issue, replaces the manual step |
| Packaging, signing, distribution | Tauri bundles per OS, macOS notarization, Windows signing, download hosting, launcher self-upgrade | Deferred, new issue |

The optional-tool pre-pull checkbox described in `docs/TODO.md` is **not** part
of this slice. Which tools are optional, and how a user chooses among them, is
undefined until epic [#5](https://github.com/syntheticgio/bioflow/issues/5)
settles it. A separate issue revisits the installer checkbox afterwards.

## Where the launcher lives

**In this repository**, under a top-level directory, built and versioned with
the rest of the project. Compiled binaries are distributed to end users so that
nobody needs to clone anything.

This reverses the epic body's statement that the launcher "lives outside the
current Python/React repository." That framing predates the decision to have
the launcher ship a compose file verbatim. In-tree, the file the launcher
bundles at build time can be *the* `docker-compose.yml` rather than a copy of
it, which removes the only real argument for a separate repository — and
removes an entire class of drift where the launcher ships a stale stack
definition. Epic #4's body needs updating to match.

## Technology

Tauri. It produces a real native window on macOS, Windows, and Linux using each
platform's own webview (WKWebView, WebView2, WebKitGTK), so the launcher is an
application window rather than a browser tab, and the binary stays small. The
UI is a handful of screens in the web stack this project already uses.

Builds are per-OS: the Windows binary is produced on Windows, and so on. That
matters for the packaging issue, not for this one.

## The compose file is shipped, never generated

The launcher bundles `docker-compose.yml` as a build-time asset and writes it to
the install directory verbatim. It never templates, generates, or edits YAML.

Everything the user chooses is expressed as an environment variable that the
compose file already substitutes, and the launcher's only writable artifact is
`.env`. This keeps the stack authored in exactly one place. A launcher that
generated YAML would become a second definition of the stack, guaranteed to
drift, with a failure mode the user cannot debug.

The shipped file is the base `docker-compose.yml` only. `docker-compose.override.yml`
is the local hot-reload development setup and is not distributed; installed
users run the published images with the `prod` web target.

## Runtime contract

### States

The launcher is a state machine evaluated at every launch and on a status poll:

1. **Not installed** — no install directory or no `.env`. Runs first-run setup.
2. **Docker unavailable** — an install exists but the daemon is unreachable.
3. **Stopped** — daemon reachable, no BioFlow containers running. Offers Run.
4. **Running** — containers up *and the API healthcheck passing*. Offers Open
   Browser, Stop, Settings, and Update when an update exists.

State 4 is health-gated rather than container-gated on purpose: "Running" must
mean the API answered, not merely that containers exist. A container that is up
but not yet serving is still state 3 from the user's point of view.

### Docker detection and auto-start

Probe the daemon. On failure, distinguish two cases, because they need
different screens:

- **Not installed** (no `docker` binary): a screen naming Docker Desktop with a
  download link and a "Check again" button. The launcher never installs Docker.
  Docker installation is privileged, divergent across platforms (Desktop on
  macOS and Windows, Engine via a distro package manager on Linux), and
  licence-gated for Docker Desktop in some organizations. Owning it would turn
  a small launcher into an installer for someone else's product.
- **Installed but stopped**: attempt to start it — `open -a Docker` on macOS,
  launching Docker Desktop on Windows, `systemctl --user start docker` on Linux
  — then poll the daemon behind a visible "Waiting for Docker…" state with a
  **60-second timeout** that falls back to the manual screen.

The timeout is part of the design, not a safety net. Daemon startup takes 10–30
seconds and can fail without reporting anything, so a launcher that waits
indefinitely simply hangs.

### First-run setup

Collects exactly three answers, each with a sensible per-OS default:

- **Storage location** → `BIOINFO_HOME`, the host directory bind-mounted at
  `/data`.
- **Install directory** — where `docker-compose.yml` and `.env` are written.
- **Port** → `WEB_PORT`.

Validation carries more weight than the questions themselves:

- The storage path must exist and be writable.
- On macOS it must be a path Docker Desktop will file-share. Choosing an
  unshared path succeeds at setup and surfaces much later as an empty `/data`,
  with nothing in the symptom pointing at the cause.
- The port must be free, verified by binding it.

Setup then writes the install directory, copies the bundled compose file in,
writes `.env`, pulls images once, and starts the stack.

### Settings after install

Storage location, port, and network exposure are editable from a settings
screen, not only at first run. Each rewrites `.env` and recreates the stack to
take effect. Changing the storage location points the stack at a different
`/data` and does not move existing data — the UI says so at the point of
change.

### Network exposure

The stack currently publishes on `0.0.0.0`, reachable by any device on the
network with no authentication in front of it. The launcher's default is
`127.0.0.1`, and an explicit **"Allow access from other devices on my network"**
toggle opens it up by setting `BIND_ADDRESS=0.0.0.0`.

The default is the locked-down side and the toggle turns exposure *on*. Framing
it the other way round would make the safe state the one the user has to find.

### Actions

All actions shell out to the user's own `docker` binary with
`--project-directory` set to the install directory. No Docker API client
library, no bundled Docker.

- **Run** — `docker compose up -d`, then wait for health, then reflect Running.
- **Stop** — `docker compose down`.
- **Update** — `docker compose pull` followed by a recreate. Only on an explicit
  click.
- **Status** — poll `docker compose ps` plus the API healthcheck.

### Update checking

`docker compose up` pulls an image only when it is absent locally. Once
`:latest` is cached it is reused indefinitely, so "latest" does not
self-update; an update requires an explicit `docker compose pull`.

The launcher therefore never downloads images unasked. It does perform a cheap
registry **manifest check** on launch — comparing the local `:latest` digest
against the registry's — to decide whether to show the Update button at all. It
runs non-blocking and fails silently when offline. A few kilobytes of metadata
to keep a button honest is a different thing from a multi-gigabyte download,
and only the latter needs the user's consent.

The compose file keeps `${BIOFLOW_TAG:-latest}` so a tag can be pinned for
debugging, but first-run setup does not ask: the launcher writes
`BIOFLOW_TAG=latest`. An Update button in the web UI is future work beyond this
issue.

### Browser handoff

After Run, wait for the API healthcheck to pass, then open the system browser
at `http://localhost:<port>`. Health-gated rather than a fixed sleep: a cold
start against an empty Mongo volume takes substantially longer than a warm one,
and opening too early shows a connection error that reads as a broken install.

### Window lifetime

Closing the window quits the launcher and leaves the stack running. The
launcher is a control panel, not a supervisor. This matches how the stack
already behaves — `restart: unless-stopped` means containers survive reboots —
and prevents a long-running alignment job from dying because someone tidied
their taskbar. Reopening the launcher re-detects state; stopping the stack means
reopening it and clicking Stop, which the UI states plainly.

## Non-goals

- **Does not create the initial BioFlow profile.** At install time the stack is
  not running and there is no API to create a profile against. The launcher
  would have to know the `Profile` schema, hash a password, and write a seed
  file the backend parses on boot — duplicating logic that already exists
  behind the API and creating a second way to create a profile that could drift
  from the first. Profile creation belongs to the web UI's first-run screen,
  which the profiles design already requires for the empty-database case, and
  which is also where a second profile gets added later.
- **Does not install Docker.**
- **Does not generate or template compose YAML.**
- **Has no system tray icon**, now or later. Closing the window and reopening
  the launcher is the interaction.
- **Does not upgrade itself.**
- **Does not manage optional tool images.**
- **Does not repair the stack**, delete volumes, or retry indefinitely on
  failure.

## Changes required in this repository

All compose changes carry defaults that preserve current local behavior, with
one deliberate exception noted below.

- **`api`, `worker`, `web`: `build:` → `image:`**, pointing at
  `ghcr.io/syntheticgio/bioflow-{backend,web}:${BIOFLOW_TAG:-latest}`. Compose
  pulls `image:` services automatically but builds `build:` services from a
  source tree the user does not have, which is why this conversion is a
  prerequisite rather than a preference.

  Two images, not three: `api` and `worker` share one build context and one
  Dockerfile, differing only by `command:`, so both reference
  `bioflow-backend`. They are the same 7.89GB image under two names, and
  publishing them separately would push identical layers twice.
- **The override file gains the `build:` directives the base file loses.** This
  is a real change to how the repository builds, not a no-op: `docker compose
  up -d --build` currently depends on the base file's build contexts. The
  override always loads locally and never ships, so it is the correct home for
  them, but the change must be made in the same commit or local builds break.
- **`web` ports → `"${BIND_ADDRESS:-127.0.0.1}:${WEB_PORT:-5173}:80"`.** This
  changes the current default from `0.0.0.0` to loopback — a deliberate
  tightening that also applies to local development.
- **`api` ports** get the same treatment; `8000:8000` has identical exposure.
- **Images are published for `linux/amd64` and `linux/arm64`**, since users are
  on both.
- **`.env.example`** documents `WEB_PORT`, `BIND_ADDRESS`, and `BIOFLOW_TAG`.

## Error handling

Each of these gets a named state with the cause on screen and the compose
output viewable. None of them is a generic failure dialog.

- **Port already in use** — detected at setup and again at Run, with an offer to
  pick another.
- **Storage path not shared with Docker Desktop (macOS)** — pre-flight warning
  when the path is outside the user's home or any explicitly shared root. The
  post-hoc symptom, an empty `/data`, gives the user nothing to work with.
- **Image pull fails or the machine is offline at first run** — setup cannot
  complete and must be resumable rather than leaving a half-written install
  directory.
- **Daemon dies while running** — the status poll flips to Docker-unavailable
  instead of showing a stale Running.
- **Disk full during a pull** — surfaced from the compose output, not swallowed.

## Testing

The Docker-facing layer sits behind one interface — probe, up, down, ps, pull —
so the state machine and its transitions are unit-testable against a fake with
no Docker present. That covers the logic where the bugs will be.

The launcher's own UI follows the pattern the frontend already uses: `vitest`
is configured (`frontend/package.json`) and nine `.test.ts` files cover pure
logic in `frontend/src/lib/`. There are no `.test.tsx` component tests and none
are expected — the launcher's testable logic is state transitions and parsing,
which is the same shape.

Everything else is manual verification on macOS, Windows, and Linux, consistent
with the rest of the repository.

## Open questions

None. The optional-tools checkbox and the web-UI update button are deferred to
named issues rather than left unresolved here.
