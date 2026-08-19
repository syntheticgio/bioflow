# Medaka long-read assembly polishing

Date: 2026-08-18.

Closes [#618](https://github.com/syntheticgio/bioflow/issues/618). Companion
to `docs/superpowers/specs/2026-08-05-polypolish-design.md`, which shipped the
short-read polishing path this one mirrors for long reads.

## Problem

BioFlow's only polishing path is short-read-only. `polish_assembly` runs
Polypolish over `bwa-mem2 mem -a` alignments, and `build_polish_card` gates
itself on the project having exactly one *short*-read set. A project assembled
purely from ONT long reads -- the Flye path this image has shipped since the
de-novo assembly work -- reaches the end of assembly with no polishing step
available at all. The raw assembler output is the final product, uncorrected
for the residual base-level errors long-read assemblers are known to leave,
particularly in homopolymer runs.

The gap is visible in the codebase rather than merely implied:
`provenance_prompt.py`'s known-tool list already names `"racon"` and
`"medaka"` among tools it recognizes when parsing provenance text, with no
runner behind either. The project anticipated these tools without building
them.

On arm64 the gap is total rather than partial. Polypolish ships no
linux-aarch64 build and `install-polypolish.sh` deliberately skips it, so an
Apple Silicon machine today has *no* polishing path for any chemistry. Medaka
does ship aarch64, which makes this change a capability gain on that platform
rather than parity work.

## Why Medaka alone, and not Racon-then-Medaka

The issue left this open, listing Racon-then-Medaka (the classic two-pass
chain) against Medaka alone. This spec chooses Medaka alone.

The two-pass chain is a habit from the R9 era, when Racon's POA consensus
meaningfully cleaned up a draft before Medaka's network saw it. Medaka's own
README now lists, as a headline feature, "Improved accuracy over graph-based
methods (e.g. Racon)". Upstream is asserting that its tool supersedes the step
we would be adding in front of it, and current ONT guidance for R10 chemistry
runs Medaka directly on the draft.

Racon is cheap in isolation -- a 0.2MB conda package, MIT-licensed, no neural
dependencies. What it is not cheap in is *surface*: a second runner, a second
`TOOL_META` entry, a chain-ordering decision, provenance for two passes, and a
card whose description has to explain when each tool applies and in what
order. That is a material amount of hand-maintained registry surface, of
exactly the kind `CLAUDE.md` warns silently rots, spent on a step upstream
says its own tool replaces.

If a concrete R9-era case appears, Racon is a follow-up issue and this
design does not block it: `polish_long` is its own card and node type, so a
`polish_racon` beside it would not disturb anything here.

## What the issue got wrong about the dependencies

The issue scoped Medaka's install risk as "heavier dependency requirements
(TensorFlow) worth scoping explicitly rather than discovering
mid-implementation." That concern is stale, and the correct concern is
different in kind.

Medaka 2.2.2's bioconda package depends on **`pytorch 2.9.*`**, not
TensorFlow; the project migrated off TF in the 2.x line. Verified against
`api.anaconda.org` on 2026-08-18 rather than recalled.

The real risk is a resolver default, not a framework. conda-forge's bare
`pytorch` package resolves preferentially to **CUDA** builds, which pull
`libtorch` in at roughly **885MB compressed**; the CPU build of the same
`libtorch` is roughly **61MB**. Both figures measured from the conda-forge
package index on 2026-08-18. Nothing errors if the pin is omitted -- the image
simply grows by about a gigabyte to ship CUDA kernels into a container that
has no GPU and never asks for one.

So `pytorch-cpu` is pinned explicitly in the install script, and the comment
there says why. This is the same shape as the `flye-samtools` shim: a
one-line install detail whose omission produces no error and a badly wrong
result.

## Scope

**R1.** Medaka installs into the image on both amd64 and arm64, and
`test_every_tool_is_documented` passes for it.

**R2.** A user whose project contains one ONT read set and a draft assembly
built from it can polish that assembly and receive a new FASTA object.

**R3.** The Actions tab offers Medaka to a long-read-only project and
Polypolish to a short-read-only project, and offers neither tool for reads it
cannot use.

**R4.** A polish run records which model Medaka used, and whether that model
was auto-resolved or fell back to a default.

**R5.** A polish run records how many positions changed between the draft and
the consensus, and a test demonstrates a run over a draft carrying only
planted single-base substitutions in which that count is non-zero and equals
the number of planted errors corrected. Substitutions-only is what makes the
equality well-defined; length-changing corrections are recorded separately as
`polish_length_delta` and are out of scope for this equality.

**R6.** Racon is out of scope, per the section above.

**R7.** GPU inference is out of scope. The CPU pin in R1's install is
deliberate and this design does not offer a GPU path.

## The input shape, and how it differs from Polypolish

Both tools take a draft plus the reads, and neither takes a pre-existing BAM.
That much `polish_assembly` and `polish_long_assembly` share, and it is what
makes provenance answerable "by construction" for both: the reads are aligned
to this draft inside the job, so the alignment target cannot be anything else.

Three differences make this **not** a copy of `polypolish_runner`, and each
one breaks something if ported over unexamined.

**Medaka writes a directory, not stdout.** The public entry point is
`medaka_consensus`, a bash wrapper that runs minimap2, `medaka inference`, and
`medaka sequence` in sequence and writes `<outdir>/consensus.fasta`. There is
no stdout to capture. `polypolish_runner.redirect_stdout` exists because both
bwa-mem2 and `polypolish polish` write their real output to stdout; Medaka
does not, and wrapping its argv in `/bin/sh -c ... > file` would produce an
empty file beside a correct consensus the handler then ignores. The runner
returns an argv plus the output directory, and the handler reads
`consensus.fasta` out of it.

**The alignment parameters are the model's, not ours.** `medaka_consensus`
calls `medaka tools get_alignment_params --model $MODEL` and passes the result
to minimap2, because the correct preset depends on which network will consume
the alignment. Polypolish's `-a` is mandatory and hardcoded for reasons this
repo documents at length; Medaka's minimap2 invocation is the opposite case,
where constructing it ourselves would be actively wrong. We do not build a
minimap2 command at all.

**`-f` is mandatory.** Without it, `medaka_consensus` prints "WARNING: Output
${OUTPUT} already exists, may use old results" and reuses whatever consensus
is already in the directory, exiting zero. The handler prepares a fresh
workdir per job so the collision should not arise, but the failure mode if it
ever does is a job that returns a *previous* run's assembly and reports
success. That is the silent-wrong-result shape this repo keeps writing down,
so `-f` is passed unconditionally rather than left to workdir hygiene.

## Installing Medaka

A new `backend/scripts/install-medaka.sh`, modeled directly on
`install-clair3.sh`, which already proves every piece of this on both
architectures: download micromamba per-arch, create an isolated prefix under
`/opt`, delete the micromamba binary and the package caches afterward.

```
ARG MEDAKA_VERSION=2.2.2
micromamba create -y -p /opt/medaka/env \
    -c conda-forge -c bioconda \
    "medaka=${MEDAKA_VERSION}" "pytorch-cpu=2.9.*"
```

Both architectures, unlike Polypolish. bioconda publishes `linux-aarch64`
builds of medaka for 2.0.1 through 2.2.2, verified 2026-08-18. The arch case
statement is `install-clair3.sh`'s, unchanged in shape.

The layer goes late in the Dockerfile, beside Clair3 and SPAdes, for the
reason those two are late: it is large and slow, and an edit anywhere above it
should not trigger a rebuild.

`medaka` ships its own minimap2 and samtools inside the conda prefix. Those
are deliberately *not* put on PATH ahead of the image's own -- the prefix is
referenced by absolute path, so nothing else in the image changes which
samtools it gets.

## Architecture

### `medaka_runner.py`

Pure functions over strings and paths, testable without a container, a queue,
or a binary -- the same split `polypolish_runner` and `ivar_runner` use.

- `build_consensus_command(*, medaka_path, draft, reads, outdir, threads,
  bacteria=False)` -> argv for `medaka_consensus`, with `-f` always present
  and `--bacteria` when asked.
- `parse_model_line(text)` -> the resolved model name and whether it was
  auto-selected, from Medaka's own stderr.
- `count_changed_positions(draft, consensus)` -> the diff described below.

### The run

One job, four phases, three of which are Medaka's own internals:

1. Resolve tool, prepare workdir, link the draft and reads under stable names.
2. `medaka_consensus -i reads -d draft -o outdir -t threads -f [--bacteria]`.
3. Read `outdir/consensus.fasta`; fail if absent or empty.
4. Diff draft against consensus, assemble facts, return.

### Reads: `is_long_read` is written positively, not as a negation

`long_read_sets()` joins `short_read_sets()` in
`services/reference_assembly.py`, built on the same `group_read_sets` pairing
logic, and the card consumes it the same way -- resolved by the orchestrator
as an async project listing, kept out of the synchronous card builder.

`is_long_read` is **not** `not is_short_read`, and this is the single most
tempting wrong edit in this change. `is_short_read` returns `False` for a
protein FASTA, for a FASTQ whose platform is unknown and whose chemistry is
not `short`, and for genuine long reads alike. Negating it would hand Medaka
every non-short object in the project.

Written positively, it reuses the same platform-first precedence
`is_short_read` documents: a known long-read platform is decisive, chemistry
votes only when the platform is unknown, and **unknown stays unknown**
(returns `False`).

That last clause is a deliberate asymmetry with its sibling.
`is_short_read` counts an unlabelled FASTQ as short, because `_qc_platform`
defaults to ILLUMINA and that module declines to second-guess the default --
without it, an uploaded Illumina FASTQ carrying no metadata would never get a
polish card. Inheriting that default here would invert its meaning: every
unlabelled file would look like a Medaka candidate as well, and the two cards
would both fire on data neither can vouch for. The residual cost is that an
uploaded ONT FASTQ with no metadata gets no Medaka card until QC runs, which
is a missing offer rather than a wrong run.

## Outputs and facts

The output object is a new FASTA, `consensus.fasta`, stored beside the draft,
matching what `polish_assembly` does with `polished.fasta`.

Facts recorded on it:

| Fact | Source |
|---|---|
| `polish_tool` | `"medaka"` |
| `polish_tool_version` | probe |
| `polish_model` | parsed from Medaka's stderr |
| `polish_model_auto_resolved` | whether Medaka inferred it or fell back |
| `polish_bacteria_mode` | whether `--bacteria` was passed |
| `polish_read_files` | count |
| `polish_changed_positions` | computed, see below |

`polish_tool` is new relative to Polypolish, which records
`polish_tool_version` without naming the tool -- it did not need to, being the
only polisher. With two polishers writing the same fact namespace onto the
same object role, a run that does not say which tool produced it is
unreadable. `polish_assembly` gains `polish_tool = "polypolish"` in the same
change, so the two are comparable rather than one being annotated and the
other inferred by absence.

### Why `polish_changed_positions` is computed rather than parsed

Polypolish prints a per-contig tally to stderr and `parse_polish_stderr`
sums it. Medaka prints nothing comparable: it writes a consensus and stops.
`stitch`'s only output line is "Polished assembly written to ... have a nice
day."

R5 requires evidence that positions actually changed, which is the bar #23
set for Polypolish and the reason that criterion is in the issue at all.
Without a computed count, a Medaka job could report "polishing complete" for a
run that changed nothing -- or for one that silently reused a stale output
directory -- and no fact on the object would distinguish it from a run that
corrected a thousand errors.

So the handler computes it: a per-contig comparison of draft against
consensus, matching contigs by name, summing substitution differences and
recording length deltas where the sequences differ in length. This is
alignment-free by design. An aligner in the fact-gathering path would be a
second failure surface for a number that exists to make failures visible, and
Medaka's consensus preserves contig identity and order, so a name-keyed
comparison is well-defined. Where a contig's length changed, the position
count is reported alongside `polish_length_delta` rather than being forced
into a substitution count that would be meaningless.

### Why the model facts exist

Medaka selects its network from basecaller metadata embedded in the FASTQ, and
**falls back to a legacy default model when it cannot find any**, succeeding
with worse output and no error. `medaka_consensus` takes this path whenever
the FASTQ was not produced by a recent basecaller writing tagged output.

Nothing in the resulting object would show that. `polish_model` and
`polish_model_auto_resolved` are what make an under-performing polish
diagnosable after the fact, and they are the same category of fact as
Polypolish's `polish_careful_mode`: a decision the tool made about how to do
its job, recorded because the output alone does not reveal it.

## Suggestion behavior

A new `build_polish_long_card`, kind `polish_long`, category
`REFERENCE_ASSEMBLY`, anchored on the assembly. Gates, in order:

1. Assembly-like and READY, else no card.
2. Medaka installed, else UNAVAILABLE with the probe's error.
3. Exactly one long-read set in the project.

Zero long-read sets and several long-read sets are both UNAVAILABLE with a
reason naming the situation. This is `build_polish_card`'s rule and it is
copied deliberately: cards launch directly with the body they carry, with no
dialog between the button and the queue, so a card that picked one of several
read sets would silently polish with whichever it chose. An assembly polished
against the wrong sample's reads is plausible and quietly wrong.

**A separate card rather than a smarter one.** The alternative -- one polish
card that inspects the reads and dispatches -- was rejected. The two tools
take different reads, produce different facts, and fail for different reasons;
merging them makes the card's description, reason string, launch body, and
node ports all conditional, and forces a project holding both chemistries to
have one tool chosen for it behind the user's back. That is precisely the
guess `build_polish_card`'s docstring exists to forbid.

R3 then falls out of the structure rather than needing enforcement. The two
cards gate on mutually exclusive predicates over the same read objects, so a
short-read project sees Polypolish, an ONT project sees Medaka, a hybrid
project sees both as separate legitimate offers, and neither card can
recommend a tool for reads it cannot use. There is no combination to get
wrong because there is no combining step.

`--bacteria` is an opt-in from the launch dialog, not a card decision --
matching iVar's primer scheme and completeness's lineage override. The card
offers the tool; it does not guess that this assembly is a bacterial isolate.
The dialog notes that ONT labels the bacterial model a research release with
minimal support, so the opt-in is informed.

## Registries

Per `CLAUDE.md`'s note on hand-maintained registries keyed by an enum, this
change touches four and each needs its entry:

- `TOOL_META["medaka"]` -- `test_every_tool_is_documented` fails without it.
- `node_types.NODE_TYPES["polish_long"]` plus `_launch_polish_long`.
  `TestExhaustiveness` must be run as a whole class, not one test: a spec
  entry and an exclusion entry both satisfy `test_every_launch_function_is_classified`
  while colliding on `test_no_launcher_is_both_used_and_excluded`, which is
  the #355 failure.
- `suggestion_service`'s card tuple list.
- `config.medaka_path`.

## Tool metadata

`TOOL_META["medaka"]` carries `homepage`, `citation`, `license`, and `usage`,
all verified against the project's own repository rather than recalled, per
`CLAUDE.md`'s rule.

**The license is the notable one.** Medaka is distributed under the *Oxford
Nanopore Technologies PLC Public License Version 1.0*, not an OSI-standard
license. Every other entry in `TOOL_META` is MIT, GPL, or BSD-shaped, so this
is the first non-standard license the `/help/software` page renders. It is
recorded verbatim rather than normalized to something familiar-looking, and
`usage` notes that Medaka is ONT's own tool under ONT's own terms. A page that
reads as authoritative saying "MIT" here would be worse than saying nothing.

`usage` describes behavior rather than flags, per the standing rule: that
BioFlow runs `medaka_consensus` over a draft and the long reads it was built
from, that Medaka performs its own alignment internally with a
model-dependent minimap2 preset, and that the resolved model is recorded as a
fact because Medaka falls back to a default when basecaller metadata is
absent.

`sources.py` gets its parallel entry, which has its own completeness test.

## Resources

Medaka's inference is the cost, and it is CPU-bound here by construction of
the `pytorch-cpu` pin. `--batch_size` defaults to 100 and controls memory;
the default is kept, since the upstream guidance for lowering it is about GPU
memory, which does not apply.

Lease and resource declaration follow `POLISH_LEASE_SECONDS`' shape. Unlike
Polypolish -- where the comment correctly notes peak RSS describes bwa-mem2's
index and therefore scales with the *draft* -- Medaka's peak scales with
batch size and model, near-flat in draft size, with runtime scaling in
depth times draft length. The handler carries that note, because a memory-model
fit that assumes the Polypolish shape is wrong in both directions.

## Testing

**Runner unit tests.** `-f` present unconditionally; `--bacteria` present only
when asked and absent otherwise; no minimap2 arguments constructed anywhere;
model-line parsing including the fallback case.

**`count_changed_positions`.** Identical sequences give zero; a known
substitution count is recovered exactly; a length change is reported as a
length delta rather than folded into a substitution count; a contig present in
one file and not the other does not raise.

**Card tests, asserting the unavailable direction.** Per `CLAUDE.md`: the
image ships tools installed, so an "available" assertion passes whether or not
the patch worked. The tests that matter assert the card flips to UNAVAILABLE
when `tools.medaka` is patched off, when the project has no long reads, and
when it has two long-read sets. A pairing test asserts a short-read-only
project yields a Polypolish card and no Medaka card, and the reverse.

**`is_long_read` against real objects.** Per `CLAUDE.md`'s "check a rule
against the real database" note, and specifically because `is_short_read`'s
docstring records that a fixture-only rule was wrong here before:
`ERR16145610.fastq` is a MinION run whose inferred chemistry is `short`. That
file must be classified long by `is_long_read` and non-short by
`is_short_read`, and it is checked with a `docker compose exec api python -c`
against real objects, not only a fixture.

**End-to-end, satisfying R5.** A synthetic draft with a known number of
planted single-base errors and ONT-like reads over it, mirroring what #23 did
for Polypolish: assert the planted errors were corrected, that
`polish_changed_positions` is non-zero, and that it corresponds to the
planted count. Completion alone is explicitly not the assertion.

## Risks

**The model fallback is invisible without the facts.** Mitigated by recording
them, not prevented. A user with untagged FASTQs gets a legacy model and a
worse polish; the facts make that diagnosable rather than impossible.

**Image size.** The layer is real -- medaka plus a CPU torch is on the order
of a few hundred MB. The `pytorch-cpu` pin is what keeps it from being a
gigabyte more, and that pin has no test behind it: a future dependency bump
that drops it would silently reintroduce the CUDA build. The install script
comment is the only guard, which is the same guard the `flye-samtools` shim
relies on.

**ONT's license is not OSI-standard.** Recorded verbatim. It permits the
research and internal use this application is for, but it is a different
class of grant from every other tool here and a user redistributing BioFlow
should see it stated accurately.

**`count_changed_positions` is alignment-free.** If a future Medaka release
renames or reorders contigs, the name-keyed comparison degrades to reporting
unmatched contigs rather than a count. That is a visible degradation rather
than a wrong number, which is the trade taken deliberately over introducing an
aligner into the fact-gathering path.
