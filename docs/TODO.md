# TODO

Two kinds of entry, kept apart because they are read differently.

**Planned features** are things we have decided to build, described from the
user's side. **Deferred findings** are problems discovered while building
something else, recorded with enough context to pick up cold. Findings are
newest first.

---

# Planned features

## Profiles — FIXED

Shipped across 2026-07-31 and 2026-08-01; **found stale on 2026-08-02**, when
the user asked what was outstanding on profiles and the answer turned out to
be "the heading". This is the fourth instance of the drift CLAUDE.md says has
"already gone wrong three times", and the first one caught by a user's
disbelief rather than an audit.

Where the code lives: `backend/app/models/profile.py` (`Profile`, `owner_id()`,
a partial unique index so only one profile can adopt the legacy owner),
`backend/app/api/v1/profiles.py` (list / create / select / delete),
`backend/app/api/deps.py` (`resolve_owner`, `get_current_owner`, `OwnerDep`),
and on the front end `stores/profileStore.ts`, `components/ProfilePicker.tsx`,
`components/AddProfileModal.tsx`, plus `profileHeaders()` in `api/client.ts`
sending `X-BioFlow-Profile` on every request including the XHR upload path.
Three plans: `2026-07-31-profiles-backend.md`, `2026-08-01-profiles-frontend.md`,
`2026-08-01-profiles-events-and-schedules.md`.

**What the implementation did differently from this entry:**

- **"Edit details" was dropped, and the header menu has one item, not three.**
  This entry specifies Switch profile / Edit details / Logout. Switch and
  Logout collapsed into one because they are the same action: selecting a
  profile issues no token and sets no cookie, so there is no session to end --
  returning to the picker is the entirety of what either would do. Edit was
  dropped because there is no route behind it; the reasoning is recorded at
  `Header.tsx:99`, where a reader is already looking.
- **The Details section was dropped too, and left a dead field.**
  `Profile.details` exists on the model; `ProfileCreate` carries only
  `username`, `password`, `email`, `is_first_boot`, and the modal collects
  nothing else. So name, institution and research areas are unreachable. Noted
  at `AddProfileModal.tsx:26`. Deliberately left as-is on 2026-08-02 rather
  than either built or deleted -- but a model field no code can write is the
  kind of thing that reads as a bug later.
- **A system pseudo-owner, which this entry does not anticipate at all.**
  `keys.SYSTEM_OWNER` and `bp:events:system` exist because four event
  publishers are installation-wide (`system.starvation_override`,
  `storage.unavailable`, `blob.drifted`, `blob.missing`) and blobs are global
  by this spec, so there is no owner to attribute them to. Every client
  subscribes to it alongside its own channel. It has since become the general
  answer for "belongs to the machine" -- maintenance jobs now use it too.
- **The dedup trap this entry predicted was real and is fixed.**
  `queue.py:117` folds the owner into the stored key
  (`f"{owner}:{dedup_key}"`), so one profile's `build_index` can no longer
  silently return `None` for another's identical request.
- **Both traps' second half -- the worker having no request -- was solved by
  giving `JobContext` an `owner` field**, rather than re-reading the job
  document on every progress tick at up to 2 Hz per running job.
- **No data migration, exactly as designed.** The first profile adopts
  `owner: "local"` literally. Zero documents rewritten, which matters because
  this repo has no migrations mechanism.
- **All three plans still show every checkbox unticked.** Per CLAUDE.md that
  is not evidence of anything -- nothing ticks them. The code is present;
  `grep` for the symbol, not the box.

**The measure that misled, worth keeping:** `TODO(profiles)` marker count was
a bad completeness signal, and "0 markers remaining" read as done while
`/api/v1/jobs` still answered with 100 jobs and no header. There are now zero
markers *and* it is actually done, which is precisely why the count proves
nothing either way.

Original entry follows.

Design: `docs/superpowers/specs/2026-07-31-profiles-design.md` (2026-07-31).

Segregate the library into named profiles chosen at startup, so several people
sharing one machine each see their own projects, files and runs. A startup
screen shows a clickable square per profile plus a `+` to add one; the
add-profile modal collects a unique username, an optional password, an optional
email, and an expandable Details section for name, institution and research
areas. An auto-login checkbox skips the picker on subsequent launches. A profile
menu in the header carries Switch profile / Edit details / Logout.

Not a security mechanism. The optional password stops someone entering the
*wrong* profile by accident; the API stays unauthenticated and the spec says so
explicitly rather than implying protection.

What the design settled that the original note left open:

- **Storage does not nest under a profile.** The original note asked whether to
  add a profile level above the current layout. It should not: `blob_rel_path`
  builds `objects/ab/abcdef...` from the SHA-256 alone, so the path *is* the
  content hash. Profiles partition the *metadata* collections (`projects`,
  `objects`, `runs`, `jobs`, `schedules`) via the `owner` field already on every
  document; `blobs` and `objects/` stay **global**. Two profiles holding the
  same reference genome then store it once, and cross-profile sharing becomes
  nearly free instead of impossible.
- **Emoji are safe**, and the numeric id the note proposed is not needed for
  paths — `owner` never becomes a path component. A profile's `ObjectId`
  supplies the stable id so renaming does not rewrite every document.
- **No data migration.** The first profile adopts `owner: "local"` literally, so
  the existing library belongs to it with zero documents rewritten. This matters
  because this repo has no migrations mechanism — see the index-definition entry
  below for what that costs.

Two traps found in the code, both silent:

- `enqueue`'s `dedup_key` is global. A key not carrying a per-profile id would
  let one profile's job silently cancel another's identical request.
- The worker has no HTTP request, and `Job.owner` defaults to `"local"` — so
  every job would be attributed to the first profile unless `enqueue` takes an
  owner and the handlers propagate it.

## Profiles: events and schedules are the last unscoped routes — FIXED

Fixed 2026-08-01, plan
`docs/superpowers/plans/2026-08-01-profiles-events-and-schedules.md`.
`publish_event` now takes a required keyword-only `owner` and publishes to
`keys.events_channel(owner)` (`bp:events:{owner}`); `/events` resolves the
`?profile=` query parameter through a new `deps.resolve_owner` -- shared with
the header dependency -- and subscribes to two channels, the caller's own and
`bp:events:system`. `schedules.py` is documented as deliberately global.
Tests: `backend/tests/queue/test_event_channels.py` and
`backend/tests/api/test_events_scoping.py`, both directions asserted, both
mutation-checked (hardcoding the channel to `"local"` fails eleven of them;
dropping just the system channel fails two).

**What the implementation did differently from this entry:**

- **A system channel, which this entry does not mention at all** -- and it is
  what makes per-owner channels workable. Four of the twelve publishers are
  installation-wide (`system.starvation_override`, `storage.unavailable`,
  `blob.drifted`, `blob.missing`); blobs are global by the spec, so there is no
  owner to attribute them to. They publish to `bp:events:system` and every
  client subscribes to it alongside its own.
- **`queue/results.py` is not a `publish_event` call site.** This entry names
  it; `grep -rn publish_event backend/app` finds only `queue.py`, `worker.py`,
  `executor.py` and `handlers.py`. Twelve call sites, not the audit this
  entry implies.
- **Schedules were closed as global rather than left open.** The decision was
  already made in code: `scheduler.py` enqueues maintenance with a hardcoded
  `owner="local"`. Scoping the routes would give every profile an empty list
  or five copies of one cron table.
- **`JobContext` grew an `owner` field.** The progress publisher had nothing to
  work with -- `_write_progress` knows a job id and an epoch, and re-reading the
  job document would add a Mongo read to every progress tick at up to 2 Hz per
  running job.
- **`complete` falls back to the system channel, not `"local"`,** when its job
  re-read comes back None. `"local"` is a real profile's owner, so that
  fallback would have delivered a stranger's job event into the adopted
  profile's stream -- the exact leak per-owner channels exist to prevent.
- **The frontend needed a guard, not just a rewrite.** `useEvents` already sent
  `?profile=`, but sent it empty when no profile was selected; that is now a
  400, and `EventSource` reconnects on error, so it would have been a reconnect
  loop rather than one failed request.

Two traps for anyone writing pub/sub tests here, both of which produce a green
"nothing leaked" result for reasons unrelated to the code:
`get_message(ignore_subscribe_messages=True)` returns `None` for each subscribe
confirmation rather than skipping past it, so an undrained subscription reports
an empty channel forever; and driving the stream over httpx's `ASGITransport`
hangs, because closing the stream never makes `request.is_disconnected()` true
and the generator loops until the suite is killed.

Original diagnosis follows, kept because it is the reasoning behind the channel
layout.

Raised: 2026-08-01. The rest of this entry's original subject -- `jobs`,
`uploads`, `search`, `pipelines`, `ncbi` -- was closed on 2026-08-01 in
`414f146`, `67931f4` and `a99e044`. What follows is what is left.

**`events.py` (1 route, 0 scoped).** The SSE stream subscribes to a single
global Redis channel (`keys.EVENTS`, `events.py:30`) and forwards every
payload to every client. The word `owner` does not appear in the file. Once
two profiles are in use, profile B's browser receives job-progress and
object-created events for profile A's files, filenames included.

Two shapes, and the difference matters: stamping an `owner` on the payload and
filtering server-side means a publisher that forgets leaks silently, which is
the same failure the dedup-key trap had. Per-owner channels
(`keys.EVENTS + ":" + owner`) mean a publisher that forgets emits into a
channel nobody reads -- a missing event rather than a leaked one. Failing
closed is worth the extra channel. `publish_event` has call sites in
`queue/queue.py`, `queue/results.py` and the executor; audit all of them.

**`schedules.py` (5 routes, 0 scoped).** Probably correct as-is -- these read
like system-level cron entries (GC, file verification) rather than user data --
but nobody has said so. Either scope it or document it as deliberately global
in the route docstrings, the way `search.py`'s `/metadata/schemas` does.

Neither is reachable today: nothing in the frontend sends
`X-BioFlow-Profile`, so no request resolves a profile at all. Both become
reachable the moment the picker ships, which is why they should close first.

### What the rest of the sweep found, worth remembering

The `TODO(profiles)` marker count was a **bad measure of completeness**. A
marker was only left where a service call already took an `owner` and the
route had nothing to give it, so routers whose service never took one carried
no marker. "0 markers" read as done while `/api/v1/jobs` still answered with
100 jobs and no header.

Seven unscoped **writes** were found, none of them marked, each reachable by
guessing an id: `cancel_run`, `cancel_job`, `retry_job`,
`PUT /uploads/{id}/chunks/{i}` (writes bytes into another profile's
in-flight file, surfacing much later as a digest mismatch), `abort_upload`,
and both `bulk_update_metadata` / `bulk_update_tags`.

And a warning for anyone adding isolation tests here: **a test asserting only
"profile B sees nothing" also passes against a route hardcoded to `"local"`,**
because A's rows are not under `"local"` either. Ten such tests were written
and shipped green across three passes; every one was caught only by mutating
the route and noticing the test survived. Assert both directions -- A sees its
own rows, and B does not.

Touches: `backend/app/api/v1/events.py`, `backend/app/api/v1/schedules.py`,
`backend/app/queue/queue.py` (`publish_event` call sites).

## Upload dedup never fires: the client never hashes — FIXED

Fixed 2026-08-02. `frontend/src/lib/upload.ts` now hashes the whole file
before opening a session and sends the digest as `client_sha256` on
`createUpload`; `upload_service.create_session` and the rest of the
server-side and UI-consumer wiring this entry describes were already correct
and needed no change.

**What the fix needed that a naive read of this entry might miss.**
`crypto.subtle.digest` -- already used in this file for per-chunk digests --
is not incremental; it takes one complete buffer, which is unusable for a
file this app's own upload module docstring sizes at 30 GB. There is no
streaming/incremental option in Web Crypto, and nothing else in the frontend
did incremental hashing, so `hash-wasm` was added (zero runtime
dependencies of its own) and fed fixed-size slices of the file the same way
chunks are already read for upload. Hashing runs in a Web Worker --
`frontend/src/lib/hashWorker.ts`, the first worker in this codebase -- so a
multi-gigabyte hash does not block the tab; Vite 6's built-in `?worker`
import syntax needed no config changes.

**The "cost more than it saves" question this entry left open was resolved
by measuring, not guessing.** WASM SHA-256 hashes at roughly the same order
of magnitude as this app's local network transfer speed or faster (a 1.5 GB
in-memory synthetic file's hashing phase was observable but brief; smaller
files raced through it too fast to catch mid-flight), so hashing does not
become the bottleneck it might have been with a slower pure-JS
implementation. The dead branch was worth keeping, not deleting.

**Verified end to end in the running app**, not just by unit test: a 5 MB
file uploaded normally with `client_sha256` now present on the session
document (confirmed via the API's Mongo query log); re-uploading the
identical bytes hit `upload_service._try_dedup`
(`upload_dedup_preflight` logged with the matching digest), created a new
`DataObject` immediately, and sent **zero** chunk `PUT` requests -- confirmed
by absence in the request log, not just a passing assertion. The tray
correctly distinguished "Complete" (first upload) from "Added to your
library — already stored locally" (dedup hit). A `"hashing"` phase label
("Checking for existing copy…") was added to `UploadTray.tsx` and confirmed
to render during the hashing phase on a large-enough file. Cancelling
mid-hash was handled cleanly via the existing `AbortSignal` plumbing, with
no orphaned worker or unhandled rejection.

Backend suite green throughout (2,315+ tests, no backend files touched).

Original entry follows.

Raised: 2026-08-01, while wiring the profiles frontend.

`upload_service.create_session` short-circuits and returns `dedup_hit: true`
when the client's digest already names a blob in the store
(`upload_service.py:99`). `api/client.ts:229` accepts a `client_sha256`
parameter for exactly that. But `frontend/src/lib/upload.ts` calls
`createUpload({project_id, filename, total_size})` and **never sends it**, so
the branch is real, wired end to end, and unreachable from the UI.

Confirmed by trying it: re-uploading the exact bytes of a blob already in
`/data/objects/` chunked and transferred normally rather than deduplicating.

Two things follow. The user pays a full transfer for content the machine
already has -- which is the whole point of content-addressed storage, and the
larger the file the more it costs. And the "already stored" message in the
upload tray and in `hooks/useUploads.ts` can never appear, so the strings are
untestable by any means other than driving the store directly.

The fix is to hash the file client-side before opening the session and pass
`client_sha256`. Worth checking what that costs for a multi-gigabyte FASTQ on
the main thread -- a Web Worker or a streamed `crypto.subtle.digest` may be
needed, and if hashing turns out to cost more than the transfer saves for
local files, the honest conclusion may be to delete the dead branch instead.
Either way the current state is the worst of both: the code implies a feature
that does not run.

Touches: `frontend/src/lib/upload.ts`, `frontend/src/api/client.ts`,
`backend/app/services/upload_service.py` (no change expected, but it is the
other half of the contract).

## Sharing between profiles

Depends on profiles. Share a file with another profile without copying the
bytes — which the storage layer already supports: a second `DataObject` with a
different `owner` pointing at the same digest, with the existing refcount
governing lifetime. The open work is policy and UI, not storage: how a share is
offered and revoked, whether the recipient sees it in their own explorer or a
separate shared area, and what happens to a share when the owner deletes their
copy (`GC_GRACE` in `blob_service.py` is currently the only thing between a
refcount reaching zero and the bytes being unlinked).

## Non-local / remote NCBI data — SPECCED

Design: `docs/superpowers/specs/2026-07-31-remote-data-design.md` (2026-07-31).

Keep an NCBI download remote rather than ingesting it: store a pointer, fetch
just-in-time when used. The file explorer badges files `Local`, `NCBI`, or both,
and an Actions entry drops the bytes of anything re-fetchable while keeping its
metadata, QC reports and provenance.

What the design settled:

- **The fetch is a real job**, gated by `depends_on` and reusing the
  `build_index` → `align_reads` pattern — so a multi-gigabyte download is
  visible in the queue with its own progress instead of making a pipeline job
  look hung, and a failed fetch names itself as the reason.
- **`locality`, not a new `ObjectStatus`.** This was the trap. `ObjectStatus`
  `.READY` is guarded in ~14 places, and two of them — the reference picker
  (`api/v1/pipelines.py:529`) and the Actions rules
  (`suggestion_service.py:654`) — *filter collections* on it. A remote file
  carrying a new status would silently disappear from both. So `status` keeps
  meaning "is this file understood" and a new `locality` field says "are its
  bytes here", leaving every existing guard working unchanged.
- **No blob row until first fetch**, since `Blob.id` *is* the SHA-256 and the
  digest of an un-downloaded file is unknown.

Two things came out free: `_resolve_readable`
(`services/pipeline_service.py:129`) is a single chokepoint that already
branches on storage mode, so no handler or runner is touched; and
`qc_reports/`, `bam_stats/` and `vcf_stats/` are keyed by object id outside
`objects/`, so dropping a blob cannot disturb them.

## Helper install program

A native executable that removes `docker compose` from the user's vocabulary.
On launch it checks whether Docker is installed and running, then whether
BioFlow is already up. If not installed, it walks through a first-run setup:
where storage lives, where the program is installed (a good default), which port
to serve on — then writes a `docker-compose.yml` in the install directory and
offers a Run button. Thereafter it is a launcher and a status check, with Run and
Shutdown buttons. Upgrading (bumping container image tags) is explicitly a
later generation.

**The installer does not create the initial profile.** The original note had it
collecting one during setup, but at install time the stack is not running and
there is no API to create a profile against. The installer would have to know
the `Profile` schema, hash a password, and write a seed file the backend parses
on boot — duplicating logic that already exists behind the API, and adding a
second way to create a profile that could drift from the first.

Instead the installer's job ends at "the stack is up and a browser is pointing
at it", and profile creation belongs to the web UI's first-run screen — which
the profiles design already requires for the empty-database case, and which is
also where a *second* profile gets added later. One code path, in the place that
already owns it.

So the installer collects only what the compose file needs: storage location,
install directory, and port. That leaves it with no dependency on the profiles
feature at all, and the two can be built in either order.

**Offer a "full install" option that pre-pulls optional tool images.** Added
2026-07-31, while designing DeepVariant. Some tools are too large to bake into
the backend image -- DeepVariant's is 8.83 GB on disk, larger than the whole
rest of the stack -- so they are pulled on demand the first time a user launches
one. That trades disk for a network dependency at first use, which is wrong for
someone about to work offline.

The installer is the natural place to resolve it, because it is the one moment
the user is already online, already waiting, and already answering questions
about disk. A checkbox ("download optional tools now -- adds ~9 GB, lets
DeepVariant run offline") makes the trade explicit and one-time instead of
surfacing it mid-analysis.

Note this means the installer needs a list of optional images and their sizes,
which should come from the backend rather than being duplicated in the
installer -- otherwise adding a future optional tool means shipping a new
installer. An endpoint returning the optional-image manifest is the cheap
version.

Also note this is a different *kind* of artifact from everything else here: a
native desktop app, outside this repo's Python/React/Docker toolchain, needing
its own repo and build/signing story.

## Software help page: filter by column — DONE

Built 2026-07-31 in `43cf771`. Clicking a column head narrows the whole page —
grid *and* entries — to that pipeline; clicking again or "Show all" restores it.

The clickable tool names asked for alongside this already worked: matrix rows
have linked to `#tool-<name>` against ids on the entry headings since the matrix
was written, which `ToolMatrix`'s docstring describes as the point of it.

Two decisions worth keeping: membership is `pipelines.includes`, not
`pipelines[0] === type`, so fastp, samtools and bcftools each appear under both
their roles — a QC filter that hid samtools would be lying about the toolchain.
And availability is deliberately absent from the predicate, so an uninstalled
tool is still listed for the job it would do.

## Post-install tool downloads

Raised: 2026-08-01, requested.

Instead of baking all tools into the container image, allow users to install
some tools after deployment (similar to the DeepVariant model). This could mean
either installing into a sidecar container or pulling a separate tool-specific
container depending on the tool.

This trades smaller initial image size and faster startup against network
bandwidth at first use, which is the right tradeoff for tools that are large
(DeepVariant's ~3 GB is already a precedent) or rarely used. The installer
already has a "full install" option to pre-pull optional images; this extends
that model to a live install flow in the running application.

Scope this against which tools are candidates (size, frequency of use, stability
of external source) and whether the sidecar or separate-container approach works
better for each. Per CLAUDE.md: `suggestion_service.py` must recognize any new
dispatch path.

## Observability in tools: progress reporting and resource transparency

Raised: 2026-08-01, requested.

When a long-running job executes, the user sees "running" but not progress
within it. For some tools we can parse output (`minimap2`, `bwa-mem2` write
progress to stderr); others we would need to instrument the source or intercept
signals. The goal is to answer questions like "% complete" or "N of M chunks
processed" and surface that in the UI during job execution.

**Architecture sketch:** A central observability server (in a container), running
a pub/sub broker, where tools report their progress and the API queries it on
demand. Tools could push to it either natively (if instrumented) or via wrapper
scripts that parse output and emit metrics. This is a needs-brainstorming-and-spec
decision; the pub/sub model is one option but may not be the right one.

Consider what metadata each tool can realistically report (some have seconds-left
estimates, others only have bytes processed), and whether the server should be
persistent (survives container restart) or ephemeral.

## Resource limits and intelligent enforcement

Raised: 2026-08-01, requested.

Allow users to set global resource constraints (max memory, max CPU %, max CPU
threads) via settings, and intelligently enforce them on running jobs. The open
question is how much is within this application's control.

Today `JobResources` on a job declares `cpu` and `mem_mb` requested, and the load
governor's admission checks gate work based on container availability. Enforce
means either:

1. **Container-level cgroups.** Tell Docker how much memory and CPU the `api` and
   `worker` containers may consume, and let the kernel enforce it. This is how
   Docker already isolates containers; setting limits here is configuration, not
   new code.
2. **Per-job subprocess limits.** Some handlers shell out to tools; those could
   be wrapped with `ulimit` or similar to cap their consumption. Finer-grained
   but does not help with tools invoked via containers or as native binaries.
3. **Load governor thresholds.** The admission checks already refuse work when
   system load or free memory crosses a threshold. Tighten these based on user
   settings.

Options 1 and 3 are complementary and doable. Option 2 is tool-specific and
fragile. Start by clarifying which resources matter most (memory is usually the
constraint; CPU % is a softer signal) and whether the user is asking for "never
use more than N GB" (admission) or "gracefully degrade when close to N GB"
(monitoring).

Touches: `backend/app/models/job.py`, `backend/app/queue/governor.py`,
`docker-compose.yml`, `docker-compose.override.yml`.

---

# Deferred findings

See CLAUDE.md, "Closing out a TODO entry", for what to do when one of these
lands. Short version: mark it `— FIXED` with a note, keep the body, and never
trust a plan's checkboxes as evidence it shipped.

## Maintenance jobs belong to whichever profile adopted "local" — FIXED

Fixed 2026-08-02. `scheduler.tick` and `scheduler.run_now` now enqueue with
`owner=keys.SYSTEM_OWNER`, and `list_jobs` unions that owner in:
`{"owner": {"$in": [owner, keys.SYSTEM_OWNER]}}`. So maintenance is visible to
every profile and owned by none.

**What the implementation did differently from this entry.**

- **It took the first candidate, which this entry recommended against.** The
  entry called the `owner="system"` route "honest, but the jobs list becomes
  two queries" and leaned toward filtering maintenance job *types* out
  instead. **The two-queries objection was simply wrong** -- it is one `$in`
  on one find. With the only stated cost gone, the entry's own argument
  against filtering decides it: filtering "hides them from everyone including
  the person who asked", and `run-now` is precisely when they want to see it.
- **The event-stream half needed no code at all.** Events already route by
  `Job.owner`, so stamping `system` puts these on `bp:events:system`, which
  every client already subscribes to. This entry treats the jobs list and the
  event stream as two consequences needing two fixes; they were one.
- **Nothing was invented.** `keys.SYSTEM_OWNER` already existed, from the SSE
  scoping work that raised this entry -- the fix is jobs becoming *consistent*
  with events, not a new concept. That is also the argument for preferring it:
  a second mechanism for "belongs to the installation" would have been the
  real cost, not a query.
- **`_owned_job` was deliberately left as strict equality**, so cancel, retry
  and log stay owner-exact. Listing a maintenance job answers "what is this
  machine doing"; cancelling one is an installation-level act that a guessed
  id should not reach. `run_now` is the deliberate door for firing them and
  there is no matching door for killing one. Asserted, since "visible
  therefore mutable" is the natural next assumption.
- **No migration for existing `owner: "local"` maintenance job documents.**
  Jobs are transient queue records with a TTL index; the historical ones aging
  out of the adopted profile's list is not worth a migration in a repo with no
  migrations mechanism.

**Tests.** `backend/tests/queue/test_scheduler_owner.py` is new -- and so was
the coverage: `grep` found **no scheduler tests at all** before this, so
`tick` and `run_now` were entirely unexercised. Plus three cases in
`test_route_owner_scoping.py::TestJobsRouter`. Mutation-checked: reverting the
route to `{"owner": owner}` and the scheduler to `"local"` fails exactly the
four new tests and nothing else.

The count had to be obtained by counting progress characters, because
`run-worktree-tests.sh` was swallowing pytest's summary line at the time --
independently diagnosed and fixed on main the same evening in `8b7d530`
(the appended `-q` landed on top of the caller's own, and `-qq` drops the
"NNNN passed" line while still exiting 0). Worth knowing only as a reminder
that "exit code 0" and "read the count" were briefly the same information.

The trap specific to this fix, and why one test asserts three facts at once:
`{"owner": {"$in": [owner, SYSTEM_OWNER]}}` has two mistypings -- swapping
`owner` out, or dropping it from the list -- that both turn every profile's
list into the maintenance list, and **both still pass a test that only checks
the system job is visible to both profiles.** Only asserting A's own job
present *and* B's job absent *and* the system job present, in one listing,
pins the query shape. Same shape as the warning this file already records
about "profile B sees nothing" passing against a hardcoded `"local"`.

Original diagnosis follows.

Raised: 2026-08-01, while scoping the SSE stream. Pre-existing; nothing in that
change caused it, and nothing in that change is the right place to fix it.

`scheduler.tick` and `scheduler.run_now` enqueue GC and file verification with
a hardcoded `owner="local"` (`backend/app/queue/scheduler.py:123`, `:161`),
which is correct in that this work belongs to the installation. But `"local"`
is not a neutral value: it is the owner string of whichever profile adopted the
pre-profiles library, so those jobs land in exactly one real person's library.

Two visible consequences, both mild until someone is confused by them.
`/api/v1/jobs` is owner-scoped, so GC and verify jobs appear in the adopted
profile's job list and in nobody else's -- a second profile pressing "Run now"
on a schedule watches the job vanish. And since the job events now route by
`Job.owner`, that profile's event stream is the only one that sees them live.

Not obviously wrong to fix and not obviously worth fixing. The candidates: give
maintenance jobs `owner="system"` and let `/api/v1/jobs` union the system owner
in (honest, but the jobs list becomes two queries); or leave the jobs alone and
filter maintenance job *types* out of the jobs list entirely (cheaper, hides
them from everyone including the person who asked). The second is probably
right, since a maintenance job is not something a user chose to run -- except
via `run-now`, which is precisely when they want to see it.

Touches: `backend/app/queue/scheduler.py`, `backend/app/api/v1/jobs.py`,
`backend/app/api/v1/schedules.py` (whose module docstring records this).

## Results should be the first tab

Raised: 2026-07-31, requested.

`tabsFor` in `frontend/src/components/DetailPanel.tsx` (~line 271) builds the
tab list Quality, Results, Metadata, Actions, and Results is only pushed when
`obj.format.kind` is `bam`, `vcf` or `bcf`. Put Results first for the objects
that have it.

Two things not to break. The tab id is persisted in the URL alongside `?sel=`,
deliberately: one `results` id across all three formats means a link stays on
Results when the selection moves from a BAM to the VCF called from it. And the
existing order is not accidental -- the docstring above `tabsFor` argues the
panel should open on "is this file good?". Reordering is a decision to
overrule that, so update the docstring to say what the new order is for rather
than leaving the old rationale sitting above contradicting code.

Objects with no Results tab keep opening on Quality, so this changes the
first-open tab only where results exist.

Touches: `frontend/src/components/DetailPanel.tsx`.

## Help → Software: two columns, one section per page

Raised: 2026-07-31, requested.

`frontend/src/components/HelpSoftware.tsx` renders `TOOL_META` as a single
column. Two columns for the descriptions, with each section starting on its own
page break.

"Page break" cuts two ways here and the answer changes the CSS: for *print*
it is `break-before: page` inside an `@media print` block; for *screen* it is
a section that starts at the top of the viewport rather than flowing on. This
page is a reference people read on screen and occasionally print for a methods
appendix, so most likely both -- `break-inside: avoid` on each tool entry so a
tool is never split across a column or page boundary, which is the failure the
two-column layout otherwise introduces.

Note `TOOL_META` is rendered directly and `test_every_tool_is_documented`
requires every entry to carry `homepage`, `citation`, `license` and `usage`, so
the column layout must not depend on any of those being short.

Touches: `frontend/src/components/HelpSoftware.tsx`, `frontend/src/styles.css`.

## Aligners: STAR and DRAGMAP — STAR FIXED, DRAGMAP still open

STAR shipped 2026-08-01 (`Merge STAR aligner support with directory-shaped
index layout`, plus a same-day follow-up fix). DRAGMAP was considered and
deliberately deferred; the rest of this entry stands for it.

**What shipped.** `Aligner.STAR`, `SidecarRole.STAR_INDEX`, `StarParams`, a
registry spec with four biology fields, `rna-star` in the Dockerfile, and the
directory-shaped `IndexLayout` this entry predicted would be needed. Members
are stored *flat* as `<reference>.STARindex.<member>` and reassembled into a
real `--genomeDir` in `aligners.materialize`, so the sidecar model, the
database records and `owns_sidecar` were left untouched -- rather than the
"directory-shaped branch" through the existence checks this entry imagined.

**Where the implementation departed from this entry.**

- **No GTF.** This entry says STAR "needs a GTF/GFF3 at index time for splice
  junctions". It does not: STAR discovers junctions de novo, and the shipped
  index is built without an annotation. Measured on real yeast data: 9,818
  splices found with no GTF supplied. Annotation-aware indexing (`--sjdbGTFfile`,
  `--sjdbOverhang`) remains genuinely useful and genuinely unbuilt -- and the
  object model has no annotation concept yet, which is the larger part of that
  work. The yeast project already holds `GCF_..._genomic.gtf` files, so the
  input exists whenever someone wants it.
- **`JobResources` — asked for by this entry, missed by the STAR change, then
  done the same day.** Both launch sites now size the reservation from the
  registry's `MemoryModel` and the reference, via
  `pipeline_service.declared_align_mem_mb`. Measured on a 3.1 Gb human
  genome, against the flat 8.0 GB every one of these used to declare:

      aligner     human align  human build
      bwa-mem2          11.3G        13.0G
      minimap2          10.8G         9.0G
      bowtie2            7.9G         9.7G
      hisat2             8.8G        16.0G
      star              34.7G        36.4G

  So a STAR index build was admitted believing it needed a quarter of what it
  does. Small genomes were over-declared in the same breath: a yeast STAR
  alignment reserves 6,038 MB, verified on a real launch. Two details that
  are easy to get wrong -- the index build passes `sort_memory_mb=0` because
  it runs no samtools sort, and the alignment recomputes with
  `building_index=False` rather than reusing the launch estimate, which
  answers the different question "does the whole operation fit" and would
  otherwise charge every alignment against an unindexed reference for
  HISAT2's 4x build multiplier. The handler-level `@handler(resources=...)`
  values stay at 8192 and are now only a fallback for the development
  enqueue route in `api/v1/jobs.py`.
- **No suggestion rule, deliberately.** This entry (and CLAUDE.md) require
  `suggestion_service.py` to gain a rule that can pick a new tool. STAR
  intentionally has none: HISAT2's ~4 GB index beats STAR's ~30 GB resident on
  this hardware, so a STAR card would be blocked by the estimator on most
  machines. The reasoning is recorded at the rule and asserted by
  `test_rna_seq_stays_on_hisat2_even_with_star_installed`, since preferring
  STAR is the natural next edit.
- **Not built with or before differential expression**, which this entry
  suggested. DE shipped separately on
  `claude/differential-expression-tool-75cbcf` and is now closed out below --
  it consumes STAR's BAMs without needing anything from this entry, which is
  the evidence that splitting them was right. One gap the split left: STAR
  records no `--rna-strandness` equivalent, so a STAR-aligned BAM reaches
  counting with no strandedness to infer and falls back to unstranded.

**A third hand-maintained mapping, not in this entry's list.** Registering the
tool was not half the change but two thirds of it. `results._SIDECAR_ROLES`
was a hand-listed role allowlist, and `star-index` was missing from it: the
first real run stored *zero* of the eight index files while `build_index`
still reported success, and the failure surfaced later as STAR complaining its
genome directory did not exist. The full unit suite was green throughout,
because every fixture fed the appliers roles already in the allowlist. It is
now derived from `SidecarRole` so the next role cannot repeat it. CLAUDE.md
names `suggestion_service` and `TOOL_META` as the mappings a new tool must
reach; this was a third, and worth adding to that list.

**Three defaults depart from STAR's own**, each because the alternative fails
silently: `--outSAMunmapped Within` (STAR discards unmapped reads, which makes
flagstat report 100% mapped whatever the truth is -- the real run read 95.67%,
which is the evidence it works), `--readFilesCommand zcat` for gzipped input
(STAR neither sniffs nor infers from the extension), and index sizing computed
from the reference's `.fai` (STAR's defaults build an index that maps almost
nothing on a small genome *while exiting 0*).

**Verified** end to end in the running app, not only by unit test: 2,176,214
Illumina reads against the yeast genome, 95.67% mapped, 86.64% properly
paired, `Log.final.out` harvested into the job log. Two facts were corrected
by running STAR 2.7.11b rather than recalling it -- genomeGenerate without an
annotation writes eight files, not eleven (requiring the phantom three would
have failed every index build), and the version probe truncated `2.7.11b` to
`2.7.11`, naming a release that never ran.

**Known rough edge, not fixed.** STAR reports MAPQ 255 for uniquely-mapped
reads while every other aligner here uses the 0-60 scale, so the alignment
report shows a mean MAPQ of ~247 against bwa-mem2's ~50 for the same reads.

Raised: 2026-07-31, requested.

Two additions to `Aligner` in `backend/app/pipelines/aligners.py`, which today
holds `BWA_MEM2`, `MINIMAP2`, `BOWTIE2`, `HISAT2`.

**STAR** is the splice-aware aligner RNA-seq wants, and is the dependency for
the differential-expression pipeline below -- build that first or together.
Its index is a *directory* of files with fixed names (`SA`, `SAindex`,
`Genome`, ...), not a set of suffixes appended to the reference path. Every
existing aligner follows the suffix pattern, and `aligners.py`'s module
docstring is explicit that index naming is a first-class concern with its own
tests. STAR breaks that assumption, so `build_index_command` and the index
existence checks need a directory-shaped branch rather than another suffix
tuple. STAR also needs a GTF/GFF3 at index time for splice junctions, and wants
~30GB RAM for a human genome -- this is the case that should carry a real
`JobResources` declaration.

**DRAGMAP** is a short-read aligner whose draw is Illumina DRAGEN
compatibility. Check the arm64 story before committing to it: it is the same
class of problem as DeepVariant below, and bwa-mem2 already needed a
from-source sse2neon build (`backend/scripts/build-bwa-mem2-arm64.sh`) to work
on Apple Silicon at all.

Per CLAUDE.md, registering either tool is only half the change --
`suggestion_service.py` must gain a rule that can pick it, and `TOOL_META`
needs `homepage`/`citation`/`license`/`usage` filled in or
`test_every_tool_is_documented` fails.

Touches: `backend/app/pipelines/aligners.py`,
`backend/app/pipelines/aligner_registry.py`,
`backend/app/pipelines/align_runner.py`, `backend/app/pipelines/tools.py`,
`backend/app/services/suggestion_service.py`, `backend/Dockerfile`.

## STAR: annotation-aware indexing (`--sjdbGTFfile`)

Raised: 2026-08-01, split out of the STAR entry above so it is findable. That
entry's heading says FIXED, which is a reason to skip reading it, and this is
open work buried in its body.

STAR ships indexing *without* an annotation. Junctions are found de novo and
that works -- 9,818 splices on real yeast data with no GTF -- but supplying
one improves sensitivity for junctions with few supporting reads, which is
the case RNA-seq cares about most.

**This is now unblocked.** The blocker was that the object model had no
annotation concept; the differential-expression merge brought one --
`pipeline_service.annotations_for_project` and `resolve_annotation`, plus a
`needs_annotation` contract the dialogs already consume. STAR's index build
can use the same resolution featureCounts does.

Two wrinkles specific to STAR. `--sjdbOverhang` should be read length minus
one, which ties the index to the reads it will be used with -- and indexes
here are cached per reference with no read-length dimension, so either accept
the default 100 (fine for typical 100-150bp Illumina) or encode the overhang
in the sidecar name. And an annotation-built index writes extra files
(`exonInfo.tab`, `geneInfo.tab`, `transcriptInfo.tab`, `sjdbList.out.tab`)
that `aligners.STAR_MEMBERS` does not list, so that tuple becomes conditional
on whether a GTF was supplied. Verify by running genomeGenerate with a GTF and
listing the directory rather than predicting it -- predicting it is what got
the no-GTF file list wrong the first time.

## STAR reports MAPQ 255, which the alignment stats present as a 0-60 score

Raised: 2026-08-01, observed on a real run. Also filed as a background task.

STAR uses MAPQ 255 for "uniquely mapped" (and 3/1/0 for 2/3-4/5+ loci), while
every other aligner here uses the conventional phred-like 0-60 scale, where
255 means "unavailable" per the SAM spec. Measured on a real yeast alignment:
`mean_mapping_quality: 246.59`, `mapq_histogram` with 193,348 reads at 255.

So the same reads aligned with STAR and with bwa-mem2 produce ~247 against
~50, which reads as a dramatically better alignment rather than a different
encoding. A histogram axis running to 255 also flattens the other aligners'
distributions.

Computed in `pipelines/bam_stats_runner.py`, displayed in
`AlignmentReport.tsx` / `BamResults.tsx`. Prefer labelling the scale or
bucketing 255 as "unique" over rescaling STAR's values to look like bwa's --
the number should stay honest.

## The align dialog offers aligners the reads cannot use

Raised: 2026-08-01, hit while testing STAR.

Feeding `SRR39891651.trimmed.fastq` (PacBio HiFi, reads up to 3,550 bp) to
STAR through the align dialog was accepted, queued, and failed ~40s later
with `EXITING because of FATAL ERROR in reads input: quality string length is
not equal to sequence length` -- a message that reads as a corrupt FASTQ
rather than "this is a short-read aligner".

The information to prevent it is already present and already used elsewhere:
`suggestion_service` reads `qc_read_chemistry` and correctly recommends
`minimap2 map-hifi` for that file. The dialog just does not consult it. The
same hazard applies to bowtie2 and HISAT2, which are equally short-read.

Deliberately a warning rather than a block -- CLAUDE.md's position is that the
dialog can afford a looser filter than the suggestion engine because a human
is choosing. But "a human is choosing" only holds if the human is told.

## Audit the hand-maintained registries a new tool must reach

Raised: 2026-08-01, after the third instance in one change.

CLAUDE.md names two mappings a new tool must be added to -- `TOOL_META` and
`suggestion_service`'s rules. Adding STAR found a third,
`results._SIDECAR_ROLES`, and missing it cost a `build_index` that reported
success while storing none of the eight index files. The full suite was green
throughout, because every fixture fed the appliers roles already in the
allowlist.

The pattern is a module-level dict keyed by something an enum already
enumerates, where a missing key is skipped rather than raised.
`_SIDECAR_ROLES` is now derived (`{role.value: role for role in
SidecarRole}`), which makes that class of drift impossible rather than merely
fixed. Worth walking the others and deriving the ones that can be:
`_QC_STATS_PLATFORM`, `EXTENSION_MAP`, `_TOKEN_SEQUENCE_TYPES`,
`_EXTENSION_SEQUENCE_TYPES`, `metadata/schemas.py`'s `FORMAT_FIELDS` and
`ROLE_FIELDS`, `assembly_components.COMPONENTS`.

Not all of them should be derived -- some genuinely hold information the enum
does not, and a passthrough would be worse than an allowlist. The test to add
where deriving is wrong is the one `_SIDECAR_ROLES` lacked: every enum member
is handled, asserted directly.

## `_APPLIERS` dispatch passed `owner=` while 11 appliers took `launching_owner=` — FIXED

Fixed 2026-08-01, same day, after the user asked for it directly. Also noted
in `docs/superpowers/specs/2026-07-31-profiles-design.md`, "Two things found
while this was mid-flight" -- that spec is the better read for whoever
continues the profiles work, since it also covers a related
`docker-compose.override.yml` change from the same day.

All fourteen appliers now take `owner`. The two chained launches inside them
(`launch_summary` off QC, `launch_bam_stats` off `index_bam`) still pass the
launching profile deliberately, and say so where they do it.

**The naming was not an accident, which is why this needed care rather than a
blind rename.** `apply`'s docstring argued that appliers inheriting an owner
from their parent should name the parameter `launching_owner` and not read it,
to mark inheriting as a decision rather than an oversight. That intent was
sound; the mechanism was not. A parameter named differently from the keyword
dispatch uses is not a signal, it is a `TypeError` at the moment the job
finishes. The docstring now records that, and the inherit-from-parent decision
is documented in the appliers that make it, where a reader is already looking.

**Why the existing tests were green.** `test_results_owner.py` covers exactly
this area and passed throughout. Every test in it called an applier *directly*,
supplying `launching_owner=` itself, so it proved the appliers worked while
never touching the dispatch that could not reach them. The one test that does
exercise the executor stubs `apply` out entirely. A green suite over both
halves of a seam that could not connect.

The new `test_every_applier_accepts_the_keyword_apply_dispatches_with`
inspects every registered applier's signature against the keyword `apply`
dispatches with. It fails loudly on the next one that drifts.

Verified end to end after the fix: a DeepVariant run against `DRR1066343.bam`
produced a registered VCF (`status: ready`, `owner: local` inherited from the
parent BAM) with its `.tbi` sidecar and full provenance, and zero
`result_apply_failed` in the worker log.

Original entry follows.

Raised: 2026-08-01, found by running a real variant-calling job end to end.

`results.py` (~line 62) calls `handler(result, owner=owner)`. Fourteen appliers
have been migrated to that keyword; **eleven still declare
`launching_owner: str`** -- including `_apply_call_variants`,
`_apply_align_reads`, `_apply_trim_reads`, `_apply_run_qc`,
`_apply_build_index`, `_apply_index_bam` and `_apply_annotate_variants`.

Each raises `TypeError` the moment its job finishes:

```
result_apply_failed error="_apply_call_variants() got an unexpected keyword
argument 'owner'" type=call_variants
```

**The job still reports `succeeded`.** The tool ran and wrote its output to the
job's tmp directory; only *registration* fails -- so the pipeline looks like it
worked and the file never appears in the library. That is the worst available
shape for this failure: nothing surfaces to the user, and the missing output
reads as "the pipeline produced nothing" rather than "a keyword argument is
wrong".

Confirmed by running DeepVariant against `DRR1066343.bam`: job succeeded, VCF
and `.tbi` present under `/data/tmp/variants/<job>/out/`, no `DataObject`
created. The same must currently be true of alignment, trimming and QC.

Left alone deliberately: this is a partially-completed refactor from the
profiles work, and finishing someone else's migration mid-flight risks
conflicting with the change still in progress. The fix is mechanical -- rename
the parameter in the remaining eleven -- but it belongs to whoever owns that
migration, and it wants a test that a finished job actually produces its
object, not merely that the handler returns.

Touches: `backend/app/queue/results.py`.

## DeepVariant: refused for a reason that is no longer true — FIXED

Shipped before 2026-08-01; **found stale by the Linux verification pass on
2026-08-01**, which is when this note was added. Both refusal messages are
gone -- `grep` for "no arm64" / "arm64 Linux build" across `backend/app/`
returns nothing -- and `VariantCaller.DEEPVARIANT` dispatches unconditionally
in `variant_handlers.py` (`_run_deepvariant`) and `pipeline_service.py`.
`TOOL_META` carries the required fields and `tools.deepvariant()` probes the
Docker client rather than a binary.

**The implementation took option 2, not option 1.** This entry recommended
pulling DeepVariant's artifacts into the BioFlow image ("much more in keeping
with how everything else here works") and warned that option 2 would need the
Docker socket, "a real privilege and architecture change this app has so far
avoided entirely". Option 2 is what shipped: `build_deepvariant_command`
assembles a `docker run`, and both `api` and `worker` bind-mount
`/var/run/docker.sock` in `docker-compose.override.yml`, whose comments accept
the privilege increase explicitly on single-user/local grounds. That also made
`host_path_for` necessary -- a sibling container gets its mounts from the
host, so `BIOINFO_HOME_HOST` had to be introduced to translate exactly the
left half of `-v`. None of that machinery would exist under option 1.

**The image is not one image, which this entry assumed it was.** It named the
arm64 community port as "the artifact this project needs", and the code
hardcoded that tag as the default on every platform. On x86-64 that is the
wrong image and fails at `docker run` with "exec format error", inside a job.
Fixed 2026-08-01 alongside the Linux verification: `default_deepvariant_image`
in `backend/app/config.py` now picks `google/deepvariant:1.9.0` on x86-64 and
the port on arm64, and the BF16 fastmath workaround is applied only on arm64
(on x86-64 `TF_ENABLE_ONEDNN_OPTS=0` is a silent slowdown, not a fix). Both
manifests verified single-platform with `docker manifest inspect -v` on
2026-08-01; both repositories verified BSD-3-Clause with
`gh api repos/.../license`.

Original entry follows.

Raised: 2026-07-31, requested. **Unblocked 2026-07-31** -- a native Linux
arm64 build now exists.

`VariantCaller.DEEPVARIANT` already exists in
`backend/app/pipelines/variant_runner.py`, and two paths refuse it with the
same message -- `backend/app/queue/variant_handlers.py` (~line 52) and
`backend/app/services/pipeline_service.py` (~line 1533). Both say it "has no
arm64 Linux build". **That claim is now false and the messages are wrong.**

A community port ships a prebuilt multi-arch image, verified pullable from this
machine on 2026-07-31:

```
ghcr.io/antomicblitz/deepvariant-arm64:v1.9.0-arm64.6
```

`docker manifest inspect` reports `"architecture": "arm64", "os": "linux"`,
~3 GB compressed. Source: https://github.com/antomicblitz/deepvariant-linux-arm64

**Do not reach for the Homebrew tap.** The same author also publishes
`brew tap antomicblitz/deepvariant`, which is a native *macOS* build using
Apple Clang and Metal GPU acceleration. It is the more famous of the two and
the easy thing to find, but it is useless here: this app runs entirely inside a
Linux container, where `brew` has nowhere to run and Metal does not exist. The
Linux arm64 image is the artifact this project needs.

Note also that bwa-mem2's arm64 support is *not* a brew install and is not a
precedent for one -- `backend/Dockerfile` (~line 80) builds it from source with
sse2neon inside the image, having only borrowed the *technique* the Homebrew
formula uses. Nothing in this repo's build touches Homebrew.

The open question is how to invoke it, since it is a separate image rather than
a binary in ours:

1. **Pull the tool into our image.** Copy the built artifacts out of that image
   in a Dockerfile stage. Keeps the "one image, tools on PATH" model every
   other tool follows, so `tools.py` probing and `require()` work unchanged.
   Cost: ~3 GB, and it inherits their build rather than ours.
2. **Invoke the container per job.** The handler shells out to `docker run`.
   Avoids the image bloat but means the API container needs the Docker socket,
   which is a real privilege and architecture change this app has so far
   avoided entirely.

Option 1 is much more in keeping with how everything else here works, and the
3 GB is a one-time image cost on a machine that already stores sequencing data.
Worth checking whether the model files can be fetched separately, since a good
chunk of that size is likely weights.

Whichever route, per CLAUDE.md: `TOOL_META` needs
`homepage`/`citation`/`license`/`usage` filled in (cite Google's DeepVariant
paper, but be accurate that this is a community arm64 port, and check the
port's own license), and `suggestion_service.py` needs a rule that can pick it
or its card will never light up. The two refusal messages must be removed or
made conditional on `TARGETARCH` rather than absolute.

Touches: `backend/app/queue/variant_handlers.py`,
`backend/app/services/pipeline_service.py`, `backend/app/pipelines/tools.py`,
`backend/app/services/suggestion_service.py`, `backend/Dockerfile`.

## Post-assembly QC: BUSCO and QUAST

Raised: 2026-07-31, requested. **Depends on the assembly pipeline below.**

Once assembly produces a FASTA, the immediate question is whether it is any
good, and neither existing QC path answers it -- `qc_stats` is about reads, and
alignment QC needs something to align to.

- **QUAST** is reference-free structural stats: N50, contig count, total
  length, misassemblies when a reference is supplied.
- **BUSCO** scores biological completeness against a lineage-specific ortholog
  set, and reports the numbers a paper quotes (complete / duplicated /
  fragmented / missing). It needs lineage datasets downloaded, which is a real
  storage and provenance concern -- closer to the reference-download machinery
  than to a tool probe.

Both produce facts that belong on the assembly object, so they should land as
facts in the same shape `qc_read_chemistry` and friends use, not as a separate
report format.

The contig-length gap recorded below (longest/shortest contig, never shipped
from the 2026-07-29 todo-batch plan) is the small end of this same question and
could fold into QUAST rather than being built separately.

## Reference-guided assembly: Pilon, RagTag, iVar

Raised: 2026-07-31, requested. **Depends on the assembly pipeline below.**

De-novo assembly first; these three all take an existing assembly plus
something else and improve it.

- **Pilon** polishes an assembly using aligned reads -- so it consumes a BAM
  against the assembly, meaning it needs the *assembly* indexed and the reads
  realigned to it. That makes it the first pipeline whose input is an alignment
  to a previous pipeline's output, which the run-provenance model should be
  checked against before building.
- **RagTag** scaffolds contigs against a reference assembly, giving
  chromosome-scale ordering.
- **iVar** is the amplicon/viral path -- primer trimming and consensus calling
  from an alignment, which is a different enough workflow from the other two
  that it may deserve its own card rather than sharing theirs.

All three are chemistry- and context-dependent enough that
`suggestion_service.py` will need real rules, not just availability checks.

## RNA-seq differential expression — FIXED

Shipped 2026-08-01 on `claude/differential-expression-tool-75cbcf`.
`pipelines/counts_runner.py`, `pipelines/de_runner.py`,
`queue/expression_handlers.py` (`quantify`, `differential_expression`),
appliers in `queue/results.py`, launch paths in `services/pipeline_service.py`,
four routes plus a results-table route in `api/v1/pipelines.py`, and
`QuantifyDialog` / `DifferentialExpressionDialog` / `ExpressionCharts` /
`ExpressionResults` on the front end.

**What the implementation did differently.**

- **The sample sheet needed no home to be built.** This entry's central
  question -- "where does a sample sheet live" -- turned out to be already
  answered: `condition`, `sample_id` and `batch` are `COMMON_FIELDS` in
  `metadata/schemas.py`, and every applier copies metadata forward from reads
  to trimmed reads to BAM to counts. So bulk-tagging six FASTQs at upload
  arrives at the DE dialog as a filled-in design. A `COUNTS_FIELDS` group was
  written and then deleted: it would have shadowed the common `condition` and
  split one concept across two keys. No new document, no new collection.
- **The split is quantify / test, not align / count / test.** Counting is
  per-sample and fits the existing one-object-in model exactly, so it is an
  Actions-tab card like any other. Only the test fans in, and it is the only
  thing that needed new shape.
- **Differential expression got no suggestion card, deliberately** -- the one
  place this departs from the "a new tool needs a rule" instruction in
  CLAUDE.md. A card exists to pre-answer a question, and none of "which
  samples", "which groups", "which way round" can be pre-answered. It is a
  project-level button in `ProjectExplorer` instead, shown once the project
  has counts. `quantify` does have a rule and a test.
- **PyDESeq2, as this entry suggested, and `TOOL_META.usage` says so.**
  Measured rather than assumed: `r-bioc-deseq2` is 110 apt packages plus an R
  runtime; `pydeseq2` is 28 pip packages in the Python the worker already
  runs.
- **featureCounts over HTSeq**, because it consumes the BAM STAR/HISAT2
  already produce and reuses annotations already in the library.

**Three things only real data exposed**, none of which any fixture would have:

- **Annotations carry no role.** All four GFF/GTF objects in the live library
  have `role=None` -- ingest only assigns a role where format cannot answer.
  A rule written against `ObjectRole.ANNOTATION` matches *nothing* in a real
  project while passing any hand-built test. `_is_annotation` filters on
  format. This is the same trap this file already records for the Actions-tab
  rules, hit again in a new place.
- **`-t exon -g gene_id` does not work on NCBI's GFF3.** Across the yeast
  RefSeq annotation's 6852 exon lines: `locus_tag` 6852, `gene` 5790,
  `gene_id` 0. featureCounts stops with "failed to find the gene identifier
  attribute" -- loudly, which is the good outcome. The launch path prefers the
  GTF NCBI ships alongside; the GFF3 fallback groups on `locus_tag`, not
  `gene`, which would silently drop the ~15% of features never named.
  Verified equivalent: the same BAM counted against the GTF with `gene_id`
  and the GFF3 with `locus_tag` gave identical numbers, 733,174 assigned
  fragments over 6,477 genes.
- **`-p` alone silently doubles every count.** In featureCounts 2.x `-p` means
  "the input is paired-end" and still counts *reads*; `--countReadPairs` is
  what switches the unit to fragments. Confirmed on a real BAM: 2,176,214
  reads counted 1,088,107 fragments, exactly half.

**Measurements.** End-to-end on six samples with 200 genes given a known 4x
effect: recall 200/200, precision 0.98, log2 fold changes of 2.0-2.2 against
the injected log2(4) = 2, and PC1 separating the conditions cleanly.
Quantifying one 2.2M-read BAM takes ~10s; the six-sample test ~15s.

**Two bugs manual UI testing caught that the suite did not**, both of the
"looks right, is wrong" kind this file keeps collecting:

- Sorting the results table by fold change *descending* put the untested genes
  first. A `(value is None, value)` sort key does the right thing ascending and
  exactly the wrong thing descending, because `reverse` flips the whole tuple.
  Now partitioned in `de_runner.sort_rows`, with the regression pinned.
- The quantify dialog told the user "this alignment looks single-end" about a
  BAM with 1.9M properly-paired reads, whenever the project held two
  assemblies' annotations: the defaults endpoint skipped deriving parameters
  when it could not resolve the annotation, and the dialog rendered its own
  fallbacks as though they were facts about the file. The server counted it as
  paired regardless, so the screen disagreed with the behaviour.

**Still open, deliberately.** Strandedness is inferred from HISAT2's
`--rna-strandness` only; STAR records no equivalent, so a STAR-aligned BAM
defaults to unstranded and the dialog says so rather than guessing. Salmon and
kallisto (alignment-free) are not wired up. Multi-factor designs are not
supported -- the design is a single `condition` column and one contrast.

**What the pre-merge review added (2026-08-01).** Merged onto main after STAR
landed. The merge was textually clean and 2,315 tests were green, and one bug
survived both -- found the same way STAR's was, by running the thing:

- `QuantifyDialog` overrode the server's refusal to choose an annotation.
  When a project holds annotations for more than one assembly the backend
  correctly returns `annotation_id: null, needs_annotation: true`; the dialog
  applied `?? annotations[0]`, which is alphabetical rather than correct.
  Measured: a BAM aligned against GCF_000146045.2 came up pre-selected with
  the GCA_000146045.2 annotation, whose contigs are named BK006935.2 against
  the reference's NC_001133.9. featureCounts assigned **0 of 1,088,107
  fragments across 0 of 6,425 genes, and the job succeeded.** The dialog now
  refuses to guess and disables its launch button until a choice is made.
  Nothing downstream was wrong -- `low_assignment_warning` already fires --
  the choice was simply being made silently.
- Verified working with the matching annotation: 733,174 of 1,088,107
  fragments assigned (67.38%), 6,302 of 6,477 yeast genes detected.
- pydeseq2 0.5.4 fits in this image, checked on synthetic counts with a
  planted 4x change across 6 samples: all 40 changed genes recovered at
  padj < 0.05, log2 fold change 2.05-2.25 against an expected 2.0. The
  in-app DESeq2 path is *not* yet exercised end to end -- that needs two
  conditions with replicates, and the test project has one RNA-seq sample.

--- original entry ---

Raised: 2026-07-31, requested. **Wants STAR (above) first.**

The full path is align (STAR, splice-aware) → count (featureCounts or HTSeq) →
test (DESeq2 or edgeR), and the last step is the one that does not fit the
current model.

Everything the app runs today is one-object-in, one-object-out. Differential
expression is inherently *multi-sample and grouped*: it needs a design -- which
samples are treatment, which are control -- and that is user-supplied
experimental metadata with nowhere to live right now. Neither `DataObject` nor
`Run` carries a sample-grouping concept.

So the interesting design work is not the tools, it is: where does a sample
sheet live, how does a user express "these six BAMs are two conditions", and
what object does a results table become. Worth brainstorming before planning.

DESeq2 and edgeR are also R, which this image has no runtime for -- either add
R, or use a Python reimplementation (`pydeseq2`) and say so plainly in
`TOOL_META.usage`, since the choice affects whether results match what a
reviewer expects.

## The in-app DESeq2 path has never been run end to end

Raised: 2026-08-01, during the pre-merge review of the entry above. Split out
of it because that entry's heading says FIXED and this is the one part of it
that is not verified.

What *is* verified: featureCounts through the app on real data (733,174 of
1,088,107 fragments assigned, 6,302 of 6,477 yeast genes), and pydeseq2 0.5.4
fitting a model in this image on synthetic counts -- 6 samples, a planted 4x
change, all 40 changed genes recovered at padj < 0.05 with log2 fold changes
of 2.05-2.25 against an expected 2.0.

What is not: `differential_expression` as a job, launched from
`DifferentialExpressionDialog`, over real counts files. That needs two
conditions with replicates -- four counts files minimum -- and the yeast test
project has one RNA-seq sample. Nobody has seen the handler, the applier, the
results object, or `ExpressionResults` / `ExpressionCharts` handle real
output.

The cheapest honest way to close this is a small multi-sample RNA-seq project
rather than synthetic counts: the parts still unexercised are the queue and
UI wiring, and synthetic files would exercise the runner that already has
unit tests. Note the tools' own floor -- `min_replicates: 2` -- so a 2x2
design is the minimum that will launch.

## Generic pipeline workflows (DAG)

Raised: 2026-07-31, requested.

Today each pipeline is a hand-written handler and `Job.depends_on` gates one
job behind another. That gate is real and exercised (`align_reads` waiting on
`build_index`), but it is a per-launch decision made in
`pipeline_service.launch_*`, not a reusable graph.

What this asks for is a user-definable DAG: run QC, then trim, then align, then
call, as one declared unit that survives a restart and reports progress as a
whole.

Two things to settle early, because they shape everything after:

- **Does a workflow instance become an object?** The activity view groups by
  `Run`, and a DAG is naturally a run-of-runs. Extending `Run` beats inventing
  a parallel concept if it can carry the nesting.
- **Failure semantics.** If step three of five fails, does the DAG halt, retry,
  or continue what does not depend on it? The current queue has retries and a
  reaper but no notion of partial workflow failure.

This is the largest item in this file and probably wants decomposing into its
own spec before any plan.

## More LLM usage: pipeline provenance narratives

Raised: 2026-07-31, requested.

The valuable version: given a VCF, generate a plain-language account of
everything that produced it -- which reads, which QC, which trim parameters,
which aligner and version, which caller -- walking the provenance chain back to
the original reads. That is a methods paragraph, generated from facts the
system already recorded rather than from the user's memory.

The chain largely exists. `align_provenance` in `backend/app/queue/results.py`
already copies facts forward so a BAM knows its reads' chemistry, and tool
versions are captured at probe time precisely because "a trimming parameter set
means nothing without the version of the tool that applied it" (the module
docstring in `tools.py`). What is missing is a walker that assembles the chain
and a prompt that renders it.

`backend/app/services/summary_prompt.py` is the existing pattern to follow, and
the summary model runs on the *host* -- containers reach it via
`host.docker.internal`, not `localhost`.

The hard constraint: this output will be pasted into papers. It must never
invent a step or a version. Prefer a narrative assembled from facts with the
model only doing the prose, over asking the model to infer what happened.

Other candidates worth considering under the same heading: explaining *why* a
QC run failed a threshold, and suggesting the next pipeline step in prose
alongside the Actions cards.

## UniProt download — FIXED

Raised: 2026-07-31, requested. **Fixed 2026-07-31.**

Shipped: `backend/app/metadata/uniprot.py` (classify, queries, resolvers),
`backend/app/services/uniprot_service.py` (launch),
`backend/app/queue/uniprot_handlers.py` (`download_uniprot`),
`_apply_uniprot_download` in `backend/app/queue/results.py`,
`backend/app/api/v1/uniprot.py`, and
`frontend/src/components/UniProtDownloadDialog.tsx`. Design and plan in
`docs/superpowers/specs/2026-07-31-uniprot-download-design.md` and
`docs/superpowers/plans/2026-07-31-uniprot-download.md`.

What the implementation did differently from this entry:

- **A separate dialog, not a branch in the NCBI one.** The entry did not say
  which; merging was possible since the namespaces do not collide. Rejected:
  `NcbiDownloadDialog` is already 762 lines carrying two result shapes, and one
  field accepting six identifier kinds plus free text is an overloaded door.
  The proteome/assembly cross-link that merging would have bought is a link on
  the proteome card instead.
- **One `RunKind` and one handler for both download shapes.** The entry
  anticipated proteomes *or* per-protein FASTA. Both turned out to be the same
  `uniprotkb/stream` request differing only in the query string, so the dialog
  branches and the job does not.
- **Almost none of `assembly_handlers.py` was copied.** The entry called this
  "the same shape as the assembly one", which is true structurally and false
  mechanically: no binary, so no `SUBPROCESS` mode, no `run_subprocess`, no
  `tools.require`, no `extend_lease`, no disk pre-flight, no
  `EXTRACTION_FACTOR`, and no zip/checksum/path-traversal handling. The closest
  model for the transport is `structure_lookup.py`.
- **`sources.py` needed the entry but not a version field.** UniProt returns
  `X-UniProt-Release`, a real build number, which that module's docstring says
  data sources do not have. The release is recorded per-download in the
  object's `facts`; `DataSource` is unchanged.
- **`suggestion_service.py` needed nothing**, checked rather than assumed: its
  align rule already filters on `role is ObjectRole.REFERENCE`, so a `PROTEIN`
  object is excluded by a guard that exists because a downloaded assembly's
  `protein.faa` once broke it.
- **The strain picker was designed, built, and then removed.** This is the
  biggest departure and is worth reading before designing anything similar.
  Brainstorming chose "reference proteome by default, expandable to a picker of
  the organism's other proteomes", and the taxon-4932 fallback was called
  mandatory. It was half right. Taxon 4932 does have no reference proteome --
  but the 360 proteomes behind it cannot be downloaded at all: their entries
  are in UniParc, not UniProtKB's searchable index, which is what both the
  count and the download query go through. `proteome:UP000037662` returns 0
  rows and an empty FASTA although its own record claims 5,389 proteins.
  Sampled across *S. cerevisiae*, *E. coli*, *M. tuberculosis*, and
  *S. aureus*: **0 of 100** non-reference proteomes were downloadable. The
  picker could only ever offer dead ends, and it offered them instead of the
  reference proteome that the organism-*name* query finds immediately. A taxon
  with no reference proteome now falls back to its name, not to a list.

Measurements, all against the live API on 2026-07-31, since four
plausible-looking choices were wrong:

- `proteome_type:1` returns **0** for every organism tried. The working
  reference filter is `reference:true`.
- `organism_id:4932 AND reference:true` returns **0** while `organism_id:4932`
  returns **360** -- UniProt attaches yeast's reference proteome to strain
  taxon 559292.
- Non-reference proteomes: **0 of 100** downloadable (see above).
- Human is **20,416 reviewed** against **147,506** including TrEMBL, which is
  why the reviewed choice is shown rather than defaulted silently.
- Sizes: yeast 6,067 proteins / 3.9 MB; human reviewed 20,427 / 13.7 MB.
- `X-Total-Results` and the delivered record count differ slightly (20,416
  reported, 20,427 delivered), so the header sizes the download and never
  asserts it.

Six bugs were found by review after the code was written and passing its own
tests, each verified against the live API before and after the fix: an HTTP 400
on internally-quoted organism names that the resolver swallowed as "found
nothing"; malformed UniProt JSON escaping every try/except; a request naming
both a proteome and accessions producing a file labelled for 6,067 proteins
holding one; HTTP 4xx retried three times at up to 300s each; a private
`_ACCESSION` coupling; and a 5,000-digit query returning HTTP 500 because
Python caps integer parsing at 4,300 digits.

Two things about this repo's test setup cost real time and are worth knowing:
`docker compose exec api python -m pytest` runs the **main** repo from inside a
worktree, because the stack bind-mounts it -- every result describes the wrong
tree. And `conftest.py` hardcodes the database name `biopipe_test` and drops
every collection at session start, so two concurrent runs against one Mongo
wipe each other (measured on one unchanged tree: 7 failed, then 1872 passed,
then 5 failed). `backend/run-worktree-tests.sh` handles both -- main added it for the
first problem while this branch was in flight, and the two were merged.

`backend/app/services/structure_lookup.py` already resolves a gene to a protein
structure via UniProt, so the client and the ID-mapping path exist. This asks
for downloading UniProt data as a stored object -- proteomes or per-protein
FASTA -- the way assemblies download from NCBI today.

`assembly_handlers.py` is the model to copy: it exists as a sibling to
`sra_handlers` rather than a branch inside it, because one accession yielding
files with no QC chained is a different operational shape from a run yielding
FASTQ pairs. A UniProt download is the same shape as the assembly one, so it
likely belongs beside it -- and `RunKind` would gain a member for it, since
that enum is a display and grouping vocabulary and "downloaded a proteome"
reads differently from "downloaded a genome".

Touches: `backend/app/queue/` (new handler module),
`backend/app/models/run.py`, `backend/app/pipelines/sources.py` (which has its
own completeness test).

## Build and run on Linux — VERIFIED (mostly), one real bug found

Verified 2026-08-01 against this repo's actual running Linux stack (x86_64,
`biopipe-api-1`), not just an audit of the code. The user had already driven
the UI end to end -- profile setup, project creation, uploads, NCBI downloads,
Clair3, bwa-mem2 -- and reported all of it working. This pass checked the five
specific accommodations the entry called out, since "the UI works" doesn't
prove any of them.

- **arm64 workarounds correctly do not fire on x86-64 -- confirmed.** The
  running container resolves `bwa-mem2` to
  `/opt/bwa-mem2-2.3_x64-linux/bwa-mem2.avx2` (log line: `Launching
  executable ".../bwa-mem2.avx2"`), the upstream binary path, not a
  source-built one. `TARGETARCH` correctly took the non-arm64 branch.
- **DeepVariant was not arch-blocked -- but it was pinned to the wrong image,
  which is the one real code bug this pass found.** Dispatch is unconditional
  (`variant_handlers.py`, `pipeline_service.py`), so the entry above is stale
  and has been marked `— FIXED`. What was actually broken: `deepvariant_image`
  hardcoded `ghcr.io/antomicblitz/deepvariant-arm64:v1.9.0-arm64.6` on *every*
  platform. `docker manifest inspect -v` on 2026-08-01 shows that tag is
  `"architecture": "arm64"` and single-platform, with no amd64 variant, while
  `google/deepvariant:1.9.0` is `"architecture": "amd64"`. On this machine
  DeepVariant would have pulled with a platform-mismatch warning and then died
  at `docker run` with "exec format error" -- inside a job, past the
  launch-time `tools.require()` check meant to catch exactly this class of
  "the tool cannot run here". Not caught by the user's testing because Clair3,
  not DeepVariant, was the caller exercised. Fixed by
  `default_deepvariant_image` in `backend/app/config.py`, plus gating the BF16
  fastmath env vars to arm64 -- on x86-64 `TF_ENABLE_ONEDNN_OPTS=0` disables
  the oneDNN kernels and is a silent slowdown rather than a crash, so carrying
  the workaround across would have been a performance bug with nothing to
  point at it.
- **The governor's disk problem does disappear on Linux -- confirmed by
  measurement, not just plausible.** `BIOINFO_HOME` is
  `/mnt/897006ef-.../BioFlow`, a real mounted filesystem (`/dev/nvme0n1`,
  938G total / 736G free per `df -h`). The container's `shutil.disk_usage`
  read against `/data` inside `biopipe-api-1` reports 1007.0GB total / 789.3GB
  free -- the same filesystem, not the VirtioFS host-boot-disk substitution
  macOS produced. No host-side capacity reporter is needed here; the plain
  `shutil.disk_usage` path the governor already uses is correct as-is on this
  machine.
- **`host.docker.internal` is confirmed NOT automatic on Linux, as
  predicted -- and fixing DNS is only half of it.** `getent hosts
  host.docker.internal` inside `biopipe-api-1` failed to resolve, and neither
  compose file carried `extra_hosts`. **Fixed 2026-08-01**: `extra_hosts:
  ["host.docker.internal:host-gateway"]` added to `api` and `worker` in
  `docker-compose.yml` (base, so the override and `worktree-up.sh` both
  inherit it). Verified with `docker compose config` and by running a
  throwaway container with the same `--add-host`, which then resolved the name
  to the bridge gateway `172.17.0.1`.

  **The second half is host-side and the repo cannot fix it.** With DNS
  working, a connection to the host model was *still* refused, because the
  model server on this machine (Ollama) binds `127.0.0.1` only -- and the
  gateway address is not loopback, so a container reaching `172.17.0.1`
  never arrives. On Docker Desktop this does not come up. Making the LLM
  features work on Linux therefore also needs the server bound to all
  interfaces (`OLLAMA_HOST=0.0.0.0` for Ollama).

  **Also note a port mismatch on this machine, left alone deliberately.**
  `llm_base_url` defaults to port `11234` (`backend/app/config.py`), while the
  Ollama here listens on its own default `11434`. Not changed, because `11234`
  may be deliberate for a different server (LM Studio) on the machine this was
  written for, and guessing wrong would break that. Set `LLM_BASE_URL`
  explicitly rather than relying on the default on any given host.

  None of this has surfaced as a bug yet because nothing in the verified
  feature list above (uploads, NCBI, Clair3, bwa-mem2) calls the summary/LLM
  path, and that path is designed to no-op when the server is absent.
- **Bind-mount UID/GID mismatch is confirmed, exactly as predicted.** Files
  written by the container show as `uid=0 gid=0` (root) both inside the
  container and on the host (`ls -ln` agrees on both sides), while the host
  user is `uid=1000`. Nothing has broken yet because nothing outside Docker
  needs to touch `BIOINFO_HOME`'s contents, but a host-side tool or a
  non-root host user trying to clean up/inspect the directory tree directly
  would hit permission friction.

Net: the entry's own five-item list was mostly reassuring -- arm64 and the
governor's disk are genuine non-issues here. The two things that actually
needed changing were **one the entry predicted** (`host.docker.internal`) and
**one it did not** (the arm64-pinned DeepVariant image), and the second was
the more serious: a wrong-architecture container image that fails inside a job
rather than at launch.

**The general lesson, since it cost the miss:** the entry framed Linux as a
list of *macOS accommodations to undo*. The DeepVariant bug was the opposite
shape -- an arm64 assumption baked in as a universal default, on a machine
that had never run anything else. Worth checking the same way for any future
"works on my machine" artifact: not "which workarounds should stop firing"
but "which single-platform thing is being treated as the only platform".

Still open, both host-side rather than repo bugs: the model server must bind
`0.0.0.0` for a container to reach it, and `LLM_BASE_URL` should be set
explicitly rather than trusting the `11234` default. Neither is fixable from
this repo.

Touched: `docker-compose.yml` (`extra_hosts` on `api`/`worker`),
`backend/app/config.py` (`default_deepvariant_image`, `is_arm64`),
`backend/app/pipelines/variant_runner.py` (arm64-gated fastmath env),
`backend/app/pipelines/tools.py` (`TOOL_META` prose and `repository`, which
claimed the arm64 port unconditionally). Suite green: 2131 passed.

## The first `/pipelines/tools` request stalls 6-15s on NanoPlot — FIXED

Fixed 2026-07-31. `lifespan` now starts a fire-and-forget task (`_warm_tools`
in `backend/app/main.py`) that probes every tool in a thread before a user asks
for one, and `backend/app/pipelines/tool_cache.py` persists the results in
Redis so a restart re-seeds the `lru_cache`s instead of re-probing -- which is
what makes `uvicorn --reload`, the only way this app runs, stop re-paying the
cost on every backend edit.

Measured on the running stack after the change:

| | |
|---|---|
| Endpoint, cold container | **0.025s** (was 6-15s) |
| Warm task completes after startup | 33ms, not gating `/readyz` |
| Second start, reading Redis | `seeded=15 tools=15` |
| First start, empty cache | `seeded=0 tools=15` |

Options 2 (skip NanoPlot's `--version`) and 3's file-based variant were not
taken. The measurement table and the "parallelism is the wrong fix" reasoning
below still describe the shape of the problem, and are kept for whenever
another heavy-import tool is registered.

Two things the design got wrong, both found during implementation and worth
knowing if this code is touched again:

- **The planned `path:mtime_ns:size` fingerprint was not viable.** Two writes
  to one path can land in a single `mtime_ns` tick, so an upgraded binary
  fingerprinted identically. It now also hashes contents.
- **Four tools are wrapper scripts, not binaries** -- `fastqc`, `bowtie2`,
  `hisat2` and `cutadapt` are Perl or Python entry points that dispatch to a
  separate payload. The fingerprint covers the wrapper, so a payload-only
  upgrade leaving the wrapper byte- and mtime-identical goes undetected. That
  gap is documented on `_fingerprint`; the 24h TTL is the backstop.

Verified against the real stack, not only the suite: a deliberately poisoned
cache entry claiming version `0.0.0-WRONG` for fastp was rejected on
fingerprint mismatch and re-probed to the true `0.24.0`. That is the property
that matters here -- a cached version string is half of what a methods section
reports.

Raised: 2026-07-31, while fixing NanoPlot being reported unavailable
(`SLOW_IMPORT_TIMEOUT_SECONDS` in `backend/app/pipelines/tools.py`).

Probing is lazy and serial, and nothing warms it. `all_tools()` calls fifteen
`lru_cache`d probe functions in sequence, each shelling out to `<tool>
--version`. No cache is populated at startup -- `lifespan` in
`backend/app/main.py` connects Mongo/Redis and loads handlers but never touches
`tools` -- so **the entire probe cost is paid inside whichever user request
reaches `/api/v1/pipelines/tools` first**, which is the tool selector and the
`/help/software` page.

Measured on this machine, cold container:

| | |
|---|---|
| NanoPlot alone | **12.0s** |
| All other 14 tools combined | ~2.7s (fastqc 0.7s, bwa-mem2 1.0s, rest <0.3s) |
| Full serial probe | **14.7s** |
| Endpoint, warm host page cache | **6.1s** |

The important shape: **this is one slow tool, not fifteen.** NanoPlot is ~80% of
the total because it imports pandas/scipy/plotly before printing one line.
cutadapt is also a Python entry point and answers in 0.2s.

That makes parallelism the *wrong* fix, which is worth stating plainly because
it is the obvious one to reach for. Running all fifteen probes concurrently
caps the total at the slowest single probe -- NanoPlot's 12s -- so it buys
about 3s of the 15 and adds a thread pool. Options actually worth considering:

1. **Warm the cache in `lifespan`, in the background.** A `create_task` that
   calls `all_tools()` after `yield`-time setup moves the cost off the request
   path entirely; by the time a user opens the tool selector it is usually
   done. Keep the laziness as the fallback for a request that arrives first --
   the point is to stop *guaranteeing* a user pays it, not to add a startup
   gate. Note this would make container start do 15 subprocess spawns, so it
   should not block `/readyz`.
2. **Don't ask NanoPlot for its version at all.** The probe exists to prove the
   binary runs and to capture a version string for provenance. `shutil.which`
   plus the version parsed from a cheaper source would collapse 12s to ~0.
   Cost: loses the "does it actually execute" check that catches an x86-64
   binary on arm64 -- the exact case `_probe`'s returncode branch was written
   for. Probably only acceptable if paired with a check that runs once and is
   persisted rather than per-process.
3. **Persist probe results across restarts.** Keyed by binary path + mtime, in
   Redis or under `.biopipe/`. Survives `uvicorn --reload`, which currently
   discards the whole cache on every backend edit -- so during active
   development this cost is paid repeatedly, not once.

Option 1 is the smallest change that fixes the user-visible symptom and is
probably where to start; 3 is the one that also helps the edit-reload loop.

Not urgent: it is a one-time-per-process stall on a page that is not on the
critical path of any pipeline, and the 60s timeout means it now *completes*
rather than silently failing. Before this was fixed the same probe hit the 10s
default and NanoPlot simply reported unavailable, which is why the latency was
not visible as latency.

Worth doing before anything else with a heavy import graph is registered --
another tool of NanoPlot's shape doubles the stall, and `all_tools()` has no
per-tool budget.

Touches: `backend/app/pipelines/tools.py`, `backend/app/main.py` (lifespan),
`backend/app/api/v1/pipelines.py`.

## Longest/shortest contig reporting never shipped — THE ENTRY WAS WRONG

Raised: 2026-07-31, by an audit of `docs/superpowers/plans/`. Retracted
2026-08-01: **it had already shipped two days before this entry was written.**

`19f6b62` ("feat: record per-sequence FASTA lengths and assembly extremes",
2026-07-29) is the third item of `2026-07-29-todo-batch.md`, delivered on
schedule. `_parse_fasta` in `backend/app/storage/parsers.py:440-503` tracks
the extremes across *every* record rather than just the stored window, emits
`sequence_longest` and `sequence_shortest`, and both are rendered by
`FactsTable.tsx` and `AssemblyFacts.tsx`.

The audit grepped for `longest_contig` and `shortest_contig`. The code calls
them `sequence_longest` and `sequence_shortest` -- the comment at
`parsers.py:496` even explains why they are "sequences" and not "contigs" (a
FASTA's records are scaffolds, and the counts diverge sharply for a
chromosome-level assembly). A grep for one plausible name and a conclusion of
"never shipped" is what produced a backlog entry proposing to build a working
feature a second time.

Kept rather than deleted for that reason alone. The lesson is not about
contigs: **an absence-of-symbol grep proves nothing unless you have checked
what the symbol is actually called**, and this repo's own guidance to verify a
TODO against the code assumed the verification would be done right.

The two entries above (QUAST/BUSCO, and the assembly design below) should not
count this as work they close -- there is nothing left to close. N50 across a
FASTA is still genuinely missing and still belongs to QUAST.

## Assembly: designed, not built — FIXED (the assembly half, 2026-08-02)

De novo assembly shipped 2026-08-02. Flye is installed, the Actions card
offers it for long reads, and a run produces a contig FASTA roled `reference`
plus a GFA graph. Code: `app/pipelines/assembler_registry.py`,
`assembly_params.py`, `assembly_runner.py`, `app/queue/assembly_handlers.py`,
`pipeline_service.launch_assembly`, `results._apply_assemble_reads`,
`suggestion_service.build_assemble_card`, `frontend/src/components/
AssembleDialog.tsx`.

**What the implementation did differently from the design, and from this
entry.**

- **This entry's `mem_mb` claim was wrong.** It predicted assembly would be
  "the first real exercise of the `mem_mb` side of the load governor's
  admission checks". `app/queue/governor.py` does not read `mem_mb` at all --
  every handler declares it and nothing enforces it. The guard is at launch
  instead, in the `resource_estimator` shape.
- **HiFi goes to Flye, not hifiasm.** hifiasm is not packaged for Debian and
  needs a source build with the arm64 SIMD problem bwa-mem2 already has, so
  the "hifiasm for HiFi, Flye for ONT/CLR" split is deferred rather than
  dropped. `spec_for_chemistry` is the one function that changes when it
  lands.
- **Genome size is never passed to the assembler.** Flye stopped requiring
  `--genome-size` at 2.8, and it only alters behaviour alongside
  `--asm-coverage`, which BioFlow does not offer. It is collected for
  BioFlow's own memory estimate; sending it anyway would record a parameter in
  a run's provenance that the tool did not act on.
- **`FormatKind.GFA` was needed**, which the design did not anticipate --
  otherwise the assembly graph files as "Text".
- **`_distinct_assemblies` needed no change.** The design worried hifiasm's
  `hap1`/`hap2` would be collapsed as one assembly. It keys on an NCBI
  accession regex, so any filename that does not look like `GCF_..._genomic`
  is already its own candidate.
- **Progress is a phase name, not a percentage.** Flye's stages differ in
  duration by more than an order of magnitude.

**Measurements.** Flye 2.9.5 from Debian, ~37 MB, dependencies already in the
image. Memory estimates: 3.2 GB for a 4.6 Mb bacterial genome, 121 GB for 3.1
Gb human. A 40,000-contig draft parses in 1.75 s into a 2.2 KB facts document,
`samtools faidx` 0.3 s, `minimap2 -d` 1.5 s / 330 MB, `bowtie2-build` 207 s.

**Three bugs this work found in existing code**, none of them in assembly:
`resolve_reference` told users a genome needed fetching while two sat in the
project; `/help/software` rendered from a hardcoded list, so featureCounts and
pydeseq2 were invisible; and the "longest/shortest contig never shipped" entry
above was wrong -- it had shipped two days before it was raised.

**Still open:** an end-to-end assembly on adequately-covered reads. The only
long-read data in the library is a ~1.2x yeast HiFi subsample, which cannot
assemble. See "Post-assembly QC: BUSCO and QUAST" and "Reference-guided
assembly" above, both of which this unblocks.

The original entry follows, kept because its diagnosis explains why the code
looks the way it does.

Design: `docs/superpowers/specs/2026-08-01-de-novo-assembly-design.md`
(2026-08-01), covering the `### Assembly` section below. What the design
settled that this entry left open:

- **Flye first and alone.** It is packaged in Debian trixie (2.9.5, 37 MB,
  depending on minimap2 and samtools the image already has) and covers ONT,
  CLR and HiFi. **hifiasm is not packaged** and needs a source build with
  probable arm64 SIMD work, so this entry's "hifiasm for HiFi, Flye for
  ONT/CLR" split is deferred rather than dropped: HiFi routes to Flye until
  hifiasm is built. SPAdes is packaged but out of scope by decision.
- **This entry's `JobResources` claim is wrong.** It predicted assembly would
  be "the first real exercise of the `mem_mb` side of the load governor's
  admission checks." `app/queue/governor.py` does not read `mem_mb` at all --
  every handler declares it and nothing enforces it. The guard is at launch
  in the `resource_estimator` shape instead.
- **Genome size is inferred, warned about, and overridable**, from a
  same-organism reference's `reference_total_length` / `ncbi_total_length`
  when the project has one. Never inferred from read volume.
- **One job, several first-class outputs** -- FASTA, GFA (a new
  `ObjectRole.ASSEMBLY_GRAPH`, not a `SidecarRole`), and `assembly_info.txt`
  parsed into facts, which is where per-contig *coverage and circularity*
  come from. Contig extremes already ship generically; see the retracted
  entry above.
- **A draft assembly disables the Align card as the code stands.** Verified
  against `resolve_reference`: a project holding an NCBI reference *and* a
  de novo draft, with a known organism, falls past the single-reference
  branch into "Fetching a reference genome for X is not wired up yet" -- a
  refusal that is false, beside two usable genomes. Fixing that is part of
  the assembly work rather than a follow-up, or shipping assembly makes
  alignment worse.
- **Three unrelated things are already called "assembly"** in `backend/app/`
  (upload-chunk reassembly, NCBI assembly metadata, NCBI assembly download).
  They get renamed first, freeing `app/queue/assembly_handlers.py` for the
  de novo one.

> **Variant calling was built on 2026-07-29** and is no longer deferred. The
> section below is kept for the assembly half, which is still unbuilt, and
> because the variant-calling notes explain design choices the code still
> follows. What actually shipped, and where it departed from this design:
>
> - **`ReadChemistry` earned its keep as predicted** -- but the fact did not
>   reach the BAM. `_apply_align_reads` copied `reads.metadata` and *not*
>   `reads.facts`, so `qc_read_chemistry` was unreachable from an alignment and
>   every caller would have silently resolved to bcftools, including for ONT
>   and HiFi. Fixed by `align_provenance` (`app/queue/results.py`), which
>   copies the fact forward, plus a fallback in
>   `pipeline_service.read_chemistry_for_alignment` that reads it off the
>   parent reads for BAMs aligned before the fix.
> - **`depends_on` was not used.** This entry proposed gating `call_variants`
>   behind a completed `index_bam`. The implementation instead requires the
>   `.bai` and the reference `.fai` to exist at launch and refuses with an
>   actionable message. Simpler, and the user gets "index it first" instead of
>   a job that sits blocked.
> - **Short reads use bcftools only.** GATK was listed as an option; it is
>   ~400MB of JARs and bcftools is sufficient for single-sample calling.
> - **DeepVariant is recognized but not installed** -- no arm64 build. The
>   handler and the launch path both refuse it with an explanation.
> - **CLR is refused outright**, as this entry suggested was worth deciding
>   explicitly. `caller_for_chemistry` raises, and the dialog renders the
>   refusal rather than offering a caller.
> - `SidecarRole.TBI` was the only new storage concept needed, as predicted.
>
> Verified end to end against a real ONT run (DRR1078403 vs. *T. brucei*):
> both Clair3 and bcftools produce a VCF with a `.tbi` sidecar, and the
> chemistry fallback resolves `ont_simplex` on a BAM that predates the fix.

Raised: 2026-07-28, during long-read QC and alignment-correctness work
(`ReadChemistry`, `preset_for_chemistry`, `qc_stats.infer_chemistry`,
`is_long_read`).

Assembly is not built. This is recorded so the model added for HiFi/CLR
correctness -- `ReadChemistry` on `align_runner`, inferred by
`qc_stats.infer_chemistry` and stamped onto QC facts as `qc_read_chemistry`
-- does not have to be reshaped later to fit it.

### Variant calling (BUILT -- see the note above)

Wants a new `RunKind.VARIANT_CALLING` (alongside the existing `ALIGNMENT`,
`TRIM`, `SRA_DOWNLOAD` in `backend/app/models/run.py`), a `variants` object
role, and a VCF/BCF output with a `.tbi` index as a sidecar -- the sidecar
model already handles exactly this shape for `.bai` (`SidecarRole.BAI` in
`backend/app/models/object.py`), so a `SidecarRole.TBI` is the only new
enum member needed, not new machinery. `FormatKind.VCF`/`FormatKind.BCF`
already exist as recognized file kinds; there is no `call_variants` handler,
job role, or `.tbi` sidecar anywhere in the codebase yet.

Caller choice is chemistry-driven, which is the concrete reason
`ReadChemistry` earns its keep beyond alignment:

- ONT -> Clair3, with the model selected per chemistry (ONT_SIMPLEX vs.
  ONT_DUPLEX) -- another consumer of the same inferred fact, not a new
  inference.
- PacBio HiFi -> DeepVariant or Clair3. CLR is not a good target for either;
  this is arguably a case where the UI should warn or refuse rather than
  offer a caller, mirroring how `is_long_read` warns rather than blocks for
  trimming -- worth deciding explicitly when this is actually built rather
  than assumed.
- Short reads -> bcftools or GATK.

Job shape mirrors alignment exactly: a `call_variants` job depends on a
completed `index_bam`, which the existing `Job.depends_on` gate
(`backend/app/models/job.py`, exercised today by `align_reads` waiting on
`build_index` in `pipeline_service.launch_alignment`) already handles with
no queue changes. This is a real, exercised pattern to extend, not a new one
to invent.

### Assembly

Wants `RunKind.ASSEMBLY`. Its output -- a FASTA -- is itself a candidate
reference, so it should feed back into the existing reference/index
machinery (`REFERENCE_KINDS`, `_check_reference`, `build_index_command`)
rather than needing a new storage concept. Tool choice is chemistry-driven
again: hifiasm for HiFi, Flye for ONT/CLR.

Both tools are memory-hungry enough to need a real `JobResources` declaration
(`backend/app/models/job.py`, `cpu`/`mem_mb`/`io`) rather than the small
defaults trim and QC use today -- and doing so would be the first real
exercise of the `mem_mb` side of the load governor's admission checks, not
just `cpu`.

### What this does not need

Neither pipeline needs a queue change (`depends_on` already exists) or a
storage-model change beyond one new `SidecarRole` member. The design cost was
almost entirely in making sure `ReadChemistry` lived on `align_runner`
(shared by alignment, and by extension anything chemistry-driven) rather than
being invented fresh, and that it is inferred once in QC and read everywhere
else rather than recomputed per consumer.

Touches when built: `backend/app/models/run.py`, `backend/app/models/object.py`
(`SidecarRole.TBI`), `backend/app/services/pipeline_service.py`,
`backend/app/queue/pipeline_handlers.py`, `backend/app/pipelines/` (new
`variant_runner.py` / `assembly_runner.py`, mirroring `align_runner.py`'s
split between command construction and progress parsing), and the
corresponding frontend dialogs alongside `AlignDialog.tsx`/`TrimDialog.tsx`.

## The align dialog's submit button needs scrolling when expanded — FIXED

Fixed in `d4d9f2a` (merged to main). `.trim-modal` converted from
`overflow-y: auto` to a flex column; `.modal-body` scrolls, `.modal-actions`
pins to the bottom via `margin-top: auto`.

Raised: 2026-07-27, during alignment, found by driving the real UI.

With "Aligner and performance" expanded, `.trim-modal` is 822px of content in
a 633px `max-height`. It scrolls, so nothing is unreachable, but the primary
action leaves the viewport at the moment the user is most likely to want it --
they have just finished changing settings.

The trim dialog has the same structure and never hit this because it has fewer
advanced fields. Worth fixing for both at once rather than tuning one modal:
pinning `.modal-actions` to the bottom of the modal with the body scrolling
between the heading and the actions would fix the class of problem.

Not urgent -- the flow works, and the section is collapsed by default.

Touches: `frontend/src/styles.css`, `frontend/src/components/AlignDialog.tsx`.

## Changing an index definition is a hard startup failure

Raised: 2026-07-27, during alignment. **The migration below has been applied to
this machine's `biopipe` and `biopipe_test` databases; it is recorded because
any other database predating the change still needs it.**

The job dependency gate added a `blocked` state, and `uniq_active_dedup_key` --
the durable guard against enqueueing the same logical work twice -- filters on
an explicit list of non-terminal states. That list now includes `"blocked"`.

`init_beanie` does not silently keep the old definition, which is what this
entry originally claimed. It calls `createIndexes` with the new
`partialFilterExpression` under a name that already exists, MongoDB rejects it
with `IndexKeySpecsConflict` (code 86), and **the API exits during startup**.
Not a quiet inconsistency: the container will not boot at all against a
database that predates the change.

A fresh database is unaffected -- the index is created correctly the first time
-- which is exactly why this does not show up until an existing deployment is
upgraded.

The fix is to drop the index so Beanie recreates it:

```js
db.jobs.dropIndex("uniq_active_dedup_key")
```

Note it must be run against **every** database carrying the collection, not
just the application's. `biopipe_test` also had a copy, created by the
`init_beanie` fixture in `tests/storage/test_object_role.py` and
`test_sidecars.py` -- and because the app and the tests share one Mongo, the
stale test-database index kept the API down after the real one was fixed.

The general lesson is larger than this one index: **any** change to an index
definition on a collection with existing data is a breaking deployment without
a migration step, and this project has no migrations mechanism. Worth building
one before the next schema change rather than after.

Touches: `backend/app/models/job.py`, `backend/app/db/client.py`.

## The load governor watches the wrong disk

Raised: 2026-07-27, during read preparation follow-up.

`governor._sample_disk` calls `shutil.disk_usage(settings.bioinfo_home)` and
feeds the result into two admission thresholds: `DISK_FREE_CLOSE_PCT` (5%) and
`DISK_FREE_CLOSE_BYTES` (20 GB). Under Docker Desktop those numbers describe
the wrong filesystem.

Docker Desktop bind-mounts the *share root* (`/Volumes`) rather than the volume
beneath it, and VirtioFS answers `statfs` from the filesystem hosting that root
-- the Mac's boot disk. Measured on this machine: the container reports 995 GB
total / 205 GB free for `/data`, while the drive the data actually sits on is
3.7 TB with 712 GB free. Every path under `BIOINFO_HOME` reports the same wrong
figure (`/data`, `/data/objects`, `/data/tmp`, `/data/.biopipe` were all
checked), so there is no sub-path trick that recovers the real value.

This was first described as "safe because it errs conservative", which is not
right. It is wrong in both directions:

- The boot disk filling up -- Xcode caches, a large download, Docker's own
  images -- would close the governor and stop pipeline work while the drive
  holding the data has terabytes free.
- The *external* drive filling up is invisible. Free space there could reach
  zero and the governor would keep admitting alignment jobs, because it is
  watching a disk that still looks healthy. Given that a single alignment run
  can write hundreds of gigabytes, this is the direction that actually costs
  something.

The API already returns `storage.disk.reliable: false` and the UI shows library
size instead of a free-space claim, so nothing untrue is displayed. The
governor is the remaining consumer that acts on the number.

### The fix: a host-side capacity reporter

The container cannot see past VirtioFS, so the value has to come from outside
it. Sketch:

A small process on the host -- a launchd agent, or a `make`-managed script --
runs `statvfs` against the real `BIOINFO_HOME` path every 30s or so and
publishes the result where the container can read it. Two plausible channels:

1. **Through the mount itself.** Write `.biopipe/capacity.json` holding
   `{total_bytes, free_bytes, measured_at}`. The container already reads
   `.biopipe/VERSION` as its mount sentinel, so this adds no new plumbing and
   inherits the same "is the drive actually there" guarantee. Cost: a file the
   application reads but does not own.
2. **Into Redis.** The agent `SET`s a key with a TTL that the governor reads.
   No filesystem involvement, and staleness self-corrects through expiry. Cost:
   the agent needs a Redis client and connection details, which is more setup
   than a file write.

The first is simpler and matches the existing mount-sentinel pattern; prefer it
unless staleness handling proves awkward.

Whichever channel, the governor needs a freshness rule, and the direction of
its failure matters. A report older than a few minutes must be treated as
*absent*, and absent must mean "do not apply disk thresholds" rather than
"assume zero free" -- otherwise a stopped agent silently halts all compute
work. Same principle as the mount sentinel: an unavailable signal aborts the
check rather than being read as bad news.

Also worth handling: `BIOINFO_HOME` on a path that is *not* a separate volume
(someone running without an external drive) should keep using `shutil.disk_usage`
directly, since there is nothing wrong with it there. The host agent is a
Docker-Desktop-on-macOS workaround, not the general path.

Deferred because it introduces a host-side component this application has so
far avoided entirely -- a real architectural addition for a threshold that has
not yet fired. Worth doing before alignment starts writing files large enough
to genuinely fill the drive.

Touches: `backend/app/queue/governor.py`, `backend/app/storage/home.py`,
`backend/app/api/v1/system.py`, `Makefile`, `ops/`.

## `JobContext.extend_lease` is inert — FIXED

Fixed 2026-07-29 by
`docs/superpowers/plans/2026-07-29-queue-and-role-provenance-cleanups.md`,
found stale by an audit on 2026-07-31.

**It was wired, not deleted -- and this entry's advice had gone dangerous.**
`_extend_cb` is now assigned in both `executor.py` (~line 54) and
`worker.py` (~line 304). Four handlers call `ctx.extend_lease` today:
`summary_handlers`, `assembly_handlers`, `sra_handlers` and
`pipeline_handlers`. Deleting the method, as suggested below, would now break
working code.

The docstring was also rewritten to draw the distinction the original muddled:
the heartbeat covers a merely *slow* job, while `extend_lease` covers lease
*length* -- a paused VM or stalled event loop stops the heartbeat entirely, and
then only the recorded TTL stands between a live job and the reaper. Covered by
`backend/tests/queue/test_lease_extension.py`.

Raised: 2026-07-27, during read preparation.

`JobContext.extend_lease` in `backend/app/queue/registry.py` calls
`self._extend_cb`, which is never assigned anywhere in the codebase. Only
`_progress_cb` is set (in `worker.py` and `executor.py`), so the method
silently does nothing. Its docstring promises the opposite: "A multi-hour
alignment sets a long lease and keeps heartbeating; without this the reaper
would treat it as hung."

Nothing is broken today. `_heartbeat_loop` renews every in-flight job's lease
every 10s regardless of duration, and because a thread-mode handler blocks only
its own worker thread the event loop keeps turning -- a multi-hour `trim_reads`
run is safe. The hazard is the API's existence: it reads as the tool for long
phases, and someone will eventually rely on it instead of the heartbeat.

Either wire `_extend_cb` to a real lease extension or delete the method. Delete
is probably right: the heartbeat already handles the case the docstring
describes, and a second mechanism for the same thing is a way to get them out of
step. Deferred because it changes a public-looking handler API that this feature
did not otherwise touch.

Touches: `backend/app/queue/registry.py`, `backend/app/queue/executor.py`,
`backend/app/queue/worker.py`.

## `bp:cancel` grows without bound — FIXED

Fixed 2026-07-29 by
`docs/superpowers/plans/2026-07-29-queue-and-role-provenance-cleanups.md`,
found stale by an audit on 2026-07-31.

**This entry's premise was wrong.** The main drop path already cleared the
flag: `SREM bp:cancel` has been in `backend/app/queue/scripts/release.lua`
(line ~42) since the initial commit. What actually leaked were the routes that
bypass that script, fixed across three commits -- `0ce1d28` (the reaper marking
a job dead), `f665812` (a blocked job failing on its dependency) and `5122c39`
(covering `_fail_blocked_job`'s own clear rather than just the helper).

`backend/tests/queue/test_cancel_cleanup.py` exists specifically for those
bypassing routes, and its module docstring names the distinction.

Raised: 2026-07-27, during read preparation.

`queue.request_cancel` adds a job id to the `bp:cancel` Redis set. The queued
path removes it again (`queue.py`, in the branch that cancels a job before it
starts), but the *running* path never does -- when a running job observes
cancellation and terminates, nothing SREMs its id.

Every worker calls `SMEMBERS bp:cancel` once a second in `_cancel_watch_loop`,
so the cost of each stale entry is paid forever, by every worker. At single-user
scale this is a slow leak rather than a problem: hundreds of cancellations would
still be a small set. It is worth fixing before anything drives cancellations
automatically.

The fix belongs wherever a job reaches a terminal state -- `queue.complete` and
the reaper both already write there. Deferred because it is a correctness
cleanup in code this feature only read.

Touches: `backend/app/queue/queue.py`, `backend/app/queue/worker.py`.

## Mate detection is filename-only

Raised: 2026-07-27, during read preparation.

`app/pipelines/pairing.py` matches paired-end files by stripping an R1/R2 token
from the end of the name. Read IDs inside the files would be authoritative, but
checking them means decompressing two files to compare their first records, and
the naming convention is near-universal.

Two consequences. Files named outside the convention (`foo_fwd.fastq.gz` /
`foo_rev.fastq.gz`, or a sample whose mate marker sits mid-name) never pair, and
the user has to link them by hand. And two genuinely unrelated files could in
principle pair if their names collide after the token is removed -- guarded
against by requiring the naming *scheme* to match and by refusing an ambiguous
match, but not impossible.

Worth revisiting only if a real dataset trips it. The launch dialog already
shows the detected mate and allows overriding it, and `mate_object_id` is never
overwritten once set, so a wrong guess is visible and correctable rather than
silent.

Touches: `backend/app/pipelines/pairing.py`, `backend/app/queue/results.py`.

## Re-ingest re-asserts a reference role the user cleared — FIXED

Fixed 2026-07-29 by
`docs/superpowers/plans/2026-07-29-queue-and-role-provenance-cleanups.md`,
found stale by an audit on 2026-07-31.

The `user_touched: list[str]` shape this entry preferred is what shipped
(`backend/app/models/object.py`, ~line 172), and the comment there records why
a list beat a per-field `role_set_by`.

**The implementation went further than this entry asked.** Checking
`user_touched` at the decision point still leaves a window: a conversion
landing between the decision and the write would be overruled by it. So
`backend/app/queue/results.py` (~line 170) re-checks `{"user_touched": {"$ne":
"role"}}` inside the update filter itself, making the write conditional rather
than the decision. That race is not mentioned below.

Raised: 2026-07-26, during assembly-accession enrichment.

`should_assign_reference_role` in `backend/app/queue/results.py` assigns the
reference role when an assembly accession is found and `role is None`. A role
the user *cleared* is indistinguishable from one never set, so converting a
reference back to reads and then re-ingesting will silently re-assign it.

Rare in practice — it needs a deliberate conversion plus a re-ingest of a file
whose name carries a GCA/GCF accession — but it quietly contradicts the promise
that an explicit choice is never overruled.

The fix needs a way to record that a user has touched the role: either a
nullable `role_set_by` field (`"user"` vs `"ingest"`), or a general
`user_touched: list[str]` on the object. The second generalizes to the same
problem for metadata fields, so it is probably the better shape. Deferred
because it is a schema change that this feature does not otherwise need.

Touches: `backend/app/models/object.py`, `backend/app/queue/results.py`,
`backend/app/services/object_service.py`.
