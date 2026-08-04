# DE and variant-call AI summaries

**Date:** 2026-08-03
**Status:** Approved, not yet implemented

## Problem

Two result pages give a scientist a table and a plot and nothing else.
`ExpressionResults.tsx` shows a differential-expression gene table, a
volcano/MA plot and a sample-PCA plot -- all real structured facts
(`significant_up`, `significant_down`, `sample_pca`, `contrast_test`) with no
prose interpreting them. `VariantResults.tsx` shows Ti/Tv, QUAL/depth
distributions and a filterable variant table, same gap. Both are the same
shape of problem the existing `FILE_SUMMARY` slot already solves for QC
reports: numbers a person has to read and judge, with no narrative pulling
out what matters.

## What ships

Two new `TaskSlot` members, `DE_SUMMARY` and `VARIANT_SUMMARY`, each
independently routable from the settings page like every other slot. Each
gets its own THREAD-mode queue handler and fact-selection module, built on
the same machinery `FILE_SUMMARY` already uses -- `summarize_object` /
`summary_prompt.py` are the worked example for all of it. No new job class,
no new queue mechanics, no new frontend data-fetching pattern: both render
through the existing `AiSummary` component.

### DE summary

Triggers automatically when the `run_deseq2` job that produces `DeFacts`
completes -- the same point `summarize_object` is queued from today for a
QC job. Prompt input:

- Aggregate facts already in `DeFacts`: `significant_up`, `significant_down`,
  `samples_by_condition`, `contrast_test` / `contrast_reference`, `alpha`,
  and a qualitative read of `sample_pca` (clean separation by condition vs.
  not, without naming exact coordinates).
- The top 20 genes by `padj`, each as name + log2FC + padj, rendered as
  plain lines in the same "measurement in words, not a raw key" style as
  today's QC sections. A gene with no symbol renders as a generic
  descriptor ("an unnamed transcript, ranked Nth by significance") rather
  than being silently dropped -- same convention as `build_user_prompt`'s
  "Organism: not recorded -- do not guess" line: tell the model what's
  missing instead of leaving a gap it might fill in.

Renders in `ExpressionResults.tsx`, above the PCA/volcano/MA plots -- prose
first, then the numbers it refers to, matching where `AiSummary` sits on
file detail pages today.

### Variant summary

Triggers after the on-demand `run_vcf_stats` job (the "Compute results"
button in `VariantResults.tsx`) completes, not after the original
variant-calling job. This matters because `VariantResults.tsx` reads
`VcfStatsFacts` -- Ti/Tv, QUAL/depth distributions, consequence breakdown --
and none of that exists until `run_vcf_stats` has run; the coarser facts
available right after
variant calling (sample/contig counts, filter values) aren't what this
page shows. Prompt input:

- The aggregate `VcfStatsFacts` fields `VariantResults.tsx` already
  displays: call count, Ti/Tv ratio, QUAL/depth distribution shape, filter
  values in use, consequence-type breakdown.
- The top N variants by consequence severity (stop-gained / frameshift
  first, then missense, etc.), each as gene symbol + position + consequence
  type. A variant with no gene annotation renders as a generic descriptor
  ("an intergenic variant on chr7") rather than being omitted, same
  fallback rule as the DE side.

Renders in `VariantResults.tsx`, above the Ti/Tv/QUAL charts and the variant
table -- same "prose first" placement as the DE summary, not the generic
per-object facts panel `AiSummary` occupies for other file types. (A
dedicated variant-browsing page already exists here -- `VariantResults.tsx`
+ `VariantTable.tsx` + `VariantCharts.tsx`, structurally the sibling of
`ExpressionResults.tsx` -- so this is not new UI surface, just a new prose
block at the top of an existing one.)

### System prompts

Both get their own `SYSTEM_PROMPT`, following the same rule shape as
`FILE_SUMMARY`'s and `ORGANISM_BLURB`'s:

- Only restate numbers given in the prompt. Never infer significance,
  pathogenicity, or causality beyond what the facts state.
- No software, parameter, or filtering recommendations.
- If nothing in the results stands out, say so plainly and briefly --
  don't manufacture a finding to fill space.
- Cite only the few genes/variants that carry the point; don't recite the
  full top-N list back as prose.

### Error handling

Identical to `FILE_SUMMARY` today:

- No provider configured for the slot -> job returns `skipped: no_provider`,
  no summary rendered, no error surfaced.
- Insufficient facts to say anything (e.g. a DE run with zero significant
  genes and a PCA that shows nothing notable, or a VCF with too few
  variants to characterize) -> the prompt builder returns `None`, the job
  skips rather than asking the model to invent a narrative from nothing.

### Testing

- Unit tests for the two new prompt-builder functions, mirroring
  `test_summary_prompt.py`'s pattern: given a facts dict, assert on the
  prompt's sections and lines, including the missing-gene/missing-annotation
  fallback text.
- Handler tests mirroring the existing `summary_handlers` tests for the
  skip-on-no-provider and skip-on-insufficient-facts paths.
- No new integration surface beyond what `summarize_object` already
  exercises -- both new handlers are the same THREAD-mode job shape.

## Out of scope

- A `DE_SUMMARY`/`VARIANT_SUMMARY`-specific settings UI beyond the existing
  per-slot row the settings page already renders for every `TaskSlot`.
- Any change to how `run_deseq2` or `run_vcf_stats` compute their facts --
  both new prompt modules read facts that already exist.
- The failure-reason explainer and project-level Q&A ideas raised in the
  same brainstorm -- different data sources and reliability profiles,
  deferred to their own specs.
