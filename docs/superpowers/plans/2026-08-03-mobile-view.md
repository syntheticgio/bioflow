# Mobile-Friendly View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship two mobile-only screens -- a read-only job activity feed and an NCBI download dispatch flow -- on separate `/m/*` routes, reached by an automatic redirect on narrow viewports.

**Architecture:** A `MobileShell` sits beside the desktop `Shell` under the existing profile `Gate`, rendering three routes with its own self-contained stylesheet. Job-formatting logic currently private to `ActivityView.tsx` moves to `lib/runFormat.ts` so both views share one definition rather than drifting. No backend changes: all seven endpoints already exist.

**Tech Stack:** React 18, react-router-dom v6, TanStack Query v5, zustand, Vite, vitest (for pure-logic tests only -- this repo has no component-testing setup).

**Design:** `docs/superpowers/specs/2026-08-03-mobile-view-design.md`

---

## Context you need before starting

**This repo has no frontend component tests.** There is no jsdom, no
testing-library, and zero `.test.tsx` files. `vitest` exists and is used for
pure-logic modules -- `frontend/src/components/HelpSoftware.test.ts` is the
only example. So:

- Tasks that extract or add **pure functions** get real vitest TDD.
- Tasks that build **components** are verified by hand in a browser. That is
  this repo's actual practice, not a shortcut. Do not add a component-testing
  framework as part of this work.

**Run the frontend test suite:**

```bash
docker compose -p biopipe-wt exec web npm test
```

If the worktree stack is not up, or you would rather not depend on it, run it
on the host from `frontend/`:

```bash
cd frontend && npm test
```

**Never run bare `docker compose` from this worktree.** A `PreToolUse` hook
blocks it, because the bind mounts are relative and it would silently
repoint the main stack on port 5173 at this worktree's code. To see the app:

```bash
./ops/worktree-up.sh
```

That serves this worktree at **http://localhost:5273** (API on 8100). Stop it
with `./ops/worktree-up.sh --down`.

**Typecheck** (there is no separate lint step -- `lint` *is* the typecheck):

```bash
cd frontend && npm run lint
```

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `frontend/src/mobile/useIsMobile.ts` | Viewport detection + the `forceDesktop` override flag. Pure-ish, testable parts extracted. |
| `frontend/src/mobile/MobileShell.tsx` | Header, bottom tab bar, `<Outlet/>`. No splitter, no `DetailPanel`, no `UploadTray`. |
| `frontend/src/mobile/MobileActivity.tsx` | The job feed. |
| `frontend/src/mobile/MobileDownload.tsx` | Project picker, search box, segmented results. |
| `frontend/src/mobile/MobileConfirm.tsx` | Runs checklist or assembly components, then launch. |
| `frontend/src/mobile/downloadStore.ts` | Carries the resolved accession between `/m/download` and `/m/download/:accession`. |
| `frontend/src/styles/mobile.css` | Self-contained mobile styles. |
| `frontend/src/lib/jobFormat.test.ts` | Tests for the extracted job helpers. |

**Modified:**

| File | Change |
|---|---|
| `frontend/src/lib/runFormat.ts` | Gains `RUNNING`, `WAITING`, `BLOCKED`, `waitingReason`, `jobLabel`. |
| `frontend/src/components/ActivityView.tsx` | Imports those instead of defining them. |
| `frontend/src/App.tsx` | Mounts `/m/*` under `Gate`; adds the redirect. |
| `frontend/src/components/Header.tsx` | "Mobile view" link. |
| `frontend/src/components/Footer.tsx` | (nothing -- see Task 8 note) |

**Why `lib/runFormat.ts` rather than a new `lib/jobFormat.ts`:** the file
already exists to hold "shared vocabulary for describing a run to a person",
and its own docstring says it lives there because two views render the same
thing in different shapes. That is exactly this situation, with a third view.
The test file is named `jobFormat.test.ts` only because `runFormat.test.ts`
does not exist yet and these tests cover the job half specifically -- if you
prefer `runFormat.test.ts`, that is fine, just be consistent.

---

## Task 1: Extract job helpers into `runFormat.ts`

The mobile feed needs `RUNNING`, `WAITING`, `waitingReason` and `jobLabel`,
all currently private to `ActivityView.tsx`. Reimplementing them on mobile is
how the two views drift. `jobLabel` is the risky one: it guesses `r1_name` /
`r2_name` / `name` out of an untyped payload, so a second copy silently stops
matching the day a handler renames a payload field.

**Files:**
- Modify: `frontend/src/lib/runFormat.ts`
- Create: `frontend/src/lib/jobFormat.test.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/lib/jobFormat.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { RUNNING, WAITING, BLOCKED, waitingReason, jobLabel } from "./runFormat";
import type { JobSummary, SystemLoad } from "../api/types";

/**
 * These moved out of ActivityView.tsx so the mobile feed could reuse them
 * rather than growing a second copy. jobLabel is the one that most needed
 * pinning: it reads untyped payload keys, so a divergent copy would fail
 * silently the day a handler renames one.
 */

const job = (over: Partial<JobSummary> = {}) =>
  ({
    id: "j1",
    type: "align_reads",
    job_class: "compute",
    state: "queued",
    payload: {},
    attempts: 1,
    max_attempts: 3,
    cancel_requested: false,
    ...over,
  }) as unknown as JobSummary;

const load = (over: Partial<SystemLoad> = {}) =>
  ({
    state: "OPEN",
    admitted_classes: ["compute", "user_interactive"],
    ...over,
  }) as unknown as SystemLoad;

describe("state sets", () => {
  it("treats blocked as in-flight, not as finished", () => {
    // The desktop view derives "recent" by negation, which is safe there
    // because a blocked job is shown inside its run's card. A flat feed has
    // no such grouping, so blocked must be positively claimed or it lands
    // under "Recent" looking like it succeeded.
    expect(BLOCKED.has("blocked")).toBe(true);
    expect(RUNNING.has("blocked")).toBe(false);
    expect(WAITING.has("blocked")).toBe(false);
  });

  it("covers every non-terminal state between the three sets", () => {
    const inFlight = ["running", "pending", "queued", "delayed", "blocked"];
    for (const s of inFlight) {
      const claimed =
        RUNNING.has(s) || WAITING.has(s) || BLOCKED.has(s);
      expect(claimed, `${s} is claimed by no set`).toBe(true);
    }
  });
});

describe("waitingReason", () => {
  it("reports cancelling ahead of everything else", () => {
    expect(waitingReason(job({ cancel_requested: true }), load())).toBe(
      "cancelling",
    );
  });

  it("names a retry rather than calling it a queue wait", () => {
    expect(waitingReason(job({ state: "delayed" }), load())).toBe(
      "retrying after a failure",
    );
  });

  it("degrades to a bare wait when the governor is unknown", () => {
    expect(waitingReason(job(), undefined)).toBe("waiting");
  });

  it("blames the governor when this job's class is not admitted", () => {
    expect(
      waitingReason(job(), load({ state: "CLOSED", admitted_classes: [] })),
    ).toBe("waiting: system loaded");
    expect(
      waitingReason(job(), load({ state: "THROTTLED", admitted_classes: [] })),
    ).toBe("waiting: system busy");
  });

  it("says it is only queueing when the class is admitted", () => {
    expect(waitingReason(job(), load())).toBe("waiting for a free slot");
  });

  it("explains a blocked job by its dependency, not by load", () => {
    // A blocked job is not waiting on the machine -- it is waiting on
    // another job. Saying "system busy" here would be a lie that sends
    // someone looking at their CPU.
    expect(waitingReason(job({ state: "blocked" }), load())).toBe(
      "waiting on an earlier step",
    );
  });
});

describe("jobLabel", () => {
  it("prefers the paired read names", () => {
    expect(
      jobLabel(job({ payload: { r1_name: "a.fastq", r2_name: "b.fastq" } })),
    ).toBe("a.fastq + b.fastq");
  });

  it("uses a single read name when there is no mate", () => {
    expect(jobLabel(job({ payload: { r1_name: "a.fastq" } }))).toBe("a.fastq");
  });

  it("falls back to the generic name key", () => {
    expect(jobLabel(job({ payload: { name: "ref.fna" } }))).toBe("ref.fna");
  });

  it("falls back to the job type when the payload names nothing", () => {
    expect(jobLabel(job({ type: "run_qc", payload: {} }))).toBe("run_qc");
  });

  it("ignores a non-string name rather than rendering it", () => {
    expect(jobLabel(job({ type: "run_qc", payload: { name: 42 } }))).toBe(
      "run_qc",
    );
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd frontend && npm test -- jobFormat
```

Expected: FAIL. The imports do not resolve — `RUNNING`, `WAITING`, `BLOCKED`,
`waitingReason` and `jobLabel` are not exported from `runFormat.ts`.

- [ ] **Step 3: Add the helpers to `runFormat.ts`**

Change the import line at the top of `frontend/src/lib/runFormat.ts` from:

```ts
import type { RunStatus, RunSummary } from "../api/types";
```

to:

```ts
import type { JobSummary, RunStatus, RunSummary, SystemLoad } from "../api/types";
```

Then append to the end of the file:

```ts
/**
 * What counts as in flight, split three ways because the three mean
 * different things to a person.
 *
 * `BLOCKED` is separate rather than folded into `WAITING` because the answer
 * to "why isn't this running" differs: a waiting job is queued behind the
 * machine, a blocked one is queued behind another job. The activity page can
 * derive "recent" by negating running and waiting because a blocked job is
 * shown inside its run's card there; a flat list has no such grouping, so it
 * must claim blocked positively or show it as though it had finished.
 */
export const RUNNING = new Set(["running"]);
export const WAITING = new Set(["pending", "queued", "delayed"]);
export const BLOCKED = new Set(["blocked"]);

/** True for any job that has not reached a terminal state. */
export function isInFlight(state: string): boolean {
  return RUNNING.has(state) || WAITING.has(state) || BLOCKED.has(state);
}

/**
 * A spinner says "wait"; this says what for. The governor's admitted_classes
 * is authoritative about whether this job's class can start at all.
 */
export function waitingReason(job: JobSummary, load?: SystemLoad): string {
  if (job.cancel_requested) return "cancelling";
  if (job.state === "delayed") return "retrying after a failure";
  if (job.state === "blocked") return "waiting on an earlier step";
  if (!load) return "waiting";
  if (!load.admitted_classes.includes(job.job_class)) {
    return load.state === "CLOSED"
      ? "waiting: system loaded"
      : "waiting: system busy";
  }
  return "waiting for a free slot";
}

/** The file a job is about, falling back to its type. */
export function jobLabel(job: JobSummary): string {
  const payload = job.payload as Record<string, unknown>;
  const name = payload.r1_name ?? payload.name;
  if (typeof name === "string" && name) {
    const mate = payload.r2_name;
    return typeof mate === "string" && mate ? `${name} + ${mate}` : name;
  }
  return job.type;
}
```

Note the one behaviour change from the original: the `blocked` branch in
`waitingReason` is new. Everything else is moved verbatim.

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd frontend && npm test -- jobFormat
```

Expected: PASS, 13 tests.

- [ ] **Step 5: Point `ActivityView.tsx` at the shared copies**

In `frontend/src/components/ActivityView.tsx`:

1. Delete these two lines near the top (around line 13-14):

```ts
const RUNNING = new Set(["running"]);
const WAITING = new Set(["pending", "queued", "delayed"]);
```

2. Delete the two private functions at the bottom of the file: the whole
   `function waitingReason(...)` block (including its docstring comment) and
   the whole `function jobLabel(...)` block (including its `/** The file a job
   is about... */` comment).

3. Extend the existing `runFormat` import. It currently reads:

```ts
import { ROLE_LABELS, STATUS_LABELS, runFacts } from "../lib/runFormat";
```

Change it to:

```ts
import {
  ROLE_LABELS,
  RUNNING,
  STATUS_LABELS,
  WAITING,
  jobLabel,
  runFacts,
  waitingReason,
} from "../lib/runFormat";
```

Leave every *use* of these symbols in the file exactly as it is. The desktop
view keeps deriving `recent` by negation — that is correct there, because
blocked jobs are grouped into their run's card.

- [ ] **Step 6: Typecheck and confirm the desktop view still builds**

```bash
cd frontend && npm run lint
```

Expected: no output, exit 0. If it reports `SystemLoad` is now an unused
import in `ActivityView.tsx`, leave it — it is still used by `GovernorNote`
and the `JobRow` props. If it genuinely is unused, remove it from that
import.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/runFormat.ts frontend/src/lib/jobFormat.test.ts frontend/src/components/ActivityView.tsx
git commit -m "Share the activity view's job helpers with lib/runFormat

The mobile feed needs the same state sets, waiting reasons and job labels.
jobLabel reads untyped payload keys, so a second copy would stop matching
silently the day a handler renames one.

Adds a BLOCKED set and a blocked branch in waitingReason: the desktop view
can derive 'recent' by negation because blocked jobs sit inside their run's
card, but a flat feed would show them as finished."
```

---

## Task 2: Viewport detection and the desktop override

**Files:**
- Create: `frontend/src/mobile/useIsMobile.ts`
- Create: `frontend/src/mobile/useIsMobile.test.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/mobile/useIsMobile.test.ts`:

```ts
import { describe, it, expect, beforeEach } from "vitest";
import { forceDesktop, setForceDesktop, MOBILE_QUERY } from "./useIsMobile";

/**
 * The hook itself needs a DOM to test and this repo has no jsdom setup, so
 * only the storage half is covered here. That is the half with a persistence
 * bug available to it; the matchMedia half is verified in a browser.
 */

class MemoryStorage {
  private m = new Map<string, string>();
  getItem(k: string) { return this.m.has(k) ? this.m.get(k)! : null; }
  setItem(k: string, v: string) { this.m.set(k, v); }
  removeItem(k: string) { this.m.delete(k); }
}

beforeEach(() => {
  (globalThis as { localStorage?: unknown }).localStorage = new MemoryStorage();
});

describe("MOBILE_QUERY", () => {
  it("matches the 600px breakpoint the design specifies", () => {
    expect(MOBILE_QUERY).toBe("(max-width: 600px)");
  });
});

describe("forceDesktop", () => {
  it("defaults to off", () => {
    expect(forceDesktop()).toBe(false);
  });

  it("round-trips through storage", () => {
    setForceDesktop(true);
    expect(forceDesktop()).toBe(true);
    setForceDesktop(false);
    expect(forceDesktop()).toBe(false);
  });

  it("clears the key rather than storing false", () => {
    // A stored "false" and an absent key must mean the same thing, so that
    // reading it can never depend on which one happens to be there.
    setForceDesktop(true);
    setForceDesktop(false);
    expect(localStorage.getItem("bioflow.forceDesktop")).toBe(null);
  });

  it("survives an unavailable localStorage rather than throwing", () => {
    // Safari private mode throws on setItem. A redirect helper that throws
    // takes the whole app down on first render.
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem() { throw new Error("denied"); },
      setItem() { throw new Error("denied"); },
      removeItem() { throw new Error("denied"); },
    };
    expect(() => setForceDesktop(true)).not.toThrow();
    expect(forceDesktop()).toBe(false);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd frontend && npm test -- useIsMobile
```

Expected: FAIL — `./useIsMobile` does not exist.

- [ ] **Step 3: Write the implementation**

Create `frontend/src/mobile/useIsMobile.ts`:

```ts
import { useEffect, useState } from "react";

/**
 * Whether this is a phone-sized viewport, and whether the user has asked us
 * to stop caring.
 *
 * The breakpoint is a viewport query rather than a user-agent sniff: a
 * desktop browser dragged narrow is genuinely a narrow viewport, and the
 * escape hatch exists for anyone who disagrees.
 */
export const MOBILE_QUERY = "(max-width: 600px)";

const FORCE_DESKTOP_KEY = "bioflow.forceDesktop";

/**
 * Storage access that cannot throw. Safari's private mode raises on
 * setItem, and a redirect helper that throws takes down the first render of
 * the whole app rather than just losing a preference.
 */
export function forceDesktop(): boolean {
  try {
    return localStorage.getItem(FORCE_DESKTOP_KEY) === "1";
  } catch {
    return false;
  }
}

export function setForceDesktop(on: boolean): void {
  try {
    if (on) localStorage.setItem(FORCE_DESKTOP_KEY, "1");
    // Cleared rather than set to "false", so an absent key and a stored one
    // cannot come to mean different things.
    else localStorage.removeItem(FORCE_DESKTOP_KEY);
  } catch {
    // A browser that will not persist the preference still works; it just
    // forgets the choice on reload.
  }
}

/**
 * Live viewport match. Subscribes rather than reading once, so rotating a
 * phone or dragging a window re-evaluates instead of staying fixed at
 * whatever it was on first render.
 */
export function useIsMobile(): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia(MOBILE_QUERY).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(MOBILE_QUERY);
    const onChange = (e: MediaQueryListEvent) => setMatches(e.matches);
    mq.addEventListener("change", onChange);
    // Re-read on mount: the query can have changed between the initial
    // useState and the effect running.
    setMatches(mq.matches);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return matches;
}
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd frontend && npm test -- useIsMobile
```

Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/mobile/useIsMobile.ts frontend/src/mobile/useIsMobile.test.ts
git commit -m "Add mobile viewport detection and the desktop override flag

Storage access is wrapped because Safari private mode throws on setItem,
and a redirect helper that throws takes down the first render."
```

---

## Task 3: Mobile stylesheet

Written before the components so they have classes to use. Self-contained
rather than added to `styles.css` (4,264 lines) or `broadsheet.css` (1,707),
both of which are written for a three-column desktop with a resizable panel.

**Files:**
- Create: `frontend/src/styles/mobile.css`

- [ ] **Step 1: Write the stylesheet**

Create `frontend/src/styles/mobile.css`:

```css
/*
 * Mobile-only styles, imported by MobileShell alone.
 *
 * Deliberately not part of styles.css or broadsheet.css: those describe a
 * three-column desktop with a resizable panel, and the point of the mobile
 * routes is that they do not share that layout. Keeping the sheets apart is
 * what stops a desktop tweak from moving a phone screen.
 *
 * Colours come from the variables the app already defines, so the mobile
 * view follows the active theme rather than inventing a second palette.
 */

.m-shell {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  min-height: 100dvh; /* excludes mobile browser chrome where supported */
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 15px;
  line-height: 1.4;
}

.m-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  background: var(--bg);
  z-index: 10;
}

.m-title { font-weight: 700; font-size: 17px; }

.m-desktop-link {
  font-size: 12px;
  color: var(--text-dim);
  text-decoration: underline;
  background: none;
  border: none;
  padding: 8px 4px; /* padding, not size, keeps the tap target at 44px */
  cursor: pointer;
}

.m-main { flex: 1; padding-bottom: 72px; }

/* Bottom tab bar */
.m-tabs {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  display: flex;
  border-top: 1px solid var(--border);
  background: var(--bg);
  padding-bottom: env(safe-area-inset-bottom, 0);
  z-index: 10;
}

.m-tab {
  flex: 1;
  text-align: center;
  padding: 14px 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-dim);
  text-decoration: none;
  min-height: 44px;
}

.m-tab.active { color: var(--accent); }

/* Sections and rows */
.m-section-head {
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-dim);
  padding: 16px 16px 8px;
  display: flex;
  justify-content: space-between;
}

.m-row {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.m-row-title { font-weight: 600; font-size: 14px; }
.m-row-sub { color: var(--text-dim); font-size: 12.5px; margin-top: 3px; }
.m-row-error { color: var(--error); font-size: 12.5px; margin-top: 3px; }

.m-empty { padding: 32px 16px; text-align: center; color: var(--text-dim); }

.m-progress {
  height: 5px;
  border-radius: 3px;
  background: var(--border);
  margin-top: 8px;
  overflow: hidden;
}
.m-progress > div { height: 100%; background: var(--accent); }

/* Forms */
.m-field { padding: 0 16px 12px; }
.m-label {
  display: block;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-dim);
  padding: 16px 0 6px;
}

.m-input,
.m-select {
  width: 100%;
  padding: 12px;
  font-size: 16px; /* below 16px, iOS Safari zooms the page on focus */
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
  color: var(--text);
  min-height: 44px;
}

.m-button {
  width: calc(100% - 32px);
  margin: 16px;
  padding: 14px;
  font-size: 15px;
  font-weight: 700;
  border: none;
  border-radius: 8px;
  background: var(--accent);
  color: var(--bg);
  min-height: 44px;
  cursor: pointer;
}

.m-button:disabled { opacity: 0.45; cursor: default; }

.m-button-ghost {
  background: none;
  border: 1px solid var(--border);
  color: var(--text);
}

/* Segmented control */
.m-segs {
  display: flex;
  margin: 12px 16px;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}

.m-seg {
  flex: 1;
  padding: 11px 8px;
  font-size: 13px;
  font-weight: 600;
  text-align: center;
  background: none;
  border: none;
  color: var(--text-dim);
  min-height: 44px;
  cursor: pointer;
}

.m-seg.active { background: var(--accent); color: var(--bg); }

/* Selectable rows */
.m-check-row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  width: 100%;
  text-align: left;
  background: none;
  border-left: none;
  border-right: none;
  border-top: none;
  color: inherit;
  font: inherit;
  min-height: 44px;
  cursor: pointer;
}

.m-check-row:disabled { opacity: 0.5; cursor: default; }

.m-check {
  width: 18px;
  height: 18px;
  flex: none;
  margin-top: 2px;
  border: 1.5px solid var(--accent);
  border-radius: 4px;
}

.m-check.on { background: var(--accent); }

.m-more {
  display: block;
  width: 100%;
  padding: 14px;
  text-align: center;
  font-size: 13px;
  color: var(--text-dim);
  background: none;
  border: none;
  border-bottom: 1px solid var(--border);
  min-height: 44px;
  cursor: pointer;
}

.m-suggestion {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  width: 100%;
  text-align: left;
  background: none;
  border-left: none;
  border-right: none;
  border-top: none;
  color: inherit;
  font: inherit;
  min-height: 44px;
  cursor: pointer;
}

.m-note { padding: 12px 16px; color: var(--text-dim); font-size: 12.5px; }
```

- [ ] **Step 2: Verify the theme variables actually exist**

The sheet uses `--bg`, `--text`, `--text-dim`, `--border`, `--accent` and
`--error`. These are the real names as of 2026-08-03 -- an earlier draft of
this plan used `--fg` and `--muted`, which do not exist and would have
rendered invisible text. Confirm before trusting them:

```bash
cd frontend && for v in bg text text-dim border accent error; do printf "%-12s " "--$v"; grep -c -- "--$v:" src/styles.css; done
```

Expected: a non-zero count for each. If any is zero, find the real name with
`grep -n '^\s*--' src/styles.css | head -40` and substitute it throughout
`mobile.css` before continuing. Do not invent a colour value.

Note `styles.css` defines these twice -- once for dark and again inside a
`prefers-color-scheme: light` block -- so using the variables rather than
literals is what makes the mobile view follow the active theme.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/styles/mobile.css
git commit -m "Add the mobile stylesheet

Self-contained rather than appended to styles.css or broadsheet.css, both of
which describe the three-column desktop the mobile routes deliberately do
not share. 16px inputs because iOS Safari zooms the page below that."
```

---

## Task 4: `MobileShell` and routing

**Files:**
- Create: `frontend/src/mobile/MobileShell.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Write `MobileShell`**

Create `frontend/src/mobile/MobileShell.tsx`:

```tsx
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { setForceDesktop } from "./useIsMobile";
import "../styles/mobile.css";

/**
 * The mobile chrome: a title bar and a two-item tab bar around whichever
 * route is showing.
 *
 * Separate from the desktop Shell rather than a variant of it, because that
 * shell carries a resizable splitter, the DetailPanel and the UploadTray --
 * none of which have a place on a phone, and all of which would need
 * conditionals threaded through them to pretend otherwise.
 */
export function MobileShell() {
  const navigate = useNavigate();

  const useDesktop = () => {
    setForceDesktop(true);
    navigate("/", { replace: true });
  };

  return (
    <div className="m-shell">
      <header className="m-header">
        <span className="m-title">BioFlow</span>
        <button className="m-desktop-link" onClick={useDesktop}>
          Use desktop version
        </button>
      </header>

      <main className="m-main">
        <Outlet />
      </main>

      <nav className="m-tabs">
        <NavLink
          to="/m/activity"
          className={({ isActive }) => `m-tab${isActive ? " active" : ""}`}
        >
          Activity
        </NavLink>
        <NavLink
          to="/m/download"
          className={({ isActive }) => `m-tab${isActive ? " active" : ""}`}
        >
          Download
        </NavLink>
      </nav>
    </div>
  );
}
```

- [ ] **Step 2: Add the routes and the redirect to `App.tsx`**

In `frontend/src/App.tsx`, add these imports alongside the existing ones:

```tsx
import { Navigate, Outlet, useNavigate } from "react-router-dom";
import { MobileShell } from "./mobile/MobileShell";
import { MobileActivity } from "./mobile/MobileActivity";
import { MobileDownload } from "./mobile/MobileDownload";
import { MobileConfirm } from "./mobile/MobileConfirm";
import { forceDesktop, useIsMobile } from "./mobile/useIsMobile";
```

`useNavigate` and `useLocation` may already be imported — merge rather than
duplicating the import line. `Route`, `Routes` and `BrowserRouter` are
already imported.

Then add this component above `Shell`:

```tsx
/**
 * Sends a narrow viewport to the mobile routes, once.
 *
 * One-directional on purpose. Redirecting a wide viewport back off /m/*
 * would make the "use desktop version" escape hatch impossible to use --
 * the moment it navigated to /, a still-narrow window would bounce it
 * straight back -- and would throw a tablet user out of the screen they
 * were reading the instant they rotated.
 */
function MobileRedirect() {
  const isMobile = useIsMobile();
  const { pathname } = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    if (!isMobile) return;
    if (forceDesktop()) return;
    if (pathname.startsWith("/m/")) return;
    navigate("/m/activity", { replace: true });
  }, [isMobile, pathname, navigate]);

  return null;
}
```

Now wire the routes. `Gate` currently ends (around `App.tsx:188-191`) with:

```tsx
  if (!ready) return null;
  if (!current) return <ProfilePicker />;
  return <Shell />;
}
```

Leave the first two lines exactly as they are — that is what keeps both
shells behind the same profile gate and the picker reused unchanged. Replace
only the `return <Shell />;` line with:

```tsx
  return (
    <>
      <MobileRedirect />
      <Routes>
        <Route path="/m" element={<MobileShell />}>
          <Route index element={<Navigate to="/m/activity" replace />} />
          <Route path="activity" element={<MobileActivity />} />
          <Route path="download" element={<MobileDownload />} />
          <Route path="download/:accession" element={<MobileConfirm />} />
        </Route>
        <Route path="*" element={<Shell />} />
      </Routes>
    </>
  );
```

The `path="*"` catch-all keeps every existing desktop route working
unchanged — `Shell` still owns its own `<Routes>` block.

- [ ] **Step 3: Add the "Mobile view" link to the desktop header**

In `frontend/src/components/Header.tsx`, import the setter:

```tsx
import { setForceDesktop } from "../mobile/useIsMobile";
```

Add a button in the header's right-hand area (beside the existing nav items —
match whatever wrapper element they sit in):

```tsx
<button
  className="header-mobile-link"
  onClick={() => {
    setForceDesktop(false);
    navigate("/m/activity");
  }}
  title="Switch to the phone view"
>
  Mobile view
</button>
```

`navigate` already exists in this component (`useNavigate` is imported at
line 2). Add a minimal style for it in `frontend/src/styles.css` near the
other `.header-*` rules:

```css
.header-mobile-link {
  background: none;
  border: none;
  color: var(--text-dim);
  font-size: 12px;
  cursor: pointer;
  text-decoration: underline;
}
```

- [ ] **Step 4: Create placeholder screens so the app compiles**

The three route components do not exist yet. Create each as a stub so this
task can be verified on its own; Tasks 5-7 fill them in.

`frontend/src/mobile/MobileActivity.tsx`:

```tsx
export function MobileActivity() {
  return <div className="m-empty">Activity</div>;
}
```

`frontend/src/mobile/MobileDownload.tsx`:

```tsx
export function MobileDownload() {
  return <div className="m-empty">Download</div>;
}
```

`frontend/src/mobile/MobileConfirm.tsx`:

```tsx
export function MobileConfirm() {
  return <div className="m-empty">Confirm</div>;
}
```

- [ ] **Step 5: Typecheck**

```bash
cd frontend && npm run lint
```

Expected: no output, exit 0.

- [ ] **Step 6: Verify in a browser**

```bash
./ops/worktree-up.sh
```

Open http://localhost:5273 and check, in devtools device emulation at 375px:

1. Loading `/` redirects to `/m/activity` and shows the tab bar.
2. Tapping the tabs moves between the two stubs; the active tab is highlighted.
3. "Use desktop version" lands on `/` and **stays there** at 375px.
4. Reloading at 375px still stays on the desktop view (the flag persisted).
5. The desktop header's "Mobile view" link returns to `/m/activity`, and a
   reload now redirects again (the flag cleared).
6. At a wide viewport, `/m/activity` still renders the mobile shell rather
   than bouncing to `/`.

Point 3 is the one worth being careful about — it is what the one-directional
rule exists for, and a two-way redirect passes points 1 and 2 while failing
this.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/mobile/ frontend/src/App.tsx frontend/src/components/Header.tsx frontend/src/styles.css
git commit -m "Add the mobile shell, routes and one-directional redirect

A narrow viewport is sent to /m/activity; a wide one is never sent back,
because a two-way redirect makes the desktop escape hatch unusable and
throws tablet users out of the screen they are reading on rotation."
```

---

## Task 5: The activity feed

**Files:**
- Modify: `frontend/src/mobile/MobileActivity.tsx`

- [ ] **Step 1: Write the feed**

Replace the whole contents of `frontend/src/mobile/MobileActivity.tsx`:

```tsx
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatDate, formatDuration } from "../lib/format";
import { RUNNING, isInFlight, jobLabel, waitingReason } from "../lib/runFormat";
import type { JobSummary, SystemLoad } from "../api/types";

/** How many finished jobs the feed carries. */
const RECENT_LIMIT = 15;

/**
 * What the machine is doing, on a phone.
 *
 * Flat rather than grouped by run, which is the whole reason this is cheap:
 * the desktop view fans `useQueries` out across every run to learn job
 * membership, up to 50 parallel requests. This asks for the job list and the
 * governor's state, and nothing else.
 *
 * Read-only in the strict sense -- nothing here is tappable. Cancelling,
 * retrying and log-reading all stay on the desktop.
 */
export function MobileActivity() {
  const { data: jobs = [], isLoading } = useQuery({
    queryKey: ["jobs", "mobile"],
    queryFn: () => api.listJobs({ limit: 50 }),
    // Poll only while something is in flight, matching the desktop rule, so
    // a phone sitting on a finished pipeline makes no requests at all.
    refetchInterval: (q) => {
      const list = q.state.data as JobSummary[] | undefined;
      return list?.some((j) => isInFlight(j.state)) ? 2000 : false;
    },
  });

  // A job does not carry its own waiting reason -- it is derived by checking
  // the job's class against the governor's admitted_classes. Without this,
  // waitingReason degrades to a bare "waiting", which is the uninformative
  // state the feed exists to avoid.
  const { data: load } = useQuery({
    queryKey: ["systemLoad", "mobile"],
    queryFn: api.systemLoad,
    refetchInterval: jobs.some((j) => isInFlight(j.state)) ? 2000 : false,
  });

  const active = jobs.filter((j) => isInFlight(j.state));
  const recent = jobs
    .filter((j) => !isInFlight(j.state))
    .slice(0, RECENT_LIMIT);

  if (isLoading) return <div className="m-empty">Loading…</div>;

  return (
    <>
      <div className="m-section-head">
        <span>In progress</span>
        <span>{active.length}</span>
      </div>
      {active.length === 0 ? (
        <div className="m-empty">Nothing running.</div>
      ) : (
        active.map((job) => <ActiveRow key={job.id} job={job} load={load} />)
      )}

      <div className="m-section-head">
        <span>Recent</span>
      </div>
      {recent.length === 0 ? (
        <div className="m-empty">No finished jobs yet.</div>
      ) : (
        recent.map((job) => <RecentRow key={job.id} job={job} />)
      )}
    </>
  );
}

function ActiveRow({ job, load }: { job: JobSummary; load?: SystemLoad }) {
  const running = RUNNING.has(job.state);
  const pct = job.progress?.pct ?? 0;

  return (
    <div className="m-row">
      <div className="m-row-title">{jobLabel(job)}</div>
      <div className="m-row-sub">
        {job.type}
        {running
          ? job.progress?.message
            ? ` · ${job.progress.message}`
            : " · running"
          : ` · ${waitingReason(job, load)}`}
      </div>
      {running && pct > 0 && (
        <div className="m-progress">
          <div style={{ width: `${Math.min(pct, 100)}%` }} />
        </div>
      )}
    </div>
  );
}

function RecentRow({ job }: { job: JobSummary }) {
  const ok = job.state === "succeeded";
  const took =
    job.timing?.duration_ms != null
      ? ` · ${formatDuration(job.timing.duration_ms)}`
      : "";

  return (
    <div className="m-row">
      <div className="m-row-title">
        {jobLabel(job)} {ok ? "✓" : "✗"}
      </div>
      <div className="m-row-sub">
        {job.type} · {formatDate(job.created_at)}
        {took}
      </div>
      {job.error && (
        <div className="m-row-error">{job.error.message}</div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Confirm `formatDuration` takes milliseconds**

```bash
cd frontend && grep -n "export function formatDuration" -A 4 src/lib/format.ts
```

The desktop calls it both as `formatDuration(elapsed)` and
`formatDuration(job.timing.duration_ms)`, so it takes ms. If the signature
says otherwise, adjust the call above to match — do not change `format.ts`.

- [ ] **Step 3: Typecheck**

```bash
cd frontend && npm run lint
```

Expected: no output, exit 0.

- [ ] **Step 4: Verify in a browser against real jobs**

With `./ops/worktree-up.sh` running, open http://localhost:5273/m/activity at
375px. Then launch something from the desktop UI on port 5273 in another tab
(a QC run is quick) and confirm:

1. The job appears under "In progress" while it runs, with its filename as
   the title rather than the bare job type.
2. A progress bar appears if the job reports progress.
3. It moves to "Recent" with a ✓ and a duration when it finishes.
4. Once everything is idle, the network tab shows the polling **stop** — no
   requests every 2s forever.
5. A failed job shows its error message.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/mobile/MobileActivity.tsx
git commit -m "Add the mobile activity feed

Flat rather than run-grouped: the desktop view fans useQueries across every
run to learn job membership, and none of that is needed to answer 'what is
it doing'. Polls only while something is in flight."
```

---

## Task 6: Download — project picker, search, segmented results

**Files:**
- Create: `frontend/src/mobile/downloadStore.ts`
- Modify: `frontend/src/mobile/MobileDownload.tsx`

- [ ] **Step 1: Write the handoff store**

The confirm screen needs the resolved payload, and re-resolving on that route
would double every NCBI call. Create
`frontend/src/mobile/downloadStore.ts`:

```ts
import { create } from "zustand";
import type {
  AssemblyResolveResponse,
  SraResolveResponse,
} from "../api/types";

/**
 * The resolved accession, handed from the search screen to the confirm
 * screen.
 *
 * In a store rather than route state because `ncbiResolve` is a real network
 * call against NCBI: re-resolving on the confirm route would double every
 * lookup, and a reload there would fire a third. The confirm screen treats
 * an empty store as "go back" rather than resolving for itself.
 */
interface DownloadState {
  projectId: string | null;
  sra: SraResolveResponse | null;
  assembly: AssemblyResolveResponse | null;
  setProject: (id: string) => void;
  setResolved: (r: {
    sra: SraResolveResponse | null;
    assembly: AssemblyResolveResponse | null;
  }) => void;
}

const LAST_PROJECT_KEY = "bioflow.lastProject";

function rememberedProject(): string | null {
  try {
    return localStorage.getItem(LAST_PROJECT_KEY);
  } catch {
    return null;
  }
}

export const useDownloadStore = create<DownloadState>((set) => ({
  projectId: rememberedProject(),
  sra: null,
  assembly: null,
  setProject: (projectId) => {
    try {
      localStorage.setItem(LAST_PROJECT_KEY, projectId);
    } catch {
      // Not persisting the choice is survivable; it just asks again.
    }
    set({ projectId });
  },
  // Only ever one branch: ncbiResolve returns an assembly or a run list,
  // never both, and leaving a stale one beside a fresh one would show two
  // answers to one lookup.
  setResolved: ({ sra, assembly }) => set({ sra, assembly }),
}));
```

- [ ] **Step 2: Write the search screen**

Replace the whole contents of `frontend/src/mobile/MobileDownload.tsx`:

```tsx
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import { useDownloadStore } from "./downloadStore";
import type {
  OrganismAssemblySummary,
  OrganismSearchResponse,
  OrganismSuggestion,
  SraRunInfo,
} from "../api/types";

/** Matches the desktop dialog's page size. */
const PAGE_SIZE = 20;

/**
 * Queue an NCBI download from a phone.
 *
 * The project picker is first because both download endpoints require a
 * project_id, and unlike the desktop dialog -- which opens from inside a
 * project -- there is no explorer here to have supplied one.
 *
 * Organism search is the reason this screen is worth having. Away from your
 * desk is exactly where you do not have an accession in front of you.
 */
export function MobileDownload() {
  const navigate = useNavigate();
  const projectId = useDownloadStore((s) => s.projectId);
  const setProject = useDownloadStore((s) => s.setProject);
  const setResolved = useDownloadStore((s) => s.setResolved);

  const [query, setQuery] = useState("");
  const [organism, setOrganism] = useState<OrganismSuggestion | null>(null);
  const [results, setResults] = useState<OrganismSearchResponse | null>(null);
  const [tab, setTab] = useState<"assemblies" | "sra">("assemblies");

  const { data: projects = [] } = useQuery({
    queryKey: ["projects", "mobile"],
    queryFn: () => api.listProjects(),
  });

  const { data: suggestions } = useQuery({
    queryKey: ["organismSuggest", query],
    queryFn: () => api.ncbiOrganismSuggest(query),
    // Two characters is not a search, it is every organism on earth.
    enabled: query.trim().length >= 3 && !organism,
  });

  const search = useMutation({
    mutationFn: (vars: {
      org: OrganismSuggestion;
      section: "both" | "assemblies" | "sra";
      assemblyPageToken?: string | null;
      sraOffset?: number;
    }) =>
      api.ncbiOrganismSearch({
        tax_id: vars.org.tax_id,
        sci_name: vars.org.sci_name,
        project_id: projectId,
        assembly_page_token: vars.assemblyPageToken ?? null,
        sra_offset: vars.sraOffset ?? 0,
        page_size: PAGE_SIZE,
        section: vars.section,
      }),
    onSuccess: (data, vars) => {
      // Paging one list must not discard the other: the API returns only the
      // requested section, so merge rather than replace.
      setResults((prev) =>
        !prev || vars.section === "both"
          ? data
          : vars.section === "assemblies"
            ? {
                ...prev,
                assemblies: [...prev.assemblies, ...data.assemblies],
                assemblies_next_page_token: data.assemblies_next_page_token,
              }
            : {
                ...prev,
                sra_runs: [...prev.sra_runs, ...data.sra_runs],
                sra_next_offset: data.sra_next_offset,
              },
      );
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const resolve = useMutation({
    mutationFn: (accession: string) =>
      api.ncbiResolve({ accession, project_id: projectId }),
    onSuccess: (data, accession) => {
      if (!data.sra && !data.assembly) {
        notify.error(`Nothing found for ${accession}`);
        return;
      }
      setResolved({ sra: data.sra, assembly: data.assembly });
      navigate(`/m/download/${encodeURIComponent(accession)}`);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const pickOrganism = (o: OrganismSuggestion) => {
    setOrganism(o);
    setQuery(o.sci_name);
    setResults(null);
    setTab("assemblies");
    search.mutate({ org: o, section: "both" });
  };

  const restart = () => {
    setOrganism(null);
    setResults(null);
    setQuery("");
  };

  const looksLikeAccession = /^(SRR|SRX|SRS|SRP|PRJ|GCF_|GCA_|ERR|DRR)/i.test(
    query.trim(),
  );

  return (
    <>
      <label className="m-label" style={{ padding: "16px 16px 6px" }}>
        Into project
      </label>
      <div className="m-field">
        <select
          className="m-select"
          value={projectId ?? ""}
          onChange={(e) => setProject(e.target.value)}
        >
          <option value="" disabled>
            Choose a project…
          </option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>

      <label className="m-label" style={{ padding: "8px 16px 6px" }}>
        Organism or accession
      </label>
      <div className="m-field">
        <input
          className="m-input"
          value={query}
          placeholder="e.g. Escherichia coli, or SRR2584863"
          onChange={(e) => {
            setQuery(e.target.value);
            if (organism) restart();
          }}
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
        />
      </div>

      {looksLikeAccession && (
        <button
          className="m-button"
          disabled={!projectId || resolve.isPending}
          onClick={() => resolve.mutate(query.trim())}
        >
          {resolve.isPending ? "Looking up…" : `Look up ${query.trim()}`}
        </button>
      )}

      {!organism &&
        !looksLikeAccession &&
        suggestions?.suggestions?.map((o) => (
          <button
            key={o.tax_id}
            className="m-suggestion"
            onClick={() => pickOrganism(o)}
          >
            <div className="m-row-title">{o.sci_name}</div>
            <div className="m-row-sub">
              taxon {o.tax_id}
              {o.common_name ? ` · ${o.common_name}` : ""}
            </div>
          </button>
        ))}

      {search.isPending && !results && (
        <div className="m-empty">Searching…</div>
      )}

      {results && (
        <>
          <div className="m-segs">
            <button
              className={`m-seg${tab === "assemblies" ? " active" : ""}`}
              onClick={() => setTab("assemblies")}
            >
              Assemblies {results.assemblies.length}
            </button>
            <button
              className={`m-seg${tab === "sra" ? " active" : ""}`}
              onClick={() => setTab("sra")}
            >
              Runs {results.sra_total_count}
            </button>
          </div>

          {tab === "assemblies" ? (
            <>
              {results.assemblies.length === 0 && (
                <div className="m-empty">No assemblies for this organism.</div>
              )}
              {results.assemblies.map((a) => (
                <AssemblyRow
                  key={a.accession ?? Math.random()}
                  a={a}
                  disabled={!projectId || resolve.isPending}
                  onPick={() => a.accession && resolve.mutate(a.accession)}
                />
              ))}
              {results.assemblies_next_page_token && organism && (
                <button
                  className="m-more"
                  disabled={search.isPending}
                  onClick={() =>
                    search.mutate({
                      org: organism,
                      section: "assemblies",
                      assemblyPageToken: results.assemblies_next_page_token,
                    })
                  }
                >
                  {search.isPending ? "Loading…" : "Load more"}
                </button>
              )}
            </>
          ) : (
            <>
              {results.sra_runs.length === 0 && (
                <div className="m-empty">No sequencing runs found.</div>
              )}
              {results.sra_runs.map((r) => (
                <RunRow
                  key={r.accession}
                  r={r}
                  disabled={!projectId || resolve.isPending}
                  onPick={() => resolve.mutate(r.accession)}
                />
              ))}
              {results.sra_next_offset != null && organism && (
                <button
                  className="m-more"
                  disabled={search.isPending}
                  onClick={() =>
                    search.mutate({
                      org: organism,
                      section: "sra",
                      sraOffset: results.sra_next_offset ?? 0,
                    })
                  }
                >
                  {search.isPending ? "Loading…" : "Load more"}
                </button>
              )}
            </>
          )}
        </>
      )}

      {!projectId && (
        <div className="m-note">Choose a project before downloading.</div>
      )}
    </>
  );
}

function AssemblyRow({
  a,
  disabled,
  onPick,
}: {
  a: OrganismAssemblySummary;
  disabled: boolean;
  onPick: () => void;
}) {
  return (
    <button className="m-check-row" disabled={disabled} onClick={onPick}>
      <div>
        <div className="m-row-title">{a.accession ?? "unknown"}</div>
        <div className="m-row-sub">
          {[
            a.strain ?? a.assembly_name,
            a.assembly_level,
            a.refseq_category,
            a.already_downloaded ? "already in library" : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
      </div>
    </button>
  );
}

function RunRow({
  r,
  disabled,
  onPick,
}: {
  r: SraRunInfo;
  disabled: boolean;
  onPick: () => void;
}) {
  return (
    <button className="m-check-row" disabled={disabled} onClick={onPick}>
      <div>
        <div className="m-row-title">{r.accession}</div>
        <div className="m-row-sub">
          {[
            r.platform,
            r.bytes ? `${(r.bytes / 1e9).toFixed(1)} GB` : null,
            r.already_downloaded ? "already in library" : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
      </div>
    </button>
  );
}
```

- [ ] **Step 3: Check the two response shapes this assumes**

Two things above are assumptions worth verifying rather than discovering at
runtime:

```bash
cd frontend && sed -n "/export interface OrganismSuggestResponse/,/^}/p" src/api/types.ts
cd frontend && grep -n "export interface Project\b" -A 8 src/api/types.ts
```

The code reads `suggestions.suggestions` and `p.name` / `p.id`. If either
field is named differently, fix the call site to match the type — do not
change the types.

- [ ] **Step 4: Typecheck**

```bash
cd frontend && npm run lint
```

Expected: no output, exit 0.

- [ ] **Step 5: Verify in a browser**

At http://localhost:5273/m/download, 375px:

1. The project dropdown lists real projects; choosing one and reloading the
   page keeps it selected.
2. Typing `escherichia` shows organism suggestions after three characters.
3. Picking one shows the segmented control with real counts on both tabs.
4. Switching tabs shows each list; "Load more" on one tab does **not** empty
   the other tab's list when you switch back. That merge is the part most
   likely to be wrong.
5. Typing `SRR2584863` hides the suggestions and shows a "Look up" button
   instead.
6. Tapping any row navigates to `/m/download/<accession>` (the stub).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/mobile/downloadStore.ts frontend/src/mobile/MobileDownload.tsx
git commit -m "Add the mobile download search screen

Project first, since both download endpoints need a project_id that a phone
has no explorer to have supplied. Organism results use a segmented toggle
mapped onto the API's own section parameter, so paging one list never
refetches the other."
```

---

## Task 7: Download — confirm and launch

**Files:**
- Modify: `frontend/src/mobile/MobileConfirm.tsx`

- [ ] **Step 1: Write the confirm screen**

Replace the whole contents of `frontend/src/mobile/MobileConfirm.tsx`:

```tsx
import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import { useDownloadStore } from "./downloadStore";

/**
 * The last screen before something is queued.
 *
 * Two branches, because ncbiResolve returns an assembly or a run list and
 * never both. Runs are a checklist; an assembly is its component set. Both
 * preselect exactly what the desktop dialog preselects -- a phone that
 * quietly downloaded less than the desktop would is how a missing GTF turns
 * up weeks later as "why can't I quantify this".
 */
export function MobileConfirm() {
  const { accession = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const projectId = useDownloadStore((s) => s.projectId);
  const sra = useDownloadStore((s) => s.sra);
  const assembly = useDownloadStore((s) => s.assembly);

  const [runs, setRuns] = useState<Set<string>>(new Set());
  const [components, setComponents] = useState<Set<string>>(new Set());
  const [runQC, setRunQC] = useState(true);

  // Preselect on arrival, matching the desktop defaults: every run not
  // already held, and every available component.
  useEffect(() => {
    if (sra) {
      setRuns(
        new Set(
          sra.runs.filter((r) => !r.already_downloaded).map((r) => r.accession),
        ),
      );
    }
    if (assembly) {
      setComponents(
        new Set(assembly.components.filter((c) => c.available).map((c) => c.key)),
      );
    }
  }, [sra, assembly]);

  const done = (message: string) => {
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["runs"] });
    notify.success(message);
    navigate("/m/activity");
  };

  const downloadRuns = useMutation({
    mutationFn: () =>
      api.sraDownload({
        project_id: projectId!,
        run_accessions: [...runs],
        run_qc: runQC,
      }),
    onSuccess: (accepted) => {
      const n = accepted.download_job_ids.length;
      done(`Downloading ${n} ${n === 1 ? "run" : "runs"}`);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const downloadAssembly = useMutation({
    mutationFn: () =>
      api.ncbiDownloadAssembly({
        project_id: projectId!,
        accession: assembly!.accession,
        components: [...components],
      }),
    onSuccess: () => done(`Downloading ${assembly!.accession}`),
    onError: (e: Error) => notify.error(e.message),
  });

  // A reload lands here with an empty store. Resolving again would be a
  // second NCBI call for a screen the user can simply re-enter, so this
  // sends them back rather than silently re-fetching.
  if (!sra && !assembly) {
    return (
      <>
        <div className="m-empty">
          Nothing loaded for {accession}. Start from the search screen.
        </div>
        <button className="m-button" onClick={() => navigate("/m/download")}>
          Back to search
        </button>
      </>
    );
  }

  const toggle = (set: Set<string>, key: string) => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return next;
  };

  if (assembly) {
    return (
      <>
        <div className="m-section-head">
          <span>{assembly.accession}</span>
        </div>
        <div className="m-note">
          {[assembly.organism, assembly.strain, assembly.assembly_level]
            .filter(Boolean)
            .join(" · ")}
        </div>

        <div className="m-section-head">
          <span>Files to download</span>
        </div>
        {assembly.components.map((c) => (
          <button
            key={c.key}
            className="m-check-row"
            disabled={!c.available}
            onClick={() => setComponents((s) => toggle(s, c.key))}
          >
            <span
              className={`m-check${components.has(c.key) ? " on" : ""}`}
            />
            <div>
              <div className="m-row-title">{c.label}</div>
              <div className="m-row-sub">
                {c.available
                  ? c.size_bytes
                    ? `${(c.size_bytes / 1e6).toFixed(1)} MB`
                    : "available"
                  : (c.reason ?? "not available")}
              </div>
            </div>
          </button>
        ))}

        <button
          className="m-button"
          disabled={components.size === 0 || downloadAssembly.isPending}
          onClick={() => downloadAssembly.mutate()}
        >
          {downloadAssembly.isPending
            ? "Queueing…"
            : `Download ${components.size} ${components.size === 1 ? "file" : "files"}`}
        </button>
      </>
    );
  }

  return (
    <>
      <div className="m-section-head">
        <span>{sra!.accession}</span>
        <span>{sra!.total_run_count} runs</span>
      </div>
      {sra!.title && <div className="m-note">{sra!.title}</div>}

      {sra!.truncated && (
        <div className="m-note">
          Showing the first {sra!.runs.length} runs of this study. Use the
          desktop view to reach the rest.
        </div>
      )}

      {sra!.runs.map((r) => (
        <button
          key={r.accession}
          className="m-check-row"
          disabled={r.already_downloaded}
          onClick={() => setRuns((s) => toggle(s, r.accession))}
        >
          <span className={`m-check${runs.has(r.accession) ? " on" : ""}`} />
          <div>
            <div className="m-row-title">{r.accession}</div>
            <div className="m-row-sub">
              {[
                r.platform,
                r.bytes ? `${(r.bytes / 1e9).toFixed(1)} GB` : null,
                r.already_downloaded ? "already in library" : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </div>
          </div>
        </button>
      ))}

      <button
        className="m-check-row"
        onClick={() => setRunQC((v) => !v)}
        style={{ marginTop: 8 }}
      >
        <span className={`m-check${runQC ? " on" : ""}`} />
        <div>
          <div className="m-row-title">Run QC after downloading</div>
          <div className="m-row-sub">
            There is no way to turn this on later.
          </div>
        </div>
      </button>

      <button
        className="m-button"
        disabled={runs.size === 0 || downloadRuns.isPending}
        onClick={() => downloadRuns.mutate()}
      >
        {downloadRuns.isPending
          ? "Queueing…"
          : `Download ${runs.size} ${runs.size === 1 ? "run" : "runs"}`}
      </button>
    </>
  );
}
```

- [ ] **Step 2: Typecheck**

```bash
cd frontend && npm run lint
```

Expected: no output, exit 0.

- [ ] **Step 3: Verify both branches end to end in a browser**

This is the real verification for the whole feature. At 375px on
http://localhost:5273:

**Runs branch:**
1. Search an organism, open the Runs tab, tap a run.
2. Its checklist appears with un-downloaded runs preselected and any
   already-held run greyed out and unselectable.
3. Tap "Download N runs". It lands on `/m/activity` with the job visible in
   "In progress".

**Assembly branch:**
4. Open the Assemblies tab, tap an assembly (or search `GCF_000005845.2`
   directly).
5. Its components appear with every available one checked and unavailable
   ones disabled showing their reason.
6. Unchecking one lowers the button's count. Downloading queues the job and
   lands on the feed.

**Reload behaviour:**
7. Reload the page while on a confirm screen. It shows "Nothing loaded" and
   a "Back to search" button rather than crashing or silently re-resolving.

- [ ] **Step 4: Run the full frontend suite and the backend suite**

```bash
cd frontend && npm test
```

Expected: all tests pass, including the pre-existing `HelpSoftware` ones.

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: the full suite green. Read the **count**, not the exit code — per
CLAUDE.md, exit 0 is not evidence on its own. No backend file was touched, so
this should match main.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/mobile/MobileConfirm.tsx
git commit -m "Add the mobile download confirm screen

Runs get a checklist, assemblies get their component set, both preselecting
exactly what the desktop dialog does. A reload lands with an empty store and
is sent back to search rather than firing a second NCBI resolve."
```

---

## Task 8: Close out the TODO entry

Per CLAUDE.md, finishing the work is not finishing the entry — this has
already gone wrong four times in this repo.

**Files:**
- Modify: `docs/TODO.md`
- Modify: `docs/TODO-done.md`

- [ ] **Step 1: Move the entry**

Cut the whole `## Mobile-friendly view for select features` entry from
`docs/TODO.md` — heading and full body — and paste it into the
`# Planned features` section of `docs/TODO-done.md`.

Change its heading to:

```markdown
## Mobile-friendly view for select features — FIXED
```

And insert this note directly under the heading, above the original body:

```markdown
Shipped 2026-08-03. Design:
`docs/superpowers/specs/2026-08-03-mobile-view-design.md`, plan:
`docs/superpowers/plans/2026-08-03-mobile-view.md`.

Where the code lives: `frontend/src/mobile/` (`MobileShell`,
`MobileActivity`, `MobileDownload`, `MobileConfirm`, `useIsMobile`,
`downloadStore`), `frontend/src/styles/mobile.css`, plus the `/m/*` routes
and the redirect in `App.tsx`.

**What the implementation did differently from this entry.**

- **The redirect is one-directional, which this entry does not consider.**
  It proposes detection at ~600px and stops there. Redirecting a wide
  viewport back off `/m/*` makes the "use desktop version" escape hatch
  impossible to use -- the moment it navigates to `/`, a still-narrow
  window bounces it straight back -- and throws a tablet user out of the
  screen they are reading the instant they rotate. Narrow viewports are
  sent to `/m/activity`; wide ones are never sent back.
- **The activity feed is flat, not a port of the Activity tab.** This entry
  names the Activity tab as "a reasonable foundation". Its structure is the
  expensive part: `ActivityView` fans `useQueries` out across every run to
  learn job membership, up to 50 parallel requests, purely to nest jobs
  under runs. A flat feed answers "what is it doing" with one `listJobs`
  and one `systemLoad` call.
- **`systemLoad` was not anticipated and is required.** A job does not
  carry its own waiting reason: `waitingReason` derives it by checking the
  job's class against the governor's `admitted_classes`. Without that call
  it degrades to a bare "waiting", which is the uninformative state the
  section exists to avoid.
- **A `blocked` job would have shown as finished.** The desktop view
  derives "recent" by negating running and waiting, which is safe there
  because blocked jobs render inside their run's card. A flat list has no
  such grouping, so `blocked` would have landed under "Recent" looking
  successful. `lib/runFormat.ts` now carries a `BLOCKED` set and
  `waitingReason` answers "waiting on an earlier step" rather than blaming
  system load for a dependency wait.
- **The download flow needed a project picker this entry does not mention.**
  Both download endpoints require a `project_id`, and the desktop dialog
  gets it ambiently by opening from inside a project. A phone has no
  explorer to have been in, so project selection became the first field,
  remembered in `localStorage` between visits.
- **Four helpers were extracted, not written twice.** `RUNNING`, `WAITING`,
  `waitingReason` and `jobLabel` moved from `ActivityView.tsx` to
  `lib/runFormat.ts`. `jobLabel` is the one that most needed it: it reads
  untyped payload keys (`r1_name`, `r2_name`, `name`), so a second copy
  would have stopped matching silently the day a handler renamed one. This
  is the concrete answer to the two-parallel-UIs drift risk the entry
  raises.
- **Deliberately dropped from the desktop download dialog:** platform
  filter, assembly-level filter, run-table sorting, and multi-select
  spanning pages. Those interrogate a large study; the mobile case is
  queueing something you already came for. A truncated study says so and
  points at the desktop view.
- **No backend changes.** All seven endpoints already existed.
```

- [ ] **Step 2: Verify `docs/TODO.md` no longer carries the entry**

```bash
grep -n "Mobile-friendly" docs/TODO.md docs/TODO-done.md
```

Expected: hits in `docs/TODO-done.md` only.

- [ ] **Step 3: Commit**

```bash
git add docs/TODO.md docs/TODO-done.md
git commit -m "Close out the mobile-friendly view TODO entry

Moved to TODO-done.md with a note on where the code lives and the six
places the implementation departed from the entry -- most importantly the
one-directional redirect and the blocked-job handling, neither of which the
entry anticipated."
```

---

## Task 9: Merge and push

Per CLAUDE.md: once the suite is green and `main` is clean, merge and push
without asking.

- [ ] **Step 1: Confirm both suites are green**

```bash
cd frontend && npm test && npm run lint
```

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Read the counts. A red or unrun suite is the one thing that stops here.

- [ ] **Step 2: Confirm `main` is clean and merge**

```bash
git -C /Users/syntheticgio/Programming/local-bio-pipeliner status --short
```

Expected: empty. If `main` has moved, merge it into this branch and re-run
both suites before continuing — a green from before the merge does not
still hold.

```bash
git -C /Users/syntheticgio/Programming/local-bio-pipeliner merge --no-ff claude/repo-docs-todos-bugs-0b9ce5
```

- [ ] **Step 3: Push**

```bash
git -C /Users/syntheticgio/Programming/local-bio-pipeliner push origin main
```

- [ ] **Step 4: Point the running stack back at main**

Only needed if anything repointed it during this work. Check:

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

If any path is under `.claude/worktrees/`, restore it from the main checkout
root:

```bash
docker compose up -d --build api web worker
```

And bring the worktree stack down:

```bash
./ops/worktree-up.sh --down
```

---

## Self-review notes

Checked against `docs/superpowers/specs/2026-08-03-mobile-view-design.md`:

| Spec section | Task |
|---|---|
| Routing table, `MobileShell` | 4 |
| Detection, one-directional redirect, escape hatch | 2, 4 |
| Profile gate reuse (`Gate` unchanged) | 4 |
| Activity feed, two calls, polling rule | 5 |
| Download step 1 (project) | 6 |
| Download step 2 (search, segmented toggle) | 6 |
| Download step 3 (confirm, both branches) | 7 |
| Files created/modified | 3, 4, 5, 6, 7 |
| Shared code extraction (4 symbols) | 1 |
| Styling, self-contained sheet | 3 |
| No backend changes | verified in 7 step 4 |
| Testing approach | 1, 2 (vitest); 4-7 (browser) |

**One thing this plan adds that the spec did not have:** the `BLOCKED` state
set and the blocked branch in `waitingReason` (Task 1). `JobState` includes
`"blocked"`, and the spec's flat-feed design would have shown such jobs under
"Recent" as though they had finished, because the desktop's negation-derived
"recent" is only safe when blocked jobs are grouped inside a run card. Worth
carrying back into the spec if it is revised.
