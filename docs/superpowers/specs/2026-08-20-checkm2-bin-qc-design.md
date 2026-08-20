# CheckM2 bin completeness/contamination QC — design

Date: 2026-08-20.

Closes [#729](https://github.com/syntheticgio/bioflow/issues/729). Child 3 of
[#630](https://github.com/syntheticgio/bioflow/issues/630); see the epic spec's
decision M4.

**Depends on [#728](https://github.com/syntheticgio/bioflow/issues/728)** —
needs bins to score.

## Why this child is not optional

A bin is a hypothesis. MetaBAT2 will happily emit forty of them from noise,
and nothing about a FASTA says whether it is a near-complete genome or a pile
of misassigned contigs. CheckM2 gives each bin a **completeness** and
**contamination** percentage, which is what turns a bin set into something a
person can act on: keep the high-completeness, low-contamination ones, discard
or re-bin the rest.

Without it, #728 produces objects nobody should trust. That is why this
follows immediately rather than being optional polish.

## What exists today

Verified against this worktree on 2026-08-20:

- **`kraken_db_registry.py`** is the exact pattern this needs. `KrakenDbSpec`
  carries `key`, `label`, `url` (a **pinned dated snapshot**, never a `latest`
  alias), `download_bytes`, `mem_mb`, `md5`, `description`. Its docstring
  records that `mem_mb` is "known a priori from the database, never fitted from
  the memory model, because a model fit from unrelated jobs would
  under-provision exactly into an OOM".
- **`db_present(key)`** checks the *specific files* the tool needs, not the
  directory — because the download extracts into `<key>.partial` and renames on
  success, so a bare directory with missing files "means a bug rather than an
  in-flight download, and either way it must read as absent".
- **`kraken_handlers.download_kraken_db`** streams a multi-gigabyte tarball
  with urllib (nothing else in the repo streams HTTP to disk), verifies md5
  against the registry, extracts to `.partial`, renames on success. **No
  applier** — it fetches shared reference data, not something derived from an
  object.
- **`launch_classify_reads` chains behind the download when the DB is absent**
  rather than refusing. That is the established posture for a large shared
  database.
- **compleasm** is the closest analog for the *scoring* half, with a
  launch-time database check.

## Decision Q1: copy the Kraken2 database pattern, do not invent a second one

CheckM2 needs a multi-gigabyte pre-built DIAMOND database. Every constraint
that shaped `kraken_db_registry` applies unchanged:

- **Pin a dated snapshot.** CheckM2's database is distributed via Zenodo with
  versioned records. Use a specific record, never a "latest" alias — a moving
  database makes two runs of the same bin incomparable with nothing to say why.
- **Record `download_bytes` and a checksum**, and verify before extracting.
- **`mem_mb` known a priori**, not fitted. A DIAMOND search's residency is a
  property of the database, and a fit from unrelated jobs under-provisions into
  an OOM — which also poisons the timing models.
- **Extract to `.partial`, rename on success**, and have `db_present` check the
  actual files rather than the directory.

**Do not** rely on `checkm2 database --download`, even though it exists. It
resolves its own URL at runtime, which is precisely the moving target the
pinned-snapshot rule exists to prevent, and it puts integrity outside this
repo's control. The Kraken2 handler's docstring already makes this argument for
a tool with no self-managing downloader; here a downloader exists and should
still not be used.

*One registry entry, not three.* Kraken2 offers three databases because the
size/coverage trade-off is a real user choice. CheckM2 has one database; a
registry keyed by name is still the right shape (it carries the pin, checksum
and memory), but it holds a single entry and the card needs no picker.

## Decision Q2: chain the download; do not refuse

Following `launch_classify_reads`: when the database is absent, **enqueue the
download and make the QC job depend on it**, rather than refusing with "run the
download first".

Why this rather than the refuse-with-a-reason posture used elsewhere in this
epic: a missing database is not a missing *decision*. There is exactly one
database, the user does not choose it, and there is nothing for them to
consider — so a refusal would be busywork asking them to click a second button
with no information. Contrast #728's card, which refuses when no alignment
exists: that is a real input the user must supply.

The job must **surface the download as a phase**, not appear hung for the
minutes a multi-gigabyte fetch takes. `queue.py`'s `_failed_dependencies`
already fails a job whose dependency failed rather than leaving it blocked
forever — the same mechanism `launch_variants` relies on for on-demand caller
installs.

## Decision Q3: score every bin in one job, not one job per bin

The card acts on the **source assembly** (or the bin set as a whole), running
CheckM2 once over the directory of bins.

Why not per-bin jobs: CheckM2's fixed cost is loading the DIAMOND database,
which dominates per-bin work. Forty jobs would pay it forty times, and forty
queue entries for one user action is the same usability failure #728's cap
guards against. CheckM2's own interface takes a directory of bins and produces
one table — the tool is already shaped this way.

Results are **merged onto each bin object** as facts
(`checkm2_completeness`, `checkm2_contamination`, `checkm2_quality_score`),
plus the full table served from a report endpoint.

## Decision Q4: scores go on the bins, and no bin is auto-discarded

Store, do not act. A low-completeness bin is **not** deleted, hidden, or
flagged as invalid.

- Completeness and contamination are continuous, and the thresholds that
  matter ("high-quality MAG" ≥90/≤5, "medium" ≥50/≤10) are community
  conventions with real disagreement at the edges.
- A 40%-complete bin is a legitimate result for a low-abundance organism, and
  discarding it would destroy the finding that the organism is present.

So: render the numbers, optionally render the standard tier as a *label*
alongside them, and let the user decide. The InfoMarker should say what the
conventional thresholds are, since that is the interpretive knowledge the
numbers need and the user should not have to look it up.

**Contamination above ~100% is possible and is not a bug** — it means the bin
contains multiple copies of the marker set, i.e. several organisms merged. The
renderer must not clamp it to 100, which would hide the worst bins by making
them look merely mediocre.

## Requirements

- **R1.** A user can score a set of bins for completeness and contamination.
- **R2.** When the database is absent, the download is chained automatically
  and surfaced as a phase of the run.
- **R3.** Each bin object carries its completeness and contamination as facts.
- **R4.** The database is pinned to a dated snapshot, checksum-verified, and
  never half-present after an interrupted download.
- **R5.** No bin is deleted, hidden, or filtered on the basis of its score.
- **R6.** Contamination over 100% renders as its true value.

## Testing

- **Registry shape** — the pinned URL is a dated record, not a `latest` alias.
  A test asserting the URL contains no `latest` is cheap and catches the exact
  regression the pin exists to prevent.
- **`db_present`** — a `.partial` directory, and a directory missing one
  expected file, both read as **absent**.
- **Chaining (R2)** — with the database absent, the launch enqueues the
  download and the QC job depends on it; with it present, no download is
  enqueued.
- **Fact merge (R3)** — per-bin, by per-key `facts.<key>` path, never a
  whole-dict merge (#606).
- **R6** — a parsed contamination of 137% survives to the fact as 137, not
  clamped.
- **Registry partitions** — whole `TestExhaustiveness` class and the provenance
  partition.
- **Real-data check** — score real bins from a #728 run; confirm the numbers
  are plausible against the bins' sizes and that a deliberately merged bin
  scores high contamination.

## Verify before implementing

1. **CheckM2's aarch64 availability** — bioconda's `linux-aarch64` subdir
   first, per CLAUDE.md. CheckM2 is Python with a DIAMOND dependency, and
   DIAMOND is the part most likely to be x86-64 only.
2. **The database's pinned URL, size, and checksum**, from the Zenodo record.
3. **Peak RSS with the database loaded**, for `mem_mb` — measured, since Q1
   forbids fitting it.
4. **CheckM2's output table columns** on the installed version, for the parser.
5. **Whether CheckM2 needs a writable `CHECKM2DB` env var or a `--database_path`
   flag**, and which the installed version honours.

## Out of scope

- **Auto-filtering bins by quality** (Q4/R5).
- **GTDB-Tk taxonomic placement.** A different (and much larger) database and
  question; #730 covers labelling with the Kraken2 path already present.
- **Re-binning low-quality bins automatically.** The scores inform that
  decision; making it is the user's.
- **CheckM1.** Superseded, slower, and needs a larger database.
