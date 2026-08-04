# Styled ModelCombo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `ModelCombo.tsx`'s native `<datalist>` dropdown with a
self-built, app-styled suggestion list, so the popup can no longer flash
from the dark theme to unreadable browser-native white.

**Architecture:** One component rewrite (`ModelCombo.tsx`) following the
exact interaction pattern `NcbiDownloadDialog.tsx`'s organism-search field
already uses -- an absolutely-positioned `<ul>` under the input, filtered
options, arrow-key navigation, delayed blur-close -- plus a small,
independently-scoped CSS block. No other file's behavior changes; the
component's external props (`value`, `options`, `onChange`, `id`) are
unchanged, so its one consumer, `ProviderForm.tsx`, needs no edits.

**Tech Stack:** React 18, plain CSS custom properties already defined in
`frontend/src/styles.css`. No new dependencies.

**Design:** `docs/superpowers/specs/2026-08-03-model-combo-styling-design.md`

---

## Context you need before starting

**This repo has no frontend component-testing setup.** No jsdom, no
testing-library, zero `.test.tsx` files. `vitest` exists for pure-logic
modules only. This component is pure UI/interaction with no extractable
pure-logic core worth a unit test on its own (the one thing worth pinning --
substring filtering -- is a two-line `.filter()` call, not a module), so
verification here is manual browser testing, which is this repo's actual
practice for UI work. Don't add jsdom or invent a test file for this.

**Run the frontend test suite** (to confirm nothing regresses, even though
this plan adds no new tests):

```bash
cd frontend && npm test
```

**Typecheck:**

```bash
cd frontend && npm run lint
```

**To see the change in a browser**, start the dev stack (adjust for
whichever workflow this repo's CLAUDE.md currently documents -- `docker
compose up -d --build api web worker` from the main checkout root, or
`./ops/worktree-up.sh` if working from a worktree) and open Settings →
Providers, where `ProviderForm.tsx` renders the Model field for each
configured AI provider.

---

## File Structure

**Modified:**

| File | Change |
|---|---|
| `frontend/src/components/ModelCombo.tsx` | Full rewrite of internals; same exported props. |
| `frontend/src/styles.css` | New `.model-combo-*` rules, structurally mirroring the existing `.sra-organism-suggestions` block. |

**Unchanged:** `frontend/src/components/ProviderForm.tsx` -- it passes
`value`, `options`, `onChange`, `id` today and will continue to; this plan
does not touch it.

---

## Task 1: Rewrite `ModelCombo.tsx` as a styled combobox

**Files:**
- Modify: `frontend/src/components/ModelCombo.tsx`

Here is the component's exact current content, for reference (so the diff
is clear -- you are replacing the whole file):

```tsx
/**
 * A model id: pick from the fetched list, or type one.
 *
 * Deliberately not a plain `<select>`. Some OpenAI-compatible servers implement
 * `/v1/models` poorly or not at all, OpenRouter returns hundreds of entries,
 * and a model id the user knows is valid must not be blocked by a listing
 * endpoint having a bad day. A datalist gives the dropdown when the list is
 * useful and gets out of the way when it is not.
 */
export function ModelCombo({
  value,
  options,
  onChange,
  id = "model-combo",
}: {
  value: string;
  options: string[];
  onChange: (value: string) => void;
  id?: string;
}) {
  return (
    <>
      <input
        className="settings-input"
        list={`${id}-options`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={options.length ? "Choose or type a model id" : "Type a model id"}
        spellCheck={false}
        autoComplete="off"
      />
      <datalist id={`${id}-options`}>
        {options.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
      {options.length === 0 && (
        <p className="settings-hint">
          No models fetched yet — press Fetch models, or type an id directly.
        </p>
      )}
    </>
  );
}
```

- [ ] **Step 1: Write the new component**

Replace the entire contents of `frontend/src/components/ModelCombo.tsx`
with:

```tsx
import { useState } from "react";

/**
 * A model id: pick from the fetched list, or type one.
 *
 * Deliberately not a plain `<select>`. Some OpenAI-compatible servers implement
 * `/v1/models` poorly or not at all, OpenRouter returns hundreds of entries,
 * and a model id the user knows is valid must not be blocked by a listing
 * endpoint having a bad day. A self-built suggestion list gives the dropdown
 * when the list is useful and gets out of the way when it is not -- free
 * text is always accepted, matched or not.
 *
 * Not a native `<datalist>`, which this used to be: a datalist's popup is
 * rendered by browser chrome rather than app CSS, so its background/text
 * couldn't be kept in the app's dark theme and would flash to unreadable
 * browser-native white a moment after opening. This follows the same
 * anchored-suggestions pattern NcbiDownloadDialog's organism search already
 * uses, styled with the app's own theme variables instead.
 */
export function ModelCombo({
  value,
  options,
  onChange,
  id = "model-combo",
}: {
  value: string;
  options: string[];
  onChange: (value: string) => void;
  id?: string;
}) {
  const [open, setOpen] = useState(false);
  const [highlighted, setHighlighted] = useState(0);

  // Recomputed from value/options on every render rather than stored as its
  // own state, so the visible list can never drift from what's currently
  // typed.
  const filtered = options.filter((m) =>
    m.toLowerCase().includes(value.toLowerCase()),
  );

  const pick = (model: string) => {
    onChange(model);
    setOpen(false);
  };

  return (
    <div className="model-combo">
      <input
        id={id}
        className="settings-input"
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
          setHighlighted(0);
        }}
        onFocus={() => {
          if (options.length > 0) setOpen(true);
        }}
        onKeyDown={(e) => {
          if (!open || filtered.length === 0) return;
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setHighlighted((h) => (h + 1) % filtered.length);
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setHighlighted((h) => (h - 1 + filtered.length) % filtered.length);
          } else if (e.key === "Enter") {
            e.preventDefault();
            pick(filtered[highlighted]);
          } else if (e.key === "Escape") {
            setOpen(false);
          }
        }}
        onBlur={() => {
          // Delayed so a click on a suggestion registers before the
          // dropdown unmounts out from under it.
          setTimeout(() => setOpen(false), 150);
        }}
        placeholder={options.length ? "Choose or type a model id" : "Type a model id"}
        spellCheck={false}
        autoComplete="off"
      />
      {open && filtered.length > 0 && (
        <ul className="model-combo-suggestions">
          {filtered.map((m, i) => (
            <li key={m}>
              <button
                type="button"
                className={i === highlighted ? "active" : undefined}
                onMouseEnter={() => setHighlighted(i)}
                onClick={() => pick(m)}
              >
                {m}
              </button>
            </li>
          ))}
        </ul>
      )}
      {options.length === 0 && (
        <p className="settings-hint">
          No models fetched yet — press Fetch models, or type an id directly.
        </p>
      )}
    </div>
  );
}
```

Note what changed and why, versus the spec:

- `filtered` is derived every render from `value`/`options`, exactly as the
  spec's "Component shape" section specifies -- no separate filter state to
  drift out of sync.
- `highlighted` indexes into `filtered`, not `options`, so wraparound is
  always relative to what's visible -- matching the spec and
  `NcbiDownloadDialog`'s own pattern.
- The `id` prop is now applied to the `<input>` directly (`id={id}`) rather
  than only being used to build a datalist id (`${id}-options`), since there
  is no longer a datalist to namespace against. Callers pass a per-provider
  id (`` `model-${provider.id}` ``) for exactly this reason -- to keep each
  provider's field uniquely identifiable -- so applying it to the input
  preserves that intent. This is a small, deliberate improvement over
  silently dropping the id's only use; `ProviderForm.tsx` needs no change
  since it already passes `id` today.
- `Enter` picks the highlighted suggestion when the list is open, per the
  spec's behavior list. When the list is closed, `Enter` does nothing special
  here (no `<form>` wraps this field in `ProviderForm.tsx`'s Model section,
  so there's no submit to prevent) -- free text the user typed is already
  live in `value` via the `onChange` that fires on every keystroke.

- [ ] **Step 2: Typecheck**

```bash
cd frontend && npm run lint
```

Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ModelCombo.tsx
git commit -m "Replace ModelCombo's native datalist with a styled combobox

The datalist's popup is rendered by browser chrome, not app CSS, which is
why its background/text flashed unreadable a moment after opening -- a
known datalist limitation. Follows the anchored-suggestions pattern
NcbiDownloadDialog's organism search already uses."
```

---

## Task 2: Add the suggestion-list CSS

**Files:**
- Modify: `frontend/src/styles.css`

The component from Task 1 references two classes that don't exist yet:
`.model-combo` (the positioning anchor) and `.model-combo-suggestions` (the
dropdown itself, plus its `button` and `.active` state).

- [ ] **Step 1: Find the insertion point**

The existing `.settings-*` rules are the right neighborhood. Locate them:

```bash
grep -n "^\.settings-hint" frontend/src/styles.css
```

This should report line 4395 (verify against current file -- report back if
it's meaningfully different, since another change may have shifted it).

- [ ] **Step 2: Insert the new rules**

Add this block immediately after the `.settings-hint` rule (which ends with
its closing `}` a few lines after the line found in Step 1) and before
`.settings-status`:

```css
/* The dropdown positions against this wrapper, not the input directly, so
   the anchor is stable regardless of the input's own box model. Mirrors
   .sra-organism-anchor / .sra-organism-suggestions in shape; kept as a
   separate block rather than shared because the two suggestion lists render
   different row content (a single model id here vs. name/common-name/rank
   spans there). */
.model-combo {
  position: relative;
}

.model-combo-suggestions {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  z-index: 5;
  margin: 2px 0 0;
  padding: 4px;
  list-style: none;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  max-height: 240px;
  overflow-y: auto;
}

.model-combo-suggestions li {
  margin: 0;
}

.model-combo-suggestions button {
  display: block;
  width: 100%;
  padding: 5px 8px;
  background: none;
  border: none;
  border-radius: calc(var(--radius) - 2px);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.model-combo-suggestions button:hover,
.model-combo-suggestions button.active {
  background: var(--bg-hover);
}
```

- [ ] **Step 3: Confirm the three referenced variables exist**

```bash
cd frontend && for v in bg-elevated border radius bg-hover; do printf "%-14s " "--$v"; grep -c -- "--$v:" src/styles.css; done
```

Expected: non-zero for all four. These are the same variables
`.sra-organism-suggestions` already uses successfully, so this is a sanity
check rather than an open question -- if any come back zero, stop and check
`grep -n '^\s*--' frontend/src/styles.css | head -40` for the real name
before continuing; do not invent a value.

- [ ] **Step 4: Typecheck (styles.css doesn't affect TS, but confirm nothing else broke)**

```bash
cd frontend && npm run lint
```

Expected: no output, exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/styles.css
git commit -m "Add themed styling for ModelCombo's suggestion list

Structurally mirrors .sra-organism-suggestions -- same positioning, same
theme variables -- kept as its own block rather than shared since the two
lists render different row content."
```

---

## Task 3: Verify in a browser

**Files:** none (verification only)

- [ ] **Step 1: Bring up the app**

From the main checkout root (adjust if this plan is being executed from a
worktree -- check CLAUDE.md's current guidance on `./ops/worktree-up.sh`
vs. the main stack):

```bash
docker compose up -d --build api web worker
```

- [ ] **Step 2: Navigate to the Model field**

Open the app, go to wherever `ProviderForm.tsx` is reachable (Settings →
Providers, or equivalent -- confirm the actual route/menu path if unsure by
searching for where `ProviderForm` is rendered:
`grep -rn "ProviderForm" frontend/src/components/*.tsx frontend/src/App.tsx`).
Select or create a provider with at least one cached model
(`provider.models_cache` non-empty) so the dropdown has something to show --
if none exists, use that provider's "Fetch models" button first.

- [ ] **Step 3: Confirm the fix, in both themes**

Toggle the app to dark theme (if not already) and click into the Model
field. Confirm:

1. The dropdown appears immediately (field is non-empty from a prior
   selection) or on focus (field is empty), styled with a dark background
   and light text -- and **stays that way**, no flash to white after a
   moment. This is the actual bug being fixed; watch for at least 2-3
   seconds after opening to be sure nothing shifts.
2. Switch to light theme (if the app has a manual toggle; otherwise skip
   this half) and repeat -- dropdown should render with light background,
   dark text, no flash.

- [ ] **Step 4: Confirm filtering and free text**

1. Clear the field, click in -- full model list shows.
2. Type a few characters that match some but not all models -- list narrows
   to matches, case-insensitive.
3. Clear the field again -- full list returns.
4. Type a string that matches nothing in `options` -- dropdown shows nothing
   (or closes, per the `filtered.length > 0` guard), but the typed text
   remains in the field and is not reverted or blocked.
5. Click away (blur) with that unmatched text still in the field, then
   trigger whatever save action `ProviderForm.tsx` offers -- confirm the
   typed value is what gets saved, not silently discarded. This is the
   behavior the component's docstring calls out as the whole reason a plain
   `<select>` was rejected; it must still hold.

- [ ] **Step 5: Confirm keyboard navigation**

1. Focus the field with several matches showing.
2. Press Arrow Down repeatedly -- highlight moves down the list and wraps
   from the last item back to the first.
3. Press Arrow Up -- highlight moves up and wraps from the first item to the
   last.
4. Press Enter with a suggestion highlighted -- that suggestion fills the
   field and the dropdown closes.
5. Reopen the dropdown, press Escape -- dropdown closes, field value
   unchanged.

- [ ] **Step 6: Confirm mouse interaction and the empty-options case**

1. Hover over different suggestion rows -- highlight follows the mouse.
2. Click a row -- it fills the field and closes the dropdown.
3. Find or create a provider with `models_cache` empty (no models fetched
   yet) -- confirm the field shows the "No models fetched yet — press Fetch
   models, or type an id directly." hint and no dropdown ever opens on
   focus, matching the pre-change behavior for this case.

- [ ] **Step 7: Run the full frontend suite**

```bash
cd frontend && npm test
```

Expected: same pass count as before this change (no new tests were added,
none should have broken). Check the count against a baseline if unsure --
run `git stash` then `npm test` then `git stash pop` to compare, or just
confirm every listed test file still shows a checkmark with no failures.

---

## Task 4: Merge and push

Per this repo's CLAUDE.md: once the suite is green and `main` is clean,
merge and push without asking further permission -- this was already
explicitly requested by the user ("go ahead and build a replacement and
merge it into main").

- [ ] **Step 1: Confirm the suite is green**

```bash
cd frontend && npm test && npm run lint
```

Read the actual counts, not just the exit code.

- [ ] **Step 2: Confirm `main` is clean**

```bash
git status --short
```

If anything unrelated to this work is present (untracked files from other
in-progress work, as has been the case in this repo before), that's fine to
leave alone -- only concern yourself with whether *this plan's* changes are
fully committed. If `main` has moved since Task 1 started (check `git log
--oneline -5` against what was expected), re-run Task 3's verification
steps before proceeding -- a green result from before a merge doesn't still
hold after one.

- [ ] **Step 3: Push**

```bash
git push origin main
```

- [ ] **Step 4: Confirm the running stack, if any, reflects the change**

If a dev stack is running (from Task 3), no restart should be needed --
`web` runs `vite dev` with hot reload on the bind-mounted source, so the
browser should already reflect the final committed state. If in doubt,
hard-refresh the browser tab.

---

## Self-review notes

Checked against `docs/superpowers/specs/2026-08-03-model-combo-styling-design.md`:

| Spec requirement | Task |
|---|---|
| Replace datalist, same external props | Task 1 |
| Focus on empty field shows full list | Task 1 (`onFocus`) |
| Typing filters, case-insensitive substring | Task 1 (`filtered`), verified Task 3 Step 4 |
| Arrow Up/Down wraparound | Task 1, verified Task 3 Step 5 |
| Enter picks highlighted | Task 1, verified Task 3 Step 5 |
| Escape closes without changing value | Task 1, verified Task 3 Step 5 |
| Mouse hover/click | Task 1, verified Task 3 Step 6 |
| 150ms delayed blur-close | Task 1 |
| Free text always accepted | Task 1 (no validation gate), verified Task 3 Step 4 |
| `options.length === 0` unchanged | Task 1 (hint text preserved, `onFocus` guards on `options.length > 0`), verified Task 3 Step 6 |
| `.model-combo-*` classes, not shared with `.sra-organism-*` | Task 2 |
| Manual browser verification, both themes | Task 3 |

No spec requirement is missing a task. No placeholders. Type/prop names
(`value`, `options`, `onChange`, `id`, `filtered`, `highlighted`, `open`)
are consistent between Task 1's code and every later reference to them.
