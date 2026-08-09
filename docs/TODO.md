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

## QC report directories can vanish from disk while the object's facts still point at them

Raised: 2026-08-02, found while investigating a user report of a 404 on the
FastQC report link for a completed, successful QC job. Tracked as
[#10](https://github.com/syntheticgio/bioflow/issues/10).

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

This entry stays open: the root cause is still unconfirmed, evidence
retention/durable sink and the mtime-timestamp check are still open, and this
was written and reasoned about, not yet observed catching a real recurrence.

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

## Resource limits and intelligent enforcement — PARTIALLY FIXED

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


---

# Deferred findings

See CLAUDE.md, "Closing out a TODO entry", for what to do when one of these
lands. Short version: mark it `— FIXED` with a note, keep the body, and never
trust a plan's checkboxes as evidence it shipped.

## `SequenceCharts.tsx`'s four charts duplicate the same axis/hover scaffolding

Raised: 2026-08-09, deferred during code review of Task 8 of the
sequence-composition-and-bias-QC plan
(`docs/superpowers/plans/2026-08-09-sequence-composition-bias-qc.md`).

`BaseCompositionChart`, `QualityChart`, `GcDistributionChart`, and
`NContentChart` (all in `frontend/src/components/SequenceCharts.tsx`, now
~630 lines) each hand-roll the same shape: a `pad`/`plotW`/`plotH` layout
object, an `x`/`y` scale closure, a transparent hit-`<rect>` for hover, and a
crosshair-plus-readout interaction. The review that added the fourth chart
(`NContentChart`, Task 8) flagged this as the point a prior review had
already named as the natural trigger for extracting shared scaffolding --
Task 7's review, adding the third chart, said so explicitly and deferred it
to "whenever a fourth chart lands." It landed, and the extraction still
didn't happen, because Task 8's scope was the new chart's behavior, not a
refactor of its siblings.

Nothing is broken. Each chart is independently correct, readable, and
consistent with the file's established hand-rolled-SVG convention (no
charting library, per the file's own top-of-file rationale). This is a
maintainability note, not a defect: a fifth chart would be the next natural
trigger, and by then the shared shape (padding object, x/y scale builders,
hover-rect wiring, crosshair rendering) is worth factoring into a small
internal helper or hook that the existing four charts adopt at the same time.

Touches: `frontend/src/components/SequenceCharts.tsx`.

## Neither model segments by thread count — PARTIALLY FIXED

**Partially addressed 2026-08-08:** the segmentation machinery landed --
`_fit_segmented` in `backend/app/services/timing_service.py` groups
`JobRunTiming` records by `threads` and fits each group with
`>= MIN_SAMPLES` (5) same-thread-count rows, falling back to the existing
pooled bytes-only fit otherwise. `estimate()`, `estimate_memory()`, and
`memory_estimate.resolve()` all take an optional `threads` argument now, and
the three real call sites that pass a job's thread count through
(`worker.py:_eta_model_ms`, and two calls inside `jobs.py:get_job`) do so via
`job.payload.get("threads")`. `stats()`'s `/timing-model` output gained a
`segments` list per job type.

**Still open:** at the time this landed, the real `job_timings` collection
held only 9 rows with a thread count at all (`align_reads @ 4` x5,
`quantify @ 4` x4) -- one thread value per job type, nothing to segment
against. The two real-row acceptance criteria on
[#8](https://github.com/syntheticgio/bioflow/issues/8) --
"thread-segmented duration and memory fits use real computation rows" and
"real-row verification... cover segmentation and fallback" -- stay open
until enough varied-thread runs accumulate. See
`docs/superpowers/specs/2026-08-08-thread-count-segmentation-design.md`
for the full design and why those two criteria were deliberately deferred
rather than faked against fixtures. Additionally, `memory_estimate.resolve()`
has three further callers inside `backend/app/services/pipeline_service.py`
(the pre-flight `LoadGovernor` memory-reservation checks, around lines 1293,
1472, 3290) that were not wired to pass `threads=` in this pass and remain
byte-only pending a follow-up.

---

## Two orchestrator sites resolve ports from the static spec, not `ports_for(node)`

Raised: 2026-08-08, deferred during code review of Task 4's tool-choice ports
work (GitHub issue #94, Task 4).

Task 4 introduced `ports_for(node)`
(`backend/app/pipelines/node_types.py`) as the correct way to resolve a
node's *actual* input/output ports once its chosen tool is taken into
account -- e.g. a STAR-configured `align` node gains an optional
`annotation` GTF input that `NODE_TYPES[node.node_type].inputs`, the static
per-node-type spec, does not know about. Two sites in
`backend/app/services/workflow_orchestrator.py` still read the static
`spec.inputs` directly instead:

- `_is_multi_port` (line 140) looks up a port by name in `spec.inputs` to
  decide whether an edge should be collected into a list. It is reached from
  `_bound_inputs`, which resolves every input a node launches with.
- `_advance` (line 259) computes `missing = [p.name for p in spec.inputs if
  p.required and p.name not in inputs]` to decide whether a node has enough
  bound inputs to launch.

This is safe today because the only tool-added port that exists yet --
STAR's `annotation` GTF input, added by
`tool_choice._resolve_align_ports` -- is `required=False` and not
`multiple=True`. Neither dormant site can currently produce a wrong answer:
`_is_multi_port` only misclassifies ports that are `multiple=True` in the
per-tool set but absent (or non-multiple) in the static set, and `_advance`'s
missing-input check only misfires on a `required=True` per-tool port the
static set doesn't have.

The moment a future tool choice adds a `required=True` or `multiple=True`
port for one tool value, both sites will misbehave silently: `_advance`
could decide a node's inputs are complete when the tool-specific spec still
needs one more, launching it with an incomplete `inputs` dict; or
`_is_multi_port` could fail to collect a multi-port's second edge into a
list, silently dropping a contribution. Either failure surfaces deep inside
the launcher as a confusing error attributed to the wrong node, per the
comment already on `_advance`'s missing-input branch -- not as a validation
message pointing at the actual gap.

Fix by changing both sites to call `ports_for(node)` and use its returned
input tuple in place of `spec.inputs`, the same migration Task 4 already
made at the sites that resolve ports for validation and the API.

Touches: `backend/app/services/workflow_orchestrator.py`
(`_is_multi_port`, line 140; `_advance`, line 259),
`backend/app/pipelines/node_types.py` (`ports_for`).

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

## STAR's `annotation` input port cannot be wired through the UI

Raised: 2026-08-08, deferred during code review of Task 7's tool-selector
work (GitHub issue #94, Task 7).

Task 4 gave a STAR-configured `align` node an `annotation` input port typed
`PortType(format=FormatKind.GTF)` (`app/pipelines/tool_choice.py`,
`_resolve_align_ports`), and Task 7 makes that port render correctly on the
canvas -- the node grows a fourth port slot and the dot draws in the right
place. But `ACCEPT_CHOICES` in
`frontend/src/components/WorkflowCanvas.tsx` -- the list of file types an
input-slot node can be configured to *produce*, so it has something to wire
into a downstream port -- has a `gff:annotation` entry (format `"gff"`) and
no entry for format `"gtf"`. `FormatKind.GTF` is a distinct enum value from
`FormatKind.GFF` on the backend (`app/models/object.py`), and `portAccepts`
(`frontend/src/lib/workflowGraph.ts`) matches on format exactly, so a
`gff`-typed input slot can never satisfy a `gtf`-typed port.

The result is a control that is visible and functionally correct but
permanently unusable: nothing in today's UI can produce an output typed
`gtf`, so no edge can ever be drawn into STAR's `annotation` port. A user
sees the port, sees no way to feed it, and has no reason to suspect the gap
is one missing dropdown entry rather than a real modeling constraint.

Fix by adding a `gtf:annotation`-shaped entry to `ACCEPT_CHOICES` --
one line, no other change needed, since `portAccepts` already does the right
thing once a slot can claim the format. Left for Task 9 (binding files at
the input node) or a standalone follow-up rather than Task 7, since Task 7's
scope was rendering the port, not making every input-slot format reachable.

Touches: `frontend/src/components/WorkflowCanvas.tsx` (`ACCEPT_CHOICES`).
