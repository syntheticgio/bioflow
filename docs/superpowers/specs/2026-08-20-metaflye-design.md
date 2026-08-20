# metaFlye (`--meta`) — design

Date: 2026-08-20.

Closes [#727](https://github.com/syntheticgio/bioflow/issues/727). Child 1 of
[#630](https://github.com/syntheticgio/bioflow/issues/630); see
`docs/superpowers/specs/2026-08-20-metagenomics-support-design.md`, decision M1.

The cheapest real capability in the metagenomics epic: Flye is already
installed, probed, registered, and resource-modelled. What is missing is one
flag.

## What exists today

Verified against this worktree on 2026-08-20:

- **`_flye_command`** (`assembly_runner.py:89`) emits
  `flye --<mode> <reads> --out-dir <d> --threads N --iterations N`. `--meta` is
  an **additional** flag in Flye's CLI, not a replacement for `--nano-raw` and
  friends — the two are orthogonal, so this is an append, not a branch.
- **`FLYE_SPEC.fields`** carries `mode` (a `select` over six accuracy choices)
  and `iterations`. No `meta`.
- **`FlyeParams`** has `mode`, `threads`, `iterations`, `genome_size`.
- **`genome_size` is already optional and already tracked.**
  `assembly_params` records `genome_size: int | None` plus
  `genome_size_source: "unset" | "user" | "inferred"`, and its module docstring
  notes the field "is not passed to Flye at all in the default case (Flye has
  not required it since 2.8) and exists here for BioFlow's own memory
  estimate."
- **`resource_estimator.estimate_mb`** returns **`None`** when
  `genome_bases is None or <= 0`, and its docstring is explicit that callers
  "must treat None as 'no opinion' and let the run proceed -- refusing to start
  because we could not guess would be worse than starting and failing, which
  at least produces a log."

## Decision F1: the memory question is already answered by the existing design

#630's spec flagged this as the thing needing thought: `bytes_per_genome_base
= 40.0` assumes one genome, and "genome size" has no meaning for a community.
That worry is real about the *model* but not about the *system*, because the
estimator already has a well-designed unknown case.

**Do not invent a community memory model.** For a `--meta` run, treat genome
size as unknown unless the user supplies one:

- `genome_size` unset → `estimate_mb` returns `None` → no opinion, run
  proceeds. This is the existing, documented, deliberate path for
  "a project that cannot supply a genome size", which the estimator's own
  docstring calls "the normal case rather than a misconfigured one".
- `genome_size` set by the user → the existing model applies to whatever they
  meant by it. Their number, their interpretation.

**What must not happen** is silently reusing an *inferred* single-genome size
for a `--meta` run. If `genome_size_source == "inferred"`, that inference was
made on single-organism assumptions and is not merely imprecise for a
community — it is meaningless, and feeding it to a 40-bytes-per-base model
produces a confidently wrong number. A wrong-low estimate is an OOM, and an
OOM-killed run also poisons the timing models
(CLAUDE.md, "Querying computation records").

So: **when `meta` is on and `genome_size_source` is `"inferred"`, drop the
inferred value and estimate as unknown.** A user-supplied size is kept; an
inferred one is discarded with the reason recorded in facts.

*Rejected:* a `bytes_per_genome_base` tuned for metagenomes. There is no
defensible coefficient — peak residency depends on community complexity, not
on a number anyone has. Inventing one dresses a guess as a model.

## Decision F2: `meta` is a checkbox, defaulting off, orthogonal to `mode`

A `ParamField` of kind `bool` (or the registry's equivalent), `default=False`,
in the `biology` group beside `mode`.

It is **not** a seventh `mode` choice. `mode` selects read accuracy
(`--nano-hq`, `--pacbio-hifi`, …) and `--meta` selects assembly strategy; a
metagenome still has a read chemistry. Folding `meta` into the `mode` select
would make the two mutually exclusive in the UI when they are orthogonal in
the tool, and would multiply the choice list by two.

Help text must say what it changes, not restate the flag: metaFlye handles
uneven coverage and does not assume one organism, at some cost in contiguity
for a true single-isolate sample — which is why it is opt-in rather than
inferred.

**Do not try to infer it.** Nothing in a FASTQ says "this is a community", and
guessing wrong in either direction degrades the assembly silently. That is the
`protein.faa` mistake this repo keeps naming.

## Decision F3: the output stays an assembly; nothing downstream changes

A `--meta` run produces the same three outputs (`assembly.fasta`,
`assembly_graph.gfa`, `assembly_info.txt`) and ingests the same way, with
contigs taking `ObjectRole.REFERENCE` exactly as today.

The contigs *are* a mixture, but that is a fact about the biology, not a
different artifact type. Everything downstream — align reads back, QC, and
eventually bin (#728) — works on it unchanged. Introducing a distinct role
here would fork every consumer for no gain, and #630's M3 already settled that
a bin is a `REFERENCE` too.

**Record it in facts.** `assembly_meta_mode: true` on the contigs, so a later
reader (and #728's binning card) can tell a community assembly from an isolate
one without guessing from the name. This is also what makes the binning card's
gate honest rather than offering to bin any assembly at all.

## Requirements

- **R1.** A user launching a Flye assembly can turn on metagenome mode from the
  assemble dialog; it is off by default.
- **R2.** With it on, the command carries `--meta` alongside the chemistry
  mode, and both are present.
- **R3.** With it off, the emitted command is byte-identical to today's.
- **R4.** With it on and `genome_size_source == "inferred"`, the memory
  estimate is computed as unknown (`None`), not from the inferred size, and
  the reason is recorded.
- **R5.** A user-supplied `genome_size` is honoured in metagenome mode.
- **R6.** The resulting contigs carry `assembly_meta_mode` in their facts.

## Testing

- **R2/R3** — command-builder unit tests. R3 is the regression guard and the
  more important of the two: an exact-argv assertion for the default case, so
  a refactor that reorders or drops a flag fails loudly.
- **R4** — the load-bearing test. Assert that an *inferred* genome size does
  not reach the estimator when `meta` is on, and that a *user* one does (R5).
  Testing only "meta on → estimate is None" would pass against an
  implementation that ignores user sizes too, which breaks R5.
- **R6** — the fact is present after a meta run and absent otherwise.
- No new registries: Flye is already in all of them, which is the point of
  this being child 1.

## Verify before implementing

1. **Does the installed Flye accept `--meta` together with every `mode`
   choice** the registry offers? `--meta` with `--pacbio-corr`, for instance,
   may or may not be a supported combination on this version. If some pairs
   are rejected, the dialog needs to say so rather than letting the job fail
   several minutes in.
2. **Does `--meta` change the output filenames?** F3 assumes not; confirm
   against a real run before relying on `assembly.fasta`.
3. **Peak RSS on a real community sample** versus the single-genome estimate,
   to know how wrong the model would have been — worth recording in the issue
   even though F1 means it is not used for gating.

## Out of scope

- **A metagenome memory model** (F1). No defensible coefficient exists.
- **Inferring metagenome mode from the reads** (F2).
- **Binning the result** — that is #728, and it is what makes this child
  useful rather than merely possible.
- **MEGAHIT / SPAdes `--meta`** for short reads — #731.
