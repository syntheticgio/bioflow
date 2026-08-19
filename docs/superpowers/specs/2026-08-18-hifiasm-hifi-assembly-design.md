# hifiasm for HiFi and ONT duplex assembly

Date: 2026-08-18.

Closes [#617](https://github.com/syntheticgio/bioflow/issues/617). Completes the
`HIFIASM_SPEC` scaffolding `assembler_registry.py` has carried since
`docs/superpowers/specs/2026-08-01-de-novo-assembly-design.md`, and follows the
architecture-split precedent set by
`docs/superpowers/specs/2026-08-18-spades-short-read-assembly-design.md`.

## Problem

Flye is the only long-read assembler installed. It handles HiFi via
`--pacbio-hifi`, but hifiasm's string-graph approach produces more contiguous,
higher-QV assemblies from HiFi reads, and hifiasm is what most users mean by
"HiFi assembly".

`assembler_registry.py` already anticipates this: `HIFIASM_SPEC` exists with
`tool=None` and `unavailable_reason="hifiasm is not installed in this build."`,
and `assemblers.py` declares `Assembler.HIFIASM`. The registry was written
expecting this tool to arrive. This change fills it in.

## Three corrections to the issue

The issue's scope section is accurate about *which files* change. It is wrong
about three facts, each verified rather than recalled, and each changes the
work.

### 1. There is no binary to vendor, on either architecture

#617 says "install hifiasm (static build or compiled from source -- check
arm64 availability...following the precedent set for other HiFi-era tools like
Polypolish". That framing presumes an amd64 binary exists and only arm64 is in
question. It does not.

Verified 2026-08-18 via `gh api repos/chhylp123/hifiasm/releases`: the latest
release (`0.25.0`, tag `0.25.0`, "Hifiasm-0.25.0-r726") has **`assets: []`** --
zero release assets, for any platform. hifiasm is also **not packaged for
Debian trixie**, unlike flye and abyss which the Dockerfile installs from apt.

So unlike SPAdes -- binary on amd64, source on arm64 -- hifiasm is a **source
build on both architectures**. This is not a degraded outcome: the build is
cheap (below), and there is no upstream binary whose toolchain this repo would
otherwise be second-guessing.

### 2. The arm64 SIMD problem is real, and much cheaper than bwa-mem2's

`assemblers.py`'s comment predicts "a source build with the arm64 SIMD problem
bwa-mem2 already has a script for". The prediction is correct in kind and
pessimistic in degree.

The problem, confirmed by building it:

- The `Makefile` hardcodes `-msse4.2 -mpopcnt` in `CXXFLAGS`. `-mpopcnt` is a
  hard error on aarch64.
- `Levenshtein_distance.h` includes four x86 intrinsic headers -- `emmintrin.h`,
  `nmmintrin.h`, `smmintrin.h`, `immintrin.h` -- none of which exist on ARM. A
  stock arm64 build fails at `Levenshtein_distance.h:6:10: fatal error:
  emmintrin.h: No such file or directory`.

Upstream PR [#931](https://github.com/chhylp123/hifiasm/pull/931) ports this
with SIMDe. **It is open and unmerged**, so this repo cannot wait for it -- but
its diagnosis is sound and the approach below is the same one, applied as an
install-time transformation rather than a vendored fork.

The fix is three steps, no patch files: vendor SIMDe v0.8.2, redirect the four
includes in one header with `sed`, and swap the x86 flags for
`-march=armv8-a+simd -DSIMDE_ENABLE_NATIVE_ALIASES`.

**This is far cheaper than bwa-mem2's arm64 path**, which needs sse2neon, three
downloaded patches, a safestringlib build, CRLF normalization, and hand-written
`_mm_prefetch` cast fixes. hifiasm needs one `sed` and two flags.

### 3. `-include linux/types.h` is needed, and it is NOT an ARM issue

This is the trap worth writing down, because the obvious diagnosis is wrong and
sends you rewriting a port that is fine.

On trixie, hifiasm fails to compile with:

```
In file included from /usr/include/aarch64-linux-gnu/bits/sched.h:63,
                 from /usr/include/sched.h:43,
                 from /usr/include/pthread.h:22,
                 from CommandLines.h:5,
                 from Assembly.h:3,
                 from Assembly.cpp:5:
/usr/include/linux/sched/types.h:99:9: error: '__u32' does not name a type
```

Because this appeared while testing the SIMDe port, the natural reading is
"SIMDe broke the headers". **It did not.** Verified three ways:

- The error reproduces with SIMDe entirely absent from the compile line.
- The include chain shown above contains no SIMDe header -- it is
  `CommandLines.h -> pthread.h -> sched.h -> linux/sched/types.h`.
- **It reproduces identically on amd64**: 10 errors from a stock
  `g++ -msse4.2 -mpopcnt Assembly.cpp` in a `linux/amd64` `python:3.12-slim`
  container.

It is a glibc/kernel-header ordering problem in trixie, where
`linux/sched/types.h` is reached without `asm/types.h` having defined `__u32`.
`-include linux/types.h` fixes it: 82 errors to 0.

**Consequence for the install script: this flag is arch-independent and belongs
outside the arm64 branch.** Gating it behind arm64 -- the natural assumption,
since that is where it was discovered -- breaks the amd64 build.

## Verification

Built and run before this spec was written, natively on an arm64 host (not
emulated) and under emulation for amd64, both in `python:3.12-slim`, the same
base image `backend/Dockerfile` uses.

### arm64 (native)

| Check | Result |
|---|---|
| `make` with SIMDe + flags above | exit **0**, **21 s** on 24 cores |
| `file hifiasm` | `ELF 64-bit LSB pie executable, **ARM aarch64**` |
| `hifiasm --version` | `0.25.0-r726` |
| Stripped size | **3.4 MB** |
| Assembly of synthetic HiFi reads (60 kb genome, 1500x12 kb reads) | exit 0, **one 59,885 bp primary contig** |
| `--ont` mode, same input | exit 0, **one 59,962 bp primary contig**, peak RSS 0.225 GB |

### amd64 (emulated)

| Check | Result |
|---|---|
| Stock `make` (no SIMDe), `-include linux/types.h` | exit **0**, ~69 s wall including image build |
| `file hifiasm` | `ELF 64-bit LSB pie executable, **x86-64**` |
| `hifiasm --version` | `0.25.0-r726` |
| Stripped size | **4.2 MB** |

At 3-4 MB, hifiasm is the cheapest tool added to this image in some time --
two orders of magnitude smaller than SPAdes' 193 MB.

### An OOM worth recording

The first `--ont` smoke run was killed with rc=137 at both 8 GB and 24 GB, on a
**60 kb** toy genome. That is not a memory requirement, it is hifiasm's default
`-f37` Bloom filter allocating a fixed ~2^37-bit table regardless of input size.
`-f0` disables it and the same run peaks at 0.225 GB.

This does not change the design -- BioFlow's users assemble real genomes, where
the default filter is correct -- but it is why a small-input smoke test must
pass `-f0`, and it is the explanation for an rc=137 that would otherwise read
as "hifiasm needs more RAM than this machine has".

## Design

### 1. `backend/scripts/install-hifiasm.sh`

One script, following `install-spades.sh`'s shape: pinned version, pinned
checksums, install under `/opt/hifiasm`, purge build tooling, print the
installed version.

Pinned checksums (verified 2026-08-18):

| Tarball | SHA256 |
|---|---|
| `hifiasm-0.25.0.tar.gz` | `51633138865207a9d41630da9377d46e4921ad4fc5facaa1740ceccae8611f1f` |
| `simde-0.8.2.tar.gz` | `ed2a3268658f2f2a9b5367628a85ccd4cf9516460ed8604eed369653d49b25fb` |

Requirements:

- **R1.** The script installs hifiasm 0.25.0 at `/opt/hifiasm/hifiasm` on both
  amd64 and arm64.
- **R2.** The script verifies each downloaded tarball against a checksum pinned
  in the script, and exits non-zero on mismatch.
- **R3.** On arm64, the script vendors SIMDe v0.8.2, redirects the four x86
  intrinsic includes in `Levenshtein_distance.h`, and builds with
  `-march=armv8-a+simd -DSIMDE_ENABLE_NATIVE_ALIASES`.
- **R4.** On amd64, the script builds with upstream's own `-msse4.2 -mpopcnt`
  and does not download SIMDe.
- **R5.** The script passes `-include linux/types.h` on **both** architectures.
- **R6.** The script strips the installed binary.
- **R7.** The script removes build-only packages and downloaded sources before
  exiting.
- **R8.** The script runs `hifiasm --version` after install and exits non-zero
  if it does not report the pinned version.

`backend/Dockerfile` gains a layer calling it, with `ARG HIFIASM_VERSION=0.25.0`,
placed beside the SPAdes layer. `settings.hifiasm_path = "hifiasm"` with
`/opt/hifiasm` on `PATH`.

The apt comment block that currently reads "hifiasm is not packaged at all and
would need a source build with the same arm64 SIMD problem bwa-mem2 has" is
updated: still not packaged, source build confirmed, and materially cheaper than
bwa-mem2's.

### 2. `tools.hifiasm()` probe

- **R9.** `tools.hifiasm()` probes `settings.hifiasm_path` with `["--version"]`.

Verified: `--version` writes `0.25.0-r726` to stdout and exits 0.

**Do not probe with `--help`.** Verified 2026-08-18: `hifiasm --help` prints
`[ERROR] unknown option in "--help"` and then **segfaults**. The short `-h` is
the help flag. A probe on `--help` would report an installed tool as broken, so
this is recorded here rather than rediscovered.

- **R10.** `hifiasm` is added to the tool list `tools.py` reports and to
  `_clear_caches`.
- **R11.** `TOOL_META["hifiasm"]` carries `homepage`, `citation`, `license`,
  and `usage`, so `test_every_tool_is_documented` passes.

License is **MIT**, verified 2026-08-18 via `gh api repos/chhylp123/hifiasm`
(`license.spdx_id == "MIT"`), not recalled.

Citation, read from upstream's own "Citating Hifiasm" README section on
2026-08-18:

> Cheng H, Concepcion GT, Feng X, Zhang H, Li H. Haplotype-resolved de novo
> assembly using phased assembly graphs with hifiasm. Nat Methods.
> 2021;18:170-175.

`citation_url` is `https://doi.org/10.1038/s41592-020-01056-5`.

Upstream lists three papers. The 2021 one describes the core assembler this
change runs; the 2022 paper covers assembly without parental data and the 2024
one covers the double-graph telomere-to-telomere mode, neither of which BioFlow
exposes -- so naming either here would put the wrong reference in a methods
section, the same reasoning `FLYE_SPEC`'s metaFlye note already records.

`usage` describes behaviour, not flags, per CLAUDE.md.

### 3. GFA to FASTA: a post-harvest hook

**This is the part the issue does not anticipate, and the reason this change is
larger than "fill in the spec".**

hifiasm writes **no FASTA at all**. Its primary contigs are
`<prefix>.bp.p_ctg.gfa`, a GFA assembly graph. Confirmed on a real run: the
output directory contains eight `.gfa` files, two `.bed`, and three `.bin`, and
no FASTA of any kind.

Every other assembler here writes contigs as FASTA and `harvest()` simply picks
the declared filename up. `OutputKind.CONTIGS` is `required=True` and becomes
the `REFERENCE` DataObject that every downstream align, polish, and QC step
consumes. Nothing in this codebase converts GFA to FASTA today.

The design adds one optional field to `AssemblerSpec`:

```python
postprocess: Callable[[Path], None] | None = None
```

- **R12.** `assemble_reads` calls `spec.postprocess(out_dir)` after a zero exit
  and before `harvest()`, when the spec declares one.
- **R13.** `HIFIASM_SPEC.postprocess` converts
  `{ASSEMBLY_NAME_PREFIX}.bp.p_ctg.gfa` into `assembly.fasta` in the same
  directory.

The prefix is not free-floating: hifiasm names every output after its `-o`
value, so the command builder passes `-o {out_dir}/{ASSEMBLY_NAME_PREFIX}`,
reusing the same `asm` constant `ABYSS_SPEC` already uses for exactly this
reason -- it makes the output filenames knowable before the run starts, which
is what lets `outputs` be declared statically.
- **R14.** `HIFIASM_SPEC.outputs` declares
  `Output(kind=CONTIGS, filename="assembly.fasta", required=True)`, so
  `harvest()` is unchanged.

The conversion itself is a pure function in `assembly_runner`, testable without
a binary like every other function in that module:

```python
def gfa_to_fasta(text: str) -> str
```

GFA `S` line layout, confirmed against a real run rather than the spec
document:

```
S	ptg000001l	TGTCCGTAATGTAGG...	LN:i:59962	rd:i:340
```

Name in field 2, sequence in field 3, optional tags after. One FASTA record per
`S` line.

- **R15.** `gfa_to_fasta` raises rather than returning an empty string when the
  GFA contains no `S` records.

That failure mode matters: a hifiasm run that exits 0 but assembled nothing
would otherwise produce a valid, empty FASTA that becomes a REFERENCE object
everything downstream silently aligns against. Raising turns it into the
missing-contigs error `harvest()` already knows how to report.

- **R16.** `HIFIASM_SPEC` also declares
  `{ASSEMBLY_NAME_PREFIX}.bp.p_ctg.gfa` as `OutputKind.GRAPH`.

The graph object a user opens in Bandage is the file hifiasm actually wrote, not
a re-serialization of the FASTA.

**Why a spec hook rather than a handler branch.** An
`if assembler is Assembler.HIFIASM:` in `assemble_reads` is a smaller diff and
is exactly the shape `assembler_registry.py`'s own docstring was written to
prevent -- "its mode flags, output filenames and memory profile all differ from
Flye's in ways that would otherwise be spread across a runner, a handler and a
dialog". The hook keeps the fact declarative and per-assembler.

### 4. Chemistry routing and the Flye fallback

- **R17.** `HIFIASM_SPEC` sets `tool=tools.hifiasm` and drops
  `unavailable_reason`.
- **R18.** `HIFIASM_SPEC.mode_flags` is `{HIFI: "hifi", ONT_DUPLEX: "ont"}`.
  See R20b for why the HiFi value is a mode name rather than an empty flag.

HiFi takes no preset flag at all -- it is hifiasm's default -- confirmed
against `hifiasm -h`, which lists exactly one preset option:

```
  Preset options:
    --ont        assemble Oxford Nanopore reads
```

- **R19.** `spec_for_chemistry` returns hifiasm for `HIFI` and `ONT_DUPLEX`
  when `hifiasm.available()`, and Flye otherwise.

**On routing ONT duplex.** The original recommendation was HiFi only, on the
grounds that hifiasm's `--ont` is documented for R10 simplex and `ReadChemistry`
cannot tell R10 from R9 -- inferring a fact the reads do not carry, the trap
`FLYE_SPEC`'s deliberately-conservative `nano-raw` default exists to avoid.

That objection does not apply to duplex. `ONT_DUPLEX` is a distinct enum member
from `ONT_SIMPLEX`; duplex reads are Q30+, genuinely HiFi-grade accuracy, which
is the regime hifiasm's string graph is built for; and duplex basecalling is
R10-era by construction, so there is no R9 duplex for the ambiguity to bite on.
`ONT_SIMPLEX` -- where the concern does apply -- stays on Flye.

Recorded because the reasoning, not the conclusion, is what a later reader needs
in order to decide whether routing `ONT_SIMPLEX` is safe.

The fallback lives inside `spec_for_chemistry` so the function keeps the promise
its docstring already makes -- "This function remains the single place that
changes". The Assemble card, `default_assembly_params`, and `launch_assembly`
all read it, so all three inherit the fallback with no further edits and cannot
drift apart.

**This makes the issue's requested `suggestion_service.py` rule a no-op.**
`build_assemble_card` already calls `spec_for_chemistry` and renders
`spec.assembler.value` in its title, so the routing change is the rule. What is
genuinely needed there is the *test*, including the unavailable direction #617
correctly insists on.

- **R19a.** `build_assembly_command` gains a `_hifiasm_command` builder passing
  `-o {out_dir}/{ASSEMBLY_NAME_PREFIX}`, `-t {threads}`, and `--ont` when the
  mode is `ont`. `memory_bytes` and `mate` are ignored --
  hifiasm has no memory-ceiling flag, and a paired long-read assembly is not a
  thing, the same asymmetry `_flye_command` already documents.
- **R20.** `HifiasmParams` is added to `assembly_params.py` and registered in
  `_BY_ASSEMBLER`.
- **R20a.** `HIFIASM_SPEC.fields` is `_SHARED_FIELDS` plus a `mode` select
  offering exactly two choices: HiFi (value `hifi`) and ONT (value `ont`).
- **R20b.** `HIFIASM_SPEC.mode_flags` maps `HIFI -> "hifi"` and
  `ONT_DUPLEX -> "ont"`; `_hifiasm_command` emits `--ont` for `ont` and **no
  preset flag** for `hifi`.

**The `mode` value is not the flag, deliberately.** The obvious encoding is
`{HIFI: ""}` -- an empty string meaning "no flag" -- and it breaks twice. The
params classes here all read `data.get("mode") or <default>`, which coerces
`""` to the default, so an explicit HiFi choice would be indistinguishable from
an unset one; and `modes_for` returns a frozenset of choice values, so `""`
would have to be a legal mode string the dialog round-trips. Naming the mode
`hifi` and translating to a flag in the builder keeps "what the user chose" and
"what the tool is passed" separate, which is the same split
`SPADES_MODES`' `standard` already uses for a mode that is likewise not a flag.
- **R21.** The `mode` field validates against
  `assembler_registry.modes_for(Assembler.HIFIASM)`.

`modes_for` reads the choices off the `mode` field declaration, so R20a is what
makes R21 non-vacuous -- a spec with no `mode` field returns an empty frozenset
and every value fails validation.
- **R22.** `from_dict`'s "Reachable: `Assembler` declares hifiasm and SPAdes so
  the registry can describe them, and neither has a params class" comment is
  corrected in the same commit, since hifiasm now has one.

`AssemblyMemoryModel` keeps its existing `bytes_per_genome_base=60.0` and
`fixed_overhead_mb=4096` -- higher than Flye's 40, correctly reflecting that
hifiasm holds all-vs-all overlaps. Published guidance, not measured on this
hardware, the same caveat every other model in that file carries.

### 5. Scope: primary contigs only

hifiasm's headline feature is haplotype-resolved assembly. This change exposes
none of it.

- **R23.** Only `bp.p_ctg.gfa` is harvested. `a_ctg`, `hap1`, `hap2`, `p_utg`,
  and `r_utg` outputs are ignored.

#617's success criterion 3 asks for "a usable primary assembly" and nothing
more. An alternate haplotype would need a second contigs DataObject per run,
which the handler's single-`OutputKind.CONTIGS` return shape does not express --
a materially larger change, and a separate decision.

- **R24.** No new progress parser. hifiasm has no `>>>STAGE:` equivalent, so it
  takes the bare `AssemblyProgress()` fallback `assemble_reads`' existing `else`
  branch already documents and defaults to.

### 6. Tests

- **R25.** `gfa_to_fasta` converts a multi-record GFA, ignoring non-`S` lines.
- **R26.** `gfa_to_fasta` raises on a GFA with no `S` records.
- **R27.** `spec_for_chemistry` returns hifiasm for `HIFI` and `ONT_DUPLEX`.
- **R28.** `spec_for_chemistry` returns Flye for both when hifiasm is
  unavailable.
- **R29.** The Assemble card names hifiasm for HiFi reads.
- **R30.** The Assemble card names Flye -- not "not installed" -- when hifiasm
  is unavailable.
- **R31.** `build_assembly_command` produces `--ont` for `ONT_DUPLEX` and omits
  any preset flag for `HIFI`.

R28 and R30 are the directions that fail when the seam breaks. Per CLAUDE.md's
warning, they patch `assembler_registry.spec_for` (or `SPECS`), **never**
`tools.hifiasm` -- `HIFIASM_SPEC` is a frozen dataclass that captures the
function object at import time, so patching the module attribute silently reads
the host machine instead.

## Out of scope

- Haplotype-resolved output (`--primary`, Hi-C `--h1/--h2`, trio `-1/-2`).
- The `-l` purge-level parameter. Considered and dropped: it materially changes
  the primary assembly for homozygous vs heterozygous genomes, but it is a
  parameter-surface decision independent of getting the tool installed and
  routed, and #617 does not ask for it.
- Routing `ONT_SIMPLEX` to hifiasm. See R19's note.
- Ultralong ONT integration (`--ul`).
