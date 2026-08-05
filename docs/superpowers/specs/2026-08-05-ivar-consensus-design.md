# iVar amplicon/viral consensus

Written 2026-08-05 for GitHub issue #47. This is the first tool slice riding on
the reference-based assembly foundation shipped under #21
(`2026-08-04-reference-assembly-foundation-design.md`). That foundation added
vocabulary, validators, and a provenance rule but installed no tool and
dispatched to nothing, so it has never been exercised by a real run. This slice
is what makes it real.

## Problem

BioFlow can align reads to a reference and call variants against one, but it
cannot produce a *consensus sequence* from an alignment. For amplicon and viral
sequencing -- SARS-CoV-2 ARTIC being the canonical case -- the consensus is the
deliverable. A variant call against a 30kb viral reference is an intermediate;
what a user submits, shares, or builds a tree from is the consensus FASTA.

There is a second, subtler gap. Amplicon protocols amplify overlapping tiled
regions using primers, and those primer sequences are *synthetic* -- they come
from the oligo, not from the sample. Reads carry them at their ends, and any
variant caller reading through primer-derived bases is reading the primer's
sequence rather than the sample's. At the overlaps, where one amplicon's primer
sits inside another amplicon's insert, this reliably manufactures false
reference-matching calls that mask real variation. Primer trimming is not a
tidiness step; it is a correctness prerequisite, and it is the reason a generic
"consensus from BAM" feature would be wrong for the amplicon case.

iVar is the tool that does both, and the pairing is why it is one slice rather
than two.

## Why iVar before Pilon

Epic #14 lists Pilon first and #23 already exists for it. This slice goes first
anyway, for three reasons:

- **It is the lighter runner.** iVar is C++ over htslib. Pilon is a JVM
  application whose memory footprint is a genuine design problem against the
  `job_timings` estimate path -- it wants heap sized to the assembly, and gets
  killed rather than degraded when that is wrong.
- **This repo already argues Pilon is obsolete for the modern case.**
  `frontend/src/assets/genome-analysis-review/body.html:601` says Pilon is
  "effectively obsolete for HiFi", superseded by hifiasm's built-in error
  correction, and worth retaining only for legacy ONT-only work. That is a
  document in this tree, not an outside opinion.
- **It exercises the foundation harder.** Pilon uses `DRAFT_ASSEMBLY` +
  `ALIGNMENT`. iVar uses `ALIGNMENT` + `REFERENCE` + `PRIMERS`, which is the
  only one of the three that touches the primer role the foundation reserved
  and never used.

## Scope

In scope:

- Installing iVar, which needs a decision because Debian has no package.
- A primer-trimming and consensus workflow: `ivar trim` then `ivar consensus`,
  as one user-visible pipeline.
- Primer BED input, using the existing BED ingestion path.
- A launch endpoint, a queue handler, a runner, and an Actions card.
- `TOOL_META` entry and the Software help page's required fields.

Out of scope:

- `ivar variants`, `ivar filtervariants`, `ivar getmasked`, `ivar removereads`.
  The consensus path is the deliverable; the variants path duplicates a
  bcftools/Clair3 capability this app already has, and the masking workflow
  depends on it.
- Amplicon *primer scheme* management -- a library of known schemes (ARTIC v3,
  v4.1, VarSkip) that a user picks by name. Worth doing later; it is a data and
  UI problem, not a pipeline one, and this slice should not block on it. The
  user supplies their own BED here.
- Pilon (#23) and RagTag.
- Chaining the alignment step. The user must already have a BAM against the
  reference; this card does not enqueue an alignment behind their back, per the
  foundation's explicit rule.

## Installing iVar

**Finding:** `apt-cache policy ivar` in the running `api` container returns
`Candidate: (none)`. iVar is not in Debian trixie at any version. samtools
(1.21) and bcftools are both present at `/usr/bin`, which matters because iVar
links htslib and shells out to samtools for sorting.

`backend/Dockerfile` documents a deliberate policy: bioinformatics tools come
from Debian rather than bioconda, because trixie carries current versions and
this avoids a conda installation for a handful of tools. iVar is the case that
policy does not cover, so this slice needs an explicit exception. Three options:

1. **Build from source.** iVar is a small autotools C++ project; its only real
   dependency is htslib, which is already in the image via samtools. Adds a
   compile step and a `build-essential`/`autoconf` surface to the image.
2. **Bioconda.** The Dockerfile's comment at line 178 notes bioconda is already
   used for one arm64-capable install, so the precedent exists. Drags in a conda
   environment for one tool, which is exactly what the stated policy avoids.
3. **Declare it unavailable.** Ship the spec, the registry entry, the card, and
   the "not installed in this build" reason -- the pattern `BUSCO_SPEC` and
   `HIFIASM_SPEC` already establish -- and install it later.

**Recommendation: build from source (1).** The existing bioconda precedent is
for a tool with a harder dependency graph; iVar's is one library the image
already has. Source-building keeps the single-toolchain property the Dockerfile
argues for, and iVar's build is genuinely small. Pin the version explicitly, the
way `install-compleasm.sh` does, rather than tracking a branch.

If the build turns out to be more than a few lines, fall back to (3) rather than
(2): a declared-unavailable tool with an honest reason is a better outcome than
a conda installation added under time pressure, and the rest of this slice --
validators, card, provenance -- is testable without the binary.

Add a `scripts/install-ivar.sh` beside `install-compleasm.sh`, and a
`tools.ivar()` probe following the `_probe("ivar", settings.ivar_path,
["version"])` shape. Note `ivar version`, not `--version`: iVar uses a
subcommand, and a probe passing `--version` gets a non-zero exit and reads as
"not installed" on a working binary.

## Inputs and validation

Three inputs, mapping onto roles the foundation already defined:

| Role | Object | Required |
|---|---|---|
| `ALIGNMENT` | BAM/CRAM of reads aligned to the reference | yes |
| `REFERENCE` | the viral/amplicon reference FASTA | yes |
| `PRIMERS` | primer scheme BED | no |

Primers are optional on purpose. Consensus from a non-amplicon viral alignment
-- metagenomic or bait-capture -- is a real workflow, and requiring a BED would
refuse it. When primers are absent the run skips `ivar trim` and calls consensus
directly, and the run record must say so, because "consensus without primer
trimming" and "consensus after trimming" are different claims about the output.

Validation reuses the foundation rather than adding parallel checks:

- `check_reference_assembly(reference)` -- ready FASTA, `ObjectRole.REFERENCE`,
  not protein/transcript.
- `validate_bam_aligned_to(bam, reference, owner=owner)` -- the async
  owner-scoped variant. This is the provenance rule, and it is the reason this
  workflow is honest: a BAM aligned to a different reference cannot produce a
  consensus against this one, however plausible the organism looks.
- A new `check_primer_bed(obj)`, which the foundation deliberately deferred.

`check_primer_bed` should validate more than `FormatKind.BED`. BED ingestion
already parses and records contigs (`_parse_tabular` in `storage/parsers.py`,
and `detect.py` sniffs BED by column shape), so the meaningful check is
available for free: **the primer BED's contigs must intersect the reference's
sequence names.** A primer scheme for a different virus is a BED whose contigs
name sequences the reference does not have, and iVar's own behavior there is to
trim nothing and exit successfully -- producing an untrimmed consensus that
looks like a successful trimmed one. That is precisely the silent-wrong-answer
shape this codebase keeps writing rules against, and it is catchable before
queueing.

Reject rather than warn. A zero-overlap primer BED is never what the user meant.

## Architecture

`PipelineType.REFERENCE_ASSEMBLY` and `RunKind.REFERENCE_ASSEMBLY` already
exist. This slice adds:

- **`RunJobRole.REFERENCE_ASSEMBLY`** -- the foundation explicitly deferred this
  until a real handler needed one. It does now.
- **`backend/app/pipelines/ivar_runner.py`** -- pure command construction and
  output parsing, following `completeness_runner.py`. Two builders,
  `build_trim_command` and `build_consensus_command`, plus a parser for iVar's
  quality/depth summary.
- **`backend/app/queue/reference_assembly_handlers.py`** -- a
  `consensus_from_alignment` handler, `HandlerMode.SUBPROCESS`,
  `JobClass.COMPUTE`. Imported by `handlers.py` for registration side effects,
  as `assembly_qc_handlers` is.
- **`launch_consensus`** in `pipeline_service.py`, and a
  `/pipelines/consensus` endpoint.

**No registry.** `assembly_qc_registry` and `assembler_registry` exist because
multiple tools compete for one job and dispatch must choose between them. There
is one consensus tool, and no second candidate close enough to name -- unlike
BUSCO beside compleasm. A registry here would be a seam with one side. The
foundation made the same call about a generic launcher, for the same reason, and
this slice should not quietly reverse it. If a second consensus tool appears,
that is when the registry is written, with two real cases to shape it.

### The two-step run

`ivar trim` and `ivar consensus` are separate invocations, and `ivar trim` emits
an unsorted BAM that `ivar consensus` cannot read -- it needs a position-sorted
pileup. So the sequence inside one job is:

1. `ivar trim -i in.bam -b primers.bed -p trimmed` (skipped without primers)
2. `samtools sort -o trimmed.sorted.bam trimmed.bam`
3. `samtools mpileup -A -d 0 -Q 0 --reference ref.fa trimmed.sorted.bam | ivar consensus -p consensus`

Step 2 is the one that is easy to omit and fails confusingly: iVar's own docs
mention it in prose, and skipping it yields either an empty consensus or a
truncated one, with a zero exit code. It belongs in the runner as a required
step, not as an optional tidy-up.

The mpileup flags are deliberate and worth recording, because each disables a
default that is wrong for amplicon data. `-A` keeps anomalous read pairs, which
are normal at amplicon boundaries. `-d 0` removes the depth cap, which
otherwise silently downsamples the high-coverage positions amplicon data is made
of. `-Q 0` disables the base-quality filter, because iVar applies its own
threshold and applying both means the user's setting is not the one in effect.

One job, not three chained jobs. `Job.depends_on` exists and is used for
`align_reads` waiting on `build_index`, but that gate is for steps a user might
launch or reuse independently. Nobody wants a trimmed-but-not-consensused BAM as
a durable artifact here, and splitting would put two intermediate objects in the
explorer that exist only to be consumed.

## Outputs

The consensus FASTA is the deliverable: a `DataObject`, `FormatKind.FASTA`,
`ObjectRole.REFERENCE`, `derived_from` the alignment, the reference, and the
primer BED when supplied. Role `REFERENCE` follows the foundation's rule that
every generated assembly is addressable, alignable, and auditable through the
same object model -- a consensus is a sequence others will align against.

The trimmed BAM is **not** retained as an object. It is an intermediate whose
only consumer is the consensus step in the same job, and keeping it doubles the
storage of the largest file in the workflow to no end. It stays in the workdir.

Facts recorded on the consensus object:

- `consensus_tool_version`
- `consensus_min_depth`, `consensus_min_freq`, `consensus_min_quality` -- the
  thresholds in effect, because a consensus is meaningless without them
- `consensus_n_count` and `consensus_ambiguous_pct` -- positions called `N` for
  insufficient depth. This is the single most important quality number for a
  viral consensus and the one a user checks first.
- `consensus_primers_trimmed` -- boolean, and the run's record of whether
  primers were supplied. Never inferrable later from the FASTA.

## Suggestion behavior

One card, `kind="consensus"`, category `REFERENCE_ASSEMBLY`, built from a
**BAM** object. The foundation left the anchor choice open ("from the BAM or
reference path once the UI can make that choice clearly"); BAM is right because
the reference is resolvable from the BAM by provenance while the reverse is a
one-to-many walk -- a reference has many alignments and the card cannot pick.

The rules, in order:

- Not a BAM/CRAM → `None`, no card.
- iVar not installed → `UNAVAILABLE`, "iVar is not installed in this build."
- No resolvable alignment target (`alignment_target_for_bam` raises) →
  `UNAVAILABLE`, "This alignment has no recorded reference, so its consensus
  could not be checked against one."
- Ambiguous target → `UNAVAILABLE`, saying so.
- Otherwise → `AVAILABLE`, launching `/pipelines/consensus` with the object id.

Two traps CLAUDE.md names explicitly, both of which apply here:

**Do not gate the card on the reference looking viral.** A genome-size or
organism check would be the `protein.faa` mistake in a new costume -- a rule
that is right about the common case and refuses a legitimate one. Consensus
against a bacterial or plasmid reference is unusual, not wrong.

**Test the unavailable direction.** The image will ship iVar as installed, so a
test asserting the card is *available* passes whether or not its patch worked.
Assert the card flips to unavailable when `tools.ivar` is patched off. Since
there is no registry here, `tools.ivar` is the real seam and the frozen-dataclass
trap does not apply -- but the probe is `lru_cache`d, so a test must clear the
cache or patch the probe function rather than the binary path.

The card must be checked against a real project before it is believed, not only
against fixtures.

## Resources

`JobResources(cpu=2, mem_mb=4096, io=IoClass.HEAVY)`. Consensus calling is a
single-threaded pileup walk; iVar does not parallelize, so requesting more CPU
would idle it. Memory is dominated by the pileup buffer at high depth, not by
genome size -- a 30kb viral reference at 10,000x is a small memory footprint and
a large I/O one, which is why `io` is heavy and `mem_mb` is not.

Lease: viral consensus is minutes. A one-hour lease is generous; there is no
case here resembling the multi-hour runs `COMPLETENESS_LEASE_SECONDS` sizes for.

`max_attempts=1`, matching `assess_completeness` and `assemble_reads`: the input
and the tool are both deterministic, so a retry fails identically.

Resource fields in `job_timings` are null under 60 seconds, so most consensus
runs will record no peak RSS. That is expected -- a short job, not a sampling
failure -- and worth knowing before someone reads the memory model's sparse rows
here as a bug.

## Tool metadata

`TOOL_META["ivar"]`, `pipelines=(PipelineType.REFERENCE_ASSEMBLY,)`.
`test_every_tool_is_documented` requires `homepage`, `citation`, `license`, and
`usage`; the entry fails the suite until they are filled in, which is the point.

Verify all four against iVar's own repository at implementation time rather than
from memory -- CLAUDE.md is explicit that a wrong license claim on a page that
reads as authoritative is worse than a blank field. `repository` and
`citation_url` are not required and should be left empty rather than guessed.

`usage` describes behaviour, not flags: that BioFlow runs iVar to trim amplicon
primers from an alignment and call a consensus sequence against its reference,
that primer trimming is skipped when no primer BED is supplied, and that the
consensus is stored as a new reference object. Flags change when the runner is
tuned and nothing can mechanically catch a stale `usage` string.

## Testing

Unit tests, `backend/tests/`:

- `check_primer_bed` accepts a BED whose contigs intersect the reference, and
  rejects one whose contigs are disjoint.
- The launch path refuses a BAM aligned to a different reference -- the
  foundation's provenance rule, reached through the real launch function rather
  than by calling the validator directly.
- The launch path accepts a matching BAM with and without primers.
- `build_trim_command` and `build_consensus_command` produce the expected
  argv, including the sort step between them.
- The consensus card is `UNAVAILABLE` with `tools.ivar` patched off.
- The card is `None` for a FASTA and for a FASTQ.
- Enum mirror tests for `RunJobRole.REFERENCE_ASSEMBLY` crossing into
  TypeScript.

Against the real database, before believing any of it:

```bash
docker compose exec api python -c "..."
```

Build the consensus card from real BAM objects in a real project and check that
the reference resolves as expected. This is the step CLAUDE.md records as having
caught two rule bugs that a full green suite missed, and the provenance walk
here is exactly the kind of rule that looks right against hand-built fixtures.

End-to-end, in the browser at localhost:5173 (or 5273 from a worktree):

A SARS-CoV-2 ARTIC dataset is the representative path -- small, public, and the
case the workflow is designed for. Align reads to the reference in the app,
supply the matching ARTIC primer BED, run the consensus, and confirm the output
FASTA appears with its `consensus_n_count` and threshold facts. Then run it
again *without* the primer BED and confirm the two differ: if trimmed and
untrimmed consensus sequences are identical, primer trimming did not happen, and
the most likely cause is a BED whose contig names do not match the reference --
the failure `check_primer_bed` exists to prevent, verified from the other side.

Restart the worker before testing (`docker compose restart worker`): the handler
is new, and `worker` does not hot-reload. A new handler that never loaded reads
as a job that silently does nothing.

## Risks

**The install may not be as small as it looks.** Everything downstream of the
binary -- validators, card, provenance, tests -- is independent of it, so the
mitigation is to build in that order and fall back to a declared-unavailable
tool if the Dockerfile work grows. Do not let an install problem block the
slice.

**iVar's exit codes are unreliable.** It exits zero in several conditions where
it produced nothing useful, including the disjoint-primer case above. The
handler should verify the consensus FASTA exists and is non-empty rather than
trusting the return code -- the same check `assess_completeness` makes for a
missing `summary.txt`, and for the same reason.

**A consensus is mostly `N` more often than users expect.** Low-coverage
amplicon dropout is common and produces a technically successful run whose
output is unusable. Recording `consensus_n_count` as a fact is what makes that
visible rather than surprising; a future card could warn on it, but that is not
this slice.
