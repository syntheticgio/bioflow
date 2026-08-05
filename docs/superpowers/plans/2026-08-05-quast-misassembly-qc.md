# Reference-based misassembly QC (QUAST) — implementation plan

**Date:** 2026-08-05

**Issue:** [#62](https://github.com/syntheticgio/bioflow/issues/62)

**Spec:** [`docs/superpowers/specs/2026-08-05-remaining-post-assembly-qc-design.md`](../specs/2026-08-05-remaining-post-assembly-qc-design.md), slice 1

The spec left three open questions. This plan answers two of them, and
**adds one the spec did not know about**: QUAST's HTML report cannot be served
the way this app serves FastQC's, and the reason is a stored-XSS vector that
was verified by exploiting it, not inferred. That finding moves work from
Phase 6 into Phase 3 and is the single most important thing on this page.

Where this plan makes a call the spec did not, it is marked **[plan decision]**.

## Shape of the change

Seven phases, each independently committable and revertable.

Phases 1–2 are inert: a tool that installs and a module of pure functions,
neither reachable from the app. Phase 3 makes it runnable, Phase 4 launchable,
Phase 5 discoverable. Phase 6 is the HTML report and is deliberately last
because it is the only phase with a security decision in it — everything a
user needs (the numbers) ships in Phases 3–5, so if Phase 6 stalls in review
the feature is still complete. Phase 7 is verification against the running app.

**[plan decision]** The facts ship before the report, not with it. The spec
treats them as one deliverable; splitting them means the XSS-adjacent work is
one revertable commit that no other phase depends on.

---

## Phase 1 — install QUAST

**Files:** `backend/scripts/install-quast.sh`, `backend/Dockerfile`,
`backend/app/config.py`, `backend/app/pipelines/tools.py`,
`backend/pyproject.toml`

Follow `install-compleasm.sh`'s shape, including its habit of recording *why*
in the script rather than in a commit message.

```sh
QUAST_VERSION="${QUAST_VERSION:-5.3.0}"
```

Four things the script must do, each verified on 2026-08-05:

**1. Fetch the GitHub tarball, not PyPI.** `pip install quast` gets 5.2.0
(2022); 5.3.0 exists only as a GitHub release. Source:
`https://github.com/ablab/quast/archive/refs/tags/quast_${QUAST_VERSION}.tar.gz`

**2. Patch two `distutils` imports.** Python 3.12 removed the module, and
unpatched QUAST dies on import:

| File | Line | Replace with |
|---|---|---|
| `quast_libs/qconfig.py` | 13 | `from packaging.version import Version as LooseVersion` |
| `quast_libs/ra_utils/misc.py` | 79 | `shutil.copytree(..., dirs_exist_ok=True)` |

**[plan decision] Add `packaging>=24` to `pyproject.toml`'s dependencies.**
It is present in the image today (26.3) but only transitively, declared by
nothing. The patch above makes an installed tool's importability depend on it,
and a transitive dependency that something load-bearing relies on is exactly
the kind of thing that disappears in an unrelated upgrade. One line, and it
documents the coupling.

**Do not** solve the `distutils` problem by pinning `setuptools<81` for its
shim. It works — verified — and it is worse: it makes a bioinformatics tool's
importability a property of a global build-system pin, breakable by any future
`pip install`, discoverable only when a QC job fails. The two-line patch was
verified with setuptools *uninstalled* and `import distutils` raising.

**3. Delete what a reference run never touches.** 400 MB → 8.6 MB, verified
working after removal:

```
external_tools/  tc_tests/  test_data/  manual.html
quast_libs/{genemark,genemark-es,barrnap,glimmer,sambamba,busco,minimap2}
```

The first four are bulk. The `quast_libs` entries back `--gene-finding`,
`--rna-finding`, `--conserved-genes-finding` and the reads-alignment mode,
none of which this slice offers.

**`quast_libs/minimap2` is on that list for a second reason and the comment
must say so.** QUAST prefers an installed minimap2 over its bundled copy
(`ca_utils/misc.py:41`, `get_path_to_program('minimap2', ..., min_version='2.19')`)
— confirmed in a real run, which logged `WARNING: Version of installed
minimap2 differs from its version in the QUAST package (2.28)` and used
Debian's 2.27. If `PATH` minimap2 ever drops below 2.19, the bundled tree
would be **compiled on demand**, and the arm64 fix for that compile is on
QUAST's master branch only (merged 2026-06-10, two years after 5.3.0). Deleting
the tree turns a silent, arch-dependent fallback into an immediate error.

**4. Install to `/opt/quast` with a wrapper on PATH.**

```sh
printf '#!/bin/sh\nexec python3 /opt/quast/quast.py "$@"\n' > /usr/local/bin/quast.py
```

**[plan decision]** A wrapper, not a symlink. QUAST locates `quast_libs`
relative to its own module path, and whether that survives a symlink is a
question the wrapper makes unnecessary. Named `quast.py` to match how the tool
is invoked and documented, the same way `ragtag_path` is `ragtag.py`.

Then:

- `config.py`: `quast_path: str = "quast.py"`, beside `ragtag_path`.
- `tools.py`: `@lru_cache(maxsize=1) def quast() -> Tool: return _probe("quast", settings.quast_path, ["--version"])`.
  Prints `QUAST v5.3.0` and exits zero — verified. Note in a comment that it
  also prints `WARNING: Python locale settings can't be changed` to stderr,
  which `_probe` already tolerates.
- `TOOL_META["quast"]` with `pipelines=(PipelineType.ASSEMBLY_QC,)` and real
  `homepage` / `citation` / `license` / `usage` — `test_every_tool_is_documented`
  fails until they exist, which is the point. **Verify the license and citation
  against ablab/quast itself, not from memory**, per CLAUDE.md.
- `usage` describes behaviour: that BioFlow runs it only in reference-based
  mode, against a reference the user picks, and stores the misassembly
  breakdown as facts. No flags — they change when the runner is tuned.

Also add an entry to `sources.py` if a reference genome source is implicated;
it is not here, so most likely no change — check rather than assume, since it
has its own completeness test.

**Green gate:** `docker compose up -d --build api web worker` from the **main
checkout**, then `./backend/run-worktree-tests.sh tests/ -q`. The image build
is the real test of this phase; the suite only proves nothing regressed.

---

## Phase 2 — `quast_runner.py`, pure functions

**Files:** `backend/app/pipelines/quast_runner.py`,
`backend/tests/pipelines/test_quast_runner.py`

Same split `ragtag_runner` and `polypolish_runner` use: strings and paths in,
dicts out, no container, no queue, no binary.

```python
def build_quast_command(*, quast_path, assembly, reference, out_dir,
                        threads, min_contig=500, label="assembly") -> list[str]
```

Positional order is `<assembly>` last, with `-r <reference>` as a flag — so
unlike RagTag there is no transposition trap here. Assert the argv anyway.

**`label` is not cosmetic and its default must not be the object's name.**
See Phase 3; the parameter exists so that Phase 3 has one place to pass a safe
value, and its docstring should say why rather than leaving it looking like a
nicety.

Parsers, each `{}` on unparseable input rather than raising — the posture
`ragtag_runner.parse_stats` documents, for the same reason: a summary that
cannot be read must not fail a run that already did the work.

- `parse_report_tsv(text) -> dict` — from `report.tsv`, **reference-derived
  rows only**:

  | QUAST row | Fact |
  |---|---|
  | `# misassemblies` | `assembly_misassembly_total` |
  | `# misassembled contigs` | `assembly_misassembly_contigs` |
  | `Misassembled contigs length` | `assembly_misassembly_contigs_length` |
  | `# local misassemblies` | `assembly_misassembly_local` |
  | `Genome fraction (%)` | `assembly_reference_genome_fraction_pct` |
  | `Duplication ratio` | `assembly_reference_duplication_ratio` |
  | `# mismatches per 100 kbp` | `assembly_reference_mismatches_per_100kbp` |
  | `# indels per 100 kbp` | `assembly_reference_indels_per_100kbp` |
  | `# unaligned contigs` | `assembly_reference_unaligned_contigs` |
  | `Unaligned length` | `assembly_reference_unaligned_length` |
  | `NGA50` / `NGA90` | `assembly_reference_nga50` / `_nga90` |

- `parse_misassemblies_report(text) -> dict` — from
  `contigs_reports/misassemblies_report.tsv`, the breakdown `report.tsv` does
  not carry: `_relocations`, `_translocations`, `_inversions`, each under
  `assembly_misassembly_`. Note the file's rows are **indented** (`    # c.
  relocations`), so match on stripped keys.

**The exclusion list is a rule, not an oversight, and belongs in the module
docstring.** N50, N90, L50, L90, auN and total length are *deliberately not
parsed*, even though QUAST reports them: `_parse_fasta` already computes them
for every FASTA at ingest, and QUAST computes them over a `--min-contig 500`
subset. Two facts that are supposed to agree, from different code paths with
different cutoffs, is the bug `assembly_n50` was deleted for. Add a test that
asserts the parser returns **no** key starting `sequence_` and no `n50` key —
the test exists to fail when someone helpfully widens the parser.

**Tests.** Use a real `report.tsv` captured from a real run as a fixture, not
a hand-written one. A hand-built fixture tests only that the parser matches its
author's memory of QUAST's row names, which is precisely the failure mode this
repo has hit before.

Fixture data to capture (from the verification runs on 2026-08-05, real yeast
GCA_000146045.2 chopped into deliberate junction errors against the GCF
reference):

| Contig | Constructed error | QUAST |
|---|---|---|
| `ctg_transloc` | 100 kb chrI + 100 kb chrIV in one contig | 1 translocation |
| `ctg_inv` | internal 50 kb reverse-complemented | **2** inversions |
| `ctg_reloc` | two chrIV loci 600 kb apart joined | 1 relocation |
| `ctg_clean` | unmodified | — |

Totals: `# misassemblies 4`, `# misassembled contigs 3`.

**Two things worth a test comment so they are not "fixed" later:**

- **An internal inversion scores two misassemblies, one per junction.** So
  `_total` counts breakpoints and `_contigs` counts contigs; they are not
  interchangeable, least of all in card copy.
- **A whole-contig inversion is not a misassembly.** Verified: chopping the
  genome into chunks and reverse-complementing every third gives
  `# misassemblies 0`, correctly — contig orientation is arbitrary. Anyone
  testing this by inverting a sequence and expecting a nonzero count will
  conclude the tool is broken.

**Green gate:** `./backend/run-worktree-tests.sh tests/pipelines/test_quast_runner.py -q`

---

## Phase 3 — the handler, and the label that is not cosmetic

**Files:** `backend/app/queue/assembly_qc_handlers.py` (extend),
`backend/app/queue/results.py`, tests for both

`assess_misassemblies`, modelled on `assess_completeness` in the same module —
read-only, facts merged onto the assembly, no new object.

```python
@handler("assess_misassemblies", mode=HandlerMode.SUBPROCESS,
         job_class=JobClass.COMPUTE,
         resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
         max_attempts=1)
```

**[plan decision] `max_attempts=1`**, matching `assess_completeness`: input and
tool are both deterministic, so a retry fails identically.

**[plan decision] A one-hour lease**, versus completeness's three. A 12 Mb
yeast assembly runs in **3.0–4.4 s** measured, so an hour is enormously
generous — but nothing vertebrate-sized has been measured (spec open question),
and a lease that expires mid-run is a worse failure than one set too long.

Failure handling copies `assess_completeness` exactly: non-zero exit →
`_failure(code, log_path, "quast")`; exit zero with no `report.tsv` →
`RetryableError`, because an exit-0 run with no report is most plausibly a
disk that filled on the final write.

Progress: `ctx.progress(phase=...)` with no percentage, the same posture Flye
takes. QUAST's stages differ enormously in duration and its stdout gives no
countable unit.

### The label finding — the reason this phase is not routine

`assess_completeness` links its input as `_named_link(work, assembly,
ctx.payload.get("assembly_name"))`, i.e. **under the user's object name**.
Doing the same here puts the object name into QUAST's HTML report, and QUAST
does not escape it.

Verified by exploiting it. A file named
`ev<img src=x onerror=alert(7)>.fasta` produces, in `report.html`:

```html
<div id='total-report-json'>
  {"date":"...","assembliesNames":["ev<img src=x onerror=alert(7)>"],...
```

Verbatim, unescaped, inside HTML text content — a real stored XSS in any page
served without `sandbox`. The cause is in QUAST's own source:
`qutils.correct_name` (line 531) sanitizes **contig** names with
`re.sub(r'[^\w\._\-]', '_', ...)` — confirmed, `>ctg_");alert(1);//` becomes
`ctg____alert_1____` — but `correct_asm_label` (line 536) does **not**. It
strips whitespace and truncates. Assembly labels come from filenames, and in
BioFlow the filename is the user's object name.

**[plan decision] Pass `-l` with a fixed, non-user-derived label, and link the
input under a fixed name.** Both, not either:

```python
assembly = _named_link(work, assembly, "assembly.fasta")   # not obj.name
cmd = quast_runner.build_quast_command(..., label="assembly")
```

Verified: `-l assembly` against the same hostile filename yields
`"assembliesNames":["assembly"]` and **no trace of the payload anywhere in
report.html**. This is what makes Phase 6 possible at all.

**Test it as a security property, at this seam.** Assert that the argv carries
the fixed label and that no payload derived from `assembly_name` reaches the
command. A test that only checks "the report renders" passes either way.

The cost is a report that says `assembly` rather than the user's filename.
Acceptable: the object name is on the object, one click away, and the report
is reached *from* that object.

### The applier

`_apply_assess_misassemblies`, a near-copy of `_apply_assess_completeness`:
facts merged onto the object, `None` object logged and ignored. Register it in
the `_APPLIERS` dict at the bottom of `results.py` — a handler with no applier
runs and silently discards its result.

Facts the handler adds beyond the parsers':
`assembly_misassembly_tool`, `_tool_version` (from `tool.version`),
`assembly_reference_id`, `assembly_reference_name`, `assembly_misassembly_min_contig`.

**Those provenance facts are not optional.** Every number here is a claim about
*this assembly relative to that reference*; a misassembly count against a
different-species reference measures real biology as error, and reads as a
defect. `--min-contig` belongs with them because two runs at different cutoffs
are not comparable.

**Green gate:** `./backend/run-worktree-tests.sh tests/queue -q`

---

## Phase 4 — the launch path

**Files:** `backend/app/services/pipeline_service.py`,
`backend/app/api/v1/pipelines.py`, tests for both

`launch_misassembly_qc(*, draft_object_id, reference_object_id, owner)`.

Validation is already written — this slice adds none. Reuse
`reference_assembly.check_draft_assembly` and `check_reference_assembly`,
which between them enforce READY, `FormatKind.FASTA`, and the `PROTEIN` /
`TRANSCRIPT` exclusions that stop `protein.faa` and `cds_from_genomic.fna`
being treated as genomes. Reuse the same-project check `launch_align` does.

**[plan decision] Reject `draft == reference` explicitly**, with its own
message. Nothing else in the validation stack catches it, and QUAST would
happily report a perfect assembly — the most misleading possible success.

**[plan decision] Do not expose `--min-contig`** (spec open question). Keep
QUAST's default of 500, store the value in facts. Exposing it invites
incomparable runs across one project; storing it means the contig count in the
report can be explained when it disagrees with the object's own.

Endpoint `POST /pipelines/misassemblies`, shaped like `/pipelines/scaffold`.

**Green gate:** `./backend/run-worktree-tests.sh tests/services tests/api -q`

---

## Phase 5 — the Actions card

**Files:** `backend/app/services/suggestion_service.py`,
`backend/tests/services/test_suggestion_service.py`

`build_misassembly_card(obj, references)`, anchored on the assembly, with
`references` resolved by the orchestrator exactly as `build_scaffold_card`
already receives it.

**[plan decision] Copy `build_scaffold_card`'s ambiguity rule verbatim** — this
is the spec's first open question, and RagTag already answered it for the
identical input shape. No references → unavailable, naming what to add. More
than one → unavailable, pointing at the dialog. Exactly one → available, with
`why=f"Reference: {reference.name}."`. Cards carry no chooser, because
`SuggestionCard.launch.body` must be a complete request.

This fires often. A project holding both the GCA and GCF genomic FASTA for one
organism is the ordinary case, not an edge case — the real yeast project in
this database is exactly that. So the dialog is where most launches will
actually happen, and shipping the card without it would be shipping a card
that is usually grey.

**[plan decision] The reference candidate list must exclude the draft itself.**
An assembly BioFlow produced carries `ObjectRole.REFERENCE`
(`results.py:1246`), so a de novo assembly *is* in the reference pool. Without
the exclusion, a project with one assembly and no other reference offers to
QUAST it against itself.

`category="ASSEMBLY_QC"`, matching the completeness card.

**Tests.** Assert the card flips to **unavailable** when `tools.quast` is
patched off. The image ships tools installed, so the "available" direction
passes whether or not the patch worked — that is the standing trap in this
repo, and the registry-vs-`spec_for` variant of it does not apply here only
because this slice deliberately has no registry (below).

**No registry.** The spec's finding stands: `assembly_qc_registry` models
*completeness* — `CompletenessTool`, `CompletenessToolSpec`, an `odb` field
that is meaningless for a tool with no ortholog database — despite a docstring
promising CRAQ and Merqury a home there. Forcing QUAST in means a nullable
`odb` and a `spec_for` returning two incompatible shapes. **Update that
docstring in this phase** to say what the module actually is, so the next
person does not inherit the same wrong promise.

**Check the rule against the real database before believing the tests**, per
CLAUDE.md — the Actions tab's rules have shipped green-but-wrong twice:

```bash
docker compose exec api python -c "..."   # from the MAIN checkout
```

**Green gate:** `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q`

---

## Phase 6 — the HTML report

**Files:** `backend/app/queue/assembly_qc_handlers.py`,
`backend/app/api/v1/pipelines.py`, frontend detail panel

Only attempt this phase after Phase 3's label fix has landed.

**QUAST's report is useless without JavaScript.** Every value lives in a JSON
blob inside `<div id='total-report-json'>` and is rendered into tables by
inline script; there is no static fallback. Under `get_qc_report`'s default
CSP (`sandbox; default-src 'none'; img-src 'self' data:; style-src
'unsafe-inline'`) the page renders empty. That is the same shape as fastp's
charts, except fastp still shows its tables and QUAST shows nothing.

So QUAST joins NanoPlot as a scripting exception, and the branch in
`get_qc_report` grows a third arm:

```python
elif parts[0] == "quast":
    csp = ("default-src 'none'; img-src 'self' data:; "
           "style-src 'unsafe-inline'; script-src 'unsafe-inline'")
```

**No external origin at all** — unlike NanoPlot, which needs
`https://cdn.plot.ly`. QUAST inlines everything, verified: the only outbound
`href` in the report is a link to QUAST's own homepage.

The docstring must record why this is defensible, because the next reader will
correctly note that dropping `sandbox` is what makes an XSS exploitable:

1. **Contig names are sanitized by QUAST** — `qutils.correct_name`,
   `[^\w\._\-]` → `_`, verified against `>ctg_");alert(1);//`. (`\w` is
   unicode-aware in Python 3, so unicode letters survive; none of them form a
   tag.)
2. **The assembly label is not sanitized by QUAST** — and is therefore fixed
   by us at the handler, Phase 3. That is the load-bearing half.
3. **No external origin is permitted**, so the CSP is strictly tighter than
   NanoPlot's.

Copy `report.html`, `icarus.html` and `icarus_viewers/` into
`qc_reports/<object_id>/quast/` and record `assembly_misassembly_report`
relative to the report dir — `get_qc_report`'s `root` already includes the
object id once, and a fact that repeats it names a path nothing was written to.
Also copy `contigs_reports/<label>.misassemblies.gff`: it carries
per-breakpoint coordinates and types, which is what makes a count actionable.

Frontend: a link beside the existing completeness display, opened in a new tab
like every other report — never an inline iframe.

**Green gate:** the suite, then Phase 7 — this phase's real test is a browser.

---

## Phase 7 — verify against the running app

```bash
./ops/worktree-up.sh          # UI on 5273, API on 8100
```

Not `docker compose` from this worktree; the hook will block it, and it would
otherwise repoint the 5173 stack at this branch.

Against the real seeded yeast project:

1. The card appears on an assembly, and names the reference it would use.
2. A run completes and the misassembly facts land on the object.
3. The report opens, **renders tables** (the CSP check — a blank page means
   Phase 6's branch is not matching the path), and Icarus loads.
4. **Name an object `<img src=x onerror=alert(1)>` and run it.** The report
   must render with no alert and no injected element. This is the regression
   test for Phase 3 that no unit test can fully stand in for.
5. Facts panel shows no duplicate N50 — one contiguity number per object.

Then `./ops/worktree-up.sh --down`, and confirm 5173 still serves main:

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

---

## Closing out

`docs/TODO.md`'s post-assembly QC entry has a "Still open" list whose first
bullet is QUAST's reference-based misassembly detection. When this lands, that
bullet is resolved — but **the entry stays in `docs/TODO.md`**, because CRAQ,
GCI and Merqury are still open under it. Only a fully closed entry moves to
`docs/TODO-done.md`.

Record in the entry: what shipped, where the code lives, and the delta from
this plan. Two numbers worth carrying over if they hold — the 8.6 MB install
and the 3–4 s yeast runtime — since the original entry's claim was that QUAST
was too expensive to be worth it.

## Open questions this plan does not close

- **`--large` / `-e` for eukaryotes.** Untested; the yeast run was fast enough
  not to need it. Measure on something vertebrate-sized before the resource
  estimator claims anything about runtime.
- **Whether the card should be pair-anchored.** Phase 5 anchors on the draft
  and copies RagTag's ambiguity rule, which is right for now but does not
  scale: a project with five assemblies and two references has ten possible
  runs and this shows two cards. First place in the app where a card is
  genuinely about a *pair*; worth revisiting when a second such card appears
  rather than designing for it now.
