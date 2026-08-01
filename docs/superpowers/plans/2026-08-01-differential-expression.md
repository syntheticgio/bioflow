# Differential Expression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Per CLAUDE.md: **nothing ticks these boxes automatically.** Do not read an
> unchecked box as "not done" or a checked one as "done" — verify against the
> code by grepping for the symbol the task names.

**Goal:** An RNA-seq differential expression vertical: aligned reads → per-sample
gene counts (featureCounts) → a negative-binomial test across a user-specified
design (PyDESeq2) → a results tab with volcano, MA, and sample-clustering plots.

**Architecture:** Two pipelines, deliberately split. **Quantify** is per-sample
and fits the existing object-centric model exactly — one BAM in, one counts file
out, offered as an Actions-tab card like Align and Call variants.
**Differential expression** is the first pipeline in this codebase that fans
*in*: N counts objects plus a design matrix produce one result. That step gets
no suggestion card (see "Decisions" below) and is launched from the Computations
section.

**Tech Stack:** Python 3.12 / FastAPI / Beanie(MongoDB) / subread 2.0.8 /
PyDESeq2 0.5.4 / pytest — React 18 / TypeScript / TanStack Query / Vite

---

## Orientation for someone new to this repo

Read this before Task 1.

**This plan is being implemented in a worktree.** That changes two commands
from what most other plans in this directory say, and getting either wrong
produces results that silently describe *main's* code instead of yours.

Run the app — never bare `docker compose` from a worktree (a `PreToolUse` hook
blocks it, for good reason):

```bash
./ops/worktree-up.sh          # UI on 5273, API on 8100
./ops/worktree-up.sh --down   # stop it, delete its volumes
```

Run the tests:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

`docker compose exec api python -m pytest` is **wrong here** — the `api`
container bind-mounts the main checkout, so it tests the wrong tree and says
nothing about it.

**The worker does not hot-reload.** `api` runs `uvicorn --reload` and `web`
runs `vite dev`, so their changes land on the next request. `worker` runs
`python -m app.worker_main` and keeps executing whatever it loaded at process
start. After changing any handler, restart the worktree stack's worker —
otherwise a job appears to run with your fix while silently executing the old
code, which reads as "the fix didn't work."

**How a pipeline job flows**, the shape every backend task here fills in:

1. `api/v1/pipelines.py` — HTTP route, validates the request body
2. `services/pipeline_service.py` — checks preconditions, resolves blobs, enqueues
3. `queue/*_handlers.py` — runs in a worker **thread**, so it *cannot touch the
   database*; it returns a plain dict
4. `queue/results.py` — runs on the event loop, merges that dict into MongoDB
   via the `_APPLIERS` table at the bottom of the file

**The reference implementation** for the quantify half is variant calling; for
the results tab it is the variant results tab. When a step here is ambiguous,
read and mirror: `pipelines/variant_runner.py`,
`queue/variant_handlers.py`, `services/pipeline_service.py:1534`
(`launch_variant_calling`), `queue/results.py:1284` (`_apply_call_variants`),
`components/VariantResults.tsx`, `components/VariantCharts.tsx`,
`components/VariantTable.tsx`.

---

## Decisions already made, and what was rejected

Recorded because each has a plausible alternative that someone will re-propose.

**featureCounts, not salmon/kallisto — for now.** featureCounts consumes the BAM
that HISAT2 already produces, so it inherits the entire existing align chain and
the `ObjectRole.ANNOTATION` objects already in the library. Salmon needs a
transcriptome FASTA (a second reference type), its own index sidecar role, and a
tximport equivalent to reach gene level. It is the better fast path and worth
adding later; it is the wrong thing to build the first vertical on.

**PyDESeq2, not r-bioc-deseq2.** Both are in Debian trixie / on PyPI. Measured:
`apt-get install -s --no-install-recommends r-bioc-deseq2` is **110 packages**
plus a full R runtime, in an image whose Dockerfile already carries arm64
special-casing. `pip install --dry-run pydeseq2` is **28 packages** (0.5.4), of
which numpy/scipy/pandas are already present via NanoPlot; anndata, zarr, h5py,
scikit-learn and matplotlib are the new weight. Same negative-binomial GLM, and
results arrive as a DataFrame rather than a TSV written by another language.
The honest cost: R DESeq2 is what a reviewer expects cited, and PyDESeq2's
`lfcShrink` coverage is narrower.

**No suggestion card for DE.** A card's contract, per its own docstring, is
"what can *this file* be run through next," and DE is not an action on one file.
Attaching it to a counts object would produce a card that is identical whichever
counts file you launch it from — a menu item wearing a suggestion's clothes.
Rejected alternatives: hanging it off a counts object (dishonest, and invisible
until a quantify run has finished), and adding a project-level card surface
(the right model, but the largest single piece of frontend work in the feature —
revisit once DE exists and it is clear whether it is wanted).

**Per-sample counts files, merged at DE time — not one multi-BAM featureCounts
invocation.** featureCounts accepts N BAMs and emits a matrix directly, which is
its standard usage and guarantees consistent gene order for free. Rejected
anyway, because it would make quantify an N-input job and thereby drag the
fan-in problem one stage earlier, costing the per-object card. Per-sample keeps
quantify inside the existing model and makes adding a twelfth sample cost one
job instead of twelve. The price is that the DE job must **verify** the gene
sets agree before merging rather than assuming it — see Task 3.2, which is not
optional.

---

## Phase 1 — Tools and vocabulary

- [ ] **1.1 Install subread and PyDESeq2 in `backend/Dockerfile`.** `subread`
      from apt (2.0.8, provides `featureCounts`); `pydeseq2` via pip. Follow the
      existing Dockerfile's comment convention of saying *why* a tool comes from
      where it does. Check whether the arm64 branch needs anything — subread is
      arch-any in Debian but confirm rather than assume.

- [ ] **1.2 Add probes to `pipelines/tools.py`.** `featurecounts()` and a
      PyDESeq2 version probe. Note that PyDESeq2 is a library, not a binary, so
      it does not fit `_probe()`'s `shutil.which` model — decide whether to
      report it via an import-and-read-`__version__` path or to leave it out of
      the tool panel entirely. Whichever: say so in a comment, because the next
      person will wonder.

- [ ] **1.3 Add `PipelineType.EXPRESSION`** to the enum in `tools.py`.

- [ ] **1.4 Fill in `TOOL_META` for both.** `homepage`, `citation`, `license`,
      `usage` are **required** — `test_every_tool_is_documented` fails without
      them, which is the point. Verify license and citation against each
      project's own repository rather than recalling them; a wrong license claim
      on a page that reads as authoritative is worse than a blank field. Write
      `usage` as behaviour, not flags. Set `runnable` correctly and check
      `queue/pipeline_handlers.py` for what actually dispatches rather than
      trusting any surrounding comment.

- [ ] **1.5 Add the new roles and run vocabulary.**
      - `ObjectRole.COUNTS` and `ObjectRole.DE_RESULTS` in `models/object.py` —
        both are anonymous TSV, so format cannot distinguish them, which is
        exactly the criterion the `ObjectRole` docstring sets out. Document each
        in that file's established style.
      - `RunKind.QUANTIFY`, `RunKind.DIFFERENTIAL_EXPRESSION` in `models/run.py`
      - `RunJobRole.QUANTIFY`, `RunJobRole.TEST`
      - `RunInputRole.COUNTS`

- [ ] **1.6 Handle `PipelineRun.label` for N inputs.** Today's labels read
      `"specimen_R1.fastq.gz -> ecoli_ref.fna"`. A DE run needs something like
      `"12 samples — treated vs control"`. Find the label-building code and give
      it a branch rather than letting a 12-input run produce a 200-character
      label.

## Phase 2 — Quantify (per-sample, fits the existing model)

- [ ] **2.1 `pipelines/counts_runner.py`.** Mirror `variant_runner.py`: a params
      dataclass with `from_dict`, defaults, and the command construction.
      Inputs are one BAM plus one annotation object.

- [ ] **2.2 Resolve the strandedness parameter carefully.** featureCounts `-s`
      takes 0/1/2 (unstranded / forward / reverse) and it **must agree with the
      library prep** — a mismatch does not error, it silently returns counts
      near zero. HISAT2 already carries an `rna_strandness` param
      (`aligner_registry.py:286`), so derive the default from the BAM's
      alignment provenance where it exists rather than asking the user twice.
      Where it does not exist, default to unstranded and say so in the dialog.
      **Write a test for the mapping in both directions.**

- [ ] **2.3 Resolve the annotation attribute keys against a real file.** The
      default `-t exon -g gene_id` is GTF-shaped. NCBI Datasets ships **GFF3**,
      whose attributes differ, so the default will produce zero or nonsense
      counts on the annotations this app actually downloads. Per CLAUDE.md,
      check this against a real object in the database, not a fixture:

      ```bash
      docker compose exec api python -c "..."   # from the MAIN checkout
      ```

      Pick defaults that work on what `download_assembly` produces, and record
      the finding in a comment — this is the single most likely source of a
      "ran fine, results are empty" report.

- [ ] **2.4 `queue/expression_handlers.py` — the `quantify` handler.** Register
      with `@handler`, `HandlerMode.SUBPROCESS`. Runs in a thread: no database
      access, return a plain dict. Import it from `handlers.py` for the
      registration side effect. Reuse `_prepare_workdir`, `_named_link`,
      `_failure` from `pipeline_handlers.py` and `_resolve_blob` from
      `align_handlers.py`.

- [ ] **2.5 `_apply_quantify` in `queue/results.py`** plus its `_APPLIERS`
      entry. Register the output object with `ObjectRole.COUNTS`, set
      `derived_from` to `[bam_id, annotation_id]` and `produced_by_job`. Record
      the annotation identity and the strandedness in the object's `facts` —
      Task 3.2 needs the former to refuse an inconsistent merge, and a counts
      file whose strandedness is unknowable is a counts file you cannot trust
      later.

- [ ] **2.6 `launch_quantify` in `services/pipeline_service.py`,** mirroring
      `launch_variant_calling:1534`. Refuse with actionable `ValidationError`s
      rather than enqueueing doomed work: no annotation in the project, BAM not
      from a splice-aware aligner, tool unavailable.

- [ ] **2.7 `POST /pipelines/quantify`** in `api/v1/pipelines.py`, plus a
      defaults endpoint if the dialog needs one.

- [ ] **2.8 `build_quantify_card` in `services/suggestion_service.py`.** This is
      the step that is silently skippable and must not be skipped: registering a
      tool does not make a card appear, and the failure mode is a card reading
      "no quantifier is installed" sitting beside an installed quantifier. Add
      the case to `backend/tests/services/test_suggestion_service.py` — and note
      the documented trap: the image ships tools as installed, so a test
      asserting a card is *available* passes whether or not the patch worked.
      Assert the card flips to **unavailable** when the probe is patched off.

## Phase 3 — Differential expression (the fan-in)

- [ ] **3.1 `pipelines/de_runner.py`.** The design representation, the contrast,
      and the PyDESeq2 call. Keep it importable and testable without a queue.

- [ ] **3.2 Merge N counts files, and refuse an inconsistent merge.** Join on
      gene id across the selected counts objects. If two inputs were quantified
      against different annotations, or their gene sets disagree beyond a
      trivial margin, **raise rather than inner-joining** — a silent inner join
      is how you get a DE result computed over the intersection of two gene
      universes without anyone knowing. This is the cost of the per-sample
      decision recorded above and it is the reason that decision is safe.

- [ ] **3.3 Validate the design before enqueueing.** PyDESeq2 needs ≥2
      replicates per condition; a design with a singleton group should fail in
      the launch path with a sentence naming the offending group, not in a
      worker thread twenty seconds later. Also refuse: fewer than 2 conditions,
      a contrast naming a condition not present, duplicate sample entries.

- [ ] **3.4 Design matrix from object metadata.** `DataObject.metadata` is
      already an open user-owned dict with a `SchemaMetadataEditor` and a
      `BulkEditBar` for editing many objects at once. Read a `condition` key as
      the **default** grouping when the dialog opens; the user's choice in the
      dialog wins and is stored in `PipelineRun.params`. No new collection, no
      new document type — revisit a first-class `Design` document only if this
      proves insufficient.

- [ ] **3.5 The `differential_expression` handler** in
      `queue/expression_handlers.py`. `HandlerMode.THREAD` (PyDESeq2 is
      in-process, not a subprocess). Long-running on large matrices — use
      `ctx.extend_lease` and report progress through `ctx`.

- [ ] **3.6 `_apply_differential_expression`** + `_APPLIERS` entry. Output
      object with `ObjectRole.DE_RESULTS`; `derived_from` lists every counts
      input. Store bounded summary numbers (genes tested, genes passing padj
      threshold, direction split) in `facts`; write the full per-gene table to
      disk. Follow the variant results tab's precedent on where the line
      between `facts` and on-disk detail falls.

- [ ] **3.7 `launch_differential_expression`, `POST /pipelines/differential-expression`,**
      and a results-fetch endpoint for the table.

## Phase 4 — Frontend

- [ ] **4.1 `QuantifyDialog.tsx`,** modelled on `VariantDialog.tsx`. Annotation
      picker, strandedness with the derived default visible and overridable.

- [ ] **4.2 `DifferentialExpressionDialog.tsx`.** The new interaction: multi-
      select counts objects, assign each to a condition (pre-filled from the
      `condition` metadata key), choose the contrast. Show the replicate count
      per group so the ≥2 rule is visible *before* submitting rather than as a
      rejection afterwards.

- [ ] **4.3 Wire DE into `Computations.tsx`** — a button beside the existing
      ones. This is the entry point; there is deliberately no Actions card.

- [ ] **4.4 `ExpressionResults.tsx`** with volcano and MA plots, mirroring
      `VariantCharts.tsx`.

- [ ] **4.5 Sample-clustering plot (PCA or sample-distance heatmap).** The one
      genuinely new chart, and the most valuable: it is how a mislabelled sample
      is caught before anyone believes a single gene call.

- [ ] **4.6 Results table,** mirroring `VariantTable.tsx` — sortable on padj and
      log2FC, paginated, filterable.

- [ ] **4.7 Manual verification at localhost:5273** (the worktree stack), which
      per CLAUDE.md is the actual verification step for anything UI-facing —
      there is no headless component-testing setup and none is expected. Run a
      real multi-sample dataset end to end: reads → trim → HISAT2 → quantify ×N
      → DE.

## Phase 5 — Close out

- [ ] **5.1 `sources.py`** if anything here reads an external service (it may
      not — check, and skip honestly if not). It has its own completeness test.

- [ ] **5.2 Check the Software help page** at `/help/software` renders both new
      tools correctly.

- [ ] **5.3 Full suite:** `./backend/run-worktree-tests.sh tests/ -q`.

- [ ] **5.4 Update `docs/TODO.md`** in the same commit or the one right after.
      Per CLAUDE.md this has already gone wrong three times, once leaving advice
      to delete code that four handlers were calling. Any entry this work
      resolves gets ` — FIXED` appended to its heading and a short note saying
      what shipped, when, and where the code lives — keeping the original body.
      Say what the implementation did **differently from this plan**; every
      entry closed so far departed from its plan somewhere, and that delta is
      the most valuable sentence in the entry.

---

## Traps, collected

The ones most likely to cost an hour, in the order you will meet them:

1. **Bare `docker compose` from this worktree** silently repoints the main
   stack. Use `./ops/worktree-up.sh`.
2. **`docker compose exec api pytest` from this worktree** tests main's code and
   gives no error saying so. Use `./backend/run-worktree-tests.sh`.
3. **The worker does not hot-reload.** A handler change not followed by a worker
   restart reads as "the fix didn't work."
4. **featureCounts on NCBI GFF3 with GTF-shaped defaults** returns empty counts
   without failing (Task 2.3).
5. **Strandedness mismatch** returns near-zero counts without failing (Task 2.2).
6. **Registering a tool does not create a card** — `suggestion_service.py` is
   hand-maintained (Task 2.8).
7. **A suggestion test asserting "available" passes even when its patch does
   not work,** because the image ships tools installed. Assert the unavailable
   direction.
8. **A silent inner join across inconsistent gene sets** produces a plausible
   DE result over the wrong universe (Task 3.2).
