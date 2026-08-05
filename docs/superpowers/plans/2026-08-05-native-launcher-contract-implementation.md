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

- [x] `docker-compose.yml`: `web` ports → `"${BIND_ADDRESS:-127.0.0.1}:${WEB_PORT:-5173}:80"`.
- [x] `docker-compose.yml`: `api` ports → `"${BIND_ADDRESS:-127.0.0.1}:${API_PORT:-8000}:8000"`.
- [x] `.env.example`: document `WEB_PORT`, `API_PORT`, `BIND_ADDRESS`, `BIOFLOW_TAG`, each with the default and a line on what it does. `BIND_ADDRESS` gets the sentence explaining that `0.0.0.0` exposes an unauthenticated stack to the network.
- [x] Verified via `./ops/worktree-up.sh` and `docker compose config` (resolves to `host_ip: 127.0.0.1` by default, follows `BIND_ADDRESS` overrides) rather than a literal `curl` from another device, which wasn't available in this environment.

**This tightens local development's default from `0.0.0.0` to loopback.** The
spec calls that deliberate. It is the one change in this plan a user can feel
without installing anything, so it goes in its own commit with that stated in
the message.

Green bar: `docker compose exec api python -m pytest tests/ -q` from the main
repo root (no backend code changed; this is a regression check that nothing
about the stack broke).

---

### Phase 2 — Scaffold `launcher/`

- [x] `cargo tauri init` (Tauri v2) into `launcher/`, wired to a Vite + React 18 + TS frontend mirroring `frontend/`'s config.
- [x] `launcher/package.json` with `test: vitest run` and `lint: tsc --noEmit`, matching `frontend/`.
- [x] Bundle `docker-compose.yml` as a build-time resource — `tauri.conf.json`'s `bundle.resources` maps `../../docker-compose.yml` (real path from `launcher/src-tauri/`) to `docker-compose.yml` in the bundle. Verified byte-for-byte identical by building a real `.app` and diffing the bundled file against the root one. Documented in `launcher/README.md`, since JSON has no comments.
- [x] `.gitignore`: root `.gitignore` already covers `node_modules/` and `dist/` repo-wide; `launcher/src-tauri/.gitignore` (Tauri-generated) covers `/target/`.
- [x] Window opens and renders on macOS — confirmed by building and launching the `.app`, process stays alive (no AppleScript/screenshot access in this environment to visually confirm the text, but the process surviving past render is the available signal).

Green bar: `cargo build` and `npm run lint` in `launcher/` both succeed.

---

### Phase 3 — The Docker interface and the state machine

The heart of the issue. No UI beyond a debug dump of the current state.

- [x] Defined the trait the spec names — `probe`, `up`, `down`, `ps`, `pull` — plus `health` (the API healthcheck) and `manifest_digest_differs` (Phase 6's manifest check, exposed through the same seam). Real implementation (`ShellDocker`) shelling out to `docker` with `--project-directory <install dir>`; fully scriptable fake (`FakeDocker`) for tests.
- [x] Implemented the four states: `NotInstalled`, `DockerUnavailable { installed: bool }`, `Stopped`, `Running`.
- [x] **`Running` is health-gated, not container-gated.**
- [x] Docker auto-start (`open -a Docker` / `systemctl --user start docker` / Windows launch) behind a 60-second timeout, with sleep/elapsed injected so the timeout test runs instantly rather than for a real minute.
- [x] Status poll (`state::evaluate`) re-evaluates from scratch every call, never cached.

Tests (`cargo test`, against the fake) — all 5 present and passing:
- [x] Each state is reached from the inputs that define it.
- [x] Containers-up-but-unhealthy → `Stopped`.
- [x] Daemon dies while `Running` → `DockerUnavailable`.
- [x] Auto-start timeout fires and falls back.
- [x] Missing `docker` binary → `DockerUnavailable { installed: false }`, distinct from installed-but-stopped.

Green bar: `cargo test` in `launcher/src-tauri`, all passing, count read rather
than exit code.

---

### Phase 4 — First-run setup

- [x] Three questions with per-OS defaults: storage location (`BIOINFO_HOME`), install directory, port (`WEB_PORT`). (`setup::defaults`, plus a `SetupWizard.tsx` UI added after Phase 5 to actually reach these from `NotInstalled`.)
- [x] Validation:
  - [x] Storage path exists and is writable — probed by an actual write, not `stat`.
  - [x] **macOS file-sharing pre-flight**: warns when the path is outside the given shared roots. `shared_roots` is currently just `$HOME` (no way yet to read Docker Desktop's actual file-sharing config), which is the safe, over-cautious direction per the spec.
  - [x] Port is free, **verified by binding it**.
- [x] Write install directory → copy bundled compose in verbatim → write `.env` → pull → up (`setup::install`).
- [x] **Resumable on failure.** Tested: a failed `pull` leaves the compose file and `.env` already on disk; a second attempt on the same directory succeeds without redoing those steps.
- [x] `.env` writing is the launcher's only writable artifact; a test asserts the compose file written matches the bundled one byte-for-byte.

Tests: 11 in `setup::{defaults,validate,install}` (`cargo test`); no pure
UI-side logic emerged worth its own `.test.ts` in this phase (mostly JSX).

---

### Phase 5 — Actions, settings, browser handoff

- [x] **Run** — `up -d`, wait for health (`actions::run`), reflect `Running`.
- [x] **Stop** — `down` (`actions::stop`).
- [x] **Update** — `pull` then recreate, only on explicit click (`actions::update`, `check_for_update` never calls it).
- [x] **Browser handoff** — health-gated (`commands::run_stack` calls `tauri-plugin-opener` only after `RunOutcome::Running`), never a fixed sleep.
- [x] Settings screen: storage location, port, network exposure (`Settings.tsx`, `settings::apply`). Each rewrites `.env` and recreates.
- [x] The network toggle reads **"Allow access from other devices on my network"** verbatim, default off.
- [x] Changing storage location shows the not-moved note at the point of change (`Settings.tsx`'s `storageChanged` conditional).
- [x] **Window-close note** stated in the UI (`App.tsx`). No tray icon anywhere in this implementation.
- [x] Named, typed error outcomes with raw compose/setup output surfaced as text (not a generic dialog) — `RunOutcome`/`StopOutcome`/`UpdateOutcome`/`InstallError`/`SettingsUpdateError`, each rendered via the UI's `<pre role="alert">` blocks. Port-in-use is checked at setup (`validate_setup_port`) and implicitly again at Run (a real port collision surfaces through `ComposeFailed`'s raw output, since compose itself will refuse); disk-full and pull-failure both flow through as raw compose output the same way.

---

### Phase 6 — Update checking

- [x] Cheap registry manifest check on launch (`update_check::update_available`, polled every 5 minutes from `App.tsx` while `Running`) deciding only whether the Update button appears.
- [x] Non-blocking (`commands::check_for_update` runs on Tauri's `spawn_blocking` pool, bounded by `GhcrClient`'s own 3s timeout); fails silently — every failure mode (offline, timeout, non-2xx, malformed header) collapses to "no update."
- [x] Never pulls unasked — `check_for_update` has no path to `docker compose pull`; only `actions::update`, from an explicit click, does.
- [x] `.env` gets `BIOFLOW_TAG=latest`; setup does not ask for it (done in Phase 4, unchanged here).

**Real-network finding, not anticipated by this plan:** GHCR requires a
bearer-token exchange even for a public, anonymous manifest read (unlike
Docker Hub's unauthenticated path). `GhcrClient` implements the token exchange;
an `#[ignore]`-marked test against a real public GHCR package
(`homebrew/core/rust`, since BioFlow's own images don't exist until #37)
verifies the mechanics work end-to-end. Every `FakeRegistry`-backed unit test
would have passed regardless of whether that auth step existed — this was only
caught by deliberately running the ignored test against the real network.

---

### Phase 7 — Close out

- [x] Manual verification on **macOS only** — no Windows or Linux machine available in this environment. Said so explicitly [in a comment on #28](https://github.com/syntheticgio/bioflow/issues/28#issuecomment-5192413520) rather than checking that box.
- [x] `docs/TODO.md` — **deviated from this item's literal instruction on purpose.** The "full install" pre-pull-optional-tools checkbox in the "Helper install program" entry is explicitly out of scope for #28 (deferred to #40), so per CLAUDE.md's own rule for partially-resolved entries, the entry stays in `docs/TODO.md` rather than moving to `docs/TODO-done.md`. Instead it's annotated `— PARTIALLY FIXED` with what shipped, where the code lives, what differed from the plan (in-tree placement, the GHCR auth finding), and exactly what's still open and why.
- [x] Epic #4's body already contained the in-tree-placement correction by the time this phase ran — someone/some process applied it before this session started, so no edit was needed. Posted a short cross-reference comment on #4 instead, pointing at #28's status.
- [x] #28's acceptance criteria: the three already-checked boxes were already checked before this session; the fourth ("builds and runs on macOS, Windows, and Linux") stays unchecked, with a comment explaining macOS-only verification.

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
