# Launcher version switching — design

**Date:** 2026-08-11

**Issue:** [#242](https://github.com/syntheticgio/bioflow/issues/242)

**Spec this extends:** [`docs/superpowers/specs/2026-08-04-native-launcher-contract-design.md`](./2026-08-04-native-launcher-contract-design.md) — specifically the "Update checking" and "Settings after install" sections this document treats as settled constraints.

## Goal

Let a user pick which BioFlow release line the launcher runs against — `release`,
`alpha`, `beta`, or `developer` — from a dropdown on the Settings screen, and
have that choice take effect with a single Apply: the launcher rewrites its
`.env` (and, for developer mode, layers a local-build override) and recreates
the stack. The dropdown is editable while the stack is Running (a version change
does not touch volumes), and the Apply shows the existing "Applying…" spinner
while the pull+recreate runs.

End users use `release`/`alpha`/`beta`. `developer` is a dev-only escape hatch:
it builds the images from a locally-checked-out repo path and runs those, with a
Rebuild button to refresh the local images without re-choosing the stage.

## Why the four options are four different things

The stage names the issue calls out, plus `developer`, do not reduce to four
equivalent tags — they resolve to images in two different ways, and the
launcher cannot ship a compile-time list of the pre-release ones:

- **`release` resolves to `latest`.** `:latest` only advances on a `v*`
  (production) tag, and never on an alpha/beta tag — see
  `.github/workflows/publish-images.yml`, which moves `latest` only on a `v*`
  tag, and the base spec's
  "Update checking" note. `release` needs no registry lookup; it is a stable
  default.
- **`alpha` / `beta` resolve to concrete, immutable** tags (`0.3.0-alpha`,
  `0.3.0-beta`). The launcher cannot know future pre-release versions, so each
  stage resolves to *the newest currently-published tag matching that stage's
  suffix*, discovered from GHCR at dropdown-render time. "beta" often has no tag
  yet (none existed on 2026-08-11 despite `0.3.0-alpha`), so the beta option
  must be able to come back empty and render disabled — not error.
- **`developer` resolves to a local build.** There is no tag for it. It picks
  the repo the user is hacking on, runs `docker compose build` against it, and
  points the stack at the locally-built image. `BIOFLOW_TAG` is not the
  mechanism here — this is the only option that cannot be expressed as a single
  `.env` line, and it is the only one that needs an extra input (the repo path)
  and an extra action (Rebuild).

So three of the four options write a `BIOFLOW_TAG` value (release=`latest`,
alpha/beta=`<resolved tag>`), and `developer` instead writes a build override
and leaves `BIOFLOW_TAG` out of play. Three registry stages plus one local-build
stage, sharing the Settings Apply/recreate path and the `applying` spinner.

## What the dropdown drives

The shared contract is unchanged: a single image reference controlled per
service by `docker-compose.yml`'s `${BIOFLOW_TAG:-latest}` for `api`/`worker`
(`bioflow-backend`) and `web` (`bioflow-web`). Both images are published with an
identical tag set on every build, so one choice drives both — confirmed against
live GHCR on 2026-08-11 (`syntheticgio/bioflow-backend` and `syntheticgio/bioflow-web`
carry the same tags).

Resolved-option table:

| Dropdown | `BIOFLOW_TAG` written | Image source | Registry needed? |
|---|---|---|---|
| release | `latest` | `ghcr.io/syntheticgio/bioflow-{backend,web}:latest` | no |
| alpha | newest `*-alpha` (e.g. `0.3.0-alpha`) | GHCR `*-alpha` tag | yes |
| beta | newest `*-beta`, or `null` → disabled | GHCR `*-beta` tag | yes |
| developer | *(none — override-driven)* | locally-built images from a repo path | no |

`latest` must never be written for a pre-release stage, and a pre-release tag
must never be written unless the user picked it — the dropdown never silently
downgrades an existing `release` to an `alpha`. Switching back to release
restores `BIOFLOW_TAG=latest` and the next Apply pulls production again. A
hand-set `sha-*` or pinned `0.2.6` (the `.env.example` "pin for debugging"
escape) survives as a **custom** read-only state: the dropdown renders the raw
tag visibly and lets any stage be re-chosen, without reinterpreting the custom
value.

## Concrete changes

### 1. Read and write the choice through the existing settings path

`settings::CurrentSettings` (`launcher/src-tauri/src/settings.rs`) grows the
data the dropdown needs. The cleanest fit to the existing `bioflow_tag: String`
proposal is to keep that field for the three registry stages and add a separate
`developer_repo: Option<PathBuf>` for developer mode:

- `settings.rs:76` — `render_env`'s hard-coded `latest` becomes
  `BIOFLOW_TAG={bioflow_tag}`.
- A new `const DEFAULT_TAG: &str = "latest"` (hoisted from the three literal
  spots: `install.rs:98`, `settings.rs:76`, `commands.rs:1058`) so
  first-run, settings, and remote-node install all default together.
- **`render_env` branches on developer:** when `developer_repo` is `Some(path)`,
  it omits the `BIOFLOW_TAG` line (compose's `${BIOFLOW_TAG:-latest}` fallback
  is harmless because the override below supplies `image:` explicitly) and the
  override file is what carries the build. When it is `None`, `render_env`
  writes `BIOFLOW_TAG=<tag>` exactly as the registry stages require.
- `commands::ApplySettingsArgs` gains `bioflow_tag: String` plus
  `developer_repo: Option<String>`. `commands.rs:465` (`apply_settings`)
  forwards both into `CurrentSettings`.
- `finish_storage_migration` (`commands.rs:720`) currently builds
  `CurrentSettings` from port/network/hard-mem and nothing else — that would
  now drop `bioflow_tag` and `developer_repo`. Mirror how it already preserves
  `hard_mem_mb` by reading it back from `.env`: parse `BIOFLOW_TAG` and the
  dev-repo path from disk and carry them through, so a storage migration never
  silently reverts the version choice.
- `current_settings` (`commands.rs:812`) returns only `hard_mem_mb` and `port`
  today. Add `parse_bioflow_tag` (mirror `parse_web_port`) so the dropdown
  shows the active stage on a relaunch, and surface the dev-repo path.
  `CurrentSettingsDto` gains `bioflow_tag: String` and `developer_repo:
  Option<String>`.

### 2. Populate the registry stages from GHCR

A new `list_version_options` Tauri command returns the three registry-resolved
options to the UI:

```ts
// launcher/src/commands.ts
listVersionOptions(): Promise<VersionOptions>
// { release: string; alpha: string | null; beta: string | null }
```

Behind it, extend the `RegistryClient` trait (`update_check.rs:23`) with a
`tags_for(image: &str) -> Option<Vec<String>>` method — the same seam so the
classification stays unit-testable against a fake, per the repo's "logic lives
behind a fake" rule. `GhcrClient` (`update_check.rs:59`) implements it with
`GET /v2/syntheticgio/bioflow-backend/tags/list`; the bearer token it already
fetches for `remote_digest` (one `scope=repository:<image>:pull` exchange)
covers this too. `FakeRegistry` (`update_check.rs:137`) returns a canned list so
a test can classify a mixed tag set without a network call.

The real command classifies each tag: `*-alpha` → alpha candidate, `*-beta` →
beta candidate, `latest`/`*-0` → release candidate, `sha-*` ignored. Within a
stage, `sort -V` picks the newest (consistent with the spec's
`0.2.6 < 0.3.0-alpha < 0.3.0-beta < 0.3.0` precedence). It runs on `spawn_blocking`
bounded by `GhcrClient`'s 3s timeout, fails silently to "alpha/beta disabled,
release still works" — the same collapse-to-default rule `check_for_update`
uses at `commands.rs:505`.

`developer` is not part of this call — it needs no registry. The UI derives the
four-option shape locally: three from `list_version_options`, plus the always
present `developer` entry.

### 3. Developer mode: local build + override file

This is the only piece that does not fit the "one `.env` line, then `up -d`"
shape, because developer mode must make `docker compose` build images from the
user's repo rather than pull a tag. The mechanism that respects "the compose
file is shipped, never generated" (base spec section): the launcher writes
**an override file** to the install dir, layered alongside the untouched base
`docker-compose.yml`:

- `Settings.tsx` prompts for the local repo path when `developer` is first
  selected (a path picker + validation that the path holds a usable compose
  context, e.g. `backend/Dockerfile`). The picked path is stored in `.env` as
  `BIOFLOW_DEVELOPER_REPO=<path>` (a new variable, ignored by the GHCR path and
  by `docker-compose.yml` itself, so end-user stacks are unaffected).
- On Apply (developer mode), `settings::apply` writes the override
  `docker-compose.dev.yml` into the install dir with, per service,
  `build: { context: <repo path> }` and `image: <same name>:local` so the
  local build supplies the image the base file references. `BIOFLOW_TAG` is
  omitted from `.env` (the override pins `image:` to `...:local`).
- `up` is then `docker compose --project-directory <install dir>
  -f docker-compose.yml -f docker-compose.dev.yml up -d --build` — a build on
  the first Apply, a fast recreate thereafter.
- **Rebuild button** (visible only when developer is the active stage): runs
  `docker compose --project-directory <install dir> -f docker-compose.yml
  -f docker-compose.dev.yml build` and surfaces the raw compose output on
  failure (same `<pre role="alert">` shape as a `RecreateFailed`).

**The open mechanic** — see Open questions #3 — is getting the override file
through `ShellDocker::up`. `ShellDocker` currently invokes
`docker compose --project-directory <install dir>` (per the base spec's "run
with `--project-directory` set to the install directory"); developer mode needs
it to also pass `-f docker-compose.dev.yml`. That either means `DockerBackend::up`
takes an optional override list, or developer mode is a distinct command path.
The stub fallback if this isn't wired in this issue: `developer` is selectable
in the dropdown but Apply falls back to `latest` with a note ("local build not
yet wired; using release"), so the UI ships complete and the plumbing lands in
a follow-up.

> The AGENTS.md `ops/worktree-up.sh` caveat (running `docker compose up` inside
> a worktree silently repoints the 5173 stack) is *not* triggered here: developer
> mode is a user-chosen checkout path distinct from the launcher's own install
> dir, and the launcher keeps its project name pinned. But a developer who
> point developer-mode at the launcher's own checkout would see the usual
> worktree/project-name collision — worth a doc warning, not a code guard.

### 4. Settings screen changes

`launcher/src/Settings.tsx` adds a `Version` field to the dialog:

- A `<select>` bound to a `version` (Stage) state, options `release` | `alpha`
  | `beta` | `developer`, with `alpha`/`beta` disabled (and titled) when their
  resolved tag is `null`. The current stage is recovered from `currentSettings()`
  via the reverse map below.
- Selecting `developer` reveals the repo-path picker (inline) and reveals the
  **Rebuild** button next to Apply. Selecting any other stage hides both.
- Editable while Running (this is the `network_exposed`/hard-mem precedent, not
  the storage/port `!running` lock — version is a recreate, not a volume move).
- Apply reuses the existing `applying` → "Applying…" spinner and the
  `applySettings({ ..., bioflowTag, developerRepo })` → `onApplied(...)` round
  trip. No new polling channel: a pull+recreate, or a local build+recreate, is
  bounded and synchronous from the user's view, the same shape as every other
  settings change.

`launcher/src/types.ts`'s `Settings` and the DTOs in `commands.ts` carry the new
fields. The tag→stage reverse map (only place the UI knows the convention) is:
`latest` → `release`; `*-alpha` → `alpha`; `*-beta` → `beta`; a dev-repo path
present → `developer`; anything else (pinned `0.2.6`, `sha-*`) → the
**custom** state above.

### Gating summary

| Field | Editable while Running? | Mechanism |
|---|---|---|
| storage location | no — read-only, "Stop first" | volume mount; changing it does not move data |
| port | no — read-only, "Stop first" | `LauncherApp.port` in-memory cache + bind |
| network exposure | **yes** | recreate via `up -d`, no volume impact |
| hard memory limit | **yes** | recreate via `up -d`; `WORKER_REPLICAS=1` written |
| **version (new)** | **yes** | recreate via `up -d`; new tag pulled, or local build via override |
| **rebuild (dev only)** | **yes** | `build` against the override, then recreate |

The `!running` lock lives in the UI (`Settings.tsx`), not in `settings::apply`;
version and rebuild are simply not added to that locked set.

## Edge cases

- **Registry unreachable / no `*-beta` published.** `list_version_options`
  returns `beta: null`; the UI disables the beta option with a tooltip ("no beta
  release available"). `alpha` may also be null. `release` is always available.
  An existing stack on a pre-release tag is unaffected — the dropdown just
  cannot offer another registry stage until the registry answers. `developer`
  never depends on the registry and stays enabled.
- **A hand-set or previously-pinned tag** (`sha-*`, pinned `0.2.6` from
  debugging). Reverse-map does not match a stage → renders as the **custom**
  read-only state (raw tag visible, stage options still listed). Any stage
  selection overwrites the custom value; "release" writes `latest`. This is the
  `.env.example` "pin for debugging" escape, unchanged.
- **First-run default.** Install writes `BIOFLOW_TAG=latest` (the constant),
  so every new stack starts on release. A developer switches to `developer`
  from Settings to start building locally; the launcher never auto-pins an
  end user to a pre-release or a local build.
- **Developer mode, repo path changed.** Re-Apply with a new repo path rewrites
  the override's `build.context` and rebuilds. Switching *away* from developer
  to a registry stage removes the override file's effect (and leaves
  `BIOFLOW_DEVELOPER_REPO` in `.env`, inert for the GHCR path) so the stack
  falls back to a pulled tag on the next Apply.
- **Developer mode, no path yet.** `developer` selected but no repo path saved:
  Apply prompts for the path (or refuses with a focused error if the path is
  invalid/empty), rather than silently building from nowhere.
- **Concurrent settings applies.** `settings::apply` is invoked from
  `spawn_blocking` per call with no shared lock (the existing limitation).
  Two overlapping applies can race on `.env` / on concurrent `docker compose`
  runs — accepted today, unchanged here, whether or not developer mode is in
  use.

## Error handling

This leans on the named-outcome rendering the base spec already mandates for
settings; version-switch adds no new *kind* of failure, only tags them onto
existing ones:

- **`RecreateFailed { output }`** from `settings::apply` already surfaces raw
  compose output when `up -d` fails (e.g. a tag that does not exist, a pull
  failure). Shown verbatim in `Settings.tsx`'s `error` `<pre role="alert">`,
  identical to how a port-in-use at Run reads.
- **Registry list failure** collapses to "alpha/beta disabled," same as
  `check_for_update` collapsing to "no update" (`commands.rs:505`). The
  launcher must not error on a flaky registry.
- **`.env` write failure** is already `CouldNotWriteEnv`, surfaced as
  "could not write .env: {reason}."
- **Developer build failure** (e.g. a broken Dockerfile in the chosen repo)
  surfaces via the Rebuild/Apply compose output through `RecreateFailed`, same
  path — no new error variant. The first Apply in developer mode pulls the
  build's raw output into the same alert, since a build failure reads as a
  compose failure.

## Testing

- **`settings.rs`** — extend the existing `render_env` round-trip and `apply`
  tests: a registry tag round-trips into `BIOFLOW_TAG=<tag>`; `render_env` omits
  `BIOFLOW_TAG` (and writes the override contract) when `developer_repo` is set;
  the `latest` default constant is asserted. The `commands.rs:1247`
  `round_trips_through_render_env` test is updated for the new fields.
- **`update_check.rs`** — add `tags_for` to `FakeRegistry` returning a canned
  list, and a test classifying a mixed tag set (`0.1.0`, `0.2.6`,
  `0.3.0-alpha`, `latest`, `sha-3055f0e`) into release=`latest`,
  alpha=`0.3.0-alpha`, beta=`null`. Add the `#[ignore]` real-network test
  against a public GHCR package (mirror the `homebrew/core/rust` precedent at
  `update_check.rs:221`) to prove the `/tags/list` mechanics, run only with
  `cargo test -- --ignored`.
- **`commands.rs`** — a `current_settings` test asserts `bioflow_tag` is parsed
  back from an `.env` containing `BIOFLOW_TAG=0.3.0-alpha`, and defaults to
  `latest` when the line is absent (mirror `parse_web_port`'s None→DEFAULT).
- **`launcher/src`** — no `.test.tsx` for the dropdown (the repo ships none and
  expects none); the suffix classification and tag→stage reverse map live in
  Rust/TS pure-logic where they are faked-and-unit-tested. A vitest on the
  reverse map is plausible if that function ends up in TS, matching
  `frontend/`'s `.test.ts` pattern.
- **Manual** — on macOS: switch release→alpha, Apply, watch the spinner, confirm
  the stack recreates and `docker images` shows `0.3.0-alpha` served; switch
  back to release and confirm `latest` is pulled again. For developer: pick a
  local repo, Apply, confirm the override file is written and images build from
  `build:` not GHCR; hit Rebuild and confirm a fresh local build. Offline:
  confirm alpha/beta are disabled and release + developer still work. (Mirrors
  the manual-verification bar already used for the launcher.)

## Non-goals (for this issue)

- Does not auto-refresh within a stage. Picking `release` again does not re-pull
  `latest`; that is the Update button's job, unchanged.
- Does not offer every historical tag or `sha-*` pin as a first-class option;
  those remain the "custom" escape hatch, off the default dropdown.
- Does not add a web-UI "this stack is on an old version" nudge — that is the
  launcher's Update button, already specified.
- Does **not** guarantee the developer-mode override plumbing ships in this
  issue if it bumps into the `ShellDocker::up` seam (see developer-mode stub
  fallback above). `developer` being selectable but reverting to release with a
  note is an acceptable first slice; local-build wiring can land in the
  follow-up.

## Open questions

1. **Newest-of-stage vs. every tag for alpha/beta.** Proposed: newest of each
   stage (three dropdown items + developer), with the "custom / raw tag" escape
   surviving in `.env`. Listing every historical pre-release is more flexible
   but invites pinning to a stale point release. (Resolved in favor of
   newest-of-stage unless you want history.)
2. **Version-switch Apply: explicit `pull` or rely on `up -d`?** Proposed: rely
   on `docker compose up -d`'s per-tag pull, because a stage change always
   lands on a *different* tag than what is cached, so Compose pulls it; an
   explicit pull only matters for same-tag refresh, which is Update's job. If
   you'd rather the Apply also guarantee a digest refresh on a same-stage
   re-Apply, flip to `pull` + `up -d` — the cost is a slower Apply the spinner
   already covers. (For developer mode this is moot: `--build` is explicit
   there.)
3. **How the developer override reaches `ShellDocker::up`.** The base
   `DockerBackend` trait and `ShellDocker` invoke
   `docker compose --project-directory <install dir>`; developer mode needs
   `-f docker-compose.dev.yml` layered on top (build + local image). Options:
   (a) add an optional override-file list to `DockerBackend::up` and thread it
   through `settings::apply` / `apply_settings` when `developer_repo` is set; or
   (b) make developer Apply a dedicated command path that calls
   `docker compose ... -f ... build` then `up -d --build`. Option (a) keeps one
   seam but widens the trait; option (b) isolates dev mode but adds a second
   command. Picked (a) is my lean — it reuses `settings::apply` verbatim, only
   parameterizing the compose invocation — but it touches the `DockerBackend`
   trait, so flag it before cutting.
4. **Developer-mode image tag.** Proposed `...:local` as the local build tag
   in the override's `image:`. Confirm that does not collide with a GHCR tag
   named `local` (today's tag set has no such tag, and GHCR vs local daemon are
   distinct namespaces, so the collision surface is only cosmetic: a stale
   local `...:local` image from a previous build). Acceptable.
