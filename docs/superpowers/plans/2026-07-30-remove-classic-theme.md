# Remove the Classic Theme Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Broadsheet the app's only appearance by deleting the theme
toggle, the persisted choice, and the Classic code path.

**Architecture:** The `theme-broadsheet` class moves from runtime JS onto the
static `<html>` tag in `index.html`. That class is what all 155 Broadsheet CSS
rules are scoped to, so making it permanent leaves the whole CSS layer working
untouched, and it lands before first paint by construction -- the same flash
prevention `applyTheme` gave us, without the JS. Neither stylesheet is edited.

**Tech Stack:** React 18 + TypeScript, Vite, zustand (the store being deleted),
react-router-dom. Docker Compose for running the app.

**Spec:** `docs/superpowers/specs/2026-07-30-remove-classic-theme-design.md`

---

## Notes for the implementer

**There are no tests in this plan, and that is deliberate.** This repo has no
headless component-testing setup -- no jsdom, no testing-library, zero
`.test.tsx` files -- and `CLAUDE.md` states none is expected. Manual testing in
the browser at localhost:5173 is the actual verification step for UI-facing
work here. Do not add a test framework to satisfy TDD habits; that would be a
much larger change than the one requested. The verification steps below are
`grep` and `tsc` (which *are* automated and *do* fail loudly) plus a browser
pass in the final task.

**Never run `docker compose` from this worktree.** The bind mounts in
`docker-compose.override.yml` are relative paths, so Compose resolves them
against wherever it was invoked while the project name stays pinned to
`biopipe`. Running it here silently repoints *the* stack at this branch with no
error and no warning. Always `cd` to the main repo root first:
`/Users/syntheticgio/Programming/local-bio-pipeliner`.

**Order matters.** Task 1 adds the class before Task 2 removes the JS that
applies it. Doing it the other way around leaves a window where the app renders
unstyled Classic. Follow the task order.

## File Structure

| File | Responsibility after this change |
|---|---|
| `frontend/index.html` | Declares the permanent `theme-broadsheet` class |
| `frontend/src/main.tsx` | Mounts React, imports both stylesheets. No theme logic |
| `frontend/src/components/Header.tsx` | Brand, nav, File + Help menus. No View menu |
| `frontend/src/stores/themeStore.ts` | **deleted** |
| `frontend/src/fonts/README.md` | Font provenance, with no live reference to Classic |

Unchanged: `styles.css`, `styles/broadsheet.css`, the vendored fonts.

---

### Task 1: Pin the Broadsheet class to `<html>`

**Files:**
- Modify: `frontend/index.html:2`

- [ ] **Step 1: Add the class**

Replace line 2 of `frontend/index.html`:

```html
<html lang="en">
```

with:

```html
<html lang="en" class="theme-broadsheet">
```

- [ ] **Step 2: Verify the app still renders in Broadsheet**

At this point `applyTheme` still runs, and it calls `classList.toggle` with a
boolean second argument, so it either re-adds the class (saved Broadsheet) or
removes it (saved Classic). Both stylesheets are still imported. The app must
not be visibly broken.

From the **main repo root**, not this worktree:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

Open localhost:5173. Expected: the app loads and is usable. If your saved
theme was Classic it may still look Classic -- that is correct for this
intermediate step and Task 2 fixes it.

- [ ] **Step 3: Commit**

```bash
git add frontend/index.html
git commit -m "feat: pin the Broadsheet class to <html>"
```

---

### Task 2: Delete the theme store and its call site

**Files:**
- Delete: `frontend/src/stores/themeStore.ts`
- Modify: `frontend/src/main.tsx`

- [ ] **Step 1: Delete the store**

```bash
git rm frontend/src/stores/themeStore.ts
```

- [ ] **Step 2: Rewrite `main.tsx`**

Replace the entire contents of `frontend/src/main.tsx` with:

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import "./styles.css";
import "./styles/broadsheet.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

Both CSS imports stay. `broadsheet.css` is an override layer that depends on
`styles.css` for all structural rules -- dropping either one breaks the app.
The comment about applying the class before first paint goes away with the code
it described; `index.html` now does that job.

- [ ] **Step 3: Confirm the build fails on `Header.tsx` only**

`Header.tsx` still imports the deleted store, so this is expected to fail, and
the failure tells you the next task's target is real.

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/busy-meninsky-e73b29/frontend && npx tsc --noEmit
```

Expected: an error on `frontend/src/components/Header.tsx:6` along the lines of
`Cannot find module '../stores/themeStore'`. Expected: **no** error mentioning
`main.tsx`. If `main.tsx` appears in the output, fix it before continuing.

- [ ] **Step 4: Do not commit yet**

The tree does not typecheck until Task 3. Leave these changes staged and
uncommitted; Task 3 commits them together as one working change.

---

### Task 3: Remove the View menu from the header

**Files:**
- Modify: `frontend/src/components/Header.tsx`

- [ ] **Step 1: Drop the store import**

Delete line 6 of `frontend/src/components/Header.tsx`:

```tsx
import { useThemeStore } from "../stores/themeStore";
```

Leave the other imports on lines 1-8 alone.

- [ ] **Step 2: Drop the two selectors**

Delete these two lines (around line 31-32) and the blank line that follows
them:

```tsx
  const theme = useThemeStore((s) => s.theme);
  const toggleTheme = useThemeStore((s) => s.toggleTheme);
```

- [ ] **Step 3: Drop the View menu and its comment**

Theme is the only item in the View menu, so the whole block goes rather than
becoming an empty dropdown. Delete this, including the two-line comment above
it:

```tsx
        {/* Menu items carry no checked state, so the label names the theme
            you'd be switching to rather than the one you're in. */}
        <Menu
          label="View"
          items={[
            {
              label:
                theme === "broadsheet"
                  ? "Switch to Classic theme"
                  : "Switch to Broadsheet theme",
              onSelect: toggleTheme,
            },
          ]}
        />
```

Keep the `File` menu above it and the `Help` menu below it. The `Menu` import
on line 8 stays -- File and Help both still use it.

- [ ] **Step 4: Verify no dangling references remain**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/busy-meninsky-e73b29 && grep -rn "themeStore\|toggleTheme\|applyTheme\|readStoredTheme" frontend/src
```

Expected: no output at all. Any hit is a leftover -- remove it before moving on.

- [ ] **Step 5: Verify the project typechecks**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/busy-meninsky-e73b29/frontend && npx tsc --noEmit
```

Expected: clean exit, no output. This is the step that proves Task 2 and Task 3
together left the tree consistent.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/main.tsx frontend/src/components/Header.tsx frontend/src/stores/themeStore.ts
git commit -m "feat: remove the Classic theme toggle"
```

---

### Task 4: Correct the fonts README

**Files:**
- Modify: `frontend/src/fonts/README.md:16-17`

- [ ] **Step 1: Fix the stale present-tense reference**

The file's last paragraph refers to a Classic theme that no longer exists.
Replace these two lines:

```markdown
Only the Broadsheet theme references these. The Classic theme keeps using the
system sans stack and loads none of them.
```

with:

```markdown
Broadsheet is the app's only theme, so these faces load on every page.
```

Leave the rest of the file -- the licensing and bind-mount paragraphs are still
accurate.

- [ ] **Step 2: Commit**

```bash
git add frontend/src/fonts/README.md
git commit -m "docs: drop the stale Classic theme reference"
```

---

### Task 5: Verify in the browser

The real verification step for UI work in this repo. Nothing here is optional
-- CSS has no compiler, so this pass is what catches a broken theme.

- [ ] **Step 1: Rebuild the running stack**

From the **main repo root**:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

- [ ] **Step 2: Confirm the stack is serving main, not this worktree**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: no path contains `.claude/worktrees/`. If one does, the stack is
pointed at the wrong tree -- re-run Step 1 from the main repo root.

- [ ] **Step 3: Walk the checklist at localhost:5173**

Check each, and treat any failure as a blocker to report rather than patch
around:

- The app renders in Broadsheet -- serif type, paper-cream background.
- Hard reload (Cmd-Shift-R): no flash of the dark theme before paint.
- The **View** menu is gone from the header.
- **File** still opens and "Clean up storage now" is present.
- **Help** still opens and navigates to BioFlow Calculations.
- Navigate to Search and Activity; both render in Broadsheet.

- [ ] **Step 4: Confirm the theme survives a fresh browser state**

The old `bioflow.theme` key is now inert, and this proves it -- a visitor with
no saved choice must still get Broadsheet.

In the browser console:

```js
localStorage.removeItem("bioflow.theme"); location.reload();
```

Expected: the app still renders in Broadsheet.

- [ ] **Step 5: Report the result**

State plainly which checklist items passed. If anything failed, say so with
what you saw rather than reporting the task complete.

---

## Out of scope

Per the spec, do **not** do these:

- Flattening `broadsheet.css` into `styles.css`.
- Deleting the dead Classic appearance rules or the `prefers-color-scheme`
  block inside `styles.css`. They are inert -- the class selector outranks them.
- Shipping cleanup code for the stale `bioflow.theme` localStorage key.
- Rewriting `plans/broadsheet-theme.md`; it is a historical record.
