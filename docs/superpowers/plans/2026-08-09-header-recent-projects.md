# Header Recent-Projects Shortcuts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show 1-3 recently-opened-project shortcuts in the header's status strip, adapting the count to available width and hiding the section entirely when there's no room.

**Architecture:** A `localStorage`-backed tracker (`recentProjects.ts`) records a visit each time `ProjectView` mounts. A new `RecentProjects` component reads that list, measures its own container's width via a new reusable `useElementWidth` hook (backed by `ResizeObserver`), and renders as many chips as fit (0-3) into the existing `header-right` strip in `Header.tsx`.

**Tech Stack:** React 18, TypeScript, Vite, React Router, `@tanstack/react-query` (already in use for `Header.tsx`'s stats query — no new dependency).

**Spec:** `docs/superpowers/specs/2026-08-09-header-recent-projects-design.md`

---

## File Structure

- Create: `frontend/src/lib/recentProjects.ts` — localStorage read/write for the visited-projects list.
- Create: `frontend/src/lib/useElementWidth.ts` — generic `ResizeObserver`-backed width hook.
- Create: `frontend/src/components/RecentProjects.tsx` — renders the chips, decides how many fit.
- Modify: `frontend/src/components/Header.tsx` — mount `<RecentProjects />` inside `header-right`.
- Modify: `frontend/src/components/ProjectExplorer.tsx` — call `recordProjectVisit` from `ProjectView`.
- Modify: `frontend/src/styles.css` — add `.recent-projects` rules for the default theme.
- Modify: `frontend/src/styles/broadsheet.css` — add `.recent-projects` rules matching the broadsheet strip's middot styling.

No backend changes. No new test files — this repo has no frontend headless test setup (confirmed: zero `.test.tsx` files); verification is manual in-browser per project convention (see `CLAUDE.md` "Verifying changes").

---

### Task 1: `recentProjects.ts` — localStorage tracker

**Files:**
- Create: `frontend/src/lib/recentProjects.ts`

- [ ] **Step 1: Write the module**

```typescript
// frontend/src/lib/recentProjects.ts

/**
 * Recently-opened-project tracking for the header shortcut list.
 *
 * localStorage rather than a backend field: this is a single-user local
 * tool, so there is nothing to sync across devices, and adding a
 * last_opened_at field plus a write-on-view endpoint call would be plumbing
 * with no payoff here.
 */

export interface RecentProject {
  id: string;
  name: string;
  visitedAt: number;
}

const STORAGE_KEY = "bioflow.recentProjects";

// More than the max 3 ever rendered, so one stale/deleted entry doesn't
// shrink the usable pool below what RecentProjects needs to fill 3 slots.
const MAX_ENTRIES = 5;

/**
 * Storage access that cannot throw. Safari private-mode raises on
 * setItem/getItem, and this is a display convenience, not a feature anyone
 * should lose the whole page over.
 */
export function getRecentProjects(): RecentProject[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed;
  } catch {
    return [];
  }
}

export function recordProjectVisit(id: string, name: string): void {
  try {
    const existing = getRecentProjects().filter((p) => p.id !== id);
    const next = [{ id, name, visitedAt: Date.now() }, ...existing].slice(
      0,
      MAX_ENTRIES,
    );
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Preference lost on this reload; the rest of the page still works.
  }
}
```

- [ ] **Step 2: Manual check in the browser console**

Run: `docker compose up -d --build api web worker` (from the main checkout root — or `./ops/worktree-up.sh` from this worktree, per project convention), then in the browser console at the running app:

```js
import("/src/lib/recentProjects.ts").then((m) => {
  m.recordProjectVisit("abc123", "Test Project");
  console.log(m.getRecentProjects());
});
```

Expected: logs `[{ id: "abc123", name: "Test Project", visitedAt: <number> }]`. This is a manual smoke check, not an automated test — this repo has no frontend test runner (see plan header).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/recentProjects.ts
git commit -m "feat(frontend): add localStorage-backed recent-projects tracker"
```

---

### Task 2: `useElementWidth.ts` — generic width-measurement hook

**Files:**
- Create: `frontend/src/lib/useElementWidth.ts`

- [ ] **Step 1: Write the hook**

```typescript
// frontend/src/lib/useElementWidth.ts
import { useEffect, useRef, useState } from "react";

/**
 * Live pixel width of a DOM element, via ResizeObserver.
 *
 * Nothing in this codebase measures a specific container's width today --
 * the existing responsive logic (useIsMobile) is viewport-width-based via
 * matchMedia, which is the wrong signal when the thing that needs to react
 * is squeezed by sibling content rather than by the window itself.
 */
export function useElementWidth<T extends HTMLElement>(): [
  React.RefObject<T | null>,
  number,
] {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWidth(entry.contentRect.width);
    });
    observer.observe(el);
    setWidth(el.getBoundingClientRect().width);

    return () => observer.disconnect();
  }, []);

  return [ref, width];
}
```

- [ ] **Step 2: Manual check**

Add a temporary throwaway usage in any component (e.g. log the returned width in `Header.tsx` during Task 4's development) and confirm in the browser console that resizing the window changes the logged value. Remove the temporary log before committing Task 4.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/useElementWidth.ts
git commit -m "feat(frontend): add useElementWidth ResizeObserver hook"
```

---

### Task 3: `RecentProjects.tsx` — the chip list

**Files:**
- Create: `frontend/src/components/RecentProjects.tsx`
- Read (for API shape reference): `frontend/src/api/types.ts:1-15` (`Project`), `frontend/src/api/client.ts:230-233` (`listProjects`)

This component needs to filter out visited projects that no longer exist. It does this with the existing `listProjects` query (already fetched elsewhere in the app and cached by `@tanstack/react-query`, so this adds no new network round-trip in the common case where the project list is already in cache from `ProjectExplorer`'s root view).

- [ ] **Step 1: Write the component**

```typescript
// frontend/src/components/RecentProjects.tsx
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { getRecentProjects } from "../lib/recentProjects";
import { useElementWidth } from "../lib/useElementWidth";

// Fixed per-chip budget rather than measuring text: exact glyph-width
// measurement is unwarranted complexity for a cosmetic shortcut list, and a
// fixed max-width is already how each chip's own label truncation works.
const CHIP_WIDTH_PX = 160;
const LABEL_WIDTH_PX = 60; // "RECENT" + its divider, roughly
const MAX_CHIPS = 3;

export function RecentProjects() {
  const [containerRef, availableWidth] = useElementWidth<HTMLDivElement>();

  const { data: projects } = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
  });

  const recent = getRecentProjects();
  if (recent.length === 0) return null;

  const knownIds = new Set((projects ?? []).map((p) => p.id));
  const visible = recent.filter((p) => knownIds.has(p.id));
  if (visible.length === 0) return null;

  // Available width is measured on a full-width probe (see Header.tsx),
  // so budget is: total minus the "RECENT" label, divided into chip slots.
  const chipBudget = Math.max(0, availableWidth - LABEL_WIDTH_PX);
  const chipCount = Math.min(
    MAX_CHIPS,
    visible.length,
    Math.floor(chipBudget / CHIP_WIDTH_PX),
  );

  return (
    <div className="recent-projects" ref={containerRef}>
      {chipCount > 0 && (
        <>
          <span className="recent-projects-label">RECENT</span>
          {visible.slice(0, chipCount).map((p) => (
            <Link
              key={p.id}
              to={`/p/${p.id}`}
              className="recent-projects-chip"
              title={p.name}
            >
              {p.name}
            </Link>
          ))}
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/RecentProjects.tsx
git commit -m "feat(frontend): add RecentProjects chip-list component"
```

---

### Task 4: Wire into `Header.tsx`

**Files:**
- Modify: `frontend/src/components/Header.tsx:1-9` (imports), `:168-184` (`header-right` block)

- [ ] **Step 1: Add the import**

In `frontend/src/components/Header.tsx`, after the existing `LoadIndicator` import (line 8):

```typescript
import { LoadIndicator } from "./LoadIndicator";
import { Menu } from "./Menu";
import { RecentProjects } from "./RecentProjects";
```

(Alphabetical among the sibling `./` imports, matching the existing ordering of `LoadIndicator`/`Menu`.)

- [ ] **Step 2: Mount the component between the stats and the load indicator**

Replace the `header-right` block (currently lines 168-184):

```tsx
      <div className="header-right">
        {/* What the library holds, then what it is doing. Library size rather
            than free space: under Docker Desktop the container cannot see the
            external drive's real capacity, and a confidently wrong "192 GB
            free" is worse than not saying. These we can count exactly. */}
        {data && (
          <div
            className="header-stats"
            title={`${data.counts.objects} files at ${data.storage.path}`}
          >
            <span>{data.counts.objects} files</span>
            <span>{data.counts.projects} projects</span>
            <span>{formatBytes(data.storage.library_bytes)} stored</span>
          </div>
        )}
        <RecentProjects />
        <LoadIndicator />
      </div>
```

- [ ] **Step 3: Rebuild and load the app**

```bash
docker compose up -d --build api web worker
```

(Or, from this worktree, `./ops/worktree-up.sh` per `CLAUDE.md` — never plain `docker compose` from a worktree.)

Open `localhost:5173` (or `:5273` for the worktree stack). Confirm no console errors and the header renders as before (RecentProjects renders nothing yet since no project has been visited through `ProjectView`).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/Header.tsx
git commit -m "feat(frontend): mount RecentProjects in header status strip"
```

---

### Task 5: Record visits from `ProjectView`

**Files:**
- Modify: `frontend/src/components/ProjectExplorer.tsx:243-273` (`ProjectView`, the `project` query)

`ProjectExplorer` (line 26-28) already dispatches to `ProjectView` whenever the route carries a `projectId` (route `/p/:projectId` in `App.tsx:109`). `ProjectView` fetches the project's own data at lines 270-273:

```typescript
  const { data: project } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
```

- [ ] **Step 1: Add the import**

At the top of `frontend/src/components/ProjectExplorer.tsx`, alongside the other `../lib/` imports (near line 5-6):

```typescript
import { formatBytes, formatKindLabel } from "../lib/format";
import { readQuality } from "../lib/readQuality";
import { recordProjectVisit } from "../lib/recentProjects";
```

Also add `useEffect` to the React import at the top (line 1):

```typescript
import { useEffect, useState } from "react";
```

- [ ] **Step 2: Record the visit once project data is available**

In `ProjectView`, immediately after the `project` query (originally lines 270-273):

```typescript
  const { data: project } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
  });

  // Recorded on view, not on any mutation -- a rename or tag edit elsewhere
  // must not count as the user having "opened" this project just now.
  useEffect(() => {
    if (project) recordProjectVisit(project.id, project.name);
  }, [project]);
```

- [ ] **Step 3: Rebuild and manually verify**

```bash
docker compose up -d --build api web worker
```

In the browser, navigate to any project (`/p/<id>`), then check the console:

```js
import("/src/lib/recentProjects.ts").then((m) => console.log(m.getRecentProjects()));
```

Expected: the visited project appears first in the returned array with a real `name` and a recent `visitedAt` timestamp.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ProjectExplorer.tsx
git commit -m "feat(frontend): record project visits for recent-projects list"
```

---

### Task 6: Styling — default theme

**Files:**
- Modify: `frontend/src/styles.css` (add after the existing `.header-stats` rule, currently ending at line 201)

The default theme's `.header-stats` (lines 194-201) uses `gap: 6px` between plain `<span>`s with no visual divider glyph between them — the middot separator is a broadsheet-specific device (`broadsheet.css:333-338`), not present in the default theme. `RecentProjects` follows the same plain-gap convention here, with its own leading divider bar to set the block apart from the stats before it (matching the screenshot's `|` before "RECENT").

- [ ] **Step 1: Add the CSS**

Append after line 201 (`.header-stats { ... }` closing brace) in `frontend/src/styles.css`:

```css
/* Recent-projects shortcuts: same quiet weight as header-stats and
   load-indicator, with a leading rule to set the block apart, matching how
   the three status-strip groups read as one line but distinct clusters. */
.recent-projects {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-dim);
  padding-left: 14px;
  border-left: 1px solid var(--border);
}

.recent-projects-label {
  color: var(--text-faint);
  letter-spacing: 0.04em;
}

.recent-projects-chip {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-dim);
  text-decoration: none;
}

.recent-projects-chip:hover {
  color: var(--accent);
  text-decoration: underline;
}
```

- [ ] **Step 2: Rebuild and visually verify**

```bash
docker compose up -d --build api web worker
```

Visit a project at `localhost:5173` to seed a recent entry (per Task 5), then reload and confirm the "RECENT" block appears in the header with the correct divider, muted color, and hover-underline on the chip.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/styles.css
git commit -m "style(frontend): style recent-projects chips for default theme"
```

---

### Task 7: Styling — broadsheet theme

**Files:**
- Modify: `frontend/src/styles/broadsheet.css` (add after the existing middot rule, currently ending at line 338)

The broadsheet theme's status strip uses uppercase text and a generated middot (`·`) between sibling spans (lines 330-338). `RecentProjects` needs the same middot treatment between its own chips (matching the screenshot exactly: `RECENT` label then middot-joined project names), and a leading `|` divider before "RECENT" to separate it from `header-stats`.

- [ ] **Step 1: Add the CSS**

Append after line 338 in `frontend/src/styles/broadsheet.css`:

```css
/* Recent-projects joins the status strip's uppercase/middot convention;
   the leading vertical bar is the same divider device used elsewhere in
   the design between the stats block and this one. */
.theme-broadsheet .recent-projects {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: inherit;
  letter-spacing: inherit;
  text-transform: inherit;
  color: inherit;
  padding-left: var(--space-4);
  border-left: 1px solid var(--ink-55);
}

.theme-broadsheet .recent-projects-chip {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: inherit;
  text-decoration: none;
  font-style: italic;
}

.theme-broadsheet .recent-projects-chip:hover {
  color: var(--color-accent-700);
}

.theme-broadsheet .recent-projects > span + a::before,
.theme-broadsheet .recent-projects > a + a::before {
  content: "·";
  margin-right: var(--space-2);
  color: var(--ink-55);
}
```

- [ ] **Step 2: Rebuild and visually verify against the screenshot**

```bash
docker compose up -d --build api web worker
```

Switch to the broadsheet theme (via Settings, if theme selection is user-facing — otherwise confirm by checking whichever route/config applies `theme-broadsheet` to the root element) and confirm the header matches the target screenshot: `| RECENT Saccharomyces cerev... · E. coli K-12 MG1655 · Mouse RNA-seq pilot` in italic serif-matching style, middot-separated.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/styles/broadsheet.css
git commit -m "style(frontend): style recent-projects chips for broadsheet theme"
```

---

### Task 8: End-to-end manual verification

**Files:** none (verification only)

- [ ] **Step 1: Zero-visits state**

Clear `localStorage` (`localStorage.removeItem("bioflow.recentProjects")` in the console) and reload. Confirm the "RECENT" block does not render at all (no orphaned divider).

- [ ] **Step 2: Progressive visits**

Visit one project, reload, confirm exactly 1 chip. Visit a second, different project, reload, confirm 2 chips (most-recent first). Visit a third, confirm 3 chips capped at `MAX_CHIPS`. Visit a fourth, confirm the list still shows the 3 most recent (the 4th oldest of the *visited* set drops off the visible chips, though it may still be in the underlying 5-entry storage list).

- [ ] **Step 3: Responsive width**

With 3 recent projects, narrow the browser window gradually. Confirm chips drop off one at a time (3 → 2 → 1 → section disappears) as available width shrinks, and reappear when widened back out.

- [ ] **Step 4: Deleted project**

Visit a project, then delete that project (via whatever the app's existing delete-project flow is). Reload and confirm the deleted project's chip does not appear (filtered out via the `listProjects` cross-check in `RecentProjects.tsx`), and does not leave a dead link or a rendering gap.

- [ ] **Step 5: Renamed project**

Visit a project, rename it, then reload without revisiting it. Confirm the chip still shows the *old* name (expected per spec — the stored label is refreshed only on the next visit, not live-synced).

- [ ] **Step 6: Click navigation**

Click a chip and confirm it navigates to that project's `/p/:id` view.

- [ ] **Step 7: Both themes**

Repeat step 2 in both the default and broadsheet themes, confirming each renders with its own divider/typography rules from Tasks 6 and 7.

- [ ] **Step 8: Final commit if any fixes were needed**

If verification surfaced any bugs, fix them and commit:

```bash
git add -A
git commit -m "fix(frontend): address recent-projects verification findings"
```

If no fixes were needed, this task requires no commit.

---

## Self-Review Notes

- **Spec coverage:** tracking (Task 1), rendering + width adaptation (Tasks 2-4), visit recording (Task 5), styling in both themes (Tasks 6-7), all spec edge cases exercised in Task 8. Non-goals (cross-device sync, pin/remove, hover preview) are correctly absent from every task.
- **Type consistency:** `RecentProject { id, name, visitedAt }` (Task 1) is the same shape read in `RecentProjects.tsx` (Task 3) and written in `ProjectExplorer.tsx` (Task 5) — `recordProjectVisit(id, name)` signature matches its one call site.
- **No placeholders:** every step has literal code, not a description of code.
