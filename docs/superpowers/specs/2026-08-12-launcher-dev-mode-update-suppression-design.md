# Launcher: suppress the update check outside Release mode

Closes the remaining half of
[#241](https://github.com/syntheticgio/bioflow/issues/241).

## Background

Issue #241 asks for a launcher "dev mode" that "should prevent it from
trying to manage the version (i.e. we can use the local built
containers)," and speculates it "needs to take a parameter to the repo
locally and manages the container builds."

**The second half already shipped.** PR #288 landed a complete Developer
mode:

- `Settings.tsx` offers `Developer (local build)` in the Version dropdown,
  with a "Local repo path" field and a **Rebuild** button.
- `settings::render_developer_override` writes a
  `docker-compose.override.yml` pointing api/worker/web at `:local` images
  with `build:` stanzas against the checkout.
- `settings::apply` runs `docker compose build` before `up`, reporting
  `BuildFailed` distinctly from `RecreateFailed`.
- `.env` records `BIOFLOW_DEVELOPER_REPO=<path>` *instead of*
  `BIOFLOW_TAG`; switching back to Release deletes the override.

**The first half did not.** The update path is still version-managed
unconditionally, which is what this spec fixes.

## Problem

`check_for_update` takes no arguments, reads no settings, and hard-codes
the tag `"latest"`:

```rust
// commands.rs:517
pub async fn check_for_update() -> bool {
    CHECKABLE_IMAGES.iter().any(|image| {
        update_check::update_available(&registry, &local, image, "latest") == Some(true)
    })
}
```

`App.tsx` polls it every five minutes whenever the stack is Running and
renders a `btn-warn` "Update available" on a truthy result. Two distinct
defects follow:

**Developer mode.** The check compares GHCR's published `latest` against
locally built `:local` images. They are unrelated, so the button appears
essentially whenever `latest` moves. Clicking it is *not* the silent
clobber it first appears to be — `docker compose pull` reads the same
auto-loaded `docker-compose.override.yml` dev mode writes, and Compose
skips services carrying a `build:` stanza, so the pull is close to a
no-op on api/worker/web. The damage is a persistent, meaningless nag and
a confusing state, not data loss.

**Alpha/Beta mode.** The same hard-coded `"latest"` is wrong for pinned
pre-release stages. Running `0.3.0-alpha` means being told an update
exists whenever `latest` moves — a comparison against a tag the user
deliberately is not on.

`update_check::update_available` already accepts `tag` as a parameter.
The plumbing for a correct fix exists; only the caller is wrong.

### Audited and explicitly out of scope

Walked for the same registry-versioning assumption, found clean:

- `optional_tools.rs` — prefetch pulls biocontainer *tool* images via
  `pull_image`, never BioFlow's own service images. Orthogonal to version
  mode.
- `migrate.rs` — moves storage; touches no image tags.
- `docker/shell.rs:207` — `manifest_digest_differs` is a stub returning
  `None`, deferred pending #37. Dead path; the live check is `GhcrClient`.

## The rule

> **Release tracks a moving `latest` and gets an update check. Every other
> mode is pinned or local, and does not.**

Alpha and Beta are suppressed rather than redirected because their tags
are **immutable and version-pinned**: `classify_version_options` picks the
highest `X.Y.Z-alpha` from the registry tag list, so a digest check
against the user's own pinned tag would essentially always report "no
update," even after `0.4.0-alpha` is published. Digest comparison is the
wrong mechanism for a pinned stage.

Teaching Alpha/Beta to notice a *newer stage tag* is the genuinely useful
version of that behavior, but it requires `update_stack` to rewrite the
pinned tag in `.env` — a feature, not a consistency fix. **Filed as a
follow-up, deliberately not in this scope.**

## Architecture

The mode is **derived, not stored.** No new `.env` line, no new setting.
Version mode is already fully determined by two lines that
`parse_developer_repo` and `parse_bioflow_tag` read today, and
`Settings.tsx:34` already derives its dropdown state from exactly that
pair. A stored "dev mode" flag would be a second source of truth able to
disagree with `.env` — the failure the `hard_mem_mb` comment in
`settings.rs` warns against ("a toggle plus a number is two controls that
can disagree").

### Rust: one pure function

Added to `update_check.rs`:

```rust
/// Which tag, if any, an update check should compare against.
/// `None` means no check is meaningful for this mode.
pub fn checkable_tag(bioflow_tag: &str, developer_repo: Option<&str>) -> Option<String>
```

| Condition | Result |
|---|---|
| `developer_repo.is_some()` | `None` — local build, no registry counterpart |
| `bioflow_tag == "latest"` | `Some("latest")` — the only moving target |
| anything else | `None` — pinned pre-release |

Developer mode takes precedence when both `.env` lines are somehow
present, matching how `current_settings` already resolves the pair. Pure
and side-effect-free so it is testable without touching GHCR — the same
split `classify_version_options` already follows.

`check_for_update` becomes a thin adapter: read `.env` (as
`current_settings` already does), call `checkable_tag`, short-circuit to
`false` on `None` without any network call. Its signature gains
`State<'_, LauncherApp>` to locate the install dir; it currently takes no
arguments.

`update_check::update_available` is **untouched** — its `tag` parameter
was always correct.

### Frontend: one pure module

`App.tsx` already loads `bioflowTag` and `developerRepo` into state in its
mount effect, so no new IPC call is needed. A pure helper in a new
`launcher/src/update-logic.ts`:

```ts
export type UpdateAffordance =
  | { kind: "hidden" }                      // Release, nothing newer
  | { kind: "available" }                   // Release, update offered
  | { kind: "suppressed"; reason: string };  // dev / alpha / beta
```

Its own module because `wizard-logic.ts`, `settings-logic.ts`, and
`migration-logic.ts` all exist for exactly this purpose. That convention
matters here: the repo has **no jsdom or testing-library setup and zero
`.test.tsx` files**, so a pure module is the only unit-testable seam.

## UI states

Replacing the single `updateAvailable &&` guard at `App.tsx:327`:

| Mode | Button |
|---|---|
| Release, up to date | absent (unchanged) |
| Release, newer image | `btn-warn` "Update available" (unchanged) |
| Developer | visible, **disabled** — *Developer mode — use Rebuild in Settings* |
| Alpha / Beta | visible, **disabled** — *Pinned to `0.3.0-alpha` — change version in Settings* |

Disabled-with-a-hint rather than hidden, following the `role="note"` +
`field-hint` treatment `Settings.tsx:146` already uses for storage
location and port while running. A control that vanishes silently leaves a
user who forgot their mode with no explanation; one that explains itself
points at the thing that *is* the update path. The dev hint names Rebuild
for that reason; the pinned hint names the actual tag so the reason is
concrete.

**Polling.** The five-minute `setInterval` at `App.tsx:96` gains the same
guard, so suppressed modes stop polling rather than polling and
discarding. Correctness lives in Rust; skipping the timer avoids a
pointless IPC round-trip every five minutes.

## Error handling

Unchanged, and silent by default. `check_for_update` returning `false` on
any failure is deliberate per its own docstring — the UI must not
distinguish "offline" from "nothing newer." Suppression returns that same
`false` through a path that makes no network call, adding no new failure
mode.

`update_stack` is untouched: already explicit-click-only per
`actions.rs:104`, and a disabled button cannot reach it.

## Testing

**Rust** (`update_check.rs`) — unit tests on `checkable_tag`:

- developer repo set → `None`
- `"latest"` → `Some("latest")`
- `"0.3.0-alpha"` → `None`
- `"0.4.0-beta"` → `None`
- both `.env` lines present → `None` (developer precedence; hand-edited
  `.env` is explicitly tolerated by the existing parsers)

**TypeScript** (`launcher/src/update-logic.test.ts`) — the three
affordance states plus hint text, matching the existing `*-logic.test.ts`
files.

**Manual** — the two disabled states verified in the launcher, since
button rendering has no headless test path in this repo.

## Files touched

| File | Change |
|---|---|
| `launcher/src-tauri/src/update_check.rs` | add `checkable_tag` + tests |
| `launcher/src-tauri/src/commands.rs` | `check_for_update` reads `.env`, short-circuits |
| `launcher/src/update-logic.ts` | new — affordance rule |
| `launcher/src/update-logic.test.ts` | new — its tests |
| `launcher/src/App.tsx` | three-state button, guarded polling |
| `launcher/src/commands.ts` | signature update if the IPC shape changes |
