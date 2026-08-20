# Short-read metagenome assembly: SPAdes `--meta` vs MEGAHIT — design

Date: 2026-08-20.

Closes [#731](https://github.com/syntheticgio/bioflow/issues/731). Child 5 of
[#630](https://github.com/syntheticgio/bioflow/issues/630), independent of the
others; see the epic spec's decision M1 and open question 1.

#731 is a **decision issue before it is a build issue**: does SPAdes `--meta`
cover the short-read case well enough that MEGAHIT is not worth a full tool
registration?

## What exists today

Verified against this worktree on 2026-08-20:

- **`SPADES_SPEC`** is registered with `tool=tools.spades`, `layout="paired"`,
  a memory model (`bytes_per_genome_base=90.0`, `bytes_per_read_base=0.6`,
  `fixed_overhead_mb=4096`), and outputs confirmed "against a real 4.3.0 run of
  the bundled test dataset, not read from documentation".
- **Its `mode` field is a select over `isolate` / `careful` / `standard`.**
  There is **no `meta`**.
- **`_spades_command`** (`assembly_runner.py:155`) maps that select to flags:
  `isolate` → `--isolate`, `careful` → `--careful`, and `standard` → neither
  ("BioFlow's name for neither flag; SPAdes has no such option"). Memory is
  passed as `-m <GB>`.
- **MEGAHIT is absent** from the codebase.

## Decision N1: SPAdes `--meta` is a fourth `mode` choice, and it is nearly free

The mode field is **already a mutually-exclusive select**, and SPAdes' `--meta`
is genuinely mutually exclusive with `--isolate` and `--careful` — the tool
rejects those combinations. So `meta` slots in as a fourth choice with no
structural change: one `Choice` in the registry, one `elif` in the command
builder.

**This is materially different from Flye's case** (#727), where `--meta` is
*orthogonal* to the accuracy mode and therefore had to be a separate checkbox.
Here the exclusivity the select already enforces is exactly the exclusivity the
tool wants. Worth stating because implementing the two the same way would be
wrong in one direction or the other.

So the short-read metagenome capability costs roughly the same as #727 did for
long reads, and MEGAHIT's cost is the full six-registry tool addition. That
asymmetry, not a quality comparison, is the dominant term.

## Decision N2: ship SPAdes `--meta` first; make MEGAHIT earn its place with
## measurements

**Do the cheap thing, then measure.** Add `meta` to `SPADES_SPEC`, and only
then ask whether MEGAHIT is worth adding.

Why this ordering rather than a bake-off first: a comparison needs a working
metaSPAdes path to compare against, and building that path *is* most of option
A. Running the bake-off first would mean standing up a throwaway metaSPAdes
invocation, measuring, and then implementing it properly anyway.

**What would justify MEGAHIT**, stated up front so the comparison is not
post-hoc:

- **Memory.** metaSPAdes is memory-hungry — the registry already models SPAdes
  at 90 bytes per genome base plus 0.6 per read base, the heaviest of the
  three assemblers here. MEGAHIT's entire reason for existing is assembling
  large complex communities in bounded memory. On the **local single-machine
  workloads this app targets**, an assembler that finishes where the other OOMs
  is not a marginal improvement; it is the difference between having the
  capability and not.
- **Wall time** on a complex community, if the difference is large.

**What would not justify it:** contig N50 or total assembly size differences of
a few percent. Both are legitimate assemblers; a modest quality edge does not
pay for six hand-maintained registries, four of which fail silently.

The measurement to record, per #731's own request, regardless of outcome:
assembly size, contig N50, wall time, **peak RSS** — the last being the one
that decides it.

## Decision N3: if MEGAHIT is added, it is a new `Assembler`, not a SPAdes mode

Recorded so the shape is not re-litigated: MEGAHIT is a different binary with
different outputs (`final.contigs.fa`), a different memory profile (succinct de
Bruijn graph, bounded by `-m`), and no `--careful`-style modes. It gets its own
`AssemblerSpec` — which is precisely what `assembler_registry`'s docstring says
the registry exists for ("the cost of the registry is a file; the cost of not
having one is discovered later, when hifiasm arrives and its mode flags, output
filenames and memory profile all differ from Flye's").

Its `bytes_per_genome_base` must be **measured, not copied from SPAdes** —
bounded memory is the entire claim being tested, so inheriting SPAdes' 90 would
model away the reason for adding it.

## Requirements

- **R1.** A user can assemble paired short reads in metagenome mode.
- **R2.** Selecting metagenome mode emits `--meta` and neither `--isolate` nor
  `--careful`.
- **R3.** The existing three modes emit exactly what they emit today.
- **R4.** The resulting contigs carry `assembly_meta_mode` (matching #727), so
  #728's binning card can gate on it.
- **R5.** A comparison of metaSPAdes against MEGAHIT on one real short-read
  community — assembly size, N50, wall time, peak RSS — is recorded on this
  issue, whichever way it comes out.
- **R6.** The decision on MEGAHIT is written down with its reason.

## Testing

- **R2/R3** — command-builder tests. R3 as a **full-argv equality** assertion
  for each existing mode, not a negative check: this edits a builder every
  existing SPAdes run goes through, and an exact assertion catches a reordering
  or a dropped flag that `"--meta" not in argv` would sail past.
- **Mutual exclusivity** — `meta` emits neither `--isolate` nor `--careful`.
  The tool rejects those combinations, so a builder that emitted both would
  fail late and unhelpfully.
- **R4** — the fact is present after a meta run.

## Verify before implementing

1. **Does the installed SPAdes accept `--meta` with the `layout="paired"`
   invocation** the runner builds (`-1`/`-2`)? metaSPAdes historically required
   paired input and rejected single-end — if so, the mode must be unavailable
   or explained for unpaired read sets rather than failing at run time.
2. **Does `--meta` change output filenames?** The registry's comment records
   that SPAdes' outputs were confirmed against a real 4.3.0 run; do the same
   here rather than assuming `contigs.fasta` persists.
3. **Whether `--meta` and `-m <GB>` interact** — metaSPAdes' memory behaviour
   under a cap is the thing MEGAHIT is being compared on, so it matters that
   the cap is honoured rather than advisory.

## Outcome: the measurements, and the decision (2026-08-20)

Both parts of the plan ran. SPAdes `--meta` shipped, and the bake-off was then
run against it. **MEGAHIT is justified**, on the memory criterion N2 named in
advance.

### The spikes, first — two of the three assumptions were wrong

- **metaSPAdes rejects single-end input.** Verified against the installed
  4.3.0: it exits 0 on the SE invocation only in the sense that the wrapper
  returns, and the log carries `ERROR ... current version of metaSPAdes can
  work either with single library (paired-end only) or in hybrid ... mode`,
  with **no `contigs.fasta` produced**. It refuses *after* read error
  correction, i.e. minutes into a real run — so the mode is gated at launch
  (`pipeline_service.launch_assembly`) rather than left to fail late.
- **`--meta` does not change the output filenames.** `contigs.fasta` and
  `assembly_graph_with_scaffolds.gfa` are both still produced, so
  `SPADES_SPEC.outputs` needed no meta-specific entries. Confirmed by running
  it, per the registry's existing standard.
- **`-m` binds hard.** metaSPAdes terminates on reaching it, exactly as its
  help text claims — which is what made the decisive measurement possible.

### The bake-off

One synthetic short-read community: 5 organisms, 4.2 Mbp total, a **30x
abundance spread** (30x down to 1x), a shared 1.5 kb repeat element seeded
~20x per genome so the graph tangles between organisms, ART HS25 150bp paired
reads. metaSPAdes 4.3.0 (the version this app ships) and MEGAHIT 1.2.9, same
input, 4 threads, aarch64, peak RSS via GNU time.

| | metaSPAdes | MEGAHIT |
|---|---|---|
| Assembly size | 3,526,808 | 3,231,398 |
| Contigs | 1,767 | 1,089 |
| **Contig N50** | 32,204 | **34,859** |
| Largest contig | 175,649 | 175,822 |
| **Wall time** | 80.5 s | **28.6 s** |
| **Peak RSS** | **776 MB** | **294 MB** |

Quality is a wash — MEGAHIT's N50 is 8% higher on 3% less assembly, which is
exactly the "few percent" N2 said would *not* justify the addition. Speed is
2.8x, which N2 listed as supporting rather than deciding.

### The measurement that decided it

N2's memory criterion is about *bounded* memory, not a smaller number on a
small sample. So both were re-run under a cap below metaSPAdes' observed peak:

| Cap | metaSPAdes | MEGAHIT |
|---|---|---|
| ~1 GB / 500 MB budget | **failed, exit 66**, `mmap(2) failed. Reason: Cannot allocate memory`, no contigs | **completed**, 301 MB peak |

The capped MEGAHIT assembly is **byte-identical in its statistics** to its
uncapped one (3,231,398 bases, 1,089 contigs, N50 34,859) — the bounded memory
is real, not a quality trade.

This is precisely N2's stated bar: *"an assembler that finishes where the other
OOMs is not a marginal improvement; it is the difference between having the
capability and not"*, on the local single-machine workloads this app targets.

**Decision: MEGAHIT earns its place.** Not for quality, and not primarily for
speed — for completing a community assembly inside a memory budget where
metaSPAdes dies. Per N3 it becomes a normal tool addition with its own
`Assembler` and `AssemblerSpec`, cut as its own issue citing these numbers,
with its `bytes_per_genome_base` **measured rather than inherited from SPAdes**.

### Caveat on generality

One synthetic community on one machine. The abundance skew and inter-organism
repeats are modelled deliberately, but a real gut or soil metagenome is far
more complex, and complexity is the axis MEGAHIT's advantage grows along — so
this measurement is a lower bound on the gap, not an upper one. Recorded here
so the next person does not re-run it, per #731's request.

### SPAdes `--meta` still ships, and is not wasted

It was the cheap path (one `Choice`, one `elif`), it is the only metagenome
option until MEGAHIT lands, and it remains the better choice where memory is
not the binding constraint — its assembly was 9% larger here. Shipping it also
produced the metaSPAdes baseline this decision rests on.

## Out of scope

- **Adding MEGAHIT in this issue.** N2 — it becomes a normal tool addition
  issue *if* the measurements justify it, and this issue records the decision
  either way.
- **Short-read binning.** #728 works on contigs regardless of which assembler
  produced them.
- **Hybrid assembly** of long and short reads for metagenomes.
