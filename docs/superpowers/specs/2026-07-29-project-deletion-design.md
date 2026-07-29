# Project deletion

Delete a project and everything belonging to it — sub-projects, files, sidecar
indexes, pipeline runs and jobs, and upload staging directories — from a button
on the project information panel. Plus a manual "clean up storage now" trigger
in the File menu, so reclaiming disk space does not require waiting for the
scheduled pass.

## Problem

There is no way to delete a project from the UI. The project panel offers
rename and metadata editing; nothing removes the project itself. Clutter
accumulates on the drive with no in-app way to clear it.

A backend endpoint exists — `DELETE /projects/{id}?cascade=true`
(`api/v1/projects.py:63`) — and `api/client.ts:101` already wraps it, but
nothing in the UI calls it. It is also incomplete in ways that matter for a
"clean up to avoid clutter" feature.

### The cascade leaks blobs

`project_service.delete_project` (`services/project_service.py:112`) detaches
blobs itself:

```python
for obj in await DataObject.find(DataObject.project_id == project_id).to_list():
    await blob_service.detach_blob_from_object(obj.id)
```

That bypasses `object_service.delete_object`, which is the function that knows
what deleting a file actually means. `delete_object`
(`services/object_service.py:512`) cascades to sidecars first, and its docstring
explains why this is required rather than tidy: blob GC is refcount-driven, and

> a sidecar's only reason to exist is its parent: nothing else will ever
> reference an orphaned index, so it would sit at refcount 1 forever and never
> be collected.

So deleting a project containing an indexed BAM removes the BAM's object row
and decrements its blob, but leaves the `.bai` object behind — a `DataObject`
whose `project_id` points at a project that no longer exists, holding its blob
at refcount 1 permanently. `gc_candidates` selects on `ref_count <= 0`
(`services/blob_service.py:163`), so those bytes are unreachable *and*
unreclaimable. This is precisely the "artifacts not listed as files" clutter the
feature is meant to eliminate, and today the delete path creates it.

### Other records survive the delete

Everything else keyed to a project is left in place:

- `PipelineRun` and `RunJob` — `models/run.py:67` carries `project_id`; nothing
  deletes them, so the activity view keeps rows for a project that is gone
- `Job` — `models/job.py:118` carries `project_id`
- `UploadSession` — `models/upload_session.py:23` carries `project_id`, and each
  session owns a staging directory under `settings.staging_dir` that is never
  removed

### The File menu is a stub

`Header.tsx:8` declares `MENUS = ["File", "View", "Help"]` and renders them as
buttons titled "not yet implemented". There is no dropdown implementation at
all, so there is nowhere to put a manual GC trigger.

## What already exists

Worth stating, because it decides what is free and what needs building.

- **`object_service.delete_object`** correctly cascades sidecars and detaches
  blobs transactionally. The fix is to *use* it, not to reimplement it.
- **`gc_blobs`** (`queue/handlers.py:550`) already unlinks managed blobs whose
  refcount has been zero past the grace window, prunes external records, and
  refuses to run when `check_home()` reports the drive is questionable. It is
  registered on a schedule (`queue/scheduler.py:32`) and returns
  `{candidates, unlinked, external_pruned}`. A manual trigger is an enqueue of
  this existing job, not new GC logic.
- **`upload_service.cleanup_staging`** (`services/upload_service.py:323`)
  removes a staging directory and already refuses to recurse outside
  `settings.staging_dir`, however the value reached it.
- **The file `DangerZone`** in `DetailPanel.tsx:821` establishes the inline
  two-step delete pattern the project button should mirror.

## Design decisions

Four decisions, each settled deliberately.

**Blocked by active jobs, not cancel-and-delete.** A project with queued or
running jobs refuses to delete, with a message naming the jobs. Waiting is cheap
in a single-user tool, and this avoids racing a worker mid-write into the CAS.
The user may not realize jobs are running at all, so the message must say so
explicitly rather than failing generically.

**Inline two-step confirm, not type-to-confirm.** Same pattern as file delete,
with the blast radius spelled out in the confirm text. The count is the part
that prevents mistakes — it tells you *right then* that you are about to delete
47 files you had forgotten about. Type-to-confirm mostly trains reflexive
typing. A mistaken delete is recoverable by hand during the GC grace window,
which is an acceptable safety net here.

**A preview endpoint, not denormalized counters.** The project document carries
`counters.object_count`, but those are rollups for that project alone — they
miss nested sub-project contents and carry no job information. Only a preview
computed from the same traversal the delete uses can warn about active jobs
*before* the user commits, which is the whole point of the first decision.

**Space returns on the normal GC schedule.** Delete drops refcounts; the
grace-windowed `gc_blobs` job reclaims bytes later. Kicking GC immediately after
a delete is close to a no-op — the grace window means that run would skip the
very blobs it was kicked for. Force-unlinking would discard both the race
protection and the manual-recovery window. The manual File-menu trigger covers
the "I want the space back now" case instead.

Staging directories are exempt from all of this: not refcounted, not shared, so
they are removed synchronously during the delete.

## Backend

### `project_service.collect_subtree(project_id) -> list[PydanticObjectId]`

Walks the project tree breadth-first and returns every descendant id including
the root. Both preview and delete build on it, so the two cannot disagree about
what "this project" covers.

### `project_service.deletion_preview(project_id) -> dict`

```python
{
  "project_ids": [...],           # whole subtree, root first
  "child_project_count": int,     # excludes the root
  "object_count": int,
  "total_bytes": int,
  "run_count": int,
  "job_count": int,
  "upload_session_count": int,
  "active_jobs": [{"id": str, "job_type": str, "state": str}],
  "blocked": bool,
}
```

`active_jobs` lists jobs anywhere in the subtree whose state is in the existing
`ACTIVE_STATES` set (`models/job.py:47`) — pending, queued, delayed, blocked,
running. Reusing that constant rather than checking for queued-or-running is
deliberate: a `DELAYED` job awaiting backoff is not running now but will be, and
deleting out from under it produces exactly the mid-write race the block exists
to prevent. `blocked` is `len(active_jobs) > 0`, precomputed so the UI does not
re-derive the rule and drift from the backend.

`total_bytes` sums `size` (`models/object.py:135`) over the subtree's objects. It describes bytes
*referenced*, not bytes that will be freed — a blob shared with an object
outside the subtree stays on disk. The UI wording accounts for this.

### `project_service.delete_project_tree(project_id) -> dict`

1. Re-run `deletion_preview`. If `blocked`, raise `ConflictError` with
   `active_jobs` in `details`. Re-running rather than trusting the caller's
   earlier preview means a job that starts between preview and confirm produces
   the same refusal, not a partial delete.
2. For each project in the subtree, **deepest first**:
   - `object_service.delete_object(obj.id)` for every object in it. This is the
     fix for the sidecar leak — sidecars cascade, every blob refcount reaches
     zero, and blobs become GC-eligible.
   - `upload_service.abort_session(s.id)` for every upload session, removing its
     staging directory.
   - Delete `RunJob` rows for the project's runs, then `PipelineRun`, then `Job`.
   - Delete the project document.
3. Return the counts actually removed, for the success toast.

Deepest-first ordering matters: if the operation fails partway, what remains is
a valid tree rather than orphans with dangling `parent_id`s.

**Derived files.** `delete_object` deliberately does not cascade `derived_from`
— deleting reads must not silently destroy alignments made from them. A project
delete is scoped by containment instead: an object inside the project goes
regardless of what it was derived from. A derived object in a *sibling* project
survives with a `derived_from` pointing at a deleted id, which is the same
dangling state an ordinary file delete already produces today. No new problem,
so no new handling.

### API

- `GET /projects/{id}/deletion-preview` → the preview dict.
- `DELETE /projects/{id}?cascade=true` → re-pointed at `delete_project_tree`.
  Re-pointing rather than adding a route means the one existing caller gets the
  fixed behavior instead of leaving a second, subtly-wrong delete path alive.
- `cascade=false` keeps its current refuse-if-not-empty behavior.
- `POST /jobs/maintenance/gc` → enqueues `gc_blobs`, returns the job id. Lives
  on the existing jobs router (`api/v1/jobs.py`). Accepts no job type from the
  client; the route hardcodes `gc_blobs` so it cannot become a general "run any
  handler" endpoint.

### Tests

Pytest, run in the `api` container
(`docker compose exec api python -m pytest tests/ -q`):

- **Sidecar blob reaches refcount 0** after deleting a project containing an
  indexed BAM. This is the regression that motivated the work.
- Nested subtree: three levels, all projects and objects gone.
- Blocked: a queued job in a descendant raises `ConflictError` and deletes
  nothing.
- Staging directories no longer exist on disk after the delete.
- Preview counts equal what the delete actually removes.
- Runs, run-jobs, and jobs for the subtree are gone.

## Frontend

### Project delete

A `ProjectDangerZone` in `DetailPanel.tsx`, rendered inside `ProjectDetail`
below the metadata editor — the position the file `DangerZone` occupies one
level down, using the same `section-title` and button classes so it reads as the
same affordance.

Clicking **Delete project** fires the preview query. Three outcomes:

**Blocked.** No confirm button. Instead:

> Can't delete yet — 2 jobs are still active in this project.
> (`align_bwa` — running, `index_bam` — queued)
> Wait for them to finish, or cancel them, then try again.

"Active" rather than "running", with each job's actual state shown, because
`ACTIVE_STATES` includes pending, queued, and delayed jobs that have not started
— telling the user something is "running" when it is queued behind a backoff
would send them looking for work that is not happening yet.

**Clear.** The confirm text carries the blast radius:

> Delete **Project X**, including 3 sub-projects, 47 files (2.1 GB), and 12
> pipeline runs? This cannot be undone.

Zero-valued clauses are omitted, so an empty project reads simply
"Delete **Project X**? This cannot be undone."

**Pending.** Button shows a loading state while the preview is in flight.

On success: invalidate `["projects"]` and `["project", id]`, clear the panel
selection (the selected id no longer exists), and toast
"Deleted Project X — disk space is reclaimed by the next cleanup pass", which
points at the new menu item.

A 409 on submit — a job started between preview and confirm — renders the same
blocked message rather than a generic error.

### Menu

A new `Menu.tsx`: click to open, click-outside and Escape to close, arrow keys
between items, `aria-expanded` on the trigger and `role="menu"` on the list.
Built as a general component rather than a File-only dropdown because that
behavior *is* the component — the reusable version is the same code in a
different file, and it means the next menu item does not reopen the question.

`Header.tsx` replaces its inert `MENUS` buttons with this. File gets one item,
**Clean up storage now**; View and Help render as empty disabled menus until
someone fills them.

The item POSTs to the maintenance route and toasts the handler's own return
values: "Reclaimed 12 files" or "Nothing to clean up." A cleanup action that
reports nothing feels broken, and `gc_blobs` already returns the counts.

Because `gc_blobs` skips when `check_home()` reports the drive is unavailable,
the skip case surfaces as its own message rather than a misleading
"nothing to clean up".

## Verification

Per CLAUDE.md, manual testing at localhost:5173 is the verification step for
anything UI-facing; there is no headless component-testing setup.

1. Create a project with a nested sub-project, files in both, and at least one
   BAM with a `.bai` sidecar.
2. Confirm the preview counts match what is actually there.
3. Start a pipeline job, click delete, confirm the blocked message names it.
4. Let the job finish, delete, confirm both projects leave the explorer.
5. Confirm the sidecar's blob now has `ref_count == 0` — the leak is fixed.
6. Run **File → Clean up storage now**, confirm bytes leave `objects_dir` and
   the toast reports the count.
7. Confirm the staging directories are gone from `settings.staging_dir`.

`gc_blobs` lives in the queue handlers, so
`docker compose restart worker` is required before re-testing the GC path —
otherwise the worker keeps executing the old in-memory code and the change reads
as not working. Run `docker compose` from the main repo root, never from this
worktree.
