# Project-Level Operations Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a collapsible "Project actions" accordion to the left panel with project-level operations that open forms in the right panel.

**Architecture:** URL-based routing extending the existing `?sel=` param with a new `operation:` kind. A new `OperationPanel` component dispatches to individual operation forms. New backend endpoints under `/projects/{id}/operations/`.

**Tech Stack:** React/TypeScript frontend, FastAPI Python backend, MongoDB

---

## Task 1: Create operations directory and OperationPanel router

**Objective:** Create the component directory and the OperationPanel routing component.

**Files:**
- Create: `frontend/src/components/operations/OperationPanel.tsx`
- Create: `frontend/src/components/operations/MergeFastqForm.tsx` (stub)
- Create: `frontend/src/components/operations/BatchRenameForm.tsx` (stub)
- Create: `frontend/src/components/operations/BatchTagForm.tsx` (stub)
- Create: `frontend/src/components/operations/ExportSummaryForm.tsx` (stub)
- Create: `frontend/src/components/operations/QcAllReadsForm.tsx` (stub)

**Step 1: Create the directory**

```bash
mkdir -p frontend/src/components/operations
```

**Step 2: Write OperationPanel.tsx**

```tsx
import { useNavigate } from "react-router-dom";
import { MergeFastqForm } from "./MergeFastqForm";
import { BatchRenameForm } from "./BatchRenameForm";
import { BatchTagForm } from "./BatchTagForm";
import { ExportSummaryForm } from "./ExportSummaryForm";
import { QcAllReadsForm } from "./QcAllReadsForm";

interface OperationPanelProps {
  name: string;
  projectId: string;
}

export function OperationPanel({ name, projectId }: OperationPanelProps) {
  const navigate = useNavigate();

  const backToProject = () => {
    // Clear the sel param to return to project detail
    const params = new URLSearchParams(window.location.search);
    params.delete("sel");
    navigate(`${window.location.pathname}?${params}`, { replace: true });
  };

  switch (name) {
    case "merge_fastq":
      return <MergeFastqForm projectId={projectId} onBack={backToProject} />;
    case "batch_rename":
      return <BatchRenameForm projectId={projectId} onBack={backToProject} />;
    case "batch_tags":
      return <BatchTagForm projectId={projectId} onBack={backToProject} />;
    case "export":
      return <ExportSummaryForm projectId={projectId} onBack={backToProject} />;
    case "qc_all":
      return <QcAllReadsForm projectId={projectId} onBack={backToProject} />;
    default:
      return (
        <div className="panel">
          <div className="panel-body">
            <div className="empty">
              <div className="empty-title">Unknown operation</div>
              <div>"{name}" is not a recognized project operation.</div>
            </div>
          </div>
        </div>
      );
  }
}
```

**Step 3: Write each stub form component**

Each stub form follows this pattern. Example for MergeFastqForm.tsx:

```tsx
interface FormProps {
  projectId: string;
  onBack: () => void;
}

export function MergeFastqForm({ projectId, onBack }: FormProps) {
  return (
    <div className="panel">
      <div className="panel-header">
        <button type="button" className="btn-text" onClick={onBack}>
          ← Back to project
        </button>
        <span className="panel-title">Merge FASTQ files</span>
      </div>
      <div className="panel-body detail">
        <p>Select FASTQ files to merge into a single file.</p>
        <p className="empty-title" style={{ marginTop: 24, color: "var(--text-faint)" }}>
          Coming soon
        </p>
      </div>
    </div>
  );
}
```

Create identical stubs for BatchRenameForm, BatchTagForm, ExportSummaryForm, and QcAllReadsForm, each with their own title and description.

**Step 4: Verify**

No runtime check yet — these aren't wired into DetailPanel.

**Step 5: Commit**

```bash
git add frontend/src/components/operations/
git commit -m "feat(ui): add OperationPanel router and stub operation forms"
```

---

## Task 2: Wire OperationPanel into DetailPanel

**Objective:** Add the `operation:` kind to DetailPanel's dispatch logic.

**Files:**
- Modify: `frontend/src/components/DetailPanel.tsx`

**Step 1: Add import**

Add to the existing imports:

```tsx
import { OperationPanel } from "./operations/OperationPanel";
```

**Step 2: Add operation kind dispatch**

Find the dispatch block around line 86-88:

```tsx
  if (kind === "project") return <ProjectDetail id={id} />;
  if (kind === "object") return <ObjectDetail id={id} />;
  return <EmptyDetail />;
```

Change to:

```tsx
  if (kind === "project") return <ProjectDetail id={id} />;
  if (kind === "object") return <ObjectDetail id={id} />;
  if (kind === "operation") {
    const projectMatch = pathname.match(/^\/p\/([^/]+)/);
    const projectId = projectMatch ? projectMatch[1] : "";
    return <OperationPanel name={id} projectId={projectId} />;
  }
  return <EmptyDetail />;
```

**Step 3: Verify**

Check that the file compiles — no runtime test yet.

**Step 4: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx
git commit -m "feat(ui): route operation: sel kind to OperationPanel"
```

---

## Task 3: Add Project actions accordion to ProjectExplorer

**Objective:** Add a collapsible "Project actions" accordion above the filter box in ProjectView.

**Files:**
- Modify: `frontend/src/components/ProjectExplorer.tsx`

**Step 1: Add state for accordion**

Find the state declarations in ProjectView (around line 253-259). Add:

```tsx
const [actionsOpen, setActionsOpen] = useState(false);
```

**Step 2: Add accordion JSX between panel-header and panel-filter**

Find the closing `</div>` of panel-header (around line 406-417) and the opening of panel-filter (around line 419). Insert between them:

```tsx
      {/* Project actions accordion */}
      <div className="panel-actions">
        <button
          type="button"
          className="group-title"
          aria-expanded={actionsOpen}
          onClick={() => setActionsOpen((v) => !v)}
        >
          <span className="group-chevron">{actionsOpen ? "▼" : "▶"}</span>
          <span>Project actions</span>
        </button>
        {actionsOpen && (
          <div className="panel-actions-body">
            <button
              type="button"
              className="action-item"
              onClick={() => select("operation:merge_fastq")}
            >
              Merge FASTQ files
            </button>
            <button
              type="button"
              className="action-item"
              onClick={() => select("operation:batch_rename")}
            >
              Batch rename files
            </button>
            <button
              type="button"
              className="action-item"
              onClick={() => select("operation:batch_tags")}
            >
              Batch tag/metadata
            </button>
            <button
              type="button"
              className="action-item"
              onClick={() => select("operation:export")}
            >
              Export project summary
            </button>
            <button
              type="button"
              className="action-item"
              onClick={() => select("operation:qc_all")}
            >
              Run QC on all reads
            </button>
          </div>
        )}
      </div>
```

**Step 3: Add CSS styles**

Add to `frontend/src/styles.css`:

```css
/* --- Project actions accordion --- */
.panel-actions {
  border-bottom: 1px solid var(--border);
}

.panel-actions .group-title {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
  background: none;
  border: none;
  cursor: pointer;
}

.panel-actions .group-title:hover {
  background: var(--hover-bg);
}

.panel-actions-body {
  padding: 2px 0 6px 12px;
  display: flex;
  flex-direction: column;
}

.action-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 5px 12px;
  font-size: 13px;
  color: var(--text);
  background: none;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}

.action-item:hover {
  background: var(--hover-bg);
  color: var(--accent);
}
```

**Step 4: Verify**

Open the project view in the browser, check that the accordion renders and toggles, and clicking an action updates the URL.

**Step 5: Commit**

```bash
git add frontend/src/components/ProjectExplorer.tsx frontend/src/styles.css
git commit -m "feat(ui): add Project actions accordion to left panel"
```

---

## Task 4: Add API client methods for operations

**Objective:** Add TypeScript API client methods for the new operation endpoints.

**Files:**
- Modify: `frontend/src/api/client.ts`

**Step 1: Add operation methods to the api object**

Add these methods after the existing project methods (e.g., after `registerInPlace` around line 373):

```tsx
  // --- Project operations ---

  mergeFastq: (projectId: string, objectIds: string[], outputName: string) =>
    request<{ job_id: string }>(`/projects/${projectId}/operations/merge-fastq`, {
      method: "POST",
      body: JSON.stringify({ object_ids: objectIds, output_name: outputName }),
    }),

  batchRename: (projectId: string, renames: { id: string; name: string }[]) =>
    request<{ updated: number }>(`/projects/${projectId}/operations/batch-rename`, {
      method: "POST",
      body: JSON.stringify({ renames }),
    }),

  batchTags: (projectId: string, objectIds: string[], add: string[], remove: string[]) =>
    request<{ updated: number }>(`/projects/${projectId}/operations/batch-tags`, {
      method: "POST",
      body: JSON.stringify({ object_ids: objectIds, add, remove }),
    }),

  exportProject: (projectId: string) =>
    `${BASE}/projects/${projectId}/operations/export?${profileQuery()}`,

  qcAllReads: (projectId: string) =>
    request<{ job_ids: string[] }>(`/projects/${projectId}/operations/qc-all`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
```

**Step 2: Verify**

Check that the file has no syntax errors.

**Step 3: Commit**

```bash
git add frontend/src/api/client.ts
git commit -m "feat(api): add client methods for project operations"
```

---

## Task 5: Create backend operations router

**Objective:** Create the backend router for project-level operations.

**Files:**
- Create: `backend/app/api/v1/operations.py`
- Modify: `backend/app/api/v1/__init__.py`

**Step 1: Write operations.py**

```python
"""Project-level operation endpoints.

Operations are project-scoped actions that don't require selecting a specific
file first. They live under /projects/{project_id}/operations/.
"""

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import OwnerDep
from app.errors import ValidationError

router = APIRouter(prefix="/projects/{project_id}/operations", tags=["operations"])


class MergeFastqRequest(BaseModel):
    object_ids: list[str] = Field(..., min_length=2, max_length=100)
    output_name: str = Field(..., min_length=1, max_length=255)


class BatchRenameRequest(BaseModel):
    renames: list[dict] = Field(..., min_length=1, max_length=100)


class BatchTagsRequest(BaseModel):
    object_ids: list[str] = Field(..., min_length=1, max_length=100)
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class QcAllRequest(BaseModel):
    pass


@router.post("/merge-fastq", status_code=status.HTTP_202_ACCEPTED)
async def merge_fastq(
    project_id: PydanticObjectId,
    body: MergeFastqRequest,
    owner: OwnerDep,
) -> dict:
    """Concatenate multiple FASTQ files into one new file."""
    # TODO: Implement merge-fastq logic
    raise ValidationError("Not implemented yet")


@router.post("/batch-rename", status_code=status.HTTP_200_OK)
async def batch_rename(
    project_id: PydanticObjectId,
    body: BatchRenameRequest,
    owner: OwnerDep,
) -> dict:
    """Rename multiple files at once."""
    # TODO: Implement batch rename logic
    raise ValidationError("Not implemented yet")


@router.post("/batch-tags", status_code=status.HTTP_200_OK)
async def batch_tags(
    project_id: PydanticObjectId,
    body: BatchTagsRequest,
    owner: OwnerDep,
) -> dict:
    """Add/remove tags on multiple files."""
    # TODO: Implement batch tags logic (can reuse existing bulk-tags service)
    raise ValidationError("Not implemented yet")


@router.get("/export", status_code=status.HTTP_200_OK)
async def export_project(
    project_id: PydanticObjectId,
    owner: OwnerDep,
) -> dict:
    """Generate and return a project summary."""
    # TODO: Implement export logic
    raise ValidationError("Not implemented yet")


@router.post("/qc-all", status_code=status.HTTP_202_ACCEPTED)
async def qc_all_reads(
    project_id: PydanticObjectId,
    body: QcAllRequest,
    owner: OwnerDep,
) -> dict:
    """Queue QC jobs for all read files in the project."""
    # TODO: Implement qc-all logic
    raise ValidationError("Not implemented yet")
```

**Step 2: Register in __init__.py**

Add import and include_router in `backend/app/api/v1/__init__.py`:

```python
from app.api.v1 import (
    ...
    operations,
    ...
)

api_router.include_router(operations.router)
```

Add `operations` to the import block (alphabetical order, after `objects` and before `pipelines`).

**Step 3: Verify**

```bash
cd backend && python -c "from app.api.v1 import operations; print('OK')"
```

**Step 4: Commit**

```bash
git add backend/app/api/v1/operations.py backend/app/api/v1/__init__.py
git commit -m "feat(api): add operations router with stub endpoints"
```

---

## Task 6: Implement MergeFastqForm UI

**Objective:** Build the full MergeFastqForm with file selection, output naming, and job launch.

**Files:**
- Modify: `frontend/src/components/operations/MergeFastqForm.tsx`

**Step 1: Write the full component**

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import type { DataObject } from "../../api/types";
import { formatBytes, formatKindLabel } from "../../lib/format";
import { notify } from "../../stores/messageStore";

interface MergeFastqFormProps {
  projectId: string;
  onBack: () => void;
}

export function MergeFastqForm({ projectId, onBack }: MergeFastqFormProps) {
  const qc = useQueryClient();
  const { data: objects } = useQuery({
    queryKey: ["objects", projectId],
    queryFn: () => api.listObjects(projectId),
  });

  const fastqFiles = (objects ?? []).filter(
    (o) => o.format.kind === "fastq" && o.status === "ready"
  );

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [outputName, setOutputName] = useState("");

  const merge = useMutation({
    mutationFn: () =>
      api.mergeFastq(projectId, [...selected], outputName.trim() || "merged.fastq.gz"),
    onSuccess: (result) => {
      notify.success(`Merge job launched: ${result.job_id}`);
      qc.invalidateQueries({ queryKey: ["objects", projectId] });
      onBack();
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const toggleFile = (id: string) => {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  };

  const canMerge = selected.size >= 2 && outputName.trim().length > 0 && !merge.isPending;

  return (
    <div className="panel">
      <div className="panel-header">
        <button type="button" className="btn-text" onClick={onBack}>
          ← Back to project
        </button>
        <span className="panel-title">Merge FASTQ files</span>
      </div>
      <div className="panel-body detail">
        <p style={{ marginBottom: 16, color: "var(--text-muted)" }}>
          Select two or more FASTQ files to concatenate into a single file.
          Files are merged in the order they are selected.
        </p>

        <div style={{ marginBottom: 16 }}>
          <label style={{ display: "block", fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
            Output filename
          </label>
          <input
            type="text"
            value={outputName}
            onChange={(e) => setOutputName(e.target.value)}
            placeholder="merged.fastq.gz"
            style={{ width: "100%", padding: "6px 8px" }}
          />
        </div>

        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>
            Select FASTQ files ({selected.size} selected)
          </div>
          {fastqFiles.length === 0 && (
            <p style={{ color: "var(--text-faint)" }}>
              No FASTQ files in this project.
            </p>
          )}
          <div style={{ maxHeight: 300, overflowY: "auto", border: "1px solid var(--border)", borderRadius: 4 }}>
            {fastqFiles.map((file) => (
              <label
                key={file.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "6px 8px",
                  cursor: "pointer",
                  background: selected.has(file.id)
                    ? "color-mix(in srgb, var(--accent) 10%, transparent)"
                    : undefined,
                }}
              >
                <input
                  type="checkbox"
                  checked={selected.has(file.id)}
                  onChange={() => toggleFile(file.id)}
                />
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13 }}>{file.name}</div>
                  <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
                    {formatBytes(file.size)}
                  </div>
                </div>
              </label>
            ))}
          </div>
        </div>

        <button
          type="button"
          className="btn primary"
          disabled={!canMerge}
          onClick={() => merge.mutate()}
        >
          {merge.isPending ? "Merging…" : "Merge files"}
        </button>
      </div>
    </div>
  );
}
```

**Step 2: Verify**

Open the browser, navigate to a project with FASTQ files, expand Project actions, click "Merge FASTQ files", verify the form renders. The button should be disabled until 2+ files are selected and a name is entered.

**Step 3: Commit**

```bash
git add frontend/src/components/operations/MergeFastqForm.tsx
git commit -m "feat(ui): implement MergeFastqForm with file selection UI"
```

---

## Task 7: Implement merge-fastq backend endpoint

**Objective:** Implement the merge-fastq endpoint that concatenates FASTQ files as a pipeline job.

**Files:**
- Modify: `backend/app/api/v1/operations.py`

**Step 1: Add merge-fastq implementation**

Replace the stub in `operations.py` with real logic:

```python
@router.post("/merge-fastq", status_code=status.HTTP_202_ACCEPTED)
async def merge_fastq(
    project_id: PydanticObjectId,
    body: MergeFastqRequest,
    owner: OwnerDep,
) -> dict:
    """Concatenate multiple FASTQ files into one new file."""
    from app.services import object_service
    from app.queue.registry import get_queue

    # Verify all objects exist, are FASTQ, and belong to this project
    objects = await object_service.list_objects(project_id, owner=owner)
    obj_map = {str(o.id): o for o in objects}
    
    for oid in body.object_ids:
        obj = obj_map.get(oid)
        if not obj:
            raise ValidationError(f"Object {oid} not found in project")
        if obj.format.kind != "fastq":
            raise ValidationError(f"Object {obj.name} is not a FASTQ file")
        if obj.status != "ready":
            raise ValidationError(f"Object {obj.name} is not ready (status: {obj.status})")
    
    # Create a merge job on the queue
    queue = get_queue()
    job = await queue.enqueue(
        job_type="merge_fastq",
        payload={
            "project_id": str(project_id),
            "object_ids": body.object_ids,
            "output_name": body.output_name,
        },
        owner=owner,
    )
    
    return {"job_id": str(job.id)}
```

**Step 2: Add merge_fastq pipeline handler**

Create `backend/app/queue/merge_fastq_handler.py`:

```python
"""Handler for merging FASTQ files by concatenation."""

import shutil
from pathlib import Path

from app.errors import PermanentError
from app.models import IoClass, JobClass, JobResources
from app.queue.executor import run_subprocess
from app.queue.registry import HandlerMode, JobContext, handler
from app.storage.paths import blob_path

log = get_logger(__name__)


@handler(
    "merge_fastq",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
    max_attempts=2,
)
def merge_fastq_handler(ctx: JobContext) -> dict:
    """Concatenate multiple FASTQ files into one.
    
    Uses shell concatenation (cat) for efficiency — FASTQ is plain text/gzip,
    and cat handles both seamlessly.
    """
    object_ids = ctx.payload.get("object_ids", [])
    output_name = ctx.payload.get("output_name", "merged.fastq.gz")
    project_id = ctx.payload.get("project_id")

    if not object_ids or len(object_ids) < 2:
        raise PermanentError("merge_fastq requires at least 2 object_ids")
    
    # Resolve input paths
    input_paths = [blob_path(oid) for oid in object_ids]
    for p in input_paths:
        if not p.exists():
            raise PermanentError(f"Input file not found: {p}")
    
    # Output path in scratch directory
    scratch = ctx.scratch_dir
    output_path = scratch / output_name
    
    # Concatenate files
    cmd = ["cat"] + [str(p) for p in input_paths]
    result = run_subprocess(cmd, stdout=str(output_path))
    
    if result.exit_code != 0:
        raise PermanentError(f"Merge failed (exit {result.exit_code}): {result.stderr}")
    
    # Register the merged file as a new object
    # TODO: register output in the project
    
    return {
        "output_name": output_name,
        "output_path": str(output_path),
        "input_count": len(object_ids),
        "total_bytes": sum(p.stat().st_size for p in input_paths),
    }
```

Note: The handler imports `get_logger` and uses `blob_path` — ensure these imports are added.

**Step 3: Import the handler**

Add to `backend/app/queue/pipeline_handlers.py` or create a separate import in the handlers registry. The simplest approach is to import the new handler module at the bottom of `pipeline_handlers.py`:

```python
# Register merge_fastq handler
from app.queue import merge_fastq_handler  # noqa: F401
```

Or register it in `handlers.py` where all handlers are collected.

**Step 4: Verify**

```bash
cd backend && python -c "from app.queue.merge_fastq_handler import merge_fastq_handler; print('OK')"
```

**Step 5: Commit**

```bash
git add backend/app/api/v1/operations.py backend/app/queue/merge_fastq_handler.py
git commit -m "feat(api): implement merge-fastq endpoint and handler"
```

---

## Task 8: Implement remaining backend stubs

**Objective:** Wire up the remaining operation endpoints to existing services.

**Files:**
- Modify: `backend/app/api/v1/operations.py`

**Step 1: Implement batch-tags (reuses existing bulk-tags service)**

Replace the batch_tags stub:

```python
@router.post("/batch-tags", status_code=status.HTTP_200_OK)
async def batch_tags(
    project_id: PydanticObjectId,
    body: BatchTagsRequest,
    owner: OwnerDep,
) -> dict:
    """Add/remove tags on multiple files. Reuses the existing bulk-tags service."""
    from app.services import search_service
    
    result = await search_service.bulk_update_tags(
        owner=owner,
        object_ids=[PydanticObjectId(oid) for oid in body.object_ids],
        add=body.add,
        remove=body.remove,
    )
    return {"updated": result}
```

**Step 2: Implement batch-rename**

```python
@router.post("/batch-rename", status_code=status.HTTP_200_OK)
async def batch_rename(
    project_id: PydanticObjectId,
    body: BatchRenameRequest,
    owner: OwnerDep,
) -> dict:
    """Rename multiple files at once."""
    from app.services import object_service
    
    updated = 0
    for rename in body.renames:
        obj_id = rename.get("id")
        name = rename.get("name")
        if not obj_id or not name:
            continue
        await object_service.update_object(
            PydanticObjectId(obj_id),
            {"name": name},
            owner=owner,
        )
        updated += 1
    
    return {"updated": updated}
```

**Step 3: Implement export (simple project stats)**

```python
@router.get("/export", status_code=status.HTTP_200_OK)
async def export_project(
    project_id: PydanticObjectId,
    owner: OwnerDep,
) -> dict:
    """Generate and return a project summary."""
    from app.services import project_service, object_service
    
    project = await project_service.get_project(project_id, owner=owner)
    objects = await object_service.list_objects(project_id, owner=owner)
    
    return {
        "project_name": project.name,
        "project_description": project.description,
        "created_at": project.created_at.isoformat(),
        "total_files": len(objects),
        "total_bytes": sum(o.size for o in objects),
        "files_by_format": _count_by(objects, lambda o: o.format.kind),
        "files_by_status": _count_by(objects, lambda o: o.status),
    }


def _count_by(items, key_fn):
    counts = {}
    for item in items:
        k = key_fn(item)
        counts[k] = counts.get(k, 0) + 1
    return counts
```

**Step 4: Implement qc-all**

```python
@router.post("/qc-all", status_code=status.HTTP_202_ACCEPTED)
async def qc_all_reads(
    project_id: PydanticObjectId,
    body: QcAllRequest,
    owner: OwnerDep,
) -> dict:
    """Queue QC jobs for all read files in the project."""
    from app.services import object_service
    from app.queue.registry import get_queue
    
    objects = await object_service.list_objects(project_id, owner=owner)
    reads = [
        o for o in objects
        if o.format.kind == "fastq" and o.status == "ready"
    ]
    
    queue = get_queue()
    job_ids = []
    for read in reads:
        job = await queue.enqueue(
            job_type="run_qc",
            payload={"object_id": str(read.id)},
            owner=owner,
        )
        job_ids.append(str(job.id))
    
    return {"job_ids": job_ids, "count": len(job_ids)}
```

**Step 5: Verify**

```bash
cd backend && python -c "from app.api.v1.operations import router; print('OK')"
```

**Step 6: Commit**

```bash
git add backend/app/api/v1/operations.py
git commit -m "feat(api): implement batch-tags, batch-rename, export, qc-all endpoints"
```

---

## Task 9: Build and verify end-to-end

**Objective:** Build the app and verify the full flow works.

**Step 1: Build and restart**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner
docker compose up -d --build api web worker
```

**Step 2: Verify in browser**

- Navigate to a project
- Confirm the "▶ Project actions" accordion is visible above the filter box
- Click to expand — verify 5 action items appear
- Click "Merge FASTQ files" — verify the right panel shows the form
- Click "← Back to project" — verify it returns to project detail
- Verify the other action items also open their forms

**Step 3: Test the backend endpoints**

```bash
# Test merge-fastq (will fail with 422 since not implemented, but tests routing)
docker compose exec api python -c "
import httpx
r = httpx.post('http://localhost:8000/api/v1/projects/000000000000000000000000/operations/merge-fastq', json={'object_ids': [], 'output_name': 'test.fastq.gz'})
print(r.status_code, r.json())
"
```

**Step 4: Commit any fixes**

```bash
git commit -am "fix: post-build adjustments"
```

---

## Verification Checklist

- [ ] "▶ Project actions" accordion renders above filter box in project view
- [ ] Accordion toggles open/closed with chevron rotation
- [ ] 5 action items visible when expanded
- [ ] Clicking an action sets `?sel=operation:<name>` in URL
- [ ] Right panel shows the operation form
- [ ] "← Back to project" clears `?sel=` and returns to project detail
- [ ] MergeFastqForm lists FASTQ files with checkboxes
- [ ] Merge button is disabled until 2+ files selected and name entered
- [ ] All backend endpoints return correct status codes
- [ ] Backend endpoints validate input correctly
- [ ] `make test` passes
- [ ] `make lint` passes
