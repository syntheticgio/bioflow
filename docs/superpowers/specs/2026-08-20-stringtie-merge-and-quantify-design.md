# StringTie `--merge` and `-e` quantify-only — design

Date: 2026-08-20.

Covers [#703](https://github.com/syntheticgio/bioflow/issues/703)
(`stringtie --merge`) and [#704](https://github.com/syntheticgio/bioflow/issues/704)
(whether `stringtie -e` earns a third RNA-seq counting path). Both were split
out of [#622](https://github.com/syntheticgio/bioflow/issues/622).

**One document because #704 cannot be answered without #703.** #704's own
text anticipates this: *"If the answer to (1) is 'only in combination with
#703', this should be closed and folded into that issue."* The analysis below
concludes exactly that, so the two are specified together and the boundary
between them is drawn explicitly rather than left to whoever implements first.

## What exists today

Verified against this worktree on 2026-08-20:

- **#622 has landed.** `backend/app/pipelines/stringtie_runner.py` exists with
  `assemble_command(...)` and `parse_gtf(...)`. There is **no
  `merge_command`**.
- **`ObjectRole.ASSEMBLED_TRANSCRIPTS`** (`models/object.py:176`) exists — the
  role #622's per-sample GTFs carry, and the input this design consumes.
- **`PortSpec.multiple` exists and is deliberately unused.** Its comment
  (`node_types.py:68`) names DE's counts and the read set as the two ports
  whose launchers genuinely take lists today, and says both are *"left scalar
  here deliberately: each needs its own decision about how the per-sample
  design travels."*
- **`_launch_differential_expression`** (`node_types.py:292`) is the worked
  example of the params workaround: `"counts"` is wired as a *single
  representative* port while the real per-sample design and the contrast
  travel through `params` — because the dialog that drives the launcher
  already builds them that way.
- **The gene-universe pair is real and deliberate.**
  `salmon_runner.parse_tx2gene` raises rather than falling back to
  transcript-as-own-gene, its docstring naming the exact hazard: a counts file
  that *"merges cleanly, passes every downstream sanity check, and quietly
  tests a gene universe the user never chose"*.
  `counts_runner.attributes_for_format` picks `-t`/`-g` per annotation format,
  measured against real NCBI files (`locus_tag` at 100% vs `gene` at 84.5% on
  GCF_000146045.2), and prefers loud failure over counting nothing.

## Decision S1: N inputs travel through `params`; do not make `PortSpec.multiple` real here

#703's first question is whether this is the point to implement
`PortSpec.multiple`. **No.**

- The precedent is established and *documented as a considered choice*, not as
  debt: DE does exactly this, and `PortSpec.multiple`'s own comment says each
  N-input launcher needs its own decision about how the per-sample design
  travels. A merge's "design" is simply the set of GTFs — strictly less
  structure than DE's sample-to-condition mapping, which already fits in
  `params`.
- Making `multiple` real is a graph-model change touching every consumer of
  `PortSpec`, and it should be driven by a case that genuinely needs *wiring*
  semantics — an N-input node a user assembles by hand in the graph editor.
  A merge is not that case (see S2: it is a project-level action).
- Doing it here would couple a small, well-understood tool addition to an
  open-ended refactor of the graph model, and the refactor would be validated
  by exactly one consumer.

So: the `merge_transcripts` node wires one **representative**
`assembled_transcripts` port, and the real set travels as
`params["gtf_object_ids"]`, mirroring `_launch_differential_expression` and
citing it in a comment.

**This decision is #703's success criterion 3** ("the N-input representation
decision is written down, not implied by the implementation"), and it is
discharged here rather than in code.

## Decision S2: the merge is a project-level action, not a per-object card

#703 raises that "a merge is a project-level action rather than a per-object
one, which does not fit the current per-object card model well." Correct.

**Follow DE's surface, not the card model.** DE is already the project-level
precedent: it is driven by a dialog that selects across the project's counts
objects, because the question "which samples am I comparing" has no single
anchoring object. A merge asks the same shape of question about GTFs.

Practically: a project-level entry point that lists the project's
`ASSEMBLED_TRANSCRIPTS` objects with checkboxes, defaulting to all of them.
**Do not** put a "merge" card on each per-sample GTF — N cards for one
action, each needing the other N−1 objects, is the per-object model applied
where it does not fit.

## Decision S3: the merged annotation records all N inputs

`derived_from` = every input GTF, plus the reference annotation when `-G` was
passed. This is #703's success criterion 2, and it is the honest shape: the
merged annotation is a function of all of them, and a user deleting one input
should see the merged object as descending from it.

The merged object takes `ObjectRole.ANNOTATION`, **not**
`ASSEMBLED_TRANSCRIPTS`. It is a reference annotation now — the thing you
quantify *against* — and the role difference is what stops it being offered
back into another merge as though it were a per-sample assembly.

## Decision S4: `stringtie -e` is worth adding, but only as the merged
## annotation's quantifier — #704 folds into #703

#704 asks whether there is a case where `stringtie -e` beats both existing
paths, and names the honest candidate: quantifying against a *merged novel*
annotation. Working through it:

- **featureCounts** counts against a known annotation, gene-level. It *can*
  take the merged GTF — it takes any GTF. So the merged annotation is not
  automatically out of featureCounts' reach.
- **Salmon** quantifies against a transcriptome FASTA, alignment-free. A
  merged GTF is not a transcriptome FASTA; producing one requires extracting
  sequences (`gffread`-style), which the app does not do today.
- **`stringtie -e`** quantifies transcript-level against exactly the GTF that
  produced the assemblies, in the same tool and the same transcript-ID space.

So the distinct capability is **transcript-level abundance over novel
transcripts** — which featureCounts cannot give (it is gene-level) and Salmon
cannot reach (no transcriptome FASTA). That is a real gap, and it exists
*only* downstream of a merge.

**Therefore #704 should be closed and folded into #703**, as its own text
allows. The quantify step ships as the third stage of this design, not as a
standalone third counting path competing for the same card slot.

This also dissolves #704's central objection — three cards on one object with
no rule for choosing. There is no third card on a BAM. The quantify step is
offered on the **merged annotation**, an object that does not exist unless the
user deliberately built it, and whose whole purpose is to be quantified
against.

## Decision S5: the gene universe question, answered before anything emits
## `ObjectRole.COUNTS`

#704's third question is the one that must not be hand-waved, because the
failure is silent and reaches a DE result.

`stringtie -e` output is **transcript-level**, keyed by StringTie's transcript
IDs (`MSTRG.*` for novel transcripts, reference IDs where the merge matched
the reference). Rolling those up to genes uses the merged GTF's `gene_id`
attribute — which for novel loci is `MSTRG.*`, an identifier that exists in
*no other annotation*.

So: **it is a third gene universe**, and it is not reconcilable with the
featureCounts universe by construction — the novel loci have no counterpart
there.

The consequence, which this design takes as binding:

- The stage-3 output **must not be silently interchangeable** with
  featureCounts or Salmon counts in a DE comparison. Whether that is enforced
  by a distinct role, or by a fact recording the annotation object the counts
  were produced against and a DE-side check that all inputs share it, is an
  implementation choice — but **one of them must exist before the object gets
  `ObjectRole.COUNTS`**.
- The second option is the better default: it fixes the general problem (any
  two counts files from different annotations are incomparable, which is true
  of featureCounts against two different GFFs today) rather than special-casing
  StringTie. It should be checked against the existing DE path first, because
  if that check already exists, this is free.
- `parse_tx2gene`'s refusal to invent a gene for an unmappable transcript is
  the posture to copy: **raise, naming the transcript**, rather than falling
  back to transcript-as-own-gene.

## Staging

| Stage | Delivers | Closes |
|---|---|---|
| 1 | `merge_command` + merged-annotation object + node type + project-level entry point | #703 |
| 2 | `-e` quantification against a merged annotation, with S5's guard | #704 (folded) |

Stage 1 is independently useful: a non-redundant annotation is a legitimate
artifact — loadable in a browser, usable by featureCounts today.

## Components (stage 1)

- **`stringtie_runner.merge_command(*, stringtie_path, gtfs, out, reference_gtf=None, min_len=None, min_cov=None)`** — pure, unit-tested. `--merge` takes the
  GTF list positionally; `-G` supplies the reference annotation when one
  resolves.
- **A handler** taking `gtf_object_ids` from `params`, materializing each,
  writing the list file StringTie wants (if the version needs one rather than
  positional args — **verify**), running, ingesting.
- **`_apply_*`** — ingest as `ObjectRole.ANNOTATION` with `derived_from` per
  S3; facts recording input count, and the transcript/novel-transcript counts
  from `parse_gtf`.
- **`node_types`** — `merge_transcripts` spec with one representative port per
  S1, plus the `EXCLUDED_LAUNCHES` partition run as a whole class.
- **`running_now.ENDPOINT_JOB_TYPES`** and a **narrative verb** (it produces an
  object a person opens).

## Verify before implementing

1. **Does the installed StringTie's `--merge` take GTFs positionally or via a
   list file?** Both forms exist across versions; the command builder's shape
   depends on it.
2. **Does `-G` on merge behave as expected** with the reference annotations
   this app actually downloads (NCBI GTF *and* GFF3)? `attributes_for_format`
   exists because those two differ in ways that break tools.
3. **Does the DE path already check that its counts inputs share an
   annotation?** Per S5 — if it does, the guard is free; if not, that is the
   work.
4. **What `gene_id` does the merge emit for novel loci** on real data, to
   confirm the `MSTRG.*` reasoning in S5 rather than assuming it.

## Amended (spike answers, 2026-08-20)

Verified against the bundled StringTie **2.2.1** (`/usr/bin/stringtie`,
`stringtie --help` in the bioflow-backend image):

1. **Positional GTFs.** `stringtie --merge [Options] { gtf_list | strg1.gtf ... }`
   — the positional form is supported and is what the command builder uses.
   No list file is needed. `-o <out_gtf>`, `-G <guide_gff>`, `-m <min_len>`,
   `-c <min_cov>` are all `[Options]` and come before the positional GTFs.
2. **`-G` accepts both GTF and GFF3** — the merge help text reads
   `-G <guide_gff>   reference annotation to include in the merging (GTF/GFF3)`.
   GFF3 is a first-class input here, unlike featureCounts (`attributes_for_format`).
3. **The DE path does check that its counts inputs share an annotation** —
   `launch_differential_expression` refuses when the samples' `annotation_sha256`
   digests differ (`digests` set length > 1). That guard is the S5 precedent;
   stage 2 inherits it.
4. **`MSTRG` is the default merge label** — `-l <label>  name prefix for output
   transcripts (default: MSTRG)`. This confirms the S5 reasoning: novel loci in
   the merged GTF carry `MSTRG.*` gene/transcript ids that exist in no other
   annotation. (Confirmed from the help text; S5's exact gene_id emission on a
   real merged GTF is captured in stage 1's real-data run.)

## Amended (real-data run, 2026-08-20)

Ran `stringtie --merge -o merged.gtf a.gtf b.gtf` (StringTie 2.2.1, bundled
image) on two synthetic per-sample GTFs sharing one transcript locus and each
carrying one locus the other lacks. Sum of inputs = 4 transcripts; merged
output = **3**, confirming success criterion 1 (a non-redundant annotation
deduplicates shared loci). The merged `transcript` lines carry
`gene_id "MSTRG.1"; transcript_id "MSTRG.1.1"` for every locus — confirming
S-3/S5's reasoning that novel loci in the merged GTF are keyed by `MSTRG.*`,
an identifier that exists in no other annotation.

## Out of scope

- **Making `PortSpec.multiple` real.** S1 — a separate change driven by a case
  that needs wiring semantics.
- **Extracting a transcriptome FASTA from the merged GTF** so Salmon could
  quantify against it. A plausible alternative to stage 2, but a different
  tool (`gffread`) and its own issue.
- **`stringtie -B` Ballgown tables.** The abundance output is what feeds the
  counts model; Ballgown is a separate downstream ecosystem.
- **Cross-project merges.** Project-scoped, like DE.
