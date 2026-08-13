# Build spec: `OnDemandCompute` component (#316)

Build spec for
[#316](https://github.com/syntheticgio/bioflow/issues/316), following the
[audit and recommendation doc](./2026-08-13-on-demand-compute-audit-design.md).

## Scope

Four Results-tab surfaces:

| Component | File | Job type | Status fact gate |
|---|---|---|---|
| `BamResults` | `BamResults.tsx` | `run_bam_stats` | `bam_stats_status === "ok"` |
| `VariantResults` | `VariantResults.tsx` | `run_vcf_stats` | `vcf_stats_status === "ok"` |
| `TranscriptQc` | `TranscriptQc.tsx` | `run_transcript_qc` | `transcript_qc_status === "ok"` |
| `AnnotationResults` | `AnnotationResults.tsx` | `run_annotation_stats` | `annotation_stats_status === "ok"` |

**Out of scope**: `DetailPanel.tsx`'s FASTQ QC — it is a different seam
(prompt, not results gate), already has its own in-flight guard (`qcActive` /
`trimActive`), and guards two job types (`run_qc` + `trim_reads`), not one.
Forcing it through this component would fight the existing code and add
complexity that the four file surfaces do not need. `AiSummary` is also out
of scope per D1 of the audit.

This corrects the audit's "five file surfaces" to four for the component,
aligning with the audit's own R1 recommendation which lists four (BAM, VCF,
transcript-QC, annotation).

## Interface

```tsx
type JobType =
  | "run_qc"
  | "run_bam_stats"
  | "run_transcript_qc"
  | "run_vcf_stats"
  | "run_annotation_stats";

type ComputeCtx = {
  /** The built-in recompute button, wired to the component's mutation and
   * guard.  Rendered inline: `<button style={{ …small accent… }} …/>`. */
  recomputeButton: ReactNode;
};

interface OnDemandComputeProps {
  /** Object id (for the `activeJobs` guard and launch). */
  objectId: string;
  /** Job type this surface launches; used to detect a matching in-flight job. */
  jobType: JobType;
  /** Launches the job. A thunk — the caller closes over targetNode / GTF id. */
  launch: () => Promise<unknown>;
  /** Toast message on successful enqueue. Default `"Computing results"`. */
  successMessage?: string;
  /** Whether the status-fact is present; gates empty state vs. results. */
  hasResults: boolean;
  /** Empty-state heading. */
  title: string;

  // -- empty-state layout ------------------------------------------------

  /** Rendered before the title: `NodeSelector`, etc. Optional. */
  preflight?: ReactNode;
  /** The note or warning block below the title. For a plain `section-note`
   * this is all that is needed — the component renders the compute-button
   * after it. */
  body?: ReactNode;
  /** Full control over everything below the title: note + button.
   * When provided `body` is ignored. TranscriptQc uses this to place its GTF
   * `<select>` and the compute button inline under its own conditional
   * logic.  Receives the component-built `computeButton` node so the
   * surface does not need to rebuild it. */
  renderBody?: (computeButton: ReactNode) => ReactNode;
  /** Compute button label. Default `"Compute results"`. */
  computeLabel?: string;
  /** Final button class: `"btn"` or `"btn primary"`. Default `"btn primary"`. */
  buttonClass?: string;
  /** Extra disable reason folded into the button's `disabled` alongside
   * the activeJobs guard (isRunning) and the mutation's isPending. */
  disabled?: boolean;

  // -- results-mode ------------------------------------------------------

  /** The results view, rendered when `hasResults` is true.
   *
   * Called with the built-in recompute-button node so each surface can place
   * it exactly where it sits today — top of `qc-provenance` (Variant,
   * Annotation), bottom of the Provenance `.section` (BAM), or not at all
   * (TranscriptQc). */
  children: (ctx: ComputeCtx) => ReactNode;
}
```

### What the component owns

1. **Mutation** (`useMutation`).  `mutationFn: launch`, `onSuccess` invalidates
   `["jobs"]` and calls `notify.info(successMessage)`, `onError` calls
   `notify.error`.

2. **In-flight guard** (the activeJobs poll).  Queries
   `["jobs", "for-object", objectId]` — the same key `DetailPanel` uses — at
   `refetchInterval: 5000`.  Returns `isRunning: boolean = (activeJobs ?? []).some(j => j.type === jobType)`.
   React Query deduplicates the shared key automatically.

3. **Empty-state shell**: `div.section` containing `preflight`,
   `div.section-title`, then either `body` + default button or `renderBody(button)`.

4. **Compute button** (default, or passed to `renderBody`):
   label = `isPending || isRunning ? "Computing…" : computeLabel`,
   `disabled = isPending || isRunning || disabled`.

5. **Recompute button** (built once, passed to `children` via `ctx`):
   small accent: `style={{ color: "var(--accent)", fontSize: 11, textTransform: "none", letterSpacing: 0 }}`,
   label = `isPending || isRunning ? "recomputing…" : "recompute results"`,
   `disabled = isPending || isRunning`.

### What the component does NOT own

- `NodeSelector` state (`targetNode`, `setTargetNode`) — that stays in the
  calling component, surfaced as the closure captured by `launch` and the node
  in `preflight`.
- The results-view DOM — that's `children`.
- The status-fact check (`hasResults`) — the caller evaluates the
  surface-specific fact expression and passes it as a boolean.
- Object invalidation on job completion — SSE (`useEvents`) already
  invalidates `["object"]` on every non-progress job event, so the object
  query refetches naturally when the compute job finishes.

## File plan

### New file

- `frontend/src/components/OnDemandCompute.tsx` — the component.

### Edited files

| File | Change |
|---|---|
| `BamResults.tsx` | Remove `useMutation` + `useQueryClient` import; replace empty-state block (lines 67–97) with `<OnDemandCompute>`; replace recompute button (lines 233–246) with `ctx.recomputeButton` placed via `children` function; remove `qc` / `compute` / `notify` / `NodeSelector` imports where now unused. |
| `VariantResults.tsx` | Same: `useMutation` + `useQueryClient` block → `<OnDemandCompute>`; recompute in `qc-provenance` → `ctx.recomputeButton` via `children`; clean imports. |
| `TranscriptQc.tsx` | Same: remove `useMutation` + `useQueryClient`; replace empty-state block (lines 64–105) and results block (lines 108–133) with `<OnDemandCompute>` using `renderBody` + `children`. |
| `AnnotationResults.tsx` | Same: `useMutation` + `useQueryClient` block → `<OnDemandCompute>`; recompute in `qc-provenance` → `ctx.recomputeButton` via `children`; clean imports. |

### Not edited

- `DetailPanel.tsx` — out of scope (different seam).
- `AiSummary.tsx` — out of scope per D1.
- Backend files — no changes needed (backend is already uniform).

## Migration: per-surface

### BamResults

Before:

```tsx
import { useMutation, useQueryClient } from "@tanstack/react-query";
// …
const qc = useQueryClient();
// …
const compute = useMutation({
  mutationFn: () => api.launchBamStats(obj.id, targetNode || undefined),
  onSuccess: () => {
    qc.invalidateQueries({ queryKey: ["jobs"] });
    notify.info("Computing results");
  },
  onError: (e: Error) => notify.error(e.message),
});
// …
{!hasResults && (
  <div className="section">
    <NodeSelector value={targetNode} onChange={setTargetNode} />
    <div className="section-title">Coverage &amp; per-contig detail</div>
    {/* three-way warn/note conditional */}
    <button className="btn" …>Compute results</button>
  </div>
)}
{hasResults && (
  <>
    {/* …all the charts and sections… */}
    <div className="section">
      <div className="section-title">Provenance</div>
      <dl className="kv">{/* provenance facts */}</dl>
      <button style={{…small accent…}} …>recompute results</button>
    </div>
  </>
)}
```

After:

```tsx
import { OnDemandCompute } from "./OnDemandCompute";
// imports for useMutation, useQueryClient, notify removed

const baseLauncher = useCallback(
  () => api.launchBamStats(obj.id, targetNode || undefined),
  [obj.id, targetNode],
);

return (
  <>
    <AlignmentReport facts={obj.facts} />
    {rnaApplies && (
      <TranscriptQc … />
    )}
    <OnDemandCompute
      objectId={obj.id}
      jobType="run_bam_stats"
      launch={baseLauncher}
      hasResults={hasResults}
      title="Coverage & per-contig detail"
      preflight={<NodeSelector value={targetNode} onChange={setTargetNode} />}
      body={
        !sortedCoordinate ? (
          <div className="warn-box">This BAM is not coordinate-sorted…</div>
        ) : !hasIndex ? (
          <div className="warn-box">This BAM has no index…</div>
        ) : (
          <div style={{ color: "var(--text-faint)", fontSize: 12, marginBottom: 8 }}>
            Coverage across the reference…
          </div>
        )
      }
      buttonClass="btn"
      disabled={!sortedCoordinate}
    >
      {({ recomputeButton }) => (
        <>
          {/* …all the charts and sections… */}
          <div className="section">
            <div className="section-title">Provenance</div>
            <dl className="kv">{/* provenance facts */}</dl>
            {recomputeButton}
          </div>
        </>
      )}
    </OnDemandCompute>
  </>
);
```

The `TranscriptQc` sub-call (lines 56–65 of original) stays exactly as-is —
the component handles `run_bam_stats` only; transcript QC is a separate
sub-component with its own `OnDemandCompute` instance.

Note: `launch` must be wrapped in `useCallback` (or an inline arrow) so it
doesn't cause infinite `useMutation` re-creation. The component memoizes the
mutationFn via its own useCallback internally (keyed on `objectId`); the
caller's obligation is only that the thunk reference is stable across renders
where args haven't changed.

### VariantResults

Before:

```tsx
const compute = useMutation({
  mutationFn: () => api.launchVcfStats(obj.id, targetNode || undefined),
  onSuccess: () => { qc.invalidateQueries({ queryKey: ["jobs"] }); notify.info("Computing results"); },
  onError: (e: Error) => notify.error(e.message),
});
// …
if (!hasResults) {
  return (
    <div className="section">
      <NodeSelector … />
      <div className="section-title">Variant summary</div>
      <div className="section-note">…</div>
      <button className="btn primary" …>Compute results</button>
    </div>
  );
}
return (
  <>
    <div className="qc-provenance">
      {[…].join(" · ")}{" "}
      <button style={{…small accent…}} …>recompute results</button>
    </div>
    {/* results */}
  </>
);
```

After:

```tsx
// useMutation, useQueryClient, notify imports removed
// compute removed; hasResults, targetNode, setTargetNode stay

return (
  <OnDemandCompute
    objectId={obj.id}
    jobType="run_vcf_stats"
    launch={useCallback(
      () => api.launchVcfStats(obj.id, targetNode || undefined),
      [obj.id, targetNode],
    )}
    hasResults={hasResults}
    title="Variant summary"
    preflight={<NodeSelector value={targetNode} onChange={setTargetNode} />}
    body={<div className="section-note">Call counts, Ti/Tv, …</div>}
  >
    {({ recomputeButton }) => (
      <>
        <div className="qc-provenance">
          {[vcf_info, called_by, samples].filter(Boolean).join(" · ")}{" "}
          {recomputeButton}
        </div>
        <AiSummary … />
        {/* …results sections… */}
      </>
    )}
  </OnDemandCompute>
);
```

### TranscriptQc

This is the most divergent surface: the compute button lives inside a
conditional (no-GTF → no button), the `<select>` and button are on one line,
there is no recompute, and `launch` captures a GTF id instead of a
`targetNode`.

Before:

```tsx
const compute = useMutation({
  mutationFn: () => api.launchTranscriptQc(obj.id, effectiveGtfId),
  onSuccess: () => { qc.invalidateQueries({ queryKey: ["jobs"] }); notify.info("Computing transcript QC"); },
  onError: (e: Error) => notify.error(e.message),
});
// …
if (!hasResults) { /* .section: title, then GTF conditional with select + button */ }
return ( /* gene body + feature distribution charts */ );
```

After — using `renderBody` for the GTF-conditional empty state:

```tsx
// useMutation, useQueryClient, notify imports removed

return (
  <OnDemandCompute
    objectId={obj.id}
    jobType="run_transcript_qc"
    launch={useCallback(
      () => api.launchTranscriptQc(obj.id, effectiveGtfId),
      [obj.id, effectiveGtfId],
    )}
    successMessage="Computing transcript QC"
    hasResults={hasResults}
    title="RNA-seq transcript QC"
    buttonClass="btn"
    computeLabel="Compute transcript QC"
    disabled={!effectiveGtfId}
    renderBody={(computeButton) =>
      gtfs.length === 0 ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          These charts need a gene annotation (GTF)…
        </div>
      ) : (
        <>
          <div style={{ color: "var(--text-faint)", fontSize: 12, marginBottom: 8 }}>
            Where reads fall within transcripts…
          </div>
          {gtfs.length > 1 && (
            <select value={effectiveGtfId} … style={{ marginRight: 8 }}>
              {gtfs.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}
            </select>
          )}
          {computeButton}
        </>
      )
    }
  >
    {() => (
      <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
        {geneBody && … <GeneBodyChart …/>}
        {featureDistribution && … <FeatureBar …/>}
      </div>
    )}
  </OnDemandCompute>
);
```

TranscriptQc has no recompute button — the `children` function receives
`recomputeButton` but ignores it. `renderBody` receives `computeButton` and
places it inline after the optional `<select>`.

### AnnotationResults

Same shape as VariantResults — `section-note` as `body`, button class
`"btn primary"` (default), recompute inside `qc-provenance` via
`ctx.recomputeButton`.

## In-flight guard detail

The component runs:

```tsx
const { data: activeJobs } = useQuery({
  queryKey: ["jobs", "for-object", objectId],
  queryFn: () => api.listJobs({ objectId, states: "active", limit: 20 }),
  refetchInterval: 5_000,
});
const isRunning = (activeJobs ?? []).some((j) => j.type === props.jobType);
```

This is the identical query `DetailPanel` makes at line 503 of
`DetailPanel.tsx`. React Query deduplicates it by key, so a DetailPanel with
both Run QC and a Results tab open runs one poll, not three.

The button's `disabled` is `isPending || isRunning || disabled`.  This is the
fix for the double-submit defect (Finding 2 of the audit): between enqueue and
completion, the button reads "Computing…" and stays disabled via `isRunning`,
not the ~1-second `isPending` window of the current code.

When the job completes, SSE broadcasts the terminal event → `useEvents`
invalidates `["object"]` → the object detail query refetches →
`hasResults` flips to true → the component renders `children`.

## Tests

### Component unit tests — new file

`frontend/src/components/OnDemandCompute.test.tsx`:

| Test | What it verifies |
|---|---|
| Renders empty state when `!hasResults` | Shows `title`, `body`, compute button |
| Empty state with `preflight` | NodeSelector (or any node) renders before title |
| Empty state with `renderBody` | `renderBody` output appears; `body` is ignored |
| Compute button fires `launch` on click | `launch` called once |
| Mocks `launch` rejection → calls `notify.error` | Toast appears with error text |
| Mocks `launch` success → invalidates `["jobs"]` + toasts | Both effects happen |
| Compute button disabled when `isPending` | `computeLabel` → `Computing…`, `disabled: true` |
| Compute button disabled when `isRunning` | Poll returns matching job type → `disabled: true` |
| Compute button disabled when `disabled` prop is true | e.g. `!sortedCoordinate` → `disabled: true` |
| Renders children when `hasResults` | Children output appears; empty state absent |
| Children receive `recomputeButton` | `ctx.recomputeButton` is a React element, not undefined |
| Recompute button fires `launch` on click | Same mutation reused |
| Custom `computeLabel` / `buttonClass` | Button text and class reflect props |
| Custom `successMessage` | `notify.info` called with custom message |

### Integration test (manual)

1. Open a BAM in the app → Results tab → "Compute results" → button reads
   "Computing…" and stays disabled until the job finishes → results appear
   automatically (SSE) → no manual refresh needed.
2. Click "Compute results", then navigate to another tab and back while
   the job runs → button still reads "Computing…" and is disabled (no
   double-submit).
3. Click "recompute results" on a BAM with existing results → same guard
   behavior.
4. Test same for a VCF, an annotation file, and an RNA-seq BAM (transcript QC)
   — each with distinct job types should not interfere.

## Follow-ups (recorded, not part of this change)

Per the audit doc D3 and D5:

- **D3**: A `*_status: "running"` fact set by the handler on accept, with a
  timeout-to-failed transition. This would make the in-flight state part of
  the object document rather than a frontend poll, and would survive the
  object being re-fetched by any reader. Touches five handlers and five
  appliers — separately scoped.
- **D5**: `launchTranscriptQc` should take `targetNode` to match the other
  four launches. Small, separable API/service/handler change.