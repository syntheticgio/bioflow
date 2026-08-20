# MultiQC report viewing — frontend design brief

Date: 2026-08-20. Input for a frontend design pass.

Backend for [#624](https://github.com/syntheticgio/bioflow/issues/624) is
implemented and merged-ready on `feat/624-multiqc-install`; this brief covers
the one remaining step. Design decisions live in
`docs/superpowers/specs/2026-08-19-multiqc-aggregate-qc-design.md` (MQ-D1..D4,
requirements MQ-1..MQ-16, and the 2026-08-20 spike findings SF-1..SF-6).

## What exists already

**Launching works end to end, with no frontend work.** MultiQC is offered as
an ordinary Actions-tab suggestion card, rendered by the existing
`PipelineSuggestions.tsx` machinery. Nothing needs designing for the launch
path:

- `build_multiqc_card` in `backend/app/services/suggestion_service.py`
- kind `multiqc`, category `QC`, rendered **last** in the card grid
- title "Summarize QC across files"
- launches `POST /pipelines/multiqc` with body `{project_id}`
- unavailable (with a reason) when MultiQC is missing, when fewer than two
  files carry parseable QC output, or when the count could not be taken

**The report is generated and served.** A finished run writes
`multiqc_report.html` plus a `multiqc_data/` sibling into
`<BIOINFO_HOME>/multiqc_reports/<project_id>/`, served by:

```
GET /pipelines/qc/multiqc/{project_id}/{report_path}
```

It is ownership-checked against the project, sets the QUAST-style CSP
(scripting allowed, no external origin), and uses `LinkableOwnerDep` — so it
works as a plain `<a href>` with the profile passed as a query param, exactly
like `api.qcReportUrl`.

## The gap this brief is about

**A user who generates a report has no way to open it.** There is no link,
no indicator that a report exists, and no place in the UI where a
project-scoped artifact currently lives.

That last point is the actual design problem. Every existing report link —
FastQC, fastp, NanoPlot, QUAST — hangs off a **single object** and renders in
that object's QC tab (`ReportLink` in `frontend/src/components/QcReport.tsx`).
A MultiQC report describes the **whole project** and belongs to no object, so
there is no established home to copy. Filing it under whichever object
launched it was considered and rejected in MQ-D4: the report would be
orphaned when that object was deleted.

## What needs designing

1. **Where the entry point lives.** Candidates, none obviously right:
   project header, a section in the Actions tab beside the card that launched
   it, the project's file-list surface, or somewhere new. The constraint is
   that it must read as project-scoped rather than belonging to whatever file
   the user happens to have selected.

2. **How "no report yet" vs "report ready" vs "building" reads.** The job is
   asynchronous. It is fast (~1.6s measured over four tools' output), so the
   building state is brief but real, and the card's Launch button already
   greys out while a job of this type runs — `running_now.ENDPOINT_JOB_TYPES`
   maps `/pipelines/multiqc` for exactly that.

3. **How staleness is communicated, if at all.** Regenerating overwrites in
   place; there is no version history (a deliberate non-goal). A report can
   therefore be older than the QC runs it claims to summarize, with nothing
   currently saying so. Whether that is worth surfacing is a design judgement
   — it may be acceptable to say nothing.

## Constraints the design must respect

- **New tab, never an inline iframe.** MQ-14, and the same rule every other
  report link here follows. The report is served with scripting enabled
  (`script-src 'unsafe-inline' 'unsafe-eval'`) because MultiQC's plots are
  Plotly, and it embeds sample names taken verbatim from QC output. It must
  not share a document with the application. Reuse `ReportLink`'s
  `target="_blank" rel="noopener noreferrer"` treatment.
- **Link, not fetch.** Add a `multiqcReportUrl(projectId, reportPath)` helper
  beside `qcReportUrl` in `frontend/src/api/client.ts`; it needs
  `profileQuery()` for the same reason `qcReportUrl` does — it is an `<a
  href>`, so it cannot carry the profile header.
- **The filename is fixed**: `multiqc_report.html`. It is a module constant
  (`multiqc_runner.REPORT_FILENAME`), not a value to discover at runtime.
- **No browser/DOM test setup exists in this repo.** Verification is manual
  at the running app, plus optional pure-function component tests under
  Vitest (see `AlignerParamFields.test.tsx` for the established pattern).

## One backend change the design will likely require

Nothing currently tells the frontend **whether a report exists** for a
project. `GET /projects/{id}` returns `ProjectDetail`, which carries only
`breadcrumbs` on top of `ProjectOut`.

Whoever designs this should say what signal they need, and the backend side
is small either way. The two plausible shapes:

- a boolean/timestamp field on `ProjectDetail` (e.g. `multiqc_report_at`),
  which also answers the staleness question in (3) for free; or
- a dedicated lightweight endpoint the report surface polls.

The first is cheaper and needs no new route. Flagging it here rather than
choosing, since it depends on where the entry point lands.

## Files a frontend implementation would touch

| File | Change |
|---|---|
| `frontend/src/api/client.ts` | `multiqcReportUrl(projectId, reportPath)` beside `qcReportUrl` (~L870) |
| `frontend/src/components/QcReport.tsx` | `ReportLink` is here and is reusable; whether it is exported or lifted depends on where the entry point lands |
| wherever the entry point lands | the new surface |
| `backend/app/api/v1/schemas.py` | `ProjectDetail`, if the design takes the field approach |
| `frontend/src/api/types/project.ts` | matching type change |

## Verification

Manual, at the running app. From a worktree, `./ops/worktree-up.sh` serves
the UI on 5273 against that branch's code. The end-to-end path is: run QC on
two or more read files, open the Actions tab, launch the card, then open the
report from whatever surface the design adds. A **blank report page** is the
specific failure to watch for — it means the CSP regressed to the FastQC
`sandbox` policy, under which MultiQC's Plotly charts cannot execute.
