# Short-read de novo assembly

Date: 2026-08-17.

Closes [#490](https://github.com/syntheticgio/bioflow/issues/490). Extends
`docs/superpowers/specs/2026-08-01-de-novo-assembly-design.md`, which built the
long-read half and shaped the registry for exactly this addition.

## Problem

A user selecting a short-read FASTQ sees an Actions card reading "Short-read
assembly is not installed. Only long reads can be assembled here." That
sentence is accurate today -- Flye is the only installed assembler and it has
no short-read mode -- but it is a dead end on the most common data type in the
library.

The 2026-08-01 design anticipated this. `SPADES_SPEC` already exists in
`assembler_registry.py` as a declared-but-not-installed placeholder carrying
`layout="paired"` and that exact refusal string, and `spec_for_chemistry`'s
docstring names itself as "the single place that changes" when a second
assembler arrives. This change takes that seam up.

## A correction to the prior spec

The 2026-08-01 design states that "**SPAdes** is packaged (3.15.5) but
deferred by decision." That was true for Debian bookworm. **It is not true for
trixie, which the image is now built on** -- `apt-cache policy spades` reports
no candidate, and the same is true of `megahit` and `soapdenovo2`. Verified in
the running `api` container on 2026-08-17.

What trixie does package, of the short-read assemblers worth considering:

| Package | Version | Status |
|---|---|---|
| `abyss` | 2.3.10-1+b1 | Actively maintained upstream |
| `velvet` | 1.2.10+dfsg1-9 | Upstream dead since 2014 |
| `idba` | 1.1.3-8+b1 | Upstream inactive |
| `minia` | 3.2.6-4+b4 | Maintained, low-memory niche |

This spec therefore builds on **ABySS**, not SPAdes. The decision and its
consequences are in Scope below.

## Scope

**ABySS only, in this change. SPAdes is a follow-up issue.**

- **ABySS 2.3.10** is one `apt-get install` line with no build risk, handles
  paired-end natively, and is genuinely good at bacterial-isolate scale, which
  is the realistic ceiling for short-read assembly on a workstation anyway.
- **SPAdes** produces better assemblies on isolates and is what most users mean
  by "short-read assembly". Reaching it needs a vendored upstream tarball
  following the `install-meryl.sh` pattern already in `backend/Dockerfile`,
  with arm64 validation. Filed as
  [#519](https://github.com/syntheticgio/bioflow/issues/519); once the runner,
  card, pairing and param scaffolding here exist, adding it is a spec plus an
  install script rather than another six-file edit.
- **Velvet** is packaged and was named in #490. It is deliberately excluded:
  shipping a card that recommends a tool abandoned in 2014, whose assemblies
  are measurably worse than ABySS's on the same reads, is building something
  we would then have to advise against using.

Not in scope: metagenome assembly workflows, read correction ahead of
assembly, and hifiasm (the registry's other placeholder, blocked on an
unrelated arm64 SIMD source build).

## Verified tool behaviour

Everything in this section was established by running ABySS 2.3.10 in the
`api` container against a synthetic 6 kb paired-end dataset on 2026-08-17, not
recalled. Three findings change the design, and each would have produced a
plausible-looking wrong implementation if assumed.

**`abyss-pe` is a GNU Make wrapper, not a conventional CLI.** `abyss-pe
--help` prints `make`'s own option list. Parameters are Make variable
assignments, not flags:

```
abyss-pe name=asm k=51 j=4 B=200M in='r1.fq r2.fq'
```

`build_assembly_command`'s existing shape -- a list of argv tokens -- still
works, but the SPAdes-style `--flag value` intuition does not apply, and
`in=` takes a single space-joined string containing both mates.

**`B` is mandatory, not an optional cap.** Omitting it fails immediately with
"must specify either `B` or `np` for the Bloom filter and MPI modes
respectively." This inverts the usual relationship between BioFlow's memory
estimate and the tool: for Flye the estimate is advisory and used only for the
guard, whereas here a number must be passed for the run to start at all. See
Memory below.

**Output filenames are stable symlinks over numbered stage files.** A run
leaves `asm-1.fa` through `asm-8.fa` plus four convenience symlinks:

| Symlink | Points at | Meaning |
|---|---|---|
| `<name>-unitigs.fa` | `-3.fa` | Pre-scaffolding |
| `<name>-contigs.fa` | `-6.fa` | Contigs |
| `<name>-scaffolds.fa` | `-8.fa` | Final, paired-end scaffolded |
| `<name>-contigs.dot` / `-scaffolds.dot` | `-6.dot` / `-8.dot` | Graphs |

`<name>-scaffolds.fa` is the assembly and becomes the `CONTIGS` output.
Harvesting must resolve symlinks -- copying the link itself yields a dangling
reference once the working directory is cleaned.

**`<name>-stats.tab` is a genuine `INFO_TABLE`.** ABySS writes assembly
statistics itself, in three formats (`.tab`, `.csv`, `.md`), with columns
`n n:500 L50 min N75 N50 N25 E-size max sum name` and one row per output
stage. This is a better fit for `OutputKind.INFO_TABLE` than Flye's
`assembly_info.txt` and means N50 and friends need no separate computation.

**The graph format is Graphviz `.dot`, not GFA.** `AssemblyGraph.tsx` renders
Flye's `assembly_graph.gfa`. ABySS's `.dot` is a different format that the
existing viewer cannot read. See Deliberate limitations.

**`abyss-pe version` writes a spurious `test: -le: unary operator expected`
line to stderr** while still exiting successfully and printing the version.
The `tools.py` probe must tolerate stderr noise and parse the version from
stdout rather than treating any stderr output as failure.

## Design

### 1. Install and declare

`backend/Dockerfile` adds `abyss` to the existing apt list. The comment block
above `flye` is corrected: its claim that SPAdes is packaged is now false and
would otherwise mislead the next person to read it.

`tools.py` gains an `abyss()` probe on `abyss-pe`, an entry in `all_tools()`,
and a `TOOL_META` record. Per CLAUDE.md, `homepage`, `citation`, `license` and
`usage` are required by `test_every_tool_is_documented` and must be verified
against the ABySS repository rather than recalled. `usage` states behaviour --
that BioFlow runs it paired-end with a Bloom-filter budget derived from the
memory estimate -- not flags.

### 2. Chemistry routing

`spec_for_chemistry` becomes a real dispatch rather than a Flye constant:
`ReadChemistry.SHORT` returns the ABySS spec, the four long chemistries return
Flye, and `None`/`UNKNOWN` continue to return `None` so the "run QC first"
refusal is untouched.

`ABYSS_SPEC` replaces `SPADES_SPEC` in the registry. `Assembler.SPADES` stays
declared-but-unavailable so the follow-up issue has somewhere to land and the
API can still say "not installed in this build" rather than "unknown
assembler". `Assembler.ABYSS` is a new enum member.

Fields on the spec:

- **`k`** (int, default 51, range 16-127). The one parameter that most changes
  assembly quality. The default suits 100-150 bp Illumina reads at typical
  coverage; the help text says so rather than presenting 51 as universal.
- **`threads`** and **`genome_size`** come from `_SHARED_FIELDS` unchanged.

Deliberately not exposed: `B` (derived, see Memory), `np` (MPI, not a
single-machine workflow), and the scaffolding sub-parameters, which are tuning
knobs with no good default to present to someone who does not already know
what they do.

### 3. Paired input

`launch_assembly` grows an optional `mate_object_id`. When absent, it resolves
the mate through `pairing.py`, which already exists for exactly this and is
used by trim and align.

The rule, keyed on `pairing.verdict()`'s four outcomes:

| Verdict | Behaviour |
|---|---|
| `CONFIRMED` | Assemble paired. |
| `NAME_ONLY` | Assemble paired. Filename agreement with no read-ID evidence is the ordinary case for files whose IDs were never captured. |
| `REJECTED_READ_IDS` | Refuse, naming both files. |
| `REJECTED_LAYOUT` | Assemble single-end. |
| `NO_MATCH` | Assemble single-end. |

Refusing on `REJECTED_READ_IDS` rather than falling back to single-end is the
important case: two files that look like mates but demonstrably are not
produce a plausible assembly with no error, which is worse than a refusal the
user can act on.

Single-end short-read assembly still runs -- ABySS accepts `se=` -- but the
card says the reads are unpaired, because the result is meaningfully worse and
the user should know before spending the time rather than after.

### 4. Runner

`build_assembly_command` currently raises for anything that is not Flye. It
becomes a dispatch on the enum, with the ABySS builder emitting Make-variable
assignments and joining both mates into one `in=` value (or `se=` when
unpaired).

`AssemblyProgress` is Flye-specific: it matches `>>>STAGE:` and carries Flye's
seven-stage order. It splits into a small protocol with two implementations.
ABySS's stages are visible in its Make output as target names; the
implementation reports "step N of M" with no percentage, preserving the
original design's reasoning, which holds here for the same reason -- stage
durations differ by more than an order of magnitude.

Output harvesting resolves symlinks before copying.

### 5. Memory

`AssemblyMemoryModel` gains an optional `bytes_per_read_base` coefficient
defaulting to `0.0`, leaving Flye's model arithmetically unchanged. ABySS sets
it, because a de Bruijn assembler's peak is driven by distinct k-mers -- a
function of genome size *and* coverage -- where Flye's repeat graph is
dominated by the genome alone.

The estimate feeds two consumers, which is new:

1. The existing `resource_estimator` band and `replan_service` proposal at
   launch, unchanged in shape from Flye.
2. **The `B` value passed to the tool.** Set to the estimate minus the fixed
   overhead, floored at 200M. This is the inversion noted above: the number is
   no longer advisory.

Because `B` is mandatory, a run with no genome size still needs one. Where
Flye tolerates an absent estimate by skipping the guard, ABySS gets a
conservative default derived from the reads' own size. The recorded parameters
carry `genome_size_source` so an inferred number is never presented as a
measured one -- the existing field, used for its existing purpose.

When the band is `BLOCK`, `replan_service` proposes lower threads as it does
today, and additionally a larger `k`, which reduces the distinct-k-mer count
and therefore the peak.

### 6. Card and copy

The `ReadChemistry.SHORT` branch of `build_assemble_card` -- the string in
#490's screenshot -- is deleted. Short reads fall through to the normal
available path with `why` reading "Paired short reads, assembled with ABySS"
or "Unpaired short reads, assembled with ABySS."

Two new unavailable reasons, both actionable:

- Mate rejected on read IDs: names the two files and says they do not appear
  to be mates.
- ABySS not installed: the generic `spec.unavailable_reason` path, already
  present.

The "No assembler here handles X reads" catch-all stays. It remains
unreachable and remains correct to keep, for the reason its own comment gives.

## Testing

Command construction, progress parsing, symlink-resolving harvest, and the
memory model are pure functions over strings and paths, tested directly
without a container -- the split the 2026-08-01 design established.

Registry exhaustiveness, per CLAUDE.md's registry-audit guidance: every
`Assembler` member whose `tool` is not `None` must have a command builder.
This is the `_SIDECAR_ROLES` failure shape -- a declared assembler with no
builder would otherwise be dispatched to Flye's builder or skipped silently.
`Assembler.SPADES`, still `tool=None`, is correctly exempt.

Suggestion rules get cases for each `verdict()` outcome. Per CLAUDE.md's
warning about tests that silently read the host, the availability test asserts
the card flips to **unavailable** when `spec_for` is patched off, rather than
asserting availability on an image that ships the tool regardless -- that is
the direction that fails when the seam breaks. `spec_for` is the patch point;
patching `tools.abyss` does not reach a frozen dataclass that captured the
function object at import time.

Beyond unit tests, and per CLAUDE.md's "check a rule against the real
database" guidance: run the card rules against a real project containing
paired short reads before calling this done. The synthetic assembly used to
establish this spec's facts is not a substitute for that.

## Deliberate limitations

**The assembly graph will not render.** `AssemblyGraph.tsx` reads GFA; ABySS
emits Graphviz `.dot`. The graph is harvested and stored as a `GRAPH` output
so it is downloadable, but the in-app viewer will not display it. Converting
`.dot` to GFA, or teaching the viewer a second format, is out of scope here
and should be filed if it matters. This is stated plainly rather than left for
someone to discover as a bug.

**The memory coefficients are published guidance, not measurements on this
hardware.** Same caveat the Flye model carries. The band will be
approximately right and occasionally wrong in both directions.

**Realistically this is bacterial isolates and small genomes.** Short-read de
novo assembly of anything eukaryote-scale will be refused by the memory guard
on a workstation. That refusal is honest and preferable to an OOM kill hours
in, but it means the feature's practical reach is narrower than "short reads
can now be assembled" suggests.
