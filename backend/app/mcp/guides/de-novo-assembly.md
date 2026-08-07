# De novo assembly

Building a genome from reads with no reference, then optionally improving
it. Unlike alignment, this is not a strict pipeline everyone runs start to
finish -- each stage after the first is optional, consumes the previous
stage's output, and produces a new object of its own.

## Assemble

`assemble_reads` assembles long reads into contigs with no reference,
defaulting to `flye`. This is the one required step; everything below is
optional refinement.

## Polish (optional)

`polish_assembly` corrects residual base errors in a draft assembly using
short reads, with `polypolish`. It aligns the short reads to the draft
itself as part of the job (via `bwa-mem2`) rather than taking a pre-made
BAM, since Polypolish needs every location a read maps to rather than a
best-alignment BAM. Produces a new, polished assembly object.

## Scaffold (optional)

`scaffold_assembly` orders and orients a draft's contigs against a related
reference, with `ragtag`. This one needs a reference genome, not just
reads -- it is a statement about how the draft relates to that reference, not
purely about the sample. Also produces a new object.

## QC, at any point

Three independent, read-only checks -- none of them produce a new object,
they attach facts to the assembly you point them at:

- `assess_completeness` scores completeness against a lineage-specific
  ortholog set, with `compleasm`.
- `assess_misassemblies` is reference-based misassembly QC, with `quast`.
- `assess_assembly_errors` is reference-free error detection, with `craq`.

Any of these can run against the raw assembly, a polished one, or a
scaffolded one -- they are not tied to a particular stage.

## Finding what applies

Because polishing and scaffolding are optional and each needs different
inputs (short reads for one, a reference for the other),
`bioflow_suggest_next` on an assembly object is the reliable way to see
which of these actually apply to it right now, rather than assuming the
whole chain is always relevant.
