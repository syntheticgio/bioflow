# TODO

Deferred work, with enough context to pick up cold. Newest first.

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

## Re-ingest re-asserts a reference role the user cleared

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
