# TODO

Two kinds of entry, kept apart because they are read differently.

**Planned features** are things we have decided to build, described from the
user's side. **Deferred findings** are problems discovered while building
something else, recorded with enough context to pick up cold. Findings are
newest first.

This file holds only open entries. Closed ones move to
[`docs/TODO-done.md`](TODO-done.md) so this file doesn't grow to carry every
finished entry's full context on every read -- see "Closing out a TODO entry"
in `CLAUDE.md` for the move itself.

---

# Planned features

## Notify on new feedback submissions — FIXED

Shipped 2026-08-05. New feedback submissions now push a Discord webhook embed
to the `#bug_reports` channel after the database insert succeeds.

**What shipped:** a `feedback_service.py` module (`backend/app/services/`)
that POSTs a Discord embed (subject, contact, comment, submission id) to a
configurable `FEEDBACK_WEBHOOK_URL` via stdlib `urllib` in a worker thread.
The endpoint (`backend/app/api/v1/feedback.py`) fires it as
`asyncio.create_task(notify_feedback_created(...))` so a slow or failing
webhook never stalls the 201. `notify_feedback_created` catches every error
internally -- a downed webhook only loses the notification, never the saved
record or the 201 response. Two settings were added to
`backend/app/config.py`: `FEEDBACK_ENABLED` (default true) and
`FEEDBACK_WEBHOOK_URL` (default empty = off), each documented in `.env.example`.

The frontend `HelpFeedback.tsx` was simplified: the previous-submissions list
was removed (notifications now go to Discord, not an on-page log), and the
intro text was updated to reflect the Discord delivery.

**Design decisions that departed from the original plan:**
- `asyncio.create_task` rather than inline `await`: an inline await would
  hold the request open for up to 10s on a slow webhook. create_task keeps
  the 201 instant; the task is unawaited because it never raises.
- stdlib `urllib` rather than `httpx`: httpx is dev-only and not in the
  runtime Docker image. This matches the pattern in `structure_lookup.py`
  and `ai/adapters.py`.
- No UI settings page for the webhook URL: configured via `.env` /
  `docker-compose.yml`, matching the project's convention for infrastructure
  secrets. A UI would be a follow-up if the user base grows beyond one
  operator.

The Feedback page under Help (`/help/feedback`,
`frontend/src/components/HelpFeedback.tsx`) saves straight to the `feedback`
collection (`backend/app/models/feedback.py`,
`backend/app/api/v1/feedback.py`) and nothing else -- no one is notified when
a submission comes in. The only way to see one today is opening the page or
querying Mongo directly.

Add a way to push new submissions to the user directly. Delivery mechanism is
unspecified for now -- a Discord webhook is the leading candidate (simple,
no OAuth, posts from a plain HTTP call), but email or another channel would
also satisfy this. Whatever is chosen, the natural hook point is
`submit_feedback` in `backend/app/api/v1/feedback.py`, right after the
`Feedback` document is inserted.

Worth deciding as part of the design: whether a delivery failure should ever
affect the 201 response to the submitter (it shouldn't -- the record is
already saved; notification is best-effort on top of it), and where the
webhook URL / credential lives (`.env` / `settings`, not hardcoded).


## Helper install program — PARTIALLY FIXED

The core launcher (Docker detection/auto-start, first-run setup writing
`.env` alongside a bundled `docker-compose.yml`, Run/Stop/Update/status,
network-exposure toggle, health-gated browser handoff, a registry manifest
check behind the Update button) shipped 2026-08-05 in
[epic #4](https://github.com/syntheticgio/bioflow/issues/4), first slice
[#28](https://github.com/syntheticgio/bioflow/issues/28), against
[`docs/superpowers/specs/2026-08-04-native-launcher-contract-design.md`](superpowers/specs/2026-08-04-native-launcher-contract-design.md)
and
[`docs/superpowers/plans/2026-08-05-native-launcher-contract-implementation.md`](superpowers/plans/2026-08-05-native-launcher-contract-implementation.md).
The code lives in `launcher/` (Tauri v2; Rust state machine in
`launcher/src-tauri/src/`, React UI in `launcher/src/`).

**What shipped differently from the plan:** the launcher lives *in this
repository* rather than as a separate native-app repo, specifically so the
compose file it bundles can be *the* `docker-compose.yml` rather than a copy
— see the spec's "Where the launcher lives" section for why that reverses
this entry's own closing note below. The update check required an extra step
the spec didn't call out: GHCR requires a bearer-token exchange even for a
public, anonymous manifest read (unlike Docker Hub's unauthenticated path),
caught only by a real-network test against a live GHCR package, since every
fake-backed unit test passed regardless of whether the auth step was there.

**What's still open, and why this stays in `docs/TODO.md` rather than moving
to `docs/TODO-done.md`:** the "full install" pre-pull-optional-tools checkbox
described below is explicitly **not** part of what shipped — it is deferred
to [#40](https://github.com/syntheticgio/bioflow/issues/40), blocked on epic
#5 settling which tools are optional. Packaging, signing, and distribution is
[#39](https://github.com/syntheticgio/bioflow/issues/39)'s job and has not
shipped, so there is still no binary an end user can download.

The image half of that blocker is now gone.
[#37](https://github.com/syntheticgio/bioflow/issues/37) shipped 2026-08-05:
`api`/`worker`/`web` reference `ghcr.io/syntheticgio/bioflow-{backend,web}`
rather than building from source, and a directory holding nothing but
`docker-compose.yml` and `.env` was verified to start all five services and
serve the built app. The launcher can therefore start a real stack on Apple
Silicon. As of 2026-08-05 `linux/amd64` is published too: both `:latest` and
`:0.1.0` are multi-arch indexes carrying `linux/amd64` alongside the original
`linux/arm64`, built natively on an amd64 Linux box rather than under QEMU.
The amd64 backend built in minutes, not hours, exactly as
[#46](https://github.com/syntheticgio/bioflow/issues/46) predicted — the
`TARGETARCH` guard in `backend/Dockerfile` skips the sse2neon source compile
and takes upstream's prebuilt `x64-linux` bwa-mem2 tarball instead.
The images themselves were verified with five tool probes (`fastp`,
`bwa-mem2`, `run_clair3.sh`, `compleasm`, `datasets`), an import check of
the baked-in `app` package, and `nginx` as the web image's `Cmd` rather than
the dev stage's `npm run dev`.
The *stack* contract was then verified on amd64 the same way #37 verified it
on arm64: a scratch directory holding only `docker-compose.yml` and a `.env`
(isolated `COMPOSE_PROJECT_NAME`, `API_PORT`, `WEB_PORT`, `BIOINFO_HOME`,
no override file, Docker logged out of ghcr.io so the pull was anonymous)
pulled `:latest`, resolved it to the amd64 manifests, and brought all five
services up. `/healthz` 200; the web container served the real nginx build
with zero `/@vite/client`; nginx proxied real routes to `api` with status
codes identical to hitting it directly and BioFlow's own `profile_unresolved`
body rather than an SPA fallback; both workers logged `handlers_loaded` with
all 31 handlers. Port isolation via `.env` alone kept the 5173 stack
untouched throughout.
#46 nonetheless stays open on its third acceptance criterion, because that
criterion names the *launcher's* install flow and no launcher binary was
involved — this exercised the compose contract the launcher drives, not the
launcher. Building it here would also conflate two variables: this machine
is amd64 **Linux**, and the launcher has only ever been built on macOS, so a
failure would most likely be the untested Linux port rather than anything
about amd64. That verification belongs with #28's cross-platform criterion.
One regression to be aware of: `docker buildx imagetools create` rebuilds an
index from only the sources named, so the `unknown/unknown` provenance
attestation the arm64-only indexes carried is gone. Nothing depends on it;
[#38](https://github.com/syntheticgio/bioflow/issues/38) should restore it
with `--provenance=true` when CI takes over the build. **Done 2026-08-05:**
`.github/workflows/publish-images.yml` sets `provenance: mode=max` on both
builds, so the attestation is back on every published tag.

**CI publishing shipped 2026-08-05 (#38).** `.github/workflows/publish-images.yml`
builds `bioflow-backend` and `bioflow-web` for both architectures and pushes to
GHCR on every push to `main`, and on a `v*` tag publishes the version tag and
moves `latest`. What it did differently from #38's sketch: the issue assumed one
runner building both architectures, but hosted arm64 runners are unavailable on
a private personal repo and #46 had already measured emulated backend builds as
hours long, so each architecture builds natively on its own self-hosted runner
and a separate job merges the digests into a multi-arch manifest. Layer caching
is BuildKit state persisted on the runners (`keep-state: true`) rather than
`type=gha`, whose 10GB repo-wide limit cannot hold a 7.9GB backend image. Setup
and the offline-runner tradeoff are in
[`docs/ci-runners.md`](ci-runners.md). Still open under this entry: #39,
packaging and distributing the launcher binary itself.

**Windows was dropped from scope on 2026-08-05.** The supported platforms are
macOS and Linux. Some `#[cfg(target_os = "windows")]` branches remain in
`launcher/src-tauri/` but nothing builds or tests them, so #28's cross-platform
criterion now reads as macOS + Linux, both of which are met.
Verification so far is macOS-only (`cargo test`, `cargo clippy
--all-targets`, `npm run lint`, and a full `tauri build --bundles app`
launching and staying alive) — #28's "builds and runs on macOS, Windows, and
Linux" acceptance criterion stays unchecked until Windows and Linux are
actually exercised, not assumed. The Linux half is tracked separately as
[#49](https://github.com/syntheticgio/bioflow/issues/49), opened 2026-08-05
specifically so it isn't conflated with #46's amd64-image verification above
— #46 exercised the compose contract on amd64 Linux, not the launcher binary,
and a launcher build on that same machine would test the untested Linux port
and the architecture at once.

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

## Resource limits and intelligent enforcement -- PARTIALLY FIXED

Raised: 2026-08-01, requested. Foundation shipped 2026-08-07 as epic
[#7](https://github.com/syntheticgio/bioflow/issues/7), issues
[#68](https://github.com/syntheticgio/bioflow/issues/68) and
[#22](https://github.com/syntheticgio/bioflow/issues/22): design in
`docs/superpowers/specs/2026-08-07-resource-limits-admission-design.md`, plan
in `docs/superpowers/plans/2026-08-07-resource-limits-foundation.md`.

**This entry stays open.** The design resolved the "admission vs monitoring"
question this entry poses two paragraphs down -- admission, not monitoring or
kill-based enforcement -- but only the persisted-settings-plus-admission
foundation shipped. The refusal UI, the estimate resolver, auto re-plan, and
cgroup enforcement (option 1 below) are separate open issues, listed at the
end of this entry.

**What shipped, and how it differs from the plan below.** A persisted
`ResourceLimits` singleton (`backend/app/models/resource_limits.py`) stores a
user-set memory ceiling. `Worker._free_resources()`
(`backend/app/queue/worker.py`) resolves it against the machine's real budget
-- a stored limit only ever lowers the ceiling, never raises it above what the
host has -- and the result flows into `claim.lua`'s existing
`mem <= mem_free` admission check. No new enforcement code was written:
`claim.lua` already refused any job whose declared `mem_mb` exceeded the
budget it was handed, so shrinking that budget *is* the enforcement.

That surfaced a live bug this entry didn't know about: `claim.lua` and
`release.lua` maintained a `bp:conc:mem_mb` reservation ledger correctly on
both sides, but `Worker._read_reservations()` never read it and
`compute_free_resources()` had no parameter for it. The ledger was written and
discarded -- two memory-heavy jobs claimed in the same second both saw full
headroom and both got admitted. Fixed as #68, alongside #22.

**This is deliberately an admission budget, not a kill switch.** A job that
overruns its prediction is not stopped -- see the spec for why cgroup
enforcement (option 1 below) was rejected as the *default*; it survives as
opt-in follow-up work.

**The opt-in cgroup enforcement (option 1) shipped 2026-08-07 as
[#72](https://github.com/syntheticgio/bioflow/issues/72):** design in
`docs/superpowers/specs/2026-08-07-cgroup-hard-limits-design.md`, plan in
`docs/superpowers/plans/2026-08-07-cgroup-hard-limits.md`. The setting lives
in the Tauri launcher, not the web UI or `docker-compose.override.yml` --
a cgroup limit applies at container creation, so changing it means
recreating the container, and the API cannot recreate the container it runs
inside. The launcher writes `BIOFLOW_HARD_MEM_LIMIT`/`BIOFLOW_HARD_MEM_MB` to
`.env` and pins `WORKER_REPLICAS=1` (a limit is per-container; two replicas
would double the effective wall). `governor.mem_budget_bytes()` already fell
back to reading the cgroup, so admission picked up the ceiling automatically
with no change -- confirmed against a real stack, not assumed.

Two things beyond the original "configuration, not new code" framing: the
web UI's soft admission budget is now clamped to the hard limit when one
exists (`PUT /settings/resources` returns 422 above it), since an unclamped
soft budget above a hard limit meant every admitted job got OOM-killed --
the worst version of the feature. And exit 137 (SIGKILL) is terminal rather
than retryable when a hard limit is set, since a job killed by an immovable
ceiling dies identically on all `job_max_attempts`; the message now names
the ceiling instead of guessing "most likely out of memory."

Real end-to-end verification (bringing up a full stack via
`./ops/worktree-up.sh`, not just unit tests) caught a bug none of the unit
suites could see: Compose's `${BIOFLOW_HARD_MEM_MB:-}` always sets the env
var, resolving to an empty string when no hard limit is configured -- the
default state -- and pydantic-settings does not treat `""` as `None` for an
`int | None` field. This crashed the `api` container at startup on every
ordinary install. Fixed with a `field_validator` mapping `""` to `None`;
every prior unit test had monkeypatched the already-parsed settings
attribute directly, never exercising real pydantic-settings env parsing.

**A narrower version of the concurrency bug remains, found during final
review and filed as
[#74](https://github.com/syntheticgio/bioflow/issues/74).** `claim.lua`
compares against a `mem_free` value computed in Python moments before the
script runs, rather than reading the live counter inside the atomic script
itself. Two workers can still both compute stale headroom and both admit in
the same tick -- narrowed from unbounded staleness (the pre-#68 bug) to about
one Redis round-trip's worth of clock skew, not eliminated.

**The layered memory estimate resolver shipped 2026-08-07 as
[#69](https://github.com/syntheticgio/bioflow/issues/69):** design in
`docs/superpowers/specs/2026-08-07-layered-memory-estimate-resolver-design.md`,
plan in
`docs/superpowers/plans/2026-08-07-layered-memory-estimate-resolver.md`.
`backend/app/services/memory_estimate.py` arbitrates between the measured
model (`timing_service.estimate_memory()`) and the heuristic estimator
(`resource_estimator`), reporting which source it used. Wired into all four
consumers of a memory number, not just the two the admission design
originally named -- notably including the reservation `claim.lua` gates on
(`declared_align_mem_mb`), not only the two advisory BLOCK-check sites.
Guarded independently on extrapolation distance and fit quality, since a
poor fit inside the observed range is invisible to an extrapolation check
alone. Checked against real `job_timings` rows on the running stack: all 15
job types with history resolved to `heuristic` (none yet have enough rows to
attempt a fit), confirming the guards were never exercised against a false
positive on today's data.

Original text follows, for the reasoning that produced the above:

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

Remaining open issues:
[#70](https://github.com/syntheticgio/bioflow/issues/70)
(four-choice refusal card), [#71](https://github.com/syntheticgio/bioflow/issues/71)
(auto re-plan), [#74](https://github.com/syntheticgio/bioflow/issues/74)
(remaining cross-worker admission race). #72 (cgroup enforcement, opt-in)
shipped -- see above.

---

# Deferred findings

See CLAUDE.md, "Closing out a TODO entry", for what to do when one of these
lands. Short version: mark it `— FIXED` with a note, keep the body, and never
trust a plan's checkboxes as evidence it shipped.

## Alignment memory estimate does not size extra reads

Raised: 2026-08-08, deferred during code review of extra-reads-for-alignment
(GitHub issue #94, Task 3).

`launch_alignment` (`backend/app/services/pipeline_service.py`) calls
`memory_estimate.resolve(... input_bytes=obj.size or None, ...)` with only the
primary read object's size. When `params["extra_reads"]` is non-empty, the
runner (`align_handlers.align_reads`) concatenates every extra read's bytes
into the primary before the aligner runs, so the real input the aligner and
`samtools sort` see can be several times `obj.size` -- but the resource
estimate that gates the job and sizes its `mem_mb` never learns about the
extras.

This is exactly the kind of gap CLAUDE.md's "Querying computation records"
section warns is easy to introduce silently: nothing raises, no test fails,
the job simply gets a memory budget sized for a fraction of its real input.
A large enough set of extra reads could OOM a job that the estimator believed
was comfortably under budget.

Fix by summing `obj.size` across the primary and every object in
`extra_reads` before calling `memory_estimate.resolve`, in `launch_alignment`.

Touches: `backend/app/services/pipeline_service.py` (`launch_alignment`),
`backend/app/pipelines/resource_estimator.py`.

## Neither model segments by thread count

Raised: 2026-08-03, deferred while building computation records
(`docs/superpowers/specs/2026-08-03-computation-records-design.md`,
`docs/superpowers/plans/2026-08-03-computation-records.md`).

`JobRunTiming.threads` is captured -- the executor reads it from
`job.payload`, where the align/assembly/expression/assembly_qc handlers
already put it -- but both `timing_service._fit` (duration) and its memory
counterpart still regress against `input_bytes` alone. The design called for
segmenting the duration fit by thread count with a bytes-only fallback.

Deferred because no row carried a thread count until the recording shipped in
this same work, so the segmentation could only have been tested against
synthetic data and would have fallen back to today's behavior on every real
row anyway -- there was nothing to segment yet.

Revisit once several job types have accumulated runs at differing thread
counts. Check against real rows, not fixtures -- per CLAUDE.md, hand-built
objects that already look the way the code expects are how the suggestion
rules passed green while being wrong.

Touches: `backend/app/services/timing_service.py`.

## QC report directories can vanish from disk while the object's facts still point at them

Raised: 2026-08-02, found while investigating a user report of a 404 on the
FastQC report link for a completed, successful QC job.

`ERR17609896_1.fastq` and `ERR17609896_2.fastq` (both short-read Illumina,
project `6a6e1120a384162f7205eb87`) had `qc_fastqc_report` /
`qc_fastp_report` facts pointing at `fastqc/ERR17609896_1_fastqc.html` and
`fastp.html` respectively -- correctly formatted, no double-`object_id`
prefix, written by a `run_qc` job that Mongo shows as `succeeded` on
2026-08-01. But `/data/qc_reports/<object_id>/` did not exist on disk at all
for either object: not empty, not partially populated, just gone. Clicking
either report link 404'd with `"No such QC report: fastqc/..."` even though
the Quality tab showed a full 5/5 grade and 14 populated facts -- the facts
that don't depend on the report file (grade, Q20/Q30, GC, duplication) were
fine; only the two facts that point at now-missing files were stale.

**What this is not:** the double-object_id-prefix bug fixed in `ccd0d74`
(2026-07-28) -- that bug stored the path wrong from the start, and this job
ran 2026-08-01, after the fix, with a correctly-formatted path. Re-running QC
regenerated working reports immediately (`ERR17609896_1_fastqc.html`, 606KB,
verified served at 200), which is itself evidence the write path is fine and
something removed the output *after* a successful write.

**What was ruled out, and why it isn't a clean explanation:**

- `reap_report_dirs` (`backend/app/queue/handlers.py:700`) only deletes a
  report directory when the parent object is **absent from Mongo** (an
  `await DataObject.get(object_id) is None` check) *and* the directory's own
  mtime is older than `max_age_hours` (default 1h, runs hourly). Both
  `ERR17609896_1.fastq` and `_2.fastq` were confirmed present in Mongo with a
  direct query -- `db.objects.find_one` returned the full document, name and
  facts intact -- so this reaper's own guard should have skipped them.
- `object_service.remove_report_dirs` is the only other caller, invoked
  synchronously from `delete_object`. Neither object was deleted -- they were
  visible and functioning in the UI throughout.

Neither of the two known code paths that remove `qc_reports_dir` entries
matches what happened, going by the state visible after the fact. That
"going by" is the actual gap: worker logs only retain a short window, and by
the time this was investigated (a day-plus after the QC job ran) there was
nothing left to confirm which code path actually ran, or whether a third,
unknown path exists. The forensic trail was already gone.

**Why this is worth fixing rather than re-running QC and moving on:** this
already happened at least twice silently (both mates of one pair), the user
only noticed because they went looking for a specific report, and the same
class of bug could be quietly affecting `bam_stats_dir` / `vcf_stats_dir`
entries too (`reap_report_dirs` sweeps all three roots with the same logic).
Without a log trail, every future occurrence is another dead end and another
"just re-run it" -- which fixes the symptom but never explains whether the
reaper's guard has a hole, whether `delete_object` is cascading somewhere
unexpected, or whether it's something outside these two paths entirely (e.g.
`ops/worktree-up.sh --down` or manual disk cleanup pointed at the wrong
target, though neither is likely in a single-user local deployment).

**What would help next time:** log a line whenever `reap_report_dirs` or
`remove_report_dirs` actually removes a directory -- object_id, path, and
which caller triggered it -- with enough retention (or a durable sink, not
just container stdout that rotates) to survive at least a few days. Today
`reap_report_dirs` only logs when `removed` is nonzero in aggregate
(`handlers.py` around line 755, `report_dirs_reaped`), with no per-object_id
detail and no record of *which* condition (Mongo-absent vs. mtime) fired.
Also worth checking whether the mtime-based cutoff in `reap_report_dirs` is
looking at the right timestamp -- FastQC/fastp write directly into
`report_dir` (`pipeline_handlers.py:422-423`, `:501`, `:662`), so the
directory's own mtime should track the last file written into it, but this
was not verified against a real repro since the original directories were
already gone by the time this was investigated.

Touches: `backend/app/queue/handlers.py` (`reap_report_dirs`),
`backend/app/services/object_service.py` (`remove_report_dirs`,
`delete_object`).

**Update 2026-08-08:** the logging half of "what would help next time" has
shipped, tracked as [#10](https://github.com/syntheticgio/bioflow/issues/10).
`remove_report_dirs` now takes required `caller`/`reason` keywords and stamps
every `report_dir_removed`/`report_dir_cleanup_failed` line with them
(`delete_object` passes `caller="delete_object"`; the reaper passes
`caller="reap_report_dirs"`). `reap_report_dirs` now logs a
`report_dir_reap_candidate` line per directory it examines --
object_id, age, the Mongo lookup result, and which branch fired
(`skip_too_young` / `skip_live_object` / `reap`) -- not just the aggregate
count it logged before. `ops/migrate-storage.sh` also now appends a
pre-delete record (paths, file/byte counts) to `ops/migrate-storage.log`
before its `rm -rf`, since that script's own delete had no log trail at all.
This entry stays open: the root cause is still unconfirmed, evidence
retention/durable sink and the mtime-timestamp check are still open, and this
was written and reasoned about, not yet observed catching a real recurrence.

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

## Post-assembly QC: BUSCO and QUAST — FIXED (as contiguity + compleasm, 2026-08-02)

Shipped 2026-08-02, same day as the design
(`docs/superpowers/specs/2026-08-02-post-assembly-qc-design.md`). Contiguity
(`sequence_n50`/`n90`/`l50`/`auN`/`sequence_gap_count`/`sequence_gap_bases`) is
computed in `backend/app/storage/parsers.py::_parse_fasta` -- no tool, no job,
runs at ingest for every FASTA. Completeness is compleasm, built from source in
`backend/Dockerfile` (`backend/scripts/install-compleasm.sh`), registered in
`backend/app/pipelines/assembly_qc_registry.py`, run by the
`assess_completeness` queue job (`backend/app/queue/assembly_qc_handlers.py`),
launched through `pipeline_service.launch_completeness`, offered by
`suggestion_service.build_completeness_card`, and reachable from the UI via a
"Score completeness" button and `CompletenessDialog.tsx` alongside the
Actions-tab card. `download_lineage` is its own job
(`backend/app/queue/lineage_handlers.py`): a completeness run must not depend
on the network partway through, the same rule Clair3's baked-in models follow.

The design keeps this entry's original diagnosis and changes both of its
tools. Read the original below first -- it is still why this exists -- then
the delta, then what shipped differently from the design.

### Original entry

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

### What the design changed, and why

The one sentence that survived unaltered is the one about lineage datasets
being reference-download machinery rather than a tool probe. That was right.

**Neither named tool is what gets built.**

- **QUAST is not in Debian trixie.** Read out of the running `api` container,
  not recalled: it is not in the archive at all, only *referred to* by
  `med-bio` and `multiqc`, and `apt-get install quast` reports "no installation
  candidate". So the entry's cheap-looking half costs a source build, to obtain
  numbers we can compute in a function. Contiguity is computed in
  `_parse_fasta` instead -- `sequence_n50`, `sequence_n90`, `sequence_l50`,
  `sequence_auN`, `sequence_gap_count`, `sequence_gap_bases` -- with no tool,
  no image growth, and coverage of every FASTA at ingest rather than only ones
  a QC job was run on. QUAST's genuinely unreplaceable capability is
  *reference-based* misassembly detection; that becomes its own entry.
- **compleasm replaces BUSCO**, 10-20x faster and without BUSCO's metaeuk step.
  BUSCO stays declared-but-unavailable in the registry the way `HIFIASM_SPEC`
  is, because it is the name a reviewer asks for.

**`assembly_n50` is deleted.** `parse_assembly_info` computes it from Flye's
table; the parser will compute it from the FASTA bytes. Two facts that are
supposed to agree, on one object, is a bug with a delay fuse. Nothing renders
it and no test asserts it.

**Scope widened: uploaded assemblies count.** The card keys on shape, not on
provenance, so an assembly a user brought with them gets the same QC. Which
means it inherits the align card's trap directly -- `protein.faa` and
`cds_from_genomic.fna` are FASTA -- and the card must exclude roles `PROTEIN`
and `TRANSCRIPT` explicitly.

**Facts namespace is `assembly_completeness_*`, not `busco_*`.** `busco_score`
is taken: it is UniProt proteome metadata (`app/api/v1/uniprot.py`), meaning
something NCBI computed about a proteome rather than something we measured
about an assembly. One object can carry both. All four categories are stored,
not a headline number -- duplicated percentage is the haplotypic-duplication
signal and a single "97.3%" discards it. Lineage and ODB version are stored
too: compleasm defaults to odb12, BUSCO 5.5 is odb10, and percentages across
versions are not comparable.

**The contig-length paragraph above is stale** and was already corrected
elsewhere in this file: `sequence_longest`/`sequence_shortest` shipped in
`19f6b62`. N50 over a FASTA was the part genuinely missing, and it is the
contiguity work above.

### Traps found while designing this, for whoever builds the rest

- **`meryl` is in trixie and is the wrong meryl.** `0~20150903+r2013-9+b1` is
  the Celera Assembler k-mer suite; Merqury needs Marbl meryl 1.3+. The probe
  would succeed against it and report an available tool that cannot do the job.
- **Debian's BUSCO cannot do eukaryotes as installed.** Its dependencies bring
  `prodigal`, not `metaeuk`; `metaeuk` is a separate package. A green install,
  a runtime failure.
- **compleasm's release asset and biocontainer are both x86-only.**
  `compleasm-0.2.9_x64-linux.tar.bz2` is the only release asset, and
  `quay.io/biocontainers/compleasm:0.2.9--pyhdfd78af_0` reports
  `is_manifest_list: false`. Either would repeat the bwa-mem2 arm64 failure.
  Build from source with miniprot from source -- miniprot supports NEON
  upstream, so unlike bwa-mem2 there is nothing to patch.
- **`assembly_runner.py` has no tests at all**, despite the assembly design's
  testing section listing them. `grep` for `parse_assembly_info` across
  `backend/tests/` returns nothing. The contiguity work edits that file.
- **`PipelineType` is not backend-only, whatever `grep` says.** Every hit in
  `backend/app/` is in `tools.py`, which reads as "a heading on the Software
  help page". But the value crosses the API and `PipelineToolSelector.tsx`
  filters the user's tool picker on it, so declaring a QC tool as `ASSEMBLE`
  would offer it as something to assemble *with*. An earlier draft of the
  design got this wrong on exactly that grep. The fix is a new `ASSEMBLY_QC`
  member; `PIPELINE_LABEL` is an exhaustive `Record<PipelineType, string>`,
  so the build fails until it is labelled.

### Still open: what this entry does not close

The heading above is `— FIXED` for contiguity and completeness specifically,
not for post-assembly QC as a whole -- these are why the entry stays in this
file rather than moving to `docs/TODO-done.md`.

- ~~**QUAST's reference-based misassembly detection.** Genuinely unreplaced by
  anything here; deserves its own entry rather than being folded into this
  one's closure.~~ **FIXED, 2026-08-06.** Shipped as QUAST 5.3.0, GitHub #62.
  See below.
- ~~**CRAQ, GCI and Merqury** all need reads realigned to the assembly, which is
  the **Pilon** entry's blocker rather than this one -- they are not peers of
  completeness and contiguity, and building them means building that first.~~
  **Wrong on both counts, corrected 2026-08-05** by the epic design note; see
  "CRAQ and GCI were never blocked on Pilon" below. Realigning reads to a
  user's own assembly has worked since the assembly work landed, and Merqury
  is k-mer based and needs no alignment at all. GitHub #63 (CRAQ), #64
  (Merqury), #65 (GCI). ~~**CRAQ**~~ **FIXED, 2026-08-06.** Shipped as CRAQ
  1.10, GitHub #63. See below. ~~**Merqury**~~ **FIXED, 2026-08-07.** Shipped
  as Merqury 1.4.1 + meryl 1.4.2, GitHub #64. See below. ~~**GCI**~~ **FIXED,
  2026-08-07.** Shipped as GCI 1.0, GitHub #65. See below.
- **gfastats** is superseded by computing contiguity here, not built.
- **Contamination screening** is a real axis nothing here covers and is a
  named non-goal: FCS-GX's database is ~470 GB.

### QUAST misassembly detection shipped, 2026-08-06

Design: `docs/superpowers/specs/2026-08-05-remaining-post-assembly-qc-
design.md`. Plan: `docs/superpowers/plans/2026-08-05-quast-misassembly-qc.md`.
GitHub #62.

Both of the two numbers the design measured held once wired into a real
handler and re-checked against a clean image rebuild: **8.6 MB** installed
(`backend/scripts/install-quast.sh`, GitHub tarball, patched, trimmed) and
**3-4 s** for a 12 Mb yeast assembly against a reference. The original entry's
claim that QUAST was too expensive to be worth it does not survive contact
with an actual install.

Code: `backend/app/pipelines/quast_runner.py` (command builder, two output
parsers), `backend/app/queue/assembly_qc_handlers.py::assess_misassemblies`
(the job) and `_copy_report` (the HTML report), `backend/app/services/
pipeline_service.py::launch_misassembly_qc`, `POST /pipelines/misassemblies`,
`suggestion_service.build_misassembly_card`, `AssemblyFacts.tsx`'s
Misassembly QC block.

**What the implementation found that the design and plan did not know yet.**
All found by running the thing against real data, not by re-reading either
document -- each would have shipped a real vulnerability or a wrong number
silently.

- **QUAST's HTML report is a stored XSS waiting to happen, and the design's
  own "just serve it like FastQC's report" plan would have shipped it.**
  QUAST sanitizes contig names (`qutils.correct_name`) but not the assembly
  *label* (`qutils.correct_asm_label`), and the label is taken from the input
  filename. An input named `ev<img src=x onerror=alert(7)>.fasta` puts that
  tag verbatim and unescaped into `report.html` -- confirmed by exploiting
  it, then re-confirmed a second time with an independent payload during
  final verification. Closed at the handler, not the report route:
  `assess_misassemblies` always links its input under a fixed filename and
  always passes QUAST a fixed `-l assembly` label, never the object's own
  name. Found while writing the implementation plan, before any code
  existed to ship the bug -- worth recording as the reason "plan before
  the security-sensitive phase" earned its keep here.
- **`assembly_reference_unaligned_contigs` parsed as a float, not an int**,
  invisible to every unit test because `2 == 2.0` in Python and the existing
  assertions were equality-only. Found by running the real handler against
  real data and reading the actual returned type. Fixed in `quast_runner`'s
  `_INT_FACTS`, with a new `isinstance` test that would have caught it.
- **`ragtag`/`polypolish` are missing from `tools.all_tools()`**, discovered
  while adding `quast` to the same list -- a pre-existing gap this entry does
  not close, left alone rather than folded into an unrelated diff.
- **The reference's own filename is safe, only the assembly's is not.**
  Verified both directions before assuming: a hostile *reference* filename
  goes through QUAST's sanitizing `correct_name` and comes out mangled but
  harmless, so only the reference gets its normal `_named_link` treatment;
  the assembly does not.

**Two of those bullets went stale and were corrected 2026-08-05** by the epic
design note (`docs/superpowers/specs/2026-08-05-remaining-post-assembly-qc-
design.md`, GitHub #13). Left in place above rather than rewritten, because
each was correctly reasoned from what it checked and the *way* they went wrong
is the useful part.

- **QUAST costs far less than "not in apt" implied.** The apt verdict still
  holds -- re-verified, no candidate in trixie -- but it was allowed to imply
  a source build, and there is none. QUAST prefers a `PATH` minimap2 over its
  bundled copy (`min_version='2.19'`), the 400 MB tarball trims to 8.6 MB with
  everything a reference run never touches removed, and a 12 Mb yeast assembly
  runs in 3-4 s. The real install cost is a GitHub tarball (PyPI stops at
  5.2.0) plus a two-line `distutils` patch for Python 3.12. Now GitHub #62.
- **CRAQ and GCI were never blocked on Pilon, and Merqury needs no alignment
  at all.** Pilon is out permanently (#23 swapped it for Polypolish). Reads
  can already be realigned to a user's own assembly: `results.py:1246` roles an
  assembly FASTA `REFERENCE` on ingest, and `_check_reference` requires only
  READY plus a FASTA kind, so the ordinary align pipeline has covered this
  since the assembly work itself landed -- discharged as a side effect, with
  nobody going back to say so. Merqury was in the group by mistake: it is
  k-mer based, and its actual blocker is Marbl meryl (the Debian-meryl trap
  recorded above, re-confirmed). Now GitHub #63, #64, #65.

### What the implementation found that the design did not know yet

All found by actually building and running the thing, not by re-reading the
design -- each would have shipped a wrong number or a wrong claim silently.

- **A lineage name's `_odb10`/`_odb12` suffix is decorative.** compleasm's own
  `download_lineage` rewrites it to match whatever `--odb` the command line
  carries (`"{}_{}".format(lineage.split("_")[0], odb)`), discarding any
  suffix the caller wrote. Requesting `bacteria_odb10` with the default `--odb
  odb12` actually downloads and scores `bacteria_odb12` -- verified against a
  real run. `completeness_runner.CompletenessParams.lineage` is therefore
  always a bare name (`bacteria`, not `bacteria_odb12`); the `odb` field is
  the only thing that controls the version.
- **compleasm's `run` subcommand writes an `I:` (interspaced) line
  unconditionally**, contradicting what reading the source in isolation
  suggested. The `analyze` subcommand's copy of the same print block has that
  line commented out; `run` -- the one this application calls -- does not. A
  parser written from the commented-out copy would have broken on the first
  real summary.
- **`Downloader.__init__` fetches `eukaryota_<odb>` and ~100MB of placement
  files on every download, regardless of which lineage was requested.**
  Verified: `compleasm download bacteria --odb odb12` also produced
  `eukaryota_odb12` and `placement_files/` on disk. One-time cost per
  `library_path`, not per lineage, but worth knowing before estimating how
  long a first download takes.
- **Lineage dataset size varies far more than expected.** `bacteria_odb12` is
  ~65MB; `saccharomycetaceae_odb12` (family-level, used for yeast) is ~1.1GB
  including the placement files, and took roughly 20 minutes to download and
  extract in a moderately loaded container -- not the ~1 minute a small
  bacterial lineage takes. The download dialog and the job's own lease
  (`download_lineage`'s `extend_lease(1800)`, 30 minutes) were sized with this
  in mind; a still-larger vertebrate lineage could plausibly need more.
- **`PipelineType` needed a real backend-crossing member.** An earlier draft of
  the design reused `PipelineType.ASSEMBLE` for compleasm, reasoning from a
  backend-only `grep` that the enum only drives a Software-help-page heading.
  It also crosses the API: `PipelineToolSelector.tsx` filters the user's tool
  picker on it, so compleasm would have been offered under "an assembler",
  beside Flye. Fixed with `PipelineType.ASSEMBLY_QC` before anything shipped.
- **End-to-end, against the real seeded yeast genome
  (`GCA_000146045.2_R64_genomic.fna`, organism "Saccharomyces cerevisiae
  S288C"):** lineage inference correctly produced `saccharomycetaceae`, not
  the `eukaryota` domain fallback. compleasm reported 99.94% complete
  (99.91% single-copy, 0.03% duplicated, 0.06% fragmented, 0% missing, 3282
  markers) -- plausible for a finished reference genome. Contiguity on the
  same file: N50 924,431 bp across 6 sequences (L50=6, matching its 16
  nuclear chromosomes plus mitochondrion), zero gaps. Re-enqueuing
  `ingest_headers` on an object ingested before the contiguity change backfilled
  `sequence_n50` and friends without disturbing the `assembly_completeness_*`
  facts a separate job had already written -- confirming the merge-not-replace
  behavior the design relied on but never ran.
- Confirmed against the real `protein.faa` and `rna.fna` (transcript role) for
  the same organism: `build_completeness_card` returns `None` for both, and
  the genome FASTA gets a real card -- the role-exclusion trap the design
  worried about inheriting from the align card does not reproduce here.

### CRAQ assembly error detection shipped, 2026-08-06

Design: `docs/superpowers/specs/2026-08-06-craq-assembly-error-detection-
design.md`. Plan: `docs/superpowers/plans/2026-08-06-craq-assembly-error-
detection.md`. GitHub #63.

Reference-*free* assembly error detection from read-clipping signals,
complementary to QUAST (#62) rather than a second flavour of it -- it gates
on a BAM aligned to the assembly, never on a reference. Consumes BioFlow's
own align pipeline output directly (upstream calls pre-made BAMs "highly
recommended"), auto-pairing a short-read and/or long-read BAM when exactly
one of each exists against an assembly, refusing ambiguity the same way
`launch_misassembly_qc` refuses an ambiguous reference.

Code: `backend/app/pipelines/craq_runner.py` (command builder, report/bed
parsers), `backend/app/queue/assembly_qc_handlers.py::assess_assembly_errors`
(the job), `backend/app/services/pipeline_service.py::launch_assembly_error_qc`
and `alignments_against`, `POST /pipelines/assembly-errors`,
`suggestion_service.build_assembly_error_card`, `AssemblyFacts.tsx`'s
Assembly errors block. Chimera-breaking (`-b`, opt-in, never offered by the
Actions card) ingests its corrected FASTA as a brand-new `REFERENCE` object
rather than replacing the input assembly -- a deliberate, explicit widening
of epic #13's "does not produce a replacement assembly" scope line, recorded
as a comment on #13 rather than left to read as a silent contradiction.

**Measured against a real run, not assumed:** 43 MB installed (`du -sh
/opt/craq`, includes the pinned commit's shallow-cloned `.git`) and 61-67s
end to end against `GCA_000146045.2_R64_genomic.fna` (12.1 Mb, 16 sequences)
with a DRR1066343 short-read BAM already aligned to it -- fast because
supplying a pre-made BAM skips the read-mapping step CRAQ's own README
calls its most expensive part.

**What the implementation found that the design and plan did not know yet,**
all from actually running CRAQ against real data rather than re-reading its
source more carefully -- a repeat of the QUAST slice's own lesson, this time
needing two real end-to-end runs before every fact matched:

- **The design's own source-read of the report filename was wrong**, and it
  shipped in this repo's spec for hours before a real run caught it. The
  spec concluded `<genome_basename>_final.Report` from `runAQI.sh`'s use of
  `$name` without confirming what `$name` resolves to. All three of
  `runAQI.sh`/`runAQI_SMS.sh`/`runAQI_NGS.sh` hardcode `name="out"` at line 5
  -- the report is unconditionally `runAQI_out/out_final.Report`. Every real
  run raised a spurious `RetryableError` until this was fixed, even though
  CRAQ had genuinely succeeded and written real output under a different
  name than the handler was looking for.
- **The whole-assembly summary row is keyed `Genome`, not `all`.** Confirmed
  hardcoded in `src/final_short_report_minlen.pl:42`, a literal unrelated to
  any input filename -- upstream-stable, not particular to one run. Every
  unit test's own fixture used `"all"` throughout, so all of them passed
  while testing an assumption no real report ever satisfies. A regression
  test asserting a per-contig row (which looks equally plausible) is *not*
  mistaken for the `Genome` row now guards the same class of bug from
  recurring.
- **`bc` was missing from the image**, a real if low-severity install gap.
  `runSR.sh`/`runLR.sh`/`runAQI.sh` call it 7 times total, all in
  parameter-sanity guards (negative/zero checks on mapq, threads, clip-rate
  cutoffs). Confirmed these guards fail *open* without it -- `bc: command
  not found` followed by a harmless `[: -eq: unary operator expected` --
  so the run still completed with correct facts either way. Fixed anyway:
  BioFlow never passes a parameter that would need catching today, but a
  silently-skipped safety check is the same shape of bug this file's own
  "Audit the hand-maintained registries" entry warns about elsewhere, for a
  few KB of image size.
- **Two integration bugs caught by code review before either reached a real
  run**, both in how a BAM's `.bai` index reaches the handler. BioFlow's
  storage is content-addressed, so a BAM and its index are separate
  `DataObject`s with no path relationship -- the first draft's path-guessing
  fallback could never find a BioFlow-produced BAM's index at all. Fixed by
  resolving the index as its own sidecar (`SidecarRole.BAI`, matching
  `launch_bam_stats`'s existing `bai_sha256`/`bai_path` pattern) -- and then
  a second review pass found the launch path's payload keys
  (`ngs_bam_bai_sha256`) didn't match what the handler actually read
  (`ngs_bai_sha256`), which would have silently reproduced the exact same
  failure the first fix closed. Both are pinned by regression tests
  asserting the literal key-name strings match, not just that some key
  exists.
- **The Actions card could go AVAILABLE for a BAM set the launch path would
  then reject.** `build_misassembly_card` already refuses an ambiguous
  reference; the CRAQ card's first draft had no equivalent check for two
  same-chemistry BAMs. Fixed by having the card call the exact same
  `alignments_against` function the launch path uses, so the two can no
  longer drift apart on what counts as ambiguous.
- Confirmed end-to-end in the real UI, not just in stored facts: a
  short-reads-only run against the real yeast assembly rendered R-AQI (0.4)
  and CRE (656) with the caveat "Short reads only: structural errors are not
  reported, because CRAQ can barely detect them without long reads" -- and
  genuinely no AQI/S-AQI/CSE rows, not a blank or a zero.

### Merqury k-mer QV assessment shipped, 2026-08-07

Design: `docs/superpowers/specs/2026-08-06-merqury-kmer-qv-design.md`. Plan:
`docs/superpowers/plans/2026-08-06-merqury-kmer-qv.md`. GitHub #64.

Reference-*free* base-level accuracy (QV): a meryl k-mer database built from
the reads, compared against the assembly's own k-mers, with no alignment and
no reference genome -- the fourth QC axis alongside completeness (compleasm),
contiguity, and error detection (CRAQ, #63). The design's central premise
(meryl 1.4.2 is the first-ever Marbl release to ship a Linux-arm64 binary, so
this slice is a tarball extract rather than a source build) held: measured
0.2s to extract vs. the C++ source build the design was written expecting to
need.

Code: `backend/app/pipelines/merqury_runner.py` (command builders, `.qv` /
`completeness.stats` parsers), `backend/app/queue/assembly_qc_handlers.py::
assess_assembly_qv` (the job) and `_apply_assess_assembly_qv` in `results.py`
(facts merge plus meryl-db sidecar caching), `backend/app/services/
pipeline_service.py::launch_qv_qc` and `_materialize_meryl_cache`,
`POST /pipelines/assembly-qv`, `suggestion_service.build_qv_card`,
`AssemblyFacts.tsx`'s K-mer accuracy block and spectra-cn plot gallery.
`SidecarRole.MERYL_DB` (confirmed the derivable-registry kind per this file's
"Hand-maintained registries" entry, so no `_SIDECAR_ROLES` edit was needed
beyond the enum member itself).

**The k-mer database cache works end to end, confirmed against real data, not
just unit tests.** A second QV run against a different assembly from the same
reads hit the cache (`meryl_cache_hit` logged, no `meryl count` subprocess,
the job's own log jumping straight to `merqury.sh`'s output) rather than
rebuilding -- the whole point of caching a multi-hour-scale artifact, and
nothing in the unit tests exercised the real round trip end to end.

**What the implementation found running against real data (S. cerevisiae R64
+ DRR1066343, 2026-08-07) that no amount of re-reading Merqury's source would
have caught**, the same lesson QUAST's and CRAQ's slices already recorded
here, again:

- **A plain-text FASTQ, linked under a fixed `.fastq.gz` name, silently built
  an empty 0-kmer database instead of erroring.** `_resolve_read_inputs`
  hardcoded every read file's link name to `.fastq.gz`, so `meryl count`
  tried to decompress a file that was not gzipped. The `meryl count` step
  still reported "Finished counting" against 23.7 billion input bases, and
  the resulting database was silently empty (`meryl statistics` reported 0
  unique/distinct/present k-mers) -- a green subprocess exit hiding a
  correctness bug identical in shape to the exit-0-but-wrong output CRAQ's
  slice already hit with its report filename. Fixed to preserve the source
  object's real suffix, matching the by-name gzip-detection convention
  `align_runner._is_gzipped` already established for aligner inputs.
- **Merqury's own k-detection line is incompatible with meryl 1.4.2's output
  format, and merqury.sh does not check for this failure.**
  `eval/spectra-cn.sh` detects k via
  `meryl print $read | head -n 2 | tail -n 1 | awk '{print length($1)}'`,
  written against a meryl whose `print` emitted k-mer data starting at line
  2. Marbl meryl 1.4.2 -- the version this image pins specifically for its
  arm64 binary -- prints an 11-line diagnostic banner before any k-mer data,
  so line 2 is always blank, `k` ends up empty, `meryl count k=` fails with
  "Kmer size not supplied", and every downstream step in the script silently
  produces empty output. `merqury.sh` itself still exits 0, because it never
  checks `spectra-cn.sh`'s exit code -- a job that "succeeds" while writing
  no `.qv` file at all, until the handler's own `if not qv_file.exists()`
  check catches it. Patched at install time (`install-merqury.sh`) to filter
  for a line that looks like actual k-mer data rather than trusting a fixed
  line number.
- **The guessed spectra-cn plot filenames were wrong.** The frontend task
  guessed `qv.spectra-cn.{fl,ln,st}.png` and `qv.qv.png` from documentation,
  explicitly flagged at the time as needing real-run verification. Merqury
  actually writes `qv.in_assembly.spectra-cn.{fl,ln,st}.png` and
  `qv.spectra-asm.{fl,ln,st}.png` -- none of the four guessed names matched
  any real output file. The `in_assembly` component comes from the handler's
  fixed assembly link name (`assembly.fasta` -> `in_assembly.fasta` via
  `_named_link`), stable across every run rather than something to
  rediscover per run. Confirmed in a real browser session after the fix: all
  six corrected plot URLs return 200.
- **`assembly_qv_tool_version` stored merqury.sh's usage banner, not a
  version.** `tools.merqury()`'s probe reports the bare-call usage text as
  `version` because that is genuinely the only output the tool ever produces
  (no `--version` flag exists) -- confirmed a real run wrote the literal
  string `"Usage: merqury.sh [-c] <read-db.meryl> [<mat.meryl> <pat.meryl>]
  <asm1.fasta> [asm2.fasta] <out>"` into this fact and, before the fix, onto
  the Quality tab's header line. Replaced with a `MERQURY_PINNED_VERSION`
  constant matching what `install-merqury.sh` actually installs.
- **`mem_mb=16384` was a directionally-reasonable but untested placeholder.**
  Measured: 8531324928 bytes (~8.14 GiB) peak RSS, 5m50s wall time, for
  23.7 billion input bases (21.4M reads) against a 12 Mb genome. Corrected to
  `12288` for headroom over the measured figure rather than the original
  guess.
- **The Celera-meryl rejection regex matched a version string the binary
  never actually prints.** Caught in code review before this shipped, not by
  a real run: the original check matched Debian's dpkg *package* version
  (`0~20150903+r2013-9+b1`), but the real Celera `meryl --version` prints
  `"Unknown option '--version'."` and exits 0 -- the dpkg version string
  never reaches the probe at all. Fixed to match the actual message text.
- **A partially-cached meryl database had no way to be told apart from a
  complete one**, also caught in code review. The applier that ingests a
  fresh k-mer database as sidecar files recorded nothing about how many
  files it expected to produce, so a partial-ingest failure (one file lost,
  the rest cached) looked identical to success from the cache-reading side
  -- the same STAR-index failure shape this file's own registry-audit entry
  already documents, recurring in a new place. Fixed by stamping
  `meryl_db_expected_count` on every ingested member and refusing any group
  whose size does not match.

### GCI assembly continuity inspection shipped, 2026-08-07

Design: `docs/superpowers/specs/2026-08-06-gci-assembly-continuity-design.md`
(committed on a sibling branch alongside #64's spec, not yet merged to `main`
as of this entry). Plan: `docs/superpowers/plans/2026-08-06-gci-assembly-
continuity.md` (same branch). GitHub #65.

Long-read continuity inspection: scores an assembly by aligning long reads
back to it and finding regions unsupported by read evidence, complementary to
CRAQ (#63, reference-free structural/regional errors) and QUAST (#62,
reference-based misassemblies) rather than a third flavour of either. Gates
on a long-read BAM aligned to the assembly, same "consume BioFlow's own align
output" shape CRAQ established.

**The issue's own gating question -- "is a minimap2-only GCI run
methodologically honest, or a misuse of the tool?" -- was answered before any
implementation planning, per the issue's own requirement, and the answer
reversed the issue's premise.** GCI never invokes an aligner: every aligner in
its Requirements list, including winnowmap, carries the same "(optional, but
wanted for mapping)" parenthetical, and GCI consumes finished BAM/PAF through
`--hifi`/`--nano`. The issue was filed believing "the real prerequisite is a
second aligner" (winnowmap, no Debian candidate) -- reading a suggestion list
as a dependency list. This slice needed no source build at all: GCI is pure
Python, MIT licensed, installed via `backend/scripts/install-gci.sh` as a
pinned-commit clone (`543cd41`, GCI's `main` HEAD as of 2026-08-06), the same
pattern a build-requiring tool would use minus the build. A minimap2-only run
is genuinely honest, with a disclosed sensitivity cost recorded as a fact
(`assembly_continuity_aligners`) rather than omitted -- upstream's own FAQ
reports similar scores between aligner pairs, unlike CRAQ's CSE, which
upstream says is "hardly detected" on NGS-only runs and is omitted outright.
Winnowmap itself is filed as a future enhancement, now unblocked by #64
(Marbl meryl 1.4.2, arm64).

Code: `backend/app/pipelines/gci_runner.py` (command builder, `.gci` parser),
`backend/app/queue/assembly_qc_handlers.py::assess_assembly_continuity` (the
job), `backend/app/services/pipeline_service.py::launch_continuity_qc`,
`gci_slot_for_chemistry`, and `_gci_candidates`, `POST
/pipelines/assembly-continuity`, `suggestion_service.build_continuity_card`,
`AssemblyFacts.tsx`'s Continuity block.

**The load-bearing decision, called out before implementation and re-verified
at every layer through to merge: PacBio CLR is refused, never routed to
`--hifi`.** CLR is long-read and therefore looks eligible, but GCI has
exactly two slots and its identity/clipping filters assume HiFi-grade
per-read accuracy -- CLR's error profile isn't close enough, and routing it
anyway would produce a confidently wrong score rather than an error.
`gci_slot_for_chemistry` is the single function every layer routes through
(the auto-pair path, the explicit-BAM-id path -- which re-derives chemistry
rather than trusting the id's label, closing the one bypass a naive
implementation would leave open -- and the suggestion card, which consumes
the same candidate-splitting helper the launch path uses rather than an
independently-derived approximation). `SHORT` and `UNKNOWN` are refused
alongside CLR: GCI has no short-read slot at all, and `UNKNOWN` diverges
deliberately from `read_chemistry_for_alignment`'s usual "fall back to a
conservative short-read default" contract, which is correct for picking an
alignment preset and wrong here.

**Verified against upstream's own real test data before merge, following the
lesson CRAQ's and QUAST's slices both paid for.** The `.gci` parser was
written from the README's prose description of the file, not from a run --
flagged as unverified at every step of the plan. A real run against GCI's own
Zenodo example dataset (`https://zenodo.org/records/12748594`, the MH63 rice
assembly) produced output the existing parser handled correctly without any
code change: the parser's existing `len(fields) < 6` and `fields[0] !=
"Genome"` filters happened to already skip both an undocumented leading
`"HiFi:"` label line and a trailing dash-separator line that neither the
README's prose nor the plan anticipated. Locked in with a regression test
built from the real captured output rather than left as an accident. Also
confirmed against the real run: `-o MH63` produces `MH63.gci` exactly, so the
handler's `out_dir / "gci.gci"` assumption (given `prefix="gci"`) holds.

**Two hand-maintained-registry entries this repo's own CLAUDE.md warns
about, both caught by running the full suite rather than by the plan
anticipating them:** the new job handler needed an entry in
`provenance_walker.py`'s `_STEP_VERBS` (silently un-narrated otherwise, the
exact shape the STAR incident recorded), and the new tool needed adding to
`tools.py`'s exhaustiveness set covering `all_tools()`. Neither was in the
plan's file list; both were required for the suite to stay green.

**One review-driven correction to the Dockerfile install**: the first draft
had no `ARG GCI_COMMIT` threading the pinned commit through as a build-time
override, unlike every other pinned-commit tool in the same file (CRAQ,
BWA-MEM2, Clair3, compleasm, polypolish, QUAST) -- `install-gci.sh` already
supported the override via its own default, but nothing in the Dockerfile
exercised it. Fixed to match the established pattern.

No `JobResources` correction was made: the Zenodo example dataset is
subsampled and completed in under a minute against the declared `cpu=8,
mem_mb=16384`, nowhere near stressing either figure -- a real production-scale
genome would need separate re-measurement, left as a known gap rather than
guessed at from an unrepresentative run.

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
