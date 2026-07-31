# UniProt download

Closes the `## UniProt download` entry in `docs/TODO.md`.

Downloads UniProt protein data into a project as a stored object, the way
assemblies download from NCBI today. One dialog, one smart accession field,
four kinds of input; one handler and one `RunKind` behind it.

## Why a separate dialog

`NcbiDownloadDialog` already branches on one accession field between an SRA
run picker and an assembly card, and the namespaces do not collide, so folding
UniProt in was possible. It is not done, for two reasons. That component is 762
lines carrying two result shapes and would carry four. And the NCBI resolver's
question -- "is this SRA or assembly?" -- is coherent because it is about one
provider; adding "or is it UniProt?" makes one field the door to everything and
makes the failure modes harder to reason about.

`UniProtDownloadDialog` therefore copies the *style* -- one field, resolve,
then a card or a picker -- into its own component with its own button.

The cross-link survives without the merge. A proteome record names its own
genome assembly (`genomeAssembly.assemblyId`, `GCA_000146045.2` for yeast
S288c), so the proteome card links to the assembly rather than offering a
combined download.

## What the resolver does

Four input classes. Classification is by shape first, then by what UniProt
actually returns.

| Input | Test | Result |
| --- | --- | --- |
| `UP000002311` | `^UP\d{9}$` | proteome card |
| `P0DTC2`, `P00533 P0DTC2` | UniProtKB accession pattern, one or more tokens | protein picker, all pre-selected |
| `4932`, `Saccharomyces cerevisiae` | all digits, or a proteome search that hits | reference proteome card, plus an "N other proteomes" disclosure |
| `spike glycoprotein` | anything else | protein picker, search results, none pre-selected |

The accession pattern is UniProt's own documented one:
`[OPQ][0-9][A-Z0-9]{3}[0-9]` or `[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}`.
Verified to classify `P0DTC2`, `P00533`, `A0A0B7P3V8`, and `Q8JSP8` as
accessions while leaving `EGFR`, `GCF_000002445.2`, and `SRR11768093` to the
text branch.

Organism-versus-free-text is the only genuinely ambiguous pair, and it is
settled by asking: run the proteome search first, and fall back to a protein
search when it returns nothing. One extra request, degrading toward the more
general answer.

### The taxon fallback is mandatory, not defensive

Measured against the live API, and this is the part most likely to be got
wrong:

- `organism_id:4932 AND reference:true` returns **0**.
- `organism_id:4932` returns **360**.

Taxon 4932 is *S. cerevisiae* at the species level, and UniProt attaches the
reference proteome to the strain taxon 559292 instead. A resolver that queried
only `reference:true` would report that yeast has no proteome while 360 sit
behind it -- for the taxon ID a user is most likely to type.

So the organism path is a chain: `reference:true` first, and when that is
empty, the unfiltered organism query with the picker opened rather than the
card. `559292` and `9606` both return exactly 1 for `reference:true` and take
the short path.

### The filter is `reference:true`

Not `proteome_type:1`. The latter looks plausible, appears in older examples,
and returns **0** for every organism tried -- including `559292`, which does
have a reference proteome. Verified: `organism_id:9606 AND reference:true`
returns UP000005640.

## Reviewed versus unreviewed

The card shows both counts and a checkbox, defaulting to reviewed-only.

This matters more than it looks. Human is **~20,400 reviewed** against
**147,506 including unreviewed** -- roughly sevenfold, and the difference
between "the human proteome" as most people mean it and every TrEMBL fragment.
Yeast hides the problem entirely at 6,067 either way.

This deviates from `structure_lookup.py`, which hardcodes `reviewed:true` and
documents at length why. The deviation is deliberate and the reasoning does not
transfer: that module *auto-selects one protein* to display, where a wrong pick
is silent and harmful. Here the user downloads a labelled file they will look
at. And that module's own warning -- "for an organism with no SwissProt
coverage it will resolve almost nothing" -- describes exactly what a hardcoded
`reviewed:true` would do to this feature for the non-model organisms people
download proteomes for.

Showing both counts costs one extra search request and puts the sevenfold in
front of the user at the moment of choice.

## One job, not two

A whole proteome and a hand-picked set are the same request:

```
/uniprotkb/stream?query=<q>&format=fasta&compressed=true
```

Only the query differs -- `proteome:UP000002311` against
`accession:P0DTC2 OR accession:P00533`. Verified: the picked-set form returns
exactly the three requested records. So the dialog branches and the job does
not. One handler, one `RunKind.UNIPROT_DOWNLOAD`, distinguished in the run
label rather than the enum.

`RunKind` gains one member. It is a display and grouping vocabulary, and
"downloaded a proteome" reads differently from "downloaded a genome" -- the
same argument that separated `ASSEMBLY_DOWNLOAD` from `SRA_DOWNLOAD`.

## Components

```
UniProtDownloadDialog.tsx    one field, four input classes
  -> POST /api/v1/uniprot/resolve
uniprot_resolver.py          classify, query, return a card or a picker
  -> POST /api/v1/uniprot/download
uniprot_service.py           validate, create the run, enqueue one job
  -> queue
uniprot_handlers.py          stream FASTA to tmp/, return a staged description
  -> applier
results.py                   _apply_uniprot_download, ingest as PROTEIN
```

The service/handler/applier split, the DB-free handler, the `dedup_key` on
(query, project), the `discard_run` when a job dedups away, provenance in
`facts` and biology in `metadata`, and per-file ingest failures that never lose
the transfer all come straight from `assembly_service` and `assembly_handlers`.

## What must not be copied from the assembly download

The TODO says a UniProt download is "the same shape as the assembly one." That
is true of the *structure* and false of the *mechanics*: `assembly_handlers.py`
is built around shelling out to a binary and guarding a multi-gigabyte
transfer, and none of that applies. Copying it wholesale would import five
things that are wrong here.

- **No `HandlerMode.SUBPROCESS`, no `run_subprocess`, no `tools.require`.**
  There is no binary. This is an HTTP GET, and the closest existing model for
  the transport is `structure_lookup.py`, not `assembly_handlers.py`.
- **No `extend_lease(3600)`.** That exists because a large assembly is a long
  transfer with no output for minutes. A yeast proteome is 3.9 MB.
- **No disk-space pre-flight.** The worst realistic case is human with TrEMBL
  at roughly 75 MB.
- **No `EXTRACTION_FACTOR`.** The assembly handler multiplies by 2.5 to guess
  at a zip expanding. Here `X-Total-Results` gives an *exact* protein count
  before the download, so any guard is on a known count rather than an
  estimated byte multiplier.
- **No zip handling, no checksum manifest, no path-traversal check.** The
  response is a gzipped FASTA stream, not an archive that writes files.

## Sizes, measured

| Case | Proteins | Compressed | Uncompressed |
| --- | --- | --- | --- |
| Yeast S288c (UP000002311) | 6,067 | 1.9 MB | 3.9 MB |
| Human reviewed (UP000005640) | 20,427 | 7.5 MB | 13.7 MB |
| Human with TrEMBL | 147,506 | not measured | not measured |

The first two rows are measured by downloading them. The human-with-TrEMBL row
is left unmeasured rather than extrapolated: scaling yeast's bytes-per-protein
to human predicted ~10 MB for the reviewed set against an actual 13.7 MB,
because human proteins are longer on average, so the same arithmetic applied to
147,506 entries would be a fabricated number in a table that otherwise reports
measurements. Its protein count is exact and is what the guard uses.

A confirmation prompt above a protein-count threshold is worth having, since
`X-Total-Results` makes it exact. The threshold is an implementation detail;
the human-with-TrEMBL case is the one it exists to catch.

`X-Total-Results` and the streamed record count are close but not identical --
the human reviewed set reported 20,416 and delivered 20,427. Treat the header
as sizing information rather than a post-download assertion; a handler that
failed the job when the two disagreed would fail on a correct download.

## Ingest

Role is `ObjectRole.PROTEIN`, which already exists and whose docstring names
this exact hazard: "a protein FASTA and a reference genome are both
`FormatKind.FASTA`, and only this keeps one out of the aligner's reference
picker."

Checked rather than assumed, per CLAUDE.md: `suggestion_service.py` needs no
change. Its align rule filters on `o.role is ObjectRole.REFERENCE`, so an
object ingested as `PROTEIN` is already excluded -- the same filter that
comment says was added because a downloaded assembly's `protein.faa` and
`cds_from_genomic.fna` made a project with one real reference look like it had
four. A downloaded proteome lands on the safe side of a guard that is already
there. No Actions card's `unavailable` reason stops being true, because
proteomes are not an input to any pipeline here today.

Facts carry provenance: the query, the proteome ID where there was one, whether
unreviewed entries were included, the protein count, and the UniProt release.

## The UniProt release, and what it means for `sources.py`

`sources.py` needs a UniProt entry, and it has a completeness test, so this is
not optional.

Its docstring argues that a data source has no version -- "there is no build
number to pin, and a page that showed one anyway would look like it was
reporting provenance without actually having any." UniProt returns
`X-UniProt-Release: 2026_02`, which is exactly a build number.

The resolution keeps that reasoning intact rather than quietly breaking it: the
release is recorded per-download in the object's `facts`, where it is real
provenance about specific bytes, and `DataSource` gains no version field, where
the docstring's argument still holds. What is true of a *source* and what is
true of a *download from it* are different claims.

`usage` must describe behaviour rather than endpoints, per CLAUDE.md, and the
licence and citation must be checked against UniProt itself rather than
recalled.

## Testing

Backend tests run in the container:
`docker compose exec api python -m pytest tests/ -q`.

- The resolver's classification, including the cases that motivated it:
  `EGFR` and `GCF_000002445.2` reaching the text branch rather than the
  accession one.
- The taxon fallback: taxon 4932 must reach the picker and not report "no
  proteome," which is the failure a `reference:true`-only query produces.
- Reviewed and unreviewed producing different queries and different counts.
- One handler serving both query shapes.
- `sources.py`'s existing completeness test, which the new entry must satisfy.

Per CLAUDE.md, the rules also get checked against a real project rather than
only against fixtures -- hand-built objects that already look the way the rules
expect are how the suggestion rules passed green while getting two things
wrong.

`worker` does not hot-reload; `docker compose restart worker` is required
before re-testing the job. The dialog is verified by hand at localhost:5173,
which is the actual verification step for anything UI-facing here.

## Out of scope

- Any pipeline that consumes a proteome. Nothing here does today; this is a
  download, and BUSCO or annotation work against it is a separate entry.
- Treating `AlphaFoldDB` cross-references as structures. `structure_lookup.py`
  deliberately declines to, and that judgement is unchanged.
- Combining the proteome and its genome assembly into one download. The card
  links to the assembly; the merge was considered and rejected above.
