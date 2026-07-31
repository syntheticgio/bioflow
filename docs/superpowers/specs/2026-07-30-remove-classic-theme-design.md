# Remove the Classic theme

Date: 2026-07-30

## Goal

Broadsheet becomes the app's only appearance. Remove the theme toggle, the
persisted choice, and the Classic code path. Both stylesheets stay
byte-identical -- only the theme *selection* machinery is deleted.

## Background

Theming today is a two-layer arrangement:

- `frontend/src/styles.css` (2803 lines) is the base and the Classic theme.
  Its default palette is dark, with a `prefers-color-scheme: light` block at
  line 21.
- `frontend/src/styles/broadsheet.css` (1153 lines) is an *override layer*.
  155 of its rules are scoped under `.theme-broadsheet` on `<html>`, and it
  re-maps the Broadsheet design system's tokens onto the app's existing token
  names (`--bg`, `--text`, `--accent`). That mapping is why the theme needs
  almost no component changes.

Broadsheet does not stand alone: it depends on `styles.css` for all
structural CSS (layout, sizing, component shapes) and overrides only
appearance. Both files are imported unconditionally in `main.tsx`; switching
is a class toggle, not a stylesheet swap.

`frontend/src/stores/themeStore.ts` holds the zustand store, the
`localStorage` key `bioflow.theme`, and `applyTheme`, which `main.tsx` calls
before React mounts so a saved Broadsheet choice does not flash the dark
theme on load.

## Approach

Move the `theme-broadsheet` class from runtime JS onto the static `<html>`
tag in `index.html`.

The class is what every Broadsheet rule is scoped to, so making it permanent
leaves the entire CSS layer working untouched. It also lands before first
paint by construction -- the same flash prevention `applyTheme` provided,
without the JS.

This was chosen over flattening the CSS (folding the 155 scoped rules into
`styles.css` and deleting dead Classic appearance rules). That would leave a
single tidy stylesheet, but it is a hand-merge across ~4000 lines where
"structural, keep" versus "Classic appearance, delete" is a judgment call on
nearly every rule, with no compiler to catch mistakes and only manual
clicking to catch regressions. On a single-user local tool an inert override
layer costs nothing at runtime, since unmatched selectors are free. The
flattening stays available later and is strictly easier once the toggle is
gone.

## Changes

| File | Change |
|---|---|
| `frontend/index.html` | `<html lang="en" class="theme-broadsheet">` |
| `frontend/src/stores/themeStore.ts` | delete the file |
| `frontend/src/main.tsx` | drop the `themeStore` import and the `applyTheme(readStoredTheme())` call; keep both CSS imports |
| `frontend/src/components/Header.tsx` | remove the `useThemeStore` import, the two selectors (`theme`, `toggleTheme`), and the View menu |
| `frontend/src/fonts/README.md` | line 16 refers to "the Classic theme" in the present tense; correct it |

### The View menu

Theme is the only item in `<Menu label="View">`, so the whole block is
removed rather than left as an empty dropdown. File and Help are unaffected.

## Consequences

**Dark mode is gone.** The dark palette in `styles.css` and its
`prefers-color-scheme: light` block both still exist but are now permanently
outranked by the class selector. The app is light-only. This is already the
lived behaviour for anyone who picked Broadsheet -- it is a paper design with
no dark variant by design.

**A stale `bioflow.theme` key.** Existing users keep a leftover localStorage
entry. Nothing reads it after this change, so it is inert and no cleanup code
ships for it.

## Out of scope

- Flattening `broadsheet.css` into `styles.css` (the alternative above).
- Deleting the dead Classic appearance rules inside `styles.css`.
- `plans/broadsheet-theme.md` is a historical record of shipped work and is
  left as written rather than rewritten.

## Verification

The repo has no headless component-testing setup and none is expected, so
per `CLAUDE.md` this is a browser check at localhost:5173:

- App renders in Broadsheet on load.
- Hard reload shows no flash of the dark theme.
- The View menu is gone; File and Help still open and work.
- `grep -rn "themeStore" frontend/src` returns nothing.
- `docker compose up -d --build api web worker` from the **main repo root**
  (never from this worktree -- the bind mounts are relative paths and would
  silently repoint the shared stack at this branch).

Frontend-only: `web` runs `vite dev` against bind-mounted source, so the
change is visible on reload with no worker restart needed.
