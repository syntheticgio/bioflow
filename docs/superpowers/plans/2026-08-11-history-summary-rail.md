# History Summary Rail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the History tab's right-side provenance summary rail match the approved reference image without changing its generation or copy behavior.

**Architecture:** Keep all data fetching and mutations in `ProvenanceNarrative`. Make the markup express the reference's stable `Summarize` and generated `Methods paragraph` sections, and use broadsheet-scoped CSS for the visual treatment. The existing container-query fallback continues to stack the rail below lineage in narrow detail panes.

**Tech Stack:** React, TypeScript, CSS, Vite.

## Global Constraints

- Preserve the existing provenance API calls, copy behavior, unavailable-reason message, and responsive layout.
- Use the supplied reference image as the visual source of truth.
- The repository has no React component-test harness; validate with the frontend build and worktree app at port 5273.

---

### Task 1: Render the reference hierarchy

**Files:**
- Modify: `frontend/src/components/ProvenanceNarrative.tsx:157-203`

**Interfaces:**
- Consumes: `prose.data?.prose`, `prose.isPending`, `data.lineage`, and existing `copy()` / `prose.mutate()` handlers.
- Produces: Stable `Summarize` invitation markup and a `Methods paragraph` block when prose exists.

- [ ] **Step 1: Add a failing behavior assertion**

The project has no component-test harness. Use the production behavior as the executable check: the current source conditionally renders either `Summarize` or `Methods paragraph`, so it cannot display the two-part reference hierarchy after prose generation.

- [ ] **Step 2: Verify the current behavior is incomplete**

Run: `rg -n 'proseText \? "Methods paragraph" : "Summarize"' frontend/src/components/ProvenanceNarrative.tsx`

Expected: the conditional heading is found, demonstrating that the approved stable heading hierarchy is not yet rendered.

- [ ] **Step 3: Write the minimal implementation**

Render `Summarize` unconditionally above the invitation. In the generated state, render `Methods paragraph` above `ai-summary-body`; retain the existing generation, copy, and regeneration handlers.

- [ ] **Step 4: Verify the implementation**

Run: `npm run build --prefix frontend`

Expected: the TypeScript/Vite production build completes successfully.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ProvenanceNarrative.tsx
git commit -m "feat(history): structure the summary rail as citable prose"
```

### Task 2: Apply the reference rail styling

**Files:**
- Modify: `frontend/src/styles/broadsheet.css:1968-1984`

**Interfaces:**
- Consumes: `.prose-invite`, `.prose-invite-body`, `.ai-summary-body`, `.section-title`, `.btn`, and `.btn-text` markup classes.
- Produces: cyan left-rule prose blocks, a compact rail action, and typography/spacing that matches the approved screenshot.

- [ ] **Step 1: Add a failing visual behavior assertion**

The current style is a bordered, rounded, filled card. The reference requires text blocks marked only by a thin cyan left rule, so the existing CSS is the failing baseline.

- [ ] **Step 2: Verify the current style is incompatible**

Run: `rg -n 'border: 1px solid var\(--color-divider\)|border-radius: var\(--radius-sm\)' frontend/src/styles/broadsheet.css`

Expected: the invitation-card border and radius declarations are found.

- [ ] **Step 3: Write the minimal implementation**

Replace the invitation card with a left border and interior padding. Scope the generated paragraph to the same rail treatment, with readable line-height and no full-card border. Keep existing theme tokens and responsive layout rules.

- [ ] **Step 4: Verify the implementation**

Run: `npm run build --prefix frontend`

Expected: the build completes successfully.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/styles/broadsheet.css
git commit -m "feat(history): style the summary rail from the reference"
```

### Task 3: Manually verify the History tab

**Files:**
- Verify only: `frontend/src/components/ProvenanceNarrative.tsx`
- Verify only: `frontend/src/styles/broadsheet.css`

**Interfaces:**
- Consumes: the worktree Compose stack at `http://localhost:5273`.
- Produces: visual confirmation at wide and narrow pane widths.

- [ ] **Step 1: Start the isolated worktree stack**

Run: `./ops/worktree-up.sh`

Expected: the worktree UI is served at `http://localhost:5273` without changing the main stack on port 5173.

- [ ] **Step 2: Inspect both summary states**

Open an object with History provenance. Confirm the ungenerated state has the `Summarize` heading, italic cyan-ruled explanation, and teal button. Generate prose and confirm the `Methods paragraph` heading, cyan-ruled prose, and Copy/Regenerate actions.

- [ ] **Step 3: Inspect responsive behavior**

Narrow the detail pane until the container query stacks the rail. Confirm no text, action, or cyan rule overflows and the visual hierarchy remains intact.

- [ ] **Step 4: Commit the plan record if it changed during execution**

```bash
git add docs/superpowers/plans/2026-08-11-history-summary-rail.md
git commit -m "docs(history): plan summary rail implementation"
```
