# Tool selection recommendations, keyed on read chemistry

Written 2026-08-10 for GitHub issue [#109](https://github.com/syntheticgio/bioflow/issues/109).

## The problem

The tool picker shows every installed tool for a pipeline equally. A user who has never run QC before sees FastQC, fastp, and NanoPlot — three tools with different strengths, none of which is obviously the right one for their data.

**FastQC is designed for short reads.** It works on long-read files (it runs on any FASTQ), but its per-base quality model assumes uniform read lengths that don't exist in PacBio or Nanopore data — and NanoPlot exists specifically to fill that gap. The runtime already knows which tool to run: `pipeline_handlers.py` branches on `facts.qc_read_chemistry`. But the user never sees that knowledge before they pick a tool.

## What this spec does

Add a **recommendation matrix** to `TOOL_META` and surface it as a **"Recommended" badge** in the tool picker, keyed on the file's chemistry (short vs long reads). The picker already receives the object's chemistry for aligner warnings — we reuse that plumbing.

## What this spec does not do

- **Change which tools are available.** A recommended tool is still just advice; all installed tools remain selectable.
- **Gate Actions-tab cards on recommendations.** Cards continue to suggest their current default (fastp for trim, etc.). The card's "why" sentence could name the recommendation later, but that's a follow-up.
- **Recommend based on platform** (ILLUMINA vs ONT vs PACBIO). Chemistry is sparser in the data and already subsumes platform for tool choice: short reads are effectively Illumina; long reads are PacBio or Nanopore.

## The recommendation matrix

### Data model

```python
@dataclass(frozen=True)
class ToolMeta:
    # ... existing fields ...
    
    recommendations: dict[ReadChemistryBucket, RecommendationLevel] = field(
        default_factory=dict,
        repr=False,
    )
```

**Axis**: `ReadChemistryBucket` — coarse short/long split matching the runtime's own branching:
- `SHORT` — for `ReadChemistry.SHORT` (Illumina-style)
- `LONG` — for HIFI / CLR / ONT_SIMPLEX / ONT_DUPLEX
- `UNKNOWN` — when chemistry is not known; no recommendations shown

**Levels**: 
- `RECOMMENDED` — first choice for this read type
- `COMPATIBLE` — usable but not the primary recommendation
- (absent) — no opinion; tool may be irrelevant or untested

### Matrix per tool

| Tool | SHORT | LONG | Rationale |
|---|---|---|---|
| **fastp** (QC, TRIM) | RECOMMENDED | COMPATIBLE | Default trimmer for short reads; works on long but NanoPlot is better for QC |
| **fastqc** (QC) | RECOMMENDED | — | Publication-standard report; runtime doesn't call it on long reads anyway |
| **nanoplot** (QC) | — | RECOMMENDED | Long-read QC specialist; per-base model FastQC assumes doesn't apply |
| **cutadapt** (TRIM) | COMPATIBLE | RECOMMENDED | Works on any platform; recommended for long reads where fastp's adapter detection is less useful |
| **trimmomatic** (TRIM) | COMPATIBLE | — | Illumina-only tool; not recommended for long reads |

### Implementation notes

- The matrix lives in `TOOL_META` entries, computed server-side.
- The tools API (`/api/tools`) includes the effective recommendation level per tool given the object's chemistry (or a generic "no opinion" if chemistry is unknown).
- Frontend renders badges: "Recommended" (bright) for RECOMMENDED; nothing for COMPATIBLE or absent.

## Where it renders

**PipelineToolSelector rows**: A badge next to the tool name, using the existing `tool-row-badge` pattern (currently used for "not installed"). The picker already has chemistry via `object.facts.qc_read_chemistry`.

```
┌─────────────────────────────────────┐
│ fastp  v0.23.4                       │
│ All-in-one Illumina QC and trimming  │ [Recommended]
├─────────────────────────────────────┤
│ cutadapt  v4.9                       │
│ Flexible adapter, primer, barcode... │
├─────────────────────────────────────┤
│ trimmomatic  v0.39                   │
│ Classic sliding-window quality trimmer│
└─────────────────────────────────────┘
```

## Testing

- `test_every_tool_is_documented` — the new field is optional with a default, so no change needed.
- Manual verification: open tool picker for QC/trim on files with known chemistry; verify badges appear/disappear correctly.
- No backend tests required (the recommendation computation is trivial data lookup).

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Matrix goes stale when new tools added | `test_every_tool_is_documented` already catches missing TOOL_META entries; add a check that QC/trim tools have recommendations set. |
| Chemistry unknown → no recommendations shown | Correct behavior: don't recommend without data. Picker shows all tools equally. |
