# De novo assembly

Date: 2026-08-01.

Closes the assembly half of `docs/TODO.md`'s "Assembly: designed, not built"
(raised 2026-07-28), whose variant-calling half shipped 2026-07-29. Unblocks
two entries that name it as a dependency: "Post-assembly QC: BUSCO and QUAST"
and "Reference-guided assembly: Pilon, RagTag, iVar".

## Problem

`build_assemble_card` in `app/services/suggestion_service.py` has always
returned an UNAVAILABLE card reading "No assembler is installed." That is
still literally true -- `tools.py` declares no assembler at all -- and it is
the last card in the Actions tab that can never light up.

Everything the card needs already exists except the tools. Read chemistry is
inferred once in QC (`qc_stats.infer_chemistry`) and stamped as
`qc_read_chemistry`; assembler choice is chemistry-driven in exactly the way
aligner preset choice already is. The output is a FASTA, which the reference
and index machinery already handles. What is missing is a binary, a runner, a
registry, and a card rule.

## Scope

**Flye only, in this change.** hifiasm and SPAdes are designed for here but
not built:

- **Flye** is packaged in Debian trixie (2.9.5, 37 MB installed, depending on
  minimap2 and samtools which the image already carries). It covers ONT and
  PacBio CLR, and PacBio HiFi acceptably. One `apt-get install` line.
- **hifiasm** is *not* packaged for Debian. It needs a source build, and it is
  SSE-heavy C++ -- so it may need the same `sse2neon` treatment `bwa-mem2`
  already has a script for. That is a separate piece of work with its own
  arm64 risk, and it should not gate the first working assembly.
- **SPAdes** is packaged (3.15.5) but deferred by decision: short-read de novo
  is not a workflow this library needs yet, and it is the largest memory risk
  of the three.

The registry below is shaped for all three from the start, so adding either is
one spec plus a Dockerfile line rather than another five-file edit. That is
the same bet `aligner_registry.py` made and it paid.

## First: three unrelated things are called "assembly"

Before adding a fourth, the existing three get named. Today:

| Module | Actually means |
|---|---|
| `app/storage/assembly.py` | Reassembling *upload chunks* into a file |
| `app/metadata/assembly.py`, `assembly_components.py` | An NCBI published assembly's metadata |
| `app/services/assembly_service.py`, `app/queue/assembly_handlers.py` | *Downloading* an NCBI assembly |

Only the enum got this right: `RunKind.ASSEMBLY_DOWNLOAD` already exists and
says which one it is. The modules do not, and dropping a de novo assembler in
beside them makes the ambiguity permanent.

Renames, mechanical and done first as their own commit:

- `app/storage/assembly.py` → `app/storage/chunk_assembly.py`
- `app/metadata/assembly.py` → `app/metadata/ncbi_assembly.py`
- `app/metadata/assembly_components.py` → `app/metadata/ncbi_assembly_components.py`
- `app/services/assembly_service.py` → `app/services/ncbi_assembly_service.py`
- `app/queue/assembly_handlers.py` → `app/queue/ncbi_assembly_handlers.py`
- and the matching `backend/tests/` files

The name `app/queue/assembly_handlers.py` is then free and de novo takes it.
That is deliberate: it is the honest name for the new module, and a reader
who lands there from `git log` on the old path will find the rename commit
one step back.

## The registry

`app/pipelines/assembler_registry.py`, mirroring `aligner_registry.py`
closely enough that its docstring's argument applies unchanged -- one place
says what an assembler *is*, and the dialog's parameter form is generated
from the same declaration rather than hand-written per tool.

```python
class Assembler(StrEnum):
    FLYE = "flye"
    HIFIASM = "hifiasm"   # declared, not installed
    SPADES = "spades"     # declared, not installed


@dataclass(frozen=True)
class AssemblerSpec:
    assembler: Assembler
    tool: Callable[[], tools.Tool]
    # Which chemistries this assembler is valid for at all.
    chemistries: frozenset[align_runner.ReadChemistry]
    # Chemistry -> the tool's own mode flag. Flye's --nano-raw/--nano-hq/
    # --pacbio-raw/--pacbio-hifi is the same shape as _CHEMISTRY_PRESETS.
    mode_flags: dict[align_runner.ReadChemistry, str]
    layout: Literal["single", "paired"]
    params_class: type[assembly_params.BaseAssemblyParams]
    memory_model: AssemblyMemoryModel
    outputs: tuple[OutputSpec, ...]
    fields: tuple[ParamField, ...] = ()
```

`ParamField` and `Choice` are imported from `aligner_registry` rather than
redeclared -- the dialog renders both through the same `AlignerParamFields`
component, and two copies of the same dataclass is how the frontend ends up
with two renderers.

Chemistry dispatch, the third consumer of `qc_read_chemistry`:

| Chemistry | Assembler | Mode |
|---|---|---|
| `ONT_SIMPLEX` | Flye | `--nano-raw` |
| `ONT_DUPLEX` | Flye | `--nano-hq` |
| `CLR` | Flye | `--pacbio-raw` |
| `HIFI` | Flye | `--pacbio-hifi` (hifiasm when built) |
| `SHORT` | — | refuse: "Short-read assembly is not installed." |
| `UNKNOWN` | — | refuse, naming the missing fact |

`HIFI` routing to Flye today and to hifiasm later is a deliberate
non-permanent answer, and `spec_for_chemistry` is the one function that
changes when hifiasm lands.

## Genome size: inferred, warned about, and overridable

Flye no longer requires `--genome-size`, but the memory estimate does, and a
wrong estimate is the difference between a job that finishes overnight and
one the kernel kills at hour six.

Three sources, in order:

1. **A same-organism reference already in the project.** `reference_total_length`
   (from `storage/parsers.py`) and `ncbi_total_length` (from the NCBI metadata)
   both exist as facts today. If the project holds an assembly whose organism
   or taxid matches the reads', its total length is the best answer available
   and it is a measured one.
2. **Nothing.** No estimate. This is the common case for the workflow assembly
   is actually for -- something with no reference yet.
3. **The user.** A `genome_size` field in the dialog, prefilled from (1) when
   it exists, editable always, accepting `4.6m` / `3.1g` shorthand the way
   Flye's own flag does.

The warning is the point: when the estimate came from inference the dialog
says so and names the file it came from. An inferred number presented as fact
is worse than a blank field, because the blank field gets filled in.

Explicitly *not* inferred from read volume. Total sequenced bases divided by an
assumed coverage is a guess wearing a measurement's clothes.

## Resources: the governor does not do what the TODO thinks

The TODO entry predicted this would be "the first real exercise of the
`mem_mb` side of the load governor's admission checks." It is not.
`app/queue/governor.py` does not mention `mem_mb` at all -- every handler
declares it and nothing reads it. Declaring `mem_mb=65536` on an assembly job
would block precisely nothing.

So the guard is at launch, not at dispatch, in the shape
`resource_estimator.py` already uses for alignment: estimate, classify into a
band, warn on tight and refuse on impossible. `AssemblyMemoryModel` carries
bytes-per-genome-base plus a fixed overhead, with the coefficients documented
as published-guidance heuristics rather than measurements -- and the block
band set at genuinely-impossible, so a bad coefficient costs a spurious
warning and never a refused run that would have worked.

With no genome size there is no estimate, and with no estimate there is no
refusal. Assembly proceeds with a warning that says the size is unknown. That
asymmetry is intentional: refusing to run because we could not guess is worse
than running and failing.

`JobResources` still gets an honest declaration (`cpu=8`, `mem_mb` from the
estimate when there is one, `io=HEAVY`) because it is the input the governor
will read when it grows that check, and a job that lies about its demand now
is a job that lies then.

## Long jobs

Assembly runs for hours. Two consequences the other pipelines never hit:

- **Lease renewal.** `JobContext.extend_lease` exists and four handlers call
  it. Assembly must, or a laptop lid closing pauses the VM, the lease expires,
  a second worker adopts a job that is still running, and the epoch fencing
  correctly rejects the writes of whichever one loses.
- **No `--resume`.** Flye can resume from its own workdir, and doing so would
  fight `reap_pipeline_scratch`, which deletes exactly that. A retry is a
  fresh run. The reaper's cutoff is raised past the longest plausible
  assembly so it cannot delete a *live* job's scratch, which is the failure
  that actually matters.

Progress is phase strings, not percentages. Flye logs stage transitions
(`assembly draft`, `consensus`, `repeat graph`, `contigger`, `polishing`) and
those map onto `JobProgress.phase` directly. Inventing a percentage from five
stages of wildly unequal duration would be a worse lie than no number.

## Outputs: one job, several first-class files

This is the one genuinely new shape. `align_reads` produces one BAM. Flye
produces three things a person cares about:

| File | Becomes | Role |
|---|---|---|
| `assembly.fasta` | DataObject | `ObjectRole.REFERENCE` |
| `assembly_graph.gfa` | DataObject | new `ObjectRole.ASSEMBLY_GRAPH` |
| `assembly_info.txt` | facts on the FASTA object | — |

A GFA is not a `SidecarRole`. That enum is scaffolding-for-machines -- indexes
beside the file they index -- and an assembly graph is a result someone opens.
The precedent that fits is the NCBI download handler, which already makes four
DataObjects from one job, and `_apply_assembly_download` is the applier to
copy.

`assembly_info.txt` carries per-contig length, coverage, circularity and
repeat status. Contig count, longest and shortest already ship for any FASTA
(`_parse_fasta` emits `sequence_longest` / `sequence_shortest`; the TODO entry
claiming otherwise was retracted 2026-08-01), so what Flye's table adds beyond
the generic parse is **coverage and circularity per contig** -- neither
derivable from the FASTA. Circularity in particular is the thing a bacterial
assembly is judged on, and it is free here.

**Roling the FASTA as `REFERENCE` is what makes the next steps free**, and one
thing that is emphatically not free. See the verification section below: as
the code stands today, adding a de novo assembly to a project *breaks* the
Align card for every other FASTQ in it. That fix is part of this work, not a
follow-up.

Note also that the ingest path will not assign the role on its own.
`results.should_assign_reference_role` requires an assembly accession from
enrichment, and a de novo draft has none. `_apply_assemble_reads` sets
`ObjectRole.REFERENCE` explicitly, the way `_apply_align_reads` sets
`ALIGNMENT`.

## Verified before writing this: what a draft assembly does to the app today

Checked 2026-08-01 against the running stack and a synthetic draft (40,000
contigs, 154 Mb, lognormal lengths -- the shape a fragmented ONT assembly
actually has), because a plausible-looking fixture is what made the Actions
tab's rules wrong last time.

**The scale is a non-issue.** Every number is comfortable:

| Step | Result |
|---|---|
| `_parse_fasta` over 40k contigs | 1.75 s |
| Facts document written to Mongo | 2.2 KB |
| `samtools faidx` | 0.3 s, 1.3 MB `.fai` |
| `minimap2 -d` index | 1.5 s, 330 MB `.mmi` |
| `bowtie2-build --threads 4` | 206 s, 230 MB across six `.bt2` files |

`MAX_STORED_CONTIGS = 50` is what keeps the facts document at 2.2 KB instead
of megabytes, and it already sets `sequence_names_truncated`, which
`AssemblyFacts.tsx` already renders. Nothing needs raising or special-casing
for drafts. One wrinkle worth knowing: above a 256 MB uncompressed FASTA the
parser switches to estimating, and in that branch **`total_bases` is not
emitted at all** -- so a large draft cannot be a genome-size source for a
later assembly.

**The `_distinct_assemblies` worry was backwards.** It keys on an NCBI
accession regex, so any filename that does not look like `GCF_..._genomic.fna`
is its own candidate. hifiasm's `hap1` and `hap2` will not be collapsed. Good
-- but being two candidates is precisely what triggers the real problem.

**The real problem: a draft assembly silently disables the Align card.**
`resolve_reference` returns usable only when there is exactly one distinct
reference; with two or more it consults the organism *before* falling through
to a choice. Run against the real function:

| Project contents | Organism | Align card |
|---|---|---|
| NCBI reference only | known | ✅ aligns against it |
| own draft only | known | ✅ aligns against it |
| **NCBI reference + own draft** | **known** | ❌ "Fetching a reference genome for *T. brucei* is not wired up yet." |
| NCBI reference + own draft | blank | ✅ aligns against the NCBI one |
| two hifiasm haplotypes | known | ❌ same refusal |

So the sequence "download a reference, align, then assemble something" ends
with the Align card refusing to work on files it handled ten minutes earlier,
with a message about fetching that is not merely unhelpful but false --
nothing needs fetching, the project holds two usable genomes.

This is the `protein.faa` failure repeated in a new place: a rule that was
right when references only ever arrived from NCBI, meeting a reference that
arrives another way.

The fix belongs in this change. Preferred shape: when the project's references
include one this pipeline *produced*, prefer it and say so, rather than
treating provenance-unknown and self-produced references as interchangeable
members of a set whose size is the only thing consulted. The organism branch
should also stop firing when usable references exist at all -- it is a
fallback for having none, and its position below the `len == 1` branch shows
that was the intent. Both need cases in
`backend/tests/services/test_suggestion_service.py`, and the fix should be
checked against a real project, not the hand-built objects above.

## The card

`build_assemble_card` stops being a constant. Its docstring -- which currently
argues at length for why an always-unavailable card is the right thing --
gets replaced, not deleted: the argument for *showing* an unavailable card
rather than hiding it still holds for the short-read case.

- FASTQ, long-read chemistry, assembler available → AVAILABLE, `why` naming
  the chemistry and the assembler ("ONT simplex reads assembled with Flye").
- FASTQ, `SHORT` → UNAVAILABLE, "Short-read assembly is not installed."
- FASTQ, `UNKNOWN` chemistry → UNAVAILABLE, "Run QC first to determine read
  chemistry." That is actionable, which is the bar the existing cards set.
- Not FASTQ → `None`, as today.

Launch payload `{"endpoint": "/pipelines/assemble", "body": {"object_id": ...}}`,
complete server-side like the others.

## Files this touches

New:

- `app/pipelines/assembler_registry.py`
- `app/pipelines/assembly_params.py`
- `app/pipelines/assembly_runner.py` -- command construction and log parsing,
  the same split `align_runner.py` uses
- `app/queue/assembly_handlers.py` (the freed name)
- `frontend/src/components/AssembleDialog.tsx`

Changed:

- `backend/Dockerfile` -- one `flye` line in the existing apt block
- `app/pipelines/tools.py` -- `flye()` probe, `TOOL_META["flye"]` with
  `homepage`, `citation`, `license`, `usage` (`test_every_tool_is_documented`
  fails without all four; the license gets read off
  https://github.com/fenderglass/Flye, not recalled)
- `app/models/run.py` -- `RunKind.ASSEMBLY`, `RunJobRole.ASSEMBLE`
- `app/models/object.py` -- `ObjectRole.ASSEMBLY_GRAPH`
- `app/services/pipeline_service.py` -- `launch_assembly`, `_check_assemblable`
- `app/services/suggestion_service.py` -- `build_assemble_card`
- `app/api/v1/pipelines.py` -- `POST /pipelines/assemble`,
  `GET /pipelines/assemblers/{assembler}/schema`
- `app/queue/results.py` -- `_apply_assemble_reads`, `assembly_provenance`
- `app/pipelines/resource_estimator.py` -- assembly band
- plus the renames listed above

## Testing

Unit, in `backend/tests/`:

- chemistry → spec dispatch, including both refusals
- genome-size inference: found, absent, user-overridden; and that an inferred
  value is labelled as inferred all the way to the dialog payload
- `assembly_info.txt` parsing into facts, including the malformed-file case
- output harvesting: all three files present, and the FASTA-only case
- **card availability in the failing direction.** The image ships tools as
  installed, so asserting the card is *available* passes whether or not the
  patch worked. The test that matters patches `spec_for` off and asserts the
  card flips to unavailable -- and it patches `spec_for`, not
  `tools.flye`, because the spec captured the probe as a function object at
  import time and patching the module attribute never reaches it.

Against the real database and a real run, because a green suite has already
been wrong about this exact card once:

- `docker compose exec api python -c "..."` over real project objects, to see
  what `build_assemble_card` actually says for the FASTQs already in the
  library
- one real assembly end to end from a small ONT dataset, checking that the
  FASTA appears as a reference, the Align card offers it, and an index builds
  over it

## What this does not include

- hifiasm and SPAdes binaries (specced above, not built)
- QUAST and BUSCO -- their own TODO entry, now unblocked
- Pilon, RagTag, iVar -- likewise
- polishing of any kind; Flye's built-in polishing rounds are the only
  polishing here
- assembly-graph *visualization*. The GFA becomes a first-class object so it
  can be downloaded and opened in Bandage; rendering it in-app is not this.
- any change to the queue. `depends_on`, leases and epochs all already do what
  this needs.
