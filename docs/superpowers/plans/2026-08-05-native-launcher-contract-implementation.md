# Native launcher contract — implementation plan

**Date:** 2026-08-05

**Issue:** [#28](https://github.com/syntheticgio/bioflow/issues/28), first slice
of epic [#4](https://github.com/syntheticgio/bioflow/issues/4).

**Spec:** [`docs/superpowers/specs/2026-08-04-native-launcher-contract-design.md`](../specs/2026-08-04-native-launcher-contract-design.md)

The spec is complete and records no open questions. This plan turns it into an
ordered build. Nothing here reopens a spec decision; where this plan makes a
call the spec did not, it is marked **[plan decision]**.

## Scope boundary with #37

The spec's "Changes required in this repository" section spans two issues. This
plan splits them so #28 is not blocked on #37:

| Change | Owner | Why |
|---|---|---|
| `web`/`api` ports → `${BIND_ADDRESS:-127.0.0.1}:${WEB_PORT:-5173}:80` | **#28** (Phase 1) | The launcher's network-exposure toggle and port question have nothing to write to without these. Independent of where images come from. |
| `.env.example` documents `WEB_PORT`, `BIND_ADDRESS`, `BIOFLOW_TAG` | **#28** (Phase 1) | Same. |
| `api`/`worker`/`web` `build:` → `image:` at `ghcr.io/syntheticgio/bioflow-{backend,web}` | **#37** | Requires published images to exist first; a swap without them breaks every local run. |
| `build:` directives move into `docker-compose.override.yml` | **#37**, same commit as the swap | CLAUDE.md is explicit that these move together or local builds break. |
| Multi-arch publishing (amd64 + arm64) | **#37**/#46 | Registry work. |

**[plan decision]** The launcher is therefore built and manually verified
against a compose file that still has `build:` services. Everything the launcher
does — probe, up, down, ps, pull, health-poll — behaves identically either way;
the only difference is that `up` builds instead of pulls on a cold machine. The
one contract the launcher must not assume before #37 lands is that `pull`
succeeds, so **Update is health-checked against a real registry only after #37**
and Phase 6 says so explicitly.

## Where it lives

`launcher/` at the repository top level, a Tauri v2 app.

- `launcher/src/` — the TypeScript/React UI (React 18 + Vite + vitest, matching
  `frontend/`, so `.test.ts` files run under the toolchain the repo already
  knows).
- `launcher/src-tauri/` — the Rust side: the Docker interface, the state
  machine, `.env` writing, port binding, path validation.

**[plan decision]** The state machine lives in **Rust**, not the UI. Every input
it evaluates (daemon probe, `compose ps`, filesystem writability, port binding)
is a system call the webview cannot make, so putting the machine in TypeScript
would mean marshalling raw results across the IPC boundary and duplicating the
transition logic to test it. The UI renders one `LauncherState` value and
dispatches named commands. `cargo test` covers the machine against a fake
Docker; vitest covers UI-side formatting and any pure parsing that ends up in
TS. The spec's testing section anticipates exactly this shape.

## Phases

Each phase is independently committable and leaves the repository green. Phases
1–3 are backend-shaped and land before any window opens; that ordering is
deliberate, since the state machine is where the spec says the bugs will be.

---

### Phase 1 — Compose surface the launcher writes to

No launcher code. Makes the stack configurable by `.env` alone.

- [ ] `docker-compose.yml`: `web` ports → `"${BIND_ADDRESS:-127.0.0.1}:${WEB_PORT:-5173}:80"`.
- [ ] `docker-compose.yml`: `api` ports → `"${BIND_ADDRESS:-127.0.0.1}:${API_PORT:-8000}:8000"`.
- [ ] `.env.example`: document `WEB_PORT`, `API_PORT`, `BIND_ADDRESS`, `BIOFLOW_TAG`, each with the default and a line on what it does. `BIND_ADDRESS` gets the sentence explaining that `0.0.0.0` exposes an unauthenticated stack to the network.
- [ ] Verify `docker compose up -d --build api web worker` from the main checkout still serves 5173, and that `curl` from another device now fails where it previously succeeded.

**This tightens local development's default from `0.0.0.0` to loopback.** The
spec calls that deliberate. It is the one change in this plan a user can feel
without installing anything, so it goes in its own commit with that stated in
the message.

Green bar: `docker compose exec api python -m pytest tests/ -q` from the main
repo root (no backend code changed; this is a regression check that nothing
about the stack broke).

---

### Phase 2 — Scaffold `launcher/`

- [ ] `cargo tauri init` (Tauri v2) into `launcher/`, wired to a Vite + React 18 + TS frontend mirroring `frontend/`'s config.
- [ ] `launcher/package.json` with `test: vitest run` and `lint: tsc --noEmit`, matching `frontend/`.
- [ ] Bundle `docker-compose.yml` as a build-time resource. **The bundled path must resolve to the repository's own `docker-compose.yml`**, not a copy — a copy is the drift the spec chose in-tree placement to avoid. Record how (Tauri `resources` entry pointing at `../docker-compose.yml`) in a comment, since it is the design's load-bearing detail.
- [ ] `.gitignore` for `launcher/src-tauri/target/` and `launcher/node_modules/`.
- [ ] Window opens and renders "BioFlow" on macOS.

Green bar: `cargo build` and `npm run lint` in `launcher/` both succeed.

---

### Phase 3 — The Docker interface and the state machine

The heart of the issue. No UI beyond a debug dump of the current state.

- [ ] Define the trait the spec names — `probe`, `up`, `down`, `ps`, `pull` — plus `health` (the API healthcheck) and `manifest_digest`. One real implementation shelling out to `docker` with `--project-directory <install dir>`; one fake for tests.
- [ ] Implement the four states: `NotInstalled`, `DockerUnavailable { installed: bool }`, `Stopped`, `Running`.
- [ ] **`Running` is health-gated, not container-gated.** Containers up but health failing is `Stopped` from the user's point of view. This is the transition most likely to be got wrong, so it gets its own test.
- [ ] Docker auto-start: `open -a Docker` (macOS), Docker Desktop launch (Windows), `systemctl --user start docker` (Linux), behind a **60-second timeout** that falls back to the manual screen. The timeout is a spec requirement, not a safety net — a test asserts it fires.
- [ ] Status poll re-evaluates from scratch; a daemon that dies while running flips to `DockerUnavailable` rather than showing a stale `Running`.

Tests (`cargo test`, against the fake):
- [ ] Each state is reached from the inputs that define it.
- [ ] Containers-up-but-unhealthy → `Stopped`.
- [ ] Daemon dies while `Running` → `DockerUnavailable`.
- [ ] Auto-start timeout fires and falls back.
- [ ] Missing `docker` binary → `DockerUnavailable { installed: false }`, distinct from installed-but-stopped.

Green bar: `cargo test` in `launcher/src-tauri`, all passing, count read rather
than exit code.

---

### Phase 4 — First-run setup

- [ ] Three questions with per-OS defaults: storage location (`BIOINFO_HOME`), install directory, port (`WEB_PORT`).
- [ ] Validation, which the spec weights above the questions:
  - [ ] Storage path exists and is writable — probe by writing, not by stat.
  - [ ] **macOS file-sharing pre-flight**: warn when the path is outside `$HOME` or any explicitly shared root. The post-hoc symptom is an empty `/data` that points nowhere near its cause.
  - [ ] Port is free, **verified by binding it**, not by scanning.
- [ ] Write install directory → copy bundled compose in verbatim → write `.env` → pull once → start.
- [ ] **Resumable on failure.** An offline first run must not leave a half-written install directory that the next launch reads as installed. Tested against a fake whose `pull` fails.
- [ ] `.env` writing is the launcher's *only* writable artifact. A test asserts the compose file written matches the bundled one byte-for-byte.

Tests: validation predicates and `.env` serialization (`cargo test`); any
pure formatting on the UI side (vitest).

---

### Phase 5 — Actions, settings, browser handoff

- [ ] **Run** — `up -d`, wait for health, reflect `Running`.
- [ ] **Stop** — `down`.
- [ ] **Update** — `pull` then recreate, only on explicit click.
- [ ] **Browser handoff** — after health passes, open the system browser at `http://localhost:<port>`. Health-gated, never a fixed sleep; a cold start against an empty Mongo volume is much slower than a warm one.
- [ ] Settings screen: storage location, port, network exposure. Each rewrites `.env` and recreates.
- [ ] The network toggle reads **"Allow access from other devices on my network"** — default off, and turning it *on* is what opens exposure. Default-locked-down is the spec's explicit framing.
- [ ] Changing storage location says at the point of change that existing data is not moved.
- [ ] **Closing the window quits the launcher and leaves the stack running**, stated in the UI. No tray icon.
- [ ] Named error states with compose output viewable, not a generic dialog: port in use (at setup *and* again at Run), unshared macOS path, pull failure/offline, daemon died, disk full during pull.

---

### Phase 6 — Update checking

- [ ] Cheap registry manifest check on launch comparing local `:latest` digest against the registry's, deciding only whether the Update button appears.
- [ ] Non-blocking; fails silently offline. A hung or slow registry must never delay the window.
- [ ] Never pulls unasked.
- [ ] `.env` gets `BIOFLOW_TAG=latest`; setup does not ask for it.

**Blocked on #37 for real verification.** Until images are published there is no
registry manifest to compare against, so this phase ships with its logic
unit-tested against a fake and its live path verified after #37 lands. Ordering
it last is what keeps the rest of #28 unblocked.

---

### Phase 7 — Close out

- [ ] Manual verification on macOS (available now), and on Windows and Linux (the acceptance criterion still open on #28). If either platform is unavailable, say so on the issue rather than checking the box.
- [ ] `docs/TODO.md`: if a "Helper install program" entry is open, append ` — FIXED` with what shipped, note where the code lives, say what this implementation did differently from the spec, and move the whole entry to `docs/TODO-done.md`.
- [ ] Update epic #4's body — the spec notes it still says the launcher lives outside this repository, which is no longer true.
- [ ] Check off #28's acceptance criteria that this work actually satisfies. The "builds and runs on macOS, Windows, and Linux" box is honest only for platforms actually exercised.

## Non-goals — restated because each is a thing an implementer would otherwise add

- Does not create the initial BioFlow profile. This is epic #4's remaining open acceptance criterion and the easiest boundary to erode: at install time no API exists to create one against, and duplicating the `Profile` schema would create a second way to make a profile that drifts from the first.
- Does not install Docker.
- Does not generate or template compose YAML.
- No system tray icon, now or later.
- Does not upgrade itself.
- Does not manage optional tool images (#40, blocked by epic #5).
- Does not repair the stack, delete volumes, or retry indefinitely.

## Risks

- **Tauri v2 on three platforms is the unknown here**, not the logic. Phase 2 is deliberately a bare window: if the toolchain fights back, that surfaces before Phases 3–6 are built on top of it.
- **The macOS file-sharing check is heuristic.** Docker Desktop's shared-roots list is not a stable public API. A warning that is occasionally wrong in the cautious direction is still better than an empty `/data` with no explanation — but it is a warning, never a hard block.
- **Phase 1's loopback tightening affects local development**, not just installed users. It is separated into its own commit so it can be reverted alone.
