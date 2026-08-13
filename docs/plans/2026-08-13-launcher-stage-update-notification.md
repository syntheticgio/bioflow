# Launcher Stage Update Notification — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Notify alpha/beta users when a newer stage tag publishes, with a one-click update path that rewrites `.env`, pulls images, and recreates the stack.

**Architecture:** Two new Rust seams (`check_stage_update` pure function + `update_to_stage` action) and a frontend `stage-update` affordance kind, all following the existing modularity (pure logic in `update_check.rs`/`update-logic.ts`, thin commands in `commands.rs`, action in `actions.rs`).

**Tech Stack:** Rust (Tauri backend), TypeScript (React frontend), Docker Compose

---

## Task 1: Add `check_stage_update` and `set_bioflow_tag` to `update_check.rs`

**Objective:** Pure function that compares the user's current tag against available version options and returns the best forward-compatible tag. Plus a helper to rewrite `BIOFLOW_TAG` in `.env` content.

**Files:**
- Modify: `launcher/src-tauri/src/update_check.rs` (append after `checkable_tag`)

**Step 1: Add `set_bioflow_tag` helper**

```rust
/// Find the BIOFLOW_TAG line in .env content and replace it with a new value.
/// Preserves all other lines and their ordering. Appends the line if not found
/// (safety net, not expected in practice).
fn set_bioflow_tag(contents: &str, new_tag: &str) -> String {
    let mut found = false;
    let result: Vec<String> = contents
        .lines()
        .map(|line| {
            if line.starts_with("BIOFLOW_TAG=") {
                found = true;
                format!("BIOFLOW_TAG={}", new_tag)
            } else {
                line.to_string()
            }
        })
        .collect();

    if !found {
        let mut result = result;
        result.push(format!("BIOFLOW_TAG={}", new_tag));
        result.join("\n")
    } else {
        result.join("\n")
    }
}
```

**Step 2: Add `check_stage_update` function**

```rust
/// Given the user's current pinned tag and the available version options from
/// the registry, returns the best forward-compatible stage tag (if any).
///
/// "Forward" means a strictly greater (major, minor, patch, stage_rank) tuple,
/// where stage rank is alpha=0, beta=1. Release mode (`"latest"`) is excluded
/// — it has its own digest-based update path.
///
/// Returns `None` when no forward-compatible tag exists or the current tag
/// cannot be parsed as a stage tag.
pub fn check_stage_update(current_tag: &str, options: &VersionOptions) -> Option<String> {
    if current_tag == "latest" {
        return None;
    }

    let current_ver = version_tuple(current_tag, "alpha")
        .map(|v| (v, 0u8))
        .or_else(|| version_tuple(current_tag, "beta").map(|v| (v, 1u8)))?;

    let candidates = [&options.alpha, &options.beta];

    candidates
        .iter()
        .flatten()
        .filter_map(|candidate| {
            let cv = version_tuple(candidate, "alpha")
                .map(|v| (v, 0u8))
                .or_else(|| version_tuple(candidate, "beta").map(|v| (v, 1u8)))?;
            if (cv.0, cv.1) > current_ver {
                Some((candidate.clone(), cv))
            } else {
                None
            }
        })
        .max_by_key(|(_, (v, rank))| (*v, *rank))
        .map(|(tag, _)| tag)
}
```

**Step 3: Verify compilation**

Run: `cd launcher && cargo build 2>&1 | tail -20`
Expected: Build succeeds with no errors.

**Step 4: Commit**

```bash
git add launcher/src-tauri/src/update_check.rs
git commit -m "feat(launcher): add check_stage_update and set_bioflow_tag helpers"
```

---

## Task 2: Add tests for `check_stage_update` and `set_bioflow_tag`

**Objective:** Cover all comparison cases from the design spec.

**Files:**
- Modify: `launcher/src-tauri/src/update_check.rs` (append to `mod tests`)

**Step 1: Add `set_bioflow_tag` tests**

```rust
#[test]
fn set_bioflow_tag_replaces_existing_tag() {
    let env = "BIOINFO_HOME=/data\nWEB_PORT=5173\nBIND_ADDRESS=127.0.0.1\nBIOFLOW_TAG=0.3.0-alpha\n";
    let result = set_bioflow_tag(env, "0.4.0-alpha");
    assert!(result.contains("BIOFLOW_TAG=0.4.0-alpha"));
    assert!(!result.contains("BIOFLOW_TAG=0.3.0-alpha"));
    assert!(result.contains("BIOINFO_HOME=/data"));
    assert!(result.contains("WEB_PORT=5173"));
}

#[test]
fn set_bioflow_tag_appends_when_missing() {
    let env = "BIOINFO_HOME=/data\nWEB_PORT=5173\n";
    let result = set_bioflow_tag(env, "0.4.0-alpha");
    assert!(result.contains("BIOFLOW_TAG=0.4.0-alpha"));
}

#[test]
fn set_bioflow_tag_preserves_trailing_newline_style() {
    let env = "BIOINFO_HOME=/data\nWEB_PORT=5173\nBIOFLOW_TAG=0.3.0-alpha\n";
    let result = set_bioflow_tag(env, "0.4.0-beta");
    assert_eq!(result, "BIOINFO_HOME=/data\nWEB_PORT=5173\nBIOFLOW_TAG=0.4.0-beta\n");
}
```

**Step 2: Add `check_stage_update` tests**

```rust
#[test]
fn stage_update_higher_version_wins_over_lower_version() {
    let opts = VersionOptions {
        release: "latest".to_string(),
        alpha: Some("0.4.0-alpha".to_string()),
        beta: Some("0.3.0-beta".to_string()),
    };
    assert_eq!(
        check_stage_update("0.3.0-alpha", &opts),
        Some("0.4.0-alpha".to_string())
    );
}

#[test]
fn stage_update_same_version_later_stage_wins() {
    let opts = VersionOptions {
        release: "latest".to_string(),
        alpha: Some("0.3.0-alpha".to_string()),
        beta: Some("0.3.0-beta".to_string()),
    };
    assert_eq!(
        check_stage_update("0.3.0-alpha", &opts),
        Some("0.3.0-beta".to_string())
    );
}

#[test]
fn stage_update_earlier_stage_is_not_forward() {
    let opts = VersionOptions {
        release: "latest".to_string(),
        alpha: Some("0.4.0-alpha".to_string()),
        beta: Some("0.4.0-beta".to_string()),
    };
    // 0.4.0-alpha is earlier stage than 0.4.0-beta at same version
    assert_eq!(
        check_stage_update("0.4.0-beta", &opts),
        None
    );
}

#[test]
fn stage_update_nothing_available_returns_none() {
    let opts = VersionOptions {
        release: "latest".to_string(),
        alpha: None,
        beta: None,
    };
    assert_eq!(check_stage_update("0.3.0-alpha", &opts), None);
}

#[test]
fn stage_update_lower_version_is_not_forward() {
    let opts = VersionOptions {
        release: "latest".to_string(),
        alpha: Some("0.2.0-alpha".to_string()),
        beta: None,
    };
    assert_eq!(check_stage_update("0.3.0-alpha", &opts), None);
}

#[test]
fn stage_update_release_mode_returns_none() {
    let opts = VersionOptions {
        release: "latest".to_string(),
        alpha: Some("0.4.0-alpha".to_string()),
        beta: None,
    };
    assert_eq!(check_stage_update("latest", &opts), None);
}

#[test]
fn stage_update_picks_highest_available() {
    let opts = VersionOptions {
        release: "latest".to_string(),
        alpha: Some("0.5.0-alpha".to_string()),
        beta: Some("0.4.0-beta".to_string()),
    };
    assert_eq!(
        check_stage_update("0.3.0-alpha", &opts),
        Some("0.5.0-alpha".to_string())
    );
}
```

**Step 3: Run tests**

Run: `cd launcher && cargo test -- update_check 2>&1 | tail -30`
Expected: All tests pass (including existing `checkable_tag` tests).

**Step 4: Commit**

```bash
git add launcher/src-tauri/src/update_check.rs
git commit -m "test(launcher): cover check_stage_update and set_bioflow_tag"
```

---

## Task 3: Add `UpdateToStageOutcome` and `update_to_stage` to `actions.rs`

**Objective:** New action function that rewrites `.env`, pulls images, and recreates the stack.

**Files:**
- Modify: `launcher/src-tauri/src/actions.rs` (append after `update` function and its tests)

**Step 1: Add import for Path and update_check**

Add to the top of `actions.rs`:
```rust
use std::path::Path;
use crate::update_check;
```

**Step 2: Add `UpdateToStageOutcome` enum and `update_to_stage` function**

```rust
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UpdateToStageOutcome {
    Updated,
    PullFailed { output: String },
    RecreateFailed { output: String },
}

/// Rewrites BIOFLOW_TAG in .env, pulls new images, then recreates the stack.
/// Crossing a stage boundary is an explicit user action (confirmed in the UI),
/// so this function only runs after the user clicked through a confirmation
/// dialog — it never runs automatically.
pub fn update_to_stage<D: DockerBackend>(
    docker: &D,
    install_dir: &str,
    new_tag: &str,
) -> UpdateToStageOutcome {
    // 1. Rewrite BIOFLOW_TAG in .env
    let env_path = Path::new(install_dir).join(".env");
    let contents = std::fs::read_to_string(&env_path).unwrap_or_default();
    let updated = update_check::set_bioflow_tag(&contents, new_tag);
    if std::fs::write(&env_path, updated).is_err() {
        return UpdateToStageOutcome::PullFailed {
            output: "Failed to update .env".to_string(),
        };
    }

    // 2. Pull new images
    match docker.pull(install_dir) {
        ActionResult::Ok => {}
        ActionResult::Failed { output } => return UpdateToStageOutcome::PullFailed { output },
    }

    // 3. Recreate containers
    match docker.up(install_dir) {
        ActionResult::Ok => UpdateToStageOutcome::Updated,
        ActionResult::Failed { output } => UpdateToStageOutcome::RecreateFailed { output },
    }
}
```

**Step 3: Verify compilation**

Run: `cd launcher && cargo build 2>&1 | tail -20`
Expected: Build succeeds with no errors.

**Step 4: Commit**

```bash
git add launcher/src-tauri/src/actions.rs
git commit -m "feat(launcher): add update_to_stage action"
```

---

## Task 4: Add tests for `update_to_stage`

**Objective:** Cover success, .env write failure, pull failure, and recreate failure.

**Files:**
- Modify: `launcher/src-tauri/src/actions.rs` (append to `mod tests`)

**Step 1: Add import for `FakeDocker` fields**

The `FakeDocker` struct is already imported. Add `use crate::update_check::set_bioflow_tag;` if needed, but since we're testing through `update_to_stage`, the test calls the function directly and it internally calls `update_check::set_bioflow_tag`.

**Step 2: Add test cases**

```rust
#[test]
fn update_to_stage_success() {
    let docker = FakeDocker::new();
    let dir = tempfile::TempDir::new().unwrap();
    let env_path = dir.path().join(".env");
    std::fs::write(&env_path, "BIOINFO_HOME=/data\nBIOFLOW_TAG=0.3.0-alpha\n").unwrap();
    let dir_str = dir.path().to_string_lossy().to_string();

    let outcome = update_to_stage(&docker, &dir_str, "0.4.0-alpha");
    assert_eq!(outcome, UpdateToStageOutcome::Updated);

    // Verify .env was rewritten
    let new_env = std::fs::read_to_string(&env_path).unwrap();
    assert!(new_env.contains("BIOFLOW_TAG=0.4.0-alpha"));
    assert!(!new_env.contains("BIOFLOW_TAG=0.3.0-alpha"));
}

#[test]
fn update_to_stage_reports_pull_failure() {
    let docker = FakeDocker::new();
    *docker.pull_result.borrow_mut() = ActionResult::Failed {
        output: "registry unreachable".to_string(),
    };
    let dir = tempfile::TempDir::new().unwrap();
    std::fs::write(dir.path().join(".env"), "BIOFLOW_TAG=0.3.0-alpha\n").unwrap();
    let dir_str = dir.path().to_string_lossy().to_string();

    let outcome = update_to_stage(&docker, &dir_str, "0.4.0-alpha");
    assert_eq!(
        outcome,
        UpdateToStageOutcome::PullFailed {
            output: "registry unreachable".to_string()
        }
    );
}

#[test]
fn update_to_stage_reports_recreate_failure() {
    let docker = FakeDocker::new();
    *docker.up_result.borrow_mut() = ActionResult::Failed {
        output: "disk full".to_string(),
    };
    let dir = tempfile::TempDir::new().unwrap();
    std::fs::write(dir.path().join(".env"), "BIOFLOW_TAG=0.3.0-alpha\n").unwrap();
    let dir_str = dir.path().to_string_lossy().to_string();

    let outcome = update_to_stage(&docker, &dir_str, "0.4.0-alpha");
    assert_eq!(
        outcome,
        UpdateToStageOutcome::RecreateFailed {
            output: "disk full".to_string()
        }
    );
}

#[test]
fn update_to_stage_rewrites_env_before_pull() {
    // If .env is missing, the function should still try to write it
    let docker = FakeDocker::new();
    let dir = tempfile::TempDir::new().unwrap();
    let dir_str = dir.path().to_string_lossy().to_string();

    let outcome = update_to_stage(&docker, &dir_str, "0.4.0-alpha");
    // Should succeed (creates .env from scratch via set_bioflow_tag's append path)
    assert_eq!(outcome, UpdateToStageOutcome::Updated);

    let new_env = std::fs::read_to_string(dir.path().join(".env")).unwrap();
    assert!(new_env.contains("BIOFLOW_TAG=0.4.0-alpha"));
}
```

**Step 3: Add `tempfile` to dev-dependencies if not present**

Check if `tempfile` is in `launcher/src-tauri/Cargo.toml`:
```bash
grep tempfile launcher/src-tauri/Cargo.toml
```

If not present, add to `[dev-dependencies]`:
```toml
tempfile = "3"
```

**Step 4: Run tests**

Run: `cd launcher && cargo test -- actions 2>&1 | tail -30`
Expected: All tests pass.

**Step 5: Commit**

```bash
git add launcher/src-tauri/Cargo.toml launcher/src-tauri/actions.rs
git commit -m "test(launcher): cover update_to_stage action paths"
```

---

## Task 5: Add `update_to_stage` Tauri command in `commands.rs`

**Objective:** Wire the new action as a Tauri IPC command.

**Files:**
- Modify: `launcher/src-tauri/src/commands.rs`

**Step 1: Add the new command**

Add after `update_stack` (around line 350):

```rust
#[tauri::command]
pub async fn update_to_stage(app: State<'_, LauncherApp>, tag: String) -> Result<(), String> {
    let install_dir = install_dir_str_blocking(&app).await.ok_or("not installed")?;

    let outcome = tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        actions::update_to_stage(&docker, &install_dir, &tag)
    })
    .await
    .map_err(|e| e.to_string())?;

    match outcome {
        actions::UpdateToStageOutcome::Updated => Ok(()),
        actions::UpdateToStageOutcome::PullFailed { output }
        | actions::UpdateToStageOutcome::RecreateFailed { output } => Err(output),
    }
}
```

**Step 2: Verify compilation**

Run: `cd launcher && cargo build 2>&1 | tail -20`
Expected: Build succeeds with no errors.

**Step 3: Commit**

```bash
git add launcher/src-tauri/src/commands.rs
git commit -m "feat(launcher): add update_to_stage Tauri command"
```

---

## Task 6: Add `checkStageUpdate` and new affordance kind to `update-logic.ts`

**Objective:** Pure TypeScript function matching the Rust `check_stage_update` logic, plus the new `stage-update` affordance kind.

**Files:**
- Modify: `launcher/src/update-logic.ts`

**Step 1: Add `VersionOptions` import**

Add to the top of `update-logic.ts`:
```typescript
import type { VersionOptions } from "./types";
```

**Step 2: Add `checkStageUpdate` function**

```typescript
/** Mirrors update_check::check_stage_update in Rust. Pure, side-effect-free. */
export function checkStageUpdate(
  currentTag: string,
  options: VersionOptions,
): string | null {
  if (currentTag === "latest") return null;

  // Parse current tag into (major, minor, patch, stageRank)
  const current = parseStageTag(currentTag);
  if (!current) return null;

  const candidates = [options.alpha, options.beta];
  let best: { tag: string; ver: [number, number, number]; rank: number } | null = null;

  for (const candidate of candidates) {
    if (!candidate) continue;
    const cv = parseStageTag(candidate);
    if (!cv) continue;

    const isForward =
      cv.ver[0] > current.ver[0] ||
      (cv.ver[0] === current.ver[0] && cv.ver[1] > current.ver[1]) ||
      (cv.ver[0] === current.ver[0] && cv.ver[1] === current.ver[1] && cv.ver[2] > current.ver[2]) ||
      (cv.ver[0] === current.ver[0] && cv.ver[1] === current.ver[1] && cv.ver[2] === current.ver[2] && cv.rank > current.rank);

    if (!isForward) continue;

    if (!best || cv.ver[0] > best.ver[0] ||
        (cv.ver[0] === best.ver[0] && cv.ver[1] > best.ver[1]) ||
        (cv.ver[0] === best.ver[0] && cv.ver[1] === best.ver[1] && cv.ver[2] > best.ver[2]) ||
        (cv.ver[0] === best.ver[0] && cv.ver[1] === best.ver[1] && cv.ver[2] === best.ver[2] && cv.rank > best.rank)) {
      best = { tag: candidate, ver: cv.ver, rank: cv.rank };
    }
  }

  return best?.tag ?? null;
}

function parseStageTag(tag: string): { ver: [number, number, number]; rank: number } | null {
  const alphaMatch = tag.match(/^(\d+)\.(\d+)\.(\d+)-alpha$/);
  if (alphaMatch) {
    return {
      ver: [parseInt(alphaMatch[1]), parseInt(alphaMatch[2]), parseInt(alphaMatch[3])],
      rank: 0,
    };
  }
  const betaMatch = tag.match(/^(\d+)\.(\d+)\.(\d+)-beta$/);
  if (betaMatch) {
    return {
      ver: [parseInt(betaMatch[1]), parseInt(betaMatch[2]), parseInt(betaMatch[3])],
      rank: 1,
    };
  }
  return null;
}
```

**Step 3: Extend `UpdateInputs` interface**

Add `versionOptions` to the interface:
```typescript
export interface UpdateInputs {
  bioflowTag: string;
  developerRepo: string | null;
  updateAvailable: boolean;
  /** Fetched from listVersionOptions; null while loading or on failure. */
  versionOptions: VersionOptions | null;
}
```

**Step 4: Extend `updateAffordance` function**

Add the `stage-update` branch between the suppressed check and the release check:

```typescript
export function updateAffordance({
  bioflowTag,
  developerRepo,
  updateAvailable,
  versionOptions,
}: UpdateInputs): UpdateAffordance {
  // Developer mode → suppressed (unchanged)
  if (developerRepo != null) {
    return { kind: "suppressed", reason: "Developer mode — use Rebuild in Settings." };
  }
  // Alpha/Beta mode → check for newer stage tag
  if (bioflowTag !== "latest" && versionOptions) {
    const target = checkStageUpdate(bioflowTag, versionOptions);
    if (target) {
      return { kind: "stage-update", targetTag: target };
    }
    return { kind: "suppressed", reason: `Pinned to ${bioflowTag} — change version in Settings.` };
  }
  // Release mode → existing digest-based check
  return updateAvailable ? { kind: "available" } : { kind: "hidden" };
}
```

**Step 5: Update `shouldPollForUpdates`**

```typescript
export function shouldPollForUpdates(
  bioflowTag: string,
  developerRepo: string | null,
): boolean {
  if (developerRepo != null) return false;
  // Poll for both release (digest) and stage (tag list) updates
  return true;
}
```

**Step 6: Verify**

The file should now compile without TypeScript errors. Run:
```bash
cd launcher && npx tsc --noEmit 2>&1 | head -20
```

Expected: No errors.

**Step 7: Commit**

```bash
git add launcher/src/update-logic.ts
git commit -m "feat(launcher): add checkStageUpdate and stage-update affordance"
```

---

## Task 7: Add tests for `checkStageUpdate`

**Objective:** Cover the same cases as the Rust tests.

**Files:**
- Create: `launcher/src/update-logic.test.ts`

**Step 1: Create the test file**

```typescript
import { checkStageUpdate } from "./update-logic";
import type { VersionOptions } from "./types";

describe("checkStageUpdate", () => {
  const makeOptions = (alpha: string | null, beta: string | null): VersionOptions => ({
    release: "latest",
    alpha,
    beta,
  });

  it("returns higher version when both are same stage", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions("0.4.0-alpha", null));
    expect(result).toBe("0.4.0-alpha");
  });

  it("returns later stage when version is same", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions("0.3.0-alpha", "0.3.0-beta"));
    expect(result).toBe("0.3.0-beta");
  });

  it("returns null when only earlier stage is available", () => {
    const result = checkStageUpdate("0.4.0-beta", makeOptions("0.4.0-alpha", "0.4.0-beta"));
    expect(result).toBeNull();
  });

  it("returns null when nothing is available", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions(null, null));
    expect(result).toBeNull();
  });

  it("returns null for lower version", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions("0.2.0-alpha", null));
    expect(result).toBeNull();
  });

  it("returns null for release mode", () => {
    const result = checkStageUpdate("latest", makeOptions("0.4.0-alpha", null));
    expect(result).toBeNull();
  });

  it("picks the highest version when multiple are forward", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions("0.5.0-alpha", "0.4.0-beta"));
    expect(result).toBe("0.5.0-alpha");
  });

  it("returns beta over alpha at same higher version", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions("0.4.0-alpha", "0.4.0-beta"));
    expect(result).toBe("0.4.0-beta");
  });
});
```

**Step 2: Run the tests**

Run: `cd launcher && npx vitest run src/update-logic.test.ts 2>&1`
Expected: All 8 tests pass.

**Step 3: Commit**

```bash
git add launcher/src/update-logic.test.ts
git commit -m "test(launcher): cover checkStageUpdate logic"
```

---

## Task 8: Update `App.tsx` — button branch, confirmation dialog, stage-mode polling

**Objective:** Wire the new affordance into the UI with a confirmation dialog and parallel polling for alpha/beta modes.

**Files:**
- Modify: `launcher/src/App.tsx`

**Step 1: Add `versionOptions` state and `updateToStage` import**

Add `versionOptions` to the state and import the new command:

```typescript
import { checkForUpdate, currentSettings, openBioFlow, otherStacks, runStack, status, stopStack, updateStack, listVersionOptions, updateToStage } from "./commands";
import type { LauncherState, OtherStack, Settings as SettingsValues, VersionOptions } from "./types";
```

Add state variable alongside `updateAvailable`:
```typescript
const [versionOptions, setVersionOptions] = useState<VersionOptions | null>(null);
```

**Step 2: Add `deriveVersionMode` helper**

```typescript
function deriveVersionMode(bioflowTag: string, developerRepo: string | null): "release" | "alpha" | "beta" | "developer" {
  if (developerRepo != null) return "developer";
  if (bioflowTag === "latest") return "release";
  if (bioflowTag.endsWith("-alpha")) return "alpha";
  if (bioflowTag.endsWith("-beta")) return "beta";
  return "release";
}
```

**Step 3: Add stage-mode polling effect**

Replace the existing update-polling `useEffect` (lines 161-177) with this:

```typescript
useEffect(() => {
  if (state.kind !== "Running") {
    setUpdateAvailable(false);
    setVersionOptions(null);
    return;
  }

  const mode = deriveVersionMode(settings.bioflowTag, settings.developerRepo);
  let cancelled = false;

  if (mode === "release") {
    // Digest-based poll for Release mode (unchanged)
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
  } else if (mode === "alpha" || mode === "beta") {
    // Tag-list poll for Alpha/Beta mode
    async function poll() {
      const opts = await listVersionOptions();
      if (!cancelled) setVersionOptions(opts);
    }
    poll();
    const id = setInterval(poll, UPDATE_CHECK_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }
  // Developer mode: no polling
  setUpdateAvailable(false);
  setVersionOptions(null);
}, [state.kind, settings.bioflowTag, settings.developerRepo]);
```

**Step 4: Add stage update handler**

```typescript
async function handleStageUpdate(targetTag: string) {
  const ok = window.confirm(
    `This will update BioFlow from ${settings.bioflowTag} to ${targetTag} and restart the stack. Continue?`,
  );
  if (!ok) return;
  setBusy(true);
  setError(null);
  try {
    await updateToStage(targetTag);
  } catch (e) {
    setError(String(e));
  } finally {
    setBusy(false);
  }
}
```

**Step 5: Add `stage-update` branch to `UpdateButton`**

Replace the `UpdateButton` component to handle the new affordance:

```typescript
function UpdateButton({ bioflowTag, developerRepo, updateAvailable, versionOptions, busy, onUpdate }: UpdateButtonProps) {
  const affordance = updateAffordance({ bioflowTag, developerRepo, updateAvailable, versionOptions });

  if (affordance.kind === "hidden") return null;

  if (affordance.kind === "available") {
    return (
      <button className="btn btn-warn" onClick={onUpdate} disabled={busy}>
        {busy ? "Updating…" : "Update available"}
      </button>
    );
  }

  if (affordance.kind === "stage-update") {
    return (
      <button
        className="btn btn-warn"
        onClick={() => handleStageUpdate(affordance.targetTag)}
        disabled={busy}
      >
        {busy ? "Updating…" : `Update to ${affordance.targetTag}`}
      </button>
    );
  }

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
}
```

**Step 6: Update `UpdateButtonProps` interface**

```typescript
interface UpdateButtonProps extends UpdateInputs {
  busy: boolean;
  onUpdate: () => void;
}
```

**Step 7: Pass `versionOptions` to `UpdateButton`**

In the JSX where `UpdateButton` is rendered (around line 399-405), add the new prop:

```tsx
<UpdateButton
  bioflowTag={settings.bioflowTag}
  developerRepo={settings.developerRepo}
  updateAvailable={updateAvailable}
  versionOptions={versionOptions}
  busy={busy}
  onUpdate={handleUpdate}
/>
```

**Step 8: Verify compilation**

Run: `cd launcher && npx tsc --noEmit 2>&1 | head -20`
Expected: No errors.

**Step 9: Commit**

```bash
git add launcher/src/App.tsx
git commit -m "feat(launcher): wire stage-update button, confirmation, and polling"
```

---

## Task 9: Add `updateToStage` binding to `commands.ts`

**Objective:** Add the TypeScript-side IPC binding for the new command.

**Files:**
- Modify: `launcher/src/commands.ts`

**Step 1: Add the function**

Add after `updateStack`:

```typescript
export function updateToStage(tag: string): Promise<void> {
  return invoke("update_to_stage", { tag });
}
```

**Step 2: Commit**

```bash
git add launcher/src/commands.ts
git commit -m "feat(launcher): add updateToStage IPC binding"
```

---

## Task 10: Final verification

**Objective:** Run full test suite and verify the launcher builds.

**Step 1: Run all Rust tests**

```bash
cd launcher && cargo test 2>&1 | tail -20
```

Expected: All tests pass.

**Step 2: Run TypeScript type check**

```bash
cd launcher && npx tsc --noEmit 2>&1
```

Expected: No errors.

**Step 3: Build the launcher**

```bash
cd launcher && cargo build 2>&1 | tail -10
```

Expected: Build succeeds.

**Step 4: Commit any remaining changes**

```bash
git add -A
git commit -m "chore(launcher): finalize stage update notification feature"
```

---

## Verification checklist

- [ ] Rust tests for `check_stage_update` pass (7 test cases)
- [ ] Rust tests for `set_bioflow_tag` pass (3 test cases)
- [ ] Rust tests for `update_to_stage` pass (4 test cases)
- [ ] TypeScript tests for `checkStageUpdate` pass (8 test cases)
- [ ] TypeScript type check passes with no errors
- [ ] Rust build succeeds
- [ ] On alpha/beta with newer tag: button reads "Update to X.Y.Z-stage" and is clickable
- [ ] On alpha/beta without newer tag: button is disabled with pinning reason
- [ ] On release: button behavior unchanged (digest check)
- [ ] On developer: button behavior unchanged (disabled with dev reason)
- [ ] Confirmation dialog appears on click; cancel blocks the update
- [ ] Confirmed update rewrites .env, pulls, and recreates
