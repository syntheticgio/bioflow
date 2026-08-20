# MultiQC aggregate QC reporting

Date: 2026-08-19. Amended 2026-08-20 after an install spike (see
[Spike findings](#spike-findings-2026-08-20), which corrects MQ-4, MQ-7,
MQ-9 and the Install section). MQ-5/MQ-6 (samtools stats + QUAST retention)
landed in [#702](https://github.com/syntheticgio/bioflow/issues/702), after
v1 shipped -- the requirement text below reflects what shipped there.

Closes [#624](https://github.com/syntheticgio/bioflow/issues/624).
Companion plan: `docs/superpowers/plans/2026-08-19-multiqc-aggregate-qc.md`.

**v1 scope was reads-side only** (FastQC + fastp). samtools-stats and QUAST
retention were deferred to #702 -- see [Non-goals](#non-goals--follow-ups),
now landed.

## Problem

BioFlow runs FastQC, fastp, samtools stats, and QUAST, but each produces
output in isolation. There is no single combined dashboard — the role MultiQC
plays in most pipelines: one HTML/JSON report that scans known tool-output
directories and renders a combined view. The issue rates this low-risk (a
report generator over existing files, not a new analysis) and high-visibility
(directly improves the Actions/QC experience when reviewing more than one
file).

The issue's mental model — "scan the directory holding a project's accumulated
QC output" — describes a directory that **does not currently exist**, and that
is the one finding this design has to resolve before any code is written.

### The directory MultiQC needs does not exist today

`qc` handlers parse their tool output into structured `facts` and discard the
raw files. `queue/results.py:_apply_run_qc` is explicit:

> "QC derives no files, so unlike trim or align there is nothing to ingest —
> the whole result is facts merged onto the object the user ran it against."

Each `run_qc` / `bam_stats` / assembly-QC handler writes its HTML report (if
any) under `settings.qc_reports_dir / <object_id>/`, parses the numbers into
`object.facts`, and leaves most of the rest — `fastp.json`, `samtools stats`
text, QUAST `report.tsv` — in the ephemeral per-job `work/` dir, which is
reaped. So a user who clicks "generate report" a week later finds almost
nothing for MultiQC to scan.

**Almost, not nothing:** FastQC is the exception. `_run_fastqc` writes
straight into `qc_reports/<object_id>/fastqc/`, so its `*_fastqc.zip` — the
file MultiQC's FastQC module actually parses — is already there and always
has been (SF-4). The gap is real but one tool narrower than first stated.

Two consequences shape the whole design:

1. The raw MultiQC-compatible files must be **retained** as a side effect of
   the existing QC jobs (decision MQ-D1).
2. MultiQC's scan target is therefore the set of per-object `qc_reports/<id>/`
   directories, not a single project-wide folder.

## Design decisions

Four decisions, each resolved with the issue author before this doc was
written.

### MQ-D1 — Retain raw QC outputs as a side effect of existing QC jobs

Extend the QC / align (bam_stats) / assembly-QC handlers so that, after parsing
as today, they also copy each tool's MultiQC-compatible **raw** file into
`settings.qc_reports_dir / <object_id>/` on disk. The MultiQC runner later
stages those files into a temp dir and runs.

This is a deliberate scope expansion the issue did not call out: it changes the
long-standing "QC derives no files" invariant and adds a small amount of
per-object disk storage. It is the only faithful option — MultiQC reads raw
tool output, not parsed `facts`, and re-running the tools on demand
(alternative considered and rejected) would be redundant compute that is not
what MultiQC is for.

Retained files per tool, matching what each MultiQC module actually parses:

| Tool | Job that produces it | Raw file retained under `qc_reports/<object_id>/` | Status |
|---|---|---|---|
| FastQC | `run_qc` (short read) | `fastqc/*_fastqc.zip` | **Already retained** — no code needed (SF-4) |
| fastp | `run_qc` (short read) | `fastp/fastp.json` | v1 — the only retention work |
| samtools stats | `run_bam_stats` (align family) | `samtools/stats.txt` | **Landed in #702** — a *new* `samtools stats` call, not a copy; fixed filename, not `<sample>.stats` (see MQ-5) |
| QUAST | assembly-QC | `quast/report.tsv` (HTML already retained) | **Landed in #702** (MQ-6) |

Corrected 2026-08-20: this table first named `fastqc/fastqc_data.txt` for
FastQC. That is an entry *inside* the zip FastQC writes, not a file any
handler produces; MultiQC parses the zip directly. See SF-4.

The existing FastQC/fastp/QUAST HTML reports stay where they are; the raw files
are a parallel addition. `get_qc_report` already serves from this directory via
an explicit `report_path`, so the extra raw files are inert there and harmless
to that route.

### MQ-D2 — On-demand card, not auto-rollup

A "Generate QC summary" card in the Actions tab (per-object card, like every
other `build_*_card`, but gated on project-level state — see MQ-D4). The user
clicks to enqueue a project-scoped `multiqc_report` job. No automatic rollup
after QC jobs; provenance stays easy to reason about, and a report is produced
only when someone wants one.

### MQ-D3 — Gate on ≥2 objects holding retained QC output

The card is available when the project contains **at least two objects, each of
which has a retained MultiQC-compatible raw file** on disk (across FastQC /
fastp / samtools-stats / QUAST). Otherwise it renders `UNAVAILABLE` with the
reason "Need at least 2 QC results to summarize." The gate counts objects, not
individual files, because the product is a per-sample aggregate and one sample
with two QC tools is not a "combined dashboard."

### MQ-D4 — Project artifact, sandboxed viewer

The report is written to `settings.multiqc_reports_dir / <project_id>/`
(`multiqc_report.html` plus its `multiqc_data/` sibling) and served through a
new `get_multiqc_report(project_id)` route that mirrors `get_qc_report`. The
frontend opens it in a **new tab** (not an inline iframe), following the
existing QUAST/NanoPlot path.

## Security model (load-bearing)

MultiQC's report is fully scripted (Plotly/D3). Under `get_qc_report`'s default
FastQC policy (`sandbox` + `default-src 'none'`, scripting disabled) it would
render blank, exactly as fastp's charts do. So MultiQC follows the **QUAST
exception** already in `get_qc_report`:

- Drop `sandbox`; serve with `script-src 'unsafe-inline' 'unsafe-eval'`,
  `style-src 'unsafe-inline'`, `img-src 'self' data:`, `font-src 'self' data:`,
  `default-src 'none'`.
- Open in a **new tab**, never an inline iframe, so the report never shares a
  document with the application.
- The route performs an ownership check (project lookup) before serving bytes,
  so the directory layout is not itself the access rule.

The XSS surface this accepts is the same class QUAST already accepts: the
report embeds sample / assembly / reference names taken verbatim from QC
output, so a crafted input can place attacker-chosen text in the HTML. The
exposure is bounded because (a) the report is generated from the project's own
QC data, (b) the route is ownership-checked, and (c) the page shares no
document or session with the app. This is a conscious trade for a generated
report, documented here so it is not "discovered" later.

## Spike findings (2026-08-20)

An install spike ran MultiQC 1.35 inside the running `api` container against
real FastQC/fastp/samtools/QUAST output, reproducing the on-disk layout the
handlers actually write. Five findings, four of which change requirements
above. Measured, not recalled.

### SF-1 -- MultiQC cannot share the image's Python environment

MultiQC pins `kaleido==0.2.1`. The image has `kaleido 1.3.0`, required by
**NanoPlot** (`pip show kaleido` reports `Required-by: NanoPlot`). A plain
`pip install multiqc` therefore downgrades kaleido and breaks long-read QC --
silently, since nothing imports kaleido at startup and NanoPlot only fails
when a long-read QC job next runs.

So `install-multiqc.sh` is **mandatory**, not the conditional "only if a
version pin or post-install step is needed" the Install section first
allowed. MultiQC goes in its own venv with a wrapper on PATH, the pattern
`install-medaka.sh` already uses -- which also sidesteps the shadowed
interpreter documented in AGENTS.md, since the wrapper names the venv's
binary by absolute path.

Architecture is a non-issue: MultiQC 1.35 is a `py3-none-any` wheel with no
compiled component, and `kaleido==0.2.1` publishes a
`manylinux2014_aarch64` wheel. Verified installing cleanly on aarch64.

**Cost: ~1.1 GB**, of which 238 MB is the Chromium that kaleido 0.2.1
bundles. Kaleido exists for *static image export* only; BioFlow serves the
interactive HTML report and never exports PNGs, so that 238 MB is dead
weight. Whether MultiQC runs with kaleido absent is worth testing during
implementation -- if it does, drop it and note the result here.

### SF-2 -- `--no-version-check` is required

MultiQC checks for a newer release over the network on every run unless
`--no-version-check` is passed. In a worker with no outbound network that is
a hang waiting to happen, and it is not behaviour a report generator should
have at all. MQ-7 requires the flag.

### SF-3 -- Empty input exits 0 and writes nothing

Against a directory MultiQC finds nothing in, it logs "No analysis results
found. Cleaning up..." and **exits 0 without writing a report**. A handler
checking only the exit code would record success and leave no file.

MQ-9 must therefore assert the report HTML exists, not trust the return
code. This is the same trap `pipeline_handlers.py` already guards for fastp
("fastp exited 0 but produced no output"); the guard is a house pattern, not
a new invention.

### SF-4 -- FastQC's raw output already persists, and MQ-4 named the wrong file

MQ-4 originally required retaining `fastqc/fastqc_data.txt`. That file is an
*entry inside* the zip FastQC writes, and MultiQC parses the zip directly.
`_run_fastqc` already writes both `_fastqc.html` and `_fastqc.zip` into
`qc_reports/<object_id>/fastqc/` and leaves them there.

Verified: MultiQC pointed at the current, unmodified layout reports
`fastqc | Found 2 reports`. **The FastQC half of MQ-4 needs no code at
all.** Retention work for v1 is fastp only.

A false lead worth recording so it is not re-investigated: `/data/qc_reports`
on the dev box holds 16 object dirs containing a flat `fastqc_report.html`
and no zip, which looks like evidence against the above. Those are **test
fixtures** -- `tests/services/test_share_reports.py` and
`test_object_offload.py` write that filename into the live `qc_reports_dir`.
Filed as a separate issue; it is not the shape a real QC run produces.

### SF-5 -- The retention fix is sufficient, and the report is self-contained

Copying `fastp_qc.json` next to the HTML already copied takes the run from
`fastp | Found 0` to `fastp | Found 2 reports`. With samtools `stats` and
QUAST `report.tsv` also staged, all four modules parse -- confirming the
deferred work in MQ-5/MQ-6 needs no design change, only the copies.

Two properties that matter for MQ-12/MQ-14:

- **Runtime is ~1.6s** for four tools. On-demand generation (MQ-D2) is
  comfortably cheap; no background rollup is justified.
- **The report renders fully offline.** Every script and style is inlined;
  the only offsite reference is a decorative GitHub emoji `<img>` that
  degrades to alt text. The CSP in MQ-14 needs no external origin.
- **Sample names come from the read filenames**, not directory names, so
  `object_id` does not leak into the report the user reads.

### SF-6 -- `node_types.py` needs an `EXCLUDED_LAUNCHES` entry

Not an install finding, but adjacent and load-bearing. `node_types.py`
asserts a partition: every `launch_*` is either classified as a workflow node
or listed in `EXCLUDED_LAUNCHES`, and `TestExhaustiveness` fails otherwise.
A project-scoped report produces no object a downstream node could consume
-- the same shape as `launch_summary` -- so its launcher belongs in
`EXCLUDED_LAUNCHES`. Added as MQ-16.

## Requirements

Quality criteria (testable, unambiguous, necessary, feasible, complete,
consistent) per `AGENTS.md`. Each ID is permanent.

### Functional

- **MQ-1** — `tools.multiqc()` returns a `Tool` probed by PATH via
  `settings.multiqc_path` with a version arg, `@lru_cache(maxsize=1)`, matching
  the `fastqc()` / `quast()` shape.
- **MQ-2** — `TOOL_META["multiqc"]` carries `homepage`, `repository`,
  `citation`, `citation_url`, `license` (SPDX), and `usage`, so
  `test_every_tool_is_documented` passes. License and citation verified against
  MultiQC's own repository before merge, not recalled.
- **MQ-3** — `TOOL_META["multiqc"]` sets `pipelines=()` deliberately: MultiQC
  is a report generator reached only through its card, never offered in a
  tool-picker screen. `PipelineType.QC` exists and would be the obvious
  choice, but that enum drives `PipelineToolSelector.tsx` -- listing MultiQC
  there would offer it to someone picking a tool to *run QC with*, which it
  is not. Empty is the honest value, and `TOOL_META` permits it.
- **MQ-4** — After a `run_qc` (short read) job, `fastp/fastp.json` exists
  under `qc_reports/<object_id>/`. FastQC needs no change: `_run_fastqc`
  already leaves `fastqc/*_fastqc.zip` there and MultiQC parses the zip
  directly (SF-4). The earlier form of this requirement named
  `fastqc/fastqc_data.txt`, which is an entry *inside* that zip rather than a
  file any handler writes.
- **MQ-5** — *(#702, landed after v1)* After a `run_bam_stats` job,
  `samtools/stats.txt` exists under `qc_reports/<object_id>/`. A *new*
  `samtools stats` invocation, not a copy -- `bam_stats_runner` parses
  idxstats/coverage/depth and never wrote a `stats` file before this. Fixed
  filename rather than `samtools/<sample>.stats` as this requirement first
  said: `RETAINED_FACT_FILES` in `multiqc_handlers.py` maps one fact key to
  one static relative path, and a per-sample name would have needed either
  storing the path in the fact's own value or a second lookup mechanism
  alongside the fixed-path one fastp and QUAST both use. Best-effort and
  never fails `run_bam_stats` -- the same posture `_run_fastqc` takes on its
  own optional extra, since idxstats/coverage/depth are the numbers the
  Results tab actually shows and a `samtools stats` failure must not cost
  them.
- **MQ-6** — *(#702, landed after v1)* After an assembly-QC (QUAST) job,
  `quast/report.tsv` exists under `qc_reports/<object_id>/` (alongside the
  already-retained HTML). Verified end-to-end against a real QUAST 5.3.0 run:
  `_copy_report` returns `(report_path, tsv_retained)`, and MultiQC pointed
  at the resulting layout reports `quast | Found 1 reports`.
- **MQ-7** — `pipelines/multiqc_runner.py` exposes
  `build_multiqc_command(*, multiqc_path, input_dir, out_dir)` that invokes
  MultiQC over `input_dir` and writes to `out_dir`. Pure, testable without a
  container. The argv must include `--no-version-check` (SF-2) and
  `--no-ansi` (log output is captured to a file, not a terminal). No
  `threads` parameter: MultiQC is single-threaded file parsing and takes no
  such flag.
- **MQ-8** — A `multiqc_report` handler (HandlerMode.ASYNC,
  JobClass.USER_BACKGROUND) stages every project object's retained QC raw
  files into one temp dir, runs MultiQC, and writes `multiqc_report.html` +
  `multiqc_data/` into `multiqc_reports/<project_id>/`. Handler is idempotent.
- **MQ-9** — The handler fails with a clear, user-facing error when zero or one
  object contributes scannable files (defensive; the card already gates), and
  **verifies the report HTML exists before reporting success**. MultiQC exits
  0 while writing nothing when it finds no parseable input (SF-3), so the exit
  code alone cannot distinguish success from a silent no-op.
- **MQ-10** — `build_multiqc_card(obj)` returns a `SuggestionCard` (kind
  `multiqc`, category `QC`) when `tools.multiqc()` is available **and** the
  project holds ≥2 objects with retained MultiQC-compatible raw files;
  otherwise `UNAVAILABLE` with a reason.
- **MQ-11** — Clicking the card enqueues `multiqc_report` with `project_id`
  and `owner`, reusing the `queue.enqueue(..., project_id=..., owner=...)`
  pattern from `pipeline_service.py`.
- **MQ-12** — `get_multiqc_report(project_id)` serves
  `multiqc_reports/<project_id>/multiqc_report.html` (and its `multiqc_data/`
  sibling) with the QUAST-style CSP and an ownership check.
- **MQ-13** — End-to-end, against a project with FastQC + fastp output
  retained, MultiQC produces a combined `multiqc_report.html`. (samtools
  stats joins this criterion when MQ-5 lands.)
- **MQ-16** — The `multiqc_report` launcher is listed in
  `node_types.EXCLUDED_LAUNCHES`, so `TestExhaustiveness` in
  `tests/pipelines/test_node_types.py` passes. A project-scoped report
  produces no object a downstream node could consume, the same reasoning
  `pipeline_service.launch_summary` is excluded under (SF-6). Run the whole
  `TestExhaustiveness` class, not just the one test: it asserts a partition,
  and an entry added to both sides passes one half while failing the other.

### Non-functional

- **MQ-14 (Security)** — The MultiQC report is served only under the QUAST
  exception CSP (scripting allowed, new-tab, ownership-checked). It is never
  inlined into the application document.
- **MQ-15 (Capacity)** — Retained raw QC files add per-object disk usage
  bounded by the size of the tool's own output (typically KB–low-MB per file);
  no new large artifact is introduced beyond the generated report.

## Component design

- **Tool** — `tools.py`: `multiqc()` probe + `TOOL_META["multiqc"]`
  (`pipelines=()`, `delivery=BUNDLED`). `multiqc_path: str = "multiqc"` added
  to `Settings`, beside `fastqc_path` / `quast_path`.
- **Retention (v1)** — `queue/pipeline_handlers.py` (`_run_short_read_qc`)
  copies `fastp_qc.json` from the workdir to
  `qc_reports/<object_id>/fastp/fastp.json`, immediately beside the
  `fastp.html` copy that already happens there. FastQC needs no change
  (SF-4). The copy is best-effort and never fails the QC write, the same
  posture `_run_fastqc` and the QUAST report copy already take: the facts are
  the half of the result a user cannot get any other way. QUAST
  (`assembly_qc_handlers.py`) and samtools (`bam_stats`) retention are
  deferred to the MQ-5/MQ-6 follow-up.
- **Runner** — `pipelines/multiqc_runner.py`, mirroring
  `quast_runner.py`'s pure-command shape.
- **Handler** — `queue/multiqc_handlers.py`, registered via
  `@registry.handler("multiqc_report", mode=HandlerMode.ASYNC,
  job_class=JobClass.USER_BACKGROUND)`; enumerates `DataObject`s by
  `project_id`, stages retained raw files, runs MultiQC.
- **Card** — `services/suggestion_service.py`: `build_multiqc_card(obj)`,
  gated on `tools.multiqc().available` and a project-level count of objects
  with retained raw QC files (disk presence under `qc_reports/<object_id>/`).
- **API** — `api/v1/pipelines.py`: `get_multiqc_report(project_id)`, mirroring
  `get_qc_report` (CSP + ownership check; `LinkableOwnerDep` so the new-tab
  link works).
- **Frontend** — a "Generate QC summary" card in the Actions tab
  (reusing the suggestion-card UI) and a viewer that opens the report in a new
  tab, mirroring the QUAST report link in `QcReport.tsx`.
- **Install** — `backend/scripts/install-multiqc.sh`, **required** rather than
  optional (SF-1). MultiQC 1.35 goes into its own venv at `/opt/multiqc/env`
  with a wrapper at `/usr/local/bin/multiqc` execing the venv binary by
  absolute path -- the `install-medaka.sh` pattern. It must not be
  `pip install`ed into the shared environment: MultiQC pins
  `kaleido==0.2.1`, NanoPlot needs `kaleido 1.3.0`, and the resulting
  downgrade breaks long-read QC with nothing failing until a long-read job
  runs. Version pinned in the script, overridable by `MULTIQC_VERSION` like
  every other install script here. Ends with `multiqc --version` as its own
  smoke check.

## Non-goals / follow-ups

- No automatic rollup after QC jobs (MQ-D2).
- No per-subset report selection in v1 — the aggregate is whole-project.
- **v1 retention was reads-side only: FastQC (already retained) and fastp.**
  samtools-stats (MQ-5) and QUAST `report.tsv` (MQ-6) were deferred to
  [#702](https://github.com/syntheticgio/bioflow/issues/702). Scoped this way
  on 2026-08-20 because the reads-side slice was self-contained -- one `cp` in
  one handler -- while samtools needed a *new* `samtools stats` invocation in
  `run_bam_stats`, which is a behaviour change to the align family rather than
  a retention tweak. The spike confirmed all four modules parse once staged
  (SF-5), so the deferral cost no design rework -- #702 landed both with no
  changes to `stage_multiqc_inputs`, `object_contributes`, or
  `newest_qc_output_at`, which already iterate `RETAINED_FACT_FILES`
  generically.
- Other MultiQC modules (e.g. featureCounts, BUSCO) are follow-ups once their
  raw outputs are also retained.
- **No cleanup policy for generated reports.** Regenerating overwrites the
  project's previous report in place; there is no version history. Provenance
  lives in the job record, as it does for every other run.
- A project-level suggestions endpoint is not introduced; the per-object card
  acting project-wide reuses the existing `list_suggestions(object_id)` wiring.
