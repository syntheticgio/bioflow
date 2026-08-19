# Kraken2 taxonomic classification / contamination screening design

**Date:** 2026-08-18

**Issue:** [#625](https://github.com/syntheticgio/bioflow/issues/625)

## Goal

Answer "what organism(s) are actually in this sample" from the reads
themselves. `contamination_stats.py` reports adapter and duplication
levels -- read-quality signal, not identity -- and `lineage_inference.py`
deliberately trusts `metadata["organism"]` rather than doing taxonomic
placement. Nothing in the stack can verify or discover that metadata from
the data. Kraken2 (k-mer classification against a reference database) plus
Bracken (species-level abundance re-estimation) closes that gap and, as a
direct consequence, detects cross-species contamination and mislabeled
samples.

## Decisions and their reasons

Settled during brainstorming, recorded here so the document survives the
conversation:

1. **Database delivery follows the `download_lineage` pattern, not
   `ON_DEMAND_IMAGE`.** The optional-tool-delivery spec
   (`2026-08-05-optional-tool-delivery-design.md`) explicitly scopes
   downloadable *data* -- naming Kraken2 databases -- out of the image
   mechanism. The shipped precedent for shared reference data fetched on
   demand is `download_lineage`: a node type with no run kind, no object
   ports, a dedup key, and a consumer that chains behind it with
   `depends_on`. This design generalizes that shape rather than building
   the deferred Settings "Data section"; the code should not preclude a
   later migration onto such a section, but none of it is built now.
2. **Kraken2 and Bracken binaries are `Delivery.BUNDLED`.** Each is a few
   MB and fails the optional-tool size rule on its own; only the database
   is heavy, and the database is data.
3. **Three pre-built databases, Standard-8 default.** From the Langmead
   k2 index collection: Standard-8 (~7.5 GB; archaea, bacteria, viral,
   plasmid, human, UniVec), PlusPF-8 (~7.5 GB; adds protozoa and fungi),
   Viral (~0.6 GB). Full-size databases are deliberately excluded: they
   need on the order of 100 GB RAM to load, which guarantees an OOM on
   the local machines this tool targets.
4. **Bracken is included, and runs inside the same job.** One
   `classify_reads` job runs Kraken2 then Bracken (seconds -- it
   re-estimates over the report, not the reads). A separate
   "re-estimate abundance" node would double the node/suggestion/UI
   surface for a step no one would decline. Bracken failure is
   non-fatal: the Kraken2 report is the deliverable.
5. **Results are facts + artifacts + a panel + a mismatch check.** The
   mismatch check against `metadata["organism"]` is the single most
   actionable output and ties directly back to the gap
   `lineage_inference.py` documents.
6. **Card availability tracks the binary probes only.** The database's
   absence changes launch-dialog copy ("first use downloads ~7.5 GB"),
   never the card state, and the download chains automatically at launch.

## Requirements

IDs are permanent; do not reuse.

### Tools

- **K2-T1.** `tools.py` gains `kraken2` and `bracken` PATH probes
  (`--version`), both `Delivery.BUNDLED`.
- **K2-T2.** Both tools carry full `TOOL_META` (`homepage`, `citation`,
  `license`, `usage`) and pass `test_every_tool_is_documented`.
  License and citation are verified against each project's own
  repository at implementation time -- expected but unconfirmed:
  Kraken2 MIT (Wood, Lu & Langmead, *Genome Biology* 2019), Bracken
  GPL-3.0 (Lu et al., *PeerJ Computer Science* 2017). A field that
  cannot be verified is left empty rather than guessed.
- **K2-T3.** `usage` describes behavior, not flags: classification of
  reads against an on-demand database; Bracken re-estimation of
  species abundance from the report.
- **K2-T4.** The Dockerfile installs both for the image's supported
  architectures; arm64 availability is confirmed during implementation
  (both are plain C++/Python and are expected to be unproblematic,
  unlike the Clair3 record).

### Database registry and storage

- **K2-D1.** A new `backend/app/pipelines/kraken_db_registry.py` holds a
  module-level registry of exactly three entries -- `standard-8`
  (default), `pluspf-8`, `viral` -- each with `key`, `label`, `url`
  (pinned versioned snapshot, never a "latest" alias),
  `download_bytes`, `mem_mb` (in-RAM load size), `md5`, and a one-line
  description of what the database can see.
- **K2-D2.** Databases live at `settings.kraken_dbs_dir` =
  `BIOINFO_HOME / "kraken_dbs" / <key>`, beside `lineages_dir`.
- **K2-D3.** `db_present(key)` returns true only when the three `.k2d`
  files exist in the entry's directory.
- **K2-D4.** Registry tests assert internal consistency: every entry
  carries every field, URLs are unique, and the default key is in the
  dict. (Keys are this repo's own strings, not an enum's members, so
  the registry-audit exhaustiveness pattern does not apply; internal
  consistency is the analogous check.)

### Download node

- **K2-N1.** A `download_kraken_db` node type mirrors
  `download_lineage`: `run_kind=None`, no input or output ports; the
  dataset lands outside the object model.
- **K2-N2.** `pipeline_service.launch_kraken_db_download(db_key, owner)`
  enqueues with `dedup_key=f"download_kraken_db:{key}"` so concurrent
  requests for the same database collapse into one job.
  `JobClass.USER_INTERACTIVE`, `IoClass.HEAVY`, `max_attempts=3`.
- **K2-N3.** The download handler verifies the tarball's md5 against
  the registry before extraction, extracts into `<key>.partial`, and
  renames into place only on success -- a killed or corrupt download
  never half-presents, and `db_present()` stays false until the rename.

### Classification node

- **K2-C1.** A `classify_reads` node type: QC-family, one FASTQ reads
  input port (any role), no output object, with a run record
  (`RunKind`/`RunJobRole` members following the existing QC jobs).
- **K2-C2.** `pipeline_service.launch_classify_reads(object_id, db_key,
  owner)` requires both tool probes; when `db_present(db_key)` is
  false it enqueues the download and chains the classify job behind it
  via `depends_on` -- the `launch_completeness` lineage shape.
- **K2-C3.** `JobResources.mem_mb` for classification comes from the
  registry entry's `mem_mb`, never from the fitted memory model: the
  load size is known a priori, and a model fit from unrelated jobs
  would under-provision exactly into an OOM.
- **K2-C4.** Both node types are registered in `NODE_TYPES` and the
  full `TestExhaustiveness` class passes (both partition tests, per
  the #355/#366 record).

### Runner

- **K2-R1.** `backend/app/pipelines/kraken_runner.py` follows the
  `quast_runner`/`bakta_runner` split: pure functions over strings and
  paths, unit-testable without binaries.
- **K2-R2.** `build_kraken2_command(...)` handles single-end and paired
  input (`--paired` with two FASTQs), gzip-compressed input, `--db`,
  `--report`, and threads. `--memory-mapping` is not used; memory is
  budgeted honestly via K2-C3 instead.
- **K2-R3.** `build_bracken_command(...)` takes the report and the
  database path, producing a species-level abundance table; read
  length comes from the reads object's stored stats, defaulting to 100
  when absent.
- **K2-R4.** `parse_kraken_report(text)` parses the six-column report
  (percentage, clade reads, direct reads, rank code, taxid, indented
  name) and returns structured rows; unparseable input yields an empty
  result rather than raising, the established `{}`-posture.
- **K2-R5.** `parse_bracken_output(text)` parses Bracken's TSV, same
  posture.
- **K2-R6.** `top_taxa(...)` selects the fact payload: top 10 taxa by
  abundance plus every taxon at >= 1%, and the unclassified fraction.
  Bracken abundances are preferred; Kraken2 species rows are the
  fallback when Bracken was skipped.
- **K2-R7.** `organism_mismatch(metadata_organism, taxa)` normalizes
  names and reports a mismatch when the metadata organism's genus is
  absent from the dominant taxa (>= 5% of clade reads). Absent
  metadata means no check and no fact -- "not stated" and "wrong" are
  different claims.

### Handler

- **K2-H1.** A `classify_reads` queue handler stages the reads, runs
  Kraken2, runs Bracken, parses both, and applies results. Non-zero
  Kraken2 exit fails the run; Bracken failure or a database without
  the `kmer_distrib` file logs a warning, records a `bracken_skipped`
  note in the facts, and ships Kraken2-only results.
- **K2-H2.** Facts stored on the reads object: a `taxonomy` fact
  (top taxa with name, rank, taxid, percent of reads; unclassified
  percent; database key and snapshot version; bracken-used flag), and
  a `taxonomy_mismatch` fact when K2-R7 fires, carrying the evidence
  (claimed organism, dominant classified taxa and their percentages).
- **K2-H3.** The raw Kraken2 report and Bracken TSV are attached to
  the run as downloadable artifacts (the formats Krona and Pavian
  consume).

### Suggestion card

- **K2-S1.** `suggestion_service.py` gains one rule: any FASTQ reads
  object yields an "Identify organisms (Kraken2)" card whose copy
  contrasts explicitly with the adapter/duplication QC card --
  identity ("what species are in these reads") versus read quality.
- **K2-S2.** Card availability follows the binary probes only; database
  absence never changes card state.
- **K2-S3.** Tests cover both directions, including asserting the card
  flips to unavailable when the probe is patched off -- the direction
  that fails when the seam breaks, since the image ships the tool. The
  rule is also checked against a real project's objects before the
  work is called done, per the repo's real-database rule.

### Frontend

- **K2-F1.** The launch dialog offers the three databases with size and
  description, Standard-8 preselected, and states "this database isn't
  downloaded yet -- first run fetches ~<size>" when `db_present` is
  false; no such line when it is present. A multi-GB download never
  starts without that sentence having been on screen.
- **K2-F2.** A results panel on the reads object shows the taxa table
  (rank, name, percent), an explicit unclassified row, a database
  name/version footer, and a warning banner when `taxonomy_mismatch`
  exists (e.g. "metadata says *E. coli*; reads classify as 94%
  *S. aureus*").
- **K2-F3.** UI verification is manual against the worktree stack
  (localhost:5273), per the repo's testing posture.

## Error handling summary

| Failure | Behavior |
|---|---|
| Download interrupted / bad md5 | Retry (max 3); partial dir never renamed into place |
| Database dir present but incomplete | `db_present()` false, re-download chains at next launch |
| Bracken missing distrib / fails | Skip, warn, Kraken2-only results (K2-H1) |
| OOM risk | Prevented up front: `mem_mb` from registry, full DBs not offered |
| Unparseable report | Empty-result posture; run fails only on non-zero Kraken2 exit |

## Testing

- Runner: fixtures from real Kraken2 report and Bracken output
  snippets; command construction incl. paired and gzipped input;
  mismatch normalization cases (genus match, species mismatch, absent
  metadata).
- Registry: K2-D4.
- Node types: full `TestExhaustiveness` class.
- Launch: chains the download when the DB is absent, does not when
  present; dedup collapses concurrent requests.
- Suggestion: K2-S3, both directions.
- Tool docs: `test_every_tool_is_documented` for both tools.
- Suite runs from the worktree via `./backend/run-worktree-tests.sh`.

## Out of scope

- Krona-style interactive visualization of the classification.
- Custom or user-built Kraken2 databases (registry entries only).
- Host-read *removal* -- this design detects contamination, it does not
  filter it; filtering is a follow-up with its own design questions.
- Migrating `download_lineage` onto a shared Settings "Data section"
  abstraction; this design keeps the door open and builds none of it.
