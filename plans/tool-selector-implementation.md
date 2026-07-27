# Tool Selection Screen — Implementation Plan

## Problem

Clicking **Trim** or **Align** in the DetailPanel jumps straight to the parameter screen. But:

- Align already has two tools (bwa-mem2, minimap2) with different strengths — short-read vs. long-read — and the choice is buried under "Aligner and performance" in an `advanced` section.
- Trim currently has one tool (fastp), but more could be added (e.g. `cutadapt`, `trimmomatic`).
- Future pipelines (QC, variant calling, assembly) will each have multiple tool options.
- Installed-but-not-runnable tools (e.g. bwa-mem2 on arm64) should be visible as disabled options with the reason, not hidden until a job fails.

**Goal:** Insert a tool selection screen between the pipeline button and the parameter screen. It shows cards for every tool available for that pipeline, with a short "what this tool excels at" summary, disabled cards for un-runnable tools, and a "Continue" button that proceeds to the existing parameter dialog with the selected tool pre-configured.

---

## 1. User Flow

```
DetailPanel: click "Trim"
  → ToolSelector modal (pipeline="trim")
    ┌─────────────────────────────────────┐
    │  Select a trimming tool              │
    │                                      │
    │  ┌────────────────────────────────┐  │
    │  │ ✓ fastp v0.24.0      SELECTED │  │
    │  │   All-purpose fastq adapter    │  │
    │  │   trimmer with QC reporting.   │  │
    │  │   Excels at: PE overlap        │  │
    │  │   adapter detection, poly-G    │  │
    │  │   trimming, per-cycle QC.      │  │
    │  └────────────────────────────────┘  │
    │                                      │
    │  [ Continue → ]                      │
    └─────────────────────────────────────┘

  → TrimDialog (existing, tool pre-selected as "fastp")
```

```
DetailPanel: click "Align"
  → ToolSelector modal (pipeline="align")
    ┌─────────────────────────────────────┐
    │  Select an aligner                   │
    │                                      │
    │  ┌────────────────────────────────┐  │
    │  │ ◯ minimap2 v2.27              │  │
    │  │   Universal aligner. Works on  │  │
    │  │   all read lengths.            │  │
    │  │   Excels at: long reads (ONT,  │  │
    │  │   PacBio), short reads with    │  │
    │  │   presets. Runs everywhere.    │  │
    │  └────────────────────────────────┘  │
    │  ┌────────────────────────────────┐  │
    │  │ ✓ bwa-mem2 v2.2.1   SELECTED  │  │
    │  │   BWA-MEM2: MEM algorithm v2  │  │
    │  │   Excels at: short-read        │  │
    │  │   alignment on Illumina data.  │  │
    │  │   Fastest option when available.│  │
    │  └────────────────────────────────┘  │
    │                                      │
    │  [ Continue → ]                      │
    └─────────────────────────────────────┘

  → AlignDialog (existing, aligner pre-selected, not hidden in "advanced")
```

For a disabled tool:

```
    ┌────────────────────────────────┐
    │  ◯ bwa-mem2 (not available) 🚫 │
    │   BWA-MEM2 is compiled for x86 │
    │   and cannot run on this host. │
    │   Use minimap2 for short reads.│
    └────────────────────────────────┘   (greyed out, not clickable)
```

---

## 2. Backend Changes

### 2.1 Pipeline-tool attribution (`backend/app/pipelines/tools.py`)

Add a `pipeline` field to the `Tool` dataclass to categorize which pipeline(s) a tool belongs to:

```python
class PipelineType(StrEnum):
    TRIM = "trim"
    ALIGN = "align"
    QC = "qc"
    SRA = "sra"       # future: fasterq-dump
    VARIANT = "variant"  # future: bcftools, GATK

@dataclass(frozen=True)
class Tool:
    name: str
    path: str | None
    version: str | None
    error: str | None = None
    pipeline: PipelineType = PipelineType.TRIM       # NEW
    summary: str = ""                                 # NEW
    strengths: tuple[str, ...] = ()                   # NEW
```

Then assign these per tool in `all_tools()`:

```python
def all_tools() -> list[Tool]:
    return [
        fastp(),       # pipeline=TRIM
        bwa_mem2(),    # pipeline=ALIGN
        minimap2(),    # pipeline=ALIGN
        fastqc(),      # pipeline=QC
        samtools(),    # pipeline=ALIGN  (sorting, indexing, markdup)
    ]
```

The `summary` and `strengths` are authored per tool in their probe function rather than in a second mapping:

```python
@lru_cache(maxsize=1)
def fastp() -> Tool:
    tool = _probe("fastp", settings.fastp_path, ["--version"])
    # Replace the frozen dataclass via object.__setattr__ is not possible.
    # Instead, include these in the probe response.
    ...
```

**Implementation detail:** Since `Tool` is a frozen dataclass, we can't modify it after `_probe`. We have two options:

**Option A** (cleaner): Have `_probe` accept optional `pipeline`, `summary`, `strengths` kwargs that it includes in the return:

```python
def _probe(name, configured, version_args, *, pipeline=PipelineType.TRIM, summary="", strengths=()) -> Tool:
    ...
    return Tool(
        name=name, path=resolved, version=version, error=..., 
        pipeline=pipeline, summary=summary, strengths=strengths,
    )
```

**Option B** (minimal diff): Keep `Tool` as-is, add a separate lookup dict mapping name → `(pipeline, summary, strengths)` that the API endpoint enriches. This keeps the probe functions unchanged and is the recommended approach for this plan.

Add in `tools.py`:

```python
TOOL_META: dict[str, tuple[PipelineType, str, tuple[str, ...]]] = {
    "fastp": (
        PipelineType.TRIM,
        "All-purpose FASTQ adapter trimmer with built-in QC reporting "
        "and per-cycle quality analysis.",
        (
            "Paired-end overlap adapter detection",
            "Poly-G trimming for NovaSeq data",
            "Per-cycle QC metrics (fastp JSON)",
            "Duplicate read removal",
        ),
    ),
    "bwa-mem2": (
        PipelineType.ALIGN,
        "BWA-MEM algorithm version 2. Fast short-read alignment. "
        "Optimized for Illumina data.",
        (
            "Fastest short-read alignment when available",
            "Standard for Illumina WGS DNA-seq",
            "x86-64 only (Intel compiler dispatch)",
        ),
    ),
    "minimap2": (
        PipelineType.ALIGN,
        "Universal aligner supporting all read lengths from short "
        "Illumina to ultra-long Nanopore.",
        (
            "Long-read alignment (ONT, PacBio HiFi/CLR)",
            "Short-read alignment with -x sr preset",
            "Runs on all architectures including arm64",
        ),
    ),
    "fastqc": (
        PipelineType.QC,
        "Standard QC report generator for Illumina short reads. "
        "Produces the HTML report collaborators expect.",
        (
            "Per-base quality scores (HTML report)",
            "Adapter content detection",
            "Overrepresented sequences",
        ),
    ),
    "samtools": (
        PipelineType.ALIGN,
        "SAM/BAM/CRAM manipulation toolkit. Sort, index, mark "
        "duplicates, flagstat.",
        (
            "Coordinate sorting of alignments",
            "Marking PCR/optical duplicates",
            "Indexing and stats (flagstat)",
        ),
    ),
}
```

Update the `all_tools()` API serialization to include the metadata:

```python
def all_tools() -> list[dict]:
    result = []
    for t in _all():
        meta = TOOL_META.get(t.name, (PipelineType.TRIM, "", ()))
        d = t.as_dict()
        d["pipeline"] = meta[0].value
        d["summary"] = meta[1]
        d["strengths"] = list(meta[2])
        result.append(d)
    return result
```

### 2.2 New API endpoint (optional)

If we want to be able to query per-pipeline, we can add:

```python
@router.get("/tools/{pipeline}")
async def list_tools_for_pipeline(pipeline: str) -> dict:
    tools_for_pipeline = [
        t for t in tools.all_tools()
        if t["pipeline"] == pipeline
    ]
    return {"pipeline": pipeline, "tools": tools_for_pipeline}
```

This is optional — the frontend can filter the full list. Having a dedicated endpoint makes the intent explicit and allows per-pipeline caching in the future.

### 2.3 Align dialog: remove aligner from "advanced"

Currently the aligner dropdown is inside the `advanced` section. Once the tool selector sets the aligner, the AlignDialog should:
- Accept a `selectedTool` prop with the aligner name
- Remove the aligner selector from the form (it's already decided)
- Keep showing the tool name prominently so the user sees what they chose
- The `aligner` field in the API call should reflect the selected tool

### 2.4 Server-side tool availability check

The existing tool probe already detects un-runnable tools:
- `bwa_mem2().available == False` on arm64 with Rosetta error
- `fastp().available == False` if not on PATH
- Each with a descriptive `error` string

The availability flows through the API unchanged. The new UI just presents it more visibly.

---

## 3. Frontend Changes

### 3.1 New component: `PipelineToolSelector.tsx`

A modal shown before the parameter dialog. It receives:

```typescript
interface PipelineToolSelectorProps {
  pipeline: "trim" | "align" | "qc";
  onSelect: (toolName: string) => void;
  onClose: () => void;
}
```

It fetches `GET /pipelines/tools` and filters by pipeline.

**Layout:**

```
modal-backdrop
  modal tool-selector
    h2: "Select a trimming tool" / "Select an aligner"
    
    // One card per tool
    div.tool-card              → clickable (when available)
      div.tool-card-header
        span.tool-radio         → radio circle, filled if selected
        span.tool-name          → "minimap2"
        span.tool-version       → "v2.27"
        span.tool-badge         → chip on the right: "recommended" / "fastest"
      div.tool-card-summary
        p                      → the summary text
      ul.tool-card-strengths
        li each strength
    
    tool-card.disabled          → greyed out, not clickable
      Same structure but add:
      div.tool-card-error
        The error message from the probe
    
    modal-actions
      btn "Cancel"
      btn primary "Continue"   → disabled until a tool is selected
```

**States for tool badges:**

| Condition | Badge |
|---|---|
| Recommended / best match for the file's platform | "recommended" (green) |
| Fastest option when available | "fastest" (blue) |
| Single option | "only option" (grey) |
| Unavailable | ✕ "not available" (red) |

The **recommended** badge logic: use the existing SAM platform mapping from `pipeline_service.sam_platform()` + `suggested_preset()` on the backend to determine which aligner matches the reads' platform. This can be exposed via the defaults endpoint. Or simpler: keep it server-side in the tools metadata.

**Actually simpler:** add a `recommended: bool` field to the per-tool metadata. For now:
- For align: `minimap2` is recommended for long reads (ONT/PacBio), `bwa-mem2` for short reads (Illumina) **when available**.
- For trim: `fastp` is always the only trim tool.
- The recommendation flag is computed server-side per-object, so it can depend on the file's platform metadata.

But this complicates the endpoint. For now, we can keep it simple: just show the tools, let the user pick. We can add recommendation badges later.

### 3.2 Styling (`frontend/src/styles/tool-selector.css`)

```css
.tool-selector {
  max-width: 520px;
  width: 100%;
}

.tool-card {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 8px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
  background: var(--surface-bg);
}

.tool-card:hover:not(.disabled) {
  border-color: var(--accent);
  background: var(--surface-hover);
}

.tool-card.selected {
  border-color: var(--accent);
  background: var(--surface-selected);
}

.tool-card.disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.tool-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.tool-radio {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 2px solid var(--border-color);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.tool-card.selected .tool-radio {
  border-color: var(--accent);
}
.tool-radio-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--accent);
}

.tool-name {
  font-weight: 600;
  font-size: 14px;
}

.tool-version {
  font-size: 11px;
  color: var(--text-muted);
}

.tool-badge {
  margin-left: auto;
  font-size: 10px;
  padding: 1px 7px;
  border-radius: 99px;
  background: var(--bg-subtle);
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.tool-badge.recommended {
  background: #e8f5e9;
  color: #2e7d32;
}

.tool-badge.fastest {
  background: #e3f2fd;
  color: #1565c0;
}

.tool-badge.unavailable {
  background: #fce4ec;
  color: #c62828;
}

.tool-card-summary {
  font-size: 12px;
  line-height: 1.4;
  color: var(--text-secondary);
  margin-bottom: 4px;
}

.tool-card-strengths {
  margin: 0;
  padding-left: 16px;
  font-size: 11px;
  color: var(--text-muted);
}

.tool-card-strengths li {
  margin-bottom: 1px;
}

.tool-card-error {
  margin-top: 6px;
  padding: 6px 8px;
  background: #fff3e0;
  border-radius: 4px;
  font-size: 11px;
  color: #e65100;
}
```

### 3.3 Modified `DetailPanel.tsx`

Current buttons:

```tsx
{canTrim && (
  <button className="btn" onClick={() => setTrimOpen(true)}>Trim</button>
)}
{canAlign && (
  <button className="btn" onClick={() => setAlignOpen(true)}>Align</button>
)}
```

New flow:

```tsx
{canTrim && (
  <button className="btn" onClick={() => setTrimToolOpen(true)}>Trim</button>
)}
{canAlign && (
  <button className="btn" onClick={() => setAlignToolOpen(true)}>Align</button>
)}

{/* Tool selector screens */}
{trimToolOpen && (
  <PipelineToolSelector
    pipeline="trim"
    onSelect={(tool) => { setTrimToolOpen(false); setSelectedTrimTool(tool); setTrimDialogOpen(true); }}
    onClose={() => setTrimToolOpen(false)}
  />
)}

{/* Existing parameter screens, now with pre-selected tool */}
{trimDialogOpen && (
  <TrimDialog
    object={obj}
    onClose={() => setTrimDialogOpen(false)}
    selectedTool={selectedTrimTool}
  />
)}

{alignToolOpen && (
  <PipelineToolSelector
    pipeline="align"
    onSelect={(tool) => { setAlignToolOpen(false); setSelectedAlignTool(tool); setAlignDialogOpen(true); }}
    onClose={() => setAlignToolOpen(false)}
  />
)}

{alignDialogOpen && (
  <AlignDialog
    object={obj}
    onClose={() => setAlignDialogOpen(false)}
    selectedTool={selectedAlignTool}
  />
)}
```

New state variables in `DetailPanel`:

```typescript
const [trimToolOpen, setTrimToolOpen] = useState(false);
const [selectedTrimTool, setSelectedTrimTool] = useState<string | null>(null);
const [trimDialogOpen, setTrimDialogOpen] = useState(false);

const [alignToolOpen, setAlignToolOpen] = useState(false);
const [selectedAlignTool, setSelectedAlignTool] = useState<string | null>(null);
const [alignDialogOpen, setAlignDialogOpen] = useState(false);
```

### 3.4 Modified `TrimDialog.tsx`

Accept `selectedTool` prop:

```typescript
export function TrimDialog({
  object,
  onClose,
  selectedTool,
}: {
  object: DataObject;
  onClose: () => void;
  selectedTool?: string;  // e.g., "fastp"
}) {
```

- Show the selected tool name in the dialog title: `"Trim reads — fastp"`
- If `selectedTool` is provided, skip the `tools` query for checking availability (we already confirmed it in the selector)
- If `selectedTool` doesn't match the pipeline (shouldn't happen), fall through to the existing behavior

### 3.5 Modified `AlignDialog.tsx`

Accept `selectedTool` prop:

```typescript
export function AlignDialog({
  object,
  onClose,
  selectedTool,
}: {
  object: DataObject;
  onClose: () => void;
  selectedTool?: AlignerName;  // e.g., "minimap2" or "bwa-mem2"
}) {
```

- **Remove** the aligner dropdown from the `advanced` section (lines 227-240 of current `AlignDialog.tsx`)
- Show the selected aligner name prominently at the top: `"Align reads — minimap2"`
- The backend's `default_align_params` still selects the best default aligner, but the tool selector choice overrides it
- Pass `selectedTool` as the `aligner` value when launching

The backend `alignDefaults` endpoint returns the default aligner. The dialog should use `selectedTool` if provided, otherwise fall back to the server default:

```typescript
const effectiveAligner = selectedTool ?? defaults?.params?.aligner ?? "minimap2";
```

### 3.6 New types in `types.ts`

```typescript
type PipelineType = "trim" | "align" | "qc" | "sra" | "variant";

interface PipelineTool {
  name: string;
  path: string | null;
  version: string | null;
  available: boolean;
  error: string | null;
  pipeline: PipelineType;
  summary: string;
  strengths: string[];
}

interface PipelineTools {
  tools: PipelineTool[];
  all_available: boolean;
}
```

Update `PipelineTools` type in client.ts and types.ts to include the new fields.

---

## 4. New API endpoint (if per-pipeline)

```python
@router.get("/tools/{pipeline}")
async def list_tools_for_pipeline(pipeline: str) -> dict:
    """
    Tools available for a specific pipeline (trim, align, qc, etc.).
    Each tool includes availability, version, summary, and strengths.
    """
    if pipeline not in [pt.value for pt in PipelineType]:
        raise ValidationError(f"Unknown pipeline: {pipeline}")
    return {
        "pipeline": pipeline,
        "tools": [
            t for t in tools.all_tools()
            if t["pipeline"] == pipeline
        ],
    }
```

---

## 5. File Inventory

### New files:
```
frontend/src/components/PipelineToolSelector.tsx  — tool selection component
frontend/src/styles/tool-selector.css             — component styles
```

### Modified files:
```
backend/app/pipelines/tools.py                    — add TOOL_META, pipeline field, all_tools() enrichment
backend/app/api/v1/pipelines.py                   — optionally: per-pipeline endpoint
frontend/src/api/types.ts                         — PipelineTool type update
frontend/src/api/client.ts                        — pipelineToolsForPipeline() (optional)
frontend/src/components/DetailPanel.tsx            — insert tool selector before TrimDialog/AlignDialog
frontend/src/components/TrimDialog.tsx             — accept selectedTool prop
frontend/src/components/AlignDialog.tsx            — accept selectedTool prop, remove aligner dropdown
```

---

## 6. Implementation Order

| Step | Area | Description |
|------|------|-------------|
| 1 | Backend | Add `TOOL_META` dictionary and `pipeline`/`summary`/`strengths` to `tools.py` |
| 2 | Backend | Update `all_tools()` to include metadata in serialization |
| 3 | Backend | Optionally add `GET /pipelines/tools/{pipeline}` endpoint |
| 4 | Frontend | Update `PipelineTool` type in `types.ts` |
| 5 | Frontend | Build `PipelineToolSelector.tsx` component |
| 6 | Frontend | Write `tool-selector.css` styles |
| 7 | Frontend | Update `DetailPanel.tsx` — add tool selector states, wire up flow |
| 8 | Frontend | Update `TrimDialog.tsx` — accept `selectedTool` prop |
| 9 | Frontend | Update `AlignDialog.tsx` — accept `selectedTool`, remove aligner dropdown |
| 10 | Test | Verify tool cards render, disabled tools greyed out, selection flows to params |

---

## 7. Edge Cases & States

| Scenario | Behavior |
|---|---|
| All tools unavailable for a pipeline | Tool selector shows all cards greyed out with errors. "Continue" is disabled. Alternative: show a warning with no selectable cards. |
| Network error fetching tools | Tool selector shows loading spinner, then error retry. User can close and try again. |
| Single tool available | Tool card shown, pre-selected with "only option" badge. Single click or Continue works. |
| File is a pair (mate detected) | Tool selector operates on the file object; the mate is handled at the parameter screen as today. |
| User clicks Back from parameter screen | Could add a Back button on the parameter dialog that returns to the tool selector. (Future improvement — not in v1.) |
| User closes tool selector without selecting | Close/Cancel returns to DetailPanel, no dialog opens. |
| Tool selector with no tools at all for pipeline | Show empty state: "No trimming tools are installed. Install fastp or set FASTP_PATH." |

---

## 8. Example: "recommended" badge (future enhancement)

The server could compute which tool is recommended per-object based on the file's platform metadata. For aligners:

```python
@router.get("/tools/{pipeline}/{object_id}")
async def tools_for_pipeline_for_object(pipeline: str, object_id: PydanticObjectId) -> dict:
    obj = await DataObject.get(object_id)
    platform = pipeline_service.sam_platform((obj.metadata or {}).get("platform")) if obj else "ILLUMINA"
    recommended = "minimap2" if platform != "ILLUMINA" else "bwa-mem2"
    
    tools = ...
    for t in tools:
        t["recommended"] = (t["name"] == recommended)
    
    return {"pipeline": pipeline, "tools": tools, "recommended": recommended}
```

Not needed for v1 — the plain list is sufficient.
