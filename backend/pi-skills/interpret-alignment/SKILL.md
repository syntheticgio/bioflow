---
name: interpret-alignment
---

# Interpret an Alignment

## When to Use

After `align_reads` (or `build_index`/`index_bam`), or when the user asks
about mapping quality, coverage, or whether an alignment is good enough for
the next step.

## Procedure

1. Read the alignment results: `bioflow_get_object(object_id)` on the
   alignment/BAM object and the `align_reads` job output.
2. Evaluate mapping rate first: a low fraction of mapped reads points at a
   reference mismatch (wrong organism, contaminated reference, or reads that
   are mostly something else), not at the aligner. High mapping with even
   coverage is the healthy state.
3. Evaluate coverage: how deep and how even. What "enough" means depends on
   the downstream goal — variant calling needs depth at the sites of
   interest; a low-depth sample changes which variants you can trust.
   Uneven coverage usually means repeats, GC bias, or multi-mapping reads.
4. Check index state: some downstream tools require a sorted and indexed
   BAM. If `index_bam` has not run, `bioflow_suggest_next` on the BAM will
   offer it.
5. Confirm the next step with `bioflow_suggest_next` on the alignment object
   — variant calling, assembly, or re-aligning with different parameters.

## Pitfalls

- Read the numbers from the object/job output; never guess mapping rate or
  coverage.
- "Low mapping" (reference problem) and "low coverage" (sequencing problem)
  are different failures with different fixes — name the right one.
- The reference the reads were aligned to matters: a BAM aligned to one
  genome cannot be quietly interpreted against another.
