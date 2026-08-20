# Delly for short-read structural variant calling

Date: 2026-08-20.

Closes [#620](https://github.com/syntheticgio/bioflow/issues/620). This is the
short-read counterpart to
[#619](https://github.com/syntheticgio/bioflow/issues/619) (Sniffles2), and it
inherits that issue's decisions by name --
`docs/superpowers/specs/2026-08-18-sniffles2-structural-variants-design.md` is
required reading before this one. In particular, #619 decided that structural
variants get their own pipeline, node type, table, and results view, separate
from small-variant calling, and that decision is assumed throughout rather than
re-argued.

## Problem

BioFlow calls structural variants only from long reads. A short-read-only
project has zero SV detection: Clair3, DeepVariant, and bcftools are
small-variant callers, and Sniffles2 refuses `ReadChemistry.SHORT` by design.

#619 anticipated this and left an explicit seam. The SV card's `SHORT` branch
reads:

> Sniffles2 needs long reads; short-read structural variant calling needs a
> different tool.

with a comment saying that #620's caller replaces that reason on the same card
rather than adding a second SV card. This document specifies that replacement.

## Decision 1: Delly, not Manta

**Delly.** The issue framed this as an open evaluation leaning toward Delly on
containerization grounds. Verified against both upstream repositories on
2026-08-20, the comparison is not close, and the deciding factor is not the one
the issue anticipated:

| | Delly | Manta |
|---|---|---|
| Upstream status | Active; last push 2026-08-17 | **Archived** |
| Latest release | v2.6.0, 2026-08-17 | v1.6.0, 2019-07-09 |
| License | BSD-3-Clause | `NOASSERTION` on GitHub; needs manual verification |
| Linux release binaries | `linux-amd64` **and** `linux-arm64` | `centos6_x86_64` only |
| Install shape | One static binary + chmod | Source build, Python 2 era |

Manta is disqualified by being archived. Integrating a caller its own vendor
stopped maintaining in 2019, when the alternative was released three days
before this document was written, is not defensible on any axis the issue
raised.

The arm64 result is worth stating explicitly because it reverses an assumption
made during brainstorming. The user accepted an amd64-only integration if
necessary, on the precedent of Polypolish (`scripts/install-polypolish.sh`
exits 0 on arm64, and `tools.polypolish()` replaces the generic "not found"
error with an architecture note). **That concession is not needed.** Delly
publishes `delly-v2.6.0-linux-arm64` as a release asset, so both architectures
run identical code and the Polypolish pattern stays unused here.

Rejected: **bioconda**. `Dockerfile:36` records the repo's standing position
against it for tools with a usable binary, and Delly has one for both
architectures.

## Decision 2: one node type, caller chosen from chemistry

**`call_structural_variants` remains a single node type, endpoint, handler, and
results path. The caller is selected inside it from the BAM's chemistry.**

The alternative -- a second `call_short_read_structural_variants` node type
with its own endpoint and handler -- was rejected because the output substrate
is genuinely shared. Both callers produce an SV VCF that `sv_db.py` ingests and
`SvResults.tsx` renders, and `_apply_call_structural_variants` in
`queue/results.py` is the largest single piece of machinery #619 built. Forking
at the node type duplicates all of it to gain nothing: the two callers differ
in how they are *invoked*, not in what they *produce*.

Dispatch-inside-one-handler is also the established pattern here.
`trim_reads` already dispatches three ways in
`queue/pipeline_handlers.py`, and `variant_runner.caller_for_chemistry` already
performs exactly this kind of chemistry-to-caller selection for small variants.

### The selection seam

A new module, `app/pipelines/sv_caller.py`:

```python
class SvCaller(StrEnum):
    SNIFFLES2 = "sniffles2"
    DELLY = "delly"


def caller_for_chemistry(chemistry: ReadChemistry) -> SvCaller | None:
    """Which SV caller covers this chemistry. None means none does."""
```

| `ReadChemistry` | Caller |
|---|---|
| `HIFI`, `CLR`, `ONT_SIMPLEX`, `ONT_DUPLEX` | `SNIFFLES2` |
| `SHORT` | `DELLY` |
| `UNKNOWN` | `None` |

**A new module rather than a function in `sniffles_runner.py`.** A Delly run
importing `sniffles_runner` to discover that it should use Delly is the tangle
that puts the next caller's branch in the wrong file. `sv_caller.py` depends on
neither runner; both handlers depend on it.

`sniffles_runner.sv_calling_allowed_for` is reimplemented as
`caller_for_chemistry(chemistry) is not None` so there is one mapping rather
than two that can disagree. **Its existing CLR comment must survive the move.**
That comment explains why `sv_calling_allowed_for` accepts `CLR` while
`variant_runner.caller_for_chemistry` refuses it -- SV calling reads alignment
structure, not per-base accuracy -- and #619's spec flagged it as the kind of
asymmetry someone harmonises away, silently deleting a real capability with no
test failing.

`UNKNOWN` continues to map to no caller. It means QC has not run, and guessing
wrong in either direction produces junk quietly.

### Delly has a long-read mode, and we do not use it

Delly 2.6.0 ships `delly lr -y ont` and `delly lr -y pb`. **Sniffles2 remains
the long-read caller.** This is recorded so that a later reader does not
"simplify" the two-caller design into one:

- Sniffles2 is the long-read standard and produces the `.snf` sidecar the
  merge card depends on (Decision 6).
- Replacing it would invalidate #619's testing for no capability gain.

`caller_for_chemistry` must therefore never return `DELLY` for a long-read
chemistry, and this is a requirement, not an implementation detail.

## Decision 3: three caller-identity assumptions must be fixed

#619 hardcoded "Sniffles2 is the only SV caller" in three places. Each is
correct today and becomes a **silent wrong answer** the moment Delly exists.
None of the three raises; none fails an existing test.

### 3a. The dedup key (the dangerous one)

`pipeline_service._sv_dedup_key` currently reads:

```
f"call_structural_variants:{bam_id}:{_params_fingerprint(params)}"
```

with a comment stating that no caller field is needed because Sniffles2 is the
only caller. With two callers, a Delly request and a Sniffles request against
the same BAM with equal params fingerprints **collide**, and the second silently
returns the first's result.

**Requirement:** the caller is part of the SV dedup key.

Note that `_variant_dedup_key` is **not** a working example to copy, despite
looking like one. Its docstring says "Includes the caller: calling one BAM with
Clair3 and with bcftools is two results worth comparing, not a double-submit to
collapse" -- and its returned string contains no caller. Small-variant calling
has the same collision this section describes, today, contradicting its own
documented intent. Found while verifying this spec on 2026-08-20 and filed
separately; it is out of scope for #620, but the SV fix must not be written by
imitating it.

### 3b. Provenance

`results.sv_provenance` returns a literal `"variants_called_by": "sniffles2"`.
Left alone, every Delly VCF is stamped as Sniffles output, permanently, on
disk, in the one record whose purpose is saying what produced the file.

**Requirement:** `sv_provenance` reads the caller from the job result rather
than from a literal.

### 3c. The support column

`sv_db._record_from` reads `info.get("SUPPORT")`. That is a Sniffles INFO key.
Delly does not emit it -- it reports `PE` (paired-end support) and `SR`
(split-read support) as separate counts, and for some call types only one is
present. Left alone, the support column is blank for every Delly row, which
reads as "no support data" rather than as a mapping gap.

**Requirement:** support extraction is per-caller.

| Caller | Extraction |
|---|---|
| `SNIFFLES2` | `SUPPORT` |
| `DELLY` | sum of `PE` and `SR` where present; `None` when neither is |

Summing rather than exposing two columns: the column means "how many reads
support this call", and both kinds do. A per-caller split would change the SV
table's schema and `SvResults.tsx` for one caller's benefit.

This is a hand-maintained registry keyed by an enum, which `CLAUDE.md` names as
the shape that skips silently rather than raising. It therefore carries an
exhaustiveness test: every `SvCaller` member has an extractor
(`set(SvCaller) == set(_SUPPORT_EXTRACTORS)`).

## Decision 4: the Delly runner

`app/pipelines/delly_runner.py`, structured like `sniffles_runner.py` -- pure
functions over strings and paths, with no queue or filesystem involvement, so
command construction is testable without a job.

### The invocation

```
delly sr -o <out.bcf> -g <reference.fa> [-q <mapq>] [-h <threads>] <input.bam>
```

**`delly sr`, not `delly call`.** Delly 2.x replaced the single `call`
subcommand with `sr` (short-read) and `lr` (long-read). Verified against the
v2.6.0 README on 2026-08-20. A `delly call` invocation targets a CLI that no
longer exists.

### BCF output, converted

Delly writes BCF via `-o`, or a VCF to stdout when `-o` is omitted. **This
design uses `-o <out.bcf>` followed by `bcftools view`**, rather than
redirecting stdout.

Stdout redirection is available and is still the wrong choice: a crash
mid-write leaves a truncated file that exists and is non-empty, which defeats
the handler's `exited 0 but produced no VCF` check -- the check would pass on a
partial callset. Writing BCF to a named path keeps that check meaningful. The
conversion costs one `bcftools` call, and bcftools is already in the image.

Rejected: teaching `sv_db` to ingest BCF. That adds a second ingest path into
one table, which is the duplication Decision 2 exists to avoid.

### Parameters

`DellyParams`, a separate dataclass from `SnifflesParams`. Verified against
`src/delly.h` at the v2.6.0 tag on 2026-08-20:

| Field | Flag | Default | Note |
|---|---|---|---|
| `threads` | `-h` | 4 | Real; mirrors `SnifflesParams.threads` |
| `min_map_quality` | `-q` | 1 | Delly's own default for min. PE mapping quality |

**There is no minimum-SV-length parameter, and this is a real asymmetry with
Sniffles.** `SnifflesParams.min_sv_length` maps to Sniffles' `--minsvlen`,
whose conventional 50 bp floor separates structural variants from indels. Delly
has no equivalent flag. Its `-m` is `minrefsep` (minimum reference separation,
default 25), which governs breakpoint clustering, not reported call size;
passing a 50 bp intent to it would be a wrong mapping that looks right.

**Decision: `DellyParams` does not offer a minimum length, and no post-filter
is applied.** Delly's own output is reported as Delly produced it. The SV
table already supports `min_length` filtering interactively
(`SvFilters.min_length`), so the capability exists where the user can see what
it is doing, rather than as a silent filter applied before ingest. This must be
documented in `TOOL_META["delly"].usage` so the difference from Sniffles is
discoverable.

`DellyParams` and `SnifflesParams` deliberately do not share a base class. Two
of the three Sniffles fields have no Delly equivalent, and a shared base would
have to either carry them as no-ops or push both classes toward a union of
flags that neither tool accepts.

## Decision 5: install

`backend/scripts/install-delly.sh`, invoked from `backend/Dockerfile` with
`TARGETARCH` in scope, following the `datasets` layer at `Dockerfile:332`:

- `TARGETARCH=arm64` → `delly-v2.6.0-linux-arm64`
- otherwise → `delly-v2.6.0-linux-amd64`

Downloaded from the GitHub release, checksum-verified, `chmod +x`, placed on
`PATH`. No builder stage and no compiler in the final image -- the constraint
`Dockerfile:59` and `:336` both record.

`ARG DELLY_VERSION=2.6.0`, pinned as every other tool in the image is.

**Contingency, and the trigger for using it.** The release binaries' glibc
requirement is unverified against this image's Debian trixie base at the time
of writing. If the binary fails to run there, the fallback in preference order
is (1) Delly's documented source build (`make all`, C++ with bundled htslib and
boost) in a discarded builder stage, following the `winnowmap-build` pattern at
`Dockerfile:20`; (2) bioconda, following `install-clair3.sh`. Only if both fail
does the amd64-only route via the Polypolish pattern apply -- and in that case
`tools.delly()` must replace the generic "not found on PATH" error with an
explicit architecture note, exactly as `tools.polypolish()` does, because the
generic message reads as a broken install.

### Tool registration

`tools.py` gains a `delly()` probe and a `TOOL_META["delly"]` entry. Verified
from upstream on 2026-08-20:

- **License:** BSD-3-Clause (GitHub API, `dellytools/delly`)
- **Homepage / repository:** `https://github.com/dellytools/delly`
- **Citation:** Rausch T, Zichner T, Schlattl A, Stuetz AM, Benes V, Korbel JO.
  "DELLY: structural variant discovery by integrated paired-end and split-read
  analysis." *Bioinformatics.* 2012 Sep 15;28(18):i333-i339.
- **`citation_url`:** `https://doi.org/10.1093/bioinformatics/bts378`

All four verified against the project's own README rather than recalled, per
`CLAUDE.md`'s requirement that a license or citation claim on the Software help
page not be fabricated.

`usage` describes behaviour, not flags, per the same section -- covering that
BioFlow runs Delly on short-read paired BAMs, converts its BCF to VCF, and does
not impose a minimum call size (Decision 4).

The probe's version invocation must be verified against a real installed binary
before the entry is written; several `tools.py` probes carry comments recording
that a `--version` guess was wrong for their tool.

## Decision 6: the card, and what stays Sniffles-only

### The SV card

`build_structural_variants_card` asks `sv_caller.caller_for_chemistry`, then
probes the caller it names.

| Chemistry | Card |
|---|---|
| Long-read, probe passes | AVAILABLE, Sniffles2 |
| `SHORT`, probe passes | AVAILABLE, Delly |
| `UNKNOWN` or `None` | UNAVAILABLE -- "Unknown sequencing platform for this BAM." (unchanged) |
| Probe fails | UNAVAILABLE -- "<tool> is not installed." |

The `SHORT`-is-impossible branch is **deleted**, not supplemented. #619's
comment on it says so explicitly, and this is what makes #620's third success
criterion -- one card offering the right caller per chemistry -- reachable.

The `why` text forks by caller. Long-read keeps "Long reads span breakpoints,
which is what makes structural variants resolvable." The short-read text must
not imply parity: paired-end and split-read signal detects structural variants
but resolves fewer of them, particularly insertions and events in repetitive
regions. A card that reads as equivalent misrepresents what the user gets.

### The merge card stays Sniffles-only -- out of scope

`build_merge_structural_variants_card` is gated on a `SidecarRole.SNF`
sidecar, which is a Sniffles-specific binary callset format. Delly has its own
merge path (`delly merge` over BCFs, then per-sample genotyping against the
merged sites), and it is **explicitly out of scope for #620**.

The issue's success criteria do not mention merging, and supporting it requires
a second sidecar role, a second merge handler, and a genotyping round-trip that
has no analogue in the Sniffles flow.

**The resulting asymmetry is visible to users and is accepted:** after this
lands, long-read SV callsets can be merged across samples and short-read ones
cannot. This is recorded here so it reads as a scoped decision rather than an
oversight, and it should be filed as a follow-up issue when this work lands.

## Testing

Per `CLAUDE.md`, unit tests over hand-built objects are the weakest evidence
here, and the tool-availability direction that actually fails is the flip to
*unavailable*.

**Pure functions**

1. `caller_for_chemistry` returns the right caller for every `ReadChemistry`
   member, including `UNKNOWN` → `None`. Exhaustive over the enum.
2. `caller_for_chemistry` never returns `DELLY` for a long-read chemistry
   (Decision 2).
3. `build_delly_command` produces `delly sr` with the reference, output, and
   input in the right positions, and includes `-q` / `-h` when set.

**The registry**

4. `set(SvCaller) == set(_SUPPORT_EXTRACTORS)` -- the exhaustiveness test
   Decision 3c requires.
5. `_record_from` on a real Delly INFO field (`PE=9;SR=4`) yields support 13;
   on `PE` alone yields 9; on neither yields `None`.
6. `_record_from` on a Sniffles record still yields its `SUPPORT` value --
   the regression direction.

**The caller-identity fixes**

7. `_sv_dedup_key` produces **different** keys for Delly and Sniffles against
   the same BAM with identical params. This is the test that would have caught
   3a.
8. `sv_provenance` stamps `"delly"` for a Delly result and `"sniffles2"` for a
   Sniffles one.

**The card**

9. `SHORT` chemistry yields an AVAILABLE card naming Delly -- replacing the
   existing `test_short_read_reason_names_the_missing_capability`, whose
   docstring already says it is the seam this issue replaces.
10. The card flips to UNAVAILABLE when `tools.delly` is patched to a failing
    probe. Per `CLAUDE.md`, this is the load-bearing direction: the image ships
    tools installed, so asserting *available* passes whether or not the patch
    worked.
11. Long-read chemistries still yield Sniffles cards, unchanged.

**End to end**

12. SV calling runs to completion on a real short-read paired BAM, producing a
    VCF whose records reach the SV table with a populated support column. This
    is the issue's second success criterion and the only check that exercises
    the install, the BCF conversion, and the ingest together.
13. `test_every_tool_is_documented` passes with the new `TOOL_META` entry --
    the issue's first success criterion.

Per `CLAUDE.md`, (12) also means checking the result against a real project
rather than a fixture: the suggestion rules have been green on hand-built
objects while wrong about real files before.

## Out of scope

- **Delly merge / joint genotyping across samples** (Decision 6). File as a
  follow-up.
- **Delly's long-read mode.** Sniffles2 remains the long-read caller
  (Decision 2).
- **Delly's CNV, somatic filtering, and assembly-based subcommands.** This
  issue is germline short-read SV discovery only.
- **Manta**, permanently, on the grounds in Decision 1.

## Requirements summary

Each is independently checkable, per `CLAUDE.md`'s specification guidance.

| ID | Requirement |
|---|---|
| SV-620-1 | Delly v2.6.0 installs from upstream release binaries on both amd64 and arm64 |
| SV-620-2 | `TOOL_META["delly"]` carries verified homepage, citation, license, and usage, and passes `test_every_tool_is_documented` |
| SV-620-3 | `sv_caller.caller_for_chemistry` maps `SHORT` to Delly, the four long-read chemistries to Sniffles2, and `UNKNOWN` to `None` |
| SV-620-4 | `caller_for_chemistry` never returns Delly for a long-read chemistry |
| SV-620-5 | The SV dedup key distinguishes callers, so a Delly and a Sniffles request on one BAM cannot collide |
| SV-620-6 | `sv_provenance` records the caller that actually ran |
| SV-620-7 | The SV table's support column is populated for Delly records from `PE` + `SR` |
| SV-620-8 | Every `SvCaller` member has a support extractor, enforced by test |
| SV-620-9 | A short-read BAM yields one AVAILABLE SV card naming Delly, and no second SV card appears |
| SV-620-10 | The SV card is UNAVAILABLE when Delly's probe fails |
| SV-620-11 | SV calling completes on a real short-read paired BAM and its calls reach the SV table |
| SV-620-12 | `sv_calling_allowed_for`'s CLR asymmetry comment survives the refactor into `sv_caller.py` |

## Sources

- Issue [#620](https://github.com/syntheticgio/bioflow/issues/620).
- `docs/superpowers/specs/2026-08-18-sniffles2-structural-variants-design.md`
  -- #619's design, whose Decisions 1-3 this document inherits.
- `dellytools/delly` README and `src/delly.h` at v2.6.0, and the GitHub API
  metadata for both `dellytools/delly` and `Illumina/manta`, all read
  2026-08-20. The CLI facts in Decision 4 (subcommand name, available flags,
  absence of a minimum-length flag) come from the source rather than from
  recall, and three of them corrected assumptions made earlier in the same
  design conversation.
