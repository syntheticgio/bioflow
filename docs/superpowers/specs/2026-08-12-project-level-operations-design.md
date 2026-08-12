# Project-Level Operations

**Date:** 2026-08-12
**Status:** Draft
**Issue:** [#284](https://github.com/syntheticgio/bioflow/issues/284)

## Problem

Some operations in BioFlow are inherently project-scoped (e.g., merging several FASTQ files into one) but currently require clicking on an individual file to start the UI. This is awkward — the user should be able to perform project-level tasks without first selecting a specific file.

## Design

### Overview

Add a collapsible "Project actions" accordion above the search/filter box in the left panel (inside a project view). When expanded, it lists project-level operations. Clicking an operation opens its form in the right panel, replacing the current detail view. Closing the operation returns to the project detail view.

### Left Panel — Project Actions Accordion

**Location:** Inside `ProjectView` in `ProjectExplorer.tsx`, between `panel-header` (breadcrumbs/upload) and `panel-filter` (search input).

**Behavior:**
- Collapsed by default, showing `▶ Project actions`
- Clicking toggles to `▼ Project actions` and reveals a list of operation buttons
- Each button sets `?sel=operation:<name>` in the URL params
- The accordion stays open after clicking an operation
- Styling matches the existing group-title pattern (chevron toggle, used for category headers)

### URL Scheme & DetailPanel Routing

New `?sel=operation:<name>` kind added to the existing URL selection system:

| sel value | Renders |
|---|---|
| (none) + /p/:projectId | ProjectDetail |
| project:\<id\> | ProjectDetail |
| object:\<id\> | ObjectDetail |
| operation:\<name\> | OperationPanel |

`OperationPanel` is a new component that switches on the operation name and renders the appropriate form. A "← Back to project" button clears `?sel=` and returns to ProjectDetail.

### Operation Forms

Each operation lives in its own file under `frontend/src/components/operations/`:

```
frontend/src/components/operations/
├── OperationPanel.tsx          ← router
├── MergeFastqForm.tsx          ← merge FASTQ files
├── BatchRenameForm.tsx         ← batch rename files
├── BatchTagForm.tsx            ← batch tag/metadata update
├── ExportSummaryForm.tsx       ← export project summary
└── QcAllReadsForm.tsx          ← run QC on all reads
```

Each form shares a pattern:
- Header with operation name + back button
- Body with form fields
- Execute button calling the backend API
- Status/progress feedback

### Initial Operations

| Operation | Description | Backend Endpoint |
|---|---|---|
| Merge FASTQ files | Select multiple FASTQ files, provide output name, concatenate | `POST /projects/{id}/operations/merge-fastq` |
| Batch rename files | Rename multiple files at once | `POST /projects/{id}/operations/batch-rename` |
| Batch tag/metadata | Add/remove tags on multiple files | `POST /projects/{id}/operations/batch-tags` |
| Export project summary | Generate and download project summary | `GET /projects/{id}/operations/export` |
| Run QC on all reads | Queue QC jobs for all read files | `POST /projects/{id}/operations/qc-all` |

### Backend

New `operations.py` router under `backend/app/api/v1/operations.py`, registered in the project router. Each operation handler is a separate endpoint. Merge FASTQ creates a pipeline job that concatenates files.

### Implementation Order

1. Frontend: Project actions accordion in ProjectExplorer
2. Frontend: OperationPanel router + DetailPanel dispatch update
3. Frontend: MergeFastqForm (first working operation)
4. Backend: Merge FASTQ endpoint + pipeline handler
5. Frontend: Remaining operation forms (stubs initially)
6. Backend: Remaining endpoints (as needed)

### Future

Operations are extensible by adding new form components and backend endpoints — no routing or state changes needed.
