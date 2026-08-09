# Header recent-projects shortcuts

## Problem

The header's status strip (`header-right`, between the file/project/storage
stats and the IDLE/CPU load indicator) has room reserved in the design for
"RECENT" project shortcuts, but nothing populates it today. There is no
tracking anywhere in the codebase of which projects a user has recently
opened -- not in frontend state, not in localStorage, not on the backend.
The closest existing signal, `Project.updated_at`, changes on any mutation
(rename, tag edit, object registration), not specifically on the user
opening/viewing a project, so it is the wrong proxy.

Users with several projects want one-click shortcuts back to the ones they
were just working in, without navigating back through the project list.

## Goals

- Show 1-3 most-recently-*opened* (viewed) projects as clickable shortcuts in
  the header.
- Number of chips shown adapts to available header width -- more room shows
  more chips, a narrow window shows fewer, and a window too narrow for even
  one chip hides the whole section.
- Single-user, local-only tool: this is a convenience shortcut, not a
  cross-device feature.

## Non-goals

- Syncing "recent" across browsers/devices/machines.
- Pinning/favoriting projects, or removing individual entries from the list.
- Any interaction beyond click-to-navigate (no hover preview, no context
  menu).
- Tracking recency by project *activity* (last job run, last file added)
  rather than by the user opening it.

## Design

### Recency tracking (new)

New module `frontend/src/lib/recentProjects.ts`, backed by `localStorage`:

- `recordProjectVisit(id: string, name: string): void` -- called once when
  the user navigates into a project's detail page. Prepends `{id, name,
  visitedAt}` to a list stored under a single localStorage key, dedupes by
  `id` (moving an existing entry to the front rather than duplicating it),
  and caps the list at 5 entries (more than the max 3 ever rendered, so a
  temporarily-hidden entry due to a deleted project doesn't shrink the pool
  below what's needed).
- `getRecentProjects(): {id, name, visitedAt}[]` -- reads and returns the
  list, most-recent-first.

Storing `name` alongside `id` avoids a network round-trip just to label a
chip. If the stored name goes stale (project renamed since last visit), the
chip shows the stale name until the next visit refreshes it -- acceptable for
a shortcut label; not worth a live lookup for this display-only case.

`recordProjectVisit` is called from the project detail route's top-level
component on mount (not from any mutation path), so the list reflects pages
the user actually opened.

### Rendering

New component `frontend/src/components/RecentProjects.tsx`, rendered inside
`Header.tsx`'s existing `header-right` div, positioned between `header-stats`
and `LoadIndicator` (matching the screenshot's `| RECENT ...` position
between the storage stats and the IDLE/CPU strip).

- Reads `getRecentProjects()`, filters out any project id no longer known to
  exist (see Edge cases), and renders:
  - A leading divider + "RECENT" label, styled like the existing `|`
    separator already used elsewhere in the status strip.
  - Up to N chips (`<Link to={`/projects/${id}`}>`), each truncated with
    `text-overflow: ellipsis` at a fixed max-width, separated by the existing
    middot convention (`.header-stats > span + span::before` /
    `.load-indicator > span + span::before` in
    `frontend/src/styles/broadsheet.css:333-338`) so the new block reads as
    part of the same status-strip typography.
- If zero projects have ever been visited, the component renders nothing --
  no orphaned divider.

### Responsive chip count (new)

No `ResizeObserver` or container-width measurement exists anywhere in this
codebase today (confirmed via search) -- only viewport-level `@media` rules
and a `matchMedia`-based `useIsMobile()` hook
(`frontend/src/mobile/useIsMobile.ts`). Viewport width is the wrong signal
here because the *available* room for the RECENT block depends on how much
the rest of the header (brand, nav links, stats) is already consuming, not
just window size.

New reusable hook `frontend/src/lib/useElementWidth.ts`:

- Takes a ref to a container element, returns its current pixel width via
  `ResizeObserver`, re-rendering on change.
- General-purpose (not specific to this feature), so it's a small reusable
  primitive rather than one-off logic baked into `RecentProjects`.

`RecentProjects` wraps its rendered content in a container measured by this
hook (or measures a sibling slot reserved for it within `header-right`), and
computes how many chips fit by walking the recent-projects list in order,
accumulating an estimated chip width (label text width via a fixed
per-character estimate, or a fixed max-width per chip -- simplest: fixed
max-width per chip, since exact text measurement adds complexity for a
cosmetic shortcut list) until adding the next chip would exceed the measured
available width. This produces a count in `[0, 3]`; unrendered chips are not
mounted, not hidden via CSS -- avoids DOM/layout work for chips that will
never be visible at the current width.

At a computed count of 0, the entire block (divider, "RECENT" label, and all
chips) is omitted.

### Interaction

Click navigates to `/projects/:id`. No other affordance: no hover preview, no
remove/unpin control, no context menu. This matches how the mocked-up chips
in the screenshot read as purely informational/clickable, and keeps the
feature's UI surface proportional to its size in the header.

### Edge cases

- **Fewer than 3 projects ever visited**: render however many exist (1 or 2),
  still subject to the width-based cap.
- **Zero projects ever visited**: section does not render.
- **A visited project has since been deleted**: filtered out at render time
  (skip silently, don't show a dead link). Not pruned from localStorage
  eagerly -- the filter is cheap and re-runs every render, so there's no
  correctness gap from leaving stale entries in storage; they simply never
  surface once the underlying project is gone. (Determining "deleted" reuses
  whatever project list/lookup data the header or a light query already has
  available -- no new endpoint.)
- **Project renamed after being visited**: chip shows the name captured at
  visit time until the next visit updates it.

## Testing

No headless component-testing setup exists in this repo (no
jsdom/testing-library, zero `.test.tsx` files), consistent with project
convention -- manual verification in the browser at `localhost:5173` (or a
worktree's `localhost:5273` via `./ops/worktree-up.sh`) is the verification
step:

- Visit 0, 1, 2, and 3+ projects; confirm chip count and section
  visibility at each stage.
- Resize the browser window (or zoom) to confirm chips drop off in order as
  width shrinks, and the whole section disappears below the width needed for
  one chip.
- Rename a visited project, then a delete a visited project; confirm stale
  name behavior and silent filtering respectively.
- Confirm chip click navigates correctly and matches the existing
  middot/divider visual style in both the default and broadsheet themes.
