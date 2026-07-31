# Chromosome names on the strip

Date: 2026-07-31

## Problem

The chromosome strip labels bars with accession digits -- `1136`, `1147`, `1224`
-- because nothing in `facts` knows that `NC_001136.10` is chromosome IV and
`NC_001224.1` is the mitochondrion. The accession tail was the strongest honest
claim available with local data only.

NCBI publishes the names. Fetching them makes the strip readable at a glance,
and the useful labels turn out to be more than roman numerals: `MT` for
organelles, `chr11-scaffold01` for unplaced scaffolds, plasmid names on
bacterial assemblies.

## The data source

`GET https://api.ncbi.nlm.nih.gov/datasets/v2alpha/genome/accession/{acc}/sequence_reports`

Same host and same API version the ingest already calls for `dataset_report`,
so it reuses `_get` from `app.metadata.sra` (throttling, retry, never-raises).

Verified live, not assumed:

| Assembly | Records | Payload | Time |
|---|---|---|---|
| `GCF_000146045.2` (yeast) | 17 | 6 KB | 0.19 s |
| `GCF_000002445.2` (Aspergillus) | 12 | 4 KB | 0.15 s |
| `GCF_000001405.40` (human) | 705 | 244 KB | 0.22 s |

Per record: `chr_name`, `refseq_accession`, `genbank_accession`, `length`,
`role`, `sequence_name`, `ucsc_style_name`, `sort_order`, `assembly_unit`.

The existing `dataset_report` response does **not** carry per-sequence names --
only `total_number_of_chromosomes` -- so this is genuinely a second call, not a
field we were discarding.

## Two findings that shape the design

**1. Both accession namespaces are present in every record.** A record carries
`refseq_accession` *and* `genbank_accession`, so one lookup labels both the
`GCF_` file (`NC_001133.9`) and the `GCA_` file (`BK006935.2`). Keying the map
on both is what makes this work for every reference in the database rather than
half of them.

**2. `chr_name` is not unique, and labeling by it alone would be wrong.**
Unplaced and unlocalized scaffolds inherit their parent chromosome's
`chr_name`. In the real Aspergillus reference:

| accession | `chr_name` | `role` | `sequence_name` | length |
|---|---|---|---|---|
| `NT_165288.1` | 11 | unlocalized-scaffold | `chr11-scaffold01` | 5.3 Mb |
| `NT_165287.1` | 11 | unlocalized-scaffold | `chr11-scaffold02` | 286 kb |

Two bars would both read "11", and the 5.3 Mb one is the *largest bar on that
strip*. Human is worse: 126 unplaced-scaffold and 40 unlocalized-scaffold
records all reuse a parent's `chr_name`, against 25 assembled molecules.

So the label rule is role-dependent:

- `role == "assembled-molecule"` -> `chr_name` (`I`, `IV`, `MT`, `1`, `2`)
- anything else -> `sequence_name` (`chr11-scaffold01`, `HSCHR1_CTG1_UNLOCALIZED`)

## Scope

Backend fetch and storage, plus the strip reading the new fact. The strip stays
a **pure local render** -- no network call at render time, no loading state, no
new failure mode. This is the whole reason for choosing ingest-time fetch over
an on-demand one.

## Backend

### `app/metadata/assembly.py`

A new `lookup_sequence_names(accession) -> dict[str, str] | None` alongside
`lookup()`. Same never-raises contract: a network failure, a rate limit, a
schema change or a retired accession returns `None` and must never fail an
ingest.

Returns a flat accession -> label map, with **both** accessions of each record
pointing at the same label:

```python
{"NC_001133.9": "I", "BK006935.2": "I", "NC_001224.1": "MT", ...}
```

Bounded by the same `MAX_STORED_CONTIGS = 50` the parser uses for
`sequence_lengths`. A strip draws at most 24 bars plus an overflow list built
from the stored lengths, so labels beyond that window would have nothing to
label. Records are taken in `sort_order` so the 50 kept are the assembly's own
leading sequences rather than an arbitrary slice.

### `app/metadata/enrich.py`

`enrich_from_assembly` gains the names when the assembly lookup succeeded.
Already gated by `settings.assembly_enrichment_enabled`, so the existing
offline switch covers this with no new flag.

Stored as `facts.sequence_labels`. Namespaced under the existing `ncbi_*`
convention would be inconsistent here -- these are labels *for our own
sequences*, keyed by our own sequence names, so they sit next to
`sequence_lengths` rather than in the published-assembly block.

### What is not built

- **No new API endpoint.** The strip reads a fact, like everything else on the
  Quality tab.
- **No on-demand fetch.** Rejected: it would put a network dependency, a
  loading state and a failure mode into a component that currently has none.
- **No backfill migration.** Re-ingest already exists and does exactly this
  (see below).

## Frontend

`lib/chromosomes.ts`: `Bar` gains an optional `label`. `classifyChromosomes`
reads `facts.sequence_labels` and attaches the label for each bar's name when
present. No change to classification -- labels never affect which bucket a
reference lands in, nor which bars are drawn or linkable.

`ChromosomeStrip.tsx`: the bar caption prefers `bar.label`, falling back to the
current accession-digits derivation when it is absent. The tooltip and
`aria-label` keep the full accession plus length, and gain the label when there
is one, so nothing is available only in abbreviated form.

Existing references with no `sequence_labels` keep today's behavior exactly.

## Backfill: a correction to what shipped

The `needs-qc` message currently reads "Re-run QC to draw the chromosome map."
**This is wrong and must be fixed.** Verified against the code:
`facts.sequence_lengths` is written only by the ingest parser
(`storage/parsers.py:495`), while `run_qc` is a FASTQ read-quality handler
(fastp / FastQC / NanoPlot) that never touches it. Running QC on a reference
does nothing for the strip.

The correct action is **re-ingest** -- `POST /objects/{id}/reingest`, already
exposed in the UI on the Computations panel. It re-runs format detection,
header parsing *and* enrichment.

Verified on the real `GCA_000146045.2_R64_genomic.fna` object: before,
`sequence_lengths` absent and `ncbi_assembly_accession` null; after re-ingest,
16 length entries and the accession populated. The strip now draws for it.

So re-ingest is also the labels backfill: no migration is needed, and the
message change points users at the one action that fixes both.

The union tag stays `needs-qc` (renaming it would churn the tests for no user
benefit); only the user-facing sentence changes, to name re-ingest and, where
the UI can, point at the existing button.

## Testing

`lookup_sequence_names` gets unit tests against **captured real payloads**, not
hand-built dicts -- the CLAUDE.md rule, and the rule that caught the last two
bugs in this feature. Fixtures are trimmed real responses for yeast (the `MT`
case), Aspergillus (the duplicate-`chr_name` case) and a human slice (the
unplaced-scaffold case).

Assertions run in the direction that fails when the logic breaks:

- Both `NC_001133.9` and `BK006935.2` map to `I` from one lookup
- `NC_001224.1` maps to `MT`
- The two Aspergillus `chr_name: "11"` records map to *different* labels
  (`chr11-scaffold01`, `chr11-scaffold02`) -- this is the regression that a
  naive `chr_name` implementation produces, so it must fail loudly
- A malformed payload (missing `reports`, wrong types, an empty body) returns
  `None` rather than raising
- The map is capped at 50 entries for a 705-record assembly

`classifyChromosomes` gets a case asserting labels attach to the right bars and
that a reference with no `sequence_labels` is unchanged.

Frontend verification is manual at localhost:5173, as ever.
