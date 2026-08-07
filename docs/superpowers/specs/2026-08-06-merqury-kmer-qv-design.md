# Merqury k-mer QV assessment — design

GitHub [#64](https://github.com/syntheticgio/bioflow/issues/64), slice 3 of
epic [#13](https://github.com/syntheticgio/bioflow/issues/13). Follows
[`2026-08-05-remaining-post-assembly-qc-design.md`](2026-08-05-remaining-post-assembly-qc-design.md),
which picked the slices, and the two that shipped before it: QUAST
([#62](https://github.com/syntheticgio/bioflow/issues/62)) and CRAQ
([#63](https://github.com/syntheticgio/bioflow/issues/63),
[design](2026-08-06-craq-assembly-error-detection-design.md)).

Every upstream fact below was read from Marbl's release metadata and from
Merqury's own scripts via `gh api` on 2026-08-06, not recalled. Facts that
need a real run to confirm are listed under "Verify before implementing"
rather than asserted.

## Why this slice matters

Merqury measures **base-level accuracy (QV)** — the fourth QC axis in #13's
table, and the only one of the four that needs no reference genome. It is the
number papers quote for assembly quality. QUAST measures structure against a
reference; CRAQ measures structure against reads; neither says whether the
*bases* are right.

It is also the only one of the three deferred slices that needs **no
alignment at all**. #13 corrected `docs/TODO.md`'s grouping of Merqury with
CRAQ and GCI as "needs reads realigned to the assembly" — Merqury is k-mer
based: a meryl database built from the *reads*, compared against the
assembly's k-mers.

## The premise correction: there is no source build

The issue says this slice **"costs a C++ source build, which puts it closest
to compleasm in cost."** That was true when it was written. It is not true
now, and the reason is two weeks old.

The issue's own instruction was to **"check Marbl's release assets for an
arm64 manifest before assuming a binary route exists"** — written expecting
the check to fail, as it did for bwa-mem2, for compleasm's release asset, and
for compleasm's biocontainer. Checked 2026-08-06:

| Release | Date | Assets |
| --- | --- | --- |
| **v1.4.2** | **2026-07-21** | Darwin-amd64, **Darwin-arm64**, Linux-amd64, **`meryl-1.4.2.Linux-arm64.tar.xz`**, source, `SHA256SUMS` |
| v1.4.1 | 2023-09-25 | Darwin-amd64, Linux-amd64, source |
| v1.4 | 2023-01-20 | Darwin-amd64, Linux-amd64, source |

**v1.4.2 is the first meryl release ever to ship a `Linux-arm64` binary.**
The arm64 check passes, by two weeks. This is a tarball extract — the
cheapest install shape in this repo — not compleasm's cost.

**The v1.4.2 pin is load-bearing and must carry a comment saying so.**
Merqury's README still points at v1.4.1 (written before v1.4.2 existed).
Following the README, or "relaxing" the pin to a floor like `>=1.4.1`,
silently reintroduces the arm64 source build this repo has been bitten by
three times. The pin is not conservatism about upstream churn; it is the
single fact that makes this slice cheap.

`SHA256SUMS` ships with the release, so the install verifies rather than
trusting the download.

## The Debian meryl trap, re-confirmed

The trap recorded in `docs/TODO.md` and in #13 is real and unchanged:
**Debian's `meryl` is `0~20150903+r2013-9+b1`**, the Celera Assembler k-mer
suite, not Marbl meryl. Merqury needs Marbl meryl 1.3+.

This is the same failure shape as Debian's BUSCO: a probe finds the binary,
reports a version, and the install looks green — then the run fails at
runtime on arguments the binary has never heard of.

Two mitigations, because one is not enough:

- **The probe rejects it explicitly.** `tools.meryl()` asserts Marbl's shape
  and treats a `0~2015`-style version as *not installed*, with an error
  saying which meryl was found and which is needed. A probe that merely
  reports whatever version string it gets is the bug.
- **The path is pinned, not searched.** `settings.meryl_path` defaults to
  `/opt/meryl/bin/meryl` rather than a bare `PATH` lookup, so a future
  `apt-get install meryl` — pulled in as some other package's dependency —
  cannot shadow the correct binary.

## Scope: the full suite, including spectra-cn plots

Decided 2026-08-06. `merqury.sh` in a single invocation produces QV, k-mer
completeness, and the spectra-cn copy-number plots.

Worth recording, because it is the part that is easy to get wrong later:
**the QV number alone would not need R or Java.** `eval/qv.sh` is a
standalone script whose only external calls are `meryl`, `bedtools` and
`awk` — verified by reading it. Merqury's README lists "R with argparse,
ggplot2, and scales" and "Java run time environment" as dependencies, but
those belong to `spectra-cn.sh` (via `Rscript $MERQURY/plot/plot_spectra_cn.R`)
and to the bundled `bedCalcN50.jar` / `kmerHistToPloidyDepth.jar`.

So the four R packages are **the marginal cost of the plots specifically**,
not of QV. That trade was taken deliberately: the copy-number spectra is the
visual Merqury is known for, and it shows things a scalar cannot — false
duplications, missing sequence, and ploidy, each as a distinct shape in the
histogram.

Anyone later wanting to shrink the image should know that dropping the plots
recovers `r-cran-*` entirely and loses nothing from the fact table.

### Trio / hap-mer mode is out of scope

`merqury.sh` accepts maternal and paternal hapmer databases for
haplotype-resolved assemblies. BioFlow has no concept of parental read sets,
and inventing one here would be a second feature wearing this one's issue
number. Single-assembly and the two-assembly (`asm1`+`asm2`) forms cover
what BioFlow can express today.

## Install

| Piece | Detail |
| --- | --- |
| `backend/scripts/install-meryl.sh` | Arch-select `meryl-1.4.2.Linux-{amd64,arm64}.tar.xz`, verify against `SHA256SUMS`, extract to `/opt/meryl` |
| `backend/scripts/install-merqury.sh` | Merqury v1.4.1 has **no release assets** (verified: `assets: []` on every tag) — fetch the tag's source archive, install to `/opt/merqury`, export `MERQURY` |
| apt additions | `bedtools`, `r-cran-argparse`, `r-cran-ggplot2`, `r-cran-scales` |
| already present | `default-jre-headless` (`backend/Dockerfile:58`), `samtools` (line 63) |

The R packages are all Debian-packaged, so this is an apt layer and **not**
an `install.packages()` compile at build time — which would be slow, network-
dependent, and the kind of thing that breaks a build months later.

`MERQURY` must be set in the image environment, not only in the install
script's shell: every one of Merqury's scripts begins
`source $MERQURY/util/util.sh` and fails immediately without it.

Both scripts follow `install-compleasm.sh` / `install-craq.sh` in shape.

## The k-mer database: what is cached and what is not

The issue asks for this explicitly — *"where the k-mer database lives, and
whether it is cached across runs — a meryl db is derived from reads and is
expensive enough that rebuilding per assembly may be the wrong default."*

**The read database is cached and shared. The assembly database is not.**
They have genuinely different lifetimes, and conflating them is what makes
the naive implementation wasteful:

- **Read meryl db** — derived from the *read set* alone, with no reference to
  any assembly. Two assemblies built from the same reads must reuse it. This
  is the expensive artifact, and rebuilding it per assembly is exactly the
  wrong default the issue warns about. It is stored as a **sidecar on the
  read object**, under a new `SidecarRole.MERYL_DB`.
- **Assembly meryl db** — derived from the assembly, cheap, and consumed
  immediately. It stays in the job's scratch directory and is not ingested.

### `k` is part of the database's identity

Not a run parameter that can vary against a cached db. `eval/qv.sh` *reads k
back out of the database* rather than taking it as an argument:

```bash
k=`meryl print $read_db | head -n 2 | tail -n 1 | awk '{print length($1)}'`
```

So a database built at one k cannot serve a run that wants another. The
sidecar records its k; a request at a different k **builds a new database**
rather than silently reusing a mismatched one. Merqury ships `best_k.sh` to
derive k from genome size — that is the default, overridable from the dialog.

### `SidecarRole` is a registry with a silent-skip failure mode

`SidecarRole` (`backend/app/models/object.py:120`) and
`queue/results.py:2128`'s `_SIDECAR_ROLES` are the exact pair CLAUDE.md names as the worked
example of this repo's hand-maintained-registry trap: adding STAR cost a
`build_index` job that reported success while storing none of its eight index
files, with the full suite green throughout, because the applier skipped
roles the allowlist did not know.

`_SIDECAR_ROLES` is the **derivable** kind (`{role.value: role for role in
SidecarRole}`), so the new member must land in the enum and the allowlist in
the same commit, and the existing exhaustiveness test covers it.

## Facts written

On the assembly object, merged, never replacing:

| Fact | Type | Notes |
| --- | --- | --- |
| `assembly_qv` | float | The QV score |
| `assembly_qv_error_rate` | float | |
| `assembly_qv_completeness_pct` | float | k-mer completeness |
| `assembly_qv_k` | int | |
| `assembly_qv_read_object_id` | str | |
| `assembly_qv_read_object_name` | str | |
| `assembly_qv_tool` | str | `"merqury"` |
| `assembly_qv_tool_version` | str | |
| `assembly_qv_meryl_version` | str | |

**The read set is not a footnote.** Every number here is a statement about
*this assembly against those reads*. Reads from a different individual — or a
different library with different bias — measure real biology as error, the
same way QUAST's misassembly count against a different-species reference
does. `assembly_qv_read_object_id` is a precondition for interpreting the
fact set at all, not metadata about the run.

**Counts parse as `int`, scores as `float`, asserted with `isinstance`, not
equality.** The QUAST slice shipped `assembly_reference_unaligned_contigs` as
a float invisibly, because `2 == 2.0` in Python and every assertion was
equality-only. CRAQ's slice closed that gap by construction; this one keeps it
closed.

## Reports

spectra-cn emits **PNGs** — `ggsave(..., dpi=300)`, with `-p` switching to
PDF (`cairo_pdf`). It emits no HTML at all.

That is a meaningful simplification over QUAST's slice, which had to give up
the `sandbox` CSP because QUAST's report renders nothing without JavaScript,
and then had to fix a stored XSS that the loss of `sandbox` had made
reachable. **Static images are inert**: they go under
`qc_reports/<object_id>/` and are served by the existing route
(`api/v1/pipelines.py:424`) with no CSP exception and none of QUAST's
scripting exposure.

**The filename-into-output risk still applies, and is handled the same way.**
`merqury.sh` derives every output name from the input basename, and
`util.sh`'s `link` symlinks inputs under their own names. The handler links
the assembly under a **fixed** name (`assembly.fasta`) and the read db under a
fixed name, so the object's own name never reaches an output path or a shell
word. This is QUAST's lesson applied before the bug exists, exactly as CRAQ's
slice did it.

## Actions card

`build_qv_card`, keyed on the assembly object. Unavailable reasons, each
saying what the user can do:

- meryl or merqury not installed → the probe's error, naming which.
- No read set in the project → "QV assessment compares an assembly against
  the reads it came from. Add a read set to this project first."
- Reads present but none usable → the specific reason.

Registered in the card list in `suggestion_service.py`. **This is the step
that is silently skippable** — CLAUDE.md records that installing a tool
without a rule that can pick it leaves a card reading "no tool installed"
beside an installed tool.

Its test asserts the card flips to **unavailable** when the probe is patched
off. The image ships tools installed, so the available-direction assertion
passes whether or not the patch worked — that direction proves nothing.

### Which reads

Merqury's method is built around a WGS read set of the assembled individual;
the README's framing is Illumina, and QV from a short-read k-mer spectrum is
the standard use. HiFi reads are also k-mer-accurate enough to be used this
way in practice.

**Pairing policy, following CRAQ's shape:** auto-pair when unambiguous, ask
otherwise. Exactly one eligible read set in the project → the card fires with
it. More than one → the dialog chooses. The card never guesses which reads an
assembly came from, because a wrong pairing produces a confidently wrong QV
rather than an error.

**Provenance is preferred over guessing where it exists.** A de novo assembly
carries its reads in `derived_from`; when that is present, it is the default
selection in the dialog rather than something the user has to re-derive.

## Slice shape

A single-tool slice modelled on QUAST and CRAQ, **not** on
`assembly_qc_registry`. That module's docstring once promised to host
Merqury; QUAST's slice found the promise was aspiration rather than a
contract, and the docstring now retracts it. Its shape fits a tool whose one
per-run parameter is an OrthoDB lineage. Merqury's is a read set and a k.

| Piece | Location |
| --- | --- |
| Install | `backend/scripts/install-meryl.sh`, `install-merqury.sh` |
| Probe + metadata | `tools.meryl()`, `tools.merqury()`, `TOOL_META` entries |
| Command + parsers | `backend/app/pipelines/merqury_runner.py` |
| Handler | `assembly_qc_handlers.assess_assembly_qv` |
| Launch | `pipeline_service.launch_qv_qc` |
| Route | `POST /pipelines/assembly-qv` |
| Card | `suggestion_service.build_qv_card` |
| UI | `AssemblyFacts.tsx` block + a dialog |

`TOOL_META` needs `homepage`, `citation`, `license`, `usage` for **both**
meryl and merqury or `test_every_tool_is_documented` fails. Verify license and
citation against each repository rather than recalling them — a wrong license
claim on a page that reads as authoritative is worse than a blank field.
`usage` describes behaviour, not flags: Merqury is run against a cached read
meryl db and one assembly, QV plus completeness plus spectra-cn, trio mode
never.

Two tools, two `TOOL_META` entries, two `cache_clear()` registrations
(`tools.py:1936`).

## Testing

- **Install**: the arm64 asset actually resolves and extracts on both arches.
  This is the claim the whole cost estimate rests on.
- **Probe**: rejects a Debian-shaped version string. This is an explicit
  acceptance criterion on the issue, and the only test that catches the trap.
- **Runner**: fixed input filenames; k derived from `best_k.sh` and
  overridable; trio flags never emitted.
- **Parsers**: typed with `isinstance`, not equality.
- **Cache**: a second run against the same reads at the same k reuses the
  sidecar; a run at a different k builds a new database rather than reusing a
  mismatched one.
- **Card**: flips to unavailable when the probe is patched off; dark with no
  reads.
- **A real-data check against the running stack**, per CLAUDE.md — the
  suggestion rules shipped two bugs that a full green suite missed and one
  look at a real project exposed.

## Verify before implementing

Everything above was read from release metadata and upstream scripts. These
need a real run in this image:

- **The arm64 tarball runs here.** The asset exists; that it executes on this
  image's arm64 is not the same claim.
- **Read-db build cost and size**, which is what decides whether the sidecar
  is worth its complexity. It is the reason the cache exists, so it should be
  measured rather than assumed.
- **The exact `.qv` and `completeness.stats` column layouts.** CRAQ's slice
  shipped a parser built from a careful source read that was wrong in two
  ways, caught only by a real run — including a summary row keyed `Genome`
  where every fixture said `all`. Build the fixture from a real output file.
- **Whether `merqury.sh` succeeds with R present but no display**, the usual
  headless-`ggsave` failure mode.

## Non-goals

- **Trio / hap-mer mode.** No parental read-set concept in BioFlow.
- **Merqury's other evaluations** — `false_duplications.sh`,
  `asm_multiplicity.sh`, block-N plots. QV, completeness and spectra-cn are
  this slice.
- **Contamination screening.** Named non-goal of #13; FCS-GX needs ~470 GB.
- **Winnowmap.** Not part of this slice — but note that this slice installs
  meryl, which is winnowmap's only unusual dependency. See the GCI design
  ([#65](https://github.com/syntheticgio/bioflow/issues/65)), where that
  matters.
