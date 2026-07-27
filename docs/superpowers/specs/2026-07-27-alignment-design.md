# Aligning reads against a reference

Date: 2026-07-27
Status: Approved, ready for implementation planning

## Problem

Phase 6a produces trimmed reads and stops. Trimmed reads are not an end in
themselves — they exist to be aligned. The system can now prepare reads and can
hold reference genomes, but has no way to put the two together.

The obstacle is not the alignment itself, which is one command. It is that
alignment tools assume a filesystem this application deliberately does not
provide. BWA locates its index by appending suffixes to the reference path
(`genome.fna` → `genome.fna.bwt`, `.amb`, `.ann`, `.pac`, `.0123`); samtools
wants `genome.fna.fai` beside it. Content-addressed storage keeps every blob
alone under its hash with no extension and no siblings.

`parsers._has_index` has named this since Phase 3: *"indexes for managed content
will be generated and tracked as their own objects later, not discovered on
disk."* This is that later.

The same class of problem already bit us once. In Phase 6a fastp read a
compressed blob as plain text because it infers gzip from the filename, and the
command that caused it was perfectly well-formed — no unit test could have
caught it. Indexes are the harder version: they are also expensive to build, so
they must be persisted and reused rather than symlinked into place per run.

## Goal

Select trimmed (or raw) reads, choose a reference, and get back a
coordinate-sorted, indexed BAM with proper read groups — with the reference
index built once and reused thereafter, and every produced file tracked,
verified, and garbage-collected like anything else in the store.

## Decisions

Recorded with their reasoning, because the reasoning constrains the
implementation.

**Sidecars are a separate relationship from derived files.** A trimmed FASTQ is
biologically meaningful: a specimen you search, annotate, and align. A `.bwt` is
computationally necessary and biologically inert — it exists only to accompany
its reference and means nothing alone. Both are produced files in CAS, but
conflating them would flood the explorer with scaffolding and make "what came
from this sample" unanswerable. `sidecar_of` is the relationship; the test for
future artifacts is whether the file is a specimen or scaffolding.

**Index reuse is keyed by content, not by project.** A sidecar attaches to a
reference's `blob_sha256`, so the same genome registered in two projects shares
one index with no cross-project bookkeeping. This falls out of content
addressing rather than being designed in.

**bwa-mem2 over Debian's bwa.** bwa-mem2 is not packaged and needs a release
binary in the Dockerfile, and its index is roughly 5x larger. That multiplier
argued against it only under an assumption that turned out to be wrong: this
application's references are organisms like *T. brucei* (26 MB, 12 contigs), not
GRCh38. At that scale 5x of a small number is still small, and the ~2x speedup
is free. Indexes are built on demand and kept.

**minimap2 alongside it, deliberately.** Long-read support is worth having, but
the sharper reason is that minimap2's index is a *single* `.mmi` file while
bwa-mem2's is a five-file set. Supporting both from the start stops the sidecar
model from quietly hardcoding BWA's shape.

**Align and sort are one job; indexing is a follow-on.** `bwa-mem2 mem … |
samtools sort` never materializes the intermediate SAM, which is several times
the size of the resulting BAM and pure waste to write. Indexing is fast,
independently useful, and separable — so it chains via `parent_job_id`, which
has sat unused since Phase 0 marked it "pipeline DAG, Phase 6".

**A failing first stage must fail the job.** In a shell pipe the exit status is
the *last* command's, so `bwa | samtools sort` reports samtools' success even
when bwa died halfway. The result is a truncated BAM that looks fine. This needs
`pipefail` and a test that asserts it, because the failure mode is a silently
wrong result rather than an error.

**Read groups are required, not optional.** GATK and most variant callers refuse
to run without `@RG`, and adding it afterwards means rewriting the whole BAM.
The dialog requires sample, library, and platform, defaulted from the reads'
existing metadata (`sample_id` and `library_prep` are already in the schema), so
it is usually a confirmation rather than data entry.

**Indexing has two entry points and one implementation.** Aligning against an
unindexed reference queues `build_index` first and the alignment behind it; a
**Build index** button on any reference queues the same job eagerly. Same job
type, so there is no second code path to keep correct.

## Data model

On `DataObject`, alongside the existing `derived_from`:

```python
# The file this one accompanies. Distinct from derived_from: a sidecar is
# scaffolding for its parent, not a specimen in its own right.
sidecar_of: PydanticObjectId | None = None
# What kind of scaffolding: "bwa-mem2-index", "minimap2-index", "fai", "bai".
sidecar_role: str | None = None
```

Indexed on `sidecar_of` for the "does this reference have an index?" lookup,
matching the existing `by_derived_from` index.

`ObjectRole` gains `ALIGNMENT` for produced BAMs. Format alone cannot carry it:
a BAM from this pipeline and a BAM someone uploaded are the same format but
differ in whether their provenance is known.

**Deleting a reference deletes its sidecars.** Blob GC is refcount-driven, and a
sidecar's only reason to exist is its parent — nothing else will ever reference
it, so an orphaned index would sit at refcount 1 forever and never be collected.
Object deletion cascades to `sidecar_of` children, which is safe precisely
because sidecars are scaffolding: nothing is lost that cannot be rebuilt from
the reference. Deriving files (`derived_from`) deliberately do *not* cascade —
a trimmed FASTQ outlives its source, and deleting reads should not silently
destroy the alignments made from them.

## The materialized workdir

The load-bearing piece, and a generalization of the Phase 6a symlink fix rather
than a repeat of it.

```python
def materialize_reference(reference: DataObject, aligner: str) -> Path
```

Builds a scratch directory in which the reference and every sidecar appear under
the names the tool expects:

```
tmp/align/<job_id>/ref/genome.fna       → symlink to the reference blob
                      genome.fna.bwt    → symlink to a sidecar blob
                      genome.fna.amb    → …
                      genome.fna.fai    → …
```

Both `align_reads` and any future tool needing a reference go through it. The
Phase 6a lesson is that a well-formed command over a wrongly-named input fails
in a way no unit test catches, so the naming becomes a first-class concern with
its own tests rather than a per-handler afterthought.

## Jobs

| Job | Class | Resources | Produces |
|---|---|---|---|
| `build_index` | `compute` | cpu=4, io=heavy | Sidecar objects for one (reference, aligner) pair |
| `align_reads` | `compute` | cpu=threads, io=heavy | A coordinate-sorted BAM |
| `index_bam` | `compute` | cpu=1, io=light | A `.bai` sidecar |

`align_reads` declares the user's chosen thread count as its CPU demand, exactly
as `trim_reads` does. Note this is the declaration that made an existing queue
bug visible (recorded in TODO.md): `claim.lua` reserves the value but
`_free_resources` computes headroom from a job count, so a 16-thread alignment
and a single-CPU job look identical to admission.

All three are `max_attempts=2`, matching `trim_reads`: alignment failures are
almost always deterministic, and retrying a long run delays the error without
making it less likely.

**Chaining and deduplication.** An alignment against an unindexed reference
enqueues `build_index` and then `align_reads` delayed behind it. Both carry
dedup keys — `build_index` on (reference blob, aligner), `align_reads` on
(reads, mate, reference, parameters) — so a second request while an index is
already building joins the existing work rather than duplicating it. The
alignment waits on the index job's completion rather than polling for the
sidecar objects, so a failed index fails the alignment with a comprehensible
reason instead of leaving it queued forever.

Progress comes from the tools' own output through the existing `on_line`
mechanism — bwa-mem2 writes `[M::mem_process_seqs] Processed N reads` to stderr.
The same honesty constraint as trimming applies: the read total is an estimate
extrapolated at ingest, so the bar caps below complete.

## Launch parameters

Reference, plus:

- **Read group**: sample, library, platform (defaulted from reads metadata)
- **Aligner preset**: minimap2's `map-ont` / `map-pb` / `sr`, or BWA's mode. Not
  cosmetic — the wrong preset for long reads produces silently poor alignments
  rather than an error.
- **Threads and sort memory**: samtools sort spills to disk when it runs out,
  and the default is conservative.
- **Mark duplicates**: standard for DNA-seq variant calling, wrong for RNA-seq
  and amplicon data, so a real choice rather than a default.

## UI

Indexes are filtered from the explorer listing and surfaced on their reference
as *"Indexes: bwa-mem2 ✓, minimap2 — not built"* with a **Build index** action.
They remain real objects with real verification and GC; they simply do not bury
the files a user works with.

An **Align** button on ready FASTQ reads opens the dialog. The resulting BAM
gets a panel showing alignment rate, properly-paired percentage, duplicate rate,
and mean coverage — read from `samtools flagstat`, run as part of `index_bam`
since the file is already being traversed.

The BAM parser already extracts sort order, reference contigs and lengths, read
groups, samples, and the program chain, so a produced BAM verifies itself
against what was requested with no new parsing work.

## Not in scope

Variant calling. RNA-seq and splice-aware alignment, which want a GTF
annotation — an input type the system does not model. Any generic DAG engine:
real multi-step pipelines still do not exist, and `parent_job_id` chaining for
one follow-on step is enough to learn from before designing for more.
