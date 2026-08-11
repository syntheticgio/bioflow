# Node selector for non-dialog launch sites

Design for [#212](https://github.com/syntheticgio/bioflow/issues/212) — wire
the existing `NodeSelector` component into inline action buttons that PR #211
did not cover.

## Background

PR #211 added `NodeSelector` to every pipeline *dialog* (Align, Trim, Assemble,
etc.). The `NodeSelector` component (`frontend/src/components/NodeSelector.tsx`)
already exists, renders a dropdown of worker nodes, and returns `null` when
≤1 node is registered. All API client functions (`launchQC`, `launchBamStats`,
etc.) already accept an optional `targetNode?: string` parameter. The
remaining work is wiring it into 6 components.

## What changes

Every non-dialog launch site adds the same 3-line pattern:

```tsx
const [targetNode, setTargetNode] = useState("");
```

```tsx
<NodeSelector value={targetNode} onChange={setTargetNode} />
```

Place the `<NodeSelector>` **above** the launch button, in the same section.
Pass `targetNode || undefined` to the API call's `targetNode` parameter.

`NodeSelector` already returns `null` when fewer than 2 nodes are registered,
so no additional gating logic is needed.

| Component | Button(s) | Placement |
|---|---|---|
| **DetailPanel.tsx** | "Run QC" button in the Overview tab | Above the QC prompt section |
| **BamResults.tsx** | "Compute results" button | Above the section title |
| **VariantResults.tsx** | "Compute results" + "recompute results" | Above the section, one selector for both buttons |
| **AiSummary.tsx** | Summary generation (via `launchFn` prop) | Above the summary card |
| **ExpressionResults.tsx** | DE Summary (via `launchFn` prop) | Same pattern as AiSummary |
| **WorkflowCanvas.tsx** | `launchWorkflow` toolbar button | Above or beside the launch button |

## DetailPanel.tsx

Two QC launch sites in DetailPanel:

1. **Overview tab — "Run QC" button** (line ~786): add NodeSelector above.
   The `runQC` mutation already calls `api.launchQC(id)` — add `targetNode`.
   The second QC call (line ~861, the guided flow's `onRunQC`) calls the
   same `runQC.mutate()` via callback — no second selector needed.

2. **Actions tab**: already handled by `PipelineSuggestions` (PR #211).

## BamResults.tsx

One launch: "Compute results" at line ~84. Add NodeSelector above the
`<div className="section-title">` and pass `targetNode` to `api.launchBamStats`.

## VariantResults.tsx

Two launches: "Compute results" (line ~40, when no results yet) and
"recompute results" (line ~76, when results exist). One selector above the
section controls both — they share the same `useMutation` (`compute`), so
one `targetNode` state applies.

The VariantSummary launch (line ~104) uses `AiSummary`'s `launchFn` — that
goes through AiSummary's own selector (below).

## AiSummary.tsx

Accepts `launchFn: (objectId: string, targetNode?: string) => Promise<...>`.
The caller passes the function. Add `targetNode` state and NodeSelector above
the summary card, and pass `targetNode || undefined` as the second argument
to `launchFn`.

## ExpressionResults.tsx

Uses `AiSummary` with `launchFn` → DE Summary. Same treatment as VariantResults'
AiSummary usage. The parent's `launchFn` already accepts `targetNode` — just
pass it through.

## WorkflowCanvas.tsx

`launchWorkflow` at line ~407: `api.launchWorkflow(definitionId!, {...})`.
Add NodeSelector above or beside the launch button. This is likely in a
toolbar area, so the placement should not disrupt the canvas layout.

## Testing

No backend changes. No new components. All verification is manual in the
browser on a stack with ≥2 registered worker nodes — with one node the
selector is invisible, which is the correct default.

Verify at localhost:5273 via `./ops/worktree-up.sh`.
