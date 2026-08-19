# MultiQC aggregate QC reporting

Date: 2026-08-19.

Closes [#624](https://github.com/syntheticgio/bioflow/issues/624).
Companion plan: `docs/superpowers/plans/2026-08-19-multiqc-aggregate-qc.md`.

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
`object.facts`, and leaves the rest — `fastqc_data.txt`, `fastp.json`,
`samtools stats` text, QUAST `report.tsv` — in the ephemeral per-job `work/`
dir, which is reaped. So a user who clicks "generate report" a week later
would find no stable files for MultiQC to scan.

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

| Tool | Job that produces it | Raw file retained under `qc_reports/<object_id>/` |
|---|---|---|
| FastQC | `run_qc` (short read) | `fastqc/fastqc_data.txt` |
| fastp | `run_qc` (short read) | `fastp/fastp.json` |
| samtools stats | `bam_stats` (align family) | `samtools/<sample>.stats` |
| QUAST | assembly-QC | `quast/report.tsv` (HTML already retained) |

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
  tool-picker screen.
- **MQ-4** — After a `run_qc` (short read) job, `fastqc/fastqc_data.txt` and
  `fastp/fastp.json` exist under `qc_reports/<object_id>/`.
- **MQ-5** — After a `bam_stats` job, `samtools/<sample>.stats` exists under
  `qc_reports/<object_id>/`.
- **MQ-6** — After an assembly-QC (QUAST) job, `quast/report.tsv` exists under
  `qc_reports/<object_id>/` (alongside the already-retained HTML).
- **MQ-7** — `pipelines/multiqc_runner.py` exposes
  `build_multiqc_command(*, multiqc_path, input_dir, out_dir, threads)` that
  invokes MultiQC over `input_dir` and writes to `out_dir`. Pure, testable
  without a container.
- **MQ-8** — A `multiqc_report` handler (HandlerMode.ASYNC,
  JobClass.USER_BACKGROUND) stages every project object's retained QC raw
  files into one temp dir, runs MultiQC, and writes `multiqc_report.html` +
  `multiqc_data/` into `multiqc_reports/<project_id>/`. Handler is idempotent.
- **MQ-9** — The handler fails with a clear, user-facing error when zero or one
  object contributes scannable files (defensive; the card already gates).
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
- **MQ-13** — End-to-end, against a project with FastQC + fastp + samtools
  stats output retained, MultiQC produces a combined `multiqc_report.html`.

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
- **Retention** — `queue/pipeline_handlers.py` (`run_qc`) and
  `queue/assembly_qc_handlers.py` (QUAST) copy raw files into
  `qc_reports/<object_id>/` after parsing; the `bam_stats` handler copies its
  `*.stats`. Each copy is best-effort and never fails the QC write (same
  posture `_apply_run_qc` takes toward the optional summary).
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
- **Install** — add `multiqc` to the backend image (pip install, pinned; an
  `install-multiqc.sh` only if a version pin or post-install step is needed,
  mirroring `install-quast.sh`).

## Non-goals / follow-ups

- No automatic rollup after QC jobs (MQ-D2).
- No per-subset report selection in v1 — the aggregate is whole-project.
- Retention covers FastQC / fastp / samtools-stats / QUAST only; other
  MultiQC modules (e.g. featureCounts, BUSCO) are follow-ups once their raw
  outputs are also retained.
- A project-level suggestions endpoint is not introduced; the per-object card
  acting project-wide reuses the existing `list_suggestions(object_id)` wiring.
