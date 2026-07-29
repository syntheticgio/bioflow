# Modal Scroll Fix — Pin Actions, Scroll Body

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** The primary action button (Cancel / Align / Trim) stays pinned at the bottom of every modal dialog regardless of how much content the dialog holds, so the user never has to scroll down to find the submit button after expanding an advanced section.

**Architecture:** Convert `.trim-modal` from a single `overflow-y: auto` box into a flex column: the heading and body scroll together in a flex-1 middle region, while `.modal-actions` sits in a flex item at the bottom that never scrolls. The modal's `max-height` constraint stays; the `overflow-y` moves from the modal root to a new `.modal-body` wrapper between the heading and the actions. The Trim and Align dialogs gain a `<div className="modal-body">` wrapper around their existing content; the SRA download dialog (which already has its own scroll handling) is excluded.

**Tech Stack:** React + TypeScript, CSS (single flat stylesheet, `frontend/src/styles.css`), Vite dev server with hot-reload.

---

## Background for the engineer

**Read the TODO entry first.** This plan implements item 3 in `docs/TODO.md`:

> The align dialog's submit button needs scrolling when expanded. With "Aligner
> and performance" expanded, `.trim-modal` is 822px of content in a 633px
> `max-height`. It scrolls, so nothing is unreachable, but the primary action
> leaves the viewport at the moment the user is most likely to want it — they
> have just finished changing settings.

**The problem is structural, not a styling tweak.** The current `.trim-modal` (styles.css:1004-1009) is:

```css
.trim-modal {
  width: 520px;
  max-width: 92vw;
  max-height: 88vh;
  overflow-y: auto;    /* ← the whole modal scrolls, including the actions */
}
```

When content exceeds `max-height: 88vh`, the entire modal scrolls — heading, body, and actions together. The actions leave the viewport. Pinning `.modal-actions` with `position: sticky; bottom: 0` would work on paper but produces a visual artefact: the actions overlap the last form field with a transparent background, and the border between them and the scrolling content flickers during scroll on Safari. The flex-column approach is the one this project already uses for `.panel` (styles.css:198-203: `display: flex; flex-direction: column; overflow: hidden`), so it is consistent with an established pattern rather than a new one.

**Two dialogs share the `trim-modal` class** (verified by search):

- `TrimDialog.tsx:114` — `<div className="modal trim-modal">`
- `AlignDialog.tsx:143` — `<div className="modal trim-modal">`

Both have the same structure:

```
.modal.trim-modal
  h2 (title + "change tool" button)
  error-box / warn-box (conditional)
  .trim-inputs
  .trim-fields / fieldset
  .trim-advanced-toggle + {advanced && ...}
  .modal-actions    ← this is what we want pinned
```

**The SRA download dialog** (`SraDownloadDialog.tsx`) does NOT use `trim-modal`; it has its own multi-step layout. It is excluded from this plan.

**No frontend test suite exists** (per CLAUDE.md: "no headless component-testing setup in this repo, no `.test.tsx` files, none expected"). Verification is manual, in the browser at `localhost:5173`, consistent with every other frontend change in this project.

**This is a CSS + wrapper-div change.** No logic, no state, no new components, no new API. The risk is layout regression — the modal must still look identical when content is short (no scroll needed) and only change behavior when content overflows.

---

## File structure

| File | Change |
|---|---|
| `frontend/src/styles.css` | Rewrite `.trim-modal` to flex column; add `.modal-body` scroll region; add `.modal-actions` border-top. |
| `frontend/src/components/TrimDialog.tsx` | Wrap dialog content (between `h2` and `.modal-actions`) in `<div className="modal-body">`. |
| `frontend/src/components/AlignDialog.tsx` | Same wrapper. |

---

## Task 1: Rewrite `.trim-modal` to a flex column with a scroll body

**Objective:** Change the modal from a single scroll container to a flex column where the body scrolls and the actions stay pinned.

**Files:**
- Modify: `frontend/src/styles.css:1004-1009` (the `.trim-modal` rule)
- Modify: `frontend/src/styles.css:880-884` (the `.modal-actions` rule)

**Step 1: Rewrite `.trim-modal`**

Replace the existing rule at `styles.css:1004-1009`:

```css
.trim-modal {
  width: 520px;
  max-width: 92vw;
  max-height: 88vh;
  overflow-y: auto;
}
```

with:

```css
.trim-modal {
  width: 520px;
  max-width: 92vw;
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
```

`overflow: hidden` on the modal root prevents the modal itself from scrolling — the scroll is delegated to `.modal-body` below. The `max-height: 88vh` constraint stays, so the flex column still caps at 88% of the viewport.

**Step 2: Add `.modal-body` scroll region**

Add a new rule immediately after `.trim-modal` (before `.trim-inputs`):

```css
.modal-body {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}
```

`min-height: 0` is the critical line: without it, a flex item's `min-height` defaults to `auto`, which means "at least as tall as my content," which prevents `overflow-y: auto` from ever engaging — the body grows to fit all content, pushing the actions off-screen, defeating the entire point. This is the one non-obvious line in the plan; get it wrong and the modal behaves exactly as it does today.

**Step 3: Pin `.modal-actions` at the bottom with a separator**

Replace the existing `.modal-actions` rule at `styles.css:880-884`:

```css
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
```

with:

```css
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  flex-shrink: 0;
  padding-top: 12px;
  margin-top: auto;
  border-top: 1px solid var(--border);
}
```

- `flex-shrink: 0` — the actions bar never shrinks; the body absorbs all the height pressure.
- `margin-top: auto` — when the modal content is shorter than `max-height`, the actions bar pushes to the bottom of the flex column rather than sitting immediately after the last form field. This is the case that must look unchanged.
- `border-top` + `padding-top` — a visual separator between the scrolling body and the pinned actions, so the user sees where the scroll region ends. Uses the existing `--border` variable, consistent with every other divider in the app.
- This rule is shared with the generic `.modal` class (the base `.modal-actions` at :880), so it also applies to the SRA download dialog and any future modal. The SRA dialog's actions are already at the bottom of its own layout, so the `border-top` adds a clean separator there too — an improvement, not a regression.

**Important: do not add `position: sticky` or `position: fixed`.** The flex approach is cleaner and does not require a positioned ancestor or z-index management. The existing `.modal-backdrop` has no `position` other than `fixed` (for the backdrop itself), and the modal is centered via `place-items: center` — sticky positioning inside it would fight the grid.

**Step 4: Verify CSS is valid**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner
npx --prefix frontend tsc --noEmit -p frontend/tsconfig.json
```

Expected: no type errors (CSS changes do not affect tsc, but this confirms no accidental edits to `.tsx` files).

**Step 5: Commit**

```bash
git add frontend/src/styles.css
git commit -m "fix: pin modal actions to bottom with flex column scroll body"
```

---

## Task 2: Wrap TrimDialog content in `.modal-body`

**Objective:** Insert the scroll-region wrapper between the heading and the actions in TrimDialog.

**Files:**
- Modify: `frontend/src/components/TrimDialog.tsx`

**Step 1: Add the wrapper**

In `TrimDialog.tsx`, the structure is currently:

```tsx
<div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
  <h2>
    Trim reads
    {onBack && (...)}
  </h2>

  {activeToolInfo && !activeToolInfo.available && (...)}
  {longRead && (...)}

  <div className="trim-inputs">
    ...
  </div>

  {activeTool === "fastp" && (<>...</>)}
  {activeTool === "cutadapt" && (...)}
  {activeTool === "trimmomatic" && (...)}

  <div className="modal-actions">
    ...
  </div>
</div>
```

Wrap everything between `</h2>` and `<div className="modal-actions">` in a `<div className="modal-body">`:

```tsx
<div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
  <h2>
    Trim reads
    {onBack && (
      <button type="button" className="dialog-tool-back" onClick={onBack}>
        change tool
      </button>
    )}
  </h2>

  <div className="modal-body">
    {activeToolInfo && !activeToolInfo.available && (
      <div className="error-box" style={{ marginBottom: 12 }}>
        {activeToolInfo.error ?? `${activeTool} is not available`}
      </div>
    )}

    {longRead && (
      <div className="warn-box" style={{ marginBottom: 12, fontSize: 12 }}>
        ...
      </div>
    )}

    <div className="trim-inputs">
      ...
    </div>

    {activeTool === "fastp" && (<>...</>)}
    {activeTool === "cutadapt" && (...)}
    {activeTool === "trimmomatic" && (...)}
  </div>

  <div className="modal-actions">
    ...
  </div>
</div>
```

The `h2` stays outside `.modal-body` so the heading never scrolls away. The `.modal-actions` stays outside so it pins to the bottom.

**Step 2: Verify TypeScript compiles**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner
npx --prefix frontend tsc --noEmit -p frontend/tsconfig.json
```

Expected: no errors.

**Step 3: Commit**

```bash
git add frontend/src/components/TrimDialog.tsx
git commit -m "fix: wrap TrimDialog content in modal-body scroll region"
```

---

## Task 3: Wrap AlignDialog content in `.modal-body`

**Objective:** Same change as Task 2, for the AlignDialog — the dialog the TODO entry was originally raised against.

**Files:**
- Modify: `frontend/src/components/AlignDialog.tsx`

**Step 1: Add the wrapper**

The AlignDialog structure is:

```tsx
<div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
  <h2>
    Align reads
    {aligner && <span className="dialog-tool-subtitle"> — {aligner}</span>}
    {onBack && (...)}
  </h2>

  {alignerInfo && !alignerInfo.available && (...)}

  <div className="trim-inputs">...</div>
  <div className="trim-fields">...</div>

  <fieldset className="trim-fields">...</fieldset>

  <button className="trim-advanced-toggle">...</button>
  {advanced && (<div className="trim-fields">...</div>)}

  <div className="modal-actions">...</div>
</div>
```

Wrap everything between `</h2>` and `<div className="modal-actions">`:

```tsx
<div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
  <h2>
    Align reads
    {aligner && <span className="dialog-tool-subtitle"> — {aligner}</span>}
    {onBack && (
      <button type="button" className="dialog-tool-back" onClick={onBack}>
        change tool
      </button>
    )}
  </h2>

  <div className="modal-body">
    {alignerInfo && !alignerInfo.available && (
      <div className="error-box" style={{ marginBottom: 12 }}>
        {alignerInfo.name} is not available on this machine.
      </div>
    )}

    <div className="trim-inputs">
      ...
    </div>

    <div className="trim-fields">
      ...reference selector...
    </div>

    <fieldset className="trim-fields">
      ...read group...
    </fieldset>

    <button
      type="button"
      className="trim-advanced-toggle"
      onClick={() => setAdvanced((a) => !a)}
      aria-expanded={advanced}
    >
      <span className="trim-chevron">{advanced ? "▾" : "▸"}</span>
      Aligner and performance
    </button>

    {advanced && (
      <div className="trim-fields">
        ...preset, threads, sort memory, mark duplicates...
      </div>
    )}
  </div>

  <div className="modal-actions">
    ...
  </div>
</div>
```

**Step 2: Verify TypeScript compiles**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner
npx --prefix frontend tsc --noEmit -p frontend/tsconfig.json
```

Expected: no errors.

**Step 3: Commit**

```bash
git add frontend/src/components/AlignDialog.tsx
git commit -m "fix: wrap AlignDialog content in modal-body scroll region"
```

---

## Task 4: Verify in the browser

**Objective:** Confirm the fix works against the live app, covering both dialogs and the short-content (no-scroll) case.

**Step 1: Ensure the stack is running**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner
make up
```

The `web` service runs `vite dev` with hot-reload, so CSS and TSX changes apply on the next page load — no rebuild needed. If the worker was also changed (it was not in this plan), `docker compose restart worker` would be needed, but this plan only touches frontend files.

**Step 2: Verify the Align dialog with advanced section expanded**

1. Open `http://localhost:5173`
2. Select a FASTQ file in the project explorer
3. Click **Align** (after the tool selector, if shown)
4. In the Align dialog, expand **"Aligner and performance"**
5. **Verify:** The Cancel / "Build index and align" buttons are visible at the bottom of the dialog without scrolling. The form fields scroll in the region above the actions.
6. Scroll the body region: the heading ("Align reads — minimap2") stays fixed at the top, the actions stay fixed at the bottom, only the middle scrolls.
7. **Verify:** The border-top separator is visible between the last form field and the actions bar.

**Step 3: Verify the Trim dialog**

1. Select a FASTQ file
2. Click **Trim** (after the tool selector)
3. Expand **"Adapters and filtering"** (fastp's advanced section)
4. **Verify:** The Cancel / "Trim" buttons are pinned at the bottom, visible without scrolling.

**Step 4: Verify the short-content case (no regression)**

1. Open the Trim dialog with the advanced section **collapsed**
2. **Verify:** The dialog looks the same as before the change — the actions sit at the bottom of the dialog, the `margin-top: auto` pushes them down so there is no gap between the last field and the actions when content is short.
3. **Verify:** No visual gap, no doubled borders, no extra padding compared to the pre-change appearance. The only visible difference should be the `border-top` on the actions bar, which is a deliberate separator.

**Step 5: Verify the SRA download dialog (unmodified, but shares `.modal-actions`)**

1. Click the **+** / "Download from SRA" option
2. **Verify:** The SRA dialog's actions bar (if visible) has the `border-top` separator. No layout regression — the dialog should look the same as before, just with the separator line.

**Step 6: Check browser console**

Open the browser console (DevTools → Console). **Verify:** No errors, no warnings about CSS or layout.

---

## Edge cases and states

| Scenario | Behavior |
|---|---|
| Content shorter than `max-height: 88vh` | Modal is exactly as tall as its content. `margin-top: auto` pushes actions to the bottom. No scroll. Visually identical to before. |
| Content exceeds `max-height: 88vh` | Body region scrolls. Heading stays fixed at top. Actions stay fixed at bottom. |
| Advanced section expanded (the original bug) | Body grows; actions remain pinned. The user sees the submit button without scrolling. |
| Advanced section collapsed | Body is short; no scroll; actions at the bottom via `margin-top: auto`. |
| Very small viewport (short window) | `max-height: 88vh` still caps; body scrolls within that. Actions remain pinned at the bottom of the 88vh box. |
| Light mode | `--border` variable inverts correctly (verified: styles.css:21-36 defines the light-mode override). The separator line is visible in both themes. |

---

## What this does NOT do

- Does not change the SRA download dialog's layout (it does not use `trim-modal`).
- Does not add `position: sticky` or `position: fixed` (flex column is the approach).
- Does not add any new React state or hooks.
- Does not create a reusable `Modal` component (YAGNI — two dialogs share a CSS class, and a wrapper div is a smaller change than a component extraction).
- Does not change the tool selector modal (`PipelineToolSelector.tsx`), which uses `.modal.tool-selector` and has its own width. Its actions are already at the bottom of a short modal and do not scroll.
