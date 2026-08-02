# Unified NCBI download: assemblies (GCA/GCF) beside SRA

Turns "Download from NCBI SRA" into "Download from NCBI": one accession box
that accepts anything INSDC issues, resolves it, and shows the right thing --
a run checklist for sequencing data, an assembly card with component
checkboxes for a genome. Adds GenBank/RefSeq assembly download (genome FASTA,
annotation, protein, CDS), three new object roles so those files are
identifiable in the explorer long before anything consumes them, and
collapsible experiment grouping so a BioProject's runs stay distinguished by
experiment.

## Why now

The resolver is already most of the way there and nothing surfaces it.
`sra_resolver.classify()` handles run, experiment, sample, study, BioProject
and BioSample, and one `esearch db=sra&term=<accession>` query resolves all of
them -- pasting `PRJNA1495534` today already returns every run across its six
experiments. Two things are missing rather than broken:

1. **Assemblies are not downloadable at all.** `assembly.py` looks up assembly
   *metadata* and is wired into ingest enrichment, so an uploaded
   `GCF_000002445.2_ASM244v1_genomic.fna` gets organism, strain, assembly
   level and N50 filled in. But there is no path that fetches the file. A
   reference genome has to be found on the NCBI website, downloaded by hand,
   and uploaded.
2. **`build_hierarchy()`'s output is ignored by the UI.** A 24-run BioProject
   renders as 24 flat rows across two pages with nothing saying which
   experiment each belongs to.

The assembly gap is the expensive one, and it comes with a hazard worth fixing
in the same pass: `pipeline_service.py:1187` gates reference selection on
`role is ObjectRole.REFERENCE`, so any FASTA arriving without a role is a
plausible-looking aligner reference. Downloading protein and CDS FASTA without
first giving them roles would put files in the reference dropdown that would
produce silent garbage if selected.

## Scope

**This slice:** assembly classification and resolution, the `datasets` CLI
dependency, the assembly download handler and applier, four downloadable
components with per-component selection, three new `ObjectRole` values with
their field vocabularies and explorer labels, the unified dialog, and
collapsible experiment grouping.

**Deliberately not this slice:** consuming annotation. Nothing reads a GFF3
today -- no annotation-aware step exists. These files are downloaded and made
identifiable so that work can start from data already in hand, which is the
explicit intent, but no pipeline stage is built on them here. `gtf`, `gbff`,
`rna` and `seq-report` are also available from the CLI and are not offered;
adding one later is a table entry plus a role, not a redesign.

**Also not this slice:** mixed selection. The box resolves one accession at a
time, so downloading a GCA and three SRRs is two actions. Nothing in the
design precludes batching later.

## Verified facts

Everything below was confirmed against `datasets` 18.30.1 and the live
Datasets v2alpha API on 2026-07-29, not recalled. The details drove real
design changes and are recorded so a future reader can tell what was checked
from what was assumed.

**`--include` component names** are `genome`, `gff3`, `protein`, `cds` (also
`rna`, `gtf`, `gbff`, `seq-report`, `all`, `none`). Default is `genome`.

**`--no-progressbar` is required.** Without it the CLI writes an ANSI
cursor-up progress bar that produced ~40 near-identical lines in a trivial
download -- it would flood the job log and defeat `_log_tail`.

**The zip layout** for `GCF_000002445.2 --include genome,gff3,protein,cds`:

```
README.md
md5sum.txt
ncbi_dataset/data/assembly_data_report.jsonl
ncbi_dataset/data/dataset_catalog.json
ncbi_dataset/data/GCF_000002445.2/GCF_000002445.2_ASM244v1_genomic.fna
ncbi_dataset/data/GCF_000002445.2/cds_from_genomic.fna
ncbi_dataset/data/GCF_000002445.2/genomic.gff
ncbi_dataset/data/GCF_000002445.2/protein.faa
```

**The genome FASTA and the CDS FASTA are both `.fna` in the same directory.**
This is the trap in the whole feature: labeling components by extension roles
`cds_from_genomic.fna` as `REFERENCE`, which puts a CDS file in the aligner's
reference dropdown -- exactly the hazard this slice exists to close.

**`dataset_catalog.json` states each file's type explicitly**, which is why
filename matching is not the primary labeling source:

```json
{"filePath": "GCF_000002445.2/cds_from_genomic.fna",
 "fileType": "CDS_NUCLEOTIDE_FASTA", "uncompressedLengthBytes": "15188456"}
```

Observed `fileType` values: `GENOMIC_NUCLEOTIDE_FASTA`, `CDS_NUCLEOTIDE_FASTA`,
`PROTEIN_FASTA`, `GFF3`, `DATA_REPORT`.

**`--preview` reports per-component availability and exact sizes** without
transferring anything. For the unannotated `GCA_000001405.29` every
non-genome component comes back `file_count: 0`:

```json
{"estimated_file_size_mb":927,
 "included_data_files":{"all_genomic_fasta":{"file_count":1,"size_mb":927.97},
 "cds_fasta":{"file_count":0,"size_mb":0},"genome_gff":{"file_count":0,"size_mb":0},
 "prot_fasta":{"file_count":0,"size_mb":0}}}
```

**GenBank records generally carry no annotation.** `annotation_info` is
present on `GCF_000002445.2` and `GCF_000001405.40`, absent on
`GCA_000001405.29`. So "annotation unavailable" is the *common* case for a
GCA, not an edge case, and the paired GCF is what the user actually wants.

**A missing assembly returns `{}`, not an HTTP error.**
`GCA_000002445.2`'s `dataset_report` is an empty object with no `reports` key.
`parse_report` already handles this, but component detection must distinguish
"assembly not found" from "no components available".

**`datasets` is not in the worker image.** Present on the host at
`~/.local/bin/datasets`; `docker compose exec worker which datasets` finds
nothing. The Dockerfile line is required, not optional.

## Accession classification

`sra_resolver.classify()` gains assembly detection at the top, delegating to
the existing `assembly.is_valid_accession`:

| Pattern | Kind |
|---|---|
| `GCA_`/`GCF_` + 9 digits + `.version` | `assembly` |
| `PRJNA`/`PRJEB`/`PRJDB` + digits | `bioproject` |
| `SAMN`/`SAME`/`SAMD` + optional letter + digits | `biosample` |
| otherwise | `sra.accession_kind()` |

`resolve()` short-circuits on `assembly` before `esearch`. A GCF sent to
`db=sra` finds nothing and would produce "No sequencing runs found for
GCF_000002445.2" -- technically true, actively misleading.

`is_resolvable()` accepts assembly accessions so a typo still gets an
immediate answer rather than a round trip.

## API surface

One new router, `backend/app/api/v1/ncbi.py`, with `sra.py`'s contents folded
in. "The NCBI router" is the honest name once it serves assemblies, and 148
lines plus the new models stays readable. The existing `/sra/*` paths are kept
as aliases so nothing that works today breaks.

**`POST /ncbi/resolve`** classifies, then dispatches:

```
{kind: str, sra: SraResolveResponse | null, assembly: AssemblyResolveResponse | null}
```

Two nullable branches with an explicit `kind` discriminator rather than one
merged model. Merging would make most fields nullable and the frontend would
branch on shape anyway; this way the branch is named.

**`AssemblyResolveResponse`** carries the summary card and the component list:

```
accession, current_accession, organism, tax_id, strain, assembly_name,
assembly_level, submitter, release_date, bioproject, paired_accession,
source_database, total_length, scaffold_count, contig_count, gc_percent,
scaffold_n50, already_downloaded: bool,
components: [{key, label, available, size_bytes, reason}], error
```

Every field but `components`, `already_downloaded` and `source_database` comes
from `AssemblyMetadata`, which already parses them.

**`POST /ncbi/download-assembly`** mirrors `/sra/download`, returning 202 with
`{run_id, download_job_ids, skipped}`.

## Assembly resolution

`assembly.py` gains `available_components(accession)`. Two sources, in order:

1. **`datasets ... --preview --no-progressbar`**, parsed for
   `included_data_files[*].file_count` and `size_mb`. Primary because it is
   the same tool that will perform the download, it answers per-component
   rather than annotation-in-general, and it yields exact sizes for the disk
   pre-flight.
2. **`annotation_info` presence** in the `dataset_report` already fetched by
   `lookup()`, when the CLI is unavailable or `--preview` fails. Coarser --
   it says "this assembly has annotation" without distinguishing GFF3 from
   protein from CDS -- so all three non-genome components are offered
   together, and an empty component simply ingests nothing.

An empty `{}` report is "assembly not found" and returns an error, distinct
from a found assembly with only a genome. This distinction is why
`available_components` returns `None` on not-found rather than an
all-unavailable list.

Components map to roles as:

| Component | `--include` | `fileType` | Format | Role |
|---|---|---|---|---|
| Genome FASTA | `genome` | `GENOMIC_NUCLEOTIDE_FASTA` | FASTA | `REFERENCE` |
| Annotation | `gff3` | `GFF3` | GFF | `ANNOTATION` |
| Protein FASTA | `protein` | `PROTEIN_FASTA` | FASTA | `PROTEIN` |
| CDS FASTA | `cds` | `CDS_NUCLEOTIDE_FASTA` | FASTA | `TRANSCRIPT` |

Genome is always selected and cannot be deselected -- every other component
describes coordinates or products of that sequence and is close to
uninterpretable without it. Unavailable components render disabled with a
reason; for a GCA whose components are all unavailable, the reason names the
`paired_accession` GCF, because that is what the user wants and they should
not have to know that RefSeq is where annotation lives.

## Assembly download

> **Renamed 2026-08-01.** This handler now lives at
> `backend/app/queue/ncbi_assembly_handlers.py`. The old path holds *de novo*
> assembly, which is unrelated code -- so following the reference below would
> land somewhere plausible and wrong. The rest of this section is unchanged and
> still describes the download handler.

New `backend/app/queue/assembly_handlers.py`, sibling to `sra_handlers.py` and
following it closely: `HandlerMode.SUBPROCESS`, `JobClass.USER_INTERACTIVE`,
`JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY)`, `max_attempts=3`, a fresh
scratch dir per attempt from `_prepare_workdir`, and a disk pre-flight before
the transfer.

Separate from `download_sra_run` rather than branching inside it, for the
reason `sra_handlers.py`'s own docstring gives for existing: the operational
shape differs. One accession yields one job producing up to four files with no
QC chained; a run yields FASTQ pairs that always chain QC. What they share --
shelling out, log capture, retry classification -- is already factored into
`run_subprocess` and `_download_failure`.

**Steps.**

1. `tools.require(tools.datasets())`.
2. `datasets ... --preview` for the size estimate. Exact per-component figures
   beat deriving from `total_sequence_length`, which would have to guess at
   annotation and protein sizes.
3. Disk pre-flight. `--preview`'s `estimated_file_size_mb` is the *download*
   size, and the zip is extracted beside itself, so the peak requirement is
   roughly the sum of both. A new `ASSEMBLY_EXTRACTION_FACTOR = 2.5` covers
   that plus headroom -- deliberately not reusing SRA's `4.0`, which exists to
   guess at a compressed archive expanding into plain FASTQ. Here the
   post-extraction size is *known* per component from
   `dataset_catalog.json`'s `uncompressedLengthBytes`, but only after the
   download; the factor is what stands in for it beforehand. Own constant, own
   comment, so neither drifts with the other.
4. `datasets download genome accession <acc> --include <selected>
   --no-progressbar --filename <work>/package.zip`.
5. Verify against `md5sum.txt`. Cheap, and a truncated transfer that exits 0
   is otherwise indistinguishable from a good one until an aligner fails on it
   hours later.
6. Extract with `zipfile` into the work dir.
7. Label each file from `dataset_catalog.json`'s `fileType`, falling back to
   filename patterns (`cds_from_genomic.fna` matched *before* `*_genomic.fna`,
   since both are `.fna` and the order is what prevents a CDS file becoming a
   reference).
8. Return `{accession, staged: [{path, name, component, role}], ...}` -- the
   same shape `_apply_sra_download` consumes, with `component` where the SRA
   handler puts `mate`.

**Progress.** No usable percentage: `--no-progressbar` suppresses the bar, and
its ANSI-heavy output is not worth parsing. Phases are reported instead --
`preview`, `downloading`, `verifying`, `extracting`, `done` -- which is honest
about a job that is mostly one opaque transfer. `ctx.extend_lease(3600)` before
the download, as the SRA handler does, so a slow transfer does not let the
reaper double-run the job.

**Failure classification** reuses `sra_handlers._download_failure`, moved to a
shared module since both need it. Its bias is right here too: a `datasets`
failure is far more often the network than the accession. A retracted
accession still fails identically forever, which the "not found"/"invalid"
permanent branch already catches.

## Ingest and identification

`_apply_assembly_download` in `results.py`, mirroring `_apply_sra_download`:
one failed component must not lose the rest, each file ingested with
`produced_by_job`, and outputs recorded on the run.

Metadata comes from `AssemblyMetadata.to_metadata()` and facts from
`to_facts()` -- both already exist and are already what ingest enrichment
writes for an *uploaded* reference. A downloaded genome is therefore annotated
identically to a hand-uploaded one and stays findable by the same search,
which is the whole reason to reuse them rather than invent a parallel
vocabulary.

Provenance facts, distinct from the biology: `assembly_downloaded_from`,
`assembly_download_source: "ncbi_datasets"`, `assembly_component`.

**Every component gets `assembly_accession` in metadata.** That is the key
that makes four files recognize each other -- the explorer can group them, and
a future annotation-aware step can find the GFF3 belonging to a reference
without a join table.

It is also what `already_downloaded` matches on. `sra_service.already_downloaded`
queries `metadata.sra_run`; the assembly equivalent queries
`metadata.assembly_accession` narrowed to `role: REFERENCE`, so "you already
have this genome" is not answered yes by a stray protein FASTA from the same
assembly. Because `assembly_accession` is also what ingest enrichment writes
for an uploaded reference, a hand-uploaded `GCF_..._genomic.fna` correctly
counts as already present -- the same property that makes downloaded and
uploaded references interchangeable everywhere else.

No mate linking. No QC chaining.

### Three new roles

`ObjectRole` extends, as its docstring anticipates. Each gains a comment in
the established style, stating what format alone cannot say:

- **`ANNOTATION`** -- a GFF3/GTF that is the authoritative annotation for an
  assembly. Format says "intervals"; it cannot distinguish NCBI's annotation
  from a user's peak calls or blacklist.
- **`PROTEIN`** -- amino acid FASTA. The role that matters most: a protein
  FASTA and a reference genome are both `FormatKind.FASTA`, and only the role
  keeps it out of the reference dropdown.
- **`TRANSCRIPT`** -- CDS/transcript nucleotide FASTA. Same hazard as
  `PROTEIN`, and worse in one way: `cds_from_genomic.fna` is nucleotide FASTA
  that would pass a "does this look like a genome" sniff test.

`schemas.py`:

- `ANNOTATION` joins `FORMAT_DERIVED_ROLES`. A GFF3's questions are
  `INTERVAL_FIELDS`, which already exist and already fit; a second interval
  vocabulary would be worse than sharing one.
- `PROTEIN` and `TRANSCRIPT` share a new `SEQUENCE_SET_FIELDS` in
  `ROLE_FIELDS` -- organism, assembly accession, sequence count, source.
  They deliberately do not get `REFERENCE_FIELDS`: asking a protein FASTA for
  its assembly level and scaffold N50 is asking about a different object.

The existing "every role is accounted for" invariant -- that each member is in
either `ROLE_FIELDS` or `FORMAT_DERIVED_ROLES` -- is what keeps a fourth role
from being added thoughtlessly, and is asserted in tests.

**Frontend.** `ProjectExplorer` and `DetailPanel` label off role for
`REFERENCE` today; the three new roles get labels and badge colors in the same
place. `FactsTable` needs no change -- it renders whatever facts exist, and
these carry the `ncbi_*` facts `to_facts()` already produces.

## The dialog

`SraDownloadDialog.tsx` becomes `NcbiDownloadDialog.tsx`. Title "Download from
NCBI", placeholder `SRR11768093, PRJNA1495534, GCF_000002445.2…`, hint line
naming every accepted type. One box, one button; the *result* shape varies.

The platform filter hides when the resolved kind is `assembly` -- it has no
meaning for a genome, and a disabled control invites the question of what it
would do.

**Assembly result:** a summary card (organism italicized, strain, assembly
name and level, submitter, release date, total length, scaffold count, N50)
above the component checkboxes with per-component sizes and a running total.
`already_downloaded` marks a reference the project holds, with the same "have"
tag and the same not-disabled treatment the run rows use -- re-downloading a
corrupted file is legitimate.

**Run result:** the current table, plus grouping below.

### Collapsible experiment grouping

Grouping is derived on the frontend from `run.experiment`, which every
`RunInfo` already carries. `build_hierarchy()` is left alone: its sample-level
grouping is defensible on its own terms, it is baked into the cached response
shape, and changing it would invalidate an hour of Redis entries to compute
something the runs already state.

Active only when the resolved kind is `bioproject` or `study` **and** there is
more than one distinct experiment. A single SRR, or a sample with one
experiment, renders flat exactly as today -- a lone collapse control around
every row is noise.

Each group is a header row spanning the table: a tri-state checkbox (all /
none / some of the group selected), the experiment accession, its title, run
count, summed size, and a disclosure caret. All groups start expanded with
everything preselected, preserving current behavior; collapsing is for
scanning a large project, not for hiding the default.

**Pagination** switches from 20 runs to 5 whole groups per page when grouped.
A group split across a page boundary is the confusing case, and whole
experiments per page is the unit the user is now reasoning in.

**Sorting** re-sorts within groups rather than dissolving them. A sort click
that silently flattened the grouping would make the control feel unreliable.

`MAX_SELECTION = 100` and its warning are unchanged. A group-header click can
exceed it; the existing warning already says so.

**The worked example.** Paste `PRJNA1495534`: six experiment groups, all runs
selected. Uncheck four group headers. The footer reads the remaining count
across the two selected groups, runs still labeled by experiment throughout.

## Tools and image

`tools.py` gains `datasets()` following the existing probe pattern, so it
appears in the tools panel with a version like every other dependency.
`datasets --version` prints `datasets version: 18.30.1`, which
`_clean_version` handles.

The worker image installs it -- a single static Go binary from NCBI's LATEST
endpoint, matching the image's architecture. Confirmed absent today, so this
is a required Dockerfile change, and `tools.require()` produces a clear error
rather than a confusing subprocess failure if it is ever missing.

`RunKind` gains `ASSEMBLY_DOWNLOAD`. Separate from `SRA_DOWNLOAD` because it
is a display and grouping vocabulary where the two read differently in the
activity view, and `_download_label` for an assembly says "Download
GCF_000002445.2 from NCBI".

## Testing

Backend, in the `api` container per CLAUDE.md:

- `tests/metadata/test_assembly_components.py` -- component detection from
  `--preview` JSON fixtures: fully annotated GCF, genome-only GCA with all
  `file_count: 0`, empty `{}` not-found, and the `annotation_info` fallback
  when the CLI is unavailable.
- `tests/queue/test_assembly_download.py` -- against a fixture zip built to
  the verified layout: labeling from `dataset_catalog.json`, the filename
  fallback, and specifically that `cds_from_genomic.fna` becomes `TRANSCRIPT`
  and not `REFERENCE`. That assertion is the regression test for the trap this
  design is built around.
- `tests/metadata/test_schemas.py` -- every `ObjectRole` member is in exactly
  one of `ROLE_FIELDS` or `FORMAT_DERIVED_ROLES`.
- `tests/services/test_assembly_service.py` -- launch validation: a
  non-assembly accession rejected, genome always included even if the request
  omits it, dedup key behavior.

Frontend is manual at localhost:5173, per CLAUDE.md -- there is no
component-testing setup and none is expected. Worth exercising: a GCF with
everything, a GCA with genome only (checking the paired-accession message), a
plain SRR, `PRJNA1495534`'s grouping and group-level selection, and a
re-resolve of an assembly already downloaded.

After any handler change, `docker compose restart worker` from the main repo
root before re-testing -- the worker does not hot-reload, and a stale handler
reads as "the fix didn't work".

## Risks

**The `.fna` collision.** Both genome and CDS are `.fna` in one directory.
Mitigated by preferring `dataset_catalog.json`, ordering the filename fallback
so `cds_from_genomic.fna` matches first, and asserting it in a test.

**Large assemblies.** GRCh38 genome alone previews at 928 MB. The pre-flight
uses `--preview`'s exact figure, and the dialog shows per-component sizes and
a total before the user commits.

**`datasets` output format changes.** `--preview` JSON and
`dataset_catalog.json` are both parsed. Both are versioned interfaces
(`"apiVersion": "V2"`), and the parsers degrade to filename matching and to
`annotation_info` rather than failing the download.

**Roles built ahead of consumers.** `ANNOTATION`, `PROTEIN` and `TRANSCRIPT`
will have no pipeline reading them for some time. Accepted deliberately: the
point is to accumulate the files now with their identity recorded, and role is
the field the explorer and `fields_for` already key on, so the infrastructure
is used for identification from day one even with no compute attached.
