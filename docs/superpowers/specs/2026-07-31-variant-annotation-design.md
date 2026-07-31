# Variant consequence annotation

## Problem

The variants table says *what changed* and nothing about *what it does*.
`NC_001133.9 : 22,639 : A→T` is a coordinate and two bases; whether it sits in
a gene, changes a protein, or is silent is unanswerable from anything the
pipeline stores.

`variant_runner.py` runs `bcftools mpileup → call → view`, and
`vcf_stats_runner.py` extracts `%CHROM %POS %REF %ALT %QUAL %FILTER %DP %GT`.
No annotator is registered in `tools.py`. So the most common question asked of
a variant list -- *which of these matter?* -- has no answer here.

This also blocks the 3D structure viewer (iCn3D) described in
`2026-07-31-variant-genomic-context-design.md`: a structure view needs a gene
and a residue number, and nothing currently produces either.

## Goal

An annotation step that adds gene, consequence type, and amino-acid change to
called variants, surfaced as filterable columns in the variants table.

## Findings from running it

`bcftools csq` was run against the real yeast VCF (6,641 variants,
`DRR1066343.bcftools.vcf.gz`) with the NCBI GFF3 for GCF_000146045.2 before
this design was written. Everything below is measured, not assumed.

### It produces what is needed

4,152 of 6,641 variants annotated (63%):

```
BCSQ=missense|CYS3|rna-NM_001178157.1|protein_coding|+|160K>160M|131277A>T
```

Gene, transcript, and residue 160 changing K→M. Consequence mix: 2,138
synonymous, 1,633 missense, 86 intron, 58 frameshift, 31 stop_gained, 26
inframe_deletion, 10 inframe_insertion, 11 non_coding.

### The default invocation aborts

```
$ bcftools csq -f ref.fa -g yeast.gff variants.vcf
exit=255
Unphased heterozygous genotype at NC_001133.9:88609. See the --phase option.
```

`csq` defaults to `-p r`, which *requires* phased genotypes; `bcftools call`
emits unphased ones. Measured alternatives:

| mode | exit | annotated |
|---|---|---|
| `-p r` (default) | 255 | aborts |
| `-p a` | 0 | 4,152 |
| `-p m` | 0 | 4,149 |
| `-p s` | 0 | 3,355 |

**`-p a` is the choice.** It treats `0/1` as `0|1` -- an arbitrary phase, which
is honest for data that has none, and matters only for haplotype-aware calls
across adjacent variants. `-p s` silently drops ~800 heterozygous sites, which
is a worse failure: the table would be quietly incomplete rather than
approximate.

### BCSQ is not one fixed shape

Field counts across the 4,152 annotated records: 7 fields (4,027), 4 (104),
5 (5), 1 (141).

```
missense|CYS3|rna-NM_...|protein_coding|+|160K>160M|131277A>T   7 fields
start_lost|SNU23|rna-NM_...|protein_coding|-                     5 fields
intron|RPL19B||protein_coding                                    4 fields
@286153                                                          1 field
```

- **`@position`** is a pointer to another record sharing a haplotype. 136 occur.
- **Commas** separate multiple consequences (124 records) -- usually the same
  variant against overlapping transcripts.
- A `@` pointer **can appear inside a comma list beside a real consequence**:
  `missense|CHS3|...|284437G>T,@286153`. This was verified specifically because
  the opposite was assumed at first. A parser that rejects any record
  containing `@` would discard real annotations.
- A `*` prefix on the consequence type marks a compound/haplotype-modified
  prediction (36 `*synonymous`, 18 `*missense`).

### Input availability is the real constraint

| project | reference | GFF3 | variants | can run? |
|---|---|---|---|---|
| T. brucei | yes | yes | 0 called | nothing to annotate |
| Yeast | yes | **no** | 6,641 | needs the GFF3 |
| Test (S. aureus) | yes | yes | none yet | needs a VCF |

No project can run this today without a missing piece. The yeast GFF3 was
downloaded by hand for the test above. This shapes the Actions card: it must
name *which* input is absent.

## Scope

**In:**

- `bcftools csq` registered as a tool and wrapped in a runner.
- A `VARIANT_ANNOTATION` run kind, queued like the existing variant calling.
- Consequence parsed into columns on the variants index.
- Gene / Consequence / AA-change columns in the table, with a consequence
  filter.
- An Actions card that names the missing input when it cannot run.

**Out:**

- **iCn3D / 3D structure.** This spec unblocks it; it does not build it.
  Deferred until the annotation columns exist and prove useful.
- **snpEff / VEP.** `bcftools csq` needs no new binary (bcftools 1.21 is
  already in the image and already probed) and no database download. If its
  accuracy proves insufficient, adding a second annotator is a later decision
  with evidence behind it.
- **Automatic GFF3 fetching.** The NCBI download flow already offers the GFF3
  alongside the genome; the card points at that rather than adding a second
  path to the same file.
- **Re-annotating on GFF3 change.** Annotation runs when asked, like every
  other pipeline step here.

## Architecture

Mirrors `variant_runner.py` and `vcf_stats_runner.py`, which already solve the
same problems (staging blobs into a job dir, streaming bcftools output,
recording provenance).

### `backend/app/pipelines/tools.py`

Add a `bcftools_csq` probe. Not a new binary -- it reuses the resolved
`bcftools` path -- but a distinct capability, so the Actions card can say "your
bcftools is too old" rather than "bcftools is missing". `csq` requires ≥1.7;
the image ships 1.21.

### `backend/app/pipelines/csq_runner.py` (new)

```
bcftools csq -f <reference.fa> -g <annotation.gff3> -p a -O z -o <out.vcf.gz> <in.vcf.gz>
```

- `-p a` for the reason measured above. The choice is a named constant with the
  measurement in its comment, not a bare flag.
- The reference needs a `.fai`; `samtools faidx` is already used elsewhere for
  exactly this.
- GFF3 parse warnings ("unknown phase", "duplicate id", "unknown biotype") are
  normal on real NCBI files -- the T. brucei GFF3 emits all three -- and are
  logged, not failed on.
- Output is a new VCF object with `role: variants`, `derived_from` the input,
  so provenance holds and the original is never mutated.

### `backend/app/pipelines/csq_parse.py` (new)

One pure function: `parse_bcsq(value: str) -> Consequence | None`, holding
every shape above. Pure and separate from the runner because it is the part
with real edge cases and the part worth testing exhaustively -- the same split
that put `focusWindow`/`markerLabel` in `lib/chromosomes.ts`.

Rules, each traceable to a finding:

1. Split on `,` into candidate consequences.
2. Discard items starting with `@` (pointers, not consequences). Discard them
   individually -- a list may mix pointers with real entries.
3. For the remainder, take fields by index with bounds checks: `[0]` type,
   `[1]` gene, `[2]` transcript, `[5]` amino-acid change when present.
4. Strip a leading `*` from the type, recording it as a `compound` flag.
5. Parse `160K>160M` into `aa_pos=160`, `aa_ref=K`, `aa_alt=M`. Synonymous
   records carry `99P` with no `>` -- position and residue, no change.
6. When several remain, keep the most severe by a fixed ranking
   (frameshift/stop_gained > missense > synonymous > intron > non_coding) and
   record the count so the UI can say "+2 more".

### `backend/app/pipelines/variant_db.py`

Four columns on the `variants` table: `gene TEXT`, `consequence TEXT`,
`aa_change TEXT`, `aa_pos INTEGER`. `VariantFilters` gains `consequence: str |
None`, and an index on `consequence` -- it is the column users will filter by,
and the existing table already indexes `filter` for the same reason.

`QUERY_FORMAT` in `vcf_stats_runner.py` gains `%INFO/BCSQ`. Absent on an
un-annotated VCF, where bcftools emits `.` -- so the existing path keeps
working and the columns are simply empty.

### Frontend

- `VariantRow` gains `gene`, `consequence`, `aa_change`.
- Three columns, placed after ALT so the biological reading (what changed →
  what it does) runs left to right ahead of the quality numbers.
- A consequence dropdown beside the existing Type/Filter controls, populated
  from what the file actually contains rather than a hardcoded list.
- Empty cells render as `—`, consistent with the existing QUAL/depth columns.
  Most VCFs will be un-annotated and that must look ordinary, not broken.

### `backend/app/services/suggestion_service.py`

An `annotate` card on VCF objects. Per CLAUDE.md, registering the tool without
this leaves a dead card, so the rule ships with the tool.

The `unavailable` reason names the specific missing input, because that is what
the user can act on:

- no reference resolvable → "The reference this VCF was called against isn't in
  this project."
- reference present, no GFF3 → "No annotation (GFF3) for this reference —
  download it from NCBI alongside the genome."
- zero variants → "This VCF has no variants to annotate." (The real T. brucei
  case.)
- bcftools < 1.7 → names the version.

## Testing

Backend, `pytest` inside the `api` container:

- `csq_parse` against every shape measured above, using real strings from the
  yeast run: the 7/5/4/1-field forms, a comma list of two transcripts, a list
  mixing a real consequence with `@286153`, a `*missense`, and the synonymous
  `99P` form with no `>`.
- Severity ranking picks frameshift over synonymous when both are present.
- The command builder emits `-p a` -- the regression that would otherwise
  return as an exit-255 in production.
- `variant_db` round-trips the new columns and filters on consequence.

Per CLAUDE.md, the rules also get checked against the real database rather than
only fixtures: run the annotation on the yeast VCF once the GFF3 is ingested,
and confirm the 4,152 annotated count matches what the command line produced.

Frontend: `focusWindow`-style pure helpers if any emerge; otherwise manual
verification at localhost:5173, which is the actual verification step for UI
work in this repo.

## Follow-on

**iCn3D.** With `gene` and `aa_pos` populated, the remaining work is a
gene→structure lookup and an iframe. The open risk is unchanged: structure
coverage is per-protein, not per-organism. Measured at NCBI, *S. cerevisiae*
has 6,709 structures and *S. aureus* 2,693, but well-studied genes (`ADH1`,
`PGK1`) have several while uncharacterised ORFs (`YAL067C`) have none -- so "no
structure available" must be a normal outcome, not an error.
