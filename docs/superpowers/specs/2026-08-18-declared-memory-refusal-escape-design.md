# Declared-memory refusal and its escape hatch, across every heavy launcher

Date: 2026-08-18
Issue: [#527](https://github.com/syntheticgio/bioflow/issues/527)
Follows: #478 (declared-vs-budget refusal on `launch_alignment` / `launch_assembly`)

## Problem

#478 fixed one shape of bug: a job whose declared `mem_mb` exceeds the
admission budget is enqueued, never claimable, and waits forever with nothing
saying why. The fix was a launch-time refusal (`refuse_if_over_budget`) with a
"Launch anyway" escape (`resource_override`), applied to the two launchers that
already accepted an override parameter.

#527 records that other launchers have the same structural exposure and cannot
take the same fix, because they accept no override parameter — refusing them
without an escape would turn "waits forever, silently" into "cannot run at all,
ever", which is worse.

Investigation for this design found two things the issue did not know:

1. **The escape hatch #478 shipped does not reach the user.** The declared
   refusal raises `details={"declared_mb", "budget_mb"}`
   (`pipeline_service.refuse_if_over_budget`), but `AssembleDialog` gates its
   refusal card on `"estimate_mb" in e.details`, so the error falls through to
   `notify.error(e.message)` — a plain toast. The message ends "...or launch it
   anyway to run it on its own when the machine is idle", describing a button
   that never renders. Were the guard to pass, the card would still fault on
   `refusal.estimate_mb.toLocaleString()` and on a missing `replan`.
   Every unit test around this exercises the pure helper and asserts on the
   message string; none assert the details reach a card. This is the
   already-documented trap of fixtures shaped the way the code expects.

2. **The exposure is 14 launchers, not the 5 the issue lists.** A sweep of
   every `JobResources(mem_mb=...)` literal in `pipeline_service.py` found 26
   flat declarations, 14 of them above 2048 MB.

## Goals

- G1. A declared-memory refusal renders an actionable card with a working
  "Launch anyway" button, on every launch surface — including the nine
  affected launchers that have no settings dialog.
- G2. Every launcher declaring more than 2048 MB refuses at launch rather than
  enqueuing an unclaimable job.
- G3. A launcher added later above the threshold without an escape hatch fails
  a test, rather than silently reintroducing this gap a third time.

## Non-goals

- Replacing flat declarations with estimates. Whether `launch_polish` should
  estimate from input size is a separate question; this design only ensures a
  flat declaration cannot strand a job.
- Applying the refusal to launchers at or below 2048 MB (see D2).
- Changing the admission budget, the governor, or `claim.lua`.

## Decisions

### D1. Refusal details gain a `kind` discriminator

Two refusal paths emit incompatible `details`, and the frontend understands one:

| Path | Keys today |
|---|---|
| Estimate-based `BLOCK` | `estimate_mb`, `budget_mb`, `estimate_source`, `detail`, `replan` |
| Declared-vs-budget (#478) | `declared_mb`, `budget_mb` |

Both paths gain `"kind": "estimate" | "declared"`. The frontend switches on
`"kind" in e.details` rather than sniffing `estimate_mb`.

**Considered and rejected:** having the declared path synthesize an
`estimate_mb` so the frontend stays single-shape. Rejected because the two
refusals differ in a way the user must see: an estimate-based refusal carries a
replan (fewer threads moves the number), a declared refusal carries none —
nothing about the run alters a fixed reservation, which
`explain_declared_refusal` already tells the user in prose. Synthesizing an
estimate would make the card offer a replan that cannot work.

Existing `estimate_mb` keys stay. This is additive; no consumer breaks.

### D2. The threshold is 2048 MB

`MIN_DECLARED_MEM_MB` is 2048. A budget low enough to refuse a 2048 MB job is a
machine on which nothing in this app runs, so a check there fires only in a
configuration that is already unusable — dead code carrying maintenance cost.
The refusal applies to declarations strictly above 2048 MB.

### D3. The Actions grid owns a shared refusal card

Nine of the 14 affected launchers have no settings dialog; they launch from
Actions cards, which today share one
`onError: (e) => notify.error(e.message)`.

`PipelineSuggestions` renders `ResourceRefusalCard` inline for the refused
card. "Launch anyway" re-posts `{...card.launch.body, resource_override: true}`
through the existing `launchSuggestion(endpoint, body)`. Because the card
already carries its own endpoint and complete body, this component needs no
per-launcher knowledge — the property that already keeps it ignorant of the
launch request shapes.

## Scope: the 14 launchers

| `mem_mb` | Launcher | Request model | Surface |
|---|---|---|---|
| 16384 | `launch_annotate_genome` | `AnnotateGenomeRequest` | Actions |
| 16384 | `launch_polish` | `PolishRequest` | Actions |
| 16384 | `launch_continuity_qc` | `AssemblyContinuityRequest` | Actions |
| 12288 | `launch_qv_qc` | `AssemblyQvRequest` | Actions |
| 8192 | `launch_variant_calling` | `VariantRequest` | Dialog |
| 8192 | `launch_completeness` | `CompletenessRequest` | Dialog |
| 8192 | `launch_scaffold` | `ScaffoldRequest` | Dialog |
| 8192 | `launch_meryl_analysis` | `MerylAnalysisRequest` | Actions |
| 8192 | `launch_consensus` | `ConsensusRequest` | Actions |
| 8192 | `launch_misassembly_qc` | `MisassemblyQcRequest` | Actions |
| 8192 | `launch_synteny` | `SyntenyRequest` | Actions |
| 8192 | `launch_assembly_error_qc` | `AssemblyErrorRequest` | Actions |
| 4096 | `launch_quantify` | `QuantifyRequest` | Dialog |
| 4096 | `launch_differential_expression` | `DifferentialExpressionRequest` | Dialog |

Excluded (at or below threshold, per D2): `launch_trim`, `launch_qc`,
`launch_summary`, `launch_de_summary`, `launch_variant_summary`,
`launch_bam_stats`, `launch_transcript_qc`, `launch_vcf_stats`,
`launch_annotation_stats`, `launch_annotation_export`,
`launch_materialize_annotation_edits`, `launch_extract_genbank_sequence`,
`launch_annotation`, `launch_lineage_download`, `launch_gc_tracks`.

`launch_alignment`, `launch_assembly`, and `launch_build_index` already compute
a declared value and refuse; they are unchanged except for D1's `kind` key.

## Requirements

- **R1.** A launcher in the scope table, invoked when its declared `mem_mb`
  exceeds the admission budget and `resource_override` is false, raises
  `ValidationError` and enqueues no job.
  *Testable:* call the launcher with a patched budget; assert the raise and
  assert the queue is empty.
- **R2.** The same launcher with `resource_override=True` enqueues a job whose
  `resource_override` field is true.
- **R3.** A launcher whose declared `mem_mb` is at or below the budget enqueues
  normally, override or not.
- **R4.** Every refusal raised by either path carries a `kind` key whose value
  is `"estimate"` or `"declared"`.
- **R5.** A user who triggers a declared refusal from an Actions card sees a
  refusal card with a working "Launch anyway" control, not a toast.
- **R6.** A user who triggers a declared refusal from a settings dialog sees
  the same, including for `launch_assembly` (the #478 regression).
- **R7.** `ResourceRefusalCard` renders without fault when `replan` and
  `estimateMb` are absent.
- **R8.** Every launcher in `pipeline_service` declaring `mem_mb` strictly above `MIN_DECLARED_MEM_MB`
  accepts a `resource_override` parameter and calls `refuse_if_over_budget`.
  *Testable:* an exhaustiveness test asserting set equality between launchers
  found above the threshold and launchers accepting the override.
- **R9.** Every request model backing a scoped launcher exposes
  `resource_override`, defaulting false.

## Testing

Unit tests in the shape of `backend/tests/services/test_declared_budget_refusal.py`,
one refusal/override/under-budget triple per scoped launcher (R1–R3).

The exhaustiveness test (R8) is the durable part of this work. It follows the
repo's established registry-pair pattern (`NODE_TYPES`/`EXCLUDED_LAUNCHES` in
`test_node_types.py`): derive the launcher set by inspecting declarations
rather than hand-listing it, so a new heavy launcher fails the test instead of
silently regressing. Per that pattern's known failure mode, the completeness
test and any exclusion list must be run together, not one at a time.

Manual verification at `localhost:5273` via `./ops/worktree-up.sh`, against a
memory budget lowered in Settings to force refusals:

- an Actions-only launcher (`annotate-genome`) shows the card and launches on
  "Launch anyway" — R5;
- a dialog launcher (`assemble`) shows the card rather than a toast — R6, the
  #478 regression, which no unit test would have caught;
- the card renders with no replan block and does not fault — R7.

This is the step that matters most here: the defect this design opens with
passed a full green suite.

## Risks

- **Polish refusals have a silent downstream consequence.** Polishing corrects
  a draft assembly; a user who cannot run it keeps an unpolished assembly and
  may not know. The refusal message is generic and stays generic for
  consistency, but this is the one case where "cannot run" is not
  self-announcing. Accepted, recorded here rather than special-cased.
- **Threshold drift.** A launcher declaring exactly 2048 MB sits at the
  boundary by design (D2). If `MIN_DECLARED_MEM_MB` changes, the exhaustiveness
  test's threshold must move with it; it reads the constant rather than
  hardcoding 2048.
- **`kind` is additive but the frontend guard changes.** Any consumer still
  sniffing `estimate_mb` keeps working, since that key is unchanged.
