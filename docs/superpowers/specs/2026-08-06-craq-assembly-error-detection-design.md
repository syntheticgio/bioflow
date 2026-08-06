# CRAQ assembly error detection — design

GitHub [#63](https://github.com/syntheticgio/bioflow/issues/63), slice 2 of
epic [#13](https://github.com/syntheticgio/bioflow/issues/13). Follows
[`2026-08-05-remaining-post-assembly-qc-design.md`](2026-08-05-remaining-post-assembly-qc-design.md),
which picked the slices, and the QUAST slice
([#62](https://github.com/syntheticgio/bioflow/issues/62)) that shipped first.

Everything upstream in this document was read from CRAQ's own README and
repository metadata via `gh api` on 2026-08-06, not recalled. Two facts remain
unverified and are marked as such in "Verify before implementing".

## What CRAQ is, and one correction

**CRAQ is reference-*free*.** Its README opens by calling it "a reference-free
genome assembly evaluator." It needs *reads aligned to the assembly*, never a
second genome.

This corrects a claim made mid-brainstorm on this slice, and it matters
because it changes what the Actions card gates on. QUAST's card is dark
without a reference in the project; CRAQ's is dark without a BAM. They are
complementary axes, not two flavours of the same check — which is the reason
the epic wanted both rather than treating QUAST's closure as covering this.

CRAQ works from clipping signals: reads that align to an assembly only
partially, with the remainder clipped, concentrate at positions where the
assembly is wrong. It reports:

- **CRE** — Clip-based Regional Errors, small-scale.
- **CSE** — Clip-based Structural Errors, large-scale.
- **CRH / CSH** — the heterozygous-variant counterparts, which are *not*
  misassemblies. CRAQ separating these is the point of the method: a diploid
  assembly's heterozygous sites otherwise read as errors.
- **R-AQI / S-AQI** — regional and structural Assembly Quality Indicator.

AQI has a published interpretation scale, from the README: **>90 reference
quality, 80–90 high, 60–80 draft, <60 low.** The UI uses these bands rather
than inventing thresholds.

## Slice shape

A single-tool slice modelled on QUAST, not on `assembly_qc_registry`. That
module's own docstring retracts its earlier promise to host CRAQ and says the
next tool should "look at what QUAST actually needed" — its shape only fits a
tool whose one per-run parameter is an OrthoDB lineage, which CRAQ has no
analogue of. So: no registry.

| Piece | Location |
| --- | --- |
| Install | `backend/scripts/install-craq.sh`, called from `backend/Dockerfile` |
| Probe + metadata | `tools.craq()`, `TOOL_META["craq"]` |
| Command + parsers | `backend/app/pipelines/craq_runner.py` |
| Handler | `assembly_qc_handlers.assess_assembly_errors` |
| Launch | `pipeline_service.launch_assembly_error_qc` |
| Route | `POST /pipelines/assembly-errors` |
| Card | `suggestion_service.build_assembly_error_card` |
| UI | `AssemblyFacts.tsx` block + a dialog |

## Input: BioFlow's BAMs

CRAQ accepts either raw reads or pre-made alignments, and **upstream prefers
alignments** — the README's words are "which are highly recommended":

```
craq -g assembly.fa -sms SMS_sort.bam -ngs NGS_sort.bam
```

So the handler consumes sorted, indexed BAMs that BioFlow's own align pipeline
produced. This was already possible before this slice: `results.py:1271` roles
a de novo assembly's FASTA `REFERENCE` on ingest precisely so "the align card
offers aligning these very reads back against their own assembly", and
`_check_reference` requires only `READY` plus a FASTA kind.

Consuming a BAM rather than realigning internally means:

- **Provenance is the *validated* shape**, not by-construction. A BAM aligned
  to this assembly carries the assembly's id in `derived_from`, so
  "is this BAM actually against the assembly under test?" is a lookup, not
  trust. `pipeline_service.reference_for_bam` (`pipeline_service.py:1642`)
  already does exactly this lookup and is reused rather than reimplemented.
- **No hidden second aligner.** A QC job that silently runs minimap2 would
  duplicate work the user can already see, and would make the QC job's cost
  unpredictable.
- **Uploaded BAMs with no provenance are not eligible.** `reference_for_bam`
  returns None for them; the card treats that as "not available" rather than
  guessing.

`-x` (the minimap2 preset) is **ignored when a BAM is supplied**, per the
README, so no preset needs choosing. Chemistry is still needed — see below —
but to route a BAM to the right *flag*, not to pick a preset.

### Which BAM is `-sms` and which is `-ngs`

`pipeline_service.read_chemistry_for_alignment` (`pipeline_service.py:734`)
already answers this. Its `ReadChemistry.SHORT` maps to `-ngs`; `HIFI`, `CLR`,
`ONT_SIMPLEX` and `ONT_DUPLEX` all map to `-sms`.

Its `UNKNOWN`/`None` case is a **genuine ambiguity the launch path must refuse
rather than guess.** That function's docstring says callers "fall back to the
conservative short-read default rather than guessing" — correct for picking an
alignment preset, wrong here, because feeding long reads to `-ngs` does not
degrade gracefully, it mislabels the evidence. The dialog asks instead.

### Pairing policy

Auto-pair when unambiguous, ask otherwise — the same shape
`launch_misassembly_qc` already uses for the one-reference-vs-many case:

- Exactly one short-read BAM and one long-read BAM against this assembly →
  card fires with both, no dialog.
- Exactly one BAM of a single type → card fires single-library.
- Anything else (two long BAMs, unknown chemistry, more than one candidate per
  slot) → dialog with a chooser. The card never guesses.

## Input policy: what a single-library run may claim

CRAQ explicitly supports one library (`-sms` alone, or `-ngs` alone) and the
README states how each case degrades. Rather than a generic "this was
long-only" qualifier, the design uses upstream's specific statements:

> "the lack of SMS long read data will make these CSE and CSH hardly
> detected... The lack of NGS data could potentially cause CRAQ report less
> CRE and CRH, especially for ONT-based assembly."

**A short-only (NGS-only) run does not write `assembly_error_cse_count` at
all.** It does not write zero. This is the load-bearing decision in this
section: a stored `0` will eventually be read by something that has lost the
caveat, and "we found no structural errors" is not what happened — CRAQ barely
looked. Absent means unmeasured, which is the honest encoding and the one that
cannot be misread downstream. This is the same failure shape CLAUDE.md records
for `job_timings`, where technically-present numbers from failed runs quietly
drag estimates in the dangerous direction.

A long-only (SMS-only) run *does* write CRE, since upstream says "less", not
"hardly detected" — but the facts carry the input flags below so the number can
be understood, and the UI notes that CRE is undercounted, especially for ONT.

Facts recording what was available:

- `assembly_error_has_ngs: bool`
- `assembly_error_has_sms: bool`

These are what the UI reads to decide which caveat to render, and what a later
run is compared against — two CRAQ runs with different inputs are not the same
measurement, the same way two completeness scores from different OrthoDB
versions are not.

## Facts written

On the assembly object, merged, never replacing:

| Fact | Type | Notes |
| --- | --- | --- |
| `assembly_error_cre_count` | int | |
| `assembly_error_cse_count` | int | **omitted** on NGS-only runs |
| `assembly_error_crh_count` | int | heterozygous, not an error |
| `assembly_error_csh_count` | int | heterozygous, not an error |
| `assembly_error_r_aqi` | float | |
| `assembly_error_s_aqi` | float | omitted on NGS-only runs |
| `assembly_error_has_ngs` | bool | |
| `assembly_error_has_sms` | bool | |
| `assembly_error_tool` | str | `"craq"` |
| `assembly_error_tool_version` | str | |

**Counts parse as `int`, AQI as `float`, asserted with `isinstance` tests, not
equality.** The QUAST slice shipped
`assembly_reference_unaligned_contigs` as a float invisibly, because `2 == 2.0`
in Python and every assertion was equality-only. That test gap is closed here
by construction rather than rediscovered.

## Chimera breaking (`-b`)

`--break|-b` makes CRAQ emit `out_correct.fa`, the assembly with chimeric
contigs split at conflict breakpoints.

**This is in scope for this slice, by explicit decision (2026-08-06), and it
widens epic #13's stated scope.** #13 says these workflows "do not improve,
scaffold, polish, or produce a replacement assembly", and `out_correct.fa` is
a replacement assembly. The exception is deliberate, not an oversight; #13
carries a note recording it so the boundary reads as amended rather than
contradicted.

Constrained so the widening stays narrow:

- **Default off.** `-b F` unless the user opts in from the dialog. The Actions
  card never enables it — a suggestion that silently rewrites an assembly is
  not a suggestion.
- **Never modifies or replaces the original.** QC facts still land on the
  input assembly; the corrected FASTA is a *new* object.
- **Ingested with full provenance**: `derived_from=[assembly, <each input
  BAM>]`, `produced_by_job` set, roled `REFERENCE` the same way
  `results.py:1271` roles a de novo assembly — so it is alignable and
  visible as derived work rather than swapped in.
- **A separate applier**, following `results.py`'s existing pattern where a
  secondary output failing to ingest never destroys the primary result.

## Security

QUAST's slice found a stored XSS: QUAST sanitizes contig names but not the
assembly *label*, which it takes from the input filename, so a filename like
`ev<img src=x onerror=alert(7)>.fasta` reached `report.html` unescaped.

CRAQ is a Perl/shell pipeline, so the analogous risk is **shell
metacharacters in filenames reaching a shell**, and the mitigation is the same
one, applied before the bug exists:

- The handler links every input under a **fixed** filename in the work
  directory and passes those fixed paths to `craq`. The object's own name never
  reaches the command line.
- Output is parsed from CRAQ's `.Report` and `.bed` files. **No CRAQ-generated
  HTML or PDF is served.** `-pl` (pycircos plotting) stays off, which also
  keeps a Python dependency out of the image.
- Contig names from `.bed` files are data from the user's own assembly, but
  they land in JSON facts and React-rendered text, both of which escape by
  default. No `dangerouslySetInnerHTML`.

## Actions card

`build_assembly_error_card`, keyed on the assembly object. Unavailable
reasons, each saying what the user can do:

- CRAQ not installed → the tool's probe error.
- No BAM against this assembly → "Assembly error detection needs reads
  aligned to this assembly. Align a read set against it first."
- BAMs exist but chemistry is unknown → available, routed to the dialog.

Registered in the card list in `suggestion_service.py`. **This is the step
that is silently skippable** — CLAUDE.md records that installing a tool
without a rule that can pick it leaves a card reading "no tool installed"
beside an installed tool. Its test goes in
`backend/tests/services/test_suggestion_service.py`, asserting the card flips
to **unavailable** when the probe is patched off, since the image ships most
tools installed and the available-direction assertion passes whether or not
the patch worked.

## Install

Not packaged for Debian (verified: no apt candidate). From GitHub:

- Perl 5, samtools 1.3.1+, minimap2 2.17+ — **all already in this image**.
- pycircos is plotting-only and **not installed**.
- `git clone` + place `craq` and its `bin/` on PATH. No compilation.
- MIT licensed (`gh api repos/JiaoLaboratory/CRAQ` → `spdx_id: MIT`).
- Cite: Li et al., CRAQ, DOI `10.5281/zenodo.8404831`.

`TOOL_META["craq"]` needs `homepage`, `citation`, `license`, `usage` or
`test_every_tool_is_documented` fails. `usage` describes behaviour, not flags:
CRAQ is run against BioFlow-produced BAMs only, reference-free, plotting off,
chimera-breaking opt-in.

## Report format, read from upstream source (2026-08-06), corrected against a real run (2026-08-06)

Initially resolved from `src/format_results_addAQI.pl` via `gh api`, not
measured — and that source read got two things wrong, both caught by
actually running CRAQ 1.10 against real yeast data
(GCA_000146045.2_R64_genomic.fna + a DRR1066343 short-read BAM) rather than
by re-reading the source more carefully. Corrected here rather than silently
fixed, because the wrong version shipped in this same document for hours and
is worth recording as the reason a real run, not just a source read, is what
"verify before implementing" means:

1. **The report is `runAQI_out/out_final.Report` — unconditionally, never
   `<genome_basename>_final.Report`.** All three of
   `runAQI.sh`/`runAQI_SMS.sh`/`runAQI_NGS.sh` hardcode `name="out"` at line 5
   of each script; the earlier read of `runAQI.sh`'s `$name` usage never
   confirmed what `$name` actually resolves to. This made every real run raise
   a spurious `RetryableError`, because the handler was looking for a file
   that never existed under that name.
2. **The whole-assembly summary row is keyed `Genome`, not `all`.** Confirmed
   hardcoded in `src/final_short_report_minlen.pl:42` — a literal, not
   derived from any input filename or chromosome name, so this is
   upstream-stable rather than particular to one run. Every unit test's own
   fixture used `"all"`, so all of them passed while testing an assumption no
   real report satisfies.

Its format, verbatim from the formatter:

```
Short Report:
#Chr	Covered.Rate	Low-conf.Rate	Avg.CRH	Avg.CSH	Avg.CRE(R-AQI)	Avg.CSE(S-AQI)	AQI
```

Three consequences for the parser:

1. **R-AQI and S-AQI are embedded inside columns**, not columns of their own:
   the CRE column is literally `<value>(<R-AQI>)`. Both need extracting from
   one field with a regex, matching the formatter's own
   `/(\S+)\((\S+)\)/`.
2. **AQI is a harmonic mean** of S-AQI and R-AQI —
   `2*S*R/(S+R)` — not an independent measurement. It is parsed rather than
   recomputed, but knowing it is derived matters: it is meaningless whenever
   either input is.
3. Every numeric field is `sprintf("%.3f")`, so all of them parse as
   **float**. The counts in this report are *averages* (`Avg.CRE`), not the
   integer counts the fact table needs — those come from counting `.bed`
   rows, which is a separate parse.

### The NGS-only trap this exposes

`runAQI_NGS.sh` (the short-only path) pipes through **the same**
`format_results_addAQI.pl`. That formatter unconditionally prints all eight
columns and computes AQI from both R-AQI and S-AQI. So a short-only run
emits **structurally valid CSE, S-AQI and AQI numbers that mean nothing** —
upstream says CSE is "hardly detected" without long reads, and here it is
printed as a clean `0.000` anyway.

This is the concrete form of the omission rule above, and it is stronger
than the spec first assumed: the danger is not that we might choose to write
a zero, it is that **reading the file correctly produces one**. A parser
that simply maps columns to facts ships the fabricated number by default.
So the omission is enforced at the parser, driven by which inputs were
supplied, and never by trusting the file's contents. Same for the derived
AQI, which inherits the meaninglessness of its S-AQI input.

**Confirmed against the real report** produced by the run above (short reads
only): the `Genome` row printed `0.000(100.000)` for the CSE/S-AQI field —
exactly the clean, structurally valid, meaningless value this section
predicted — and the shipped parser correctly omitted
`assembly_error_cse_count`/`assembly_error_s_aqi`/`assembly_error_aqi` from
the stored facts rather than storing that zero.

## Measured (2026-08-06, real run)

Both fell out of the same real run against `GCA_000146045.2_R64_genomic.fna`
(12.1 Mb, 16 sequences) and a DRR1066343 short-read BAM already aligned to it:

- **Install size: 43 MB** (`du -sh /opt/craq`), including the pinned commit's
  shallow-cloned `.git` — QUAST's 8.6 MB is not a fair comparison, since that
  figure came after trimming everything a reference run never touches, which
  this install does not attempt.
- **Runtime: 61–67 s end to end** (two runs, before and after the `bc` fix
  below), API launch to job `succeeded`, against a BAM CRAQ never had to
  produce itself. CRAQ's own README warns that read mapping dominates its
  cost from FASTQ; supplying a pre-made BAM is exactly what keeps this run
  in the low-minute range rather than needing README's warning to apply.
- **A real, if low-severity, install gap**: `bc` was missing from the image.
  `runSR.sh`/`runLR.sh`/`runAQI.sh` call it 7 times total, all in
  parameter-sanity guards (negative/zero checks on mapq, threads, clip-rate
  cutoffs). Confirmed these guards fail *open* without it — `bc: command not
  found` followed by a harmless `[: -eq: unary operator expected` — so the
  run still completed and produced correct facts either way. Fixed by adding
  `bc` to the install script regardless: BioFlow never passes a parameter
  that would need catching today, but a silently-skipped safety check is the
  same shape of bug this repo's CLAUDE.md warns about elsewhere, and `bc`
  costs a few KB.

## Testing

- `craq_runner` command builder: flag routing by chemistry, fixed filenames,
  `-b` off by default, `-pl` never set.
- Parsers: typed with `isinstance`, including the NGS-only case asserting CSE
  facts are **absent** rather than zero.
- Launch path: refuses unknown chemistry; validates each BAM's `derived_from`
  actually contains the assembly.
- Card: flips to unavailable when the probe is patched off; dark with no BAM.
- A real-data check against the running stack, per CLAUDE.md — the suggestion
  rules shipped two bugs that a full green suite missed and one look at a real
  project exposed.

## Non-goals

- **Contamination screening.** Named non-goal of #13; FCS-GX needs ~470 GB.
- **Realigning from FASTQ inside the QC job.** Upstream prefers BAMs and the
  align pipeline already exists.
- **CRAQ's IGV inspection workflow and circos plots.**
- **Reference-based misassembly detection** — that is QUAST, shipped in #62.
