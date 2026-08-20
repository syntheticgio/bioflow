# MultiQC aggregate QC reporting — implementation plan

Date: 2026-08-19. Amended 2026-08-20 to match the spec's spike findings
(SF-1..SF-6) and the reads-side-only v1 scope.

Closes [#624](https://github.com/syntheticgio/bioflow/issues/624). Companion
to `docs/superpowers/specs/2026-08-19-multiqc-aggregate-qc-design.md` (design
decisions MQ-D1..MQ-D4 and requirements MQ-1..MQ-16).

**v1 is reads-side only.** FastQC already retains what MultiQC needs (SF-4),
so the only retention work is one `fastp.json` copy. samtools-stats and QUAST
retention are a separate follow-up issue.

## Files to touch

| File | Change |
|---|---|
| `backend/app/config.py` | Add `multiqc_path: str = "multiqc"` to `Settings`; add `multiqc_reports_dir` property returning `bioinfo_home / "multiqc_reports"` (mirror `qc_reports_dir` at L385). |
| `backend/app/pipelines/tools.py` | Add `multiqc()` probe (L293 shape) and `TOOL_META["multiqc"]` entry (`pipelines=()`, `delivery=BUNDLED`, all four bibliographic fields — verify license/citation against the repo). |
| `backend/app/pipelines/multiqc_runner.py` | **New.** `build_multiqc_command(*, multiqc_path, input_dir, out_dir)` — pure, testable. Must emit `--no-version-check` (SF-2) and `--no-ansi`. No `threads` arg: MultiQC takes no such flag. |
| `backend/app/queue/multiqc_handlers.py` | **New.** `@registry.handler("multiqc_report", mode=HandlerMode.ASYNC, job_class=JobClass.USER_BACKGROUND)`. Enumerate project objects, stage retained raw QC files, run MultiQC, write report. Idempotent. |
| `backend/app/queue/pipeline_handlers.py` | In `_run_short_read_qc`, copy the workdir's `fastp_qc.json` → `qc_reports/<id>/fastp/fastp.json` (best-effort), beside the existing `fastp.html` copy. **FastQC needs no change** — `_run_fastqc` already writes `*_fastqc.zip` there and MultiQC parses it (SF-4). |
| ~~`backend/app/queue/assembly_qc_handlers.py`~~ | **Deferred (MQ-6)** — QUAST `report.tsv` retention moves to the follow-up issue. |
| ~~`backend/app/queue/<bam_stats handler>`~~ | **Deferred (MQ-5)** — and note it is a *new* `samtools stats` call, not a copy; `bam_stats_runner` never writes a stats file today. |
| `backend/app/services/suggestion_service.py` | Add `build_multiqc_card(obj)`: gate on `tools.multiqc().available` and project-level count of objects with retained raw QC files; enqueue `multiqc_report` on click. |
| `backend/app/api/v1/pipelines.py` | Add `get_multiqc_report(project_id)` mirroring `get_qc_report` (CSP QUAST exception + ownership check + `LinkableOwnerDep`). |
| `frontend/src/components/QcReport.tsx` (or Actions-tab card UI) | "Generate QC summary" card + new-tab viewer link, mirroring the QUAST report link. |
| `backend/scripts/install-multiqc.sh` + `backend/Dockerfile` | **New script, required not optional (SF-1).** MultiQC 1.35 into its own venv at `/opt/multiqc/env` + wrapper on PATH, `install-medaka.sh` pattern. Never `pip install` into the shared env: it pins `kaleido==0.2.1` against NanoPlot's `1.3.0`. |
| `backend/tests/pipelines/test_tools.py` | `TOOL_META["multiqc"]` covered by existing `test_every_tool_is_documented` (no new test needed once entry exists). |
| `backend/tests/pipelines/test_multiqc_runner.py` | **New.** Command-shape tests. |
| `backend/tests/services/test_suggestion_service.py` | **New class.** Card gates correctly on ≥2 objects with retained QC. |
| `backend/tests/queue/test_multiqc_handlers.py` | **New.** Staging + report write; failure when <2 objects contribute; **and that a MultiQC run producing no HTML is reported as failure, not success** (SF-3). |
| `backend/app/pipelines/node_types.py` | Add the `multiqc_report` launcher to `EXCLUDED_LAUNCHES` (MQ-16, SF-6). |
| `backend/tests/pipelines/test_node_types.py` | Run the whole `TestExhaustiveness` class — it asserts a partition, so a half-fix passes one test and fails its sibling. |

## Implementation steps (ordered)

1. **Install + tool.** Write `install-multiqc.sh` (own venv + wrapper, SF-1)
   and wire it into the Dockerfile; add `multiqc_path` and
   `multiqc_reports_dir` to `Settings`; add `multiqc()` probe and
   `TOOL_META["multiqc"]`. Verify `test_every_tool_is_documented` passes
   (success criterion 1). Confirm NanoPlot still runs after the image build --
   that is the regression SF-1 is guarding against, and it is silent.
   Bibliographic fields, read from the 1.35 wheel metadata rather than
   recalled: license GPL-3.0, homepage `https://multiqc.info`, repository
   `https://github.com/MultiQC/MultiQC`, citation Ewels et al. 2016,
   `doi:10.1093/bioinformatics/btw354`. Re-verify against the repo before
   merge.
   *Worth testing here:* whether MultiQC runs with kaleido removed. It is
   238 MB of bundled Chromium for a static-export path BioFlow never uses.
2. **Runner.** Write `multiqc_runner.build_multiqc_command` (pure). Unit-test
   the argv.
3. **Retention (MQ-4).** One best-effort copy in `_run_short_read_qc`:
   `fastp_qc.json` → `qc_reports/<id>/fastp/fastp.json`. Confirm against a
   real QC run that MultiQC then reports `fastp | Found N reports` where it
   previously found 0. MQ-5/MQ-6 are deferred.
4. **Handler (MQ-7..MQ-9).** `multiqc_report`: list project objects, stage
   retained raw files, run MultiQC, write `multiqc_reports/<project_id>/`.
   Idempotent; clear error when <2 contribute.
5. **Card (MQ-10..MQ-11).** `build_multiqc_card(obj)` with project-level gate;
   enqueue on click. Wire into `list_suggestions`.
6. **API + frontend (MQ-12, MQ-14).** `get_multiqc_report` with QUAST-exception
   CSP + ownership check; new-tab viewer + Actions-tab card.
7. **Node type (MQ-16).** Add the launcher to `EXCLUDED_LAUNCHES` and run the
   full `TestExhaustiveness` class.
8. **End-to-end (MQ-13, success criterion 2 & 3).** Against a project with
   FastQC + fastp retained output, generate and open the combined report in
   the browser. Check the report is not blank -- a blank page means the CSP
   regressed to the FastQC `sandbox` policy.

## Tests → success criteria

| Success criterion | Test |
|---|---|
| 1. MultiQC installs + passes `test_every_tool_is_documented` | Step 1 + existing test. |
| 2. End-to-end report from FastQC + fastp | `test_multiqc_handlers.py` with retained fixtures; manual browser check. samtools stats joins when MQ-5 lands. |
| 3. Report viewable/downloadable from UI | `get_multiqc_report` route test + frontend new-tab link. |
| 4. Card gates on qualifying QC output | `TestMultiqcCard` in `test_suggestion_service.py`. |

## Risks

- **Disk retention growth (MQ-15).** Raw QC files are small per object but
  accumulate; acceptable, bounded by tool output size. No cleanup policy in v1.
- **MultiQC module coverage.** Only FastQC + fastp in v1; other modules
  render as "no data" until their raw outputs are kept.
- **Objects QC'd before this ships have no `fastp.json`.** The card gates on
  retained files, so an older project reports "not enough QC results to
  summarize" rather than silently producing a thin report. Re-running QC fixes
  it. Say this in the card's `unavailable` reason -- "ambiguity is
  unavailable, not a guess" is the rule the suggestion service already holds.
- **Image size.** The MultiQC venv is ~1.1 GB as measured, 238 MB of it
  kaleido's bundled Chromium. If step 1 shows kaleido is droppable, take it.
- **CSP scripting (MQ-14).** MultiQC requires `unsafe-inline`/`unsafe-eval`;
  served new-tab + ownership-checked, matching QUAST. Documented in the spec;
  do not regress to the FastQC `sandbox` policy or the report renders blank.
