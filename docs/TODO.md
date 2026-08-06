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
  1.10, GitHub #63. See below. Merqury and GCI are still open.
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

`backend/app/services/summary_prompt.py` is the existing pattern to follow.
As of the AI provider settings feature (2026-08-03,
`docs/superpowers/specs/2026-08-03-ai-provider-settings-design.md`), the model
is no longer a single hardcoded host process -- routing goes through
`app/services/ai/router.resolve(TaskSlot)`, and the `host.docker.internal`
address is now just the "Local / custom" preset's default, not a universal
fact. This walker would want its own `TaskSlot` member (e.g.
`PROVENANCE_NARRATIVE`) rather than assuming any particular provider.

The hard constraint: this output will be pasted into papers. It must never
invent a step or a version. Prefer a narrative assembled from facts with the
model only doing the prose, over asking the model to infer what happened.

Other candidates worth considering under the same heading: explaining *why* a
QC run failed a threshold, and suggesting the next pipeline step in prose
alongside the Actions cards.

