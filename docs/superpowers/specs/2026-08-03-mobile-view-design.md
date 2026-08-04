# Mobile-friendly view for select features

Design, 2026-08-03. Closes the "Mobile-friendly view for select features"
entry in `docs/TODO.md`.

A limited, mobile-only UI covering two workflows that make sense on a phone:
checking on a running pipeline, and dispatching an NCBI download. Not a
responsive redesign of the desktop interface -- the full UI is built for a
three-column desktop with a resizable panel, and maintaining both at feature
parity is not viable.

## Scope

**In:** an activity feed (read-only) and an NCBI download flow
(dispatch-only).

**Out:** alignment, QC, assembly, variant calling, the file explorer, the
detail panel, uploads, search, and the help pages. These need multi-step
workflows, dense parameter selection, and real-time feedback that do not
translate to a phone.

The scope is what bounds the maintenance risk the TODO entry raised. Two
screens, both read-or-dispatch-only, holding no state the desktop also holds.
The one genuine coupling is the job-state vocabulary, and this design removes
it by extraction (see "Shared code").

## Routing

Three routes under `/m/`, rendered by `MobileShell` rather than the desktop
`Shell`:

| Route | Screen |
|---|---|
| `/m/activity` | Job status feed. The landing route. |
| `/m/download` | Project picker, organism/accession search, results. |
| `/m/download/:accession` | Confirm: runs checklist or assembly components. |

`MobileShell` is a separate shell because the desktop one carries the
resizable splitter, `DetailPanel` and `UploadTray`, none of which belong on a
phone. It renders a header (title plus the desktop escape hatch) and a
two-item bottom tab bar over an `<Outlet/>`.

### Detection and redirect

`useIsMobile()` wraps `window.matchMedia("(max-width: 600px)")` with a change
listener, so rotation and resize re-evaluate rather than being fixed at first
render.

**The redirect fires in one direction only.** A narrow viewport on a desktop
route redirects to `/m/activity`. A wide viewport on `/m/*` does **not**
redirect back. Bouncing both ways would make the escape hatch unusable -- the
moment a desktop user chose the mobile view they would be thrown out of it --
and would yank a tablet user out of the screen they were reading on rotation.

**Escape hatch.** A "Use desktop version" link in the mobile header sets
`bioflow.forceDesktop` in `localStorage`, which suppresses the redirect for
that browser; the desktop footer carries a matching "Mobile view" link to
clear it. This needs its own storage rather than `uiStore`, which is not
persisted (`stores/uiStore.ts` is a plain zustand `create` with no `persist`
middleware, deliberately -- navigation lives in the URL).

### Profile gate

Both shells sit behind the existing `Gate` in `App.tsx`, and the mobile view
reuses `ProfilePicker` unchanged. It is a grid of squares and an optional
password field; if anything reads badly at 375px that is a CSS fix in the
existing component, not a parallel screen. Profile selection is a
once-per-session action and does not warrant a third mobile screen in a
feature scoped at two.

This also keeps mobile on the same security posture as desktop, which
skipping the gate would quietly have changed.

## Activity feed (`/m/activity`)

Two calls: `api.listJobs({ limit: 50 })` and `api.systemLoad()`. No
`listRuns`, no per-run `getRun`.

`systemLoad` is needed because a job does not carry its own waiting reason --
`waitingReason(job, load)` derives it by checking the job's class against the
governor's `admitted_classes`. Without it the function degrades to a bare
`"waiting"`, which is exactly the uninformative state this section exists to
avoid. It is one small request alongside the job list, on the same 2s
interval.

That is the concrete consequence of choosing a flat feed over the desktop's
run-grouped view: `ActivityView` fans out `useQueries` across every run to
learn job membership, which is up to 50 parallel requests. None of them
happen here. Run grouping, tap-to-expand, and a drill-in detail screen were
all considered and deferred -- each requires that membership data, and the
feed answers "what is it doing" without it.

**A `blocked` job must be claimed positively.** `JobState` includes
`"blocked"`, and it is in neither the `RUNNING` nor the `WAITING` set.
`ActivityView` derives its "recent" list by negating both, which is safe
there because a blocked job is rendered inside its run's card -- but a flat
feed has no such grouping, so the same negation would file a blocked
`align_reads` under "Recent" as though it had succeeded. The extracted
helpers therefore carry a third `BLOCKED` set, and `waitingReason` answers
"waiting on an earlier step" for it rather than blaming system load for what
is a dependency wait.

Two sections, flat, nothing tappable:

- **Running** -- jobs in `running`, `pending`, `queued`, `delayed`. A running
  job shows a progress bar where progress exists. A waiting job shows its
  waiting reason instead, which is the part worth carrying over from the
  desktop view's own rationale: a queued job with no explanation is
  indistinguishable from a stuck one, and the honest answer is usually "the
  machine is busy", not "something is wrong".
- **Recent** -- the last ~15 settled jobs with outcome, age and duration. A
  failed job shows its error line.

**Polling, not SSE.** Refetch every 2s while anything is running or waiting;
stop entirely when nothing is. This matches `ActivityView`'s existing rule, so
a phone sitting on a finished pipeline makes no requests at all. `useEvents`
is deliberately not mounted: it holds a persistent `EventSource` open, which
is the wrong thing to keep alive on cellular, and the 2s poll already covers
the update rate.

## Download flow

### Step 1 -- project (`/m/download`)

A `<select>` populated by `api.listProjects()`. Required: both download
endpoints take a `project_id`, and on desktop that is ambient because the
dialog opens from inside a project. A phone has no explorer to have been in,
so this becomes the first field and an extra request the mobile view needs
regardless.

The last-used project id is remembered in `localStorage` and preselected, so
the common case costs zero taps.

### Step 2 -- find something

One text field serving both paths, as the desktop dialog also does:

- **A name** produces organism suggestions (taxon autocomplete). Picking one
  calls `ncbiOrganismSearch` with `section: "both"`.
- **An accession** calls `ncbiResolve` and skips to step 3.

Organism search is the feature that makes this worth building. Away from your
desk is exactly where you do not have an accession in front of you.

`ncbiOrganismSearch` returns assemblies **and** SRA runs in one response, each
with its own pager. On a phone these render as a **segmented toggle** --
`Assemblies (41) | Runs (2,847)` -- showing one list at a time. Paging the
visible list sends `section: "assemblies"` or `section: "sra"`, which is what
that parameter exists for, so paging one list never refetches the other.

**Deliberately dropped from the desktop dialog:** platform filter, assembly
level filter, run table sorting, and multi-select spanning pages. Those are
tools for interrogating a large study; the mobile case is queueing the thing
you already came for.

### Step 3 -- confirm (`/m/download/:accession`)

`ncbiResolve` returns an assembly *or* a run list, never both, so this screen
has two branches:

- **Runs** -- a checklist with everything not already in the library
  preselected and the count in the button, matching the desktop default.
  Calls `api.sraDownload`.
- **Assembly** -- component checkboxes (genome, protein, GTF, CDS) with all
  available components preselected, again matching desktop exactly. Calls
  `api.ncbiDownloadAssembly`.

The assembly branch gets its own screen rather than a fixed policy because
the alternatives both misbehave: "genome only" means the same accession
yields different files depending on which device queued it, and the missing
GTF surfaces much later as "why can't I quantify this?". "All, no choice"
never downloads less than desktop but cannot be narrowed at all.

The `run_qc` chaining checkbox is kept, defaulted on -- it is one checkbox,
and there is no way to turn it on afterwards.

**On success:** invalidate the `jobs` and `runs` query keys, then navigate to
`/m/activity`, which is the same thing the desktop dialog does and lands the
user on the screen showing what they just started.

## Files

New:

```
frontend/src/mobile/
  MobileShell.tsx        header + bottom tab bar + <Outlet/>
  MobileActivity.tsx     the feed
  MobileDownload.tsx     project picker + search + segmented results
  MobileConfirm.tsx      runs checklist / assembly components
  useIsMobile.ts         matchMedia hook + forceDesktop flag
frontend/src/styles/mobile.css
```

Changed:

- `App.tsx` -- mount `/m/*` under the existing `Gate`, add the redirect.
- `Header.tsx`, `Footer.tsx` -- the two escape-hatch links.
- `lib/runFormat.ts` -- gains the `RUNNING` / `WAITING` state sets.

### Shared code

Four things currently private to `ActivityView.tsx` move to
`lib/runFormat.ts`, because the mobile feed needs all four and would
otherwise reimplement them:

- `RUNNING`, `WAITING` and a new `BLOCKED` -- the state sets defining what
  counts as in-flight, plus an `isInFlight(state)` helper over the three.
- `waitingReason(job, load)` -- why a queued job has not started. Reused
  rather than rewritten specifically because the governor's
  `admitted_classes` is authoritative, and a second hand-rolled copy would
  answer differently the moment a job class is added.
- `jobLabel(job)` -- the filename a job is about, falling back to its type.
  It reads `r1_name` / `r2_name` / `name` out of an untyped payload, which is
  the kind of key-guessing that silently stops matching if a handler renames
  a payload field. One copy, not two.

These are the places the two views could meaningfully drift; extracting them
is what keeps the parallel-UI risk bounded. `GovernorNote` is deliberately
*not* extracted -- the mobile feed shows per-job reasons rather than a
standing banner, so sharing it would mean sharing layout, not logic.

Also reused unchanged: `formatDuration` / `formatDate` from `lib/format`, the
status vocabulary in `lib/runFormat`, and `profileHeaders()` in
`api/client.ts`, which supplies profile scoping for free.

### Styling

A self-contained `mobile.css` imported only by `MobileShell`, rather than
additions to the existing sheets. `styles.css` is 4,264 lines and
`broadsheet.css` another 1,707, both written for a three-column desktop with a
resizable panel; adding mobile rules there would entangle the two surfaces
this design is trying to keep separate.

Single column, tap targets at least 44px, system font stack. The viewport
meta tag in `index.html` is already correct and needs no change.

Colours come from the variables `styles.css` already defines -- `--bg`,
`--text`, `--text-dim`, `--border`, `--accent`, `--error` -- rather than
literals, since that file redefines all of them inside a
`prefers-color-scheme: light` block and hardcoding would break one theme.
Inputs are 16px because iOS Safari zooms the page on focus below that.

## Backend

**No backend changes.** Every endpoint exists: `listJobs`, `systemLoad`,
`listProjects`, `ncbiResolve`, `ncbiOrganismSearch`, `sraDownload`,
`ncbiDownloadAssembly` -- all seven verified present in
`frontend/src/api/client.ts` on 2026-08-03.

## Testing

Manual, in a real browser, per this repo's actual practice -- there is no
jsdom or testing-library setup and none is expected.

- `./ops/worktree-up.sh`, then `localhost:5273` with devtools device
  emulation at 375px.
- A real phone against the LAN address is worth doing for the tap targets and
  the software keyboard, which emulation does not reproduce faithfully.
- Verify: the redirect fires and the escape hatch survives a reload; the feed
  polls while running and stops when idle; an organism search reaches both a
  runs download and an assembly download, each landing a real job visible in
  the feed.

`pytest` should stay green untouched, since no backend file changes. Run it
from the worktree with `./backend/run-worktree-tests.sh tests/ -q`, not
`docker compose exec api`, which would test main's code instead.
