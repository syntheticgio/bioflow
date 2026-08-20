# MetaBAT2 contig binning — design

Date: 2026-08-20.

Closes [#728](https://github.com/syntheticgio/bioflow/issues/728). Child 2 of
[#630](https://github.com/syntheticgio/bioflow/issues/630) and the core of that
epic; see the epic spec's decisions M2 and M3, which this document makes
concrete.

**Depends on [#727](https://github.com/syntheticgio/bioflow/issues/727)** for
something to bin.

## The stage that has no analog

A metagenome assembly is one FASTA with contigs from many organisms
interleaved. The unit a biologist wants — a MAG, one organism's draft genome —
does not exist until binning separates them. Every other stage of this app's
pipeline transforms one artifact into one artifact; **this one turns one
artifact into N**, where N is data-dependent.

## What exists today

Verified against this worktree on 2026-08-20:

- **`_apply_assemble_reads`** (`results.py:1547`) already ingests **two**
  objects from one job, contigs first and independently, with the comment that
  losing a six-hour assembly's FASTA because a secondary output tripped "would
  be indefensible". This is the posture to copy, generalized from two to N.
- **`ObjectRole.REFERENCE`** is what a de novo assembly's contigs get, so
  every downstream card (align, annotate, completeness, GC tracks) keys off it.
- **mosdepth landed** (#626) with `parse_summary` producing per-contig rows
  (`contigs`, `total`) and `build_report` writing them to `coverage_dir`.
- **`assembly_meta_mode`** will be on the contigs' facts once #727 lands — the
  honest gate for "this is a community assembly".
- **MetaBAT2 is absent** from the codebase.

## Decision B1: use MetaBAT2's own depth tool, not mosdepth's output

The tempting shortcut — mosdepth already computes per-contig depth (#626), so
feed it to MetaBAT2 — is wrong, and worth stating because the epic spec
gestured at it approvingly.

MetaBAT2 ships `jgi_summarize_bam_contig_depths`, which produces a depth file
carrying **mean depth *and* depth variance per contig**. The variance is not
decoration: MetaBAT2's binning uses coverage *co-variance across samples and
within a contig* as a signal alongside tetranucleotide composition. mosdepth's
per-contig summary carries mean depth, not the variance in the form MetaBAT2
expects.

Feeding it a hand-built file from mosdepth's numbers would produce a file
MetaBAT2 accepts and bins from — worse quality, no error, no way to tell. That
is the silent-degradation shape this repo keeps naming.

**So: run `jgi_summarize_bam_contig_depths` as part of the binning job.** It
comes with MetaBAT2, so it is not a separate tool registration.

*Consequence:* the binning job takes the **BAM**, not mosdepth's report, and
does not depend on #626 at all. The epic spec's claim that mosdepth supplies
the binning input is superseded by this decision.

## Decision B2: N bins, each `ObjectRole.REFERENCE`, ingested independently

Per the epic's M3, and made concrete:

Each bin FASTA is ingested as its own `DataObject` with
`role=ObjectRole.REFERENCE`, `derived_from=[contigs.id, bam.id]`, carrying the
assembly's `metadata` forward.

**Ingest each bin independently**, in `_apply_assemble_reads`' posture: a
failure on bin 12 must not lose bins 1–11 or 13–40. Log and continue; report
the count actually ingested rather than the count produced.

Facts per bin, so a bin is traceable without a container object:

- `bin_index` — which bin, as MetaBAT2 numbered it;
- `bin_source_assembly` — the contigs object id;
- `bin_contig_count`, `bin_total_bases`;
- `bin_mean_depth` — from the depth file, so bins are comparable at a glance.

## Decision B3: unbinned contigs become an object, not a discard

MetaBAT2 leaves contigs it cannot place. #728 says silently dropping them
"hides how much of the community was not recovered", and the resolution is:

**Write the unbinned contigs as one object** with `role=ObjectRole.REFERENCE`
and `bin_index = null` plus `bin_unbinned: true`, alongside a fact on the
*source assembly* recording the split: `binning_binned_bases`,
`binning_unbinned_bases`, `binning_bin_count`.

Why an object rather than only a fact: the unbinned fraction is frequently the
most interesting part of a metagenome — novel organisms with no close
relative, or a community too diverse to resolve at this depth. Making it a
first-class object means it can be re-assembled, re-binned at different
parameters, or classified (#730) like anything else. A number on a fact page
cannot be acted on.

The fraction matters more than the file, though, so **both** exist: the number
tells the user whether to care, the object lets them act.

## Decision B4: cap by count, refuse rather than truncate

#728 asks for a cap and what the UI does at that scale. Two different limits,
and conflating them is how this goes wrong:

- **A storage cap** on how many objects one click may create. A deep community
  can produce hundreds of bins; 400 new objects from one click is a usability
  failure even when every object is correct.
- **A signal cap** — there is no such thing. Every bin is a real result.

So the cap must **refuse, not truncate**. If MetaBAT2 produces more bins than
the cap allows, **fail the job with a message naming the count and the cap**,
rather than ingesting the first N. Truncating would silently discard MAGs with
no way to tell which — and the ones dropped would be ordered by MetaBAT2's
numbering, not by quality, so the discarded set is arbitrary.

A refusal is actionable: the user can raise `--minContig`, accept fewer bins,
or ask for the cap to be raised. A truncation is not.

Suggested cap: **200**, as a `Settings` value rather than a constant, so
raising it does not need a code change. The number is a guess about usability,
not about biology, and should be easy to change when it proves wrong.

## Decision B5: card on the assembly, gated on the meta fact

`build_binning_card(obj)` anchors on the **contigs** (the thing being split),
and is UNAVAILABLE with distinct reasons when:

- MetaBAT2 is not installed;
- the object is not a FASTA / `REFERENCE`;
- **no BAM of reads aligned back to this assembly resolves** — binning needs
  coverage, and without an alignment there is nothing to bin on;
- the assembly is not a metagenome assembly.

That last gate uses `assembly_meta_mode` from #727. It should be a **soft**
gate — a reason the card explains, not a hard refusal — because binning an
isolate assembly is unusual rather than wrong (a contaminated isolate is
exactly a case someone might want to bin). Offer it, and say plainly that this
assembly was not assembled in metagenome mode.

This is the `build_consensus_card` posture: *"Deliberately not gated on the
reference looking viral… That is the `protein.faa` mistake in a new costume —
right about the common case, wrong about a legitimate one."*

## Requirements

- **R1.** A user can bin a metagenome assembly for which an alignment of its
  own reads exists.
- **R2.** Binning produces N MAG objects, each usable by the existing align,
  annotate, and completeness cards without special-casing.
- **R3.** A failure ingesting one bin does not lose the others; the reported
  count is the count actually ingested.
- **R4.** The unbinned contigs are available as an object, and the
  binned/unbinned base split is visible on the source assembly.
- **R5.** A run producing more bins than the configured cap fails with a
  message naming both numbers; it never ingests a truncated subset.
- **R6.** The card explains each unavailability cause distinctly, and offers
  binning on a non-`--meta` assembly with an explanation rather than refusing.

## Testing

- **Depth file (B1)** — `jgi_summarize_bam_contig_depths` is invoked and its
  output is what reaches MetaBAT2. The failure this guards is silent, so assert
  the command shape rather than only the end-to-end result.
- **Partial ingest (R3)** — patch one bin's ingest to raise; assert the others
  land and the count reflects reality.
- **Cap (R5)** — a fixture with cap+1 bins fails, and **nothing is ingested**.
  Asserting only "the job failed" would pass an implementation that ingested
  200 objects and then failed.
- **Card, failing direction first** — each of B5's four causes, asserted on the
  message. Patch `spec_for`, not `tools.metabat2`.
- **Registry partitions** — whole `TestExhaustiveness` class and the
  provenance partition.
- **Real-data check** — a real metagenome assembly plus its alignment; confirm
  bins are usable by the completeness card, which is the point of B2's role
  choice and the thing a unit test cannot show.

## Verify before implementing

1. **MetaBAT2's aarch64 availability** — bioconda's `linux-aarch64` subdir
   before any GitHub release binary, per CLAUDE.md.
2. **License and citation**, from the project's own repository.
3. **Does `jgi_summarize_bam_contig_depths` ship in the same package** as
   `metabat2` on whichever channel is used? B1 assumes so; if not, the install
   needs both.
4. **Bin output naming and location** — `bins/bin.1.fa` style, and whether
   unbinned contigs are written at all by default or need `--unbinned`.
5. **Typical bin counts** on a real community, to sanity-check B4's cap of 200.

## Out of scope

- **Bin QC** — #729 (CheckM2). Without it a bin is untrustworthy, which is why
  that child follows immediately.
- **Taxonomic labelling** — #730.
- **Re-binning at multiple parameter sets** and comparing, or ensemble binning
  (DAS Tool). A later concern if MetaBAT2 alone proves insufficient.
- **Multi-sample co-binning**, where coverage across several samples is the
  strongest signal MetaBAT2 has. Genuinely valuable and genuinely a bigger
  change — it needs the N-input representation #703's spec discusses. Its own
  issue if wanted.
