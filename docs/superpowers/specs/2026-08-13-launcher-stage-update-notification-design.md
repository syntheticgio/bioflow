# Launcher: notify and update when a newer alpha/beta stage tag publishes

Follow-up from [#324](https://github.com/syntheticgio/bioflow/issues/324),
deliberately scoped out of the dev-mode update suppression spec
([2026-08-12-launcher-dev-mode-update-suppression-design.md](2026-08-12-launcher-dev-mode-update-suppression-design.md)).

## Background

The suppression spec established one rule: Release tracks a moving `latest`
and gets an update check; every other mode is pinned or local and does not.
Alpha and Beta are suppressed — the Update button renders disabled with a
"Pinned to `0.3.0-alpha` — change version in Settings" hint.

That suppression is correct given the digest-comparison mechanism, but a user
on `0.3.0-alpha` has no in-app signal when `0.4.0-alpha` publishes. They
find out by opening Settings and reading the dropdown — the only place the
newer tag surfaces today.

## Problem

The gap is that the Update button is either "clickable (Release mode)" or
"disabled with a static hint (everything else)". There is no "a newer stage
tag is available" state for alpha/beta users.

The mechanism to detect a newer stage tag already exists: `list_version_options`
fetches and classifies the GHCR tag list into `VersionOptions { release, alpha,
beta }`. The missing piece is a comparison between the user's pinned tag and
those options, plus the action to act on it.

## The rule

> A user on an alpha or beta stage tag can see and act on a newer forward-
> compatible tag from the same registry, without opening Settings.

"Forward-compatible" means any tag with a strictly greater `(major, minor,
patch, stage_rank)` tuple, where stage rank is alpha=0, beta=1. This allows
alpha→alpha, alpha→beta, beta→beta, and beta→alpha (when the version number
is higher) moves.

## Architecture

Two new seams, kept separate from the existing digest-based `check_for_update`:

1. **`check_stage_update`** — a pure function in `update_check.rs` that
   compares the user's current tag against `VersionOptions` and returns the
   best forward-compatible tag, or `None`.
2. **`update_to_stage`** — a new Tauri command + action function that rewrites
   `BIOFLOW_TAG` in `.env`, pulls new images, and recreates the stack.

### Rust: `check_stage_update`

Added to `update_check.rs`. Pure and side-effect-free so it is testable
without touching GHCR — the same split `classify_version_options` already
follows.

```rust
/// Given the user's current pinned tag and the available version options,
/// returns the best forward-compatible stage tag, or `None`.
pub fn check_stage_update(current_tag: &str, options: &VersionOptions) -> Option<String>
```

Comparison logic:

1. If `current_tag == "latest"`, return `None` — Release has its own path.
2. Parse `current_tag` via `version_tuple` for `-alpha` or `-beta` suffix to
   get `(major, minor, patch, stage_rank)`.
3. Collect candidates from `options.alpha` and `options.beta`.
4. For each candidate, parse it the same way. If `(cand_ver, cand_rank) >
   (current_ver, current_rank)`, it's a forward candidate.
5. Among forward candidates, pick the one with the highest `(major, minor,
   patch, stage_rank)` tuple.
6. Return the tag string of the best candidate, or `None`.

### Rust: `update_to_stage`

A new function in `actions.rs`:

```rust
pub enum UpdateToStageOutcome {
    Updated,
    PullFailed { output: String },
    RecreateFailed { output: String },
}

pub fn update_to_stage<D: DockerBackend>(
    docker: &D,
    install_dir: &str,
    new_tag: &str,
) -> UpdateToStageOutcome
```

It:
1. Reads `.env` from the install directory
2. Replaces the `BIOFLOW_TAG=<old>` line with `BIOFLOW_TAG=<new>` (via a small
   `set_bioflow_tag` helper in `update_check.rs` or `settings.rs`)
3. Writes the updated `.env` back
4. Runs `docker compose pull` (like `update_stack`)
5. Runs `docker compose up -d` (like `update_stack`)
6. Returns the outcome

A new Tauri command in `commands.rs` wraps this:

```rust
#[tauri::command]
pub async fn update_to_stage(app: State<'_, LauncherApp>, tag: String) -> Result<(), String>
```

### Frontend: `update-logic.ts`

A new affordance kind and a pure `checkStageUpdate` function:

```typescript
export type UpdateAffordance =
  | { kind: "hidden" }
  | { kind: "available" }
  | { kind: "stage-update"; targetTag: string }
  | { kind: "suppressed"; reason: string };

export function checkStageUpdate(
  currentTag: string,
  options: VersionOptions,
): string | null;
```

`updateAffordance` gains `versionOptions` as an input. When the current mode
is alpha or beta and a forward tag exists, it returns `stage-update` with the
target tag.

### Frontend: `App.tsx`

The `UpdateButton` component gets a new branch:

- `stage-update` → a clickable `btn-warn` reading `"Update to 0.4.0-alpha"`
- Clicking fires a `window.confirm()` dialog explaining the version change
- On confirm, calls the new `updateToStage` IPC command

The polling `useEffect` gains a parallel path for alpha/beta modes that fetches
`listVersionOptions` (reusing the existing IPC command) and stores the result
in a new `versionOptions` state variable. The 5-minute interval is shared.

## UI states

| Mode | Button |
|---|---|
| Release, up to date | absent (unchanged) |
| Release, newer image | `btn-warn` "Update available" (unchanged) |
| Developer | disabled — *Developer mode — use Rebuild in Settings* (unchanged) |
| Alpha/Beta, no newer tag | disabled — *Pinned to `0.3.0-alpha` — change version in Settings* (unchanged) |
| Alpha/Beta, newer tag exists | `btn-warn` "Update to `0.4.0-alpha`" → confirm() → update |

## Error handling

- **`.env` write failure** — surfaces as a `PullFailed` error with the message
  "Failed to update .env"
- **Pull failure** — surfaces as `PullFailed` with compose output (same as
  `update_stack`)
- **Recreate failure** — surfaces as `RecreateFailed` with compose output (same
  as `update_stack`)
- **Network failure during `listVersionOptions`** — `versionOptions` stays
  `null`, the affordance falls through to the suppressed state. Same silent
  degradation as the Settings dropdown.

## Testing

### Rust (`update_check.rs`)

Unit tests on `check_stage_update`:

| Current tag | Alpha available | Beta available | Expected result |
|---|---|---|---|
| `0.3.0-alpha` | `0.4.0-alpha` | `0.3.0-beta` | `Some("0.4.0-alpha")` |
| `0.3.0-alpha` | `0.3.0-beta` | `0.4.0-beta` | `Some("0.4.0-beta")` |
| `0.3.0-alpha` | `0.3.0-alpha` | `0.3.0-beta` | `Some("0.3.0-beta")` |
| `0.4.0-beta` | `0.4.0-alpha` | `0.4.0-beta` | `None` |
| `0.3.0-alpha` | None | None | `None` |
| `0.3.0-alpha` | `0.2.0-alpha` | None | `None` |
| `latest` | anything | anything | `None` |

### Rust (`actions.rs`)

Unit test on `set_bioflow_tag`:

- Replaces existing `BIOFLOW_TAG=0.3.0-alpha` with `BIOFLOW_TAG=0.4.0-alpha`
- Preserves other lines (storage location, port, etc.)
- Appends `BIOFLOW_TAG` if missing (safety, not expected in practice)

### TypeScript (`update-logic.test.ts`)

Test `checkStageUpdate` with the same cases as the Rust tests.

### Manual

- Both the `stage-update` and `suppressed` states verified in the launcher
  (button rendering has no headless test path in this repo)
- Confirmation dialog appears and blocks the update on cancel
- Full flow verified: click Update → confirm → stack restarts with new tag

## Files touched

| File | Change |
|---|---|
| `launcher/src-tauri/src/update_check.rs` | Add `check_stage_update`, `set_bioflow_tag`, tests |
| `launcher/src-tauri/src/actions.rs` | Add `UpdateToStageOutcome`, `update_to_stage` |
| `launcher/src-tauri/src/commands.rs` | Add `update_to_stage` command |
| `launcher/src/update-logic.ts` | New `stage-update` affordance kind, `checkStageUpdate` |
| `launcher/src/update-logic.test.ts` | Tests for `checkStageUpdate` |
| `launcher/src/App.tsx` | New button branch, confirmation dialog, stage-mode polling |
| `launcher/src/commands.ts` | Add `updateToStage` binding |

## Out of scope

- **Developer mode** — stays suppressed permanently. A local build has no
  registry counterpart, and Rebuild in Settings is its update path. Unchanged
  from the suppression spec.
- **Release mode** — keeps its existing digest-based check. Unchanged.
- **Auto-update** — all updates remain explicit-click-only. No silent pulls.
- **Health-gated wait after update** — `update_stack` already does not wait
  for health; `update_to_stage` follows the same pattern for consistency. If
  the stack doesn't come up, the error surfaces and the user can investigate.
