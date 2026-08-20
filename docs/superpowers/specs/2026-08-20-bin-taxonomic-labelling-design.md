# Taxonomic labelling of metagenome bins — design

Date: 2026-08-20.

Closes [#730](https://github.com/syntheticgio/bioflow/issues/730). Child 4 of
[#630](https://github.com/syntheticgio/bioflow/issues/630); see the epic spec's
decision M5.

**Depends on [#728](https://github.com/syntheticgio/bioflow/issues/728)** —
needs bins to label.

## The first step, done

#730 said its size depends on one question: **is `classify_reads` bindable to a
contigs FASTA, or is it bound to FASTQ in a way that makes this more than a
parameter change?**

**It is bindable, and the binding is one condition.** Verified against this
worktree on 2026-08-20:

- **`build_kraken2_command`** (`kraken_runner.py:12`) is entirely
  format-agnostic. It emits `kraken2 --db --threads --report --output` plus
  `--gzip-compressed` when gzipped and `--paired` **only when a mate is
  present**. Nothing in it assumes FASTQ; Kraken2 itself accepts FASTA.
- **`launch_classify_reads`** (`pipeline_service.py:6315`) carries the entire
  restriction in one place:

  ```python
  if obj.format.kind is not FormatKind.FASTQ:
      raise ValidationError("Classification runs on FASTQ reads", ...)
  ```

- Everything else — the database registry, the download chaining, the memory
  declaration from `spec.mem_mb`, the report parsing, Bracken — is
  input-shape-independent.

So this child is the cheapest useful item in the epic, exactly as the epic
spec suspected. **No new tool, no new registry, no new handler.**

## Decision L1: widen the gate to FASTA, do not fork the path

Replace the FASTQ-only check with one accepting FASTQ **or** FASTA, and pass
`mate=None` for FASTA (a contigs file has no mate — the existing signature
already handles this, since `--paired` is conditional).

Why widen rather than add a parallel `classify_contigs` launch: a second path
would duplicate the database resolution, the download chaining with its
documented race handling, the budget refusal, and the memory declaration —
four pieces of non-obvious logic whose comments explain hard-won behaviour.
Two copies is how they drift, and the drift would be silent.

The one thing that genuinely differs is the **user-facing meaning**, not the
mechanism (L2).

## Decision L2: the meaning differs, so the wording must

Classifying reads answers *"what is in my sample"* — a composition question
over millions of independent observations.

Classifying a **bin's contigs** answers *"what is this organism"* — and the
expected answer is one dominant taxon. That difference matters in two places:

- **The card's title and description.** A card reading "Classify reads" on a
  bin is wrong. It is "Identify this bin", and its `why` should say that a
  clean bin classifies overwhelmingly to one taxon while a spread across
  distant taxa means the bin is mixed.
- **The stored facts.** A bin should carry a `bin_taxon_label` (the dominant
  taxon) and `bin_taxon_fraction` (how dominant), derived from the same report,
  because those are the two numbers that make a bin set readable at a glance.
  The full report stays available as today.

**The fraction is not decoration.** A bin labelled *Bacteroides* at 94% and one
labelled *Bacteroides* at 31% are very different results, and a label without
its fraction presents the second as though it were the first. Never render the
label alone.

## Decision L3: this is a hint, and must not be presented as identification

Kraken2 classifies against whichever database is loaded. The registry's own
`viral` entry is described as "blind to bacterial or human contamination", and
`standard-8` covers archaea, bacteria, viruses, plasmids and human — so an
organism outside the loaded database **cannot** be labelled correctly, and will
either go unclassified or be assigned to its nearest represented relative.

For metagenomics this is not an edge case: a novel MAG with no close relative
in the database is a common and interesting outcome, and it is exactly the case
where a confident-looking label is most misleading.

So the InfoMarker must say the label is **relative to the loaded database**,
and the unclassified fraction must be visible alongside the label. This is the
same honesty #641's blobplot InfoMarker owes about unlabelled clusters, and the
reason #630's epic keeps GTDB-Tk (a proper placement tool with a much larger
database) out of scope rather than pretending Kraken2 replaces it.

## Requirements

- **R1.** A user can classify a bin's contigs and see a dominant taxon label.
- **R2.** The label is always accompanied by the fraction supporting it.
- **R3.** The unclassified fraction is visible.
- **R4.** The card on a bin is worded for identification, not composition.
- **R5.** Classifying a FASTQ read set behaves exactly as it does today.
- **R6.** No new tool, handler, or database registry is introduced.

## Testing

- **R5 is the regression guard and the more important test.** The existing
  FASTQ path must be byte-identical: same command, same payload, same chaining.
  Widening a validation gate is exactly the kind of change that silently alters
  a neighbouring path.
- **The widened gate** — a FASTA object is accepted; a BAM or VCF is still
  refused with a message naming the format.
- **`mate=None` for FASTA** — the emitted command has no `--paired`.
- **Fact derivation** — dominant taxon and fraction from a fixture report,
  including the case where **no taxon exceeds a plausible dominance** (a
  thoroughly mixed bin), which must produce an honest "mixed" answer rather
  than confidently naming whichever taxon happened to lead at 12%.

## Verify before implementing

1. **Does Kraken2 want a flag for FASTA input** on the installed version, or
   does it detect the format? The command builder has no such flag today.
2. **Does the report parser assume read counts** in a way that misreads contig
   counts? The numbers are counts of sequences either way, but the *labels*
   ("reads") may need to change in the UI.
3. **Whether Bracken is meaningful on contigs.** It re-estimates *abundance*
   from a Kraken2 report, which is a read-level notion; on contigs it may be
   meaningless rather than merely different. If so, skip it for FASTA input
   rather than running it and presenting the output.

## Out of scope

- **GTDB-Tk placement.** The proper tool for MAG taxonomy, with a much larger
  database; out of scope for the whole epic (#630).
- **Auto-naming bin objects** from their label. Tempting, but a label that
  turns out wrong then persists in a name; the fact is enough.
- **Per-contig labels within a bin.** A useful contamination view, but it is
  the blobplot's job (#641) and a different presentation.
