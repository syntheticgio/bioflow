# Broadsheet theme — switchable UI skin

Add the "BioFlow Broadsheet" design (from the Claude Design project
`7243ab25-1c08-4ff8-893c-0ba23fce1f3a`) as a **second selectable theme**
alongside the existing one. Both ship; the user picks. Once a winner is
chosen, the loser's files are deleted and this doc goes with them.

## Why this is cheap (the key finding)

The existing UI is already themeable, we just never used it that way:

- **Colors are fully tokenized.** All 14 CSS variables (`--bg`, `--text`,
  `--accent`, …) are defined in one `:root` block in `frontend/src/styles.css`.
  Only **6 hardcoded hex colors** exist in all of `components/` — the base
  colors in `SequenceCharts.tsx:26-31`.
- **Styling is class-driven.** Components use `className` against one
  2240-line stylesheet. The 249 inline `style={{…}}` occurrences are
  overwhelmingly *layout* (flex/gap/padding), not color.

So the theme is delivered as **CSS, not a component fork**. No component gets
duplicated. That is the whole design of this plan, and the thing to protect:
if a step starts wanting a `<BroadsheetProjectExplorer>`, that is the signal
the CSS approach was abandoned by accident.

## What the Broadsheet design actually is

Editorial newspaper treatment, near-opposite of today's dark IDE look:

| | Current | Broadsheet |
|---|---|---|
| Ground | `#0f1216` dark | `#f3f2f2` warm paper |
| Type | 14px system sans | Source Serif 4, 15px, serif headings |
| Accent | `#4a9eff` blue | `#0088b0` cyan + `#d6006c` magenta |
| Radius | 6px | 1–4px (nearly square) |
| Shell | Fixed viewport, panes scroll | Page scrolls under a masthead |
| Density | Compact | Airy (`--space-*`, 5→40px) |

Signature elements: a heavy 4px masthead rule under the wordmark, a
letterspaced uppercase status strip, serif `clamp()` display headings,
italic project names, `.tag` pills for job status, hairline-ruled tables.

## Design decisions

**1. Theme = a class on `<html>`, `.theme-broadsheet`.** Not a separate
stylesheet swap, not a build flag. The base stylesheet keeps its `:root`
tokens; a new `broadsheet.css` re-declares those same 14 tokens under
`.theme-broadsheet` and adds the editorial layer. One import, always loaded.
Cheap and instant to switch.

**2. Reuse the existing token names.** The Broadsheet system names things
`--color-bg` / `--color-text` / `--space-4`. Rather than rewrite every
component to the new names, map Broadsheet's palette *onto* the existing
`--bg` / `--text` / `--accent` names. New Broadsheet-only tokens
(`--space-*`, `--font-heading`, ramps) are added alongside for the editorial
layer to use. This is what keeps component churn near zero.

**3. Dark mode.** Today `styles.css` has a `prefers-color-scheme: light`
block. Broadsheet is inherently a light/paper design; it will **not** get a
dark variant. Its token block must therefore override the light-mode media
query too — specificity of `.theme-broadsheet` handles this, but it has to be
verified on a machine set to light mode, not just dark.

**4. Fonts self-hosted, not from Google.** The design system's `styles.css`
does `@import` from `fonts.googleapis.com` and pulls Phosphor icons from
`unpkg`. This app runs in Docker with no assumption of internet at runtime.
Vendor Source Serif 4 into `frontend/public/fonts/` and skip Phosphor
entirely — the app has its own icon set (`src/icons/`) and emoji glyphs
already. Falls back to `system-ui` serif if a font file is missing.

**5. Scroll model is the one real structural change.** Today `.shell` is a
`grid` locked to `100vh` with panes scrolling internally; Broadsheet scrolls
the whole page under a masthead. Handled in CSS by relaxing `.shell` /
`.main` / `body` overflow under the theme class. This is the highest-risk
step — see Phase 3.

## Phases

Each phase ends in a working, viewable app at localhost:5173. Rebuild with
`docker compose up -d --build web` from the repo root; `vite dev` hot-reloads
CSS, so most steps need no restart at all.

### Phase 1 — Theme plumbing

Goal: a toggle that visibly does *something*, before any real styling.

- `src/stores/themeStore.ts` — zustand store, `theme: "classic" | "broadsheet"`,
  persisted to `localStorage` under `bioflow.theme`. Applies the class to
  `document.documentElement` on change.
- Read the persisted value in `main.tsx` **before first paint** to avoid a
  flash of the wrong theme.
- `src/styles/broadsheet.css` — start with just the token block (paper bg,
  ink text) under `.theme-broadsheet`. Imported once from `main.tsx`.
- Toggle UI: add to the existing `Menu` under **View** in `Header.tsx` —
  that menu exists today with `items={[]}`, so it is the natural home and
  needs no new chrome.

Verify: flipping View → Theme turns the app from dark to paper, survives
reload. Nothing else needs to look right yet.

### Phase 2 — Typography and tokens

- Vendor Source Serif 4 (400/600, normal + italic) to `public/fonts/`,
  `@font-face` with `font-display: swap`.
- Fill in the full Broadsheet token set: neutral/accent/accent-2 ramps,
  `--space-*`, `--radius-*` (1/2/4px), `--shadow-*`.
- Retune base type under the theme: serif family, 15px/1.55 body, serif
  headings with `-0.015em` tracking.
- Keep `--mono` as-is — IDs, sequences, and job names stay monospace in both
  themes; the mockup does this too.

Verify: app reads as the paper design, still laid out like today.

### Phase 3 — Shell, masthead, scroll model

The structural phase. Under `.theme-broadsheet` only:

- `.shell` drops the fixed `100vh` grid; `body` scrolls.
- `.header` becomes the masthead: 34px serif wordmark, "Genomics pipeline
  desk" kicker, text-button nav, 4px solid rule beneath.
- Add the uppercase status strip (project · counts · queue state) below the
  rule, fed by the `systemStats` query the header already runs.
- `.footer` flattens into a hairline-ruled page footer.
- `.main` gains the mockup's `290px minmax(0,1fr)` gutter grid.
- **Splitter:** the drag-to-resize splitter has no place in the editorial
  layout (fixed 290px gutter). Hide it under the theme and pin `--left-w`;
  do *not* delete the component — Classic still uses it.

Risk to watch: `App.tsx` sets `--left-w` inline and `.splitter` positions
absolutely against `--header-h`/`--footer-h`. Both must be neutralized under
the theme or the gutter will fight the grid.

Verify: scroll behaves on a long file list; `/activity` and `/help/*`
(the `main-single` routes) still fill width.

### Phase 4 — Content surfaces

Restyle in place, in rough order of visual payoff:

1. **Project explorer / file rows** — the 3px accent selection bar, italic
   names, `62%`-ink secondary text.
2. **Detail panel tabs** — mockup's stacked label+count tab buttons with a
   rule under the active one.
3. **Tables** — `.table` treatment: uppercase letterspaced `th`, hairline
   row borders, 4%-ink hover.
4. **Buttons** — `.btn-primary` / `secondary` / `ghost` mapped onto the
   app's existing button classes.
5. **Tags/badges** — job status pills via the accent-100/800 pairs.
6. **Dialogs** — square corners, paper surface, `--shadow-lg`.
7. **Charts** — `SequenceCharts.tsx` is the *one* component needing a code
   change: lift its 6 hardcoded base colors into CSS variables so the theme
   can retune them to process inks. Read them via `getComputedStyle` or
   pass through a small hook.

Verify each against the mockup's corresponding section.

### Phase 5 — Sweep and finish

- Walk every route and dialog in both themes: `/`, `/p/:id`, `/search`,
  `/activity`, `/help/calculations`, plus Trim / Align / Variant / NCBI
  dialogs and the upload tray.
- Check the light-mode media query interaction (decision 4).
- `npm run lint` (`tsc --noEmit`) and `npm test` — `format.test.ts` and
  `readQuality.test.ts` are pure logic and must stay green.
- Backend untouched, so no `pytest` run is needed; note that explicitly
  rather than silently skipping it.

## Out of scope

- No backend changes. Theme is client-only, `localStorage`, not user prefs.
- No responsive/mobile work — the app is desktop-only today and the mockup
  doesn't change that.
- Not porting the design system's print treatments (CMYK plate separation,
  halftone screens, `print-plates.js`). They're for the marketing deck, not
  an application UI, and the mockup itself doesn't use them.
- No component API changes, no new dependencies.

## Files

New:
- `frontend/src/stores/themeStore.ts`
- `frontend/src/styles/broadsheet.css`
- `frontend/src/fonts/` (vendored woff2 + README)
- `plans/broadsheet-theme.md` (this file)

Modified:
- `frontend/src/main.tsx` — import theme CSS, pre-paint class
- `frontend/src/components/Header.tsx` — View menu toggle
- `frontend/src/components/SequenceCharts.tsx` — de-hardcode 6 colors
- `frontend/src/App.tsx` — scroll reset on route change

Untouched: every other component. The CSS-not-a-fork approach held.

## What changed from the plan during implementation

- **Fonts live in `src/fonts/`, not `public/`.** The override bind-mounts
  only `./frontend/src`, so a `public/` directory would need a rebuild to
  appear in the container. Under `src/` they hot-reload like everything else.
- **`--success` could not take the cyan accent.** The plan reasoned from the
  mockup's `.tag-accent` job pills, but `SequenceCharts` draws its "good"
  quality band from `--success` and the curve itself from `--accent` — folding
  them together erased the band. Success keeps a distinct muted green; the
  badge pills get the mockup's cyan through `.badge` rules instead.
- **The status strip needed no `Header.tsx` change.** The kicker and layout
  are done with CSS grid areas and a `::after`, so the component stays shared.
- **`App.tsx` changed for scroll, not the splitter.** The splitter was fully
  handled in CSS as hoped. But because Broadsheet scrolls the window rather
  than the panes, route changes carried the previous view's scroll offset —
  a real bug, invisible in Classic. Fixed with a `scrollTo(0,0)` on pathname.
- **Class names had to be checked, not assumed.** `.tag`, `.pill`, `.dialog`,
  `.btn-primary` and `.table` don't exist in this codebase; the real ones are
  `.btn`/`.primary`, `.badge`, `.chip`, `.modal`. Worth re-checking against
  `styles.css` before adding more theme rules.

## Verification performed

- Both themes exercised at localhost:5173 across `/`, `/p/:id`, `/search`,
  `/activity`, `/help/calculations`, the New Project dialog, and the QC charts.
- Classic confirmed pixel-unchanged, including drag-to-resize on the splitter
  (340px → 420px → reset).
- Theme choice persists across reload and applies before first paint.
- `tsc --noEmit` clean (the 16 `.png` module errors pre-date this work and
  appear identically on a stashed tree); `vitest` 16/16; `vite build` succeeds
  and fingerprints all four font files.
- Backend untouched, so `pytest` was not run.
