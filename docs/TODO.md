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

## Notify on new feedback submissions

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

## Mobile-friendly view for select features

A limited, mobile-optimized UI for small screens (phones and tablets) rather than
a fully responsive redesign. The full interface is designed for desktop and
maintaining both at feature parity is not viable; instead, offer a minimal mobile
experience that covers the two workflows that matter on a phone.

**In scope:** Two features that serve actual mobile use cases --

1. **Activity progress view.** Check the status of a running pipeline job from your
   phone -- what step is it on, has it finished, did it fail. This is read-only,
   aligned with checking on a long-running task without re-running it.
2. **Trigger NCBI downloads.** Start a `fetch_reads_via_sra` job remotely to
   queue up a large download before leaving the office, or delegate a download to
   run on the server rather than draining your laptop's bandwidth. No need to
   monitor it from the phone, just dispatch it and walk away.

**Out of scope:** Building UI for alignment, QC, or assembly on a phone. These
require multi-step workflows, dense parameter selections, and real-time feedback
that do not translate to mobile well. The mobile view is for checking progress
and dispatching background jobs, not for orchestrating a full analysis.

**Implementation notes:** Detection can be simple (screen width under ~600px,
`window.matchMedia("(max-width: 600px)")`), and the view should be a separate
mobile-only route or a conditional render, not a responsive breakpoint tacked onto
the existing desktop UI. Starting from the Activity tab and the Downloads modal
(or a simplified version of it) is a reasonable foundation.

The risk that mobile-first design creates in a maintenance standpoint -- two
parallel UIs that drift from one another -- is mitigated by scope. Only two
screens exist, both read-heavy or dispatch-only; there is no complex state
management to keep synchronized between mobile and desktop versions.

Touches: `frontend/src/pages/`, routing layer, mobile-specific styling.

## Sharing between profiles

Depends on profiles. Share a file with another profile without copying the
bytes — which the storage layer already supports: a second `DataObject` with a
different `owner` pointing at the same digest, with the existing refcount
governing lifetime. The open work is policy and UI, not storage: how a share is
offered and revoked, whether the recipient sees it in their own explorer or a
separate shared area, and what happens to a share when the owner deletes their
copy (`GC_GRACE` in `blob_service.py` is currently the only thing between a
refcount reaching zero and the bytes being unlinked).

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

## No provenance panel for computation records

Raised: 2026-08-03, deferred while building computation records (same specs
as above).

`timing_service.records_for_object()` returns every run that touched an
object, failures included, and nothing renders it. The design listed
per-object provenance as one of three read surfaces; the accessor shipped
(and is covered by `backend/tests/queue/test_record_outcomes.py`), the UI and
the route exposing it did not.

Would show, per run: duration, peak RSS, thread count, tool and version, the
machine it ran on, and outcome (a failed run is the most useful record here --
it is the whole reason `records_for_object` includes failures when every other
reader filters them out).

Touches: `frontend/src/`, plus a new route in `backend/app/api/v1/jobs.py` (or
wherever an object-scoped provenance list belongs) exposing
`timing_service.records_for_object`.

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

- **QUAST's reference-based misassembly detection.** Genuinely unreplaced by
  anything here; deserves its own entry rather than being folded into this
  one's closure.
- **CRAQ, GCI and Merqury** all need reads realigned to the assembly, which is
  the **Pilon** entry's blocker rather than this one -- they are not peers of
  completeness and contiguity, and building them means building that first.
- **gfastats** is superseded by computing contiguity here, not built.
- **Contamination screening** is a real axis nothing here covers and is a
  named non-goal: FCS-GX's database is ~470 GB.

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
