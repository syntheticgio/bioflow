# TODO

Two kinds of entry, kept apart because they are read differently.

**Planned features** are things we have decided to build, described from the
user's side. **Deferred findings** are problems discovered while building
something else, recorded with enough context to pick up cold. Findings are
newest first.

---

# Planned features

## Profiles — SPECCED

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

Also note this is a different *kind* of artifact from everything else here: a
native desktop app, outside this repo's Python/React/Docker toolchain, needing
its own repo and build/signing story.

## Software help page: filter by column

On `/help/software`, clicking a column header (Alignment, Quality Control, ...)
filters the page to the tools matching that column — primary *or* secondary, and
including uninstalled ones. All columns stay visible, since a tool can occupy
several.

**The clickable tool names asked for alongside this already work.** Matrix rows
render `<a href={`#tool-${tool.name}`}>` (`HelpSoftware.tsx:112`) against
`id={`tool-${tool.name}`}` on each entry heading (line 219), and `ToolMatrix`'s
docstring states the intent: "Names link to the entries, so the matrix works as
the page's index." Only the column filtering is outstanding.

Small enough to build directly rather than spec: contained frontend work in one
component, no backend change, and the matrix already distinguishes primary from
secondary rank.

---

# Deferred findings

## The first `/pipelines/tools` request stalls 6-15s on NanoPlot

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

## Assembly: designed, not built

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

## `JobContext.extend_lease` is inert

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

## `bp:cancel` grows without bound

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
