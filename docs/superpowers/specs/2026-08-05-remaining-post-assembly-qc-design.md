# Remaining post-assembly QC

Written 2026-08-05 for GitHub issue #13, the epic left open when the
2026-08-02 post-assembly QC design shipped contiguity and completeness
(`docs/TODO.md`, "Post-assembly QC: BUSCO and QUAST — FIXED (as contiguity +
compleasm, 2026-08-02)"). That entry closed two of four axes and named the
rest: reference-based misassembly detection, and the CRAQ / GCI / Merqury
group.

This is an epic design note, so its job is to pick the first executable slice
and to make the other slices' prerequisites explicit enough to file. It does
that, and in the course of doing it **overturns two claims the closing entry
left behind** -- one about QUAST's cost, one about what blocks CRAQ and GCI.
Both were reasonable in 2026-08-02 and both are wrong now, which is the whole
reason this note is longer than a list of issue titles.

Every upstream fact below was checked against a real install or a real run on
2026-08-05 in this image, not recalled. Where a number appears, it was
measured here.

## Problem

An assembly can be wrong in four independent ways, and BioFlow measures two.

| Axis | Question | State |
|---|---|---|
| Contiguity | how fragmented is it? | shipped, `_parse_fasta`, every FASTA at ingest |
| Completeness | are the expected genes there? | shipped, compleasm |
| **Structural correctness** | are the pieces joined *correctly*? | **nothing** |
| Base-level correctness | are the bases right? | nothing (Merqury's axis) |

The third row is the gap that matters most, because the two shipped axes
actively hide it. A chimeric assembly that joins two chromosomes into one
contig scores *better* on N50 than the correct assembly, and compleasm scores
it identically -- every ortholog is still present, just in the wrong place. A
user optimising the numbers BioFlow currently shows can make their assembly
worse and watch both metrics improve. That is not a missing feature so much as
a misleading one.

## Scope

This epic covers workflows that **evaluate** an existing assembly and record
QC facts or reports. It does not improve, scaffold, polish, or produce a
replacement assembly -- that is #14's territory, shipped as iVar (#47),
Polypolish (#23) and RagTag (#52) on the #21 foundation.

The boundary is worth stating precisely because RagTag sits right on it.
`ragtag.py correct` breaks contigs at suspected misassemblies, which is
detection followed by a destructive edit; it was deliberately excluded from
#52 and it stays excluded here too. **Detecting a misassembly and cutting the
user's contig at it are different products**, and the first is a precondition
for anyone sensibly wanting the second.

Non-goals, unchanged from the closing entry:

- **Contamination screening.** A real axis, uncovered by anything here, and
  out of scope for database size alone: FCS-GX wants ~470 GB resident. It is
  not a slice of this epic and should not be filed as one.
- **gfastats.** Superseded by computing contiguity in `_parse_fasta`. Not
  built, not deferred -- there is nothing left for it to do.
- **BUSCO.** Stays declared-but-unavailable in `assembly_qc_registry`, for
  the reason recorded there.

## Slice 1: reference-based misassembly QC (QUAST)

This is the first executable slice and the only one this note designs in
detail.

### QUAST is much cheaper than the last design concluded

The 2026-08-02 design rejected QUAST and the rejection was correctly reasoned
from what it checked. It checked apt. That verdict still holds exactly as
written -- re-verified today in a clean `debian:trixie` container, since the
`api` image ships with empty package lists and `apt-cache policy` there
silently answers nothing at all for every package, which is how a probe can
look like a verdict:

```
quast:   Installed: (none)   Candidate: (none)      <- referred to, not packaged
meryl:   Installed: (none)   Candidate: 0~20150903+r2013-9+b1
```

But "not in apt" was allowed to imply "costs a source build to obtain numbers
we can compute ourselves", and that inference is what turns out to be wrong on
both halves. Measured today:

| | |
|---|---|
| PyPI's newest `quast` | **5.2.0** (2022), and it is **broken on this image** |
| GitHub's newest release | **5.3.0** (2024-11-10), not on PyPI |
| Release tarball, unpacked | 400 MB |
| **After dropping what a reference run never touches** | **8.6 MB** |
| Compilation required | **none** |
| Runtime, 12 Mb yeast assembly vs. reference, 4 threads | **3.0-4.4 s** |

There is no source build. QUAST is Python, and the C parts it ships are for
tools it prefers to find on `PATH` -- see the minimap2 finding below. The
"cheap-looking half that costs a source build" was contiguity, and that
half is already gone: it was replaced by `_parse_fasta`, correctly, and
nothing here reopens it.

### What actually has to be done to install it

Three things, all verified by doing them.

**1. Take the GitHub tarball, not PyPI.** `pip install quast` gets 5.2.0 and
5.2.0 is dead here:

```
File "quast_libs/qconfig.py", line 12, in <module>
    from distutils.version import LooseVersion
ModuleNotFoundError: No module named 'distutils'
```

Python 3.12 (this image runs 3.12.13) removed `distutils`. 5.3.0 has the same
import; the difference is that PyPI does not have 5.3.0 at all, so the version
question and the patch question are independent.

**2. Patch two lines.** `distutils` appears in exactly two files that a
reference run reaches:

- `quast_libs/qconfig.py:13` -- `from distutils.version import LooseVersion`
- `quast_libs/ra_utils/misc.py:79` -- `from distutils.dir_util import copy_tree`

Both have stdlib/`packaging` equivalents and a `sed` in the install script is
enough. **Do not solve this by pinning `setuptools<81` instead.** That does
work -- setuptools' `distutils` shim makes unpatched QUAST run, verified --
and it is the worse fix: it makes a bioinformatics tool's importability a
property of a global build-system pin that any future `pip install` can
silently break, in a container where nothing would notice until a QC job
failed. The two-line patch was verified with setuptools *uninstalled* and
`import distutils` failing.

**3. Delete four fifths of the tarball.** Verified working after removing
`external_tools/` (232 MB), `tc_tests/` (65 MB), `test_data/`, `manual.html`,
and the bundled `genemark`, `genemark-es`, `barrnap`, `glimmer`, `sambamba`,
`busco` and `minimap2` trees under `quast_libs/`. Those back `--gene-finding`,
`--rna-finding`, `--conserved-genes-finding` and the reads-alignment mode,
none of which this slice offers. 400 MB → 8.6 MB, same output, same 3 s.

The install script belongs beside `backend/scripts/install-compleasm.sh` and
follows its shape.

### The arm64 trap that isn't one, and why it still needs a guard

This repo has been bitten three times by x86-only upstreams (bwa-mem2,
compleasm's release asset, its biocontainer), so the bundled minimap2 in
QUAST's tree looks like the fourth. It nearly is: **the minimap2 arm64
compile fix is on QUAST's master branch only** (`Fix: Enable Minimap2
compilation on ARM64 platforms`, merged 2026-06-10), and 5.3.0 predates it by
two years.

It does not bite, because QUAST prefers an installed minimap2 over its own:

```python
# quast_libs/ca_utils/misc.py:41
minimap_fpath = get_path_to_program('minimap2', contig_aligner_dirpath,
                                    min_version='2.19', recommend_version='2.28')
```

Confirmed in a real run -- the log says `WARNING: Version of installed
minimap2 differs from its version in the QUAST package (2.28)`, and BioFlow's
Debian minimap2 2.27 is what did the aligning. The bundled tree is a fallback
that only compiles when nothing on `PATH` qualifies.

So the guard is a version floor, not a build: **QUAST needs minimap2 ≥ 2.19 on
`PATH`, and if that stops being true it will quietly try to compile the
bundled copy** -- fine on amd64, a failure on arm64 that would surface as a QC
job dying inside a tool nobody thought was compiling anything. Deleting the
bundled `quast_libs/minimap2` tree at install time (step 3 above) converts
that silent fallback into an immediate, legible error, which is the reason it
is on the delete list rather than merely the reason it is unnecessary.

### What QUAST actually detects, verified against constructed errors

This was tested rather than assumed, and the first attempt at testing it was
wrong in an instructive way.

**Attempt 1.** Chop the real yeast genome (GCA_000146045.2) into 200 kb
chunks, reverse-complement every third chunk, run against the GCF reference.
Result: **`# misassemblies 0`**, genome fraction 99.294%.

That is correct behaviour, not a failure to detect. A contig's orientation is
arbitrary -- an inverted *whole contig* aligns cleanly in reverse and asserts
nothing false. A misassembly is a **junction inside a contig** that the
reference contradicts. Anyone testing this slice by inverting a sequence and
expecting a nonzero count will conclude the tool is broken; it is worth having
that in writing before someone spends an afternoon on it.

**Attempt 2.** Four contigs, three of them carrying a real junction error:

| Contig | Constructed error | QUAST |
|---|---|---|
| `ctg_transloc` | 100 kb of chrI joined to 100 kb of chrIV | 1 translocation |
| `ctg_inv` | internal 50 kb segment reverse-complemented | 2 inversions |
| `ctg_reloc` | two loci 600 kb apart on chrIV joined | 1 relocation |
| `ctg_clean` | unmodified 200 kb | -- |

```
# misassemblies            4
  # contig misassemblies   4
    # c. relocations       1
    # c. translocations    1
    # c. inversions        2
# misassembled contigs     3
Misassembled contigs length  700000
```

Note the internal inversion scores **two** misassemblies, one per junction --
so `# misassemblies` counts breakpoints and `# misassembled contigs` counts
contigs, and the two are not interchangeable in any card copy.

`contigs_reports/<name>.misassemblies.gff` gives per-breakpoint coordinates
and types, which is what makes this actionable rather than a scary number:

```
ctg_reloc  QUAST  possible_assembly_error  99998  99999  ...  type=relocation;Note=inconsistency is 600000
```

### Facts, and the ones deliberately not stored

QUAST's `report.tsv` carries 50-odd rows and most of them duplicate facts
`_parse_fasta` already computes for every FASTA at ingest. **Store only what
is reference-derived.** The closing entry deleted `assembly_n50` for exactly
this reason -- "two facts that are supposed to agree, on one object, is a bug
with a delay fuse" -- and QUAST would reintroduce six of them (N50, N90, L50,
L90, auN, total length) computed by a different code path with a different
`--min-contig` cutoff, which means they would eventually disagree honestly.

Proposed namespace `assembly_misassembly_*` (leaving `assembly_completeness_*`
and the bare `sequence_*` contiguity facts untouched):

- `assembly_misassembly_total`, `_relocations`, `_translocations`,
  `_inversions`, `_local`, `_contigs`, `_contigs_length`
- `assembly_reference_genome_fraction_pct`, `_duplication_ratio`
- `assembly_reference_mismatches_per_100kbp`, `_indels_per_100kbp`
- `assembly_reference_unaligned_contigs`, `_unaligned_length`
- `assembly_reference_nga50`, `_nga90` (reference-aware, no ingest-time twin)
- provenance: `assembly_misassembly_tool`, `_tool_version`,
  `assembly_reference_id`, `assembly_reference_name`, `_min_contig`

**The reference is not a footnote.** Every number above is a statement about
*this assembly relative to that reference*, and a misassembly count against a
different-species reference measures real biology as error. RagTag's design
made the same argument for scaffolds and it applies harder here, because a
count reads as a defect rather than as a comparison. `assembly_reference_id`
is a precondition for interpreting the fact set at all, not metadata about the
run -- and `--min-contig` (default 500) belongs with it, since two runs at
different cutoffs are not comparable.

### The HTML report

`report.html` (377 KB) and `icarus.html` (54 KB, the contig browser) are
**self-contained** -- verified: the only outbound `href` is a link to QUAST's
own homepage, and every asset is inlined. They belong under
`qc_reports/<object_id>/` and are served by the existing route
(`api/v1/pipelines.py:391`), the same way NanoPlot's and FastQC's reports
already are. `icarus_viewers/` is a subdirectory of further HTML and the route
already serves relative paths beneath the report dir.

**Corrected 2026-08-05, while writing the implementation plan
(`docs/superpowers/plans/2026-08-05-quast-misassembly-qc.md`): "served by the
existing route, the same way" is not true, and the gap is a stored XSS.**
Two findings, both verified by doing them rather than by reading:

- **QUAST's report renders nothing without JavaScript.** Every value lives in
  a JSON blob inside `<div id='total-report-json'>`, rendered into tables by
  inline script. The route's default CSP is `sandbox`, which disables
  scripting -- so the page arrives blank. QUAST has to join NanoPlot as a
  scripting exception, which means giving up `sandbox` for it.
- **QUAST does not escape the assembly label, and the label is the filename.**
  `qutils.correct_name` sanitizes *contig* names (`[^\w\._\-]` -> `_`,
  confirmed), but `correct_asm_label` only strips and truncates. An input file
  named `ev<img src=x onerror=alert(7)>.fasta` puts that tag verbatim and
  unescaped into `report.html`. Since `assess_completeness` links its input
  under the user's object name, copying that pattern here would be a real
  stored XSS the moment `sandbox` came off.

The fix is at BioFlow's seam, not QUAST's: link the input under a fixed name
and pass `-l assembly`, verified to leave no trace of a hostile filename in
the output. The plan puts that in the handler phase, before the phase that
serves the HTML, so the two never land out of order.

### Inputs, validation, and where it hangs off the code

Inputs are a draft assembly and a reference assembly -- the identical shape
RagTag takes, so `reference_assembly.check_draft_assembly` and
`check_reference_assembly` apply unchanged, including the
`protein.faa` / `cds_from_genomic.fna` exclusions that the align card learned
the hard way.

**`assembly_qc_registry` is the wrong home, despite the name.** Its
docstring anticipates "CRAQ, Merqury and BUSCO all become specs here", but
what it actually models is *completeness*: `CompletenessTool`,
`CompletenessToolSpec`, and an `odb` field that means nothing to a tool with
no ortholog database. QUAST has no lineage and takes a second FASTA. Forcing
it in means an `odb` that is `None` for one member and a `spec_for` that
returns two incompatible shapes. The registry docstring's forward-looking
sentence is aspiration, not a contract, and the implementation issue should
say plainly that it was found to be wrong rather than quietly widening the
dataclass. A sibling registry, or -- given exactly one tool -- a runner with
no registry until a second one arrives, the way `polypolish_runner` and
`ragtag_runner` do.

The rest is the well-worn path: `quast_runner.py`, a queue handler, a launch
endpoint, `pipeline_service.launch_misassembly_qc`, a
`suggestion_service` card, and a `TOOL_META` entry under
`PipelineType.ASSEMBLY_QC` (which already exists, and which
`PipelineToolSelector.tsx` filters on -- do not reach for `ASSEMBLE`).

The card's availability rule needs one thing the completeness card did not:
a *reference other than the assembly itself*. `_distinct_assemblies` already
exists for the align card's version of this question and is the thing to check
against a real project before trusting its unit tests, per this repo's
standing warning about suggestion rules.

### Testing

Beyond the usual runner-parser tests: assert the card goes **unavailable**
when the tool probe is patched off, not available when it is on -- the image
ships tools installed, so the available direction passes whether or not the
patch worked. And the constructed-misassembly FASTA above is small enough to
be a fixture; a parser test fed a hand-built `report.tsv` proves only that the
parser matches the fixture's author's memory of QUAST's row names.

## Slices 2-4: prerequisites, and a correction

The closing entry grouped CRAQ, GCI and Merqury as one deferred blob: "all
need reads realigned to the assembly, which is the **Pilon** entry's blocker".
**That sentence is wrong twice, and both errors are load-bearing for what gets
filed.**

**First: Pilon is gone.** #23 swapped it for Polypolish before any code was
written and `docs/TODO-done.md` records it as "out, permanently". A
prerequisite pointing at it points at nothing.

**Second: realigning reads to an assembly already works.** Verified in the
code, not inferred: `results.py:1246` gives a Flye assembly's FASTA
`ObjectRole.REFERENCE` on ingest, and `pipeline_service._check_reference`
requires only `READY` plus a FASTA format kind -- not an NCBI accession, not a
particular provenance. So a user can already align reads back to their own
assembly through the ordinary align pipeline and get a sorted, indexed BAM.
The blocker the entry described was discharged as a side effect of the
assembly work itself, and nobody went back to say so.

What that leaves is three genuinely different prerequisites, which is why they
should be three issues and not one:

**CRAQ** -- needs reads aligned to the assembly, which now exists. Not
packaged for Debian (verified: no candidate); it is a Perl/shell pipeline from
GitHub over bwa/minimap2/samtools, all of which this image has. Its remaining
unknown is input policy, not tooling: CRAQ's regional/structural error
signals are strongest with both short and long reads, and the child issue
should decide what a single-library run is allowed to claim rather than
running it and reporting whatever comes out.

**GCI** -- needs long reads aligned to the assembly. Its real prerequisite is
a **second aligner**: GCI's method leans on winnowmap alongside minimap2, and
winnowmap has no Debian candidate (verified). The child issue's first job is
to establish whether a minimap2-only run is methodologically honest or a
misuse of the tool. That is an upstream-reading question, not a build
question, and it should be answered before the issue is labelled ready.

**Merqury** -- needs **no alignment at all**. It is k-mer based: a meryl
database built from the *reads*, compared against the assembly's k-mers, which
is what makes it the only base-level-accuracy (QV) measure here and the one
that is independent of any reference. Grouping it with the realignment tools
was the entry's error. Its actual blocker is meryl, and the trap already
recorded in `docs/TODO.md` is confirmed today: **Debian's `meryl` is
`0~20150903+r2013-9+b1`, the Celera Assembler k-mer suite, not Marbl meryl
1.3+.** A probe would find it, report a version, and fail at runtime on
arguments it has never heard of. Marbl meryl is a C++ source build, which puts
this slice's cost closest to compleasm's -- and it should carry compleasm's
lesson: check the release assets for an arm64 manifest before assuming a
binary route exists.

Ordering: Merqury is the most valuable of the three (base accuracy, no
reference, the QV number papers quote) and the most expensive. CRAQ is the
cheapest now that its blocker is gone. GCI is the least certain and should not
be filed as ready until the winnowmap question is answered.

## Child issues to file

1. **Add reference-based misassembly QC with QUAST** -- this note's slice 1,
   `status:ready`. Blocks nothing; blocked by nothing.
2. **Add CRAQ assembly error detection** -- `status:specification document`.
   Note in the body that the realignment prerequisite is already satisfied.
3. **Add Merqury k-mer QV assessment** -- `status:specification document`,
   with the Marbl-vs-Debian meryl trap in the body so it is not rediscovered.
4. **Add GCI assembly continuity inspection** -- `status:specification
   document`, explicitly gated on the winnowmap question.

#13 stays open as the epic until all four land, and its "Related issues"
section gets them. Contamination screening does **not** get an issue: naming a
non-goal in a backlog is how it becomes a goal.

## Open questions

- **Does the misassembly card belong on the assembly object or the reference
  pair?** Every other card keys on one object. This one is only meaningful for
  a pair, and a project with five assemblies and two references has ten
  possible runs. Slice 1's card rule is the first place this shape appears and
  it is worth designing rather than defaulting.
- **`--min-contig` exposure.** Default 500 hides short contigs from the
  counts. Exposing it invites incomparable runs; hiding it invites confusion
  when the contig count in the report disagrees with the one on the object.
  Recommendation: keep the default, store the value, do not expose it yet.
- **`--large` / `-e` for eukaryotes.** Untested here -- the yeast run was fast
  enough not to need it. Should be measured on something vertebrate-sized
  before the resource estimator claims anything about runtime.
