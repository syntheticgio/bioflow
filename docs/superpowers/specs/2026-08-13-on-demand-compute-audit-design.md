# On-demand compute pattern audit

Investigation and recommendation for
[#316](https://github.com/syntheticgio/bioflow/issues/316).

Six surfaces let a user compute per-file numbers on demand behind a
"Compute results" button, and five of them are near-identical copies. This
doc is the investigation the issue asked for: it confirms what is duplicated,
finds the correctness gap the duplication hides, decides whether the pattern
can be factored, and decides which of the six (if any) should compute at
ingest instead. It is a recommendation, not a refactor plan; the component
shape below is sketched to the point of proving the seam is real, and a
build spec follows this doc once the direction is agreed.

## Source and scope

Asked by the author on the annotation work, which added the fifth copy. The
`2026-08-12-annotation-results-design.md` spec already names this audit in
its follow-ups: the five instances "duplicate their empty state,
`NodeSelector`, and recompute button," and `AiSummary`'s `launchFn` prop "is
the one place the pattern was factored rather than copied." This doc is that
follow-up.

In scope: decide whether and how to factor the compute-on-demand surfaces,
and whether any of them should run at ingest. Out of scope: the refactor
itself, and any change to `AiSummary` (see D1).

## What exists today

Six surfaces. Five are copies of one pattern; one is the factored version of
a *different* pattern.

| Surface | Component | Launch | Status gate |
|---|---|---|---|
| FASTQ QC | `DetailPanel.tsx` | `api.launchQC(id, node)` | none in the button itself; gated by `activeJobs` polling |
| BAM stats | `BamResults.tsx` | `api.launchBamStats(id, node)` | `bam_stats_status === "ok"` |
| VCF stats | `VariantResults.tsx` | `api.launchVcfStats(id, node)` | `vcf_stats_status === "ok"` |
| Transcript QC | `TranscriptQc.tsx` | `api.launchTranscriptQc(id, gtfId)` | `transcript_qc_status === "ok"` |
| Annotation stats | `AnnotationResults.tsx` | `api.launchAnnotationStats(id, node)` | `annotation_stats_status === "ok"` |
| AI summary | `AiSummary.tsx` | `launchFn(objectId)` | provider availability + fingerprint staleness |

The five copies share the same ~40 lines: a `targetNode` state, a
`useMutation` whose `mutationFn` calls `launch*(id, targetNode || undefined)`
and whose `onSuccess` invalidates `["jobs"]` and toasts, a
`<type>_status === "ok"` branch between an empty state (`NodeSelector` +
note + "Compute results" button) and a results view, and a "recompute"
button wired to the same mutation.

The backend is already uniform and is not the problem. Every surface is the
same pipeline: a `launch_*` route (`backend/app/api/v1/pipelines.py:189`,
`:662`, `:677`, `:757`, `:932`), a `pipeline_service.launch_*` that enqueues
a job, a handler registered in a dispatch table, and an `_apply_*` in
`backend/app/queue/results.py` (dispatch dict at `:2618`) that merges the
handler's facts onto the object. The five appliers are textually the same
`obj.set({facts: {**obj.facts, **facts}})` merge. The duplication lives
entirely in the frontend.

## Finding 1: there are two seams, not one

`AiSummary` looks like a sixth instance of the same thing, but it is not.
The code states the distinction itself, in the comment on its `launchFn`
prop: *"Summaries run on the AI provider, never on a compute node, so there
is no target-node param."* The `#212` plan to give `AiSummary` a node
selector did not survive — correctly — because three things about it are
load-bearing different from the file-results surfaces:

- **Status is provider availability**, not a fact. It gates on
  `statusFn()` returning `available: true`, and self-suppresses entirely when
  there is nothing stored and nothing to generate from. The file surfaces
  gate on a `*_status` fact merged from a finished compute job.
- **No node targeting.** A summary is an AI call, not a worker-compute job.
- **Staleness is first-class.** It compares a server-computed fingerprint to
  the one stored with the summary and shows an "Out of date" badge. The file
  surfaces have no staleness concept; their result is simply present or not.

So the audit's subject is really **seam A — "compute results on demand"** —
the five file surfaces. `AiSummary` is seam B, already generalized for its
own (three summary) instances, and should be left alone. Forcing B into A, or
A into B, would be the mistake this doc exists to prevent.

## Finding 2: the correctness gap the duplication hides

`<type>_status` is **only ever written as `"ok"`, and only on success**
(`align_handlers.py:905`, `transcript_qc_handlers.py:141`,
`pipeline_handlers.py:448`). There is no `"running"` or `"failed"` value.

That makes an absent status fact ambiguous between "never ran" and "running
right now." The mutation's `isPending` flips off the moment the job is
*queued*, not when it finishes. So a user who clicks "Compute results," then
navigates away and back while the job runs, sees the "Compute results" button
again — and can queue a duplicate job against the same file.

Only `DetailPanel` guards against this, and it does so with an unrelated
mechanism: it polls `api.listJobs({ objectId, states: "active" })` and
computes `qcActive`/`trimActive` to disable the button. The four Results
surfaces have no such guard. This is the one defect in the pattern worth
fixing regardless of whether the dedup ever lands, and the shared component
below makes it the component's job rather than each surface's to remember.

## Finding 3: the six are on-demand for three different reasons

"On-demand" is doing three jobs at once, and naming which one applies to
which surface is the durable part of this audit:

| Surface | Why on-demand | Category |
|---|---|---|
| Transcript QC | needs a GTF the user has not picked; applicability is *inferred*, not a stored fact | deferred by **missing input** |
| Annotation stats | `#257` capped `_parse_tabular` at 5000 lines so ingestion stays bounded | deferred by **ingest bound** |
| BAM / VCF stats | `samtools depth` / `bcftools stats` scale with the data; a "small BAM" is the exception, not the rule | deferred by **cost** |
| FASTQ QC | cheap (sampled), but it is the *gate* for align and needs node targeting | deferred as a **step**, not a computation |

No surface cleanly qualifies for ingest-time compute today. The right outcome
is not "move X to ingest" but "record *why* each stays on-demand, so nobody
re-litigates it per file."

## Decisions

### D1 — factor seam A; leave `AiSummary` alone

One shared component owns the empty state, `NodeSelector`, compute button,
recompute affordance, and the in-flight guard for the five file surfaces.
`AiSummary` and the AI-summary surfaces are out of scope and must not change.

Rejected: one component for all six. It would have to smuggle
provider-availability gating and fingerprint staleness into a seam that has
neither, or else discard `AiSummary`'s self-suppression and staleness
guarantees — both of which its header comment names as the governing
constraint.

### D2 — the seam parameterizes on a launch thunk, not an `(objectId)` callback

The component's launch prop is `() => Promise<unknown>`, a thunk the caller
closes over. `TranscriptQc` needs to capture its selected GTF id (and, unlike
its siblings, takes `gtfObjectId` instead of `targetNode`), so an
`(objectId) => …` signature does not fit it. A thunk fits every caller and
moves nothing about the launch site's state out of the site.

### D3 — the in-flight guard is part of the seam

The component polls `api.listJobs({ objectId, states: "active" })` — the same
query `DetailPanel` already makes under the key `["jobs", "for-object", id]`,
so React Query deduplicates it across surfaces — and disables the compute
button while a job of that surface's type is active. This fixes the
double-submit defect with no backend change, which keeps the seam
frontend-only, matching Finding 1's conclusion that the backend is already
uniform.

Rejected for now: a `*_status: "running"` fact set by the handler on accept.
It is the better UI state (a running job is object state, not a polling
concern, and it survives the object being re-fetched by any reader) but
touches five handlers and five appliers, and "running" would also need a
timeout-to-failed transition that the absent/`"ok"` binary does not. Recorded
as a follow-up, not part of this change.

### D4 — keep all six on-demand

See Finding 3. Nothing moves to ingest. FASTQ QC is the one genuine candidate
for automation, but making it automatic is a policy change (importing 100
FASTQs would launch 100 QC jobs), so the near-term move is to keep it
explicit and make the align card's existing "Run QC" suggestion the canonical
one-click path. A per-project "auto-QC on import" setting is a real future
follow-up, not part of this audit.

### D5 — `launchTranscriptQc` should take `targetNode`

`launchTranscriptQc(objectId, gtfObjectId)` is the only launch without
`targetNode`, and transcript QC is a worker-compute job like the rest. This
is an inconsistency to fix, but it is a small, separable API/service/handler
change and is recorded as a follow-up so it does not widen this audit's
scope. The shared component does not depend on it: a surface with no node
selector simply passes `targetNode || undefined` like the others once the
param exists, or omits it until then.

## Recommended shape

A single component — name undecided; `OnDemandCompute` is a working name —
with this interface, enough to prove the seam holds:

```tsx
<OnDemandCompute
  hasResults={f.bam_stats_status === "ok"}
  launch={() => api.launchBamStats(obj.id, targetNode || undefined)}
  jobType="run_bam_stats"
  title="Coverage & per-contig detail"
  note="…"
  computeLabel="Compute results"
  recomputeLabel="Recompute results"
  preflight={<>
    <NodeSelector value={targetNode} onChange={setTargetNode} />
    {/* BamResults' coordinate-sort / index warning, TranscriptQc's GTF picker */}
  </>}
>
  {/* results view: charts, tables */}
</OnDemandCompute>
```

The component owns everything that was duplicated: the `useMutation` with its
notify and `["jobs"]` invalidation, the empty-state shell, the recompute
button, and the D3 in-flight guard. What genuinely differs between surfaces —
BamResults' sort/index warning, TranscriptQc's GTF selection and
applicability message, the `NodeSelector` itself — is a `preflight` slot or
`children`. Collapsing five ~40-line copies into one ~80-line component is
the whole change; nothing about any surface's launch target or results
rendering changes.

## Requirements

The following are the acceptance criteria for the recommended change. Each
names the actor and states one checkable obligation; the `R` ids are
permanent and will not be reused.

- **R1.** A user opening a Results tab for a file whose `<type>_status` fact
  is not `"ok"` sees an empty state naming what the compute produces, a
  target-node selector when more than one worker node is registered, and a
  compute button — and this empty state is rendered by one shared component
  for the BAM, VCF, transcript-QC, and annotation surfaces.
- **R2.** While a job of the surface's type is active for the object, the
  compute button is disabled, and it re-enables when the job leaves the
  active state, so navigating away and back cannot offer a duplicate launch.
- **R3.** Each surface launches the same job type it launches today; the
  refactor changes no launch target and no results rendering.
- **R4.** The transcript-QC surface renders its GTF picker and applicability
  message through the shared component without reimplementing the empty-state
  shell.
- **R5.** `AiSummary` and the AI-summary surfaces are byte-for-byte
  behaviorally unchanged by this refactor.
- **R6.** No surface changes its trigger from on-demand to ingest-time.

R1–R3 and R5–R6 are verifiable by hand in the browser; R4 by reading the
component. None of them requires a backend change.

## Follow-ups, deliberately out of scope

- **A `*_status: "running"` fact** with a timeout-to-failed transition,
  replacing the active-jobs polling guard with object state (D3, rejected
  alternative).
- **`targetNode` on `launchTranscriptQc`** (D5).
- **Auto-QC on import**, a per-project setting that runs FASTQ QC at ingest
  (D4).
- The build spec for `OnDemandCompute` itself, written once this direction is
  agreed.
