# Alignment and variant calling

A chain of three job types, each depending on the previous one's output.

## 1. Index the reference

A reference cannot be aligned against until it has an index for the aligner
you intend to use -- `build_index` builds one. BioFlow supports several
aligners (`minimap2`, `bwa-mem2`, `bowtie2`, `hisat2`, and `star` for
annotated RNA references), and the index is specific to whichever one you
pick.

`bioflow_suggest_next` on a reference object will say whether an index
already exists for a given aligner, rather than you having to track that
yourself -- prefer it over assuming.

## 2. Align

`align_reads` aligns a read file (or R1/R2 pair) against an indexed
reference and produces a coordinate-sorted BAM as a new object -- sorting is
done with `samtools` as part of the same job, piped straight from the
aligner so the unsorted intermediate is never written to disk. Indexing the
BAM itself is a separate follow-on step, `index_bam` -- it is fast and
independently useful (it also reports flagstat-style alignment statistics,
also via `samtools`), so it is not folded into the alignment job.

## 3. Call variants

`call_variants` runs on an aligned, indexed BAM against the same reference
(also needing the reference's own index/`.fai`). It dispatches to one of a
few callers depending on the payload -- `bcftools` by default, or `clair3` /
`deepvariant` for long-read or deep-learning-based calling.

## Checking progress

Every step here is a job: launch with `bioflow_run_pipeline`, poll with
`bioflow_get_job`. `bioflow_suggest_next` on the object at each stage is the
reliable way to see what is ready to run next rather than assuming the chain
above always applies verbatim -- installed tools and existing indexes vary
per reference.
