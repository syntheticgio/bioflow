# Tool Selection Screen — Implementation Plan

> **Revised 2026-07-27.** The original version predated the alignment merge
> (`4e15413`) and the detail-panel tab split (`fc160b1`). Corrections: the CSS
> in §3.2 referenced seven custom properties that do not exist in this project
> and would have silently rendered as invisible borders on transparent
> backgrounds; `all_tools()` was described as returning two tools when it
> returns five; `TOOL_META` was defined here *and* in the QC plan with
> conflicting shapes; and the pipeline attribution field was singular in three
> places and plural in one.

## Problem

Clicking **Trim** or **Align** in the detail panel jumps straight to the
parameter screen. But:

- Align has two tools (bwa-mem2, minimap2) with genuinely different strengths —
  short-read vs long-read — and the choice sits inside the `advanced`
  collapsible (`AlignDialog.tsx:225-233`), which is where you put things people
  rarely change. This one is a primary decision.
- Trim has one tool today (fastp); cutadapt and trimmomatic are planned.
- Installed-but-unrunnable tools (bwa-mem2 on arm64) should be visible as
  disabled options with a reason, not discovered when a job fails.

**Goal:** a tool selection screen between the pipeline button and the parameter
screen — cards per tool, a short "what this excels at" summary, disabled cards
for unrunnable tools, and Continue into the existing parameter dialog with the
tool pre-selected.

---

## Dependency — read first

This plan **consumes** `TOOL_META`, `PipelineType`, and `all_tools_with_meta()`
as defined in **`pipeline-tool-additions-qc.md` Part 1.3**. It does not
redefine them. Phase 1 of that plan must land first, or there is no metadata to
render.

The attribution field is **`pipelines`, plural** (a list). fastp belongs to both
trim and QC; a singular field would drop it from one of them.

---

## 1. User Flow

```
Detail panel: click "Align"
  → ToolSelector modal (pipeline="align")
    ┌──────────────────────────────────────┐
    │  Select an aligner                   │
    │  ┌────────────────────────────────┐  │
    │  │ ◯ minimap2  v2.27              │  │
    │  │   Universal aligner. Works on  │  │
    │  │   all read lengths.            │  │
    │  │   • Long reads (ONT, PacBio)   │  │
    │  │   • Short reads via -x sr      │  │
    │  │   • Runs on all architectures  │  │
    │  └────────────────────────────────┘  │
    │  ┌────────────────────────────────┐  │
    │  │ ✓ bwa-mem2  v2.2.1   SELECTED  │  │
    │  │   Standard short-read aligner. │  │
    │  │   • Gold standard for Illumina │  │
    │  │   • Insert-size modeling       │  │
    │  └────────────────────────────────┘  │
    │              [ Cancel ] [ Continue ] │
    └──────────────────────────────────────┘
  → AlignDialog (aligner pre-selected)
```

Unavailable tool: card greyed, not clickable, showing the probe's `error`
string (e.g. "bwa-mem2 is compiled for x86 and cannot run on this host").

---

## 2. Backend

### 2.1 Metadata

Comes from `pipeline-tool-additions-qc.md` Part 1.3. Nothing to do here beyond
confirming that plan's Phase 1 has landed and `GET /pipelines/tools` returns
`pipelines`, `summary`, and `strengths` per tool.

### 2.2 Per-pipeline endpoint (optional)

The frontend can filter the full list client-side, which is the simpler start.
A dedicated endpoint is only worth adding if per-pipeline caching or a
per-object recommendation (§7) arrives later.

If added, filter on the **plural** field:

```python
@router.get("/tools/{pipeline}")
async def list_tools_for_pipeline(pipeline: str) -> dict:
    if pipeline not in {pt.value for pt in tools.PipelineType}:
        raise ValidationError(f"Unknown pipeline: {pipeline}")
    return {
        "pipeline": pipeline,
        "tools": [
            t for t in tools.all_tools_with_meta()
            if pipeline in t["pipelines"]   # membership, not equality
        ],
    }
```

### 2.3 Tool availability

Already flows through the API: `_probe` records an `error` string and
`Tool.available` is false when the binary is missing or unrunnable. The new UI
only presents it more visibly. No backend change.

---

## 3. Frontend

### 3.1 `PipelineToolSelector.tsx` (new)

```typescript
interface PipelineToolSelectorProps {
  pipeline: PipelineType;              // from types.ts, shared with the API
  onSelect: (toolName: string) => void;
  onClose: () => void;
}
```

Fetches `GET /pipelines/tools` via the existing client and filters on
`tool.pipelines.includes(pipeline)`.

Structure, reusing the existing `.modal-backdrop` / `.modal` / `.modal-actions`
classes (styles.css:843-884) rather than inventing a modal:

```
.modal-backdrop
  .modal.tool-selector          → wider than the 380px default
    h2                          → "Select an aligner"
    .tool-card[.selected][.disabled]
      .tool-card-header
        .tool-radio             → filled when selected
        .tool-name / .tool-version
        .chip                   → reuse existing chip class for badges
      .tool-card-summary
      ul.tool-card-strengths
      .tool-card-error          → probe error, disabled cards only
    .modal-actions
      button.btn "Cancel"
      button.btn.primary "Continue"   → disabled until a tool is selected
```

**Accessibility:** this is a radio group, not a list of buttons. Use
`role="radiogroup"` with `role="radio"` and `aria-checked` on the cards, arrow
keys moving selection, and `aria-disabled` on unavailable ones. `Tabs.tsx`
(added in `fc160b1`) is a working local example of roving-tabindex keyboard
handling to copy from.

### 3.2 Styles — append to `frontend/src/styles.css`

**Not a new file.** The project has one flat stylesheet; there is no
`frontend/src/styles/` directory.

The variables below are the ones this project actually defines (styles.css:1-36,
with a `prefers-color-scheme: light` block at :21). An earlier draft of this
plan used `--border-color`, `--surface-bg`, `--surface-hover`,
`--surface-selected`, `--text-muted`, `--text-secondary`, and `--bg-subtle` —
**none of which exist here.** Every rule using them would have fallen back to
initial values.

```css
/* ---------- Tool selector ---------- */
.modal.tool-selector {
  width: 520px;
}

.tool-card {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 14px;
  margin-bottom: 8px;
  cursor: pointer;
  background: var(--bg-elevated);
  transition: border-color 0.15s, background 0.15s;
}

.tool-card:hover:not(.disabled) {
  border-color: var(--accent);
  background: var(--bg-hover);
}

.tool-card.selected {
  border-color: var(--accent);
  background: var(--bg-hover);
}

.tool-card.disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.tool-card:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.tool-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.tool-radio {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid var(--border);
  flex-shrink: 0;
  display: grid;
  place-items: center;
}

.tool-card.selected .tool-radio {
  border-color: var(--accent);
}

.tool-radio-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
}

.tool-name {
  font-weight: 600;
  font-size: 14px;
}

.tool-version {
  font-size: 11px;
  color: var(--text-faint);
}

.tool-card-summary {
  font-size: 12px;
  line-height: 1.4;
  color: var(--text-dim);
  margin-bottom: 4px;
}

.tool-card-strengths {
  margin: 0;
  padding-left: 16px;
  font-size: 11px;
  color: var(--text-faint);
}

.tool-card-error {
  margin-top: 6px;
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 11px;
  /* Same treatment as .warn-box (styles.css:646) so an unavailable tool reads
     the same as every other warning in the app. */
  background: color-mix(in srgb, var(--warn) 12%, transparent);
  color: var(--warn);
}
```

Badges reuse the existing `.chip` class (styles.css:634). Accent-coloured
variants use `var(--success)` / `var(--accent)` / `var(--error)`, all of which
already invert correctly in light mode — unlike the hardcoded hex values
(`#e8f5e9`, `#1565c0`, `#c62828`) in the earlier draft, which would have been
unreadable there.

### 3.3 Detail panel wiring

The Trim and Align buttons live in the `panel-header` row of `ObjectDetail`
(`DetailPanel.tsx`), outside the tab strip, so they are reachable from any tab.
Today they set `trimOpen` / `alignOpen` directly. The selector inserts one step:

```typescript
type PipelineFlow = { pipeline: "trim" | "align"; tool: string | null } | null;
const [flow, setFlow] = useState<PipelineFlow>(null);
```

Clicking Trim sets `{pipeline: "trim", tool: null}`; the selector renders while
`tool` is null; choosing a tool sets it; the parameter dialog renders once
`tool` is non-null. Closing anywhere resets to `null`.

This replaces the six separate `useState` calls the earlier draft proposed
(`trimToolOpen`, `selectedTrimTool`, `trimDialogOpen`, and the align triplet) —
six booleans encoding one position in a two-step flow admits states like "both
dialogs open" that the flow does not have.

### 3.4 `TrimDialog.tsx`

Accept an optional `selectedTool?: string`. Show it in the title ("Trim reads —
fastp"). Until per-tool parameter models exist, only fastp reaches the
parameter screen, so the prop is display-only at first.

### 3.5 `AlignDialog.tsx`

**Re-read this file before editing.** It shipped after the original plan was
written; what follows was verified against the current version:

- The aligner `<select>` is at `AlignDialog.tsx:230-240`, inside
  `{advanced && (...)}` which starts at :225.
- `aligner` is read at :72 from `params`, and `alignerInfo` at :73 drives an
  availability warning at :122-124 and an index-existence check at :78-79.

Changes: accept `selectedTool?: AlignerName`; when provided, seed `params.aligner`
from it and remove the `<select>` from `advanced`; show the chosen aligner in
the title. Keep the availability warning and the index check — they key off
`aligner`, which still has a value.

```typescript
const effectiveAligner = selectedTool ?? defaults?.params?.aligner ?? "minimap2";
```

Note the minimap2-only preset control at :242 keys off `params.aligner` and
must keep working when the value arrives via the prop.

### 3.6 Types — `frontend/src/api/types.ts`

`PipelineType` and `PipelineTool` are defined in
`pipeline-tool-additions-qc.md` §1.5. Nothing additional needed here.

---

## 4. Open decisions — settle before building

Two questions the earlier draft raised and left as "consider". Both change the
component's shape, so they are not deferrable:

**Single-tool pipelines.** Trim has exactly one tool today. Always showing the
selector means a mandatory click on a screen with no decision on it.
**Recommendation:** when exactly one *available* tool matches the pipeline, skip
straight to the parameter dialog. The selector earns its place only when there
is a choice. This makes the Align flow gain a step and the Trim flow keep its
current shape.

**Changing your mind.** Without a Back control, a user who picks the wrong tool
must close the parameter dialog and restart. **Recommendation:** show the chosen
tool in the parameter dialog title as a button that returns to the selector.
Cheap, since `flow` (§3.3) already holds both steps — set `tool` back to `null`.

---

## 5. Implementation Order

| Step | Area | Description |
|---|---|---|
| 0 | — | **Prerequisite:** `pipeline-tool-additions-qc.md` Phase 1 has landed |
| 1 | Decide | Settle both questions in §4 |
| 2 | Frontend | `PipelineToolSelector.tsx` with radiogroup semantics |
| 3 | Frontend | Append tool-selector styles to `styles.css` |
| 4 | Frontend | `DetailPanel.tsx` — the `flow` state machine |
| 5 | Frontend | `TrimDialog.tsx` — `selectedTool` prop |
| 6 | Frontend | `AlignDialog.tsx` — re-read first, then prop + remove `<select>` |
| 7 | Backend | *Optional:* `GET /pipelines/tools/{pipeline}` |
| 8 | Test | Cards render, disabled tools unselectable, selection reaches params |

---

## 6. Edge Cases

| Scenario | Behavior |
|---|---|
| No tools installed for the pipeline | Empty state: "No trimming tools are installed. Install fastp or set FASTP_PATH." Continue disabled. |
| All matching tools unavailable | Cards shown greyed with their probe errors, so the reason is visible. Continue disabled. |
| Exactly one available tool | Skip the selector (§4). |
| Network error fetching tools | Error state with retry; Cancel closes. |
| Paired reads | Selector acts on the object; the mate is resolved at the parameter screen, unchanged. |
| Close without selecting | Returns to the panel, no dialog opens. |
| Tool disappears between selection and launch | The parameter dialog's existing availability check still fires; `tools.require()` on the backend is the real guard. |

---

## 7. Per-object recommendation (future)

The server could mark a recommended tool per object from the reads' platform —
`sam_platform()` and `suggested_preset()` (pipeline_service.py:292, :310)
already do this mapping for align defaults. That would mean minimap2 for
ONT/PacBio and bwa-mem2 for Illumina when available.

Deferred: it needs a per-object endpoint, and the plain list is enough for v1.
