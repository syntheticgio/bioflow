# Taxonomic labelling of metagenome bins — implementation plan

Date: 2026-08-20.

Closes [#730](https://github.com/syntheticgio/bioflow/issues/730). Companion to
`docs/superpowers/specs/2026-08-20-bin-taxonomic-labelling-design.md`
(decisions L1–L3, requirements R1–R6).

**Depends on [#728](https://github.com/syntheticgio/bioflow/issues/728)** —
needs bins to label.

**The spike is already done** (spec, "The first step, done"): `classify_reads`
is bindable to FASTA, and the restriction is a single condition at
`pipeline_service.py:6315`. `build_kraken2_command` is format-agnostic —
`--paired` is emitted only when a mate is present. So this is a small change,
and the plan is short because the work is.

## Files to touch

| File | Change |
|---|---|
| `backend/app/services/pipeline_service.py` (~6315) | Widen the gate: accept `FormatKind.FASTQ` **or** `FormatKind.FASTA`. Pass `mate=None` for FASTA. |
| `backend/app/pipelines/kraken_runner.py` | Only if S-1 says FASTA needs a flag. Otherwise untouched. |
| `backend/app/queue/kraken_handlers.py` | Derive `bin_taxon_label` / `bin_taxon_fraction` / unclassified fraction from the existing report (L2). |
| `backend/app/queue/results.py` | Merge those onto the object by per-key `facts.<key>` path (#606). |
| `backend/app/services/suggestion_service.py` | An identification-worded card for a bin (L2/R4). |
| `frontend/src/lib/metricInfo.ts` | Entries carrying L3's database caveat. |

## Spike

- **S-1. Does the installed Kraken2 need a flag for FASTA input**, or does it
  detect the format? The builder has none today.
- **S-2. Does the report parser assume read counts** in a way that misreads
  contig counts? The numbers are sequence counts either way, but UI labels
  saying "reads" would be wrong on a bin.
- **S-3. Is Bracken meaningful on contigs?** It re-estimates *abundance* from a
  Kraken2 report, which is a read-level notion. On contigs it may be
  meaningless rather than merely different — if so, **skip it for FASTA input**
  rather than running it and presenting output that looks like an answer.

## Ordered steps

1. **The FASTQ regression test, first.** Before touching the gate, pin the
   existing path: same command, same payload, same download chaining, for a
   FASTQ object. **This is the most important test in the change** (R5) —
   widening a validation gate is exactly the kind of edit that silently alters
   a neighbouring path, and the FASTQ path is the one users have today.
2. **Widen the gate** (L1). Accept FASTA, pass `mate=None`. **Do not fork a
   parallel `classify_contigs` launch**: it would duplicate the database
   resolution, the download chaining with its documented race handling, the
   budget refusal, and the memory declaration — four pieces of non-obvious
   logic whose comments record hard-won behaviour, and two copies is how they
   drift silently.
   Tests: FASTA accepted; BAM/VCF still refused with the format named; the
   FASTA command carries no `--paired`.
3. **Fact derivation** (L2/R2/R3). Dominant taxon, its fraction, and the
   unclassified fraction, from the report the handler already parses.
   **The test that matters is the mixed bin**: when no taxon reaches a
   plausible dominance, the result must be an honest "mixed" rather than
   confidently naming whichever taxon led at 12%. A test using only a clean
   94%-dominant fixture would pass an implementation that always names the
   top row.
   **Never store or render the label without its fraction** — *Bacteroides* at
   94% and at 31% are different results, and a bare label presents the second
   as the first.
4. **The card** (R4). Worded for identification, not composition: "Identify
   this bin", with a `why` saying a clean bin classifies overwhelmingly to one
   taxon while a spread across distant taxa means the bin is mixed. A card
   reading "Classify reads" on a bin is simply wrong.
5. **InfoMarker** (L3). The label is **relative to the loaded database**. The
   registry's own `viral` entry is "blind to bacterial or human contamination",
   and `standard-8` covers a fixed set — so a novel MAG with no close relative
   either goes unclassified or is assigned to its nearest represented relative.
   In metagenomics that is common and interesting, and it is exactly where a
   confident label misleads most. Show the unclassified fraction beside the
   label.
6. **Real-data check.** Label real bins from a #728 run. The falsifiable one:
   a bin CheckM2 (#729) scored as highly contaminated should classify across
   distant taxa, and a clean bin should not. If those two disagree, one of the
   two features is wired up wrong.

## Out of scope

Per the spec: GTDB-Tk placement, auto-naming bin objects from their label (a
wrong label would persist in a name; the fact is enough), and per-contig labels
within a bin (that is #641's blobplot).
