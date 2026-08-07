# RNA quantification and differential expression

Counting reads per gene for one sample, then comparing counts across
samples.

## Align against an annotated reference

Quantification runs on an aligned BAM, so reads are aligned first with
`align_reads` the same way any other alignment is -- against a reference
indexed with `build_index`. For RNA, that index is commonly built with
`star`, which is the aligner BioFlow's index step treats as annotation-aware:
give `build_index` a GTF alongside the reference and it builds a
splice-aware index for it.

## Quantify

`quantify` counts reads per gene for one aligned sample against a GTF
annotation, with `featurecounts`. One BAM per job, so a per-sample count is
its own object with its own provenance -- adding another sample later does
not require redoing the others.

## Differential expression

`differential_expression` compares quantified counts across samples, with
`pydeseq2`. It needs **multiple quantified samples in the same project** to
compare -- run `quantify` on each sample first. It also checks that the
samples' gene sets actually agree with each other rather than assuming it,
since `quantify` runs one BAM at a time and nothing else enforces that they
line up.

## Finding what applies

`bioflow_suggest_next` on a quantified-counts object is the way to tell
whether differential expression is actually runnable yet -- in particular
whether enough comparable samples exist in the project -- rather than
assuming it is as soon as one sample is quantified.
