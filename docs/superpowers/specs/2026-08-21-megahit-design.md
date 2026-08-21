# MEGAHIT for short-read metagenome assembly — design

Date: 2026-08-21.

Implements [#781](https://github.com/syntheticgio/bioflow/issues/781), the
last open child of [#630](https://github.com/syntheticgio/bioflow/issues/630).

#781 is already argued: [#731](https://github.com/syntheticgio/bioflow/issues/731)
ran the bake-off and MEGAHIT won on the criterion set in advance — under a
500 MB cap metaSPAdes terminated with `Cannot allocate memory` and produced
nothing, while MEGAHIT finished and produced an assembly statistically
identical to its uncapped run. This document does not re-litigate that. It
settles the things #781 left to implementation, each of which is a way this
lands wrong and silently.

## What #781 already fixed, and is not reopened here

- **Its own `Assembler` member and `AssemblerSpec`**, not a SPAdes mode —
  decision N3 of `2026-08-20-short-read-metagenome-assembly-design.md`.
- **Bioconda, not a release binary.** Re-verified against the anaconda.org API
  on 2026-08-21: `megahit` 1.2.9 publishes `linux-64` **and**
  `linux-aarch64`. So there is no arm64 skip and `tools.megahit()` needs no
  architecture branch — unlike CheckM2 (#784) and SPAdes.
- **`bytes_per_genome_base` must be measured**, not copied from SPAdes.

## Decision G1: reuse the `assemble_reads` job type

#781 asks whether MEGAHIT needs its own job type and says it is "worth
deciding first". It does not need one.

`assemble_reads` already dispatches on `params.assembler` — the handler picks
a command builder, a progress parser and a facts parser per assembler, and
`_apply_assemble_reads` ingests contigs plus an optional graph without caring
which tool produced them. A new job type would duplicate all of that to gain
nothing, and would drag in the three registries CLAUDE.md warns about
(`node_types.NODE_TYPES`, `running_now.ENDPOINT_JOB_TYPES`,
`provenance_walker._NO_NARRATIVE_STEP`), each of which fails silently.

**Consequence: registries 3–6 of #781's list collapse to one.** Only
`tools.all_tools()` needs the new entry; the job-type trio does not.

## Decision G2: `-o` must not exist, and the handler creates it

**This is the failure that would otherwise hit every single run**, so it is
the first decision rather than a footnote.

`assembly_handlers` does `out_dir.mkdir(parents=True, exist_ok=True)` before
building any command — correct for Flye (`--out-dir`), ABySS (`-C`) and
SPAdes (`-o`), all of which accept an existing directory. MEGAHIT does not.
From its own wrapper source at v1.2.9:

```python
if not opt.force_overwrite and not opt.test_mode and os.path.exists(opt.out_dir):
    raise Usage('Output directory ' + opt.out_dir + ' already exists, ...')
```

So a MEGAHIT run through the existing handler fails instantly, every time,
before assembling anything.

**Chosen: pass `--force`.** The alternative — teaching the handler not to
create `out_dir` for this one assembler — makes a shared code path
conditional on the tool, which is the shape that breaks the *next* assembler
silently. `--force` is safe here because the directory is a per-job scratch
dir the handler just made: there is nothing of anyone's to overwrite.

## Decision G3: `-m` is bytes-or-fraction, not gigabytes

SPAdes' `-m` is **gigabytes** and is a **ceiling it dies at**.
MEGAHIT's `-m` is neither:

- **Units.** `-m/--memory <float>`: "max memory in byte to be used in SdBG
  construction (if set between 0-1, fraction of the machine's total memory)".
  Bytes, not GB. `-m 4` to MEGAHIT means *four bytes*, not four gigabytes.
- **Semantics.** It is a budget MEGAHIT *plans within* — it adjusts its SdBG
  build to fit — not a wall it terminates at. That is precisely why it
  survived the capped bake-off metaSPAdes died in.

Both halves are silent when wrong. `MIN_SPADES_MEMORY_GB`-style reasoning
copied over would pass `-m 4`, which is a legal float MEGAHIT would accept
and interpret as a four-byte budget.

**Chosen: pass `memory_bytes` directly, floored at
`MIN_MEGAHIT_MEMORY_BYTES` (2 GiB), and never a fraction.** Passing bytes
keeps the one number the guard used and the one number the tool gets
identical — the same argument `_abyss_command`'s Bloom budget makes. The
fraction form is deliberately unused: it would make the run's memory depend
on the host rather than on the estimate that admitted it, so two runs with
identical recorded parameters would behave differently on different machines.

## Decision G4: MEGAHIT is always a metagenome assembler

MEGAHIT has no isolate/meta switch — it is a metagenome assembler by
construction. So `assembly_meta_mode` should be set unconditionally for it,
which #781 states and which needs one small change to honour.

`results.assembly_provenance` records the fact from the params:

```python
if params.get("meta") or params.get("mode") == "meta":
```

`MegahitParams` has neither a `meta` boolean (Flye's spelling) nor a `mode`
(SPAdes'). Rather than give it a vestigial always-true `meta` field — a
parameter recorded in provenance that no flag corresponds to, which is the
lie `assembly_runner`'s docstring refuses for genome size — **the params
class answers the question directly**: `_is_meta_assembly` gains a
`MegahitParams` branch returning `True`, and `assembly_provenance` learns the
same. The fact stays one key across three assemblers, which is what
`suggestion_service`'s binning card reads.

**Consequence:** the binning card offers MEGAHIT assemblies without its
"not assembled in metagenome mode" caveat, which is correct — they always were.

## Decision G5: no genome-size term in the memory model

`AssemblyMemoryModel` carries `bytes_per_genome_base` and
`bytes_per_read_base`. For MEGAHIT the genome term is not merely small, it is
meaningless — a community has no genome size, which is the same reasoning
`FLYE_SPEC.meta_memory_model` and `SPADES_SPEC.meta_memory_model` already
apply. MEGAHIT is *only* ever meta, so it needs no second model: its single
`memory_model` is the meta one.

`bytes_per_genome_base=0.0`, with the weight on `bytes_per_read_base`,
measured on this hardware per #781's constraint. A copied SPAdes 90 would
model away the reason for adding the tool.

**Measured 2026-08-21** — MEGAHIT 1.2.9, linux-aarch64, 4 threads, peak RSS
via GNU `time -v`, over synthetic communities of 5 to 150 genomes:

| Read bases | Peak RSS |
|---|---|
| 2.8 Mbp | 268 MB |
| 11.4 Mbp | 284 MB |
| 34.1 Mbp | 325 MB |
| 85.3 Mbp | 418 MB |

A near-perfect line: **1.91 bytes per read base on a 263 MB intercept**.
Shipped as `2.5` on a `384` MB floor so every measured point sits under the
estimate rather than on it — #727's bias, that low is an OOM and high is an
overridable warning.

**The trap in re-measuring this.** At a *fixed* community size, peak RSS is
flat against read depth (1.4 Mbp and 80.7 Mbp over the same five genomes both
peaked at ~272 MB) and flat against `-m` (500 MB, 2 GB, 8 GB and the default
all gave ~266 MB on one community — MEGAHIT takes what the graph needs rather
than pre-allocating the budget). What the coefficient actually tracks is
community *complexity*, for which read volume is the only proxy available at
launch time. A re-measurement that varies depth alone will see a flat line and
wrongly conclude the coefficient should be zero.

## Decision G6: reachable exactly as SPAdes is; the default does not move

`spec_for_chemistry` routes `SHORT` to ABySS, and the assemble dialog has no
assembler picker — it renders whatever `default_assembly_params` chose. So
MEGAHIT is reachable by an API caller passing `assembler: "megahit"` (which
`assembly_params.from_dict` dispatches on and `launch_assembly` enqueues from
`parsed.assembler`), and not from the dialog.

That is **the same reachability SPAdes has today**, and
`test_short_reads_still_route_to_abyss_after_spades_is_installed` already
records the reasoning: "Installing an assembler makes it selectable.
Promoting it to the default changes every existing user's results and is a
separate decision." The same holds here, doubly — routing every short-read
assembly to a metagenome assembler would be wrong for the isolates that are
this app's common case.

**Out of scope, and worth its own issue:** an assembler picker in the
dialog. Without one, MEGAHIT ships correct and hard to reach. Filing that
rather than smuggling a UI decision into a tool-addition PR.

## Requirements

- **R1.** A caller can assemble paired short reads with MEGAHIT.
- **R2.** MEGAHIT's argv carries `--force`, so a pre-created `out_dir` does
  not fail the run.
- **R3.** `-m` receives a byte count, floored, never a 0–1 fraction.
- **R4.** The three existing assemblers' argv is byte-for-byte unchanged.
- **R5.** Contigs produced by MEGAHIT carry `assembly_meta_mode: true`.
- **R6.** `megahit` is probed, listed in `all_tools()`, and described in
  `TOOL_META` with a license and citation verified against upstream.
- **R7.** `bytes_per_read_base` is measured on this hardware and the numbers
  are recorded in the spec and the registry comment.

## Testing

- **R2/R3** — command-builder tests, including an explicit assertion that no
  argument to `-m` is in `[0, 1]`.
- **R4** — **full-argv equality** for Flye, ABySS and each SPAdes mode, not a
  negative check. This edits a builder every existing assembly goes through;
  the same reasoning #731's spec gave.
- **R5** — `assembly_provenance` over MEGAHIT params sets the fact, plus the
  suggestion-service binning card dropping its caveat.
- **Exhaustiveness** — `TestExhaustiveness` in `test_assembler_registry.py`
  must be run whole, not just the new case: it is the registry-pair CLAUDE.md
  warns about, where a fix that adds an entry collides with one that excludes
  it.
- **Availability** — patch `spec_for`, not `tools.megahit` (the frozen spec
  captured the function at import), and assert the card flips to *unavailable*
  when the probe is patched off.

## Verification — done 2026-08-21

All three against a real bioconda env on linux/arm64, built exactly as
`install-megahit.sh` builds it (including its `rm -rf` trimming).

1. **A real run, not `--version`.** The install script ends with a genuine
   two-organism assembly. It found a real bug that `--version` alone would
   have shipped — see G7 below.
2. **Outputs confirmed against that run.** `final.contigs.fa` produced, 2
   contigs from a 2-organism fixture (29,904 and 29,964 bp from two 30 kb
   genomes), so the assembler separated the community rather than merging it.
3. **`--force` confirmed necessary**, not assumed: with a pre-created `-o`
   and no `--force`, exit 1 and no assembly. With `--force`, exit 0. This is
   exactly what `assembly_handlers` does to every assembler, so G2 was a real
   bug and not a defensive guess.
4. **The memory coefficient measured** across four read volumes spanning 30x
   (see G5).

## Decision G7: the wrapper must put the env's bin on PATH

Found by the smoke test in (1), and it would have shipped a completely broken
tool otherwise.

`megahit` is not a binary — it is a Python script whose shebang is
`#!/usr/bin/env python3`, which is why `python` is a declared dependency of
the bioconda package. `install-metabat2.sh`'s wrapper shape, copied verbatim:

```sh
#!/bin/sh
exec /opt/megahit/env/bin/megahit "$@"
```

…resolves `python3` against the **caller's** PATH — the image's `/usr/bin`,
which has no `python3` — so every invocation dies with `env: 'python3': No
such file or directory`. Verified against the trimmed env.

The wrapper therefore prepends the env's own bin. That also keeps the
`megahit_core` / `megahit_core_popcnt` / `megahit_core_no_hw_accel` binaries
resolvable by name, which is how the wrapper execs them after its CPUID
probe.

This is the CLAUDE.md rule about ending an install script with a real run
rather than `--version` paying for itself — except that here even `--version`
would have caught it, and the reason it nearly did not is that the failure is
in the *wrapper*, which no amount of reading the conda package would reveal.

## Out of scope

- **An assembler picker in the assemble dialog** (G6). Filed separately.
- **Promoting MEGAHIT to the short-read default** (G6).
- **`--presets meta-sensitive` / `meta-large`.** Real knobs, but a preset that
  changes k-list and min-count is a parameter someone should choose knowing
  why; the default k-list is right for a first release.
