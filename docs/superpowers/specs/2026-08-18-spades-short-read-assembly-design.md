# SPAdes for short-read assembly

Date: 2026-08-18.

Closes [#519](https://github.com/syntheticgio/bioflow/issues/519). Follows
`docs/superpowers/specs/2026-08-17-short-read-assembly-design.md`, which
shipped ABySS and deferred SPAdes to this issue.

## Problem

ABySS is installed and short-read assembly works. SPAdes produces better
assemblies on bacterial isolates -- the realistic ceiling for short-read
assembly on a workstation -- and is what most users mean by "short-read
assembly". It is not packaged for trixie, so it needs a vendored upstream
build.

`SPADES_SPEC` already exists in `assembler_registry.py` as a
declared-but-not-installed placeholder carrying `layout="paired"` and the
refusal string "Short-read assembly is not installed." This change fills it in.

## A correction to the issue

**#519 says this should be "an install script, a filled-in spec entry, and a
command builder", following the `install-meryl.sh` vendored-tarball pattern.
The vendored-tarball half of that does not work, because SPAdes ships no
Linux-arm64 binary.**

The release assets for v4.3.0 (verified 2026-08-18 via
`gh api repos/ablab/spades/releases`):

| Asset | Arch |
|---|---|
| `SPAdes-4.3.0-Darwin-arm64.tar.gz` | macOS arm64 |
| `SPAdes-4.3.0-Darwin-x86_64.tar.gz` | macOS x86-64 |
| `SPAdes-4.3.0-Linux.tar.gz` | **x86-64 only** |
| `SPAdes-4.3.0.tar.gz` | source |

The macOS assets are arch-qualified; the Linux one is not, and is amd64.
Confirmed by extracting it and reading the ELF header:

```
spades-core: ELF 64-bit LSB executable, x86-64, ...
             interpreter /lib64/ld-linux-x86-64.so.2
```

Upstream's own installation docs describe the Linux binaries as ManyLinux 2.28
builds and list compatible *distributions* with no architecture caveat, which
is why this needs checking rather than reading.

Vendoring that tarball alone would leave SPAdes entirely absent on arm64.
`.github/workflows/release.yml` publishes `linux/amd64` and `linux/arm64` as
the contract [#46](https://github.com/syntheticgio/bioflow/issues/46)
established, so that is a broken half of a shipped image, not a deferred nicety.

**SPAdes does support ARM, from source.** Upstream issue #1062 ("Support Apple
m1") is closed, and v4.3.0's release notes include "Fixed gzipped GFA reading
on ARM/Linux platforms" -- ARM is a supported build target that upstream simply
does not ship a binary for.

## Decision: the TARGETARCH split

Install per architecture, the pattern `backend/Dockerfile` already uses for
bwa-mem2 at the `ARG TARGETARCH` block:

- **amd64** -- vendor `SPAdes-4.3.0-Linux.tar.gz`.
- **arm64** -- build from `SPAdes-4.3.0.tar.gz` with `spades_compile.sh`.

### Verification

The arm64 build was run before this spec was written, natively (not emulated)
in a `linux/arm64` `python:3.12-slim` container -- the same base image
`backend/Dockerfile` uses:

| Check | Result |
|---|---|
| `PREFIX=/opt/spades ./spades_compile.sh` | exit **0**, no patches, no source edits |
| `file spades-core` | `ELF 64-bit LSB pie executable, ARM aarch64` |
| `spades.py --version` | `SPAdes genome assembler v4.3.0` |
| Assembly of the bundled `test_dataset` E. coli pair | exit 0, contigs written |
| Clean build time | **124 s** on 24 cores |
| Installed size | **193 MB** |

Toolchain in trixie is g++ 14.2 and cmake 3.31, well past SPAdes' documented
minimums of g++ 9 and cmake 3.16. Build dependencies are `g++ cmake make
zlib1g-dev libbz2-dev` -- all apt, nothing vendored.

**This is materially cheaper than bwa-mem2's arm64 path**, which needs
sse2neon, three downloaded patches, and a safestringlib build. SPAdes needs a
stock CMake build with no SIMD translation.

The arm64 CI leg runs on `ubuntu-24.04-arm`, a native runner -- #46 already
rejected emulation as "hours long and liable to fail partway" -- so the 124 s
above is representative of CI, not a local-only figure.

### Why not a source build on both arches

Measured, the two paths cost nearly the same: 193 MB built from source versus
196 MB vendored. A single source build would mean one code path instead of an
arch conditional, and would sidestep the wrapper trap below entirely.

It was rejected because the vendored binary is what upstream builds, tests and
ships for amd64. A source build makes this repo responsible for the amd64
toolchain to save roughly two minutes of build time on the architecture most
users run. bwa-mem2 already sets the precedent of binary-on-amd64,
build-on-arm64, and a second tool doing the same thing the same way is worth
more than a marginally shorter Dockerfile.

## Design

### 1. `backend/scripts/install-spades.sh`

One script, branching on `uname -m`, following `install-meryl.sh`'s shape:
checksum the download, install under `/opt/spades`, purge build tooling, print
the installed version.

Requirements:

- **R1.** The script installs SPAdes 4.3.0 under `/opt/spades` on both amd64
  and arm64.
- **R2.** The script verifies each downloaded tarball -- the binary tarball on
  amd64, the source tarball on arm64 -- against a SHA-256 constant recorded in
  the script itself, and exits non-zero on mismatch.
- **R3.** On arm64 the script builds from the source tarball via
  `PREFIX=/opt/spades ./spades_compile.sh`.
- **R4.** The script removes build-only packages and downloaded sources before
  exiting, leaving no compiler toolchain in the image layer.
- **R5.** The script exits non-zero if `spades.py --version` does not report the
  expected version after install.

The version pin is load-bearing in the same way meryl's is, for a different
reason: 4.3.0 is the release whose notes carry the ARM/Linux GFA fix. The
script header must say so, or a later "relax the pin" reintroduces an ARM bug
that has already been fixed once.

**R2 pins hashes rather than fetching them, because SPAdes publishes no
checksum file.** This is a real departure from `install-meryl.sh`, which
downloads a `SHA256SUMS` asset and runs `sha256sum -c` against it. Verified
2026-08-18: the v4.3.0 release has four assets, none of them checksum-shaped,
and the release body does not mention checksums.

GitHub's API does expose a per-asset `digest`, and it is correct -- checked
against a real download, which matched. It is not used here for two reasons.
It is computed by GitHub over whatever bytes GitHub stores, so it attests
transport integrity rather than upstream provenance the way a maintainer-built
`SHA256SUMS` does; and reading it during the image build would mean an
unauthenticated API call from the Dockerfile, which ships no `gh`.

A hash committed in a reviewed script is the stronger option regardless. A
fetched `SHA256SUMS` sitting beside the tarball is replaceable by anyone who
can replace the tarball; a constant in this repo is not.

The hashes, verified 2026-08-18:

| Asset | sha256 |
|---|---|
| `SPAdes-4.3.0-Linux.tar.gz` | `e88a8c533c8614dd4b7c5788cfcd46427848a0575267f97c690a75fd2a343034` |
| `SPAdes-4.3.0.tar.gz` | `09671ca39f9c6d2479d9fc168100bfd089b4a24002d51b815386d2b24d424456` |

The cost is that a version bump means updating three constants, not one. The
script header must say so: a stale hash after a bump fails the build loudly,
which is the right failure, but only if the next person knows to expect it.

`ca-certificates` is deliberately **not** purged -- the bwa-mem2 block records
that purging it broke later layers' HTTPS silently, surfacing only as tar
choking on an empty stream.

### 2. Dockerfile placement

Its own layer, late in the tool section, next to Clair3 and for the same
reason: at ~193 MB this is among the largest single additions in the image, and
an edit anywhere above it should not trigger a reinstall.

The existing SPAdes comment at `backend/Dockerfile:124` -- which says SPAdes
"is NOT packaged for trixie ... and needs a vendored upstream tarball. See
#519" -- is now half wrong and must be updated in the same change. A comment
that outlives its accuracy is the failure the CLAUDE.md `ToolMeta.runnable`
note already records.

- **R6.** `/usr/local/bin/spades.py` is a wrapper script that execs
  `/opt/spades/bin/spades.py`, not a symlink.

A symlink breaks the same way bwa-mem2's did: `spades.py` locates its sibling
binaries (`spades-core`, `spades-hammer`) relative to its own path, so a
symlink into `/usr/local/bin` sends it looking for them there.

### 3. `tools.spades()`

- **R7.** `tools.spades()` probes `spades.py --version` and reports the version
  string the binary itself prints.

No `abyss-pe`-style stderr handling needed; SPAdes' entrypoint is a
conventional CLI.

### 4. `SPADES_SPEC`

- **R8.** `SPADES_SPEC.tool` is `tools.spades`, making `available()` return
  True when the binary is installed.
- **R9.** The spec declares `contigs.fasta` as its required CONTIGS output,
  `assembly_graph_with_scaffolds.gfa` as GRAPH, and `scaffolds.fasta`.

All three filenames were confirmed present in the verification assembly above
rather than read from documentation.

`layout="paired"` is already correct and unchanged. `mode_flags` stays empty:
like ABySS, SPAdes has no read-accuracy mode flag, and `spec_for_chemistry`
routes to it explicitly rather than by chemistry lookup.

Fields are the shared `threads` and `genome_size`, plus:

- **R10.** The dialog offers a `mode` select with exactly three values:
  `isolate` (default), `careful`, and a plain mode passing neither flag.

`--isolate` is upstream's own recommendation for high-coverage isolate data,
which is this feature's main case. Upstream documents `--isolate` and
`--careful` as mutually incompatible, which is why this is a select and not two
checkboxes -- a UI that can express an invalid combination will eventually be
asked to run one.

Deliberately **not** exposed:

- **`--frugal`.** Its own manual says it "affects the assembly results in an
  unpredictable way" and targets complex metagenomes. A parameter whose
  documentation cannot say what it does is not one to put in a dialog.
- **`-k`.** SPAdes auto-selects k from read length; ABySS does not, which is
  why ABySS has the field. Exposing k here invites a hand-set value that is
  worse than the automatic one.

### 5. Memory: `-m` is a hard ceiling

The one genuinely new behaviour, and the reason this is more than a command
builder.

`-m <int>` sets SPAdes' memory limit **in gigabytes**, defaults to **250**, and
**SPAdes terminates when it reaches it**. Left at the default on a workstation,
a run dies late rather than never starting -- the worst shape of failure, since
it costs the full runtime first.

The seam already exists. `pipeline_service.launch_assembly` computes an
estimate and puts `bloom_bytes` in the payload for ABySS's mandatory Bloom
budget; `assembly_handlers` forwards it; `assembly_runner` floors it at
`MIN_BLOOM_MB`. SPAdes needs the same number for a different flag.

- **R11.** A SPAdes run passes `-m` derived from the same memory estimate that
  admitted the run, converted to whole gigabytes.
- **R12.** A SPAdes run with no available estimate still passes a valid `-m`,
  floored to a documented minimum rather than inheriting upstream's 250 GB
  default.
- **R13.** The payload key carrying this number is named for the quantity
  (memory), not for ABySS's use of it (a Bloom filter).

R13 is a rename of `bloom_bytes` to `memory_bytes` across
`pipeline_service.py`, `assembly_handlers.py` and `assembly_runner.py`. It is
included because the alternative -- a key named `bloom_bytes` that SPAdes reads
to set a memory ceiling -- is exactly the kind of name that makes the next
reader believe SPAdes has a Bloom filter. Kept as its own commit, separable
from the behaviour change, per CLAUDE.md.

### 6. Routing: unchanged, deliberately

- **R14.** `spec_for_chemistry` continues to return ABySS for
  `ReadChemistry.SHORT`.

Installing SPAdes makes it selectable. Making it the default changes the result
of every short-read assembly for existing users, including reruns of past work,
and that is a decision about assembly quality rather than about packaging. It
belongs to its own issue with its own before-and-after comparison.

This is the one place where a reader might expect this spec to go further. The
Actions card will suggest ABySS after this change, exactly as it does now, and
SPAdes is reached by choosing it in the dialog.

## Testing

The existing assembler tests are the model, including their traps.

- **T1.** `spec_for(Assembler.SPADES)` is patched -- never `tools.spades` --
  when simulating absence. `AssemblerSpec` is a frozen dataclass that captured
  the function object at import time, a seam `assembler_registry`'s own
  docstring records.
- **T2.** A test asserts the SPAdes card flips to **unavailable** when the probe
  is patched off. Asserting availability proves nothing: the image ships the
  tool installed, so that direction passes whether or not the patch works.
- **T3.** Command-builder tests cover `--isolate`, `--careful`, and plain mode,
  and assert `-m` is present with the expected integer in every case.
- **T4.** A test asserts `-m` is still passed, at the floor, when no estimate is
  available.
- **T5.** The full `TestExhaustiveness` class is run, not only the tests named
  here -- per CLAUDE.md, a registry pair's completeness and
  no-double-classification tests can only catch a collision when run together.

Beyond the suite, and per CLAUDE.md's "check a rule against the real database"
note: run one real paired-end assembly through the running stack on each
architecture before closing the issue. The bundled `test_dataset` E. coli pair
is a valid smoke input and takes seconds.

## Risks

- **The amd64 tarball is 196 MB.** The image grows by roughly that. Accepted:
  it is the tool.
- **`spades_compile.sh` is upstream's script, not ours.** A future release
  changing its interface breaks the arm64 build. The pin limits the blast
  radius, and R5's version assertion makes the failure loud at build time
  rather than at first run.
- **The memory coefficients are published guidance, not measurements.**
  `SPADES_SPEC.memory_model` currently carries `bytes_per_genome_base=90.0` as
  an unverified placeholder. This spec keeps a documented estimate and does not
  claim it is measured -- the same caveat `FLYE_SPEC` and `ABYSS_SPEC` both
  carry. Wrong in either direction costs a warning, never a refusal, per
  `resource_estimator`'s band placement.
