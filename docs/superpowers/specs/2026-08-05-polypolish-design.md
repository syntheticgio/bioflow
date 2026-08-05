# Polypolish short-read polishing

Written 2026-08-05 for GitHub issue #23, which this spec **repurposes from Pilon
to Polypolish**. It is the second tool slice on the reference-based assembly
foundation shipped under #21
(`2026-08-04-reference-assembly-foundation-design.md`), after iVar (#47,
`2026-08-05-ivar-consensus-design.md`).

## Problem

BioFlow can assemble reads (Flye) and can align reads to a sequence, but it
cannot *polish* an assembly -- use short reads to correct residual base errors
in a long-read assembly. For ONT-only assemblies that is not a refinement, it is
the difference between a usable consensus and one whose every homopolymer is
suspect: the polishing-decision study this repo already cites (Luan et al., BMC
Genomics 2024) found homopolymers accounted for 96% of residual Flye errors, and
that short-read polishing is what rescues a Flye assembly.

## Why Polypolish, and not Pilon

Issue #23 originally named Pilon, following the TODO entry. Pilon is the wrong
tool to add in 2026, for a reason that is structural rather than a matter of
version currency.

**Pilon consumes best-alignment BAM.** Each read is assigned to exactly one
locus, so in a repeat every read lands on one copy and Pilon then "corrects"
that copy toward a consensus assembled from reads that came from its paralogs.
Pilon's characteristic failure is *introducing* errors in repetitive regions --
the regions a long-read assembly was chosen for in the first place.

**Polypolish consumes all-alignment SAM.** `bwa mem -a` reports every location a
read maps to, and Polypolish changes a position only where the evidence is
unambiguous across all of them. Ambiguous repeat positions are left alone rather
than confidently mis-corrected. It is the one polisher in this family that
reliably does not make an assembly worse.

That difference is why the follow-up benchmark (Bouras et al., *Microbial
Genomics* 2024, doi:10.1099/mgen.0.001254 -- the same paper Polypolish's own
README cites for v0.6.0) landed on Polypolish as the recommended short-read
polisher for ONT bacterial assemblies, with Pilon among the riskier options.

Three secondary points, all pointing the same way:

- **This repo already argues Pilon is obsolete.**
  `frontend/src/assets/genome-analysis-review/body.html:601` calls it
  "effectively obsolete for HiFi," superseded by hifiasm's built-in error
  correction, worth retaining only for legacy ONT-only work. But "legacy
  ONT-only work" *is* the ONT-assembly-plus-Illumina case Polypolish took over,
  so the narrow scope Pilon was going to be given contains nothing Polypolish
  does not do better.
- **It dodges the JVM problem.** Pilon is a Java application that wants heap
  sized to the assembly and gets OOM-killed rather than degraded when that is
  wrong -- a genuine design problem against `job_timings`' memory estimates, and
  the stated reason iVar went first. Polypolish is a single static Rust binary
  with a modest, predictable footprint.
- **It is maintained.** Polypolish v0.7.1, last pushed 2026-07-28. Pilon's last
  release was 2021.

Verified against the projects' own repositories 2026-08-05, not from recall:
Polypolish is GPL-3.0, `rrwick/Polypolish`, latest release v0.7.1.

## Scope

In scope:

- Installing Polypolish, which needs a decision because Debian has no package.
- A polish workflow: `bwa-mem2 mem -a` per read file, `polypolish filter`,
  `polypolish polish`, as one user-visible pipeline.
- Draft assembly plus paired short reads as inputs.
- A launch endpoint, a queue handler, a runner, and an Actions card.
- `TOOL_META` entry and the Software help page's required fields.

Out of scope:

- **pypolca.** The Bouras 2024 recommendation is Polypolish *followed by*
  `pypolca --careful`, and pypolca is a reasonable follow-on slice (MIT,
  `gbouras13/pypolca` v0.4.0, pip- and bioconda-installable). It is deliberately
  not in this slice: it is a second tool with its own install route and its own
  card, and Polypolish alone is the larger share of the benefit. Adding it later
  is additive, not a rework.
- **Long-read polishing** (medaka, Racon). A different input shape and, in
  medaka's case, a deep-learning stack that would dominate the image -- the
  genome-analysis review says as much about adding medaka to a pipeline.
- **Iterative polishing rounds.** Polypolish is typically run once; multi-round
  polishing is a workflow question that belongs to the DAG work (#18).
- Pilon, permanently. RagTag remains follow-on work under the epic.

## The input shape, and why it differs from iVar

This is the most important thing in this spec, because it is where a reader who
knows the iVar slice will guess wrong.

**Polypolish cannot consume an existing BAM.** It requires SAM files in which
each read is aligned to *all* its possible locations. BioFlow's `align_reads`
produces a coordinate-sorted best-alignment BAM -- exactly what Polypolish must
not be given. Feeding it one does not error; it produces a worse polish, quietly,
which is the Pilon failure mode reintroduced through the back door.

So the alignment is **internal to the polish job**, not a user-supplied input:

| Role | Object | Required |
|---|---|---|
| `DRAFT_ASSEMBLY` | the assembly to polish, FASTA | yes |
| `READS` | short reads R1, FASTQ | yes |
| `MATE` | short reads R2, FASTQ | no |

Three consequences worth stating plainly:

**The epic's provenance requirement is satisfied by construction here, not by
validation.** Epic #14 requires that provenance for alignments against
pipeline-produced assembly output be explicit. For iVar that meant
`check_bam_aligned_to` refusing a BAM aligned to something else. For Polypolish
there is no BAM to check: the job aligns the supplied reads to the supplied
draft itself, so the alignment target *is* the draft by construction. This is a
stronger guarantee than the validated one, and the run record should still say
what was aligned to what -- the requirement is that the fact be explicit, and an
unrecorded guarantee is not.

**`check_draft_assembly` is the validator that applies**, not
`check_reference_assembly` -- the foundation already distinguishes them, and this
is the first caller of the draft one.

**Reads must be short reads.** Polypolish on ONT reads is meaningless; the whole
point is high-accuracy short reads correcting a long-read assembly. The reads
object's platform facts are the seam, and see the suggestion-rules section for
how hard to gate on this.

## Installing Polypolish

**Not in Debian.** Checked properly against the running `api` container with an
`apt-get update` first -- the stale-cache false negative that produced a wrong
"not in Debian" claim for iVar: no candidate for `polypolish`, `pypolca`, or
`polca`.

**Use the precompiled static binary from the GitHub release.** v0.7.1 ships
`polypolish-linux-x86_64-musl-v0.7.1.tar.gz` -- a musl-static Rust binary with
no runtime dependencies, which is about as clean as an out-of-Debian install
gets. Add `backend/scripts/install-polypolish.sh` following the compleasm
precedent: fetch the pinned release tarball, verify, extract the single binary
to `/usr/local/bin`, and pin the version in one place so a bump is one line.

**arm64 is a real gap and must be handled, not discovered.** The v0.7.1 release
assets are `linux-x86_64-musl`, `macos-aarch64`, and `macos-x86_64`. There is no
`linux-aarch64` binary. This is the same architecture problem the Dockerfile
comment already records for bwa-mem2, and it matters more here because
Polypolish's aligner *is* bwa-mem2. Two options, decide at implementation:

1. Build from source with `cargo build --release` on arm64 only. Correct, but
   drags a Rust toolchain into the image for one binary.
2. Declare the tool unavailable on arm64, following the
   `BUSCO_SPEC`/`HIFIASM_SPEC` pattern the iVar spec kept on file for exactly
   this case.

**Option 2 is the recommendation.** bwa-mem2 is already the constraint on the
same platform, so an arm64 Polypolish would have no supported aligner to pair
with -- building it would produce a tool that installs and then cannot run. A
card reading "Polypolish is not available on this architecture" is the honest
outcome, and it is one the suggestion layer already knows how to render.

Add `tools.polypolish()` with `_probe("polypolish", settings.polypolish_path,
["--version"])`. Confirm the flag at implementation: iVar needed `version` as a
subcommand and a probe passing `--version` would have read a working binary as
missing.

## Architecture

Follows the iVar slice closely, which is the point of having built the
foundation:

- **`backend/app/pipelines/polypolish_runner.py`** -- pure command construction
  and output parsing. Builders for the index step, the two alignment steps, the
  filter step, and the polish step, plus a parser for Polypolish's stderr
  summary (it reports how many positions it changed, which is the number a user
  checks first).
- **`backend/app/queue/reference_assembly_handlers.py`** -- a
  `polish_assembly` handler alongside the existing `consensus_from_alignment`.
  `HandlerMode.SUBPROCESS`, `JobClass.COMPUTE`.
- **`launch_polish`** in `pipeline_service.py`, and a `/pipelines/polish`
  endpoint.
- `RunJobRole.REFERENCE_ASSEMBLY` already exists, added by the iVar slice.

**No registry**, for the reason the iVar spec gives: one tool, no second
candidate close enough to name. pypolca, if it lands, is a *sequential*
companion rather than a competing alternative, so even that would not make a
registry the right shape.

### The five-step run

One job, five subprocess stages:

1. `bwa-mem2 index draft.fasta`
2. `bwa-mem2 mem -t N -a draft.fasta reads_1.fastq.gz > alignments_1.sam`
3. `bwa-mem2 mem -t N -a draft.fasta reads_2.fastq.gz > alignments_2.sam`
4. `polypolish filter --in1 … --in2 … --out1 … --out2 …`
5. `polypolish polish draft.fasta filtered_1.sam filtered_2.sam > polished.fasta`

Four things here are easy to get wrong and each produces a plausible-looking
wrong answer rather than an error:

- **`-a` is mandatory.** Without it bwa-mem2 reports best alignments only and
  Polypolish silently degrades to Pilon's failure mode. Confirmed present in
  the image's bwa-mem2: `-a  output all alignments for SE or unpaired PE`.
- **R1 and R2 are aligned *separately*, not as a pair.** Two independent
  `bwa-mem2 mem` invocations against one file each. This looks like a mistake to
  anyone used to paired alignment and is not one -- Polypolish's filter step is
  what reunites the pairs and applies insert-size logic. Aligning them together
  defeats `-a`.
- **The SAMs are not sorted or converted to BAM.** Polypolish reads raw
  name-ordered SAM. Adding a `samtools sort` here -- the reflex the iVar slice
  needed -- breaks it.
- **The filter step is skippable but should not be skipped.** Upstream calls it
  optional; it is what removes alignments inconsistent with the insert-size
  distribution, and omitting it is a quality regression with no benefit.

The SAM files are large and intermediate. They stay in the workdir and are never
ingested as objects.

### The `--careful` decision

Polypolish's own guidance: use `--careful` at read depth of 25x or lower, where
it discards multi-mapping reads -- reducing the risk of introducing errors at
the cost of not correcting repeats.

Compute depth rather than asking the user: total read bases divided by draft
assembly length, both already available as object facts. Apply `--careful`
below 25x, record `polish_careful_mode` and the computed depth as facts, and
surface the depth in the launch dialog so the user can see the basis for the
choice. A hidden threshold that changes what the tool does is the kind of thing
that reads as nondeterminism later.

## Outputs

The polished FASTA is the deliverable: a `DataObject`, `FormatKind.FASTA`,
`ObjectRole.REFERENCE`, `derived_from` the draft assembly and both read objects.
Role `REFERENCE` follows the foundation's rule that every generated assembly is
addressable, alignable, and auditable through the same object model.

The draft is **not** replaced or superseded. A polished assembly is a new
object beside its input; the comparison between them is the evidence that
polishing helped, and destroying the input destroys the comparison.

Facts recorded:

- `polish_tool_version`
- `polish_changed_positions` -- how many bases Polypolish altered. The single
  number that says whether the run did anything, and the one to check first. A
  polish that changed zero positions is either a clean assembly or a broken
  pipeline, and only this fact plus the depth distinguishes them.
- `polish_read_depth` and `polish_careful_mode` -- the computed depth and
  whether the threshold triggered.
- `polish_aligner` and `polish_aligner_version` -- bwa-mem2. The alignment is
  internal, so nothing else in the object graph records it, and this is the
  epic's provenance requirement discharged.

## Suggestion behavior

One card, `kind="polish"`, category `REFERENCE_ASSEMBLY`, anchored on the
**draft assembly** object. Unlike the consensus card there is no provenance walk
to do -- the reads are a sibling selection in the same project, not something
resolvable from the anchor.

The rules, in order:

- Not a FASTA, or not assembly-like (`check_draft_assembly`'s criteria) → `None`.
- Polypolish not installed / unsupported architecture → `UNAVAILABLE`, saying
  which.
- No short-read FASTQ in the project → `UNAVAILABLE`, "Polishing needs short
  reads; this project has none."
- Otherwise → `AVAILABLE`, launching `/pipelines/polish`.

**Gate on the reads being short, not on the assembly being ONT-derived.** This
is the `protein.faa` trap in its natural habitat. The tempting rule is "only
suggest polishing for long-read assemblies," and it is wrong twice: BioFlow
often will not know how an imported assembly was produced, and polishing a
short-read or hybrid assembly is unusual rather than incorrect. The rule that
*is* safe is the one about the reads, because Polypolish on long reads is
meaningless rather than merely unusual -- and even there, prefer a warning in
the dialog over a refusal when platform facts are absent rather than
contradictory.

**Do not auto-chain from the assembler.** The foundation's explicit rule is that
a card does not enqueue an alignment behind the user's back; the same reasoning
says the Assemble card should not silently grow a polish step.

**Test the unavailable direction**, per CLAUDE.md: the image will ship
Polypolish as installed on x86_64, so a test asserting the card is available
passes whether or not the patch worked. Assert it flips to unavailable with the
probe patched off, and remember the probe is `lru_cache`d.

## Resources

`JobResources(cpu=8, mem_mb=16384, io=IoClass.HEAVY)`.

The profile is dominated by bwa-mem2, not by Polypolish. bwa-mem2 threads well
and its index is memory-hungry -- roughly 28 bytes per reference base, so a 5Mb
bacterial genome is trivial and a multi-Gb draft is not. Polypolish's own step is
single-threaded and modest. Size the request for the aligner and the polish step
rides along free.

That asymmetry is worth recording because it makes the memory model's rows here
misleading in a specific way: `peak_rss_bytes` for a polish job describes
bwa-mem2's index, so it scales with the *draft*, not with read count. A fit that
treats it as a function of input bytes will be wrong in both directions.

Lease: an hour is not enough for a large draft. Size it like the alignment jobs,
not like the consensus one.

`max_attempts=1`, matching the family: deterministic tool, deterministic input,
a retry fails identically.

## Tool metadata

`TOOL_META["polypolish"]`, `pipelines=(PipelineType.REFERENCE_ASSEMBLY,)`.
`test_every_tool_is_documented` requires `homepage`, `citation`, `license`,
`usage`; the entry fails the suite until they are filled.

Verified 2026-08-05 against the project's own repository and README:

- `homepage` / `repository`: `https://github.com/rrwick/Polypolish`
- `license`: GPL-3.0
- `citation`: Wick RR, Holt KE. "Polypolish: short-read polishing of long-read
  bacterial genome assemblies." *PLOS Computational Biology*, 2022.
  doi:10.1371/journal.pcbi.1009802
- `citation_url`: `https://doi.org/10.1371/journal.pcbi.1009802`

Polypolish's README also asks that v0.6.0+ users cite Bouras et al. 2024
(doi:10.1099/mgen.0.001254). `ToolMeta` carries one citation field; put the
primary paper there and mention the follow-up in `usage`, rather than
concatenating two references into a field the help page renders as one.

`usage` describes behaviour, not flags: that BioFlow runs Polypolish to correct
residual base errors in a draft assembly using short reads, that it aligns the
reads to the draft internally with bwa-mem2 reporting all alignments, that
`--careful` engages automatically below 25x depth, and that the polished
assembly is stored as a new object beside the draft.

## Testing

Unit tests, `backend/tests/`:

- `check_draft_assembly` accepts a draft FASTA and rejects protein/transcript
  roles -- first real caller of the foundation's draft validator.
- The launch path refuses a project with no short reads.
- The launch path accepts draft + paired reads, and draft + single-end reads.
- `build_align_command` includes `-a`, and builds one invocation per read file.
- The depth calculation crosses the 25x threshold in both directions and sets
  `--careful` accordingly.
- The polish card is `UNAVAILABLE` with the `polypolish` probe patched off.
- The card is `None` for a FASTQ and for a BAM.
- Enum/type mirror tests for anything crossing into TypeScript.

Against the real database, before believing any of it -- CLAUDE.md records this
step catching two rule bugs a green suite missed:

```bash
docker compose exec api python -c "..."
```

Build the polish card from real assembly objects in a real project. The specific
thing to check is the same shape as the bug that motivated the rule: that
`protein.faa` and `cds_from_genomic.fna` do **not** produce a polish card, and
that a project's actual assembly does.

End-to-end, at localhost:5173 (or 5273 from a worktree). The honest note here is
that **the current database may not support a good tier-1 run**: polishing needs
a long-read draft assembly *and* short reads from the same sample, which is a
narrower combination than the consensus slice needed. Check what the yeast and
*T. brucei* projects actually hold before planning the run; if no project has
both, a small bacterial ONT+Illumina dataset is the cheapest way to get one, and
bacterial is the case Polypolish is built and benchmarked for.

The end-to-end assertion that matters is not "it completed" but
`polish_changed_positions > 0` together with a plausible depth -- a run that
changed nothing completes successfully and looks fine.

Restart the worker before testing (`docker compose restart worker`): the handler
is new and `worker` does not hot-reload.

## Risks

**The arm64 gap is decided, not discovered.** Recorded above with a
recommendation (declare unavailable) rather than left for implementation to hit.
The failure mode if ignored is an install script that works on the maintainer's
machine and breaks the image build on Apple Silicon.

**bwa-mem2's index memory is the real resource risk.** A large draft can exceed
the container's memory during step 1, before Polypolish is reached at all --
which will read as "the polish tool OOMs" when the polisher never ran. Record
the stage in the failure message so the log says which of the five steps died.

**Skipping `-a` is the silent one.** Everything runs, the output is a polished
FASTA, and the polish is quietly worse than it should be in exactly the regions
that matter. Assert the flag in a unit test on the built argv rather than
trusting the runner to keep it.
