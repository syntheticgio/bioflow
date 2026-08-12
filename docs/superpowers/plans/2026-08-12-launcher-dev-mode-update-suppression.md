# Launcher Dev-Mode Update Suppression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the launcher offering a meaningless "Update available" button when it is running locally-built (Developer) or version-pinned (Alpha/Beta) images, and explain why the button is inert instead of hiding it.

**Architecture:** One rule — *Release tracks a moving `latest` and gets an update check; every other mode is pinned or local and does not.* The mode is derived from the two `.env` lines that already determine it (`BIOFLOW_TAG`, `BIOFLOW_DEVELOPER_REPO`), never stored as a new flag. The rule is implemented twice as a pure function — once in Rust (`checkable_tag`, authoritative, short-circuits the network call) and once in TypeScript (`updateAffordance`, decides what the button renders and whether to poll at all).

**Tech Stack:** Rust (Tauri 2 backend, `cargo test`), TypeScript + React 18 (Vite, `vitest`).

**Spec:** [2026-08-12-launcher-dev-mode-update-suppression-design.md](../specs/2026-08-12-launcher-dev-mode-update-suppression-design.md)

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `launcher/src-tauri/src/update_check.rs` | Registry/digest logic + the new mode rule | Add `checkable_tag` and its tests |
| `launcher/src-tauri/src/commands.rs` | Tauri IPC adapters | `check_for_update` reads `.env`, short-circuits on `None` |
| `launcher/src/update-logic.ts` | **New.** Pure affordance rule for the button | Create |
| `launcher/src/update-logic.test.ts` | **New.** Its unit tests | Create |
| `launcher/src/App.tsx` | Launcher shell UI | Three-state button, guarded polling |
| `launcher/src/launcher.css` | The launcher's single stylesheet | One rule stacking the disabled button and its hint |

`update-logic.ts` is its own module because `wizard-logic.ts`, `settings-logic.ts`, and `migration-logic.ts` already establish that convention. It matters here: this repo has **no jsdom or testing-library setup and zero `.test.tsx` files**, so a pure module is the only unit-testable seam for UI logic.

**Note on `commands.ts`:** the spec listed it as possibly touched. It is not. `checkForUpdate()` is a no-argument `invoke("check_for_update")` and stays exactly that — the Rust side gains a `State` parameter, which Tauri injects rather than passing over IPC. No TypeScript signature changes.

---

### Task 1: The Rust mode rule (`checkable_tag`)

**Files:**
- Modify: `launcher/src-tauri/src/update_check.rs` (add function after `update_available`, ~line 60; add tests in the existing `mod tests`)

- [ ] **Step 1: Write the failing tests**

Add to the bottom of the existing `#[cfg(test)] mod tests` block in `update_check.rs` (it already has `use super::*;` at its top, so no new imports are needed):

```rust
    #[test]
    fn release_is_the_only_mode_that_checks() {
        assert_eq!(checkable_tag("latest", None), Some("latest".to_string()));
    }

    #[test]
    fn developer_mode_never_checks() {
        // A local :local build has no registry counterpart to compare against.
        assert_eq!(checkable_tag("latest", Some("/home/me/bioflow")), None);
    }

    #[test]
    fn a_pinned_alpha_never_checks() {
        // Stage tags are immutable, so a digest check against the pinned tag
        // would be near-permanently silent even after 0.4.0-alpha publishes.
        assert_eq!(checkable_tag("0.3.0-alpha", None), None);
    }

    #[test]
    fn a_pinned_beta_never_checks() {
        assert_eq!(checkable_tag("0.4.0-beta", None), None);
    }

    #[test]
    fn developer_mode_wins_when_env_somehow_carries_both() {
        // .env is hand-editable (see parse_bioflow_tag's docstring), so both
        // lines can coexist. current_settings already resolves the pair with
        // developer taking precedence; match it rather than inventing a
        // second answer.
        assert_eq!(checkable_tag("0.3.0-alpha", Some("/home/me/bioflow")), None);
    }
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd launcher/src-tauri && cargo test checkable_tag
```

Expected: FAIL — `cannot find function 'checkable_tag' in this scope`.

- [ ] **Step 3: Write the implementation**

Insert into `update_check.rs` immediately after the `update_available` function (after line 59, before the `VersionOptions` doc comment):

```rust
/// Which tag, if any, an update check should compare against -- the single
/// rule behind "Release tracks a moving `latest` and gets an update check;
/// every other mode is pinned or local and does not."
///
/// `None` means no check is meaningful for this mode, and the caller must
/// skip the registry call entirely rather than falling back to `"latest"`:
///
/// - **Developer** (`developer_repo` set): the stack runs locally-built
///   `:local` images. There is no registry counterpart, so any comparison is
///   against an unrelated image. Checked first, so a hand-edited `.env`
///   carrying both lines resolves the same way `current_settings` does.
/// - **Alpha/Beta** (a pinned stage tag): stage tags are immutable --
///   `classify_version_options` picks the highest `X.Y.Z-alpha` published --
///   so a digest check against the user's own pinned tag can only fire if a
///   tag were re-published under the same name, which the release process
///   does not do. Noticing that a *newer* stage tag exists is a different
///   mechanism (tag-list comparison) and a separate feature; see #324.
/// - **Release** (`latest`): the only moving target, and the only mode where
///   a digest check answers a real question.
pub fn checkable_tag(bioflow_tag: &str, developer_repo: Option<&str>) -> Option<String> {
    if developer_repo.is_some() {
        return None;
    }
    if bioflow_tag == "latest" {
        return Some("latest".to_string());
    }
    None
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd launcher/src-tauri && cargo test checkable_tag
```

Expected: PASS — 5 passed.

- [ ] **Step 5: Commit**

```bash
git add launcher/src-tauri/src/update_check.rs
git commit -m "feat(launcher): add the rule for which modes have a checkable tag

Release tracks a moving latest and is the only mode where a digest
check answers a real question. Developer builds :local images with no
registry counterpart, and alpha/beta pin an immutable stage tag a
digest check can never see move.

Pure and side-effect-free so the rule is testable without touching
GHCR, matching how classify_version_options is already split out.

Refs #241"
```

---

### Task 2: Wire the rule into `check_for_update`

`check_for_update` currently takes no arguments and hard-codes `"latest"`. It gains `State<'_, LauncherApp>` to locate the install dir, reads `.env` the same way `current_settings` does, and short-circuits before any network call.

**Files:**
- Modify: `launcher/src-tauri/src/commands.rs:516-527` (the `check_for_update` command)

There is no unit test here — this is a Tauri command that reads the filesystem and talks to GHCR, and the repo has no fixture harness for `State<LauncherApp>`. The logic under test lives in `checkable_tag` (Task 1); this task is the adapter, verified by compilation and by the manual check in Task 5.

- [ ] **Step 1: Replace the command body**

Replace lines 506-527 of `commands.rs` (the doc comment and function) with:

```rust
/// Whether the Update button should appear -- a cheap registry manifest
/// check, never a pull. `async` and run on Tauri's blocking-task pool via
/// `spawn_blocking` so a slow or hung registry (bounded by `GhcrClient`'s own
/// timeout, but a real network call all the same) cannot stall the IPC
/// thread or delay anything else the UI is doing. Failing silently is the
/// point: this returns `false` for "no update to offer" whether that's
/// because the machine is offline or because there is genuinely nothing
/// newer -- per the spec, the UI is not supposed to be able to tell those
/// apart.
///
/// Outside Release mode there is nothing to check: `checkable_tag` returns
/// `None` for a developer build (locally-built `:local` images with no
/// registry counterpart) and for a pinned alpha/beta stage tag, and this
/// returns `false` without making any network call at all. The frontend
/// stops polling in those modes too (see `update-logic.ts`), so this is the
/// backstop rather than the only guard.
#[tauri::command]
pub async fn check_for_update(app: State<'_, LauncherApp>) -> Result<bool, ()> {
    // .env is the source of truth for which mode the stack runs in, exactly
    // as it is for current_settings -- no separate stored flag that could
    // disagree with what the stack is actually running.
    let Some(install_dir) = install_dir_str(&app) else {
        return Ok(false);
    };
    let env_contents =
        std::fs::read_to_string(Path::new(&install_dir).join(".env")).unwrap_or_default();
    let bioflow_tag =
        parse_bioflow_tag(&env_contents).unwrap_or_else(|| DEFAULT_BIOFLOW_TAG.to_string());
    let developer_repo = parse_developer_repo(&env_contents);

    let Some(tag) = update_check::checkable_tag(&bioflow_tag, developer_repo.as_deref()) else {
        return Ok(false);
    };

    let available = tauri::async_runtime::spawn_blocking(move || {
        let registry = GhcrClient::default();
        let local = DockerImageInspector;
        CHECKABLE_IMAGES.iter().any(|image| {
            update_check::update_available(&registry, &local, image, &tag) == Some(true)
        })
    })
    .await
    .unwrap_or(false);

    Ok(available)
}
```

The return type changes from `bool` to `Result<bool, ()>`: a Tauri command taking `State<'_, ...>` must return `Result` so the borrow's lifetime is tied to the future. `current_settings` in this same file already has that exact shape (`Result<CurrentSettingsDto, ()>`), so the frontend pattern for it is established.

- [ ] **Step 2: Verify it compiles**

```bash
cd launcher/src-tauri && cargo check
```

Expected: success, no errors. `Path`, `parse_bioflow_tag`, `parse_developer_repo`, `DEFAULT_BIOFLOW_TAG`, `install_dir_str`, and `update_check` are all already in scope in `commands.rs` — no new `use` lines.

- [ ] **Step 3: Run the full Rust suite for regressions**

```bash
cd launcher/src-tauri && cargo test
```

Expected: PASS. Note the count; nothing should newly fail.

- [ ] **Step 4: Commit**

```bash
git add launcher/src-tauri/src/commands.rs
git commit -m "fix(launcher): stop checking for updates against a tag the stack is not on

check_for_update read no settings and hard-coded latest, so developer
mode compared GHCR's published latest against locally built :local
images and pinned alpha/beta stages were compared against a tag the
user deliberately is not on. Both surfaced a permanent, meaningless
Update available button.

Reads the mode off .env the way current_settings already does and
returns false without any network call when the mode has no checkable
tag.

Refs #241"
```

---

### Task 3: The frontend affordance rule

**Files:**
- Create: `launcher/src/update-logic.ts`
- Create: `launcher/src/update-logic.test.ts`

- [ ] **Step 1: Write the failing tests**

Create `launcher/src/update-logic.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { updateAffordance } from "./update-logic";

describe("updateAffordance", () => {
  it("hides the button in release mode when nothing is newer", () => {
    expect(
      updateAffordance({ bioflowTag: "latest", developerRepo: null, updateAvailable: false }),
    ).toEqual({ kind: "hidden" });
  });

  it("offers the update in release mode when something is newer", () => {
    expect(
      updateAffordance({ bioflowTag: "latest", developerRepo: null, updateAvailable: true }),
    ).toEqual({ kind: "available" });
  });

  it("suppresses in developer mode and points at Rebuild", () => {
    expect(
      updateAffordance({
        bioflowTag: "latest",
        developerRepo: "/home/me/bioflow",
        updateAvailable: false,
      }),
    ).toEqual({
      kind: "suppressed",
      reason: "Developer mode — use Rebuild in Settings.",
    });
  });

  it("suppresses in developer mode even if a check somehow reported true", () => {
    // The backend already returns false here, but the button must not depend
    // on that: a stale poll result from before a mode switch must not flash
    // an Update button at a local build.
    expect(
      updateAffordance({
        bioflowTag: "latest",
        developerRepo: "/home/me/bioflow",
        updateAvailable: true,
      }).kind,
    ).toBe("suppressed");
  });

  it("suppresses on a pinned alpha and names the tag", () => {
    expect(
      updateAffordance({ bioflowTag: "0.3.0-alpha", developerRepo: null, updateAvailable: false }),
    ).toEqual({
      kind: "suppressed",
      reason: "Pinned to 0.3.0-alpha — change version in Settings.",
    });
  });

  it("suppresses on a pinned beta and names the tag", () => {
    expect(
      updateAffordance({ bioflowTag: "0.4.0-beta", developerRepo: null, updateAvailable: false }),
    ).toEqual({
      kind: "suppressed",
      reason: "Pinned to 0.4.0-beta — change version in Settings.",
    });
  });

  it("developer mode wins over a pinned tag", () => {
    expect(
      updateAffordance({
        bioflowTag: "0.3.0-alpha",
        developerRepo: "/home/me/bioflow",
        updateAvailable: false,
      }).kind,
    ).toBe("suppressed");
    expect(
      updateAffordance({
        bioflowTag: "0.3.0-alpha",
        developerRepo: "/home/me/bioflow",
        updateAvailable: false,
      }),
    ).toEqual({
      kind: "suppressed",
      reason: "Developer mode — use Rebuild in Settings.",
    });
  });
});

describe("shouldPollForUpdates", () => {
  it("polls only in release mode", async () => {
    const { shouldPollForUpdates } = await import("./update-logic");
    expect(shouldPollForUpdates("latest", null)).toBe(true);
    expect(shouldPollForUpdates("latest", "/home/me/bioflow")).toBe(false);
    expect(shouldPollForUpdates("0.3.0-alpha", null)).toBe(false);
    expect(shouldPollForUpdates("0.4.0-beta", null)).toBe(false);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd launcher && npm test
```

Expected: FAIL — `Failed to resolve import "./update-logic"`.

- [ ] **Step 3: Write the implementation**

Create `launcher/src/update-logic.ts`:

```ts
// Pure logic for the Update button on App.tsx -- split out the same way
// wizard-logic.ts and settings-logic.ts separate their components' pure
// logic, so it can be unit tested without rendering anything. That matters
// more here than convention: this repo has no jsdom or testing-library
// setup and no .test.tsx files, so a pure module is the only testable seam.

/**
 * Mirrors `update_check::checkable_tag` in Rust. The backend is
 * authoritative -- it makes no network call in a suppressed mode -- but the
 * button must not depend on a poll result that could be stale across a mode
 * switch, and skipping the poll entirely avoids a pointless IPC round-trip
 * every five minutes.
 */
export type UpdateAffordance =
  /** Release mode, nothing newer published. No button. */
  | { kind: "hidden" }
  /** Release mode, a newer image exists. The clickable btn-warn. */
  | { kind: "available" }
  /** Developer or a pinned stage: visible, disabled, and self-explaining. */
  | { kind: "suppressed"; reason: string };

export interface UpdateInputs {
  /** Mirrors BIOFLOW_TAG in .env. */
  bioflowTag: string;
  /** Mirrors BIOFLOW_DEVELOPER_REPO in .env; null outside developer mode. */
  developerRepo: string | null;
  /** The latest result of the backend's check_for_update poll. */
  updateAvailable: boolean;
}

/**
 * Whether an update check means anything in this mode. Developer is checked
 * first so a hand-edited .env carrying both lines resolves the way
 * current_settings resolves it.
 */
export function shouldPollForUpdates(
  bioflowTag: string,
  developerRepo: string | null,
): boolean {
  if (developerRepo != null) return false;
  return bioflowTag === "latest";
}

export function updateAffordance({
  bioflowTag,
  developerRepo,
  updateAvailable,
}: UpdateInputs): UpdateAffordance {
  // Named before the pinned case: developer mode takes precedence, and the
  // hint names Rebuild because that genuinely is the update path for a
  // local build.
  if (developerRepo != null) {
    return { kind: "suppressed", reason: "Developer mode — use Rebuild in Settings." };
  }
  // Naming the tag keeps the reason concrete -- "pinned" alone does not tell
  // the user what they are pinned to.
  if (bioflowTag !== "latest") {
    return {
      kind: "suppressed",
      reason: `Pinned to ${bioflowTag} — change version in Settings.`,
    };
  }
  return updateAvailable ? { kind: "available" } : { kind: "hidden" };
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd launcher && npm test
```

Expected: PASS — all `update-logic` tests green, existing `settings-logic` / `wizard-logic` / `migration-logic` tests still green.

- [ ] **Step 5: Commit**

```bash
git add launcher/src/update-logic.ts launcher/src/update-logic.test.ts
git commit -m "feat(launcher): add the Update button's affordance rule

Three states rather than a boolean: hidden, available, and suppressed
with a reason. Suppressed carries its own explanation so the button can
say why it is inert instead of vanishing.

Refs #241"
```

---

### Task 4: Render the three states and guard the poll

**Files:**
- Modify: `launcher/src/App.tsx:89-104` (the update-check effect)
- Modify: `launcher/src/App.tsx:327-331` (the button block)

- [ ] **Step 1: Import the rule**

In `App.tsx`, add after the existing imports (the `./commands` import is on line 3):

```ts
import { shouldPollForUpdates, updateAffordance } from "./update-logic";
```

- [ ] **Step 2: Guard the polling effect**

Replace the update-check `useEffect` (lines 89-104) with:

```tsx
  // Only Release mode has a moving target to poll for. Developer builds and
  // pinned alpha/beta stages skip the interval entirely -- the backend
  // already returns false for them without a network call, so this is about
  // not making a pointless IPC round-trip every five minutes.
  useEffect(() => {
    if (state.kind !== "Running" || !shouldPollForUpdates(settings.bioflowTag, settings.developerRepo)) {
      setUpdateAvailable(false);
      return;
    }
    let cancelled = false;
    async function poll() {
      const available = await checkForUpdate();
      if (!cancelled) setUpdateAvailable(available);
    }
    poll();
    const id = setInterval(poll, UPDATE_CHECK_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [state.kind, settings.bioflowTag, settings.developerRepo]);
```

The dependency array gains both settings fields so a mode switch in Settings starts or stops the polling immediately rather than at the next remount.

- [ ] **Step 3: Render the three states**

Replace the button block (lines 327-331) with:

```tsx
                {(() => {
                  const affordance = updateAffordance({
                    bioflowTag: settings.bioflowTag,
                    developerRepo: settings.developerRepo,
                    updateAvailable,
                  });
                  if (affordance.kind === "hidden") return null;
                  if (affordance.kind === "available") {
                    return (
                      <button className="btn btn-warn" onClick={handleUpdate} disabled={busy}>
                        {busy ? "Updating…" : "Update available"}
                      </button>
                    );
                  }
                  // Suppressed: disabled rather than absent, following the
                  // same "explain why this control is inert" treatment
                  // Settings.tsx gives storage location and port while
                  // running. A control that vanishes silently leaves a user
                  // who forgot their mode with no explanation.
                  return (
                    <span className="update-suppressed">
                      <button className="btn btn-secondary" disabled>
                        Update
                      </button>
                      <span className="field-hint" role="note">
                        {affordance.reason}
                      </span>
                    </span>
                  );
                })()}
```

- [ ] **Step 4: Style the suppressed pairing**

The disabled button and its hint need to stack rather than sit inline in the `state-actions` row, which is `display: flex` (`launcher.css:135`). Append to `launcher/src/launcher.css` — that is the launcher's single stylesheet, imported by `main.tsx:4`; there is no `App.css`:

```css
/* The disabled Update button and its "why" note, stacked so the hint sits
   under the button rather than stretching the .state-actions flex row. */
.update-suppressed {
  display: inline-flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}
```

`.field-hint` is already defined at `launcher.css:200` and needs no new rule — reusing it is what makes the suppressed hint read identically to Settings' own "Stop the stack to change this" notes.

- [ ] **Step 5: Typecheck and test**

```bash
cd launcher && npm run lint && npm test
```

Expected: both PASS. `lint` is `tsc --noEmit`, which will catch a mismatch between the `UpdateInputs` shape and what `App.tsx` passes.

- [ ] **Step 6: Commit**

```bash
git add launcher/src/App.tsx launcher/src/launcher.css
git commit -m "fix(launcher): explain the inert Update button instead of nagging in dev mode

Developer and pinned alpha/beta modes now render Update disabled with
the reason underneath -- 'use Rebuild in Settings' for a local build,
the pinned tag for a stage -- rather than showing a live Update button
for a comparison that does not apply. Release is unchanged.

Polling stops in those modes too, so a suppressed launcher makes no
five-minute IPC round-trip.

Refs #241"
```

---

### Task 5: Manual verification

Button rendering has no headless test path in this repo (no jsdom, no `.test.tsx`), so the three states are verified by hand. This is the step that would catch a wrong `.env` parse or a hint that never renders.

**Files:** none modified.

- [ ] **Step 1: Build and run the launcher**

```bash
cd launcher && npm run tauri dev
```

- [ ] **Step 2: Verify Release mode is unchanged**

With Version set to **Release** and the stack Running: the Update button behaves exactly as before — absent when up to date, `btn-warn` "Update available" when GHCR has a newer `latest`. Confirm `.env` carries `BIOFLOW_TAG=latest` and no `BIOFLOW_DEVELOPER_REPO` line:

```bash
grep -E "BIOFLOW_TAG|BIOFLOW_DEVELOPER_REPO" ~/.bioflow/.env
```

- [ ] **Step 3: Verify Developer mode suppresses**

Settings → Version → **Developer (local build)**, set the repo path to this checkout, Apply. Then confirm:

- The Update button is visible but **disabled**
- The hint reads *Developer mode — use Rebuild in Settings.*
- `~/.bioflow/.env` has `BIOFLOW_DEVELOPER_REPO=<path>` and **no** `BIOFLOW_TAG` line
- No update-check network traffic: leave it running past five minutes, or confirm by reading the code path — `shouldPollForUpdates` returns false, so no `checkForUpdate` IPC fires

- [ ] **Step 4: Verify a pinned stage suppresses**

Settings → Version → **Alpha** (if a published alpha tag exists; the dropdown disables the row when none does), Apply. Confirm:

- The Update button is visible but **disabled**
- The hint names the actual tag, e.g. *Pinned to 0.3.0-alpha — change version in Settings.*

If no alpha tag is published, simulate it by stopping the stack and hand-editing `~/.bioflow/.env` to `BIOFLOW_TAG=0.3.0-alpha`, then relaunching the launcher. The `.env` parsers explicitly tolerate hand-editing, and `App.tsx` reads the tag back on mount.

- [ ] **Step 5: Verify switching back restores Release**

Settings → Version → **Release**, Apply. The Update button returns to its normal behavior and polling resumes without a launcher restart (the effect's dependency array covers this).

- [ ] **Step 6: Bring down anything you started**

Per CLAUDE.md, a stack brought up for testing is yours to bring back down. If a worktree stack was started for this, `./ops/worktree-up.sh --down`. Leave the shared main stack on 5173 running.

---

### Task 6: Open the PR

- [ ] **Step 1: Confirm the full suite is green**

```bash
cd launcher/src-tauri && cargo test
```

```bash
cd launcher && npm run lint && npm test
```

Read the counts, not just the exit codes. Both must be green before pushing.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

The PR title lands in the release notes verbatim. If `--fill` picks a weak subject from the multi-commit branch, set it explicitly:

```bash
gh pr edit --title "fix(launcher): stop offering updates the running mode cannot use"
```

- [ ] **Step 3: Label the PR**

`.github/release.yml` categorizes by label, not by the title's prefix — an unlabelled PR lands under "Other changes".

```bash
gh pr edit --add-label "type:bug,area:infrastructure,area:frontend"
```

- [ ] **Step 4: Add `Closes #241` to the description**

The spec commit deliberately did not close the issue; the implementation PR does. Confirm the body carries `Closes #241`, and that it mentions #324 as the deferred follow-up so a reader does not think alpha/beta tag-advance was forgotten.

- [ ] **Step 5: Watch CI**

`gh pr create` returns before any check runs. Poll until every check reports pass or fail:

```bash
gh pr checks <N>
```

```bash
gh pr view <N> --json mergeable,mergeStateStatus
```

A "pending" read seconds after creation is the run not having started, not a signal to stop watching. If a check fails, read the log, apply the minimal fix, push, and re-poll. If `mergeStateStatus` reports a real conflict (not `UNSTABLE`), rebase on `origin/main` and push again. Only once checks are green and `mergeable` is clean, report the PR URL and stop.

**Do not merge.** The user reviews and merges.

---

## Self-review notes

**Spec coverage.** Every section maps to a task: the Rust rule → Task 1; `check_for_update` wiring → Task 2; the frontend pure module → Task 3; the three UI states and guarded polling → Task 4; the manual verification the spec calls for → Task 5. The spec's "Files touched" table listed `launcher/src/commands.ts`; verified unnecessary and explained under File Structure above — `checkForUpdate()` stays a no-argument invoke because Tauri injects `State` rather than passing it over IPC.

**Deferred by design.** Alpha/beta noticing a *newer* stage tag is [#324](https://github.com/syntheticgio/bioflow/issues/324), not this plan.

**Type consistency.** `checkable_tag(&str, Option<&str>) -> Option<String>` is used identically in Tasks 1 and 2. `updateAffordance(UpdateInputs) -> UpdateAffordance` and `shouldPollForUpdates(string, string | null) -> boolean` are used identically in Tasks 3 and 4. The three `kind` values (`hidden` / `available` / `suppressed`) are spelled the same in the type, the tests, and the JSX.

**One risk worth naming.** Task 2 changes `check_for_update`'s return type from `bool` to `Result<bool, ()>`. `@tauri-apps/api`'s `invoke` unwraps `Ok` transparently, so `checkForUpdate(): Promise<boolean>` in `commands.ts` continues to work unchanged — but if Task 4's typecheck reports a mismatch there, that is the cause, and the fix is in `commands.ts`, not in `App.tsx`.
