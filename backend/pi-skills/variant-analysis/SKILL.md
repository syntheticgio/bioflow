---
name: variant-analysis
---

# Find Variants vs a Reference Genome

## When to Use

The user has reads and wants to know the variants relative to an organism's
reference genome — "what's different between my reads and the reference".

## Procedure

1. Quality-gate the reads if not done: see the `run-qc` skill. QC decides
   whether trimming (`trim_reads`) is warranted before alignment.
2. Align: `build_index` on the genome, then `align_reads`, then `index_bam`
   — the general pattern is in the `drive-pipelines` skill. Check
   `bioflow_suggest_next` on the reads/genome for the ready-made payloads.
3. Call variants: `call_variants` (kind and payload from
   `bioflow://jobs/types` / `bioflow_suggest_next`).
4. Report the results: use `run_vcf_stats` and the variant job output to
   summarize the variants found.

## Pitfalls

- The reference must match the organism and be the same one the reads were
  aligned to; a variant call against the wrong reference is noise.
- Reads must pass QC before alignment; skipping it produces variant calls
  nobody can trust.
- `bioflow_run_pipeline` returns immediately — poll the job with
  `bioflow_get_job(job_id)` before reporting results.
