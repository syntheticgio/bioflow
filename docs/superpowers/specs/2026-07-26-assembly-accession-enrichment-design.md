# Assembly accession detection and NCBI enrichment

Date: 2026-07-26
Status: Approved, ready for implementation planning

## Problem

Reference genomes arrive from NCBI with the assembly accession already in the
filename — `GCF_000002445.2_ASM244v1_genomic.fna` carries `GCF_000002445.2`.
NCBI holds far better metadata for that assembly than anyone will retype:
organism, strain, assembly name, submitter, release date, BioProject.

Today none of that is used. The file lands in Reads (its format is FASTA, and
nothing says otherwise), and every field must be entered by hand.

This is the same problem `metadata/sra.py` already solves for sequencing runs,
and the solution is deliberately the same shape.

## Goal

A file whose name (or `assembly_accession` field) contains a GCA/GCF accession
is recognized as a reference, categorized as one automatically, and enriched
with the NCBI assembly record — while still being parsed and measured as any
other file, so what the file actually contains can be compared against what its
name claims.

## Decisions

**Parser facts stay authoritative; NCBI stats are additive.** Our parser
describes *the file on disk*; NCBI describes *the published assembly*. These
legitimately differ — a file may hold primary chromosomes only, be filtered, or
sit at a different patch level. Both are shown. A divergence is informative
rather than an error: a file named for a full assembly that contains one
chromosome is a real problem that is otherwise very hard to notice.

**Filename pattern alone assigns the role; the lookup only enriches.** The
existing code is explicit that "a network failure, a rate limit, or a retired
accession must never fail an ingest." Making *categorization* depend on a
network call would violate that in spirit — the same file would land in a
different section depending on whether NCBI was reachable. The accession regex
is strict enough (`GC[AF]_` + 9 digits + `.` + version) that false positives are
unlikely.

**Auto-assignment never overrides an explicit choice.** Role is set
automatically only when it is currently `None`. A user who converted a file to
reads may be running an unusual experiment, or know something about the file
that its name does not say. This mirrors the enrichment rule that a user's
value always stands.

**Identity fields go to `metadata`, statistics to `facts`.** `metadata` is
"things a person might correct or search on"; `facts` is "what we measured or
were told." Keeping NCBI's stats in `facts` under an `ncbi_` prefix also puts
both sides of the parser-vs-NCBI comparison in one dict, so the panel can render
them side by side.

**Enrichment never overwrites what a person entered.** Inherited verbatim from
`enrich.py`. Fields already set are left alone; disagreements are surfaced as
conflicts for the user to resolve.

## Accession detection — `backend/app/metadata/assembly.py` (new)

Mirrors the structure of `sra.py`.

```python
_ACCESSION_RE = re.compile(r"(?:^|[^A-Za-z0-9])(GC[AF]_\d{9}\.\d+)", re.IGNORECASE)
```

Anchored at a word boundary so `GCF_000002445.2_ASM244v1_genomic.fna` matches
and `MYGCA_000000001.1` does not.

**Endpoint:** NCBI Datasets v2alpha,
`https://api.ncbi.nlm.nih.gov/datasets/v2alpha/genome/accession/{acc}/dataset_report`.
This is the genome-oriented API; E-utilities (used by `sra.py`) is not. Verified
to return the full record for both `GCF_000002445.2` and `GCA_000001405.29`.

Same discipline as `sra.py`: 20s timeout, ≤3 req/s rate limiting, 2 retries,
`tool=` identification, and **every failure caught** — enrichment is a
convenience and must never turn a good file into a failed ingest.

**Version tolerance.** A filename often carries a superseded version. If
`GCF_000002445.3` returns nothing, retry once with the accession stripped of its
version suffix (NCBI resolves to current) and record which version actually
answered, so the user can see they hold an older file than the record describes.

## What is captured

**To `metadata`** — searchable, user-correctable, subject to the
never-overwrite rule:

| Key | NCBI source |
|---|---|
| `assembly_accession` | the resolved accession |
| `organism` | `organism.organism_name` |
| `strain` | `organism.infraspecific_names.strain` |
| `reference_build` | `assembly_info.assembly_name` (e.g. ASM244v1) |
| `source` | `assembly_info.submitter` |
| `bioproject` | `assembly_info.bioproject_accession` |
| `tax_id` | `organism.tax_id` |
| `assembly_level` | `assembly_info.assembly_level` (Chromosome, Scaffold, Contig) |
| `assembly_date` | `assembly_info.release_date` |
| `paired_accession` | `paired_accession` (the GCA↔GCF counterpart) |

The last four are new `REFERENCE_FIELDS` entries. `tax_id`, `assembly_level`,
and `assembly_date` are not `suggested` — they are almost always filled by
enrichment rather than by hand.

**To `facts`** under an `ncbi_` prefix, clearly distinct from parser output:
`ncbi_total_length`, `ncbi_contig_count`, `ncbi_gc_percent`, `ncbi_scaffold_n50`,
`ncbi_assembly_name`, `ncbi_fetched_at`.

**Deliberately not captured:** `is_primary_assembly` and `has_decoy` stay
user-only. NCBI describes the published assembly, and neither property can be
asserted about *the file the user actually has*.

## Auto-role assignment

In `_apply_ingest_headers` (`backend/app/queue/results.py`), which is where the
ingest result is applied to the object:

```python
if enrichment.get("assembly_accession") and obj.role is None:
    update[DataObject.role] = ObjectRole.REFERENCE
```

The `obj.role is None` guard is the whole of the never-override rule. Parsing is
untouched: FASTA parsing runs exactly as it does now, so the file is measured
whether or not enrichment succeeds.

Detection order matches `enrich.resolve_accession`: an explicit
`metadata.assembly_accession` beats the filename, so typing an accession and
re-ingesting is the escape hatch when a name is missing or misparsed.

## Enrichment integration

`enrich.py` gains `enrich_from_assembly` beside `enrich_from_sra`, reusing
`EnrichmentResult` and the conflict logic verbatim. It is eligible only for
`FormatKind.FASTA` — a FASTQ of reads should not be assembly-enriched.

The two paths cannot collide on realistic filenames. `SRA_ELIGIBLE_FORMATS`
includes FASTA, so in principle a FASTA reaches both — but the SRA regex
requires an `SRR`/`ERR`/`DRR`-family prefix, and an assembly filename carries
none. Verified: `sra.parse_accession("GCF_000002445.2_ASM244v1_genomic.fna")`
returns `None`.

So no precedence rule is needed, and none should be added. Both enrichments run
for a FASTA; in practice at most one finds an accession. A file whose name
somehow contained both is pathological enough that recording both accessions is
the honest outcome, not something to arbitrate.

Provenance goes to `facts` mirroring the SRA convention: `assembly_accession_source`
(`"metadata"` or `"filename"`), `assembly_fields_applied`, `assembly_conflicts`,
`assembly_error`.

## Settings

`assembly_enrichment_enabled: bool = True` in `config.py`, beside
`sra_enrichment_enabled`, with a comment noting it is an outbound network call
and can be disabled for a fully offline stack.

## Display

`AssemblyFacts.tsx` gains a "Published assembly (NCBI)" block beside the
measured figures, reading the `ncbi_`-prefixed facts.

**Divergence note.** When the measured and published figures disagree beyond a
tolerance — >1% on total length, or any difference in sequence count — render a
neutral observation:

> This file has 5 sequences totalling 12.1 Mb; the published assembly has 50
> totalling 26.1 Mb. It may be a subset.

Phrased as an observation, not a warning: holding a chromosome subset is often
deliberate. The point is to make it visible, not to imply an error.

When enrichment failed, show `assembly_error` quietly rather than silently
displaying nothing — a user who expected metadata should learn why it is absent.

## Testing

- Regex: matches `GCF_000002445.2_ASM244v1_genomic.fna`, `GCA_000001405.29`,
  lowercase; rejects a missing version, an 8-digit body, and `MYGCA_000000001.1`.
- Lookup: parses a captured NCBI response fixture into the expected fields;
  never raises on network error, timeout, malformed JSON, or an empty
  `reports` array.
- Version fallback: a not-found versioned accession retries bare and records the
  version that answered.
- Enrichment: user-set fields are never overwritten; disagreements become
  conflicts; a FASTQ is not assembly-enriched.
- Role assignment: assigned when `role is None`; **not** assigned when the user
  already set a role, in either direction.
- Offline: with `assembly_enrichment_enabled = False`, ingest still succeeds and
  the file is still parsed.

Network tests use a captured fixture, not a live call — the suite must stay
runnable offline, consistent with `tests/fixtures/sra_*.xml`.

## Out of scope

- Downloading the assembly from NCBI. This reads metadata for a file the user
  already has.
- Verifying file contents against the published assembly (checksum or per-contig
  comparison). The divergence note is a summary-level observation only.
- Auto-assigning roles other than reference.
- Backfilling existing objects. Applies on ingest and re-ingest; the existing
  re-ingest button is the way to apply it to a file already in the system.
