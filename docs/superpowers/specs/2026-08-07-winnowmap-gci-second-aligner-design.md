# Winnowmap as a second aligner for GCI continuity scoring

Design for [#73](https://github.com/syntheticgio/bioflow/issues/73). Follows
[#65](https://github.com/syntheticgio/bioflow/issues/65) (GCI, shipped) and
[#64](https://github.com/syntheticgio/bioflow/issues/64) (Merqury, which
shipped meryl 1.4.2 -- winnowmap's blocking dependency).

## The open question the issue raised, answered

The issue deferred this to a spec because it was unclear "whether GCI's
`--hifi`/`--nano` flags accept multiple BAM paths, or whether this needs two
separate alignment jobs merged before GCI runs."

**It is the first, natively.** Read from the installed GCI at
`/opt/gci/GCI.py:1041-1042`:

```python
group_io.add_argument('--hifi', nargs='+', metavar='',
                      help='PacBio HiFi reads alignment files (at least one bam file)')
group_io.add_argument('--nano', nargs='+', metavar='', ...)
```

`nargs='+'` means each slot takes a list. No merge step, no second GCI
invocation, no BAM merging with samtools. `build_gci_command` changes from
`Path | None` per slot to `Sequence[Path]` per slot, and the handler links N
BAMs instead of one. **That is the entire GCI-side change**, and it is the
part the issue thought would be hard.

GCI's own README (line 157) states the intended shape directly: "We recommend
to input only one alignment file per software (minimap2 and winnowmap) using
the same set of long reads." So the target is exactly two BAMs per populated
slot -- one per aligner, same reads, same assembly -- not an open-ended list.

### What multi-BAM mode changes inside GCI

Two filter options exist *only* for this mode, and both are load-bearing
enough that leaving them at defaults silently is the wrong call:

- `--mq-cutoff` (default 50) -- "the cutoff of mapping quality for keeping the
  alignment (only used when inputting more than one alignment file)"
  (`GCI.py:1053`). In single-BAM mode this is dead; in multi-BAM mode it is a
  second, stricter quality gate layered on top of `-mq`.
- `--ovlp-percent` (default 0.9) -- "minimum overlapping percentage of the
  same read alignment if inputting more than one alignment file"
  (`GCI.py:1055`). This is how GCI cross-checks: a read is kept when both
  aligners place it compatibly.

`-mq/--map-qual` is already passed explicitly by `build_gci_command` rather
than left to GCI's default, and the docstring says why -- "upstream is
explicit that lowering it admits multi-mapping reads from repetitive regions,
which makes runs at different thresholds incomparable." **The identical
argument applies to `--mq-cutoff` and `--ovlp-percent`**, and only in
two-aligner mode, where they are the parameters that define what
"cross-checking" actually meant for this score. They get the same treatment:
passed explicitly whenever more than one BAM is in a slot, stored as facts
(`assembly_continuity_mq_cutoff`, `assembly_continuity_ovlp_percent`),
omitted from the command and left null in facts in single-BAM mode -- where
recording them would assert a filter that never ran.

### One caveat worth stating, since it bounds what this buys

GCI's README line 226 notes: "all the results are computed using one bam file
from winnowmap and one paf file from minimap2 which would be slightly higher
than all bams". Upstream's own published numbers use a BAM+PAF pair, not
BAM+BAM. BioFlow's align pipeline produces sorted BAMs and has no PAF
conversion path (that needs `paftools.js`, not installed). Two BAMs is a
supported invocation -- `--hifi` documents "at least one bam file" -- and the
scores will differ slightly from upstream's table. That is a comparability
footnote, not a blocker, and it is the same class of fact
`assembly_continuity_aligners` already exists to record.

## Where the real work is: installing winnowmap

The issue's framing has this backwards -- it treated the install as the
solved part ("its one real prerequisite already is" installed) and the GCI
command as the design question. In fact the GCI command is a `nargs='+'`
one-liner and the install is where the risk lives.

### No binary release exists

Verified 2026-08-07 via `gh api repos/marbl/Winnowmap/releases`: the latest
release is `v2.03`, and its asset list is **empty**. Every release is
source-only. This cannot follow `install-meryl.sh`'s tarball-extract shape;
it is a `make` from source, the shape this repo has repeatedly gotten burned
by on arm64 (see `install-meryl.sh`'s header, which enumerates bwa-mem2 and
compleasm as prior instances).

Winnowmap builds with `make` against a vendored copy of minimap2's codebase
plus its own meryl-derived k-mer weighting.

**The arm64 build was verified during this design pass, and it needs two
non-obvious things.** Both were found by building v2.03 in
`ghcr.io/syntheticgio/bioflow-backend`'s own base on this aarch64 machine;
neither is in Winnowmap's README, and either one silently costs a day if
found after the registry wiring is written.

**(a) `-fsigned-char` must be passed on the command line, not left to the
Makefile.** `src/Makefile` has an `aarch64` branch that appends
`-D_FILE_OFFSET_BITS=64 -fsigned-char` to `CPPFLAGS` -- but the top-level
`Makefile` does `export CPPFLAGS=...` and then invokes `$(MAKE) -e -C src`.
Under `-e`, the exported environment value **overrides** the sub-Makefile's
`CPPFLAGS+=`, so the aarch64 branch's flags are computed and then discarded.
`char` is unsigned on ARM, and the build dies at `chain.c:10`:

```
chain.c:10:9: error: narrowing conversion of '-1' from 'int' to 'char' [-Wnarrowing]
```

Passing the full flag string as a `make` variable (which beats `-e`) builds
clean. The documented `make arm_neon=1 aarch64=1` alone does **not** work.

**(b) Do not build the bundled `ext/meryl` -- use the meryl #64 already
installed.** After `bin/winnowmap` links successfully, the top-level Makefile
continues into `ext/meryl`, which is an old vendored meryl that fails on
Debian trixie:

```
utility/src/utility/system.C:37:10: fatal error: sys/sysctl.h: No such file or directory
```

`sys/sysctl.h` was removed from glibc. This is not a problem to solve: it is
a second, older copy of the tool `install-meryl.sh` already installs at
1.4.2. The install script should build the `winnowmap` target only and
install `bin/winnowmap`, ignoring `ext/meryl` entirely.

**Verified working end to end** on aarch64 in the backend image: `meryl count
k=15` -> `meryl print greater-than distinct=0.9998` -> `winnowmap -W ... -ax
map-pb` -> `samtools sort`/`index`, with all 400 synthetic reads aligned
(690 records incl. secondaries) and `winnowmap --version` printing `2.03`.
The standalone 1.4.2 meryl drives the `-W` step correctly, which is the
assumption the whole slice rests on.

**Runtime deps are already satisfied.** `ldd bin/winnowmap` needs only
`libz`, `libstdc++`, `libm`, `libgomp`, `libgcc_s`, `libc` -- all present in
the image. Unlike meryl, winnowmap needs **no** vendored OpenSSL 1.1 and no
`LD_LIBRARY_PATH` entry.

**Build-time deps are not.** The image has no `g++`/`make`; `build-essential`
and `zlib1g-dev` are needed to compile. Use a multi-stage build that compiles
in a throwaway stage and `COPY`s the single ~1.5 MB `bin/winnowmap` into the
final image, the way the `legacy-ssl` stage already does for meryl's libs --
rather than leaving a compiler toolchain in the shipped image.

### License: PUBLIC DOMAIN, not "NOASSERTION"

`gh api repos/marbl/Winnowmap` reports `license.spdx_id: "NOASSERTION"`,
which is GitHub failing to classify the file, **not** an absent license.
Reading `LICENSE` directly: it is an NIH/NHGRI public domain dedication --
"This software is freely available to the public for use without a copyright
notice. Restrictions cannot be placed on its present or future use." It is a
joint work whose individual contributions carry their own licenses noted in
the source files.

`TOOL_META.license` should say so in those terms rather than copying
GitHub's `NOASSERTION`, which would read as "unlicensed" -- the exact kind of
wrong-license claim CLAUDE.md warns is worse than a blank field. Record the
verification method in a comment beside it, the way `gci`'s entry records
`gh api repos/yeeus/GCI -> license.spdx_id: MIT`.

Citation: Jain et al., "Weighted minimizer sampling improves long read
mapping" (Bioinformatics 2020) and Jain et al., "Long-read mapping to
repetitive reference sequences using Winnowmap2" (Nat Methods 2022) -- verify
both against the repo's own README before writing them in.

### The meryl pre-step is not optional, and it is not free

Winnowmap does not align from a reference alone. It needs a repetitive-k-mer
file built by meryl first, and passed via `-W`:

```
meryl count k=15 output merylDB $asm
meryl print greater-than distinct=0.9998 merylDB > repetitive_k15.txt
winnowmap -W repetitive_k15.txt -ax map-pb $asm $reads > out.sam
```

(GCI's own README, lines 146-152, documents exactly this.) So a winnowmap
alignment is a **two-tool, two-phase** job in a way no existing
`AlignerSpec` is. The `repetitive_k15.txt` is a per-reference artifact --
built once, reused for every read set aligned against that assembly -- which
makes it an index in everything but name, and it should be modeled as one
rather than rebuilt per alignment job.

That is the single largest design decision in this slice, and it is
addressed below.

## Design decision: winnowmap is an `Aligner`, with meryl as its index builder

`aligner_registry.AlignerSpec` already has the seam this needs.
`builder_tool` exists precisely for "the separate binary that builds this
aligner's index, when there is one -- bowtie2-build, hisat2-build", and
`align_handlers.build_index` dispatches through it. Winnowmap's meryl step is
that same shape: a different binary, run once per reference, producing an
artifact the aligner then consumes.

So:

- `Aligner.WINNOWMAP = "winnowmap"` in `aligners.py`.
- `SidecarRole.WINNOWMAP_INDEX`, `INDEX_ROLE[Aligner.WINNOWMAP]`, and a
  suffix (`.repetitive_k15.txt`) in `index_suffixes` / `_LAYOUTS`.
- `AlignerSpec(aligner=WINNOWMAP, tool=tools.winnowmap,
  builder_tool=tools.meryl, ...)`, with `params_class` carrying `k` (default
  15) and `distinct` (default 0.9998) alongside the shared threads/sort
  fields, and a `preset` select restricted to long-read presets
  (`map-pb` for HiFi, `map-ont` for ONT) -- winnowmap has no short-read mode,
  and offering `sr` would be offering a run that cannot work.
- A `MemoryModel` -- meryl counting is the memory-hungry phase, so
  `index_build_multiplier` should be set from a measured run rather than
  guessed, and the docstring on `MemoryModel` already says these are
  "heuristics ... roughly right and occasionally wrong" with the block band
  set at genuinely-impossible.

**The registry-audit trap applies here.** Per CLAUDE.md, `Aligner` is an enum
with several hand-maintained dicts keyed by it -- `INDEX_ROLE`, `_LAYOUTS`,
`_SPECS`, `index_suffixes`'s if-chain. This is the "genuinely derivable"
category: each should carry (or already carries) an exhaustiveness test of
the `set(Aligner) == set(the_dict)` shape. Adding `WINNOWMAP` is exactly the
change that finds a dict without one. Check every dict keyed by `Aligner`
before implementing; a missing entry here is the STAR `_SIDECAR_ROLES`
failure again -- green suite, silently missing outputs.

**And the `spec_for` patching trap.** CLAUDE.md is explicit:
`aligner_registry`'s specs are frozen dataclasses that captured
`tools.minimap2` as a function object at import time, so patching
`app.pipelines.tools.winnowmap` will not reach `spec.tool`. Tests patch
`spec_for`. And assert the *unavailable* direction -- the image will ship
winnowmap installed, so a test asserting a card is available passes whether
or not the patch worked.

### Alternative considered and rejected: a bespoke winnowmap job

Bolting a winnowmap-specific handler beside the align pipeline, rather than
registering it as an `Aligner`, would avoid touching the enum-keyed dicts.
It is the wrong trade. `aligner_registry`'s module docstring is a record of
what the pre-registry world cost: "adding an aligner meant five coordinated
edits ... Nothing said what an aligner *was*, so the answer was 'whatever
those five files agree on', and they only agree until someone edits four of
them." A bespoke path recreates that, and it also means winnowmap BAMs would
not appear in `alignments_against`, would not carry `aligned_by`, and could
not be picked by `_gci_candidates` -- which is the entire point of the
feature.

## The GCI side

### `gci_runner.build_gci_command`

Signature changes from `hifi_bam: Path | None` to `hifi_bams: Sequence[Path]`
(and likewise `nano_bams`), with the existing "at least one of the two" guard
becoming "at least one path across the two". Both new filter flags are
appended only when the corresponding slot has more than one entry:

```python
if len(hifi_bams) > 1 or len(nano_bams) > 1:
    cmd += ["--mq-cutoff", str(mq_cutoff), "-op", str(ovlp_percent)]
```

The docstring must record why they are conditional, in the same register as
the existing `map_qual` note: passing them in single-BAM mode would record a
filter that GCI's own help says is "only used when inputting more than one
alignment file", making the stored facts claim something that never ran.

`parse_gci` is unchanged. GCI's output format does not vary with input count.

### The handler

`_GCI_HIFI_LINK = "hifi.bam"` / `_GCI_NANO_LINK = "nano.bam"` become indexed
names -- `hifi.0.bam`, `hifi.1.bam` -- since two BAMs cannot both link to
`hifi.bam`. Each still needs its `.bai` linked beside it; the handler's
existing `_link_bam_index` call becomes a loop, and the docstring's "Each
BAM's .bai must be linked beside it" / GCI's "this is necessary!!!" note
holds for every one of them.

Payload shape moves from `hifi_bam_sha256`/`hifi_bam_path` (+ `hifi_bai_*`)
to a list of such dicts per slot. **This is a payload break for in-flight
jobs.** Given this repo's single-user, local-only posture, accepting both
shapes indefinitely is not worth the branch; read the list form and let any
queued old-shape job fail loudly rather than silently scoring one aligner
while claiming two.

### `assembly_continuity_aligners`, and the partial-run question

The issue asks "what a partial run (only one alignment succeeded) should
record." The answer falls out of the existing invariant rather than needing a
new rule.

`assembly_continuity_aligners` is currently `list(payload.get("aligners") or
["minimap2"])` -- a payload default, which is to say **the handler asserts a
fact it does not verify.** That is fine today because there is exactly one
possible value. With two aligners it stops being fine: a payload claiming
`["minimap2", "winnowmap"]` while only one BAM actually reached the command
line stores a score labeled as cross-checked when it was not, and the label
is the whole reason the field exists.

So: **derive it from the BAMs that were actually linked, not from the
payload.** Each BAM object carries `aligned_by` (set in `results.py:1183`
from the align job's `aligner`). The handler resolves the input objects
already; it reads `aligned_by` off each and takes the sorted distinct set.
The payload's `aligners` key is deleted, not defaulted.

The consequences for the cases the issue asked about then need no special
handling:

- **Both aligners ran** -> `["minimap2", "winnowmap"]`. Accurate.
- **Only minimap2's alignment exists** -> `["minimap2"]`. This is exactly
  today's supported, labeled, degraded mode, and the `gci_runner` docstring
  already explains why it is stored rather than omitted (upstream says the
  scores are similar -- unlike CRAQ's CSE, which upstream says is "hardly
  detected" on NGS-only runs and which BioFlow therefore omits).
- **The winnowmap alignment job failed** -> nothing to resolve; the GCI job
  either runs on what exists and labels it honestly, or never launches.
  There is no state where a failed alignment produces a mislabeled score.
- **A BAM with no `aligned_by`** (register-in-place import) ->
  the value is unknown, not assumed. Record it as such rather than defaulting
  to `"minimap2"`; a guess here is the same class of error as the payload
  default this replaces.

### Launch path and auto-pairing

`_gci_candidates` splits `long_` into hifi/nano by chemistry. With two
aligners, a project will routinely have two HiFi BAMs against one assembly --
which today trips `launch_continuity_qc`'s "several long-read alignments;
name the ones to use" refusal (`len(hifi_candidates) > 1`). **The feature
would be unusable through auto-pair without changing that check**, and this
is the one place where the ambiguity rule genuinely needs to weaken.

The refinement: candidates in a slot that are the same reads under *different
aligners* are not ambiguous -- they are the intended input. Group each slot's
candidates by `aligned_by`; if every group has exactly one BAM, pass them all.
Keep the refusal when any single aligner contributed two BAMs to a slot, which
is the genuinely ambiguous case the check was written for. Whether "same
reads" can be verified beyond `aligned_by` distinctness (a shared source
FASTQ id in `derived_from`) should be checked against a real project during
implementation -- CLAUDE.md's rule about testing suggestion rules against the
real database rather than only fixtures applies directly, and the Actions-tab
precedent it cites is this exact failure mode.

The explicit-id path (`hifi_bam_id`/`nano_bam_id`) becomes
`hifi_bam_ids`/`nano_bam_ids` lists. Its per-BAM chemistry validation loop --
which exists so "a dialog client could not pass any BAM id under
`hifi_bam_id` regardless of what it actually is" -- runs per element,
unchanged in intent.

### Suggestion service and the Software page

Per CLAUDE.md's "Adding a pipeline tool": registering winnowmap in
`tools.py` is half the change.

- `suggestion_service.py` -- the Align card's aligner options are where
  winnowmap needs to become pickable; and the continuity card's copy should
  say when it will run cross-checked versus single-aligner, since that is now
  a user-visible distinction rather than a fixed property. Add cases to
  `backend/tests/services/test_suggestion_service.py`.
- `TOOL_META["winnowmap"]` with `homepage`, `citation`, `license`, `usage`
  filled -- `test_every_tool_is_documented` fails until they are. `usage`
  says *behaviour*: that BioFlow runs it as a second long-read aligner whose
  BAM is paired with minimap2's for GCI cross-checking, and that its
  repetitive-k-mer file is built by meryl per reference. No flags -- flags
  change when a runner is tuned and nothing catches a stale `usage` string.
- `tools.winnowmap()` probe: confirm which flag prints a version and whether
  it exits zero, the way `gci()`'s comment records that `GCI.py` takes
  `-v/--version` via argparse and exits zero. Do not assume `--version`.
- `config.py` gets `winnowmap_path`.

## Implementation order

The install is the risk; everything else is mechanical once it is real. So:

1. ~~Build winnowmap from source and confirm it runs on arm64.~~ **Done
   during this design pass** -- see the two gotchas above. What remains is
   writing `backend/scripts/install-winnowmap.sh` and the Dockerfile
   multi-stage block around the build that is already known to work.
2. ~~Verify the meryl pre-step end to end.~~ **Done** -- chain confirmed on
   the backend image. What remains is measuring peak memory of `meryl count`
   on a *real* assembly (the synthetic 72 kb reference proves correctness,
   not cost); that number is `MemoryModel.index_build_multiplier`, and
   guessing it is what the `MemoryModel` docstring warns against.
3. Register the tool: `config.py`, `tools.winnowmap()` probe, `TOOL_META`.
4. Register the aligner: enum member, sidecar role, layout, `AlignerSpec`,
   and the exhaustiveness tests on every `Aligner`-keyed dict.
5. `align_handlers.build_index` dispatch for the meryl pre-step.
6. `gci_runner.build_gci_command` -> sequences, plus the two conditional
   filter flags.
7. Handler: indexed links, per-BAM `.bai`, `aligned_by`-derived
   `assembly_continuity_aligners`.
8. Launch path: list payload, same-reads-different-aligner auto-pair, list
   explicit ids.
9. `suggestion_service.py` wiring and its tests.
10. Run GCI on a real project both ways -- one aligner and two -- and confirm
    the stored `assembly_continuity_aligners` matches what actually ran, and
    that the two scores are in the same neighbourhood (upstream reports they
    should be; a large divergence means the cross-check filters are
    misconfigured, not that the assembly changed).

## What this deliberately does not do

- **No PAF path.** Upstream's headline numbers use winnowmap BAM + minimap2
  PAF, which needs `paftools.js`. Two BAMs is supported and simpler; the
  slight score difference is recorded above as a comparability note rather
  than chased.
- **No VerityMap.** Upstream tested it and recommends WM2+MM2 over WM2+VM;
  there is no reason to add a third aligner whose own FAQ says it detects
  fewer issues.
- **No retroactive rescoring.** Existing GCI results labeled
  `["minimap2"]` stay as they are. They are correctly labeled, which is what
  the field is for.
