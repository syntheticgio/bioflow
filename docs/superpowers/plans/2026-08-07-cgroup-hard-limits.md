# Opt-in cgroup hard limits — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user opt into a kernel-enforced memory ceiling on the worker container, set from the launcher, blank by default.

**Architecture:** The launcher writes `BIOFLOW_HARD_MEM_LIMIT` (compose-facing, e.g. `16g`) and `BIOFLOW_HARD_MEM_MB` (a plain integer for the API) into `.env`, then recreates the stack. `docker-compose.yml` applies `mem_limit` to `worker` only; an unset variable defaults to `0`, Docker's own no-limit value, so off-by-default needs no code. The API receives the MB value as an env var and clamps the soft admission budget to it. The worker turns exit 137 terminal when a limit is set.

**Tech Stack:** Rust (Tauri launcher, `cargo test`), React + TypeScript (launcher UI and web UI), Python (FastAPI backend, `pytest`), Docker Compose.

**Spec:** [`docs/superpowers/specs/2026-08-07-cgroup-hard-limits-design.md`](../specs/2026-08-07-cgroup-hard-limits-design.md)

---

## Background an implementer needs

**How the launcher configures the stack.** `launcher/src-tauri/src/settings.rs::apply()` fully rewrites `.env` in the install directory (never patches it), then calls `docker.up()` which runs `docker compose up -d`. The rewrite is a full replace, so any line not emitted by `render_env()` is lost — that is why `env_extra` exists.

**The launcher never reads `.env` back.** `launcher/src/App.tsx:32` seeds settings from hardcoded defaults (`port: 5173`, empty storage location). Nothing parses `.env` into `Settings`. This matters for Task 5: without a read-back, a limit set in one session shows blank on the next, and the next Apply would silently erase it.

**Why `api` is not capped.** Capping the API container would put the web UI under an OOM ceiling — a user who set the limit too low would lose the interface needed to raise it. So `api` gets the number as an env var instead of reading its own cgroup, which would return `max`.

**Why the governor needs no change.** `governor.mem_budget_bytes()` (`backend/app/queue/governor.py:221`) already falls back to `_read_cgroup_mem()`. That runs inside the worker, whose cgroup *is* limited. Verified end-to-end in Task 8; no code task.

**Test commands.** Rust: `cd launcher/src-tauri && cargo test`. Python from a worktree: `./backend/run-worktree-tests.sh tests/ -q` (never `docker compose exec api`, which tests main's code). Launcher TS: `cd launcher && npm test`.

---

## File Structure

**Modify:**
- `launcher/src-tauri/src/settings.rs` — `CurrentSettings` gains `hard_mem_mb: Option<u32>`; `render_env` emits the two vars and pins replicas
- `launcher/src-tauri/src/commands.rs:386` — `ApplySettingsArgs` and `apply_settings` carry the new field
- `launcher/src/types.ts` — `Settings` gains `hardMemGb: string`
- `launcher/src/Settings.tsx` — the field, its copy, and validation
- `launcher/src/App.tsx:32` — seed and persist the new field
- `docker-compose.yml` — `mem_limit` on `worker`, env var on `api`
- `backend/app/config.py:65` — `bioflow_hard_mem_mb` setting
- `backend/app/services/resource_limit_service.py` — clamp in `resolve_mem_budget_mb`, new `hard_mem_mb()` accessor
- `backend/app/api/v1/settings.py:97-123` — `ResourceLimitsOut.hard_mem_mb`, save-side rejection
- `frontend/src/api/types.ts` — `ResourceLimits.hard_mem_mb`
- `frontend/src/components/SettingsResources.tsx` — clamp copy and validation
- `backend/app/queue/pipeline_handlers.py:756-771` — `_failure` takes the hard limit into account

**Create:**
- `launcher/src/settings-logic.ts` — pure parse/validate for the GB field, so it is testable without a DOM (mirrors the existing `wizard-logic.ts` split)
- `launcher/src/settings-logic.test.ts`
- `backend/tests/services/test_hard_mem_clamp.py`
- `backend/tests/queue/test_failure_classification.py`

---

## Task 1: Launcher writes the two env vars and pins replicas

**Files:**
- Modify: `launcher/src-tauri/src/settings.rs`
- Test: `launcher/src-tauri/src/settings.rs` (inline `#[cfg(test)] mod tests`)

- [ ] **Step 1: Write the failing tests**

Add to the existing `mod tests` in `launcher/src-tauri/src/settings.rs`:

```rust
    fn settings_with_hard_mem(hard_mem_mb: Option<u32>) -> CurrentSettings {
        CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
            hard_mem_mb,
        }
    }

    #[test]
    fn blank_hard_limit_writes_no_limit_lines_at_all() {
        // Absence, not an empty assignment. `BIOFLOW_HARD_MEM_LIMIT=` would
        // also read as unlimited to Compose today, but that makes the "off"
        // state depend on Compose's empty-value handling rather than on the
        // variable simply being unset.
        let env = render_env(&settings_with_hard_mem(None), &[]);
        assert!(!env.contains("BIOFLOW_HARD_MEM_LIMIT"));
        assert!(!env.contains("BIOFLOW_HARD_MEM_MB"));
        assert!(!env.contains("WORKER_REPLICAS"));
    }

    #[test]
    fn set_hard_limit_writes_both_vars_and_pins_replicas() {
        let env = render_env(&settings_with_hard_mem(Some(16384)), &[]);
        assert!(env.contains("BIOFLOW_HARD_MEM_LIMIT=16384m"));
        assert!(env.contains("BIOFLOW_HARD_MEM_MB=16384"));
        // mem_limit is per-container; 2 replicas would double the wall.
        assert!(env.contains("WORKER_REPLICAS=1"));
    }

    #[test]
    fn clearing_a_previously_set_limit_removes_every_trace() {
        // The direction that regresses silently: a stale
        // BIOFLOW_HARD_MEM_LIMIT left behind would keep enforcing a limit the
        // user believes they removed. render_env is a full replace, so this
        // asserts the replace is genuinely total.
        let was_set = render_env(&settings_with_hard_mem(Some(8192)), &[]);
        assert!(was_set.contains("BIOFLOW_HARD_MEM_LIMIT=8192m"));

        let now_clear = render_env(&settings_with_hard_mem(None), &[]);
        assert!(!now_clear.contains("BIOFLOW_HARD_MEM_LIMIT"));
        assert!(!now_clear.contains("BIOFLOW_HARD_MEM_MB"));
        assert!(!now_clear.contains("WORKER_REPLICAS"));
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd launcher/src-tauri && cargo test settings::`

Expected: FAIL to compile — `CurrentSettings` has no field `hard_mem_mb`.

- [ ] **Step 3: Add the field and emit the lines**

In `launcher/src-tauri/src/settings.rs`, add to `CurrentSettings`:

```rust
    /// Kernel-enforced memory ceiling for the worker container, in MB.
    /// `None` means no hard cap -- the default, and what Compose sees as an
    /// unset `mem_limit`. There is deliberately no separate on/off flag: a
    /// toggle plus a number is two controls that can disagree, and the
    /// number alone already expresses off.
    pub hard_mem_mb: Option<u32>,
```

Replace `render_env` with:

```rust
fn render_env(settings: &CurrentSettings, env_extra: &[(String, String)]) -> String {
    let mut lines = vec![
        format!("BIOINFO_HOME={}", settings.storage_location.display()),
        format!("WEB_PORT={}", settings.port),
        format!("BIND_ADDRESS={}", settings.bind_address()),
        "BIOFLOW_TAG=latest".to_string(),
    ];

    // Emitted only when a limit is set, so "off" is the variable being
    // absent rather than present-and-empty. Replicas are pinned because
    // mem_limit is per-container: two workers under a 16 GB limit would
    // let the machine reach 32 GB, and the wall would sit at twice the
    // number the user typed.
    if let Some(mb) = settings.hard_mem_mb {
        lines.push(format!("BIOFLOW_HARD_MEM_LIMIT={mb}m"));
        lines.push(format!("BIOFLOW_HARD_MEM_MB={mb}"));
        lines.push("WORKER_REPLICAS=1".to_string());
    }

    for (key, value) in env_extra {
        lines.push(format!("{key}={value}"));
    }
    lines.push(String::new());
    lines.join("\n")
}
```

Fix the three existing tests that construct `CurrentSettings` (`default_settings_bind_to_loopback`, `network_exposed_toggle_binds_to_all_interfaces`, `apply_rewrites_env_and_preserves_extra_lines`) by adding `hard_mem_mb: None` to each literal.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd launcher/src-tauri && cargo test settings::`

Expected: PASS, including the three pre-existing tests.

- [ ] **Step 5: Commit**

```bash
git add launcher/src-tauri/src/settings.rs
git commit -m "feat(launcher): write hard memory limit vars to .env"
```

---

## Task 2: Wire the field through the Tauri command

**Files:**
- Modify: `launcher/src-tauri/src/commands.rs:386-424`

- [ ] **Step 1: Add the field to the args struct**

In `launcher/src-tauri/src/commands.rs`, change `ApplySettingsArgs`:

```rust
#[derive(Debug, Deserialize)]
pub struct ApplySettingsArgs {
    pub storage_location: String,
    pub port: u16,
    pub network_exposed: bool,
    /// `None` when the user left the field blank -- no hard cap.
    pub hard_mem_mb: Option<u32>,
}
```

- [ ] **Step 2: Pass it into CurrentSettings**

In the same file, in `apply_settings`, change the `CurrentSettings` construction:

```rust
    let settings = CurrentSettings {
        storage_location: PathBuf::from(args.storage_location),
        port: args.port,
        network_exposed: args.network_exposed,
        hard_mem_mb: args.hard_mem_mb,
    };
```

- [ ] **Step 3: Verify it compiles**

Run: `cd launcher/src-tauri && cargo test`

Expected: PASS, no compile errors.

- [ ] **Step 4: Commit**

```bash
git add launcher/src-tauri/src/commands.rs
git commit -m "feat(launcher): carry hard_mem_mb through apply_settings"
```

---

## Task 3: Pure GB-field parsing and validation

**Files:**
- Create: `launcher/src/settings-logic.ts`
- Create: `launcher/src/settings-logic.test.ts`

The launcher shows GB (friendlier to type) and the backend wants MB. Keeping the conversion pure mirrors the existing `wizard-logic.ts` split and makes it testable without a DOM.

- [ ] **Step 1: Write the failing test**

Create `launcher/src/settings-logic.test.ts`:

```typescript
import { describe, expect, it } from "vitest";
import { parseHardMemGb } from "./settings-logic";

describe("parseHardMemGb", () => {
  it("treats blank as no hard cap", () => {
    expect(parseHardMemGb("")).toEqual({ kind: "none" });
    expect(parseHardMemGb("   ")).toEqual({ kind: "none" });
  });

  it("converts a valid GB value to MB", () => {
    expect(parseHardMemGb("16")).toEqual({ kind: "set", mb: 16384 });
    expect(parseHardMemGb("1.5")).toEqual({ kind: "set", mb: 1536 });
  });

  it("rejects values that are not a positive number", () => {
    expect(parseHardMemGb("abc").kind).toBe("invalid");
    expect(parseHardMemGb("0").kind).toBe("invalid");
    expect(parseHardMemGb("-4").kind).toBe("invalid");
  });

  it("rejects a limit too small to run anything", () => {
    // A 0.2 GB ceiling would OOM-kill the worker on startup, before any job
    // runs -- an unrecoverable state reached by a plausible typo.
    expect(parseHardMemGb("0.2").kind).toBe("invalid");
  });

  it("accepts the smallest sane limit", () => {
    expect(parseHardMemGb("2")).toEqual({ kind: "set", mb: 2048 });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd launcher && npm test -- settings-logic`

Expected: FAIL — cannot resolve `./settings-logic`.

- [ ] **Step 3: Write the implementation**

Create `launcher/src/settings-logic.ts`:

```typescript
/**
 * Parsing for the hard memory limit field.
 *
 * Blank is a real value meaning "no hard cap", not an error and not a
 * disabled state -- it is the default every fresh install has. The field is
 * shown in GB because that is what a human types; the backend wants MB.
 */

const MB_PER_GB = 1024;

/**
 * Below this, the worker cannot start at all: the container would be
 * OOM-killed before running any job, which is unrecoverable from the UI.
 * Rejecting it at the field is the only place a user gets told.
 */
export const MIN_HARD_MEM_GB = 1;

export type HardMemValue =
  | { kind: "none" }
  | { kind: "set"; mb: number }
  | { kind: "invalid" };

export function parseHardMemGb(raw: string): HardMemValue {
  if (raw.trim() === "") return { kind: "none" };

  const gb = Number(raw);
  if (!Number.isFinite(gb) || gb <= 0) return { kind: "invalid" };
  if (gb < MIN_HARD_MEM_GB) return { kind: "invalid" };

  return { kind: "set", mb: Math.round(gb * MB_PER_GB) };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd launcher && npm test -- settings-logic`

Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add launcher/src/settings-logic.ts launcher/src/settings-logic.test.ts
git commit -m "feat(launcher): parse the hard memory limit field"
```

---

## Task 4: The launcher settings field and its copy

**Files:**
- Modify: `launcher/src/types.ts`
- Modify: `launcher/src/Settings.tsx`
- Modify: `launcher/src/commands.ts`

- [ ] **Step 1: Add the field to the Settings type**

In `launcher/src/types.ts`:

```typescript
export interface Settings {
  storageLocation: string;
  port: number;
  networkExposed: boolean;
  /** GB as typed, or "" for no hard cap. Converted to MB at the IPC edge. */
  hardMemGb: string;
}
```

- [ ] **Step 2: Update the IPC caller**

In `launcher/src/commands.ts`, find `applySettings` and change its argument type and body so it converts GB to MB. If the current signature is `applySettings(args: Settings)`, replace the invoke payload construction with:

```typescript
import { parseHardMemGb } from "./settings-logic";

export async function applySettings(args: Settings): Promise<void> {
  const hard = parseHardMemGb(args.hardMemGb);
  await invoke("apply_settings", {
    args: {
      storage_location: args.storageLocation,
      port: args.port,
      network_exposed: args.networkExposed,
      hard_mem_mb: hard.kind === "set" ? hard.mb : null,
    },
  });
}
```

Keep the existing `invoke` import and any surrounding error handling exactly as it is; only the payload gains `hard_mem_mb`.

- [ ] **Step 3: Add the field to the dialog**

In `launcher/src/Settings.tsx`, add the import and state:

```typescript
import { parseHardMemGb } from "./settings-logic";
```

```typescript
  const [hardMemGb, setHardMemGb] = useState(current.hardMemGb);
```

Change the `handleApply` body's two calls to include the field:

```typescript
      await applySettings({ storageLocation, port, networkExposed, hardMemGb });
      onApplied({ storageLocation, port, networkExposed, hardMemGb });
```

Add this field block inside `<div className="dialog-fields">`, after the network exposure block:

```tsx
          <div className="field dialog-field-narrow">
            <span className="field-label">Hard memory limit (GB)</span>
            <input
              className="field-value-input field-value-numeric"
              type="number"
              min="1"
              step="1"
              placeholder="No limit"
              value={hardMemGb}
              onChange={(e) => setHardMemGb(e.target.value)}
              disabled={applying}
              aria-label="Hard memory limit in GB"
            />
            {hardMem.kind === "none" && (
              <span className="field-hint" role="note">
                No hard cap. BioFlow will not <em>plan</em> to exceed the memory
                budget you set inside the app, but a job that uses more than
                predicted can still go over. Nothing is killed.
              </span>
            )}
            {hardMem.kind === "set" && (
              <span className="field-hint-warn" role="note">
                BioFlow <em>cannot</em> exceed {hardMemGb} GB. A job that tries is
                killed and loses its work. Protects the machine; costs the job.
                Takes effect on restart.
              </span>
            )}
            {hardMem.kind === "invalid" && (
              <span className="field-hint-warn" role="note">
                Enter a whole number of GB (at least 1), or leave blank for no
                hard cap.
              </span>
            )}
          </div>
```

Add the derived value next to the existing `storageChanged` line:

```typescript
  const hardMem = parseHardMemGb(hardMemGb);
```

And disable Apply while the field is invalid — change the Apply button's `disabled`:

```tsx
          <button
            className="btn btn-primary"
            onClick={handleApply}
            disabled={applying || hardMem.kind === "invalid"}
          >
```

- [ ] **Step 4: Verify it builds**

Run: `cd launcher && npm run build`

Expected: builds with no TypeScript errors. If `App.tsx` errors about a missing `hardMemGb`, that is expected — Task 5 fixes it. If you want a clean build here, do Task 5 first; they can also be committed together.

- [ ] **Step 5: Commit**

```bash
git add launcher/src/types.ts launcher/src/Settings.tsx launcher/src/commands.ts
git commit -m "feat(launcher): hard memory limit field with both-state copy"
```

---

## Task 5: Read the limit back from .env so it survives a restart

**Files:**
- Modify: `launcher/src-tauri/src/commands.rs`
- Modify: `launcher/src/App.tsx:32`

Nothing currently parses `.env` back into the launcher UI — `App.tsx` seeds from hardcoded defaults. Without this task, a limit set in one session shows blank on the next, and the next Apply silently erases it. That is the same silent-regression shape Task 1's third test guards against, one layer up.

- [ ] **Step 1: Write the failing test**

Add to `launcher/src-tauri/src/commands.rs` (create a `#[cfg(test)] mod tests` at the end of the file if none exists):

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_hard_mem_mb_back_from_env() {
        let env = "BIOINFO_HOME=/data\nWEB_PORT=5173\nBIOFLOW_HARD_MEM_MB=16384\n";
        assert_eq!(parse_hard_mem_mb(env), Some(16384));
    }

    #[test]
    fn absent_hard_mem_reads_as_no_limit() {
        let env = "BIOINFO_HOME=/data\nWEB_PORT=5173\n";
        assert_eq!(parse_hard_mem_mb(env), None);
    }

    #[test]
    fn malformed_hard_mem_reads_as_no_limit() {
        // A hand-edited .env should not stop the launcher from opening.
        let env = "BIOFLOW_HARD_MEM_MB=not-a-number\n";
        assert_eq!(parse_hard_mem_mb(env), None);
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd launcher/src-tauri && cargo test parse_hard_mem`

Expected: FAIL to compile — `parse_hard_mem_mb` is not defined.

- [ ] **Step 3: Write the parser and expose it**

Add to `launcher/src-tauri/src/commands.rs`:

```rust
/// Reads `BIOFLOW_HARD_MEM_MB` out of a `.env` body.
///
/// Anything unparseable reads as `None` rather than erroring: a hand-edited
/// `.env` must not stop the launcher from opening, and "no hard cap" is the
/// safe reading of a value nobody can interpret.
fn parse_hard_mem_mb(env_contents: &str) -> Option<u32> {
    env_contents
        .lines()
        .find_map(|line| line.strip_prefix("BIOFLOW_HARD_MEM_MB="))
        .and_then(|value| value.trim().parse().ok())
}
```

Then find the existing command that reports current settings to the UI (the one backing `App.tsx`'s settings state — search for `install_dir.lock()` in a `#[tauri::command]` returning state) and have it include the parsed value by reading `install_dir.join(".env")`:

```rust
    let hard_mem_mb = std::fs::read_to_string(install_dir.join(".env"))
        .ok()
        .and_then(|contents| parse_hard_mem_mb(&contents));
```

Return `hard_mem_mb` as part of that command's DTO, adding the field to the DTO struct with `#[derive(Serialize)]` alongside its existing fields.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd launcher/src-tauri && cargo test parse_hard_mem`

Expected: PASS, 3 tests.

- [ ] **Step 5: Seed the UI state from it**

In `launcher/src/App.tsx`, change the settings initializer at line 32:

```typescript
  const [settings, setSettings] = useState<SettingsValues>({
    storageLocation: "",
    port: 5173,
    networkExposed: false,
    hardMemGb: "",
  });
```

Then, wherever `status()` populates settings, convert the MB value back to a GB string (`String(mb / 1024)`) and set `hardMemGb`. If the status DTO does not yet flow into `settings`, add it to the same `setSettings` call that handles port.

- [ ] **Step 6: Verify the build is clean**

Run: `cd launcher && npm run build && cd src-tauri && cargo test`

Expected: both succeed.

- [ ] **Step 7: Commit**

```bash
git add launcher/src-tauri/src/commands.rs launcher/src/App.tsx
git commit -m "feat(launcher): read the hard memory limit back from .env"
```

---

## Task 6: Compose applies the limit to worker only

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add mem_limit to worker**

In `docker-compose.yml`, in the `worker` service, add above `deploy:`:

```yaml
    # Opt-in kernel-enforced ceiling. `0` (the default, and what an unset
    # BIOFLOW_HARD_MEM_LIMIT resolves to) is Docker's own "no limit" value,
    # matching `docker run --memory=0` -- not a sentinel this repo invented.
    # An empty-string default was tried first: Compose v5 type-checks
    # mem_limit as a byte-size field and rejects '' outright, so off-by-default
    # needs this specific default rather than an empty one, though it is still
    # free of code -- the launcher simply omits the variable and this default
    # fills in.
    # Worker only: capping `api` would put the web UI under an OOM ceiling,
    # and a user who set the limit too low would lose the interface they need
    # in order to raise it.
    mem_limit: ${BIOFLOW_HARD_MEM_LIMIT:-0}
```

- [ ] **Step 2: Pass the MB value to api**

In the `api` service's `environment:` block, add:

```yaml
      # For the admission-budget clamp only; `api` itself is NOT capped.
      # It cannot read the worker's cgroup, and reading its own would return
      # `max` and silently skip the clamp -- failing open exactly when a
      # guarantee was requested.
      BIOFLOW_HARD_MEM_MB: ${BIOFLOW_HARD_MEM_MB:-}
```

- [ ] **Step 3: Verify compose still parses with the variable unset**

Run: `docker compose -p biopipe-plancheck config --quiet`

Expected: exits 0 with no output. (A project name is required — bare `docker compose` from a worktree is blocked by `ops/hooks/block-compose-in-worktree.sh`. This only validates the file; it starts nothing.)

- [ ] **Step 4: Verify the limit appears when the variable is set**

Run: `BIOFLOW_HARD_MEM_LIMIT=16384m docker compose -p biopipe-plancheck config | grep -A2 "mem_limit"`

Expected: shows the worker's `mem_limit` resolved to bytes (`17179869184` for `16384m`).

Also verify the unset case doesn't just parse but genuinely reads as "no limit": `docker compose -p biopipe-plancheck config | grep mem_limit` should print nothing at all -- Compose omits a `0`-valued `mem_limit` from its rendered config entirely, which is a second confirmation (beyond the exit code) that it's being read as unlimited rather than as a limit of zero bytes.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: apply opt-in mem_limit to the worker container"
```

---

## Task 7: The API clamps the soft budget to the hard limit

**Files:**
- Modify: `backend/app/config.py:65`
- Modify: `backend/app/services/resource_limit_service.py`
- Modify: `backend/app/api/v1/settings.py:97-123,284-308`
- Create: `backend/tests/services/test_hard_mem_clamp.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_hard_mem_clamp.py`:

```python
"""The clamp that makes a soft budget above the hard limit unrepresentable.

Without it, a 32 GB admission budget under a 16 GB kernel ceiling means every
job admission approves is then OOM-killed -- which reads as BioFlow being
broken rather than as a misconfiguration.
"""

import pytest

from app.services.resource_limit_service import resolve_mem_budget_mb


def test_hard_limit_lowers_a_larger_soft_budget():
    # The case that matters: the clamp must actually bind.
    assert (
        resolve_mem_budget_mb(stored_mb=32768, machine_mb=65536, hard_mem_mb=16384)
        == 16384
    )


def test_soft_budget_below_the_hard_limit_is_untouched():
    # The normal, correct configuration -- admission keeps jobs off the wall.
    assert (
        resolve_mem_budget_mb(stored_mb=8192, machine_mb=65536, hard_mem_mb=16384)
        == 8192
    )


def test_no_hard_limit_leaves_existing_behaviour_unchanged():
    # The clamp must not become an unconditional ceiling.
    assert resolve_mem_budget_mb(stored_mb=32768, machine_mb=65536, hard_mem_mb=None) == 32768


def test_hard_limit_binds_even_with_no_soft_budget():
    # "No limit" in the web UI still cannot exceed the kernel's ceiling.
    assert (
        resolve_mem_budget_mb(stored_mb=None, machine_mb=65536, hard_mem_mb=16384)
        == 16384
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_hard_mem_clamp.py -q`

Expected: FAIL — `resolve_mem_budget_mb() got an unexpected keyword argument 'hard_mem_mb'`.

- [ ] **Step 3: Add the setting**

In `backend/app/config.py`, after line 65 (`bioinfo_mem_budget_mb`):

```python
    # Kernel-enforced ceiling on the worker container, set by the launcher.
    # None means no hard cap. `api` is not itself capped, so this arrives as
    # an env var rather than being read from a cgroup -- see
    # docs/superpowers/specs/2026-08-07-cgroup-hard-limits-design.md.
    bioflow_hard_mem_mb: int | None = None
```

- [ ] **Step 4: Add the clamp**

In `backend/app/services/resource_limit_service.py`, replace `resolve_mem_budget_mb`:

```python
def resolve_mem_budget_mb(
    *, stored_mb: int | None, machine_mb: int, hard_mem_mb: int | None = None
) -> int:
    """The memory ceiling admission should compute headroom against.

    A stored limit only ever *lowers* the budget. Typing 64 GB on a 16 GB
    machine cannot conjure headroom, and letting it try would over-admit
    exactly as badly as having no limit at all -- the number is a budget to
    stay under, not a claim about the hardware.

    Zero and negatives are treated as "no opinion" rather than as a real
    ceiling of nothing. A literal zero budget would admit no job ever and
    stall the queue with no error anywhere, which is the silent-failure shape
    this codebase already goes out of its way to avoid.

    `hard_mem_mb` is the kernel-enforced cgroup ceiling when one is set. It
    binds unconditionally: a soft budget above it would admit jobs the kernel
    then kills, which is the worst available outcome. Below it is normal and
    expected -- that is admission doing its job and the wall never being hit.
    """
    budget = machine_mb
    if stored_mb is not None and stored_mb > 0:
        budget = min(stored_mb, machine_mb)
    if hard_mem_mb is not None and hard_mem_mb > 0:
        budget = min(budget, hard_mem_mb)
    return budget


def hard_mem_mb() -> int | None:
    """The kernel-enforced ceiling, or None when hard limits are off.

    Reads the value the launcher passed in rather than a cgroup file: `api`
    is deliberately uncapped, so its own cgroup reports `max`.
    """
    configured = settings.bioflow_hard_mem_mb
    if configured is None or configured <= 0:
        return None
    return configured
```

Add the import at the top of the file:

```python
from app.config import settings
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_hard_mem_clamp.py -q`

Expected: PASS, 4 tests.

- [ ] **Step 6: Surface and enforce it at the API**

In `backend/app/api/v1/settings.py`, add to `ResourceLimitsOut` after `machine_cpu`:

```python
    # The kernel-enforced ceiling, when hard limits are on. None means the
    # soft budget is the only ceiling and nothing is ever killed.
    hard_mem_mb: int | None
```

Change `_limits_out`:

```python
def _limits_out(limits) -> ResourceLimitsOut:
    machine_mem_mb, machine_cpu = _machine_budget()
    return ResourceLimitsOut(
        max_mem_mb=limits.max_mem_mb,
        max_cpu=limits.max_cpu,
        max_threads=limits.max_threads,
        machine_mem_mb=machine_mem_mb,
        machine_cpu=machine_cpu,
        hard_mem_mb=resource_limit_service.hard_mem_mb(),
    )
```

Change `set_resource_limits` to reject a budget above the ceiling:

```python
@router.put("/resources", response_model=ResourceLimitsOut)
async def set_resource_limits(body: ResourceLimitsIn) -> ResourceLimitsOut:
    # Refused rather than silently clamped: a budget that saves as a number
    # the user did not type is worse than an error saying why.
    hard = resource_limit_service.hard_mem_mb()
    if hard is not None and body.max_mem_mb is not None and body.max_mem_mb > hard:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A hard limit of {hard} MB is enforced on this machine. "
                f"The memory budget cannot exceed it."
            ),
        )
    limits = await resource_limit_service.save(
        max_mem_mb=body.max_mem_mb,
        max_cpu=body.max_cpu,
        max_threads=body.max_threads,
    )
    return _limits_out(limits)
```

Ensure `HTTPException` is imported from `fastapi` at the top of the file (add it to the existing `from fastapi import ...` line if absent).

- [ ] **Step 7: Run the full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`

Expected: PASS. Read the count — some existing tests construct `ResourceLimitsOut` and will need `hard_mem_mb=None` added.

- [ ] **Step 8: Commit**

```bash
git add backend/app/config.py backend/app/services/resource_limit_service.py backend/app/api/v1/settings.py backend/tests/services/test_hard_mem_clamp.py
git commit -m "feat(api): clamp the admission budget to the cgroup hard limit"
```

---

## Task 8: Exit 137 is terminal under a hard limit

**Files:**
- Modify: `backend/app/queue/pipeline_handlers.py:756-771`
- Create: `backend/tests/queue/test_failure_classification.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/queue/test_failure_classification.py`:

```python
"""How a killed tool is classified, with and without a hard limit.

Retrying a 137 is right on an unlimited machine -- the host OOM killer fired
under transient pressure and a later attempt may succeed. It is wrong under a
cgroup ceiling, which does not move: the job dies identically on all five
attempts, burning its full runtime each time.
"""

from pathlib import Path

import pytest

from app.errors import PermanentError, RetryableError
from app.queue.pipeline_handlers import _failure


def test_137_is_retryable_when_there_is_no_hard_limit(tmp_path, monkeypatch):
    # The regression guard: existing behaviour on an unlimited machine.
    monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", None)
    err = _failure(137, tmp_path / "missing.log", tool="minimap2")
    assert isinstance(err, RetryableError)


def test_137_is_terminal_when_a_hard_limit_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", 16384)
    err = _failure(137, tmp_path / "missing.log", tool="minimap2")
    # PermanentError and RetryableError are siblings under AppError, so this
    # single assertion is enough -- it cannot pass for a retryable error.
    assert isinstance(err, PermanentError)


def test_terminal_137_message_names_the_ceiling(tmp_path, monkeypatch):
    # With a known ceiling the cause is known, so the message says it rather
    # than guessing -- the whole reason this branch is worth having.
    monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", 16384)
    err = _failure(137, tmp_path / "missing.log", tool="minimap2")
    assert "16384 MB hard limit" in str(err)


def test_non_137_exits_are_unaffected_by_the_hard_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", 16384)
    err = _failure(1, tmp_path / "missing.log", tool="minimap2")
    assert isinstance(err, PermanentError)
    assert "hard limit" not in str(err)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/queue/test_failure_classification.py -q`

Expected: 2 FAIL (`test_137_is_terminal_when_a_hard_limit_is_set`, `test_terminal_137_message_names_the_ceiling`), 2 PASS.

- [ ] **Step 3: Change the classification**

In `backend/app/queue/pipeline_handlers.py`, replace `_failure`:

```python
def _failure(code: int, log_path: Path, tool: str = "fastp") -> Exception:
    """Classify a non-zero exit from an external tool.

    The tail of the log goes into the message because the job record is where
    the user looks first, and "fastp exited 1" on its own tells them nothing.
    """
    tail = _log_tail(log_path)
    detail = f"{tool} exited {code}"
    if tail:
        detail = f"{detail}: {tail}"

    if code == 137:
        # 137 is SIGKILL, which on this stack means the OOM killer.
        #
        # Under a cgroup hard limit the ceiling does not move, so the job dies
        # identically on every attempt -- job_max_attempts turns one dead job
        # into five full-length dead ones. Terminal, and the message names the
        # cause, which is only possible because the ceiling is known.
        hard_mem_mb = settings.bioflow_hard_mem_mb
        if hard_mem_mb:
            return PermanentError(
                f"{detail} (killed at the {hard_mem_mb} MB hard limit -- "
                f"this job needs more memory than the limit allows)"
            )
        # With no ceiling, this was the host OOM killer under transient
        # pressure: a quieter machine or fewer threads may well succeed.
        return RetryableError(f"{detail} (killed, most likely out of memory)")

    return PermanentError(detail)
```

Confirm `settings` is already imported in this module (it is — `_prepare_workdir` uses `settings.tmp_dir`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/queue/test_failure_classification.py -q`

Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/pipeline_handlers.py backend/tests/queue/test_failure_classification.py
git commit -m "fix(worker): make exit 137 terminal under a cgroup hard limit"
```

---

## Task 9: Web UI states the distinction and blocks an over-budget save

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/components/SettingsResources.tsx`

There is no headless component-testing setup in this repo (CLAUDE.md), so this task is verified in the browser at the end of Task 10.

- [ ] **Step 1: Add the field to the API type**

In `frontend/src/api/types.ts`, add to the `ResourceLimits` interface:

```typescript
  /** Kernel-enforced ceiling, or null when hard limits are off. */
  hard_mem_mb: number | null;
```

- [ ] **Step 2: Clamp the input and state the distinction**

In `frontend/src/components/SettingsResources.tsx`, add after the `machineMemGb` line:

```typescript
  const hardMemMb = limits.data.hard_mem_mb;
  const hardMemGb = hardMemMb == null ? null : (hardMemMb / MB_PER_GB).toFixed(1);
  const overHardLimit =
    hardMemMb != null && !noLimit && parseFloat(memGb || "0") * MB_PER_GB > hardMemMb;
```

Change the `invalidMem` line to include it:

```typescript
  const invalidMem =
    (!noLimit &&
      (memGb.trim() === "" || Number.isNaN(parseFloat(memGb)) || parseFloat(memGb) <= 0)) ||
    overHardLimit;
```

Add the `max` attribute to the memory input so the browser hints the ceiling:

```tsx
          max={hardMemMb == null ? undefined : hardMemMb / MB_PER_GB}
```

Replace the existing hint paragraph (the one beginning "BioFlow will not start work it expects to exceed this") with a pair that covers both states:

```tsx
      {hardMemGb == null ? (
        <p className="settings-hint">
          BioFlow will not start work it expects to exceed this. A job that ends
          up using more than predicted is not stopped -- this is an admission
          check on new work, not a running cap. No hard cap is enforced.
        </p>
      ) : (
        <p className="settings-hint">
          A hard limit of {hardMemGb} GB is enforced on this machine: a job that
          exceeds it is killed and loses its work. This budget is the softer
          check that keeps jobs from reaching that limit, and cannot be set
          above it. Change the hard limit in the BioFlow launcher.
        </p>
      )}

      {overHardLimit && (
        <p className="settings-hint settings-hint-warn" role="alert">
          This budget is above the {hardMemGb} GB hard limit. Jobs admitted
          above the limit would be killed by the kernel.
        </p>
      )}
```

- [ ] **Step 3: Verify it builds**

Run: `cd frontend && npx tsc --noEmit`

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/components/SettingsResources.tsx
git commit -m "feat(ui): state the hard-limit distinction and clamp the budget"
```

---

## Task 10: End-to-end verification against a real stack

**Files:** none modified — this is verification.

CLAUDE.md is explicit that a green suite is not enough: the suggestion rules passed every unit test while being wrong about real data, because the fixtures already looked the way the code expected. Two claims here are structurally unverifiable by unit test, and one of them ("the governor picks it up automatically") is the issue's own acceptance criterion.

- [ ] **Step 1: Bring up a worktree stack with a limit set**

```bash
BIOFLOW_HARD_MEM_LIMIT=4g BIOFLOW_HARD_MEM_MB=4096 ./ops/worktree-up.sh
```

Expected: the stack comes up; UI on 5273, API on 8100.

- [ ] **Step 2: Confirm the kernel actually applied the limit**

```bash
docker inspect $(docker ps --filter "name=worker" --format '{{.Names}}' | head -1) --format '{{.HostConfig.Memory}}'
```

Expected: `4294967296` (4 GB in bytes). A `0` means the limit did not apply — check that the compose var reached the container.

- [ ] **Step 3: Confirm the governor reads it as its budget**

This is the acceptance criterion the design claims comes for free.

```bash
docker exec $(docker ps --filter "name=worker" --format '{{.Names}}' | head -1) \
  python -c "from app.queue.governor import LoadGovernor; print(LoadGovernor().mem_budget_bytes())"
```

Expected: `4294967296` — the cgroup limit, not host RAM. If it prints host RAM, `_read_cgroup_mem()` is not seeing the limit and the whole premise needs revisiting before proceeding.

- [ ] **Step 4: Confirm the clamp binds in the real UI**

Open `http://localhost:5273/settings/resources`. Expected: the page states a 4.0 GB hard limit is enforced, and entering a memory budget above 4 GB disables Save and shows the warning.

- [ ] **Step 5: Confirm the blank case still says something**

```bash
./ops/worktree-up.sh --down
./ops/worktree-up.sh
```

Open `http://localhost:5273/settings/resources`. Expected: with no hard limit, the page says no hard cap is enforced and nothing is killed — the blank state is captioned, not silent.

- [ ] **Step 6: Tear down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 7: Confirm the main stack is untouched**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: paths under the main checkout, not under `.claude/worktrees/`. `worktree-up.sh` avoids repointing 5173 by construction; this confirms it.

---

## Task 11: Documentation and issue close-out

**Files:**
- Modify: `docs/TODO.md` or `docs/TODO-done.md` (only if an entry covers this)

- [ ] **Step 1: Check whether a TODO entry covers this work**

```bash
grep -n "cgroup\|hard limit\|enforcement" docs/TODO.md
```

If an entry exists, append ` — FIXED` to its heading, add a note saying what shipped and where the code lives, note what the implementation did differently from this plan, and move the whole entry to `docs/TODO-done.md`. If no entry matches, skip to Step 2 — this work came from an issue, not the backlog.

- [ ] **Step 2: Run the full suite one last time**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count, not just the exit code.

```bash
cd launcher/src-tauri && cargo test && cd .. && npm test
```

Expected: both PASS.

- [ ] **Step 3: Commit any docs changes**

```bash
git add docs/
git commit -m "docs: record cgroup hard limits as shipped"
```

- [ ] **Step 4: Merge and push**

Per CLAUDE.md, once the suite is green and `main` is clean, merge and push without asking.

```bash
git checkout main && git pull && git merge - && ./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS after the merge — `main` may have moved, so the green must be re-established rather than assumed.

```bash
git push origin main
```

- [ ] **Step 5: Update the issue**

```bash
gh issue close 72 --comment "Shipped. Launcher writes BIOFLOW_HARD_MEM_LIMIT/BIOFLOW_HARD_MEM_MB to .env and pins WORKER_REPLICAS=1; compose applies mem_limit to the worker only; the API clamps the admission budget to the ceiling; exit 137 is terminal under a hard limit. Verified against a real stack: docker inspect reports the applied limit and the governor reads it as its budget with no code change."
```
