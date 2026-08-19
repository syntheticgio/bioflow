# MultiQC aggregate QC reporting — implementation plan

Date: 2026-08-19.

Closes [#624](https://github.com/syntheticgio/bioflow/issues/624). Companion
to `docs/superpowers/specs/2026-08-19-multiqc-aggregate-qc-design.md` (design
decisions MQ-D1..MQ-D4 and requirements MQ-1..MQ-15).

## Files to touch

| File | Change |
|---|---|
| `backend/app/config.py` | Add `multiqc_path: str = "multiqc"` to `Settings`; add `multiqc_reports_dir` property returning `bioinfo_home / "multiqc_reports"` (mirror `qc_reports_dir` at L385). |
| `backend/app/pipelines/tools.py` | Add `multiqc()` probe (L293 shape) and `TOOL_META["multiqc"]` entry (`pipelines=()`, `delivery=BUNDLED`, all four bibliographic fields — verify license/citation against the repo). |
| `backend/app/pipelines/multiqc_runner.py` | **New.** `build_multiqc_command(*, multiqc_path, input_dir, out_dir, threads)` — pure, testable. |
| `backend/app/queue/multiqc_handlers.py` | **New.** `@registry.handler("multiqc_report", mode=HandlerMode.ASYNC, job_class=JobClass.USER_BACKGROUND)`. Enumerate project objects, stage retained raw QC files, run MultiQC, write report. Idempotent. |
| `backend/app/queue/pipeline_handlers.py` | In `run_qc`, after parsing, copy `fastqc_data.txt` → `qc_reports/<id>/fastqc/` and `fastp.json` → `qc_reports/<id>/fastp/` (best-effort). |
| `backend/app/queue/assembly_qc_handlers.py` | In QUAST path, copy `report.tsv` → `qc_reports/<id>/quast/` (HTML already retained). |
| `backend/app/queue/<bam_stats handler>` | Copy `*.stats` → `qc_reports/<id>/samtools/`. Locate via `grep -n "bam_stats" queue/`. |
| `backend/app/services/suggestion_service.py` | Add `build_multiqc_card(obj)`: gate on `tools.multiqc().available` and project-level count of objects with retained raw QC files; enqueue `multiqc_report` on click. |
| `backend/app/api/v1/pipelines.py` | Add `get_multiqc_report(project_id)` mirroring `get_qc_report` (CSP QUAST exception + ownership check + `LinkableOwnerDep`). |
| `frontend/src/components/QcReport.tsx` (or Actions-tab card UI) | "Generate QC summary" card + new-tab viewer link, mirroring the QUAST report link. |
| `backend/Dockerfile` (or `backend/scripts/install-multiqc.sh`) | Install `multiqc` (pinned) into the backend image. |
| `backend/tests/pipelines/test_tools.py` | `TOOL_META["multiqc"]` covered by existing `test_every_tool_is_documented` (no new test needed once entry exists). |
| `backend/tests/pipelines/test_multiqc_runner.py` | **New.** Command-shape tests. |
| `backend/tests/services/test_suggestion_service.py` | **New class.** Card gates correctly on ≥2 objects with retained QC. |
| `backend/tests/queue/test_multiqc_handlers.py` | **New.** Staging + report write; failure when <2 objects contribute. |

## Implementation steps (ordered)

1. **Install + tool.** Pin `multiqc` in the image; add `multiqc_path` and
   `multiqc_reports_dir` to `Settings`; add `multiqc()` probe and
   `TOOL_META["multiqc"]`. Verify `test_every_tool_is_documented` passes
   (success criterion 1). Verify license/citation against MultiQC's repo.
2. **Runner.** Write `multiqc_runner.build_multiqc_command` (pure). Unit-test
   the argv.
3. **Retention (MQ-4..MQ-6).** Add best-effort raw-file copies in the three
   QC handlers. Confirm files land under `qc_reports/<id>/` after a real run.
4. **Handler (MQ-7..MQ-9).** `multiqc_report`: list project objects, stage
   retained raw files, run MultiQC, write `multiqc_reports/<project_id>/`.
   Idempotent; clear error when <2 contribute.
5. **Card (MQ-10..MQ-11).** `build_multiqc_card(obj)` with project-level gate;
   enqueue on click. Wire into `list_suggestions`.
6. **API + frontend (MQ-12, MQ-14).** `get_multiqc_report` with QUAST-exception
   CSP + ownership check; new-tab viewer + Actions-tab card.
7. **End-to-end (MQ-13, success criterion 2 & 3).** Against a project with
   FastQC + fastp + samtools-stats retained output, generate and open the
   combined report.

## Tests → success criteria

| Success criterion | Test |
|---|---|
| 1. MultiQC installs + passes `test_every_tool_is_documented` | Step 1 + existing test. |
| 2. End-to-end report from FastQC + fastp + samtools stats | `test_multiqc_handlers.py` with retained fixtures; manual browser check. |
| 3. Report viewable/downloadable from UI | `get_multiqc_report` route test + frontend new-tab link. |
| 4. Card gates on qualifying QC output | `TestMultiqcCard` in `test_suggestion_service.py`. |

## Risks

- **Disk retention growth (MQ-15).** Raw QC files are small per object but
  accumulate; acceptable, bounded by tool output size. No cleanup policy in v1.
- **MultiQC module coverage.** Only FastQC/fastp/samtools-stats/QUAST retained
  in v1; other modules render as "no data" until their raw outputs are kept.
- **CSP scripting (MQ-14).** MultiQC requires `unsafe-inline`/`unsafe-eval`;
  served new-tab + ownership-checked, matching QUAST. Documented in the spec;
  do not regress to the FastQC `sandbox` policy or the report renders blank.
