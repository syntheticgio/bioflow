# Replace ModelCombo's native datalist with a styled combobox

Design, 2026-08-03.

## Problem

`ModelCombo.tsx` pairs an `<input>` with a native `<datalist>` for its
dropdown. The datalist's popup is rendered by the browser's own form-control
UI, not by this app's CSS -- reported symptom: the dropdown initially shows
dark background with light text, then flips to a white background a moment
later, making the text unreadable. This is a known limitation of
`<datalist>`: its popup styling cannot be reliably controlled with CSS in any
browser, so the app's dark theme can't reach it.

## Scope

Replace the native datalist in `frontend/src/components/ModelCombo.tsx` with
a self-built combobox: a text input plus an absolutely-positioned,
app-styled suggestion list. External props stay the same
(`value`, `options`, `onChange`, `id`), so the sole consumer,
`frontend/src/components/ProviderForm.tsx`, needs no changes.

Out of scope: any other native form control in the app (`<select>` elements
elsewhere are unaffected), and any change to how models are fetched or
stored.

## Precedent to follow

`frontend/src/components/NcbiDownloadDialog.tsx`'s organism-search field
already solves this exact problem for a different input, using:

- `.sra-organism-anchor` (`position: relative`) wrapping the label/input.
- `.sra-organism-suggestions` (`position: absolute`, `top: 100%`) as the
  dropdown, styled with the app's own `--bg-elevated`/`--border`/`--text`
  variables and a `box-shadow`.
- `showSuggestions`/`highlighted` state, arrow-key navigation with
  wraparound, `Escape` to close, mouse-hover to sync the highlight, and a
  150ms-delayed `onBlur` close (so a click on a suggestion registers before
  the list unmounts).

`ModelCombo` follows this same shape rather than inventing a new one.

## Behavior

- **Focus (even with an empty field) shows the full `options[]` list.** This
  matches what the native datalist already did and was chosen explicitly
  over "only show suggestions once typing starts" so browsing the fetched
  model list needs no keystroke.
- **Typing filters the list** to options containing the typed text as a
  substring, case-insensitive. Chosen over an unfiltered list because
  `options[]` can be hundreds of entries long (the OpenRouter case this
  component's own docstring names), and filtering is also what a native
  datalist already did -- this is a styling fix, not a behavior change.
- **Arrow Up/Down** moves a highlighted index with wraparound (matching the
  NCBI dialog's `(h + 1) % suggestions.length` / `(h - 1 + n) % n` pattern).
- **Enter** picks the highlighted suggestion if the list is open; otherwise
  the typed value stands as-is (this is a combobox, not a required
  selection).
- **Escape** closes the list without changing the value.
- **Mouse hover** over a suggestion syncs the highlight; **click** picks it.
- **Blur** closes the list after a 150ms delay, for the same reason the NCBI
  dialog uses one: without the delay, a click on a suggestion fires after
  the blur has already unmounted the list.
- **Free text is always accepted**, matched or not. This is the entire
  reason the component avoided a plain `<select>` in the first place (its
  own docstring: "a model id the user knows is valid must not be blocked by
  a listing endpoint having a bad day"). The suggestion list is advisory
  only -- there is no validation step, and closing the list or typing a
  value with zero matches never blocks `onChange` from firing with the
  typed text.
- **No options fetched yet** (`options.length === 0`): unchanged from
  today -- no dropdown ever opens (there's nothing to suggest), and the
  existing "No models fetched yet" hint text stays.

## Component shape

`ModelCombo` becomes a small stateful component:

- `open: boolean` -- whether the suggestion list is showing.
- `highlighted: number` -- index into the *filtered* list, not the full
  `options[]`, so arrow-key wraparound is always relative to what's
  currently visible.
- The input's `onChange` calls the caller's `onChange` directly (as today --
  every keystroke is live, free-text-first) and separately recomputes the
  filtered list for rendering.
- Filtering derives from `value` and `options` on each render (a `.filter()`
  over `options`, no memoization needed at this list size) rather than being
  stored as its own state, so it can never drift from what's currently typed.

## Styling

New CSS scoped to this component (`.model-combo-*` class names, to avoid
colliding with `.sra-organism-*`), structurally identical to the existing
`.sra-organism-suggestions` block: `position: absolute`, `top: 100%`,
`background: var(--bg-elevated)`, `border: 1px solid var(--border)`,
`box-shadow`, `max-height` with `overflow-y: auto`, and a `.active` state on
the highlighted row using `var(--bg-hover)`.

A shared class between this and `.sra-organism-suggestions` was considered
and rejected: the two call sites' row content differs enough (this one is a
single model-id string; the NCBI one has a name, a common name, and a rank
as separate styled spans) that a shared class would either force one to
carry unused hooks for the other's fields or need its own set of modifier
classes -- more indirection than two independently-readable, structurally
similar blocks.

## Testing

Manual, in a real browser -- this repo has no jsdom/component-testing setup
and none is expected. Verify via `ProviderForm.tsx`'s real usage:

- Both light and dark theme: dropdown background/text never flashes to
  unreadable, matching the reported bug's fix.
- Typing filters the list; clearing the field shows the full list again.
- Arrow Up/Down wraps at both ends; Enter selects the highlighted row;
  Escape closes without changing the input.
- A typed value with no match in `options[]` is still accepted and saved
  (free text never blocked).
- Clicking a suggestion fills the field and closes the list.
- With `options.length === 0`, behavior is unchanged: no dropdown, hint text
  still shows.
