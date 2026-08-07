# Read QC and trimming

Run QC on raw reads before doing anything else with them. It costs little
and tells you whether trimming is even warranted.

## QC first

`run_qc` measures read quality without changing anything -- it produces a
report and a set of facts on the object, not a new file. It picks its tool by
platform: short reads get `fastp` plus `fastqc`, long reads (Nanopore,
PacBio) get `nanoplot` instead, since a per-base quality model built for
uniform-length short reads is meaningless on reads spanning hundreds of bp to
tens of kb.

Read the report before deciding whether to trim -- adapter content, quality
dropoff, and duplication rate are the numbers that make that call.

## Trimming, if warranted

`trim_reads` adapter-trims and quality-filters. It defaults to `fastp`, and
can also dispatch to `cutadapt` or `trimmomatic`. Unlike `run_qc`, this
produces a **new object** -- the input file is untouched, and the trimmed
reads are a separate object with their own provenance back to the original.

## Paired reads

For a paired-end R1/R2 set, BioFlow matches the mate by filename convention
when one is not already recorded, so `trim_reads` (and later `align_reads`)
generally finds the right R2 without it being spelled out in every call.
`bioflow_suggest_next` on the R1 object is still the reliable way to confirm
what it found before launching.

## What's next

A trimmed read object is itself a fresh object -- run `bioflow_suggest_next`
on it rather than assuming trimming is the end of the chain. From here reads
typically head into `alignment-and-variants` or `de-novo-assembly`.
