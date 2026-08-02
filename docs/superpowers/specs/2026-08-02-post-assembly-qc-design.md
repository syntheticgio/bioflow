# Post-assembly QC

Written 2026-08-02, the day de novo assembly shipped. Closes the "Post-assembly
QC: BUSCO and QUAST" backlog entry, which named two tools; this designs
something narrower than that entry and wider than it at the same time, for
reasons the first section explains.

## Problem

Assembly produces a FASTA. The immediate question is whether it is any good,
and nothing here answers it. `qc_stats` is about reads. Alignment QC needs
something to align to. `parse_assembly_info` reports what Flye's own table
says, which is real but is neither independent of the assembler nor available
for an assembly that Flye did not produce.

Two numbers answer the question for most people:

- **How contiguous is it** -- N50, contig count, gaps.
- **How complete is it** -- what fraction of the genes that ought to be there
  can be found.

Everything else on the modern assembly-QC menu answers a third question, "is it
*correct*", and that one is deferred here for a structural reason given at the
end.

## Verified before writing this

Package availability was read out of the running `api` container (Debian 13
trixie), not recalled. It is the single largest input to this design and it
does not match what a survey of the field would suggest:

| Tool | trixie | Note |
|---|---|---|
| `busco` | **yes**, 5.5.0-3 | `Depends:` pandas, biopython, hmmer, prodigal, bbmap |
| `quast` | **no** | not in the archive; only *referred to* by `med-bio` and `multiqc`. `apt-get install quast` reports "no installation candidate" |
| `gfastats` | no | source build |
| `compleasm` | no | and `miniprot`, which it needs, is not packaged either |
| `merqury`, `fastk`, `purge-dups` | no | |
| `seqkit`, `kraken2`, `metaeuk`, `augustus` | yes | 2.9.0, 2.1.3, 7-bba0d80, 3.5.0 |

Two traps fell out of that check, both of the kind that produce a green install
and a wrong result:

- **`meryl` is in trixie and is the wrong meryl.** Candidate
  `0~20150903+r2013-9+b1` is the Celera Assembler k-mer suite. Merqury needs
  Marbl meryl 1.3 or newer. `shutil.which("meryl")` succeeds against the wrong
  one, `_probe` scrapes a version out of it, and the tool panel reports an
  available tool that cannot do the job. Whoever eventually builds Merqury here
  must not reach for the apt package.
- **Debian's BUSCO cannot do eukaryotes as installed.** Its dependencies bring
  `prodigal`, which is prokaryotic gene finding. `metaeuk` is a separate
  package and is not pulled in. A eukaryotic lineage therefore fails at run
  time on an install that looked complete.

Also verified, because it changes what "install a tool" can mean here: the
sibling-container route is no longer hypothetical. `backend/Dockerfile` ships a
static Docker CLI, `docker-compose.override.yml` mounts the host socket into
`api` and `worker`, and `variant_handlers._run_deepvariant` runs DeepVariant
from its own image with `_require_image` pre-flighting the pull.
`tools.deepvariant` shows how such a tool probes: ask the daemon, report the
image tag as the version. It is available. It turns out not to be the right
answer for compleasm, for a reason in "Installing compleasm" below.

And one thing verified about this repo rather than about tools:
**`assembly_runner.py` has no unit tests.** The assembly design's own testing
section lists "`assembly_info.txt` parsing into facts, including the
malformed-file case" among what it would cover; `grep` for `parse_assembly_info`
and `assembly_runner` across `backend/tests/` returns nothing. Assembly is
tested at the launch and card level only (`test_assembly_launch.py`,
`test_suggestion_service.py`). This matters because the contiguity change below
modifies `parse_assembly_info`, and there is no existing test to tell us if we
break it.

## Why not the five tools the survey named

The field has moved past BUSCO and QUAST, and the tools it moved to are
gfastats, compleasm, CRAQ, Merqury and GCI. Under this repo's constraints that
ordering partly inverts:

- **Zero of the five are apt-installable**, against BUSCO which is one line.
  The framing "BUSCO and QUAST are the legacy option" is true of the field and
  not true of the cost here.
- **QUAST is not the cheap half of the entry's own title.** It is not packaged
  at all, so it costs a build exactly like gfastats does, while most of what it
  computes we can compute ourselves.
- **CRAQ, GCI and Merqury are not peers of the other two.** All three work by
  relating reads back to the assembly, which needs the assembly indexed and the
  reads realigned against it -- the same prerequisite the Pilon entry already
  identifies as "the first pipeline whose input is an alignment to a previous
  pipeline's output". They are gated on that, not on assembly, and building
  them here would mean building that first.

So this design takes contiguity and completeness, and takes them by the
cheapest honest route rather than the most modern one.

## Contiguity: computed here, not shipped in

**No tool.** N50 over a FASTA is arithmetic over a list of lengths, and
`_parse_fasta` already walks every record of every FASTA at ingest.

The facts, under the parser's existing `sequence_` prefix:

```
sequence_n50  sequence_n90  sequence_l50  sequence_auN
sequence_gap_count  sequence_gap_bases
```

`auN` is included because it is the one contiguity number that does not depend
on an arbitrary threshold -- N50 moves discontinuously when one contig crosses
the halfway point, and two assemblies with the same N50 can differ a lot.

Three implementation notes that are not obvious from the outside:

- **The lengths list cannot be the stored one.** `_parse_fasta` caps
  `sequence_lengths` at `MAX_STORED_CONTIGS` (50), which is the right cap for a
  fact document and the wrong input for N50. Accumulate every length in a local
  list and store only the summary; a million-contig assembly is about 8 MB of
  Python ints for the duration of one parse, which is acceptable and bounded.
- **Emit nothing when the parse truncated.** The parser stops at 256 MB for
  uncompressed input and sets `sequence_lengths_partial`. An N50 over a prefix
  of an assembly is not an approximate N50, it is a different number, and the
  parser's existing rule -- "lengths are never extrapolated: there is no sound
  way to guess an unseen contig's length from a byte ratio" -- applies here
  with more force. Omit the keys entirely rather than emit a flagged wrong
  value.
- **Gaps need the sequence bytes, which the loop already has.** Today the
  non-header branch only takes `len(line)`. Counting `N`/`n` runs means looking
  at the characters, which costs a scan of data already in cache.

**`assembly_n50` goes away.** `parse_assembly_info` currently computes N50 from
Flye's `assembly_info.txt` while the ingest parse computes facts from the FASTA
bytes, and after this change both would land on the same object. Two N50 facts
that are *supposed* to agree is a bug with a delay fuse. The parser wins: it is
independent of the assembler and it works for the uploaded assemblies that
decision 3 puts in scope. Flye's table keeps coverage and circularity, which
are the things it uniquely knows and no FASTA parse can produce.

Nothing renders `assembly_n50` by name -- `AssemblyFacts.tsx` does not
reference it and no test asserts it -- so removing it is a change to the fact
document and not to any consumer. `FactsTable`'s generic path picks up the new
`sequence_*` keys; they want entries in its `LABELS` map so they read as
"Contig N50" rather than "Sequence n50".

**Why not gfastats.** It is MIT, it is a clean C++ `make`, and it is the ERGA
and VGP standard, so this is not a judgement about the tool. It is that we
would be adding a build and an image dependency to obtain numbers we can
compute in a function, and doing so would reintroduce the two-sources problem
we just resolved. Its genuine advantages -- one pass over GFA, seconds on
multi-Gb genomes -- are not constraints this application has today. Revisit
when GFA statistics are wanted in their own right; the assembly graph is
already a first-class object, so that is a plausible future.

## Completeness: compleasm

compleasm rather than BUSCO, which is a deliberate choice against the cheaper
install:

- It is roughly 10-20x faster, and it recovers BUSCOs that BUSCO's metaeuk step
  misses (Huang & Li 2023).
- It sidesteps the metaeuk trap above by construction: alignment is miniprot,
  so there is no separate eukaryotic gene finder to forget to install.
- Its dependency set is smaller than Debian BUSCO's, which pulls bbmap and
  therefore Java.

BUSCO stays declared in the registry as an unavailable spec, the way
`HIFIASM_SPEC` is declared in `assembler_registry`. It is the thing a reviewer
asks for by name, and having somewhere to point when that happens is worth the
handful of lines.

### Installing compleasm: the release tarball is a trap

The obvious install routes are both x86-only, and taking either would repeat
the bwa-mem2 arm64 failure exactly:

- The only asset on the latest release (v0.2.9) is
  `compleasm-0.2.9_x64-linux.tar.bz2`. There is no arm64 asset.
- The biocontainer `quay.io/biocontainers/compleasm:0.2.9--pyhdfd78af_0` is
  333 MB and its registry entry reports `is_manifest_list: false` -- a single
  amd64 image, not a multi-arch manifest. So the sibling-container route that
  works for DeepVariant does not work for this tool.

The portable route, and the one to take:

- **miniprot from source.** MIT, plain `make`, and its own documentation says
  it "requires SSE2 or NEON instructions and only works on x86_64 or ARM CPUs"
  -- NEON is native upstream support, so unlike bwa-mem2 there is nothing to
  patch and no sse2neon to vendor. One small binary.
- **hmmer from apt.** Already in trixie.
- **pandas** is already in the image, pulled in by NanoPlot and PyDESeq2.
- **compleasm itself from the source tree**, not the x64 tarball. bioconda
  packages it as `noarch`, which confirms the Python half is
  architecture-independent; the x86 in the release asset is only the bundled
  binaries, which we are supplying ourselves.

compleasm resolves `miniprot` and `hmmer` off `PATH`, so this arrangement is
what it expects.

**No sepp.** It is required only for autolineage mode, and autolineage is not
what we want anyway -- see below. That removes the one dependency with no
Debian candidate.

`TOOL_META` per CLAUDE.md: Apache-2.0 (the repo also carries a `LICENSE-BUSCO`
alongside it, which is worth reading before writing the license field), the
Huang & Li 2023 *Bioinformatics* paper for `citation`, and a `usage` string
that describes behaviour. Verify all of it against the project's own repository
at implementation time rather than from this document.

### Lineage datasets are reference data, not part of the tool

compleasm's lineage databases are downloaded, sized from single-digit MB to a
few hundred MB, and versioned. That makes them much closer to the reference
downloads this app already models than to anything in `tools.py`, and it
creates one hard constraint: **a QC job must not depend on the network.** That
is the rule Clair3's baked-in models exist to satisfy, and an implicit download
mid-job would quietly break it.

So:

- A `download_lineage` job, modelled on the NCBI download handlers, writing
  under `BIOINFO_HOME`. It is the thing the user waits on once.
- The completeness job requires the dataset to be present locally and fails
  with an actionable message when it is not, the way `_require_image` does for
  DeepVariant.
- **The lineage is chosen from organism metadata, not auto-detected.** BioFlow
  already carries organism from SRA, NCBI and UniProt enrichment; mapping that
  to a lineage is a lookup. Autolineage would download several datasets to
  decide, which is the expensive way to answer a question we mostly already
  know the answer to. The dialog shows the inferred lineage and lets the user
  change it -- the same "inferred, labelled as inferred, overridable" shape
  genome size uses in the assemble dialog.
- **An assembly with no organism metadata is a normal case**, not an error.
  Uploaded assemblies often have none. The dialog then requires the user to
  pick a lineage, which is an honest refusal rather than a guess.

**ODB version is provenance.** compleasm supports odb10, odb12 and odb12.2 and
defaults to odb12; BUSCO 5.5 is odb10. A completeness percentage from one is
not comparable to one from another, including to a later run of this same
application. The version is recorded in the facts, not just in the download.

## The registry

`backend/app/pipelines/assembly_qc_registry.py`, in the shape
`assembler_registry` established -- one spec per tool, `spec_for()` as the
accessor, `ParamField` imported rather than redeclared so the dialog renders
through the same component.

The seam this buys is the one the entry actually needs later: CRAQ, Merqury and
BUSCO all become specs rather than edits spread across a runner, a handler and
a card.

**Patch `spec_for`, not `tools.compleasm`.** The spec is a frozen dataclass
that captures the probe as a function object at import time, so patching the
module attribute never reaches `spec.tool`. This is written down in both
existing registries and is repeated here because it is the mistake that
produces a test which silently reads the host machine while appearing to
control it.

## One job per tool

`assess_completeness` is its own handler and its own job, not a member of a
combined `assess_assembly`. Two reasons, both practical:

- The runtimes are not comparable. A bacterial compleasm run is minutes; a
  vertebrate one is hours. Bundling means a card whose progress means nothing.
- A failure should be scoped. Completeness failing must not lose contiguity --
  and under this design it structurally cannot, because contiguity is not a job
  at all.

That is the pleasant consequence of computing contiguity in the parser: the
cheap half needs no queue work, no card, and no user action. It simply appears
on every FASTA at ingest, including the ones already in the library once they
are re-parsed.

`ingest_headers` is already idempotent ("parsing is a pure read, so a repeat
run recomputes the same facts"), so backfilling existing assemblies is a
re-enqueue rather than a migration.

## Facts

Namespace `assembly_completeness_*`, tool-agnostic so a later BUSCO or a
re-run under a different ODB writes the same keys:

```
assembly_completeness_tool          "compleasm"
assembly_completeness_tool_version
assembly_completeness_lineage       "bacteria_odb12"
assembly_completeness_odb           "odb12"
assembly_completeness_single_pct
assembly_completeness_duplicated_pct
assembly_completeness_fragmented_pct
assembly_completeness_missing_pct
assembly_completeness_complete_pct  single + duplicated
assembly_completeness_total         count of BUSCOs in the set
```

`busco_score` is deliberately **not** used: it is already taken by UniProt
proteome metadata (`app/api/v1/uniprot.py`, `app/metadata/uniprot.py`), where it
means the completeness of a *proteome* NCBI computed, not of an assembly we
measured. An object can plausibly carry both.

Storing all four categories rather than one headline number is the point.
Duplicated percentage is the signal for haplotypic duplication, which is a
different problem with a different fix from a low complete percentage, and a
single "97.3%" throws it away.

## The card

`build_assess_completeness_card` in `suggestion_service.py`, which per CLAUDE.md
is the half of adding a tool that is easy to skip and silently makes the tool
unreachable.

**It offers on any assembly-shaped FASTA, not only on ones we produced.** That
is decision 3 and it is the reason the card cannot key on `produced_by_job` or
on assembly provenance. An uploaded assembly is a first-class input here.

The rules, and the traps each one exists for:

- FASTA format, and role `REFERENCE` or unset.
- **Exclude roles `PROTEIN` and `TRANSCRIPT`.** `protein.faa` and
  `cds_from_genomic.fna` are FASTA and would pass any "does this look like a
  genome" sniff test. This is the exact failure CLAUDE.md records the align
  card having shipped with a green suite.
- Unavailable when compleasm is not installed, with the reason naming the tool.
- Unavailable when no lineage dataset is present *and* no organism can be
  inferred, with a reason that says which of the two is missing -- those are
  different problems and the user can fix each differently.

Unlike the align card, this needs no `_distinct_assemblies` deduplication: the
card is per-object, and running completeness twice on two copies of the same
assembly is wasteful rather than wrong.

## Files this touches

- `backend/app/storage/parsers.py` -- `_parse_fasta`: full length list, gaps,
  the truncation rule
- `backend/app/storage/sequence_stats.py` -- or here instead, if the N50 family
  reads better beside the composition stats
- `backend/app/pipelines/assembly_runner.py` -- drop `_n50` and
  `assembly_n50`
- `backend/app/pipelines/assembly_qc_registry.py` -- new
- `backend/app/pipelines/completeness_runner.py` -- new: command building and
  output parsing, pure functions over strings and paths
- `backend/app/pipelines/tools.py` -- `compleasm()` probe, `TOOL_META` entry
- `backend/app/queue/assembly_qc_handlers.py` -- new: `assess_completeness`
- `backend/app/queue/lineage_handlers.py` -- new: `download_lineage`
- `backend/app/queue/results.py` -- apply function writing the facts
- `backend/app/services/pipeline_service.py` -- launch path
- `backend/app/services/suggestion_service.py` -- the card
- `backend/Dockerfile` -- hmmer from apt, miniprot from source, compleasm from
  source
- `frontend/src/components/FactsTable.tsx` -- labels for the new keys
- `frontend/src/components/AssemblyFacts.tsx` -- completeness display
- `frontend/src/api/types.ts`,
  `frontend/src/components/PipelineToolSelector.tsx`,
  `frontend/src/components/HelpSoftware.tsx` (+ its test) -- the new
  `PipelineType` member and its label
- new dialog component for lineage selection

`PipelineType` gets a new member, `ASSEMBLY_QC`, and compleasm is declared
`pipelines=(PipelineType.ASSEMBLY_QC,)`.

This reverses what an earlier draft of this document said, and the reason it
was wrong is worth more than the conclusion. `grep` for `PipelineType.` across
`backend/app/` returns hits in exactly one file, `tools.py`, which reads as
"this is a display grouping on the Software help page and nothing more". It is
not: the value is serialized to the frontend, and `PipelineToolSelector.tsx`
filters on it -- `tools.filter((t) => t.pipelines.includes(pipeline))` -- to
build the picker a user actually chooses a tool from. A backend-only grep
cannot see that, because the consumer is across the API boundary. Declaring
compleasm as `ASSEMBLE` would have listed it in the picker headed "an
assembler", beside Flye, as something to assemble *with*.

The cost of the new member is small and self-announcing.
`PipelineToolSelector`'s `PIPELINE_LABEL` is a `Record<PipelineType, string>`
and is deliberately exhaustive -- its own comment says "the compile error is
the feature", and records that it is what caught `expression` reaching the
backend without reaching the frontend at all. So adding the member fails the
build until a label exists. `HelpSoftware.tsx` carries a hand-ordered list of
the types and a test over it, which needs the same one-line addition.

## Testing

Unit, all without a container or a binary:

- N50 / N90 / L50 / auN over hand-built length lists, including the
  single-contig and empty cases
- **the truncated-parse case**, asserting the keys are absent rather than
  wrong. This is the one that fails if someone later "fixes" the omission by
  extrapolating.
- gap counting, including a sequence that is entirely N and one with no gaps
- compleasm output parsing, including a malformed summary -- the same
  swallow-and-continue posture `parse_assembly_info` takes, since a completeness
  parse failing must not fail a job that ran for hours
- lineage inference from organism, including the no-organism case
- **card availability in the failing direction**, patching `spec_for` off. The
  image ships tools as installed, so asserting the card is *available* passes
  whether or not the patch worked.
- card refusal for `protein.faa` and `cds_from_genomic.fna`

While here, and separately from the feature: `assembly_runner.py` has no tests
at all. The contiguity change edits it. Adding coverage for
`parse_assembly_info`, `build_assembly_command` and `AssemblyProgress` is a
small commit that should land before or alongside the edit rather than after.

Against the real database, because a green suite has already been wrong about a
card in this application twice:

- `docker compose exec api python -c "..."` over the real project objects, to
  see what the card actually says for every FASTA in the library -- the Flye
  assembly, the downloaded GCF references, `protein.faa`,
  `cds_from_genomic.fna`
- one real compleasm run on the bacterial assembly, checking the facts land,
  the lineage and ODB version are recorded, and the numbers are plausible
  against what is published for that organism

## What this does not include, and why

- **QUAST.** Not in trixie, so it costs a build; most of what it computes
  reference-free is what the contiguity section computes. Its genuinely
  unreplaceable capability is *reference-based* misassembly detection, and this
  application is unusually well placed for that -- it already downloads NCBI
  assemblies and already resolves a reference per project. That deserves its
  own entry rather than being smuggled in here.
- **gfastats.** Superseded by computing contiguity in the parser. See above.
- **CRAQ, GCI, Merqury.** All three need reads realigned to the assembly.
  That is the Pilon blocker, not this one. Merqury additionally wants the *raw*
  reads: the k-mer spectrum is deflated by trimming, and this application's
  provenance graph does know whether the reads that produced an assembly were
  `TRIMMED_READS`, so building it later means using that rather than taking
  whatever reads are nearest. And it must not use Debian's `meryl`.
- **Contamination screening.** Named as a non-goal rather than left as a hole,
  because it is a real axis of assembly QC that none of the tools discussed
  here touch, and anything headed for a submission needs it. NCBI's FCS-GX is
  the standard and its database is around 470 GB, which is not a thing this
  application can carry. Kraken2 is in trixie and its database is the same
  problem at smaller scale. Out of scope until someone wants it enough to
  accept the storage.
- **purge_dups.** The fix for the duplication that
  `assembly_completeness_duplicated_pct` reveals. It modifies an assembly
  rather than assessing one, so it belongs with Pilon and RagTag.
- **Telomere and gap-adjacency analysis** beyond the gap count. The "is this
  really T2T" question, which is GCI's territory and inherits GCI's blocker.
